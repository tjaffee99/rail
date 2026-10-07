"""Generate the rail vector tiles (data/tiles/*.bin + data/manifest.json) for the whole world from
the network assembled by tools/world/assemble.py (see tools/world/FORMAT.md).

    python3 tools/make_tiles.py build/full.json build/geometry.pickle [out dir, default data/]
                                [--store DIR] [--cache build/tiles.cache] [--prev <previous tiles.cache> <previous data/>]

Incremental runs (tools/update.sh):
  --store DIR   the rail network index (every way's nodes as flat arrays, the 0.1° cell grid; about 800 MB of .npy)
                is kept in DIR, made there by a run on a geometry.pickle; a run on assemble.py --lean's build/lines.pickle
                (no ways) memory-maps it instead of loading the 1.7 GB pickle
  --cache FILE  keep what the next run can reuse: each line's routing results (gaps, joins, ends) under a digest of
                everything they read (its stops, their positions, its track as the stage sees it), and each chunk's digest
                (everything make_chunk reads: the chunk's features at each zoom, simplified, their properties and labels)
  --prev C D    reuse the run that made cache C and data dir D: lines whose inputs are unchanged take its routing
                results, chunks whose digest is unchanged are copied byte for byte from D's files, and only the .bin
                files with a new, changed or removed chunk are written again (under new names, rail_<n>.bin after
                D's last; the others keep theirs and their bytes). Decoded tiles are those of a full run on the same input.

Every line in full.json is drawn along its OSM ways, cut to the stretch between its end stops. Layers and properties:
  rail  l primary line id · ls "|id|id|" all lines on this track · k kind (h r m l s t f)
        c colour and r badge text (urban) · nm / nz line name for labels (intercity, z5-11)
        pair 1: one of the two features (one per kind, same geometry) of track that high-speed and other intercity
        lines share; the site draws them side by side. Every intercity line is drawn in its own kind along all its track.
        Track shared by intercity and urban lines is two features, the urban one drawn over the intercity one.
  stn   i station id · n / z names · b second name in "Both" mode · k kind · x lines at the station complex
        c ring colour · rk rank · ls line ids · ks kinds at this node · kc kinds at the complex
        cx complex main station · rep 1 if this node is the complex's main station
z0-2 hold an overview: the intercity network merged and simplified, and the biggest cities' main stations.
Archive: chunks of tiles ("TPK1" + index + gzipped MVT), each the tiles of one zoom group under one ancestor tile
(manifest rail.groups); see app.js getTile(). Routing and tiling use all cores.
"""
import bisect, ctypes, gc, gzip, hashlib, heapq, json, math, os, pickle, re, shutil, struct, sys, time
import multiprocessing as mp
from collections import Counter, defaultdict
import numpy as np
import shapely
import geonamescache
import mapbox_vector_tile
from shapely.geometry import Point

ROOT = os.path.join(os.path.dirname(__file__), '..')
POS, OPT, _a = [], {}, 1
while _a < len(sys.argv):
    _n = {'--store': 1, '--cache': 1, '--prev': 2}.get(sys.argv[_a])
    if _n: OPT[sys.argv[_a]] = sys.argv[_a + 1:_a + 1 + _n]; _a += 1 + _n
    else: POS.append(sys.argv[_a]); _a += 1
OUT = POS[2] if len(POS) > 2 else os.path.join(ROOT, 'data')
T0 = time.time()
def log(*a): print(f'[{time.time() - T0:5.0f}s]', *a, flush=True)
D = json.load(open(POS[0]))
L, S, C, CT = D['lines'], D['stations'], D['cities'], D['countries']
G = pickle.load(open(POS[1], 'rb'))
GEOM, WAYS = G['geometry'], G.get('ways')
STORE = OPT.get('--store', [None])[0]
SRC = open(__file__).read()
def version(a, b=None):          # digest of the code from the section header a to b (or the end)
    a, b = ('# ' + '-' * 64 + ' ' + x if x else None for x in (a, b))
    return hashlib.blake2b(SRC[SRC.index(a):SRC.index(b) if b else None].encode(), digest_size=8).hexdigest()
MINZ, OVERZ, MAXZ, EXT, BUF = 0, 3, 12, 4096, 64
# chunks: [first zoom, last zoom, zoom of the ancestor tile a chunk holds the tiles of]; a view loads a few of up to
# about 500 KB, and a manifest of a few thousand
GROUPS = [[0, 2, 0], [3, 3, 1], [4, 4, 2], [5, 5, 3], [6, 6, 4], [7, 7, 5], [8, 9, 6], [10, 11, 7], [12, 12, 8]]
FORK, NPROC = mp.get_context('fork'), os.cpu_count()
# what a run can reuse of the last one (--prev) holds while the rail network (the geometry pickles assemble.py read) and
# the code of the stages are the same
NEWC = {'src': G.get('src'), 'route': version('rail network index', 'rail features') + hashlib.blake2b(SRC[SRC.index('def visible'):SRC.index('# ' + '-' * 64)].encode(), digest_size=8).hexdigest(),
        'tile': version('tiling') + str((MINZ, OVERZ, MAXZ, EXT, BUF, GROUPS))}
PREVC = pickle.load(open(OPT['--prev'][0], 'rb')) if '--prev' in OPT else {}
DIGEST = '--cache' in OPT or '--prev' in OPT
if PREVC and (not NEWC['src'] or PREVC['src'] != NEWC['src'] or PREVC['route'] != NEWC['route']):
    log('--prev: another rail network or routing code, its routes are not reused'); PREVC.update(gaps={}, joins={}, ends={})
if PREVC and PREVC['tile'] != NEWC['tile']: log('--prev: other tiling code, its chunks are not reused'); PREVC['chunks'] = {}
log('lines', len(L), '· stations', len(S), '· ways', len(WAYS) if WAYS is not None else f'from {STORE}')

def visible(i): return 0 <= i < len(L) and not L[i][10]
def trim():  # hand freed memory back to the system
    try: ctypes.CDLL('libc.so.6').malloc_trim(0)
    except OSError: pass
def settle():  # before forking: free what we can and keep the workers' GC off the shared pages
    gc.collect(); trim(); gc.freeze()
def merc(lon, lat):
    lat = np.clip(lat, -85, 85)
    return (lon + 180) / 360, (1 - np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) / np.pi) / 2
# routing distances: metres on a local equirectangular projection
def cosl(lat): return math.cos(math.radians(lat))
def mxy(p, c): return np.column_stack([p[:, 0] * 111320 * c, p[:, 1] * 110540])
def stop_lists(li): return [q for q in [L[li][6]] + L[li][7] if q]

# ---------------------------------------------------------------- rail network index
# The searches below run on small local graphs: every rail way (sidings too) is kept once in flat
# arrays and bucketed by 0.1° grid cell, and a search builds the graph of the cells around it.
# WAYS then keeps only the other ways (metro, tram...): they move over in blocks, so that memory
# peaks at little more than the loaded pickle.
# With --store the index is saved (with the other ways, as flat arrays) and a run without ways (build/lines.pickle)
# memory-maps it.
CELL = 0.1
STORED = ('RW', 'NV', 'OFF', 'SERV', 'XY', 'REF', 'GK', 'GO', 'GV', 'RWKM', 'OW', 'ONV', 'OOFF', 'OXY', 'OREF')
if WAYS is None:
    if not STORE or json.load(open(os.path.join(STORE, 'meta.json')))['src'] != G['src']:
        sys.exit(f'{POS[1]} has no ways and --store {STORE} was not made from the geometry it came from')
    A = {k: np.load(os.path.join(STORE, k + '.npy'), mmap_mode='r').view(np.ndarray) for k in STORED}
    RW, NV, OFF, SERV, XY, REF, RWKM = (A[k] for k in ('RW', 'NV', 'OFF', 'SERV', 'XY', 'REF', 'RWKM'))
    GRID = dict(zip(A['GK'].tolist(), np.split(np.array(A['GV']), A['GO'].tolist())))
    WAYS = {w: (A['OXY'][o:o + n], None, A['OREF'][o:o + n]) for w, o, n in zip(A['OW'].tolist(), A['OOFF'].tolist(), A['ONV'].tolist())}
else:
    RW = np.array([w for w, v in WAYS.items() if v[1].get('railway') in ('rail', 'narrow_gauge') and len(v[2]) >= 2], np.int64)
    NV = np.array([len(WAYS[w][2]) for w in RW], np.int64)
    OFF = np.cumsum(NV) - NV
    SERV = np.array([bool(WAYS[w][1].get('service')) for w in RW], bool)
    XY, REF = [np.zeros((0, 2))], [np.zeros(0, np.int64)]
    for a in range(0, len(RW), 200000):
        ws = RW[a:a + 200000].tolist()
        XY.append(np.concatenate([WAYS[w][0] for w in ws], dtype=np.float64)); REF.append(np.concatenate([WAYS[w][2] for w in ws], dtype=np.int64))
        for w in ws: del WAYS[w]
        trim()
    XY, REF = np.concatenate(XY), np.concatenate(REF)
IX = dict(zip(RW.tolist(), range(len(RW))))
def coords(w):
    i = IX.get(w)
    return XY[OFF[i]:OFF[i] + NV[i]] if i is not None else np.asarray(WAYS[w][0], np.float64).reshape(-1, 2)
def refs(w):
    i = IX.get(w)
    return REF[OFF[i]:OFF[i] + NV[i]] if i is not None else np.asarray(WAYS[w][2], np.int64)
def known(w): return w in IX or (w in WAYS and len(WAYS[w][0]) >= 2)
def ckey(ix, iy): return (ix + 2000) * 4000 + iy + 1000
def cells(lon0, lat0, lon1, lat1):
    ix = np.arange(math.floor(lon0 / CELL) - 1, math.floor(lon1 / CELL) + 2)
    iy = np.arange(math.floor(lat0 / CELL) - 1, math.floor(lat1 / CELL) + 2)
    return ckey(ix[:, None], iy[None, :]).ravel().tolist()
if 'GRID' not in globals():
    _k = ckey(*np.floor(XY / CELL).astype(np.int64).T) * len(RW) + np.repeat(np.arange(len(RW)), NV)
    _k = np.unique(_k[np.r_[True, _k[1:] != _k[:-1]]])
    _cut = np.flatnonzero(np.diff(_k // len(RW))) + 1
    GRID = dict(zip((_k[np.r_[0, _cut]] // len(RW)).tolist(), np.split(_k % len(RW), _cut))) if len(_k) else {}
    del _k, _cut
    # the length of each line's own track (km), to pick the line a shared stretch is drawn as
    _d = np.hypot(np.diff(XY[:, 0]) * np.cos(np.radians(XY[1:, 1])), np.diff(XY[:, 1])) * 111.2
    _d[OFF[1:] - 1] = 0
    RWKM = np.add.reduceat(np.r_[_d, 0], OFF) if len(RW) else np.zeros(0)
    del _d
    if STORE and G.get('src') and not (os.path.exists(os.path.join(STORE, 'meta.json')) and
                                       json.load(open(os.path.join(STORE, 'meta.json')))['src'] == G['src']):
        os.makedirs(STORE, exist_ok=True)
        ow = list(WAYS); oxy = [np.asarray(WAYS[w][0], np.float64).reshape(-1, 2) for w in ow]
        onv = np.array([len(x) for x in oxy], np.int64); gk = list(GRID)
        A = {'RW': RW, 'NV': NV, 'OFF': OFF, 'SERV': SERV, 'XY': XY, 'REF': REF, 'RWKM': RWKM,
             'GK': np.array(gk, np.int64), 'GO': np.cumsum([len(GRID[k]) for k in gk], dtype=np.int64)[:-1],
             'GV': np.concatenate([GRID[k] for k in gk] or [np.zeros(0, np.int64)]), 'OW': np.array(ow, np.int64), 'ONV': onv,
             'OOFF': np.cumsum(onv) - onv, 'OXY': np.concatenate(oxy or [np.zeros((0, 2))]),
             'OREF': np.concatenate([np.asarray(WAYS[w][2], np.int64) for w in ow] or [np.zeros(0, np.int64)])}
        if os.path.exists(os.path.join(STORE, 'meta.json')): os.remove(os.path.join(STORE, 'meta.json'))
        for k in STORED: np.save(os.path.join(STORE, k + '.npy'), A[k])
        json.dump({'src': G['src']}, open(os.path.join(STORE, 'meta.json'), 'w'))
        del A, oxy
        log('rail network index saved to', STORE)
def way_km(w):
    if w in IX: return float(RWKM[IX[w]])
    c = coords(w); return float(np.hypot(np.diff(c[:, 0]) * np.cos(np.radians(c[1:, 1])), np.diff(c[:, 1])).sum() * 111.2)
LEN = [sum(way_km(w) for w in ways if known(w)) if visible(li) else 0 for li, ways in enumerate(GEOM)]
log('rail network index:', len(RW), 'ways,', len(XY), 'nodes,', len(GRID), 'cells')

# Each routing stage below reads, per line, only its stops, their positions and its own track as the stage sees it
# (and the rail network): with --prev, a line whose inputs have the digest they had in the last run takes that
# run's result, and the main process applies the results in the same order as ever.
def digest(*a): return hashlib.blake2b(pickle.dumps(a, protocol=5), digest_size=16).digest()
def stop_sig(li): return [(q, [S[x][2:4] for x in q]) for q in stop_lists(li)]
def ways_sig(ws): return np.array(list(ws), np.int64).tobytes()
def par(stage, fn, todo, key, chunksize):
    keys = [key(li) for li in todo] if DIGEST else [None] * len(todo)
    old = PREVC.get(stage, {})
    miss = [li for li, k in zip(todo, keys) if k not in old]
    got = {}
    if miss:
        settle()
        with FORK.Pool(NPROC) as pool: got = dict(zip(miss, pool.imap(fn, miss, chunksize=chunksize)))
    res = [got[li] if li in got else old[k] for li, k in zip(todo, keys)]
    if DIGEST: NEWC[stage] = dict(zip(keys, res))
    if PREVC: log(f'{stage}: {len(miss)} of {len(todo)} lines routed, the rest reused')
    return res

def graph(cks, c, service, keep=None):
    """Graph of the rail ways in some grid cells (service tracks at 1.5x their length, or left out;
    keep: nodes allowed) -> node refs (sorted), positions (m), adjacency (ptr, node, length, way)."""
    ws = [GRID[k] for k in cks if k in GRID]
    if not ws: return None
    ws = np.unique(np.concatenate(ws))
    if not service: ws = ws[~SERV[ws]]
    n = NV[ws]; start = np.cumsum(n) - n
    vi = np.arange(n.sum()) + np.repeat(OFF[ws] - start, n)
    P = mxy(XY[vi], c)
    m = np.ones(len(vi), bool); m[start + n - 1] = False
    s = np.flatnonzero(m); sw = np.repeat(ws, n - 1)
    if keep is not None:
        k = keep(P); ok = k[s] & k[s + 1]; s, sw = s[ok], sw[ok]
    wt = np.hypot(*(P[s] - P[s + 1]).T)
    if service: wt = wt * np.where(SERV[sw], 1.5, 1.0)
    ids, inv = np.unique(REF[vi], return_inverse=True)
    pos = np.empty((len(ids), 2)); pos[inv] = P
    return ids, pos, adjacency(inv[s], inv[s + 1], wt, RW[sw], len(ids))
def adjacency(a, b, wt, label, n):
    src, dst = np.column_stack([a, b]).ravel(), np.column_stack([b, a]).ravel()
    o = np.argsort(src, kind='stable')
    ptr = np.searchsorted(src[o], np.arange(n + 1))
    return ptr.tolist(), dst[o].tolist(), np.repeat(wt, 2)[o].tolist(), np.repeat(label, 2)[o].tolist()

def dijkstra(adj, dist, dst, cutoff=1e18):
    """Shortest path from the start nodes (dist: node -> start cost) to any node in dst -> its edge labels (ways)."""
    ptr, nbr, wt, wy = adj
    prev = {}; h = [(d, u) for u, d in dist.items()]; heapq.heapify(h)
    while h:
        du, u = heapq.heappop(h)
        if du > dist.get(u, 1e18) or du > cutoff: continue
        if u in dst:
            ways = set()
            while u in prev: u, w = prev[u]; ways.add(w)
            return ways
        for j in range(ptr[u], ptr[u + 1]):
            v = nbr[j]; nd = du + wt[j]
            if nd < dist.get(v, 1e18): dist[v] = nd; prev[v] = (u, wy[j]); heapq.heappush(h, (nd, v))
    return None

# ---------------------------------------------------------------- gaps
# Some line relations omit a stretch their trains run over another line's track (e.g. a
# corridor using an intercity line between two of its stops). Where a line has no track of its
# own between consecutive stops (of its main route or a branch), route it along the main-line
# network (shortest path over OSM nodes inside an ellipse around the two stops) and draw that
# track as part of the line. Its own track counts when, seen along the way from one stop to the
# other, its segments near them (sum of the distances to both at most 1.6x theirs) leave no fifth
# of the way bare: track that only leaves one of the stops in another direction does not.
def stops(a, b):
    p = np.array([S[a][2:4], S[b][2:4]], np.float64); c = cosl(p[:, 1].mean())
    A, B = mxy(p, c)
    return p, c, A, B, float(np.hypot(*(A - B)))
def route(a, b):
    p, c, A, B, d = stops(a, b)
    r = 0.75 * d + 1000; mx, my = p.mean(0)   # the ellipse lies within r of the midpoint
    rx, ry = r / (111320 * c), r / 110540
    inside = lambda P: np.hypot(*(P - A).T) + np.hypot(*(P - B).T) <= 1.5 * d + 2000
    g = graph(cells(mx - rx, my - ry, mx + rx, my + ry), c, False, inside)
    if g is None: return None
    ids, pos, adj = g
    dA = np.hypot(*(pos - A).T); dB = np.hypot(*(pos - B).T); ok = inside(pos)
    src = np.flatnonzero((dA < 1500) & ok); dst = set(np.flatnonzero((dB < 1500) & ok).tolist())
    if not len(src) or not dst: return None
    return dijkstra(adj, {int(i): float(dA[i]) * 3 for i in src}, dst)
_routes = {}
def bare(t, s):
    """The longest stretch of [0, 1] that the intervals between t[s] and t[s + 1] leave uncovered."""
    lo, hi = np.clip(np.minimum(t[s], t[s + 1]), 0, 1), np.clip(np.maximum(t[s], t[s + 1]), 0, 1)
    o = np.argsort(lo); lo, reach = lo[o], np.maximum.accumulate(hi[o])
    return max(lo[0], (lo[1:] - reach[:-1]).max(initial=0), 1 - reach[-1]) if len(s) else 1
def gaps(li):
    ws = [coords(w) for w in GEOM[li] if w in IX or w in WAYS]
    own = np.concatenate(ws or [np.zeros((0, 2))])
    sg = np.ones(len(own), bool); sg[np.cumsum([len(x) for x in ws], dtype=np.int64) - 1] = False; sg = sg[:-1]   # own[i]-own[i+1] in one way
    out = []
    for a, b in {(a, b) for q in stop_lists(li) for a, b in zip(q, q[1:])}:
        p, c, A, B, d = stops(a, b)
        if d < 3000: continue
        if len(own):
            O = mxy(own, c); near = np.hypot(*(O - A).T) + np.hypot(*(O - B).T) <= 1.6 * d
            if bare((O - A) @ (B - A) / (d * d), np.flatnonzero(sg & (near[:-1] | near[1:]))) < 0.2: continue
        if (a, b) not in _routes: _routes[(a, b)] = route(a, b)
        if _routes[(a, b)]: out.append(list(_routes[(a, b)]))
    return out

way_lines = defaultdict(set)
for li, ways in enumerate(GEOM):
    if visible(li):
        for w in ways: way_lines[w].add(li)
todo = [li for li in range(len(L)) if visible(li) and L[li][0] in 'hrs']
filled = 0
for li, got in zip(todo, par('gaps', gaps, todo, lambda li: digest(ways_sig(GEOM[li]), stop_sig(li)), 4)):
    for ws in got:
        for w in ws: way_lines[w].add(li)
        filled += 1
log('gaps between stops routed along the rail network:', filled)

# Join each intercity line's track into one piece: from every piece, search the rail network
# (station tracks and sidings included) for the nearest other piece of the same line, within
# 15 km of track, and draw the path as part of the line. A piece that can't be joined and has
# no station of the line (main route or branch) on it is left out, so no line stops dead in open country.
def pieces(ws, links=()):
    par = {}
    def f(x):
        while par.get(x, x) != x:
            par[x] = par.get(par[x], par[x]); x = par[x]
        return x
    for w in ws:
        r = refs(w).tolist(); a = f(r[0])
        for n in r[1:]:
            b = f(n)
            if a != b: par[b] = a
    for a, b in links:           # nodes joined by a straight bridge
        if f(a) != f(b): par[f(b)] = f(a)
    out = defaultdict(set)
    for w in ws: out[f(int(refs(w)[0]))].add(w)
    return list(out.values())
def join(a, rest, cutoff):
    pa = np.concatenate([coords(w) for w in a]); c = cosl(float(pa[:, 1].mean()))
    Pa, Pr = mxy(pa, c), mxy(np.concatenate([coords(w) for w in rest]), c)
    # a path can only start at a node within the cutoff (in a straight line) of another piece
    gk = lambda P, dx, dy: (np.floor(P[:, 0] / cutoff).astype(np.int64) + dx) * 1000003 + np.floor(P[:, 1] / cutoff).astype(np.int64) + dy
    rk = np.unique(gk(Pr, 0, 0)); near = np.zeros(len(Pa), bool)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1): near |= np.isin(gk(Pa, dx, dy), rk)
    if not near.any(): return None
    ix, iy = np.unique(np.floor(pa[near] / CELL).astype(np.int64), axis=0).T
    rx, ry = math.ceil(cutoff / (111320 * c) / CELL) + 1, math.ceil(cutoff / 110540 / CELL) + 1
    dx, dy = np.meshgrid(np.arange(-rx, rx + 1), np.arange(-ry, ry + 1))
    g = graph(np.unique(ckey(ix[:, None] + dx.ravel(), iy[:, None] + dy.ravel())).tolist(), c, True)
    if g is None: return None
    ids = g[0]
    ra = np.concatenate([refs(w) for w in a])[near]
    src = np.searchsorted(ids, ra).clip(0, len(ids) - 1); src = np.unique(src[ids[src] == ra])
    dst = set(np.flatnonzero(np.isin(ids, np.concatenate([refs(w) for w in rest]))).tolist())
    if not len(src) or not dst: return None
    return dijkstra(g[2], {int(i): 0.0 for i in src}, dst, cutoff)
def bridge(li, a, rest, limit=3000):
    """Piece a of a line and its other pieces that no track joins (a break in the OSM data): when both carry stops of the
    line and come within `limit` m, their nearest nodes -> ('bridge', coords, node a, node b), else None."""
    from scipy.spatial import cKDTree
    A = np.concatenate([coords(w) for w in a]); RA = np.concatenate([refs(w) for w in a]); c = cosl(float(A[:, 1].mean()))
    st = mxy(np.array([S[x][2:4] for q in stop_lists(li) for x in q], np.float64).reshape(-1, 2), c)
    PA = mxy(A, c)
    if not len(st) or cKDTree(PA).query(st)[0].min() > 1500: return None
    rw = list(rest); B = np.concatenate([coords(w) for w in rw]); RB = np.concatenate([refs(w) for w in rw]); PB = mxy(B, c)
    near = (np.abs(PB - (PA.min(0) + PA.max(0)) / 2) <= np.ptp(PA, 0) / 2 + limit).all(1)
    if not near.any(): return None
    d, j = cKDTree(PB[near]).query(PA); i = int(d.argmin())
    if d[i] > limit: return None
    k = np.flatnonzero(near)[j[i]]
    return ('bridge', np.array([A[i], B[k]]), int(RA[i]), int(RB[k]))
def joins(li):
    ws = line_ways[li]; added, dropped, links = [], [], []
    ps = pieces(ws)
    stuck = []
    while len(ps) > 1:
        ps.sort(key=len)
        a = ps[0]; rest = set().union(*ps[1:])
        path = join(a, rest, 15000)
        if path is None:
            b = bridge(li, a, rest)
            if b is None: stuck.append(ps.pop(0)); continue
            added.append(b); links.append(b[2:])     # a stretch OSM leaves out (under 3 km) between pieces with stops: straight
            ps = pieces(set().union(*ps), links); continue
        added.append(list(path))
        ws |= path
        ps = pieces(set().union(*ps) | path, links)
    for a in stuck:
        P = np.concatenate([coords(w) for w in a]); c = cosl(float(P[:, 1].mean()))
        P, st = mxy(P, c), mxy(np.array([S[x][2:4] for q in stop_lists(li) for x in q], np.float64).reshape(-1, 2), c)
        if any(np.hypot(*(P - q).T).min() < 1500 for q in st): continue
        dropped.append(list(a))
    return added, dropped

joined = dropped = 0
line_ways = defaultdict(set)
for w, ls in way_lines.items():
    for li in ls: line_ways[li].add(w)
todo = [li for li in line_ways if L[li][0] in 'hrs']
bridged = 0
for li, (added, drop) in zip(todo, par('joins', joins, todo, lambda li: digest(ways_sig(line_ways[li]), stop_sig(li)), 4)):
    for path in added:
        if path[0] == 'bridge':       # a straight way of its own between the two nodes
            w = -1 - bridged; bridged += 1; WAYS[w] = (path[1], {'railway': 'rail'}, np.array(path[2:], np.int64)); path = [w]
        for w in path: way_lines[w].add(li); line_ways[li].add(w)
        joined += 1
    for a in drop:
        for w in a:
            way_lines[w].discard(li); line_ways[li].discard(w)
            if not way_lines[w]: del way_lines[w]
        dropped += 1
log('line pieces joined along the rail network:', joined, f'({bridged} straight across breaks in the data)', '· stray pieces without a station left out:', dropped)
del GRID, _routes

# ---------------------------------------------------------------- ends
# Relations often run on past their end stops (to a depot or yard, or along the rest of a railway line). Each line
# keeps the shortest paths over its own track between consecutive stops (main route and branches) and all of its
# track within 150 m of them (the other track of a double line, station tracks); of the rest, dead ends go (track
# that meets the kept part in one place only: past a terminal, a depot spur) and other ways between two places of
# the line stay (a paired track, a diversion). A way is cut at the node where it leaves. Pieces where the mapping
# breaks (a stop off the track, consecutive stops on disconnected pieces) are left whole.
def components(a, b, n):  # node labels of the graph with edges a-b: hook roots to the smaller one, then flatten
    comp = np.arange(n)
    while True:
        lo, hi = np.minimum(comp[a], comp[b]), np.maximum(comp[a], comp[b])
        if (lo == hi).all(): return comp
        np.minimum.at(comp, hi, lo)
        while (comp[comp] != comp).any(): comp = comp[comp]
def ends(li):
    ws = [w for w in line_ways[li] if known(w)]
    # (a loop has no ends to cut; and on one-way track its return runs on other track than the shortest path back: the
    # Disneyland Monorail's second beam)
    if not ws or L[li][8]: return {}
    R = [refs(w) for w in ws]; n = np.array([len(r) for r in R]); start = np.cumsum(n) - n
    P = np.concatenate([coords(w) for w in ws]); c = cosl(float(P[:, 1].mean()))
    ids, inv = np.unique(np.concatenate(R), return_inverse=True)
    pos = np.empty((len(ids), 2)); pos[inv] = mxy(P, c)
    m = np.ones(len(inv), bool); m[start + n - 1] = False; s = np.flatnonzero(m)
    a, b = inv[s], inv[s + 1]
    comp = components(a, b, len(ids))
    adj = adjacency(a, b, np.hypot(*(pos[a] - pos[b]).T), np.arange(len(s)), len(ids))
    snap = {}
    for x in {x for q in stop_lists(li) for x in q}:
        d = np.hypot(*(pos - mxy(np.array([S[x][2:4]], np.float64), c)[0]).T); k = int(d.argmin())
        if d[k] < 1500: snap[x] = k
    pairs = {(p, q) for q_ in stop_lists(li) for p, q in zip(q_, q_[1:])} | ({(L[li][6][-1], L[li][6][0])} if L[li][8] and L[li][6] else set())
    core, whole = np.zeros(len(s), bool), set(np.unique(comp).tolist()) - {int(comp[k]) for k in snap.values()}
    for p, q in pairs:
        if p in snap and q in snap and comp[snap[p]] == comp[snap[q]]:
            if snap[p] != snap[q]: core[list(dijkstra(adj, {snap[p]: 0.0}, {snap[q]}))] = True
        else: whole |= {int(comp[snap[x]]) for x in (p, q) if x in snap}
    if not core.any(): return {}
    keep = np.isin(comp, list(whole))
    keep[shapely.STRtree(shapely.linestrings(np.stack([pos[a[core]], pos[b[core]]], 1))).query(
        shapely.points(pos), predicate='dwithin', distance=150)[0]] = True
    gone = ~(keep[a] & keep[b])
    if gone.any():
        ga, gb = a[gone], b[gone]; lab = components(ga, gb, len(ids))
        at = np.unique(np.r_[ga[keep[ga]], gb[keep[gb]]])        # where left-out track meets the kept part
        nodes = np.unique(np.r_[ga, gb])
        for l in np.unique(lab[at]):
            if np.hypot(*np.ptp(pos[at[lab[at] == l]], 0)) > 1000: keep[nodes[lab[nodes] == l]] = True
    ks = keep[a] & keep[b]
    out = {}
    for k, w in enumerate(ws):
        seg = ks[start[k] - k:start[k] - k + n[k] - 1]
        if not seg.all(): out[w] = seg.tobytes() if seg.any() else None
    return out

part = defaultdict(list)         # way -> [(line, kept segments)] for lines using only part of it
todo = [li for li in line_ways if L[li][6]]
cut = cutkm = 0
def ends_sig(li):   # bridges (negative ids) are numbered in line order: their nodes too
    return digest(ways_sig(line_ways[li]), stop_sig(li), L[li][8], [(w, WAYS[w][0].tobytes(), WAYS[w][2].tobytes()) for w in line_ways[li] if w < 0])
for li, got in zip(todo, par('ends', ends, todo, ends_sig, 8)):
    if got: cut += 1
    for w, seg in got.items():
        way_lines[w].discard(li)
        if not way_lines[w]: del way_lines[w]
        if seg is not None: part[w].append((li, np.frombuffer(seg, bool)))
        c = coords(w); km = np.hypot(np.diff(c[:, 0]) * cosl(c[0, 1]), np.diff(c[:, 1])) * 111.2
        cutkm += km.sum() - (km[np.frombuffer(seg, bool)].sum() if seg is not None else 0)
log(f'lines cut to their end stops: {cut}, {cutkm:.0f} km of track left out')
del line_ways

# ---------------------------------------------------------------- rail features
# Each stretch of track (a way, or the part of one between the nodes where the set of lines on it changes) is drawn
# once for its intercity lines and once for its urban lines. The urban feature, drawn above, keeps its line's colour
# on track it shares with intercity trains; the intercity line's name is not written there. Every intercity line is drawn
# in its own kind all along its track: where high-speed and other intercity trains share track (a TGV among TER trains
# west of Rennes, a Mini-shinkansen), each kind gets a feature of its own (pair 1), and the site draws the two side by side.
URBAN_COLOUR = {'s': '#1E8C73', 'm': '#D0453A', 'l': '#D98E04', 't': '#B83280', 'f': '#7C5C3B'}   # lines without one
SPECIAL = re.compile(r'(?i)special|event|game|stadium|concert|festival|fair\b|expo\b|holiday|christmas|charter|seasonal|excursion|'
                     r'weekend|saturday|sunday|night|nacht|nuit|nocturn|ночн|臨時|深夜')
groups = defaultdict(list)
def add(ls, w, i0, i1):
    ic = frozenset(i for i in ls if L[i][0] in 'hr')
    if ic:
        k = {L[i][0] for i in ic}
        groups[(ic, k.pop() if len(k) == 1 else 'hr', len(ic) < len(ls))].append((w, i0, i1))
    if len(ic) < len(ls): groups[(frozenset(ls) - ic, None, False)].append((w, i0, i1))
for w, ls in way_lines.items():
    if w not in part: add(ls, w, 0, None)
for w, ps in part.items():
    sets = [frozenset(way_lines.get(w, set()) | {li for li, seg in ps if seg[j]}) for j in range(len(ps[0][1]))]
    j0 = 0
    for j in range(1, len(sets) + 1):
        if j == len(sets) or sets[j] != sets[j0]:
            if sets[j0]: add(sets[j0], w, j0, j + 1)
            j0 = j
del way_lines, part

def primary(ls, wk=None):
    """The line a stretch is drawn as, and its kind: intercity the longest line of its kind (wk); urban
    by the commonest kind, then a regular (not event, weekend or night) line with a colour, the colour most lines
    on it share, the most track, the most stops."""
    if wk:
        same = [i for i in ls if L[i][0] == wk] or list(ls)
        return max(same, key=lambda i: (L[i][9] or 0, len(L[i][6]))), wk
    kinds = [L[i][0] for i in ls]
    k = max(sorted(set(kinds), key='hrsmlft'.index), key=kinds.count)
    cols = Counter(L[i][4].upper() for i in ls if L[i][4])
    same = [i for i in ls if L[i][0] == k]
    return max(same, key=lambda i: (not SPECIAL.search(L[i][2] or L[i][1]), bool(L[i][4]), cols[L[i][4].upper()] if L[i][4] else 0,
                                    round(LEN[i] / 5), len(L[i][6]))), k
# route badges: short public line codes ("1", "U4", "S51", "RE3", "C-3", "A", "JY"), not mapper codes ("SN3.12",
# "B-L", "PURP", "ARM", "GLNELG") or names ("Babylon"); British suburban refs (Thameslink "TL8") are all internal
def badge(li):
    r = re.sub(r'(?<=[^\W\d_])\s+(?=\d)', '', L[li][3].strip())
    ok = (re.fullmatch(r'[^\W\d_]{0,3}-?\d{1,3}(?:bis|[^\W\d_])?', r) and not r.startswith('-') or
          re.fullmatch(r'[^\W\d_]{1,2}', r) and r == r.upper())
    return r if ok and len(r) <= 5 and not (CT[L[li][12]][0] == 'GB' and L[li][0] == 's') else ''

# each group's ways, merged into as few lines as possible, in Web Mercator (0-1)
gws = [[(w, i0, i1) for w, i0, i1 in ws if known(w)] for ws in groups.values()]
has = [j for j, ws in enumerate(gws) if ws]
wl = [coords(w)[i0:i1] for j in has for w, i0, i1 in gws[j]]
pts = np.concatenate(wl or [np.zeros((0, 2))])
lines = shapely.linestrings(np.column_stack(merc(pts[:, 0], pts[:, 1])), indices=np.repeat(np.arange(len(wl)), [len(p) for p in wl]))
merged = shapely.line_merge(shapely.multilinestrings(lines, indices=np.repeat(np.arange(len(has)), [len(gws[j]) for j in has])))
F, fg = shapely.get_parts(merged, return_index=True)
def props(ls, wk):
    prim, k = primary(ls, wk)
    p = {'l': prim, 'ls': '|' + '|'.join(str(i) for i in sorted(ls)) + '|', 'k': k}
    if k not in 'hr':
        p['c'] = L[prim][4] or URBAN_COLOUR[k]
        if badge(prim): p['r'] = badge(prim)
    return p
keys = list(groups)
GP = [[props(keys[j][0], keys[j][1])] if keys[j][1] != 'hr' else
      [dict(props({i for i in keys[j][0] if L[i][0] == k}, k), pair=1) for k in 'hr'] for j in has]
# The site draws a pair's high-speed half on the right of the feature's direction, so pairs side by side (the tracks
# of a double line) or one after another must run the same way. Starting from the longest, each takes the direction of
# its neighbours (pieces within about 25 m that run in much the same direction); one with none runs along its high-speed
# line's axis (the direction its stops spread furthest in), eastwards.
def axis(li):
    p = np.array([S[x][2:4] for q in stop_lists(li) for x in q], np.float64).reshape(-1, 2)
    p = np.column_stack(merc(p[:, 0], p[:, 1])); p -= p.mean(0) if len(p) else 0
    v = np.linalg.svd(p, full_matrices=False)[2][0] if len(p) >= 2 and p.any() else np.array([1.0, 0.0])
    return v if v[0] > 0 or v[0] == 0 and v[1] < 0 else -v
def tangent(g, at, e=2e-6):
    s = shapely.line_locate_point(g, at)   # (a negative distance would count from the far end)
    t = (shapely.get_coordinates(shapely.line_interpolate_point(g, np.minimum(s + e, shapely.length(g)))) -
         shapely.get_coordinates(shapely.line_interpolate_point(g, np.maximum(s - e, 0))))
    return t / np.maximum(np.hypot(*t.T), 1e-15)[:, None]
n = np.array([len(GP[g]) for g in fg.tolist()], np.int64)
ix = np.flatnonzero(n == 2); P = F[ix]
a, b = shapely.STRtree(P).query(P, predicate='dwithin', distance=1e-6); a, b = a[a < b], b[a < b]
sl = shapely.shortest_line(P[a], P[b])
dot = (tangent(P[a], shapely.get_point(sl, 0)) * tangent(P[b], shapely.get_point(sl, 1))).sum(1)
nb = defaultdict(list)
for i, j, d in zip(a.tolist(), b.tolist(), dot.tolist()):
    if abs(d) > 0.5: nb[i].append((j, d > 0)); nb[j].append((i, d > 0))
way = np.zeros(len(P), np.int8)   # 1 as it is, -1 turned
for r in np.argsort(-shapely.length(P)).tolist():
    if way[r]: continue
    ch = shapely.get_coordinates(shapely.get_point(P[r], -1)) - shapely.get_coordinates(shapely.get_point(P[r], 0))
    way[r] = 1 if float((ch @ axis(GP[fg[ix[r]]][0]['l']))[0]) >= 0 else -1
    todo = [r]
    while todo:
        i = todo.pop()
        for j, same in nb[i]:
            if not way[j]: way[j] = way[i] if same else -way[i]; todo.append(j)
back = way < 0
F[ix[back]] = shapely.reverse(F[ix[back]])
F, FP = np.repeat(F, n), [p for g in fg.tolist() for p in GP[g]]
SHARED = np.repeat(np.array([keys[has[g]][2] for g in fg.tolist()], bool), n)
del WAYS, G, IX, XY, REF, groups, gws, wl, pts, lines, merged, P, sl, nb
log('rail features', len(F), '·', sum(p['k'] in 'hr' for p in FP), 'intercity ·', int(2 * (n == 2).sum()), 'in pairs,', int(back.sum()), 'pairs turned')

# the overview (z0-2): the intercity network of each kind merged into one, so it simplifies without gaps at junctions
IC = np.array([p['k'] for p in FP])
F0, FP0 = [], []
for k in 'rh':
    g = shapely.get_parts(shapely.line_merge(shapely.multilinestrings(F[IC == k]))) if (IC == k).any() else []
    F0 += list(g); FP0 += [{'l': -1, 'ls': '', 'k': k}] * len(g)
F0 = np.array(F0, object)

# ---------------------------------------------------------------- stations
# Ranks 1-13 (the site labels 11+ at low zoom, 8+ from z7, 5+ from z9) go to a complex's main station; the other
# members start at z10 with a small rank, so a hub is one dot and one name. A station weighs the directions its rail
# lines leave in, the kinds of rail at the complex and, logarithmically (Russian and Indian relations are single
# trains), its distinct lines. A city (full.json's cities and GeoNames places of 30,000+; a place within reach of a
# bigger one counts towards it) gives its rank, by that population, to one station: the heaviest near its centre,
# preferring one named after it ("Nairobi", "Roma Termini"), never a platform, yard or airport node; a very busy one
# ranks one up. Up to 8 other stations named after it ("Paris Est", "Mumbai CSMT") rank 10 at most. A capital, a
# country's largest city and the three busiest stations of its cities of 150,000+ rank 11 at least; a smaller city in
# the orbit of a big one (Machida, Klang) 10 at most. China, Hong Kong and Macau keep the curated rule: every station
# named after one of their cities (from full.json; all have a metro) gets the city's rank, others rank by their lines.
RAILK = 'hrs'
JUNK = re.compile(r'(?i)\b(?:v[ií]as?|voies?|gleis|platforms?|bahnsteig|quai|track|peron|binario|spoor|and[eé]n|perron)\s*\d|'
                  r'товарн|сортиров|tovarn|sortirov|güterbahnhof|rangierbahnhof|marshalling|freight|goods yard|парк\s*["«]|park\s*["«]')
AIRPORT = re.compile(r'(?i)airport|a[eé]roport|aeropuerto|aeroporto|flughafen|lufthavn|flygplats|lentoasema|lotnisko|letiště|'
                     r'havaliman|аэропорт|аеропорт|空港|机场|機場|공항|สนามบิน|sân bay|فرودگاه|مطار|נמל התעופה')
CENTRAL = re.compile(r'(?i)(?:hbf|hauptbahnhof|hb|central|centraal|centrale?|centralny|centralna|główny|glowny|hlavn[ií] n[aá]dra[zž][ií]|'
                     r'hl\. ?n\.|glavny|главный|termini|sentral|c|h|s|railway station|station|junction|jn|terminus|main|stazione|'
                     r'gare|estaci[oó]n|esta[cç][aã]o|вокзал|centrum|центральный|kolodvor|pályaudvar)\.?|[站駅역]')
CUR = {i for i, c in enumerate(CT) if c[0] in ('CN', 'HK', 'MO')}
def rep_of(i): return S[i][7] if S[i][7] >= 0 else i
def junk(s): return bool(JUNK.search(s[0]) or JUNK.search(s[1] or ''))
MEM = defaultdict(list)
for i, s in enumerate(S):
    if any(visible(x) for x in s[5]): MEM[rep_of(i)].append(i)
NB = defaultdict(set)             # neighbouring stops on rail lines
for li, l in enumerate(L):
    if visible(li) and l[0] in RAILK:
        for q in stop_lists(li):
            for a, b in zip(q, q[1:]): NB[a].add(b); NB[b].add(a)
def weight(r):
    lon, lat = S[r][2:4]; c = cosl(lat); b = set()
    for m in MEM[r]:
        for j in NB[m]:
            dx, dy = (S[j][2] - lon) * c, S[j][3] - lat
            if rep_of(j) != r and dx * dx + dy * dy > 1e-5: b.add(round(math.degrees(math.atan2(dy, dx))) % 360)
    b = sorted(b); dirs = max(1, int((np.diff(b + [b[0] + 360]) > 40).sum())) if b else 0
    allv = {x for m in MEM[r] for x in S[m][5] if visible(x)}
    rail = {(L[x][0], L[x][1]) for x in allv if L[x][0] in RAILK}
    urban = {(L[x][0], L[x][3] or L[x][1]) for x in allv if L[x][0] not in RAILK}
    if S[r][8] in CUR:            # the curated rule, as before
        ni, nu = len({L[x][1] for x in allv if L[x][0] in 'hr'}), len({L[x][1] for x in allv if L[x][0] not in 'hr'})
        return 0, min(9, 1 + 2 * ni + nu), ni + nu
    w = dirs + len({L[x][0] for x in allv} & set('hrsml')) + math.log2(1 + len(rail)) + 0.5 * math.log2(1 + len(urban))
    return w, max(1, min(3 if junk(S[r]) else 9, round(w) - 3)), len(rail | urban)
W = {r: weight(r) for r in MEM}
RK = {r: w[1] for r, w in W.items()}
MAIN = set()                      # each city's one station: drawn from z0 at rank 13, from z2 at rank 12
def named(nm, cn):                # "北京南" and "Paris-Nord" are named after 北京 and Paris, "Parisot" isn't
    return len(cn) >= 2 and nm.startswith(cn) and (len(nm) == len(cn) or not nm[len(cn)].isalpha() or ord(cn[-1]) >= 0x2e80)
def naming(nm, cn):               # 2: the city's own station ("Nairobi", "Roma Termini", "東京"), 1: named after it ("Paris Est")
    if not named(nm, cn): return 0
    rest = re.sub(r'\([^)]*\)', '', nm[len(cn):]).strip(' -–—/,')   # "Frankfurt (Main) Hbf", not "Fukuoka (Tenjin)"
    if len(nm) == len(cn) or rest and CENTRAL.fullmatch(rest): return 2
    return 0 if ord(cn[-1]) >= 0x2e80 and rest[0] not in '東西南北东中' else 1     # 大阪天満宮 is not named after 大阪

# China, Hong Kong, Macau
CGRID = defaultdict(list)
for ci, c in enumerate(C):
    if c[6] in CUR: CGRID[(math.floor(c[2]), math.floor(c[3]))].append(ci)
def china_city(s, en):
    near = [ci for dx in (-1, 0, 1) for dy in (-1, 0, 1) for ci in CGRID.get((math.floor(s[2]) + dx, math.floor(s[3]) + dy), ())
            if abs(s[2] - C[ci][2]) < 0.8 and abs(s[3] - C[ci][3]) < 0.8]
    for f in ((0, 1) if en else (0,)):
        for ci in sorted(near, key=lambda ci: (-len(C[ci][f] or ''), ci)):
            if s[f] and named(s[f], C[ci][f] or ''): return ci
    return -1
ours = defaultdict(list)
for r in MEM:
    if S[r][8] not in CUR: continue
    ci = china_city(S[r], any(L[x][0] in 'hr' for m in MEM[r] for x in S[m][5] if visible(x)))
    if ci >= 0:
        p = C[ci][4]; RK[r] = 13 if p > 15e6 else 12 if p > 8e6 else 11 if p > 4e6 else 10; ours[ci].append(r)
for ci, rs in ours.items():
    rs = [r for r in rs if S[r][4] in RAILK and not junk(S[r])]
    if rs: MAIN.add(max(rs, key=lambda r: (naming(S[r][0], C[ci][0]) == 2, W[r][2])))

# the rest of the world: places, biggest first; one within reach of a bigger one (8 km for 100,000 people, 20 km for
# 10 million) counts towards it
gnc = geonamescache.GeonamesCache(min_city_population=15000)
PL = [[c[2], c[3], c[4], c[6], {c[0], c[1]} - {''}] for c in C if c[6] not in CUR]
nfj, cc, CAP = len(PL), {c[0]: i for i, c in enumerate(CT)}, set()
caps = {(g['iso'], g['capital']) for g in gnc.get_countries().values()}
PG = defaultdict(list)
for i, p in enumerate(PL): PG[(math.floor(p[0]), math.floor(p[1]))].append(i)
def near_places(grid, lon, lat, r):
    js = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in grid.get((math.floor(lon) + dx, math.floor(lat) + dy), ())]
    d = np.hypot((np.array([PL[j][0] for j in js]) - lon) * 111.32 * cosl(lat), (np.array([PL[j][1] for j in js]) - lat) * 110.54)
    return [(float(d[k]), j) for k, j in enumerate(js) if d[k] < r]
for g in sorted(gnc.get_cities().values(), key=lambda g: -g['population']):
    ci = cc.get(g['countrycode'])
    if ci is None or ci in CUR or g['population'] < 30000: continue
    lon, lat, pop = g['longitude'], g['latitude'], g['population']
    names = {g['name']} | {a for a in g['alternatenames'] if len(a) >= 4 or not a.isascii()}
    same = [(d, j) for d, j in near_places(PG, lon, lat, 10) if j < nfj and PL[j][3] == ci and (names & PL[j][4] or 0.3 < pop / max(PL[j][2], 1) < 3)]
    if same:
        j = min(same)[1]; PL[j][2] = max(PL[j][2], pop); PL[j][4] |= names
    else:
        j = len(PL); PL.append([lon, lat, pop, ci, names]); PG[(math.floor(lon), math.floor(lat))].append(j)
    if (g['countrycode'], g['name']) in caps: CAP.add(j)
def reach(p): return 8 + 6 * math.log10(max(p, 1e5) / 1e5)
order = sorted(range(len(PL)), key=lambda i: -PL[i][2])
agg, owner, CG = [p[2] for p in PL], {}, defaultdict(list)
for i in order:
    near = [(d, j) for d, j in near_places(CG, PL[i][0], PL[i][1], 30) if d < reach(PL[j][2])]
    if near: j = min(near)[1]; owner[i] = j; agg[j] += PL[i][2]
    else: CG[(math.floor(PL[i][0]), math.floor(PL[i][1]))].append(i)
def pop_rank(p): return 13 if p >= 12e6 else 12 if p >= 4e6 else 11 if p >= 1.2e6 else 10 if p >= 4e5 else 9 if p >= 1.5e5 else 8 if p >= 5e4 else 0
largest, CR = {}, {}
for i in order:
    if i not in owner: largest.setdefault(PL[i][3], i)
for i in order:
    CR[i] = (min(10, pop_rank(PL[i][2]), CR[owner[i]] - 1) if i in owner else
             min(13, max(pop_rank(agg[i]) + (i in CAP), 11 if i in CAP or largest[PL[i][3]] == i else 0)))
# a city of under a million in the orbit of one of 4 million+ (within 2.5 of its reaches: Machida, Klang, Virar)
# ranks 10 at most
mega = np.array([[PL[i][0], PL[i][1], agg[i], reach(agg[i]) * 2.5] for i in order if i not in owner and agg[i] >= 4e6]).reshape(-1, 4)
SAT = set()
for i in order:
    if i in owner or PL[i][2] >= 1e6 or i in CAP: continue
    d = np.hypot((mega[:, 0] - PL[i][0]) * 111.32 * cosl(PL[i][1]), (mega[:, 1] - PL[i][1]) * 110.54)
    if ((d < mega[:, 3]) & (mega[:, 2] > agg[i])).any(): SAT.add(i); CR[i] = min(CR[i], 10)
RAIL = [r for r in MEM if S[r][8] not in CUR and not junk(S[r]) and not AIRPORT.search(S[r][0] + ' ' + (S[r][1] or ''))
        and any(L[x][0] in RAILK for m in MEM[r] for x in S[m][5] if visible(x))]
RG = defaultdict(list)
for r in RAIL: RG[(math.floor(S[r][2] / 0.2), math.floor(S[r][3] / 0.2))].append(r)
big = defaultdict(list)           # country -> the stations of its cities of 150,000+
for i in sorted(CR, key=lambda i: (-CR[i], -agg[i])):
    if not CR[i]: continue
    lon, lat, _, _, names = PL[i]; rad = reach(PL[i][2]) / 2
    cand = []
    for r in (r for dx in range(-2, 3) for dy in range(-2, 3) for r in RG.get((math.floor(lon / 0.2) + dx, math.floor(lat / 0.2) + dy), ())):
        s = S[r]; d = math.hypot((s[2] - lon) * 111.32 * cosl(lat), (s[3] - lat) * 110.54)
        if d >= 2 * rad: continue
        nm = max((naming(f, cn) for f in s[:2] if f for cn in names), default=0)
        if nm or d < rad and (d < rad / 2 or W[r][0] >= 7): cand.append((W[r][0] + (0, 2, 6)[nm] - 3 * d / rad, nm, r, d < rad))
    cand.sort(reverse=True)
    main = next((c[2] for c in cand if c[3]), None)
    if main is not None and main not in MAIN:
        MAIN.add(main); RK[main] = max(RK[main], CR[i] + (10 <= CR[i] < 13 and W[main][0] >= 13.5))
        if i not in owner and i not in SAT and CR[i] >= 9: big[PL[i][3]].append(main)
    for _, nm, r, _ in [c for c in cand if c[1] and c[2] != main and W[c[2]][2] >= 2][:8]: RK[r] = max(RK[r], min(10, CR[i] - 1))
for rs in big.values():
    for r in sorted(rs, key=lambda r: -W[r][0])[:3]: RK[r] = max(RK[r], 11)

def latin(s): return all(ch < 'ʰ' or 'Ḁ' <= ch <= 'ỿ' for ch in s if ch.isalpha())
stn_feats = []
for i, s in enumerate(S):
    vis = [x for x in s[5] if visible(x)]
    if not vis: continue
    rep = rep_of(i)
    allv = {x for m in MEM[rep] for x in S[m][5] if visible(x)}
    inter, urb = {L[x][1] for x in allv if L[x][0] in 'hr'}, {L[x][1] for x in allv if L[x][0] not in 'hr'}
    ks = {L[x][0] for x in vis}   # the main kind at the node (suburban-only nodes are 's', also in older data)
    k = s[4] if s[8] in CUR or s[4] in ks else min(ks, key='hrsmlft'.index)
    rk = RK[rep] if rep == i else min(4, W[rep][1])
    own_urban = [x for x in vis if L[x][0] not in 'hr']
    col = (L[own_urban[0]][4] or URBAN_COLOUR[L[own_urban[0]][0]]) if len(own_urban) == 1 else '#FFFFFF'
    n = s[1] or s[0]
    props = {'i': i, 'k': k, 'n': n, 'z': s[0], 'b': s[0] if s[0] != n and not latin(s[0]) else '', 'x': len(inter | urb), 'c': col, 'rk': rk,
             'ls': '|' + '|'.join(str(x) for x in sorted(vis)) + '|', 'ks': ''.join(sorted(ks)),
             'kc': ''.join(sorted({L[x][0] for x in allv})), 'cx': rep, 'rep': int(rep == i)}
    mz = ((10 if props['x'] <= 1 else 9) if k not in RAILK else 9 if rep != i else 0 if i in MAIN and rk >= 13 else
          2 if i in MAIN and rk >= 12 else 3 if rk >= 11 else 5 if rk >= 10 else 6 if rk >= 8 else 7 if rk >= 5 else 9)
    stn_feats.append((s[2], s[3], props, mz))
SP = [p for _, _, p, _ in stn_feats]
SX, SY = merc(np.array([f[0] for f in stn_feats], np.float64), np.array([f[1] for f in stn_feats], np.float64))
SZ = np.array([f[3] for f in stn_feats], np.int64)
log('stations', len(SP), '· from z0', int((SZ == 0).sum()), '· z2', int((SZ <= 2).sum()), '· z3', int((SZ <= 3).sum()),
    '· z6', int((SZ <= 6).sum()), '· z7', int((SZ <= 7).sum()))

# ---------------------------------------------------------------- tiling
# Per zoom, every feature is simplified once and handed, with each tile it touches, to that tile's chunk (the
# ancestor tile of its zoom group). The tiles a line touches are found from the vertices of a coarser, densified
# copy: each vertex lies within reach of at most 2 x 2 tiles. Workers then clip, encode and pack whole chunks.
GZ = [next(g for g, (a, b, _) in enumerate(GROUPS) if a <= z <= b) for z in range(MAXZ + 1)]
def chunk_code(z, tx, ty):
    az = GROUPS[GZ[z]][2]
    return (GZ[z] << 26) | ((tx >> (z - az)) << 13) | (ty >> (z - az))
def chunk_key(code): return f'g{code >> 26}_{(code >> 13) & 0x1fff}_{code & 0x1fff}'
CH, GEO, PROPS = defaultdict(list), {}, {}
def assign(z, kind, idx, tx, ty):
    code = chunk_code(z, tx, ty); o = np.argsort(code, kind='stable')
    code, idx, tx, ty = code[o], idx[o], tx[o], ty[o]
    cut = np.flatnonzero(np.diff(code)) + 1
    for k, a, x, y in zip(code[np.r_[0, cut]].tolist() if len(code) else [], np.split(idx, cut), np.split(tx, cut), np.split(ty, cut)):
        CH[k].append((z, kind, a, x, y))
for z in range(MINZ, MAXZ + 1):
    n = 2 ** z; px = 1 / (n * 256)
    FZ, PROPS[z] = (F0, FP0) if z < OVERZ else (F, FP)
    FLEN, URB = shapely.length(FZ), np.array([p['k'] not in 'hr' for p in PROPS[z]], bool)
    sel = np.flatnonzero(~(URB & (FLEN < 4 * px))) if z < 7 else np.arange(len(FZ))
    g = shapely.simplify(FZ[sel], px * 0.5, preserve_topology=False) if z < MAXZ else FZ[sel]
    ok = ~shapely.is_empty(g) & (shapely.length(g) >= px * 0.5); sel, g = sel[ok], g[ok]
    GEO[z] = np.empty(len(FZ), object); GEO[z][sel] = g
    cg = shapely.simplify(g, 1 / (8 * n), preserve_topology=False); e = shapely.length(cg) <= 0; cg[e] = g[e]
    xy, j = shapely.get_coordinates(shapely.segmentize(cg, 1 / (4 * n)), return_index=True)
    e = BUF / EXT / n + 1 / (4 * n) + 1e-12
    t = np.concatenate([np.column_stack([j, np.floor((xy[:, 0] + ex) * n), np.floor((xy[:, 1] + ey) * n)]).astype(np.int64)
                        for ex in (-e, e) for ey in (-e, e)])
    t = t[(t[:, 1:] >= 0).all(1) & (t[:, 1:] < n).all(1)]
    t = np.unique((t[:, 0] * n + t[:, 1]) * n + t[:, 2])
    assign(z, 'r', sel[t // (n * n)], t // n % n, t % n)
    tx, ty = (SX * n).astype(np.int64), (SY * n).astype(np.int64)
    s = np.flatnonzero((SZ <= z) & (tx >= 0) & (tx < n) & (ty >= 0) & (ty < n))
    assign(z, 's', s, tx[s], ty[s])
    log(f'z{z}: {len(sel)} lines in {len(t)} line-tile pairs, {len(s)} stations')
del xy, j, t, cg

def encode(layers):
    ls = [{'name': name, 'features': f} for name, f in (('rail', layers['rail']), ('stn', layers['stn'])) if f]
    return gzip.compress(mapbox_vector_tile.encode(ls, default_options={'extents': EXT, 'y_coord_down': True, 'quantize_bounds': None,
                                                                          'on_invalid_geometry': None}), compresslevel=9, mtime=0)
def write_chunk(entries):
    entries.sort(); idx, data, off = [], [], 0
    for k, t in entries:
        idx.append(struct.pack('<III', k, off, len(t))); data.append(t); off += len(t)
    return b'TPK1' + struct.pack('<I', len(entries)) + b''.join(idx) + b''.join(data)
def make_chunk(code):
    tiles = defaultdict(lambda: {'rail': [], 'stn': []})
    for z, kind, idx, tx, ty in CH[code]:
        n = 2 ** z
        if kind == 's':
            for i, x, y in zip(idx.tolist(), tx.tolist(), ty.tolist()):
                tiles[(z, x, y)]['stn'].append({'geometry': Point(SX[i] * n * EXT - x * EXT, SY[i] * n * EXT - y * EXT), 'properties': SP[i]})
            continue
        b = BUF / EXT / n
        g = shapely.intersection(GEO[z][idx], shapely.box(tx / n - b, ty / n - b, (tx + 1) / n + b, (ty + 1) / n + b))
        ok = ~shapely.is_empty(g); g, idx, tx, ty = g[ok], idx[ok], tx[ok], ty[ok]
        xy, j = shapely.get_coordinates(g, return_index=True)
        g = shapely.set_coordinates(g, xy * (n * EXT) - np.column_stack([tx, ty])[j] * EXT)
        for c, i, x, y in zip(g, idx.tolist(), tx.tolist(), ty.tolist()):
            pr = dict(PROPS[z][i])
            if pr['k'] in 'hr' and 5 <= z <= 11 and not SHARED[i]:
                lab = L[pr['l']]; pr['nm'] = lab[11] or lab[2] or lab[1]; pr['nz'] = lab[1]
            tiles[(z, x, y)]['rail'].append({'geometry': c, 'properties': pr})
    entries, sizes = [], np.zeros((MAXZ + 1, 2), np.int64)
    for (z, x, y), layers in tiles.items():
        t = encode(layers); entries.append(((z << 26) | (x << 13) | y, t)); sizes[z] += (1, len(t))
    return code, write_chunk(entries), sizes

# --cache / --prev: each chunk's digest covers everything make_chunk reads for it: per zoom and rail feature its
# simplified geometry, properties and label, per station its properties and position, and the tiles each goes to
def hsh(b): return hashlib.blake2b(b, digest_size=16).digest()
def rail_digests(z):
    pj, lab = PJ[z < OVERZ], LB if 5 <= z <= 11 else None
    return z, b''.join(hsh(w + pj[i] + (lab[i] if lab else b'')) if w is not None else bytes(16) for i, w in enumerate(shapely.to_wkb(GEO[z]).tolist()))
DIG, PD = {}, OPT['--prev'][1] if '--prev' in OPT else None
if DIGEST:
    PJ = {True: [json.dumps(p).encode() for p in FP0], False: [json.dumps(p).encode() for p in FP]}
    LB = [json.dumps([L[p['l']][11] or L[p['l']][2] or L[p['l']][1], L[p['l']][1]]).encode() if p['k'] in 'hr' and not SHARED[i] else b''
          for i, p in enumerate(FP)]
    settle()
    with FORK.Pool(NPROC) as pool: RD = {z: np.frombuffer(b, np.uint8).reshape(-1, 16) for z, b in pool.imap_unordered(rail_digests, range(MINZ, MAXZ + 1))}
    SD = np.frombuffer(b''.join(hsh(json.dumps(p).encode() + struct.pack('<dd', SX[i], SY[i])) for i, p in enumerate(SP)), np.uint8).reshape(-1, 16)
    for code, parts in CH.items():
        h = hashlib.blake2b(digest_size=16)
        for z, kind, idx, tx, ty in parts:
            h.update(bytes([z]) + kind.encode()); h.update((RD[z] if kind == 'r' else SD)[idx].tobytes()); h.update(tx.tobytes()); h.update(ty.tobytes())
        DIG[code] = h.digest()
    NEWC['chunks'] = DIG
    log('chunk digests', len(DIG))
def code_of(key): g, x, y = map(int, key[1:].split('_')); return (g << 26) | (x << 13) | y
PMAN = {code_of(k): v for k, v in json.load(open(os.path.join(PD, 'manifest.json')))['rail']['chunks'].items()} if PD else {}
_pf = {}
def prev_chunk(code):
    fn, off, n = PMAN[code]
    if fn not in _pf: _pf[fn] = open(os.path.join(PD, 'tiles', fn), 'rb').read()
    return _pf[fn][off:off + n]
same = {c for c in CH if c in PMAN and PREVC.get('chunks', {}).get(c) == DIG.get(c)} if PD else set()

settle()
codes = sorted(set(CH) - same, key=lambda k: -sum(len(p[2]) for p in CH[k]))
blobs, sizes = {}, np.zeros((MAXZ + 1, 2), np.int64)
log('encoding', len(codes), 'chunks on', NPROC, 'processes')
with FORK.Pool(NPROC) as pool:
    for code, blob, sz in pool.imap_unordered(make_chunk, codes):
        blobs[code] = blob; sizes += sz
        if len(blobs) % max(1, len(codes) // 10) == 0: log(f'  {len(blobs)}/{len(codes)} chunks')

# chunks go into files of about 1.5 MB, neighbours together (the site reads a chunk by its byte range). With --prev,
# every chunk stays in its file (a new one goes into the file of the chunk before it), a file whose chunks are all
# unchanged is copied as it is, and the others are written again, under new names.
def morton(x, y): return sum(((x >> i & 1) << (2 * i + 1)) | ((y >> i & 1) << (2 * i)) for i in range(13))
def order(k): return (k >> 26, morton((k >> 13) & 0x1fff, k & 0x1fff))
TDIR = os.path.join(OUT, 'tiles')
if PD: assert os.path.realpath(os.path.join(PD, 'tiles')) != os.path.realpath(TDIR), 'write the update to another data dir'
os.makedirs(TDIR, exist_ok=True)
for f in os.listdir(TDIR): os.remove(os.path.join(TDIR, f))
for c in [c for c in blobs if c in PMAN and blobs[c] == prev_chunk(c)]: del blobs[c]; same.add(c)   # encoded again, same bytes
man, fi, buf, pos = {}, 0, [], 0
def flush():
    global fi, buf, pos
    if buf:
        open(os.path.join(TDIR, f'rail_{fi}.bin'), 'wb').write(b''.join(buf)); fi += 1; buf, pos = [], 0
if PMAN:
    pk = sorted(PMAN, key=order); po = [order(c) for c in pk]
    home, was = defaultdict(list), defaultdict(set)
    for c, v in PMAN.items(): was[v[0]].add(c)
    for c in CH: home[PMAN[c][0] if c in PMAN else PMAN[pk[max(0, bisect.bisect(po, order(c)) - 1)]][0]].append(c)
    fi = 1 + max((int(m.group(1)) for m in (re.fullmatch(r'rail_(\d+)\.bin', f) for f in was) if m), default=-1)
    copied = []
    for f, cs in sorted(home.items(), key=lambda x: order(min(x[1], key=order))):
        if set(cs) == was[f] and not any(c in blobs for c in cs):
            shutil.copyfile(os.path.join(PD, 'tiles', f), os.path.join(TDIR, f)); copied.append(f)
            for c in cs: man[c] = PMAN[c]
            continue
        for c in sorted(cs, key=order):
            blob = blobs[c] if c in blobs else prev_chunk(c)
            man[c] = [f'rail_{fi}.bin', pos, len(blob)]; buf.append(blob); pos += len(blob)
        flush()
    man = {chunk_key(c): man[c] for c in sorted(man, key=order)}
    log(f'{len(blobs)} chunks new or changed, {len(PMAN) - len(set(PMAN) & set(CH))} gone, {len(same)} copied; '
        f'{len(set(v[0] for v in man.values())) - len(copied)} files written, {len(copied)} kept')
else:
    for code in sorted(blobs, key=order):
        blob = blobs[code]
        if pos and pos + len(blob) > 1_500_000: flush()
        man[chunk_key(code)] = [f'rail_{fi}.bin', pos, len(blob)]; buf.append(blob); pos += len(blob)
    flush()
json.dump({'rail': {'minzoom': MINZ, 'maxzoom': MAXZ, 'groups': GROUPS, 'chunks': man}},
          open(os.path.join(OUT, 'manifest.json'), 'w'), separators=(',', ':'))
if '--cache' in OPT:
    pickle.dump(NEWC, open(OPT['--cache'][0] + '.tmp', 'wb'), protocol=5); os.replace(OPT['--cache'][0] + '.tmp', OPT['--cache'][0])
for z in range(MINZ, MAXZ + 1): log(f'z{z}: {sizes[z][0]} tiles{" encoded" if PD else ""}, {sizes[z][1] / 1e6:.1f} MB')
cs = np.array([len(b) for b in blobs.values()] or [0])
log(f'total {sizes[:, 0].sum()} tiles{" encoded" if PD else ""}, {sizes[:, 1].sum() / 1e6:.1f} MB in {fi} files, {len(man)} chunks '
    f'(largest {cs.max() / 1e6:.2f} MB, {(cs > 500_000).sum()} over 500 KB)')
