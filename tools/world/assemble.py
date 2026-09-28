"""Merge the China network and the rest-of-world network into the files the site loads.

    python3 tools/world/assemble.py [--world build/world/network.json build/world/geometry.pickle]
                                    [--china build/china] [--out-data data] [--out-build build]
                                    [--overrides tools/world/ref/overrides.json] [--prev <previous build dir>] [--lean DIR]

  --prev   keep the line and station ids of that build (its ids.json, below); without it (the first build, or to
           start over) ids are positional as they always were: by country, then build order
  --lean   read the way lists of the lines from DIR (world_lines.pickle, china_lines.pickle: made there on the first
           run from the geometry pickles, remade when they change) instead of loading the geometry pickles: no
           build/geometry.pickle then, only build/lines.pickle (make_tiles.py takes the ways from its --store)

Overrides (tools/world/ref/overrides.json): hand-checked corrections applied before anything else
  {"lines": [{"note": "why",
              "match": <OSM relation id of a route in a world line>  |  "match_cn": "<native name>|<kind>" (China lines,
                        every line of that name and kind),
              "merge": [relation ids (world) / "<native>|<kind>" (China) of lines folded into it: their stops continue
                        its stop list where they meet it, else become a branch; their track and length are added],
              "set":   {"<line field index>": value, ...}   (FORMAT.md line fields, e.g. "4": "#E31837" colour),
              "drop":  true   (hide the line: it leaves the site, its id becomes a tombstone)}],
   "logos": {key: {name, url}}}      logos the "14" (logo key) of a "set" may name

Stable ids (--prev): a line keeps its id when it has the most OSM route relations in common with a line of the
previous build (world), else the same native name, kind and terminal names, else the only line of that name and
kind (China, and world lines whose relations were remapped), in the same country; a station keeps its id when
the previous build had one of that native name within 300 m (unnamed: 30 m) in the same country. Ids no line or
station takes stay as tombstones (a line hidden with no stations and country -1, a station with no lines and
country -1: the site never reaches them); new lines and stations get new ids in one more shard per country
(<iso><n>.json after the country's others). The shard table of the previous build is kept as it is.
  build/ids.json          {lines: [[iso, "native|kind|first|last", [relation ids]]], stations: [[iso, native, lon,
                          lat, kind]], shards} per id: what the next build matches against

Inputs
  build/china/network.json, build/china/geometry.pickle   China pipeline (tools/build_network.py + curate.py)
  build/world/network.json, build/world/geometry.pickle   tools/world/build_world.py (optional)

Outputs (see tools/world/FORMAT.md)
  data/index.json         countries, cities, shard table, totals        (loaded at start)
  data/net/<shard>.json   lines and stations of one country (or part)  (loaded on demand)
  data/search.json        names for search                             (loaded on first search)
  build/full.json         every line / station / city / country with global ids (for make_tiles.py)
  build/geometry.pickle   {'geometry': [way ids per global line id], 'ways': {way id: (coords, tags, refs)}}
  build/lines.pickle      {'geometry': [way ids per global line id], 'src': the geometry pickles' sizes and dates}
                          (with --lean)
  data/logos.json        the hand-checked logos (China) plus an entry for every "wd:Q…" logo key the lines use:
                          {name, url} with the 120 px Commons thumbnail or English Wikipedia infobox logo, else
                          {name, wiki} (tools/world/wikidata.py caches), so the site makes no Wikidata calls for them

Where the pipelines meet (Urumqi, Suifenhe), a world station at a China station (the same node, or the same
name within 150 m) becomes that China station. A world station the 1:10m borders put in China whose lines
all run in the neighbouring country a few km away is filed there (Hyesan on the Yalu). A city takes the
country most of its urban lines' stations are in, and so do its urban lines that have stations there (the
Jerusalem Light Rail is one system).
"""
import json, math, os, pickle, re, sys, time
from collections import Counter, defaultdict
import numpy as np

ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
def arg(name, default): return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
B = arg('--out-build', os.path.join(ROOT, 'build'))
DATA = arg('--out-data', os.path.join(ROOT, 'data'))
CHINA = arg('--china', os.path.join(ROOT, 'build', 'china'))
LOGOS = os.path.join(ROOT, 'data', 'logos.json')
SHARD_BYTES = 1_200_000     # target size of one shard file (raw JSON)

sys.path.insert(0, os.path.dirname(__file__))
from countries import country_code, COUNTRIES as _COUNTRIES

# ---------------------------------------------------------------- inputs
SYSTEM = [(r'地铁', 'Metro'), (r'(?:城市)?轨道交通', 'Rail Transit'), (r'市域(?:轨道交通|铁路)|市郊铁路', 'Suburban Railway'),
          (r'(?:现代)?有轨电车', 'Tram'), (r'云巴', 'SkyShuttle')]
def china_operator(l, C, logos):
    """Operator and logo key of a China line, from its OSM network / operator tags: the city system that a
    "<city>地铁…" tag names (its data/logos.json name and logo when it has one), else the tag as mapped, else ''."""
    net, opr = (l[12] if len(l) > 12 else '') or '', (l[13] if len(l) > 13 else '') or ''
    txt = ' '.join((net, opr, l[1]))
    if re.search(r'港鐵|輕鐵', net) or '香港鐵路' in opr: return 'MTR', 'Hong Kong'
    if '香港電車' in txt: return 'Hong Kong Tramways', 'hktram'
    if '澳門輕軌' in net: return 'Macau LRT', 'Macau'
    if re.search(r'珠三角城际|广东城际|粤港澳大湾区城际', txt): return 'Guangdong Intercity', 'prdir'
    if l[0] in 'hr' or '中国铁路' in txt: return 'China Railway', 'cr'
    for zh, en in sorted(((c[0], c[1]) for c in C if c[1]), key=lambda c: -len(c[0])):
        tag = next((t for t in (net, opr) if t.startswith(zh)), '')
        kind = next((k for p, k in SYSTEM if re.match(p, re.sub(r'^市', '', tag[len(zh):]))), None) if tag else None
        if not kind: continue
        logo = logos.get(en, {}).get('name', '')
        if kind in ('Metro', 'Rail Transit') and logo or kind == 'Tram' and 'Tram' in logo: return logo, en
        return f'{en} {kind}', ''
    city = C[l[5]] if 0 <= l[5] < len(C) else None
    logo = logos.get(city[1], {}).get('name', '') if city else ''
    if not (net or opr) and logo and (l[0] == 'm' or 'Tram' in logo): return logo, city[1]
    m = re.fullmatch(r'.*[一-鿿）)]\s+([A-Za-z][\x20-\x7e’]*)', net or opr)    # "山頂纜車有限公司 Peak Tramways Company Limited"
    op = m.group(1) if m else net or opr
    return op, next((k for k, v in logos.items() if v.get('name') == op), '')
def china_ref(names, logos):
    """The logo key tools/world/ref/operators.json "china" gives the first of these names (the operator, the network and
    operator tags, "City|kind") that it maps: a data/logos.json key, else the first of its Wikidata items with a known
    logo ("wd:Q…"); '' when none."""
    import wikidata as wd
    if 'china' not in _REF: _REF['china'] = json.load(open(wd.REF)).get('china', {}) if os.path.exists(wd.REF) else {}
    for n in names:
        v = _REF['china'].get(n) if n else None
        for x in ([v] if isinstance(v, str) else v or []):
            if has_logo(x, logos): return x
            if re.fullmatch(r'Q\d+', x) and wd.op(x)[1]: return 'wd:' + x
    return ''
_REF = {}
def has_logo(key, logos):
    """A hand-checked logo with a url, or whose English article's infobox logo is known."""
    import wikidata as wd
    v = logos.get(key) or {}
    return bool(v.get('url') or v.get('wiki') and wd.wiki_logo(v['wiki']))

def china():
    """China network (v1 records) -> v2 records, hidden lines None. Stations keep the country the pipeline
    gave them (CN, HK, MO: not the 1:10m borders). Every city is kept, those without shown lines too,
    so make_tiles.py can still rank the stations named after them."""
    d = json.load(open(os.path.join(CHINA, 'network.json')))
    L, S, C = d['lines'], d['stations'], d['cities']
    logos = json.load(open(LOGOS)) if os.path.exists(LOGOS) else {}
    lines = []
    for l in L:
        if l[10]: lines.append(None); continue
        op, key = china_operator(l, C, logos)
        if not has_logo(key, logos):   # no hand-checked logo: the curated one of its system, operator, tags or city and kind
            key = china_ref([key, op, l[12] if len(l) > 12 else '', l[13] if len(l) > 13 else '', f"{C[l[5]][1] if 0 <= l[5] < len(C) else ''}|{l[0]}"], logos) or key
            if key and not op: op = logos[key]['name'] if key in logos else wikidata_name(key)
        lines.append(list(l[:12]) + ['', op, key])
    stations = [list(s[:8]) + [s[8] if len(s) > 8 else ''] for s in S]
    cities = [list(c[:6]) + ['', c[1] if has_logo(c[1], logos) else china_ref([c[1]], logos) or (c[1] if c[1] in logos else '')] for c in C]
    return lines, stations, cities

def wikidata_name(key):
    import wikidata as wd
    return wd.op(key[3:])[0] if key.startswith('wd:') else ''

def world(path):
    d = json.load(open(path))
    return d['lines'], d['stations'], d['cities']

def metres(a, b):
    return math.hypot((a[2] - b[2]) * math.cos(math.radians((a[3] + b[3]) / 2)), a[3] - b[3]) * 111000

def main_cluster(pts, gap_km=500, share=0.05):
    """The points of the clusters (single linkage over 2° cells, gaps under ~gap_km) that hold at least
    `share` of them: drops overseas outliers such as Guadeloupe for France, Hawaii and Alaska for the US."""
    cell = defaultdict(list)
    for x, y in pts: cell[(int(x // 2), int(y // 2))].append((x, y))
    keys = list(cell); parent = {k: k for k in keys}
    def find(k):
        while parent[k] != k: parent[k] = parent[parent[k]]; k = parent[k]
        return k
    cen = {k: np.mean(cell[k], axis=0) for k in keys}
    for a in keys:
        for b in keys:
            (x1, y1), (x2, y2) = cen[a], cen[b]
            if a < b and math.hypot((x1 - x2) * math.cos(math.radians((y1 + y2) / 2)), y1 - y2) * 111 < gap_km + 250:
                parent[find(a)] = find(b)
    size = Counter()
    for k in keys: size[find(k)] += len(cell[k])
    big = {r for r, n in size.items() if n >= share * len(pts)} or {size.most_common(1)[0][0]}
    return [p for k in keys if find(k) in big for p in cell[k]]

# hand-checked corrections (tools/world/ref/overrides.json, schema above): world lines by OSM relation id, China lines by
# "native|kind"; merge lines into another (its extra stops continue the main list where they meet it), set fields, drop
OVERRIDES = json.load(open(arg('--overrides', os.path.join(os.path.dirname(__file__), 'ref', 'overrides.json'))))
def overrides(L, G, key, find):
    """Apply the overrides matched by `key` ('match' or 'match_cn') to the lines L of that pipeline in place; find: the
    line indices of each match value."""
    for o in OVERRIDES['lines']:
        if key not in o: continue
        if not find.get(o[key]): print('override:', o[key], 'not in the build'); continue
        for a in find[o[key]]:
            la = L[a]
            if la is None: continue
            if o.get('drop'): la[10] = 1; continue
            for b in (b for r in o.get('merge', []) for b in find.get(r, [])):
                if b == a or L[b] is None or L[b][10]: continue
                lb, main = L[b], L[a][6]
                extra = [s for s in lb[6] if s not in main]
                if main and main[-1] in lb[6]: la[6] = main + [s for s in lb[6][lb[6].index(main[-1]) + 1:] if s not in main]
                elif main and main[0] in lb[6]: la[6] = [s for s in lb[6][:lb[6].index(main[0])] if s not in main] + main
                elif extra: la[7] = la[7] + [lb[6]]
                la[9] = (la[9] or 0) + (lb[9] or 0) * len(extra) // max(1, len(lb[6]))
                G['geometry'][a] = list(G['geometry'][a]) + [w for w in G['geometry'][b] if w not in set(G['geometry'][a])]
                lb[10] = 1
            for k, v in o.get('set', {}).items(): la[int(k)] = v
# a pale grey or white line colour (a service colour chosen for a dark timetable, e.g. #DCDDDE) vanishes on the light map:
# drop it so the line takes its kind's colour
def pale(c):
    if not c or len(c) != 7: return False
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return min(r, g, b) > 0.78 and max(r, g, b) - min(r, g, b) < 0.12

# ---- stable ids (--prev)
PREV = arg('--prev', None)
TOMB_L = ['', '', '', '', '', -1, [], [], 0, 0, 1, '', -1, '', '']      # an id no line takes any more: hidden, nowhere
TOMB_S = ['', '', 0, 0, '', [], [], -1, -1]
def stable(lkey, skey):
    """New ids of the lines and stations (provisional index -> id) keeping the previous build's where they match (see
    the top), and the ids.json of this build (keys per id, tombstones keep theirs; shards: the previous ones and one
    more for each country with new ids)."""
    P = json.load(open(PREV if PREV.endswith('.json') else os.path.join(PREV, 'ids.json')))
    PL, PS = P['lines'], P['stations']
    lnew, taken = {}, set()
    def claim(new, cand):          # cand: (score, provisional, old id), best first
        for _, o, i in sorted(cand):
            if o not in new and i not in taken: new[o] = i; taken.add(i)
    by_rel = {r: i for i, k in enumerate(PL) for r in k[2]}
    claim(lnew, [(-n, o, i) for o, k in lkey.items() for i, n in Counter(by_rel[r] for r in k[2] if r in by_rel).items() if PL[i][0] == k[0]])
    for loose in (False, True):  # the same name, kind and terminals (in order); then the only line of a name and kind
        f = (lambda k: (k[0],) + tuple(k[1].split('|')[:2])) if loose else (lambda k: (k[0], k[1]))
        old, new = defaultdict(list), defaultdict(list)
        for i, k in enumerate(PL):
            if i not in taken: old[f(k)].append(i)
        for o, k in lkey.items():
            if o not in lnew: new[f(k)].append(o)
        for key, os_ in new.items():
            if not loose or len(os_) == len(old.get(key, ())) == 1: claim(lnew, [(0, o, i) for o, i in zip(os_, old.get(key, ()))])
    snew, taken = {}, set()
    grid = defaultdict(list)
    for i, k in enumerate(PS): grid[(k[0], k[1], math.floor(k[3] * 100))].append(i)
    cand = []
    for o, k in skey.items():
        c = math.cos(math.radians(k[3]))
        for dy in (-1, 0, 1):
            for i in grid.get((k[0], k[1], math.floor(k[3] * 100) + dy), ()):
                d = math.hypot((PS[i][2] - k[2]) * c, PS[i][3] - k[3]) * 111000
                if d < (300 if k[1] else 30): cand.append((d + 50 * (PS[i][4] != k[4]), o, i))
    claim(snew, cand)
    ids = {'lines': list(PL), 'stations': list(PS), 'shards': [list(x) for x in P['shards']]}
    fresh = defaultdict(lambda: ([], []))          # country -> its new lines, stations
    for o, k in lkey.items():
        if o in lnew: ids['lines'][lnew[o]] = k
        else: fresh[k[0]][0].append(o)
    for o, k in skey.items():
        if o in snew: ids['stations'][snew[o]] = k
        else: fresh[k[0]][1].append(o)
    for cc in sorted(fresh):
        ls, ss = fresh[cc]; n = cc.lower() or 'xx'
        num = [re.fullmatch(re.escape(n) + r'(\d*)\.json', s[0]) for s in ids['shards']]
        num = [int(m.group(1) or 0) for m in num if m]
        ids['shards'].append([f'{n}{max(num) + 1}.json' if num else f'{n}.json', len(ids['lines']), len(ls), len(ids['stations']), len(ss)])
        for o in ls: lnew[o] = len(ids['lines']); ids['lines'].append(lkey[o])
        for o in ss: snew[o] = len(ids['stations']); ids['stations'].append(skey[o])
    print('stable ids: lines kept', len(lkey) - sum(len(v[0]) for v in fresh.values()), 'new', sum(len(v[0]) for v in fresh.values()),
          'tombstones', len(ids['lines']) - len(lkey), '· stations kept', len(skey) - sum(len(v[1]) for v in fresh.values()),
          'new', sum(len(v[1]) for v in fresh.values()), 'tombstones', len(ids['stations']) - len(skey), '· new shards', len(fresh))
    return lnew, snew, ids

LEAN = arg('--lean', None)
def geometry_of(name, path):
    """A geometry pickle; with --lean only its way lists ('ways' None), from DIR/<name>_lines.pickle when that was made
    from this file (else made now)."""
    src = f'{os.path.getsize(path)}:{os.stat(path).st_mtime_ns}'
    cache = os.path.join(LEAN, f'{name}_lines.pickle') if LEAN else None
    if cache and os.path.exists(cache):
        c = pickle.load(open(cache, 'rb'))
        if c['src'] == src: return {'geometry': c['geometry'], 'ways': None, 'src': src}
    G = pickle.load(open(path, 'rb')); G['src'] = src
    if cache:
        os.makedirs(LEAN, exist_ok=True)
        pickle.dump({'src': src, 'geometry': G['geometry']}, open(cache + '.tmp', 'wb'), protocol=5); os.replace(cache + '.tmp', cache)
    return G

def main():
    t0 = time.time()
    parts = [('china', china(), geometry_of('china', os.path.join(CHINA, 'geometry.pickle')))]
    find = defaultdict(list)
    for i, l in enumerate(parts[0][1][0]):
        if l is not None: find[f'{l[1]}|{l[0]}'].append(i)
    overrides(parts[0][1][0], parts[0][2], 'match_cn', find)
    rels = {}                            # world line -> its OSM route relations (build_world.py sources.json)
    if '--world' in sys.argv:
        i = sys.argv.index('--world')
        parts.append(('world', world(sys.argv[i + 1]), geometry_of('world', sys.argv[i + 2])))
        sources = os.path.join(os.path.dirname(sys.argv[i + 1]), 'sources.json')
        if os.path.exists(sources):
            rels = dict(enumerate(json.load(open(sources))))
            overrides(parts[-1][1][0], parts[-1][2], 'match', {r: [i] for i, rs in rels.items() for r in rs})
    # ---- concatenate with provisional ids
    lines, stations, cities, geometry, ways, lrels = [], [], [], [], {}, []
    for name, (L, S, C), G in parts:
        lo, so, co = len(lines), len(stations), len(cities)
        keep = [i for i, l in enumerate(L) if l is not None and not l[10]]
        lmap = {i: lo + j for j, i in enumerate(keep)}
        for i in keep:
            l = list(L[i])
            if pale(l[4]): l[4] = ''
            l[5] = co + l[5] if l[5] is not None and l[5] >= 0 else -1
            l[6] = [so + s for s in l[6]]; l[7] = [[so + s for s in b] for b in l[7]]
            lines.append(l); geometry.append(list(G['geometry'][i])); lrels.append(rels.get(i, []) if name == 'world' else [])
        for s in S:
            s = list(s)
            s[5] = [lmap[x] for x in s[5] if x in lmap]
            s[6] = [so + t for t in s[6]]
            s[7] = so + s[7] if s[7] is not None and s[7] >= 0 else -1
            stations.append(s)
        for c in C: cities.append(list(c))
        if G['ways'] is not None: ways.update(G['ways'])
        if name == 'china': china_s, china_c = len(stations), len(cities)
    # ---- one station where the pipelines meet: a world station at a China station becomes that station
    grid = defaultdict(list)
    for i in range(china_s):
        if stations[i][8] in ('CN', 'HK', 'MO'): grid[(round(stations[i][2] * 100), round(stations[i][3] * 100))].append(i)
    def zh(n): return re.sub(r'\s.*|站$', '', n or '')
    same = {}
    for w in range(china_s, len(stations)):
        s = stations[w]; gx, gy = round(s[2] * 100), round(s[3] * 100)
        near = [(metres(s, stations[c]), c) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for c in grid.get((gx + dx, gy + dy), ())]
        near = [(d, c) for d, c in near if d < 30 or d < 150 and zh(s[0]) and zh(s[0]) == zh(stations[c][0])]
        if near: same[w] = min(near)[1]
    for l in lines:
        l[6] = [same.get(s, s) for s in l[6]]; l[7] = [[same.get(s, s) for s in b] for b in l[7]]
    for s in stations:
        s[6] = [same.get(t, t) for t in s[6]]
        s[7] = same.get(s[7], s[7])
    # a station's lines are the lines that list it (curate.py drops repeated stops without updating their stations)
    for s in stations: s[5] = []
    for i, l in enumerate(lines):
        for s in dict.fromkeys(l[6] + [x for b in l[7] for x in b]): stations[s][5].append(i)
    print('lines', len(lines), 'stations', len(stations), 'cities', len(cities), 'merged border stations', len(same),
          round(time.time() - t0), 's', flush=True)

    # ---- drop stations no visible line uses (keep complex / transfer partners of used ones)
    used = set()
    for l in lines:
        used.update(l[6]); [used.update(b) for b in l[7]]
    keep_s = set(used)
    for s in used:
        st = stations[s]
        keep_s.update(t for t in st[6] if stations[t][5])
        if st[7] >= 0: keep_s.add(st[7])
    # ---- countries
    for i in keep_s:
        s = stations[i]
        if not s[8]: s[8] = country_code(s[2], s[3])
    for l in lines:        # the country most of its stations are in; a tie goes to the middle station's
        if not l[12]:
            cc = Counter(stations[s][8] for s in l[6] if stations[s][8]).most_common()
            top = [c for c, n in cc if n == cc[0][1]] if cc else []
            mid = stations[l[6][len(l[6]) // 2]][8] if l[6] else ''
            l[12] = (mid if mid in top else top[0]) if top else (cities[l[5]][6] if l[5] >= 0 else '')
    ring = [(0.03 * math.cos(a), 0.03 * math.sin(a)) for a in np.arange(8) * math.pi / 4]
    for i in keep_s:       # a world station the 1:10m borders put in China (Hyesan on the Yalu): its lines' country
        s = stations[i]; ccs = {lines[x][12] for x in s[5]}
        if i >= china_s and s[8] in ('CN', 'HK', 'MO') and len(ccs) == 1 and s[8] not in ccs \
                and any(country_code(s[2] + dx, s[3] + dy) in ccs for dx, dy in ring):
            s[8] = next(iter(ccs))
    by_city = defaultdict(Counter)
    for l in lines:
        if l[5] >= 0 and l[0] not in 'hr': by_city[l[5]].update(stations[s][8] for s in l[6] if stations[s][8])
    for ci, c in enumerate(cities):
        if by_city[ci]: c[6] = by_city[ci].most_common(1)[0][0]
        elif not c[6]: c[6] = country_code(c[2], c[3])
    for l in lines:
        if l[5] >= 0 and l[0] not in 'hr' and l[12] != cities[l[5]][6]:
            if sum(stations[s][8] == cities[l[5]][6] for s in l[6]) >= 0.25 * len(l[6]): l[12] = cities[l[5]][6]
    ccs = sorted({l[12] for l in lines} | {c[6] for c in cities} | {stations[i][8] for i in keep_s})
    ccs = [c for c in ccs if c]
    cid = {c: i for i, c in enumerate(ccs)}

    # ---- global order: by country, stations and lines contiguous per shard
    s_order = sorted(keep_s, key=lambda i: (stations[i][8], i))
    l_order = sorted(range(len(lines)), key=lambda i: (lines[i][12], i))
    snew = {o: n for n, o in enumerate(s_order)}; lnew = {o: n for n, o in enumerate(l_order)}
    lkey = {o: [lines[o][12], '|'.join([lines[o][1], lines[o][0]] + [stations[x][0] for x in lines[o][6][:1] + lines[o][6][-1:]]), lrels[o]]
            for o in l_order}
    skey = {o: [stations[o][8], stations[o][0], stations[o][2], stations[o][3], stations[o][4]] for o in s_order}
    ids = {'lines': [lkey[o] for o in l_order], 'stations': [skey[o] for o in s_order], 'shards': None}
    if PREV: lnew, snew, ids = stable(lkey, skey)
    c_used = sorted({lines[i][5] for i in range(len(lines)) if lines[i][5] >= 0} | set(range(china_c)))
    cnew = {o: n for n, o in enumerate(c_used)}
    L2, S2, G2 = [TOMB_L] * len(ids['lines']), [TOMB_S] * len(ids['stations']), [[]] * len(ids['lines'])
    for o in l_order:
        l = list(lines[o])
        l[5] = cnew.get(l[5], -1)
        l[6] = [snew[s] for s in l[6] if s in snew]; l[7] = [[snew[s] for s in b if s in snew] for b in l[7]]
        l[10] = 0; l[12] = cid.get(l[12], -1)
        L2[lnew[o]] = l; G2[lnew[o]] = geometry[o]
    for o in s_order:
        s = list(stations[o][:9])
        s[5] = [lnew[x] for x in s[5] if x in lnew]
        s[6] = [snew[t] for t in s[6] if t in snew]
        s[7] = snew.get(s[7], -1) if s[7] >= 0 else -1
        s[8] = cid.get(s[8], -1)
        S2[snew[o]] = s
    L2 = [list(l) for l in L2]; S2 = [list(s) for s in S2]
    members = defaultdict(list)          # station 9 on a complex's main station: all its members, itself included
    for i, s in enumerate(S2):
        if s[7] >= 0: members[s[7]].append(i)
    for rep, m in members.items():
        if S2[rep][7] == rep: S2[rep].append(m)
    C2 = []
    for o in c_used:
        c = list(cities[o]); c[6] = cid.get(c[6], -1); c[5] = 0; C2.append(c)
    for l in L2:
        if l[5] >= 0 and l[0] not in 'hr': C2[l[5]][5] += 1

    # ---- countries table; the view box covers the main clusters of stations, not overseas outliers
    countries = []
    for cc in ccs:
        info = _COUNTRIES.get(cc, {})
        countries.append([cc, info.get('name', cc), '', 0, 0, 0, [180, 90, -180, -90]])
    for l in L2:
        c = countries[l[12]] if l[12] >= 0 else None
        if not c: continue
        if l[0] in 'hr': c[3] += 1
        else: c[4] += 1
    for ci in C2:
        if ci[6] >= 0 and ci[5] > 0: countries[ci[6]][5] += 1
    pts = defaultdict(list)
    for s in S2:
        if s[8] >= 0 and s[5]: pts[s[8]].append((s[2], s[3]))
    for k, p in pts.items():
        x, y = zip(*main_cluster(p))
        countries[k][6] = [round(v, 3) for v in (min(x), min(y), max(x), max(y))]

    # ---- logos: the hand-checked ones and the Wikidata logos the lines and cities use, resolved from the caches
    logos = {k: v for k, v in (json.load(open(LOGOS)) if os.path.exists(LOGOS) else {}).items() if not k.startswith('wd:')}
    logos.update(OVERRIDES.get('logos', {}))
    names = defaultdict(Counter)
    for l in L2: names[l[14]][l[13]] += 1
    import wikidata as wd
    for v in logos.values():       # hand-checked {name, wiki}: the article's infobox logo when it was looked up
        if v.get('wiki') and not v.get('url') and wd.wiki_logo(v['wiki']): v['url'] = wd.wiki_logo(v['wiki'])
    keys = sorted(k for k in set(names) | {c[7] for c in C2} if k.startswith('wd:'))
    for k in keys:
        en, url = wd.op(k[3:]); wiki = '' if url else wd.wiki(k[3:])
        url = url or wd.wiki_logo(wiki) or ''    # the article's infobox logo, looked up once (wikidata.py wikilogos)
        name = next((n for n, _ in names[k].most_common() if n), en)
        if url or wiki and wd.wiki_logo(wiki) is None: logos[k] = {'name': name, 'url': url} if url else {'name': name, 'wiki': wiki}
    miss = {k for k in keys if k not in logos}          # no logo anywhere: no key, so the site does not look for one
    for l in L2:
        if l[14] in miss: l[14] = ''
    for c in C2:
        if c[7] in miss: c[7] = ''
    os.makedirs(DATA, exist_ok=True)
    json.dump(logos, open(os.path.join(DATA, 'logos.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
    print('logos', len(logos), 'wikidata', sum(k.startswith('wd:') for k in logos), '(wiki only', sum(1 for v in logos.values() if 'wiki' in v),
          ') keys without a logo, cleared', len(miss))

    # ---- shards: per country, split by size; lines of a country go with its stations (--prev: the previous
    # build's shards, and one more per country for its new ids)
    os.makedirs(os.path.join(DATA, 'net'), exist_ok=True)
    for f in os.listdir(os.path.join(DATA, 'net')): os.remove(os.path.join(DATA, 'net', f))
    by_c_l = defaultdict(list); by_c_s = defaultdict(list)
    for i, l in enumerate(L2): by_c_l[l[12]].append(i)
    for i, s in enumerate(S2): by_c_s[s[8]].append(i)
    shards = []
    def sz(x): return len(json.dumps(x, ensure_ascii=False, separators=(',', ':')).encode())
    for fn, l0, nl, s0, ns in ids['shards'] or []:
        json.dump({'l0': l0, 's0': s0, 'lines': L2[l0:l0 + nl], 'stations': S2[s0:s0 + ns]},
                  open(os.path.join(DATA, 'net', fn), 'w'), ensure_ascii=False, separators=(',', ':'))
        shards.append([fn, l0, nl, s0, ns])
    for c in sorted(set(by_c_l) | set(by_c_s)) if ids['shards'] is None else []:
        ls, ss = by_c_l.get(c, []), by_c_s.get(c, [])
        total = sum(sz(L2[i]) for i in ls) + sum(sz(S2[i]) for i in ss)
        n = max(1, math.ceil(total / SHARD_BYTES))
        cc = ccs[c].lower() if c >= 0 else 'xx'
        for k in range(n):
            lpart = ls[len(ls) * k // n: len(ls) * (k + 1) // n]
            spart = ss[len(ss) * k // n: len(ss) * (k + 1) // n]
            fn = f'{cc}{k}.json' if n > 1 else f'{cc}.json'
            l0 = lpart[0] if lpart else 0; s0 = spart[0] if spart else 0
            json.dump({'l0': l0, 's0': s0, 'lines': [L2[i] for i in lpart], 'stations': [S2[i] for i in spart]},
                      open(os.path.join(DATA, 'net', fn), 'w'), ensure_ascii=False, separators=(',', ':'))
            shards.append([fn, l0, len(lpart), s0, len(spart)])

    # ---- search index: visible lines, one station per complex / name cluster, and the complex members
    # with names of their own (Luohu and Lo Wu, Wan Chai); cities
    sl = [[i, l[2], l[1], l[3], l[5], l[0], l[12]] for i, l in enumerate(L2) if not l[10]]
    seen, ss, nmain = set(), [], 0
    for i, s in enumerate(S2):
        if not s[5]: continue
        main = s[7] < 0 or s[7] == i
        key = (s[0], s[4], round(s[2] * 20), round(s[3] * 20)) if main else (s[0], s[7])
        if key in seen or not main and s[0] == S2[s[7]][0]: continue
        seen.add(key); nmain += main
        ss.append([i, s[1] if s[1] != s[0] else '', s[0], s[4], len(s[5]), s[8]])
    json.dump({'l': sl, 's': ss}, open(os.path.join(DATA, 'search.json'), 'w'), ensure_ascii=False, separators=(',', ':'))

    stats = {'urban': sum(1 for l in L2 if l[0] not in 'hr' and not l[10]), 'intercity': sum(1 for l in L2 if l[0] in 'hr' and not l[10]),
             'stations': nmain, 'cities': sum(1 for c in C2 if c[5] > 0), 'countries': sum(1 for c in countries if c[3] or c[4])}
    date = os.path.join(ROOT, 'build', 'DATE')
    date = open(date).read().strip() if os.path.exists(date) else ''
    json.dump({'v': 2, 'date': date, 'stats': stats, 'countries': countries, 'cities': C2, 'shards': shards,
               'nLines': len(L2), 'nStations': len(S2)},
              open(os.path.join(DATA, 'index.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
    os.makedirs(B, exist_ok=True)
    json.dump({'lines': L2, 'stations': S2, 'cities': C2, 'countries': countries},
              open(os.path.join(B, 'full.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
    ids['shards'] = shards
    json.dump(ids, open(os.path.join(B, 'ids.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
    src = ' '.join(G['src'] for *_, G in parts)
    if all(G['ways'] is not None for *_, G in parts):
        with open(os.path.join(B, 'geometry.pickle'), 'wb') as f:
            pk = pickle.Pickler(f, protocol=5); pk.fast = True     # no memo: it would take GBs for the planet's ways
            pk.dump({'geometry': G2, 'ways': ways, **({'src': src} if LEAN else {})})
    elif os.path.exists(os.path.join(B, 'geometry.pickle')): os.remove(os.path.join(B, 'geometry.pickle'))
    if LEAN: pickle.dump({'geometry': G2, 'src': src}, open(os.path.join(B, 'lines.pickle'), 'wb'), protocol=5)
    print('assembled', stats, 'shards', len(shards), round(time.time() - t0), 's')

if __name__ == '__main__':
    main()
