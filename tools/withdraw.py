"""Withdraw lines from the published site data without a rebuild (a service that closed after the last build).

    python3 tools/withdraw.py <line id> [<line id> ...] [--root .]

Works on the deployed tree (data/): the lines become tombstones in their shard (the record assemble.py leaves for an id
nothing takes), and so do their stations that no other line serves (the others lose the withdrawn line ids); the
search index, stats, country and city counts follow; in the rail tiles, the track features only withdrawn lines use and
those stations' features are removed, other features lose the ids. Changed tile chunks are written to one new
rail_<n>.bin and the manifest points at them (the old files stay: chunks not changed still live there). The next full
build should drop the same lines (tools/world/ref/drops.json), so that it agrees.
Needs: pip install mapbox-vector-tile.
"""
import glob, gzip, json, math, os, struct, sys
import mapbox_vector_tile
from shapely.geometry import shape

args = [a for a in sys.argv[1:] if not a.startswith('--')]
ROOT = sys.argv[sys.argv.index('--root') + 1] if '--root' in sys.argv else '.'
if '--root' in sys.argv: args.remove(ROOT)
W = {int(a) for a in args}
D = os.path.join(ROOT, 'data')
ix = json.load(open(os.path.join(D, 'index.json')))
LINE_TOMB = ['', '', '', '', '', -1, [], [], 0, 0, 1, '', -1, '', '']
STN_TOMB = ['', '', 0, 0, '', [], [], -1, -1]

def shard_of(lid=None, sid=None):
    for f, l0, lc, s0, sc in ix['shards']:
        if lid is not None and l0 <= lid < l0 + lc or sid is not None and s0 <= sid < s0 + sc: return f

# ---- records
shards = {}
def load(f):
    f = f if f.endswith('.json') else f + '.json'          # (the shard table names the files)
    if f not in shards: shards[f] = json.load(open(os.path.join(D, 'net', f)))
    return shards[f]
gone_lines, touched_stations, pts = {}, set(), []
for lid in sorted(W):
    d = load(shard_of(lid=lid)); l = d['lines'][lid - d['l0']]
    if not l[1] and l[12] == -1: print('already withdrawn:', lid); continue
    gone_lines[lid] = list(l); d['lines'][lid - d['l0']] = list(LINE_TOMB)
    touched_stations.update(l[6]); touched_stations.update(s for b in l[7] for s in b)
gone_stations = set()
for sid in touched_stations:
    d = load(shard_of(sid=sid)); s = d['stations'][sid - d['s0']]
    pts.append((s[2], s[3]))
    left = [x for x in s[5] if x not in W]
    if left: s[5] = left
    else: gone_stations.add(sid); d['stations'][sid - d['s0']] = list(STN_TOMB)
for f, d in shards.items():          # transfers / complexes pointing at a withdrawn station
    for s in d['stations']:
        s[6] = [x for x in s[6] if x not in gone_stations]
        if s[7] in gone_stations: s[7] = -1
for f in glob.glob(os.path.join(D, 'net', '*.json')):
    name = os.path.basename(f)
    if name in shards: continue
    d = json.load(open(f)); ch = False
    for s in d['stations']:
        if set(s[6]) & gone_stations or s[7] in gone_stations:
            s[6] = [x for x in s[6] if x not in gone_stations]; s[7] = -1 if s[7] in gone_stations else s[7]; ch = True
    if ch: shards[name] = d
for f, d in shards.items():
    json.dump(d, open(os.path.join(D, 'net', f), 'w'), ensure_ascii=False, separators=(',', ':'))

# ---- index: stats, country and city counts
st = ix['stats']
for lid, l in gone_lines.items():
    urban = l[0] not in 'hr'
    st['urban' if urban else 'intercity'] -= 1
    c = ix['countries'][l[12]]; c[4 if urban else 3] -= 1
    if urban and l[5] >= 0:
        city = ix['cities'][l[5]]; city[5] -= 1
        if city[5] == 0: c[5] -= 1; st['cities'] -= 1
st['stations'] -= len(gone_stations)
json.dump(ix, open(os.path.join(D, 'index.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
sp = os.path.join(D, 'search.json')
if os.path.exists(sp):
    se = json.load(open(sp))
    se['l'] = [e for e in se['l'] if e[0] not in W]
    se['s'] = [e for e in se['s'] if e[0] not in gone_stations]
    json.dump(se, open(sp, 'w'), ensure_ascii=False, separators=(',', ':'))

# ---- tiles
man_p = os.path.join(D, 'manifest.json'); man = json.load(open(man_p)); m = man['rail']
TD = os.path.join(D, 'tiles')
def tile_xy(lon, lat, z):
    n = 2 ** z; lat = max(-85.05, min(85.05, lat))
    return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
def key_of(z, x, y):
    g = next(i for i, r in enumerate(m['groups']) if r[0] <= z <= r[1]); az = m['groups'][g][2]
    return f'g{g}_{x >> (z - az)}_{y >> (z - az)}'
keys = set()
for z in range(m['minzoom'], m['maxzoom'] + 1):
    for lon, lat in pts:
        x, y = tile_xy(lon, lat, z)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1): keys.add(key_of(z, (x + dx) % 2 ** z, min(max(y + dy, 0), 2 ** z - 1)))
keys = sorted(k for k in keys if k in m['chunks'])
def ids(s): return [int(x) for x in str(s or '').split('|') if x]
def fix(layers):
    changed = False; out = []
    for name, lay in layers.items():
        feats = []
        for f in lay['features']:
            p = dict(f['properties'])
            ls = ids(p.get('ls'))
            if name == 'stn' and p.get('i') in gone_stations: changed = True; continue
            if ls and set(ls) & W:
                rest = [x for x in ls if x not in W]
                if not rest and name == 'rail': changed = True; continue
                p['ls'] = '|' + '|'.join(map(str, rest)) + '|' if rest else ''; changed = True
                if name == 'rail' and p.get('l') in W: p['l'] = rest[0]
            feats.append({'geometry': shape(f['geometry']), 'properties': p})
        if feats: out.append({'name': name, 'features': feats, 'extent': lay['extent']})
    return changed, out
new_chunks = {}
for k in keys:
    fn, off, ln = m['chunks'][k]
    with open(os.path.join(TD, fn), 'rb') as fh: fh.seek(off); buf = fh.read(ln)
    n = struct.unpack('<I', buf[4:8])[0]; base = 8 + 12 * n
    ents = [struct.unpack('<III', buf[8 + 12 * i: 20 + 12 * i]) for i in range(n)]
    tiles, any_ch = [], False
    for tk, toff, tlen in ents:
        raw = buf[base + toff: base + toff + tlen]
        pb = gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw
        layers = mapbox_vector_tile.decode(pb, default_options={'y_coord_down': True})
        ch, out = fix(layers)
        if not ch: tiles.append((tk, raw)); continue
        any_ch = True
        if not out: continue
        ext = out[0]['extent']
        enc = mapbox_vector_tile.encode([{'name': o['name'], 'features': o['features']} for o in out],
                                        default_options={'extents': ext, 'y_coord_down': True, 'quantize_bounds': None, 'on_invalid_geometry': None})
        tiles.append((tk, gzip.compress(enc, compresslevel=9, mtime=0)))
    if any_ch: new_chunks[k] = tiles
if new_chunks:
    nums = [int(os.path.basename(f)[5:-4]) for f in glob.glob(os.path.join(TD, 'rail_*.bin'))]
    out_fn = f'rail_{max(nums) + 1}.bin'; data = b''
    for k, tiles in sorted(new_chunks.items()):
        tiles.sort(); idx, body, o = [], [], 0
        for tk, t in tiles: idx.append(struct.pack('<III', tk, o, len(t))); body.append(t); o += len(t)
        c = b'TPK1' + struct.pack('<I', len(tiles)) + b''.join(idx) + b''.join(body)
        m['chunks'][k] = [out_fn, len(data), len(c)]; data += c
    open(os.path.join(TD, out_fn), 'wb').write(data)
    json.dump(man, open(man_p, 'w'), separators=(',', ':'))
print('withdrawn lines', sorted(gone_lines), [l[2] or l[1] for l in gone_lines.values()], '· stations', len(gone_stations),
      '· chunks rewritten', len(new_chunks), 'of', len(keys), 'checked')
