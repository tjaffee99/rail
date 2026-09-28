"""Coverage and audit report: how much of each country's main-line track the site's lines use, every stretch they
miss, and what the OSM data says about each line's track and stations.

    python3 tools/world/coverage_report.py <raw.pickle> <build dir (full.json, lines.pickle, ids.json)> <out.json> [PREV build dir]

Needs the raw planet pickle (~10 GB in memory: run under the heavy lock). For each country (China excluded: its own pipeline):
  total_km, covered_km  main-line track (railway=rail/narrow_gauge, no service tag) and the part the lines use
  gaps                  connected stretches no line uses, longest first: km, the track's names, usage/traffic tags, the
                        named passenger stations within 150 m of it, the route relations (railway / train) its ways belong
                        to (a relation the builder rejected), a point on it; stretches along used track (second tracks)
                        are left out
  lines                 each line: kind, names, operator, km, stations, its OSM relations (negative: a synthetic relation
                        made from unmapped track, with 'syn_ways', way ids of its own track, for drops.json "w<id>" keys),
                        its track's tags (km by usage / traffic mode / passenger / electrified, names, operators) and its
                        stations' tags (how many carry passenger evidence), 'new' when the PREV build did not have it
"""
import json, math, pickle, re, sys, time
from collections import Counter, defaultdict
import numpy as np
sys.path.insert(0, __file__.rsplit('/', 1)[0])
from countries import country_code

T0 = time.time()
def log(*a): print(f'{time.time() - T0:7.1f}s', *a, flush=True)
BUILD = sys.argv[2]
full = json.load(open(BUILD + '/full.json'))
ids = json.load(open(BUILD + '/ids.json'))
geo = pickle.load(open(BUILD + '/lines.pickle', 'rb'))['geometry']
prev_n = len(json.load(open(sys.argv[4] + '/ids.json'))['lines']) if len(sys.argv) > 4 else None
prev_live = None
if len(sys.argv) > 4:
    pf = json.load(open(sys.argv[4] + '/full.json'))
    prev_live = {i for i, l in enumerate(pf['lines']) if l[12] != -1 and l[1]}
    del pf
CC = [c[0] for c in full['countries']]
log('build loaded', len(full['lines']), 'lines')
raw = pickle.load(open(sys.argv[1], 'rb')); W, N, R = raw['ways'], raw['stations'], raw['relations']
log('raw loaded', len(W), 'ways')
used = {int(w) for ws in geo for w in ws}
log('ways used by lines', len(used))

def is_main(t): return t.get('railway') in ('rail', 'narrow_gauge') and not t.get('service')
def km(c):
    c = np.asarray(c); lat = math.radians(float(c[:, 1].mean()))
    return float(np.hypot(np.diff(c[:, 0]) * 111.32 * math.cos(lat), np.diff(c[:, 1]) * 110.57).sum())
_ccg = {}
def wcc(c):
    p = c[len(c) // 2]; k = (round(float(p[0]), 2), round(float(p[1]), 2))
    if k not in _ccg: _ccg[k] = country_code(*k)
    return _ccg[k]

main = {w: v for w, v in W.items() if is_main(v[0])}
tot, cov, cc_of = Counter(), Counter(), {}
for w, (t, c, refs) in main.items():
    cc = cc_of[w] = wcc(c); k = km(c); tot[cc] += k
    if w in used: cov[cc] += k
log('main ways', len(main), 'countries', len(tot))

# cells of used track (second tracks of mapped lines are not gaps)
CELL = 0.002
def dense(c, step=0.0005):
    """The way's points every ~step degrees (vectorised: no Python loop over segments)."""
    c = np.asarray(c, dtype=np.float64)
    if len(c) < 2: return c
    n = np.maximum(1, (np.abs(np.diff(c, axis=0)).max(1) / step).astype(np.int64))
    idx = np.repeat(np.arange(len(n)), n)
    t = (np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n) + 1) / np.repeat(n, n)
    return np.concatenate([c[:1], c[idx] + (c[idx + 1] - c[idx]) * t[:, None]])
def cellkeys(pts): return (np.floor(pts[:, 0] / CELL).astype(np.int64) << 32) + np.floor(pts[:, 1] / CELL).astype(np.int64)
ucells = np.unique(np.concatenate([cellkeys(dense(W[w][1])) for w in used if w in W]))
log('used cells', len(ucells))
cand = [w for w in main if w not in used and cc_of[w] not in ('CN', 'HK', 'MO')]
pl = [dense(W[w][1]) for w in cand]
wi = np.repeat(np.arange(len(cand)), [len(p) for p in pl])
hit = np.isin(cellkeys(np.concatenate(pl)), ucells)          # one call: never per way against millions of cells
share = np.bincount(wi, weights=hit, minlength=len(cand)) / np.maximum(np.bincount(wi, minlength=len(cand)), 1)
free = [w for w, f in zip(cand, share.tolist()) if f <= 0.6]
del pl, wi, hit
log('uncovered main ways (not along used track)', len(free))

# connected stretches
par = {}
def f(x):
    while par.get(x, x) != x: par[x] = par.get(par[x], par[x]); x = par[x]
    return x
by_node = defaultdict(list)
for w in free:
    refs = W[w][2]
    for n in (int(refs[0]), int(refs[-1])): by_node[n].append(w)
for ws in by_node.values():
    for w in ws[1:]:
        a, b = f(w), f(ws[0])
        if a != b: par[a] = b
comp = defaultdict(list)
for w in free: comp[f(w)].append(w)

# station grid, way -> route relations
PAX = lambda t: t.get('train') == 'yes' or t.get('public_transport') == 'station'
SG = defaultdict(list)
for n, (lon, lat, t) in N.items():
    if t.get('name') and (t.get('railway') in ('station', 'halt') or t.get('public_transport') == 'station') and \
            not any(t.get(k) for k in ('disused', 'abandoned')):
        SG[(int(lon / 0.01), int(lat / 0.01))].append(n)
way_rels = defaultdict(set)
for rid, r in R.items():
    if r['tags'].get('type') == 'route' and r['tags'].get('route') in ('railway', 'train'):
        for typ, ref, role in r['members']:
            if typ == 'w': way_rels[ref].add(rid)
def stations_near(ws):
    got = {}
    for w in ws:
        for x, y in dense(W[w][1], 0.001).tolist():
            for n in SG.get((int(x / 0.01), int(y / 0.01)), ()):
                lon, lat, t = N[n]
                if n not in got and math.hypot((lon - x) * 111320 * math.cos(math.radians(lat)), (lat - y) * 110540) < 150:
                    got[n] = (t.get('name:en') or t.get('name'), t.get('name'), t.get('railway'), t.get('usage') or t.get('station') or '', bool(PAX(t)))
    return list(got.values())

out = defaultdict(lambda: {'total_km': 0, 'covered_km': 0, 'gaps': [], 'lines': []})
for cc in tot: out[cc]['total_km'] = round(tot[cc]); out[cc]['covered_km'] = round(cov[cc])
for ws in comp.values():
    L = sum(km(W[w][1]) for w in ws)
    if L < 5: continue
    cc = Counter(cc_of[w] for w in ws).most_common(1)[0][0]
    names = Counter()
    for w in ws: names[W[w][0].get('name') or '(no name)'] += km(W[w][1])
    tags = Counter()
    for w in ws:
        t = W[w][0]
        for k in ('usage', 'railway:traffic_mode', 'passenger', 'railway', 'electrified', 'gauge', 'operator'):
            if t.get(k): tags[f'{k}={t[k]}'] += 1
    rels = Counter(r for w in ws for r in way_rels.get(w, ()))
    mid = W[ws[len(ws) // 2]][1]; p = mid[len(mid) // 2]
    st = stations_near(ws)
    out[cc]['gaps'].append({'km': round(L, 1), 'names': [(n, round(k, 1)) for n, k in names.most_common(5)], 'tags': dict(tags.most_common(10)),
                            'stations': [s[0] for s in st][:60], 'n_stations': len(st), 'n_pax_tagged': sum(s[4] for s in st),
                            'relations': [(r, R[r]['tags'].get('route'), R[r]['tags'].get('name:en') or R[r]['tags'].get('name'), n)
                                          for r, n in rels.most_common(6)],
                            'at': [round(float(p[0]), 4), round(float(p[1]), 4)], 'ways': len(ws), 'way': int(min(ws))})
for v in out.values(): v['gaps'].sort(key=lambda g: -g['km'])
log('gaps', sum(len(v['gaps']) for v in out.values()))

# the lines: their track's and stations' tags
node_at = {}
for n, (lon, lat, t) in N.items():
    if t.get('railway') in ('station', 'halt') or t.get('public_transport') == 'station': node_at[(round(lon, 5), round(lat, 5))] = t
S = full['stations']
for i, l in enumerate(full['lines']):
    if l[12] == -1 or not l[1] and not l[2]: continue
    cc = CC[l[12]]
    if cc in ('CN', 'HK', 'MO'): continue
    ws = [w for w in geo[i] if w in W]
    trk = Counter(); names = Counter(); ops = Counter(); total = 0.0
    for w in ws:
        t = W[w][0]; k = km(W[w][1]); total += k
        for key in ('usage', 'railway:traffic_mode', 'passenger', 'service', 'electrified', 'railway'):
            if t.get(key): trk[f'{key}={t[key]}'] += k
        if t.get('name'): names[t['name']] += k
        if t.get('operator'): ops[t['operator']] += k
    st = Counter(); sts = [S[s] for s in l[6]] + [S[s] for b in l[7] for s in b]
    for s in sts:
        t = node_at.get((round(s[2], 5), round(s[3], 5)))
        if t is None: st['not matched'] += 1; continue
        st['station' if t.get('railway') == 'station' else 'halt' if t.get('railway') == 'halt' else 'other'] += 1
        for key in ('train', 'public_transport', 'ref', 'railway:ref', 'uic_ref', 'wikidata', 'operator', 'usage', 'station', 'passenger'):
            if t.get(key): st[key if key not in ('train', 'usage', 'station', 'passenger') else f'{key}={t[key]}'] += 1
    rels = ids['lines'][i][2] if i < len(ids['lines']) else []
    rec = {'id': i, 'kind': l[0], 'native': l[1], 'en': l[2], 'ref': l[3], 'operator': l[13], 'km': l[9], 'loop': l[8],
           'stations': [S[s][1] or S[s][0] for s in l[6]], 'branches': [[S[s][1] or S[s][0] for s in b] for b in l[7]],
           'ends': [[S[l[6][0]][2], S[l[6][0]][3]], [S[l[6][-1]][2], S[l[6][-1]][3]]] if l[6] else [],
           'rels': rels, 'track_km': round(total), 'track': {k: round(v) for k, v in trk.most_common(12)},
           'track_names': [(n, round(k)) for n, k in names.most_common(4)], 'track_operators': [(n, round(k)) for n, k in ops.most_common(3)],
           'station_tags': dict(st)}
    if any(r < 0 for r in rels): rec['syn_ways'] = sorted(w for w in ws if w not in way_rels and w in main)[:3]
    if prev_live is not None: rec['new'] = i not in prev_live
    out[cc]['lines'].append(rec)
json.dump(out, open(sys.argv[3], 'w'), ensure_ascii=False)
log('countries', len(out), 'gaps', sum(len(v['gaps']) for v in out.values()), 'lines', sum(len(v['lines']) for v in out.values()))
top = sorted(((v['total_km'] - v['covered_km'], cc) for cc, v in out.items()), reverse=True)[:30]
for miss, cc in top:
    v = out[cc]
    print(f"{cc} total {v['total_km']} km, covered {v['covered_km']} ({100 * v['covered_km'] // max(1, v['total_km'])}%), gaps {len(v['gaps'])} "
          f"({round(sum(g['km'] for g in v['gaps']))} km), lines {len(v['lines'])}, new {sum(bool(x.get('new')) for x in v['lines'])}")
