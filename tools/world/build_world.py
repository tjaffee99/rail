"""Build the rail network of the rest of the world (everything but mainland China, Hong Kong and Macau, which
come from tools/build_network.py + curate.py) from the raw pickle of tools/world/extract_world.py.

    python3 tools/world/build_world.py build/world/raw.pickle build/world [-v] [--cache DIR]
                                            (-v: list what is dropped and why; DBG=<relation ids> traces relations;
                                             --cache: reuse each route's processing from the last run, see R = {} below)

Writes <outdir>/network.json ({lines, stations, cities}: the records of tools/world/FORMAT.md, local ids, ISO
country codes), <outdir>/sources.json ([OSM relation ids] per line, for audits) and <outdir>/geometry.pickle
({'geometry': [way ids per line], 'ways': {way id: (coords, tags, refs)}}: every railway=rail/narrow_gauge way, for
routing gaps in the tile step, plus every way a line uses), and <outdir>/lines.pickle (the way lists alone, what
identifies the ways, and a digest of network.json: assemble.py --lean reads it instead of geometry.pickle; copy it
along). Reads the Wikidata caches of tools/world/wikidata.py.

Routes    passenger route relations (route=train/subway/light_rail/tram/monorail/funicular) that are open (no lifecycle
          tags, no future opening date, their own track mostly open rail) and not junk (freight, museum / heritage /
          park / children's railways, theme parks, airside people movers, chairlifts, depot and event runs: words in
          the route's own name, operator, network or brand, not in its stops' names) nor in the audited drop list
          (tools/world/ref/drops.json: relation ids). A junk route_master drops routes that name no operator of their own.
          Stops: stop / platform members (any role spelling) and PTv1 role-less stop nodes, snapped to their station
          (same or similar name within 400 m, else the nearest station of the mode; "Voie B" by distance). With its
          track (the longest path through its ways) a route's stops are put in track order (a forking route: the stops
          off that path become branches along their own stretch of track), stations are added from the track when the
          relation lists few of them (all of them for local services, only the ones other long-distance trains stop at
          for expresses), the stations at the ends of the track become termini, and ways past the end stops are cut.
Lines     one line per service: a route_master (unless its name is only an operator's) or lone routes, clustered
          around a representative by ref and network (sharing a quarter of its stations; a category given as a ref,
          "R" of Polregio, is no ref), or by shared stations (containment >= 0.8, Jaccard >= 0.6); never transitively.
          Train-number relations (THSR, TRA, elektrichki, TGV missions ...) group into corridors the same way. The
          longest route is the main stop list; other routes add branches (their divergent part, 2+ stations) or single
          one-direction stops. Shinkansen / HSR infrastructure relations anchor their services.
Fallback  route=railway relations (and timetable / infrastructure relations tagged route=train) where passenger
          services are not mapped: not freight, abandoned or listed, 3+ stations with passenger evidence on their own
          track (more than a public_transport tag where a country's services are mapped), and most of their stations
          not already served by a service line; a long line whose services cover half of it (200+ stations or 1500+ km:
          the Trans-Siberian) or 80% (100+ stations: St Petersburg - Warsaw) is left out, never shipped in pieces; its
          unserved stations go into the services that call on either side of them. One on a service's track (80% of it)
          gives that service its missing stations instead; one whose track has a 100+ km hole is a line per piece.
Kinds     h only for high-speed services (service / highspeed tags, HSR brands, or long-distance products on 250 km/h
          track); s for S-Bahn / RER / commuter / elektrichka (also route=light_rail S-Bahns); m l t f by mode.
Names     native names cleaned of route descriptions, platform numbers, codes; English (tools/world/english.py): a Latin
          native name as is, else the node's Wikidata label, a nearby Wikidata station's, name:en / int_name /
          name:<lang>-Latn, the place it is named after, a transliteration. Line names that are train numbers, categories
          or operators become "A – B" from the ends the relation names or the termini; stop lists read in the name's
          direction. Operators in English, their logo key the Wikidata item that has a logo (english_operator()).
Then duplicates (also night lines whose stops day lines serve) are dropped, stations merged by name, transfers and
complexes found, and urban lines get a city (city_of()). Lines mostly in mainland China, Hong Kong or Macau are left out.
"""
import datetime, gc, heapq, json, math, os, pickle, re, sys, time
from collections import Counter, defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))
from countries import country_code
from translit import latin, plain, ja_word, KANA, HAN, HANGUL, CYR, ABJAD, TIFINAGH
from english import english_station, english_line, english_operator, line_operator, line_values, infra_network, latinize, NOT_OPERATOR
import wikidata as wd

T0 = time.time()
def log(*a): print(f'{time.time() - T0:7.1f}s', *a, flush=True)
VERBOSE = '-v' in sys.argv
DBG = {int(x) for x in os.environ.get('DBG', '').split(',') if x}      # relation ids to trace
def drop_log(why, *a):
    if VERBOSE: print('  drop', why + ':', *a)

raw = pickle.load(open(sys.argv[1], 'rb')); OUT = sys.argv[2]
RELS, WAYS, NODES, PLACES = raw['relations'], raw['ways'], raw['stations'], raw['places']
del raw
# the pickle holds a copy of every tag key and value per way / member: share them (saves GBs on the planet)
_str = {}
def share(x): return _str.setdefault(x, x) if len(x) < 40 else x
for w, (t, c, refs) in WAYS.items(): WAYS[w] = ({share(k): share(v) for k, v in t.items()}, c, refs)
for r in RELS.values(): r['members'] = [(typ, ref, share(role)) for typ, ref, role in r['members']]
del _str; gc.collect()
log('loaded', len(RELS), 'relations', len(WAYS), 'ways', len(NODES), 'nodes', len(PLACES), 'places')
_date = os.path.join(os.path.dirname(__file__), '..', '..', 'build', 'DATE')
DATE = open(_date).read().strip() if os.path.exists(_date) else datetime.date.today().isoformat()
MODES = {'train', 'subway', 'light_rail', 'tram', 'monorail', 'funicular'}
URBAN = {'subway': 'm', 'light_rail': 'l', 'monorail': 'l', 'tram': 't', 'funicular': 'f'}
RANK = {'h': 0, 'r': 1, 's': 2, 'm': 3, 'l': 4, 'f': 5, 't': 6}
EXCLUDE = {'CN', 'HK', 'MO'}          # the China pipeline covers these
# relations an audit found are not passenger lines (heritage, freight, duplicates, closed ...): {relation id: why}
# ("w<way id>": a synthetic relation made from unmapped track that has this way)
DROP = {int(k[1:]) * -1 if k.startswith('w') else int(k): v
        for k, v in json.load(open(os.path.join(os.path.dirname(__file__), 'ref', 'drops.json'))).items()}
DROP_WAYS = {-k: v for k, v in DROP.items() if k < 0}
# lines an audit found missing: {relation id: why} (a railway relation the fallback takes without the passenger-evidence,
# freight-name and served-stretch tests) and {"w<way id>": why} (the unmapped track round this way becomes a synthetic
# railway relation in any country, taken the same way)
_force = os.path.join(os.path.dirname(__file__), 'ref', 'force.json')
FORCE_FB = {int(k[1:]) * -1 if k.startswith('w') else int(k): v for k, v in (json.load(open(_force)).items() if os.path.exists(_force) else ())}
FORCE_WAYS = {-k for k in FORCE_FB if k < 0}

# ---------------------------------------------------------------- geometry helpers
def metres(lon1, lat1, lon2, lat2):
    return math.hypot((lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2)), lat2 - lat1) * 111195
def dist_to_ways(ways, lon, lat):
    k = math.cos(math.radians(lat)); best = 1e18
    for w in ways:
        c = WAYS[w][1]
        P = np.column_stack([(c[:, 0] - lon) * k, c[:, 1] - lat]) * 111195
        A, B = P[:-1], P[1:]; AB = B - A
        t = np.clip(-(A * AB).sum(1) / np.maximum((AB * AB).sum(1), 1e-9), 0, 1)
        best = min(best, float(np.hypot(*(A + AB * t[:, None]).T).min()))
    return best
_len = {}
def way_len(w):
    if w not in _len:
        c = WAYS[w][1]; _len[w] = float(np.hypot(np.diff(c[:, 0]) * math.cos(math.radians(c[0, 1])), np.diff(c[:, 1])).sum() * 111195)
    return _len[w]

class Grid:
    """Keyed points in cells of `size` degrees; near(lon, lat, r) -> [(metres, key)] within r, nearest first."""
    def __init__(self, items, size):
        self.s, self.c = size, defaultdict(list)
        for k, lon, lat in items: self.c[(math.floor(lon / size), math.floor(lat / size))].append((k, lon, lat))
    def near(self, lon, lat, r):
        cx, cy = math.floor(lon / self.s), math.floor(lat / self.s)
        ny = int(r / (self.s * 111195)) + 1; nx = int(r / (self.s * 111195 * max(math.cos(math.radians(lat)), 0.05))) + 1
        out = []
        for i in range(-nx, nx + 1):
            for j in range(-ny, ny + 1):
                for k, x, y in self.c.get((cx + i, cy + j), ()):
                    d = metres(lon, lat, x, y)
                    if d <= r: out.append((d, k))
        out.sort(key=lambda a: a[0])
        return out

# rail track as cells of 0.0025° (~250 m): which cells any open track passes through (stops of lines still being
# built are dropped), and which main-line ways pass near passenger stations (the fallback lines' stations)
CELL = 0.0025
def cells(lon, lat): return (np.floor(lon / CELL).astype(np.int64) + 80000) * 100000 + np.floor(lat / CELL).astype(np.int64) + 40000
def samples(wids, step=0.001):
    """Points at most `step` degrees apart along the ways -> (coords, index into wids)."""
    cs = [WAYS[w][1] for w in wids]
    n = np.fromiter((len(c) for c in cs), np.int64, len(cs))
    C = np.concatenate(cs); wi = np.repeat(np.arange(len(cs)), n)
    ok = wi[:-1] == wi[1:]; A, B, wa = C[:-1][ok], C[1:][ok], wi[:-1][ok]
    k = np.ceil(np.abs(B - A).max(1) / step).astype(np.int64)
    m = k > 1; A, B, wa, k = A[m], B[m], wa[m], k[m] - 1
    seg = np.repeat(np.arange(len(k)), k)
    t = (np.arange(len(seg)) - np.repeat(np.cumsum(k) - k, k) + 1) / np.repeat(k + 1, k)
    return np.concatenate([C, A[seg] + (B[seg] - A[seg]) * t[:, None]]), np.concatenate([wi, wa[seg]])
def is_main(t): return t.get('railway') in ('rail', 'narrow_gauge') and not t.get('service')
ALL_W = list(WAYS); MAIN_W = [w for w in ALL_W if is_main(WAYS[w][0])]
rc = []
for i in range(0, len(ALL_W), 200000):
    P, _ = samples(ALL_W[i:i + 200000]); rc.append(np.unique(cells(P[:, 0], P[:, 1])))
RAIL_CELLS = np.unique(np.concatenate(rc)) if rc else np.zeros(0, np.int64)
del rc
OFFS = np.array([dx * 100000 + dy for dx in range(-2, 3) for dy in range(-2, 3)], np.int64)
def near_track(nids):
    """nids within ~500 m of open track (vectorised)."""
    if not nids or not len(RAIL_CELLS): return set()
    X = np.array([NODES[n][:2] for n in nids]); K = cells(X[:, 0], X[:, 1])[:, None] + OFFS[None, :]
    hit = (RAIL_CELLS[np.minimum(np.searchsorted(RAIL_CELLS, K), len(RAIL_CELLS) - 1)] == K).any(1)
    return {n for n, h in zip(nids, hit) if h}
log('rail cells', len(RAIL_CELLS))

class NearIdx:
    """Nearest of many points P (lon, lat) for query points: looked up in the 3 x 3 cells of `size` degrees around each
    query point (exact within `size`), brute force for the few with none there. near(X) -> (index into P, metres)."""
    def __init__(self, P, size):
        self.P, self.s = P, size
        key = np.floor(P[:, 0] / size).astype(np.int64) * 4000003 + np.floor(P[:, 1] / size).astype(np.int64)
        o = np.argsort(key, kind='stable'); u, st, cnt = np.unique(key[o], return_index=True, return_counts=True)
        self.o, self.at = o, {k: (a, a + c) for k, a, c in zip(u.tolist(), st.tolist(), cnt.tolist())}
    def near(self, X):
        X = np.asarray(X, float).reshape(-1, 2); j = np.zeros(len(X), np.int64); d = np.full(len(X), 1e18)
        for i, (x, y) in enumerate(X.tolist()):
            cx, cy = math.floor(x / self.s), math.floor(y / self.s)
            idx = [self.o[a:b] for k in ((cx + a) * 4000003 + cy + b for a in (-1, 0, 1) for b in (-1, 0, 1)) if k in self.at
                   for a, b in [self.at[k]]]
            idx = np.concatenate(idx) if idx else np.arange(len(self.P))
            Q = self.P[idx]; dd = np.hypot((Q[:, 0] - x) * math.cos(math.radians(y)), Q[:, 1] - y)
            m = int(dd.argmin()); j[i] = idx[m]; d[i] = dd[m] * 111195
        return j, d

class Track:
    """The main path through a set of ways (build_network.py chain(): longest path per connected piece, gaps under 300 m
    bridged, pieces beside a longer one dropped, the others joined nearest end first), sampled every ~25 m:
    locate(points) -> (distance along, distance off) per point."""
    def __init__(self, way_ids, dbg=False):
        refs = [WAYS[w][2] for w in way_ids]
        allr = np.concatenate(refs); u, cnt = np.unique(allr, return_counts=True)
        junc = set(u[cnt > 1].tolist())
        adj, seg = defaultdict(list), []          # vertices: way ends and shared nodes; edges: way pieces between them
        for w, r in zip(way_ids, refs):
            c = WAYS[w][1]; rl = r.tolist()
            cut = [0] + [i for i in range(1, len(rl) - 1) if rl[i] in junc] + [len(rl) - 1]
            for a, b in zip(cut, cut[1:]):
                P = c[a:b + 1]; L = float(np.hypot(np.diff(P[:, 0]) * math.cos(math.radians(P[0, 1])), np.diff(P[:, 1])).sum() * 111195)
                e = len(seg); seg.append((rl[a], rl[b], P, L)); adj[rl[a]].append((rl[b], L, e)); adj[rl[b]].append((rl[a], L, e))
        # gaps (ways that end near each other without sharing a node): bridge each dead end to the nearest vertex of
        # another piece within 300 m
        pos = {}
        for a, b, P, L in seg: pos[a] = P[0]; pos[b] = P[-1]
        comp, cid = {}, 0
        for v0 in adj:
            if v0 in comp: continue
            stack = [v0]; comp[v0] = cid
            while stack:
                for y, _, _ in adj[stack.pop()]:
                    if y not in comp: comp[y] = cid; stack.append(y)
            cid += 1
        if cid > 1:
            vg = Grid(((v, float(x), float(y)) for v, (x, y) in pos.items()), 0.003)
            for v in [v for v in adj if len(adj[v]) == 1]:
                u = next((u for d, u in vg.near(float(pos[v][0]), float(pos[v][1]), 300) if comp[u] != comp[v]), None)
                if u is not None:
                    P = np.array([pos[v], pos[u]]); L = metres(*P[0], *P[1]) + 1
                    e = len(seg); seg.append((v, u, P, L)); adj[v].append((u, L, e)); adj[u].append((v, L, e))
        def sweep(src):
            dist, prev, h = {src: 0.0}, {}, [(0.0, src)]
            while h:
                du, x = heapq.heappop(h)
                if du > dist[x]: continue
                for y, L, e in adj[x]:
                    if du + L < dist.get(y, 1e18): dist[y] = du + L; prev[y] = (x, e); heapq.heappush(h, (du + L, y))
            return max(dist, key=dist.get), prev, dist
        def path(b, prev):
            parts, x = [], b
            while x in prev:
                y, e = prev[x]; P = seg[e][2]
                parts.append(P if seg[e][0] == y else P[::-1]); x = y
            return np.concatenate([p[:-1] for p in reversed(parts)] + [parts[0][-1:]])
        def folds(Q):       # out on one track and back on the other (a double track joined at one end only): a quarter
            k = math.cos(math.radians(float(Q[0, 1])))      # of the path lies in ~300 m cells it passes 3+ km apart
            c = np.concatenate([[0], np.cumsum(np.hypot(np.diff(Q[:, 0]) * k, np.diff(Q[:, 1])) * 111195)])
            if c[-1] < 6000: return False
            t = np.arange(0, c[-1], 100.0)
            key = np.floor(np.interp(t, c, Q[:, 0]) * k / 0.003).astype(np.int64) * 1000003 + np.floor(np.interp(t, c, Q[:, 1]) / 0.003).astype(np.int64)
            u, inv = np.unique(key, return_inverse=True)
            lo = np.full(len(u), np.inf); hi = np.full(len(u), -np.inf); np.minimum.at(lo, inv, t); np.maximum.at(hi, inv, t)
            return ((hi - lo)[inv] > 3000).mean() >= 0.25
        seen, pieces = set(), []
        for n0 in list(adj):
            if n0 in seen: continue
            a, _, dist = sweep(n0); seen |= set(dist)
            b, prev, dist = sweep(a)
            if dist[b] < 300: continue
            Q = path(b, prev)
            if folds(Q):     # then the line runs between the two points farthest apart
                V = list(dist); X = np.array([pos[v] for v in V]); X[:, 0] *= math.cos(math.radians(float(X[0, 1])))
                i = int(((X - X[0]) ** 2).sum(1).argmax()); j = int(((X - X[i]) ** 2).sum(1).argmax())
                _, prev, dist2 = sweep(V[i])
                if V[j] in prev: Q = path(V[j], prev); b = V[j]; dist = dist2
            pieces.append((dist[b], Q))
        self.ok = bool(pieces)
        if dbg: print('  DBG track', len(way_ids), 'ways', cid, 'components', [(round(L), tuple(np.round(Q[0], 3)), tuple(np.round(Q[-1], 3))) for L, Q in sorted(pieces, key=lambda p: -p[0])[:8]])
        if not pieces: return
        pieces.sort(key=lambda p: -p[0]); out = pieces[0][1]; rest = []
        for _, Q in pieces[1:]:            # a piece alongside a longer one (the other track of a double line) adds nothing
            idx = NearIdx(np.concatenate([out] + rest), 0.002)
            if (idx.near(Q[::max(1, len(Q) // 50)])[1] < 150).mean() < 0.6: rest.append(Q)
        jn = []              # the path's segments that join two pieces
        while rest:
            d, j, end, rev = min((metres(*(out[0] if end == 0 else out[-1]), *(Q[-1] if end == 0 else Q[0])), j, end, rev)
                                 for j, P in enumerate(rest) for end in (0, 1) for rev, Q in ((0, P), (1, P[::-1])))
            Q = rest.pop(j); Q = Q[::-1] if rev else Q
            jn = [i + len(Q) for i in jn] + [len(Q) - 1] if end == 0 else jn + [len(out) - 1]
            out = np.concatenate([Q, out]) if end == 0 else np.concatenate([out, Q])
        k = math.cos(math.radians(float(out[:, 1].mean())))
        seglen = np.hypot(np.diff(out[:, 0]) * k, np.diff(out[:, 1])) * 111195
        n = np.maximum(np.ceil(seglen / 25).astype(np.int64), 1)
        idx = np.repeat(np.arange(len(seglen)), n); f = (np.arange(len(idx)) - np.repeat(np.cumsum(n) - n, n)) / np.repeat(n, n)
        self.P = np.concatenate([out[:-1][idx] + (out[1:][idx] - out[:-1][idx]) * f[:, None], out[-1:]])
        cum = np.concatenate([[0], np.cumsum(seglen)])
        self.T = np.concatenate([cum[:-1][idx] + seglen[idx] * f, cum[-1:]]); self.L = float(cum[-1])
        self.gaps = [(float(cum[i]), float(cum[i + 1])) for i in jn if seglen[i] > 1000]      # stretches no mapped track covers
        self.idx = NearIdx(self.P, 0.005)
    def locate(self, X):
        j, d = self.idx.near(X)
        return self.T[j], d
def track(ways):
    tr = Track(ways) if ways else None
    return tr if tr is not None and tr.ok else None
PASS2 = {}           # tracks of expresses without stops, kept for pass 2 (the others are not kept: they would take GBs)

# ---------------------------------------------------------------- names
def first(v): return (v or '').split(';')[0].strip()
ARROW = r'(?:<=>|<->|<>|=+\.?>|-+>|= >|–>|<=|<-|\s--\s|→|←|⟶|⇒|⇐|↔|⇔|⟷|⇄|⇆|⇋|⇌|↑|↓|⇅|↺|↻|＝＞|⇨|➔|\|\||>)'
DIRWORD = (r'(?:上り|下り|外回り|内回り|北向|南向|東向|西向|南下|北上|上行|下行|逆行|順行|往\S+|opposite|oppsite|reverse|inbound|outbound|outbond|northbound|southbound|eastbound|westbound|clockwise|'
           r'(?:north|south|east|west)[\s-]?bound|anti-?clockwise|counter-?clockwise|outer|inner|fast|slow|stopping|semi-?fast|express|local|rapid|return|Hinfahrt|Rückfahrt|'
           r'Richtung|aller|retour|ida|vuelta|прямое|обратное|прямий|зворотний|туда|обратно|[A-Z]{4}\b|[^()（）]*\s행)')
SERVICE_JA = r'(?:各駅停車|各停|各駅|普通|快速急行|快速特急|通勤特急|通勤急行|通勤快速|通勤準急|区間急行|区間準急|区間快速|準急|急行|快特|特急|快速)'
# words that make a whole name generic: a category, a product or "train" (the line is then named after its termini)
CATEGORY = (r'(?:(?:пригородн\w*|городск\w*|туристическ\w*|(?:высоко)?скоростн\w*|'
            r'грузопассажирск\w*)\s+)?(?:электропоезд|дизель-?(?:электро)?поезд|поезд|рельсобус|автомотриса|'
            r'электричка)(?:[\s-]+экспресс)?|электропоезд-экспресс|скор\w+ поезд|пассажирск\w+ поезд|'
            r'фирменн\w+ поезд|приміськ\w+ поїзд|поїзд|потяг|електричка|прыгарадн\w* (?:электра)?цягнік|'
            r'электрацягнік|цягнік|пригородный|пассажирский|скорый|train|trains|tren|treno|trem|trein|zug|tog|tåg|'
            r'juna|vlak|voz|влак|pociąg|vonat|vlaky|tren regional|regionale?|regional train|regionalzug|intercity|'
            r'inter-city|interregio|express|expreso|rapid|local|passenger|passenger train|mail|superfast|'
            r'superfast express|express train|mail express|memu|demu|emu|dmu|media distancia|larga distancia|'
            r'cercanías|rodalies|subway|metro|tram|tramway|light rail|monorail|funicular|service|stopping service|'
            r'regional express|regionalexpress|unknown|unbekannt|train service|railway|railroad|line|linie|linia|navette|shuttle|lanzadera|'
            r'ligne|linea|línea|línia|route|系統|号系統|号線|jr local train|列車|普通|快速|急行|特急|普通列車|火车|火車|列车|기차|열차|قطار|רכבת|'
            r'მატარებელი|գնացք|qatar|qatar\w*|خط|трамвай|трамвайн\w* маршрут|маршрут(?: трамвая)?|троллейбус|'
            r'монорельс|фуникул[её]р|фунікулер|tramvaj|tramvay|tranvía|tranvia|tramvia|straßenbahn|strassenbahn|'
            r'tramwaj|villamos|hatt[ıi]|yüksek hızlı (?:tren|demiryolu)|hızlı tren|bölgesel tren|yht|banliyö treni|'
            r'treni|track|track [ivx]+|relief line|branch line|chord line|loop line|bypass|nattåg|nachtzug|'
            r'night train|nightjet|sleeper|through coach|kurswagen|беспересадочный вагон')
_NO = r'\d{1,5}(?:[A-ZА-ЯЁa-zа-яё]{0,2}(?:\([\dA-ZА-ЯЁa-zа-яё]{1,2}\))?[A-ZА-ЯЁa-zа-яё]?)'        # 747А, 099А(С), 013(4)Ж
TRAIN_NO = r'(?:№\s*)?' + _NO + r'(?:\s*[/,-]\s*' + _NO + r')*(?:\s*(?:UP|DN|Up|Dn|up|dn|次|號|号|列車|호|열차))?'
def desc(n):
    """The route description of a name, arrows as dashes: "DLR: Bank → Lewisham" -> "Bank – Lewisham"."""
    parts = re.split(r'\s*[:：]\s*', n or '', 1)
    n = parts[1] if len(parts) > 1 else parts[0] if re.search(ARROW + r'|\s-\s|_', parts[0]) else ''
    n = re.sub(r'\s*\((?:[^()]*(?:via|über|par|per|por|через)[^()]*|[^()]*' + DIRWORD + r'[^()]*)\)', '', n, flags=re.I)
    return re.sub(r'\s+', ' ', re.sub(r'\s*' + ARROW + r'\s*|\s+[-–—]\s+|_', ' – ', n)).strip(' –')
def clean_name(n):
    """Line name without the route description: "東京メトロ銀座線 : 浅草→渋谷" -> "東京メトロ銀座線",
    "Western Line (fast): Churchgate => Virar" -> "Western Line", "Train 11021 Chalukya Express: …" -> "Chalukya Express",
    "London Liverpool Street => Clacton-on-Sea" -> '' (only a description), "Transilien N (DAPO)" -> "Transilien N"."""
    n = re.sub(r'\s+', ' ', first(n) if ';' in (n or '') and not re.search(ARROW, n or '') else (n or '')).strip()
    n = re.sub(r'\s*_\s*', ': ', n.replace('（', '(').replace('）', ')'))
    n = re.sub(r'\s*' + ARROW + r'\s*$', '', n)
    m = re.match(r'FUN \S+ \((.+)\)$', n) or re.match(r'((?:' + CATEGORY + r')\s*(?:№|No\.?|Nr\.?|#)?\s*\w{1,4})(?:\s+[-–]\s+\S.*\s[-–]\s|\.\s+\S|\s+[^-–\s][^-–]*\s[-–]\s)', n, re.I) or \
        re.match(r'((?:' + CATEGORY + r'))\.\s+\S', n, re.I)
    if m: return m.group(1)
    parts = re.split(r'\s*[:：]\s*', n, 1)
    if re.fullmatch(r'(?:Train|Zug|Treno|Tren|Поезд)?\s*' + TRAIN_NO, parts[0]) and len(parts) > 1 and not re.search(ARROW, parts[1]):
        return parts[1]      # "Train 20991: Ajmer Daund Superfast Special"
    if len(parts[0]) >= 1 and (len(parts[0]) >= 2 or len(parts) == 1 or re.match(r'\w', parts[0])): n = parts[0]
    n = re.sub(r'(?i)\s*\((?:[^()]*\b(?:am|pm|rush|daytime|weekdays?|weekends?|nights?|evenings?|peak|off-peak|only|except|'
               r'mon|tue|wed|thu|fri|sat|sun|sundays?|holidays?)\b[^()]*)\)', '', n)
    n = re.sub(r'^[A-Z]{2,6}\s+[-–]\s+(?=[^-–]{1,14}$)', '', n)      # "NYCS - G Train"
    n = re.sub(r'\s*\((?:[^()]|\([^()]*\))*' + ARROW + r'(?:[^()]|\([^()]*\))*\)', '', n)
    n = re.sub(r'\s*\(\s*' + DIRWORD + r'[^()]*\)', '', n, flags=re.I)
    n = re.sub(r'\s*\((?:[A-Z]{4}|rapide|semi-direct|direct|omnibus|кольцев\w*|circular|loop|Mon-\w+[^()]*|daytime|weekdays?|weekends?|early am|late pm)\)', '', n, flags=re.I)
    n = re.sub(r'\s*\(?(?:上り|下り|外回り|内回り|北向|南向|南下|北上|上行|下行|逆行|順行)\)?$', '', n)
    n = re.sub(r'\s+(?:North|South|East|West)(?:bound)?(?=\s*\(|$)(?:\s*\([^()]*\))?$|\s+(?:Northbound|Southbound|Eastbound|Westbound)\b', '', n)
    if re.search(ARROW, n): return ''
    n = re.sub(r'^(?:Train|Zug|Treno|Tren|Trein|Comboio|Pociąg|Vlak|Vonat|Поезд)\s+(?=' + TRAIN_NO + r'(?:\s|$)|[A-Z]{1,4} ?\d)|^Train\s+(?=[A-Z])', '', n)
    n = re.sub(r'^\d{5}(?:/\d+)?\s+(?=\D)', '', n); n = re.sub(r'(?<=\D)\s+\d{5}(?:/\d+)?$', '', n)
    n = n.split(';')[0]
    m = re.fullmatch(r'(.*?)\s*[«"„“]([^»"“”]{3,})[»"“”]\s*(?:' + TRAIN_NO + r')?\s*', n)
    if m and generic(m.group(1)): n = m.group(2)                   # a named train: 'Скорый поезд 001Э/002Э «Россия»' -> 'Россия'
    n = re.sub(r'^(?:KBS|Kursbuchstrecke)\s*\d+\s*', '', n)
    n = re.sub(r'\s+and$|\s*/\d+$', '', n)
    m = re.match(r'(.{2,}?)\s*' + SERVICE_JA + r'$', n)
    if m and not re.search(r'[(（]', n): n = m.group(1)
    return n.strip(' -–_;')
PRODUCT_NAME = re.compile(r'(?:TGV(?: InOui| Lyria)?|InOui|Ouigo(?: España)?|Lyria|ICE|ICE Sprinter|IC|ICN|EC|EN|RJX?|railjet|Nightjet|'
                          r'Frecciarossa|Frecciargento|Frecciabianca|Italo|AVE|Avlo|Alvia|Avant|Iryo|Euromed|Intercity(?: direct)?|InterCity|'
                          r'EuroCity|EuroNight|Eurostar|Thalys|Sprinter|KTX(?:-산천|-이음)?|SRT|ITX(?:-새마을|-청춘|-마음)?|무궁화호|새마을호|'
                          r'Mugunghwa(?:-ho)?|Saemaeul(?:-ho)?|Intercités|TER|Regio|REX|Alfa Pendular|Pendolino|Sapsan|Сапсан|Ласточка|'
                          r'Lastochka|YHT|Allegro|Snabbtåg|X2000|Flytoget|Aeroexpress|Аэроэкспресс|Vande Bharat(?: Express)?|Rajdhani(?: Express)?|'
                          r'Shatabdi(?: Express)?|Duronto(?: Express)?|自強號?|莒光號?|區間車|區間快車?|普悠瑪號?|太魯閣號?|Tzu-chiang|Chu-kuang)', re.I)
def strip_no(n): return re.sub(r'\s*(?:No\.?|Nr\.?|№|#)?\s*' + TRAIN_NO + r'$', '', n).strip(' -–:')
OPKEYS = ('operator', 'operator:en', 'network', 'network:en', 'brand', 'operator:short', 'network:short')
_broad, OPLIKE = [], set()
def broad():
    """Operator / network / brand values that several lines carry (an operator's name, not a line's)."""
    if not _broad:
        acc = defaultdict(set)
        for r in RELS.values():
            t = r['tags']
            if t.get('type') not in ('route', 'route_master'): continue
            b = re.sub(r'\W', '', (t.get('ref') or t.get('name') or '').lower())[:10]
            for k in OPKEYS:
                for v in (t.get(k) or '').split(';'): acc[v.strip().lower()].add(b)
        _broad.append({v for v, x in acc.items() if len(x) >= 3 and v})
    return _broad[0]
def generic(n, ts=()):
    """A name that does not name a line: a category word, a train number, only codes, a product or an operator."""
    s = re.sub(r'\s+', ' ', (n or '').lower()).strip(' -–:')
    if not s: return True
    s = re.sub(r'\b(?:no\.?|nr\.?|n°|№|#)\s*', '', s)
    s = re.sub(r'(?<![a-zа-я])' + TRAIN_NO.replace('A-ZА-ЯЁ', 'a-zа-яё') + r'(?![a-zа-я])', ' ', s).strip()
    s = re.sub(r'\s+', ' ', s).strip(' -–:/,#')
    if not s or re.fullmatch(r'(?:' + CATEGORY + r')(?:\s+(?:' + CATEGORY + r'))*', s): return True
    if re.fullmatch(r'[a-z]{0,3}', s): return True                  # "R", "RE", "IC" + number
    toks = [x for x in re.split(r'[\s/,;+]+', (n or '').strip()) if x]
    code = [x for x in toks if not re.fullmatch(CATEGORY, x, re.I)]
    if toks and all(re.fullmatch(r'(?:[A-Za-z]{1,5}[-\s]?)?\d{0,5}[A-Za-z]?\+?', x) for x in code) and \
            (re.search(r'\d', n or '') or all(len(x) <= 3 and x.isupper() for x in code)): return True    # "RE / TER K59", "GR E", "G Train"
    if PRODUCT_NAME.fullmatch(strip_no(n or '')): return True     # a product: "TGV InOui 801A", "Intercity", "Sprinter"
    ops = {first(t.get(k)).lower() for t in ts for k in OPKEYS if t.get(k)} & (OPLIKE or broad())
    if re.match(r'(?:rete ferroviaria|réseau ferré|red ferroviaria|rail network)\b', s): return True     # a network ("Rete ferroviaria AV d'Italia")
    parts = re.split(r'\s*[-/&+]\s*', strip_no(n or '').lower())
    if len(parts) >= 2 and all(p in (OPLIKE or broad()) for p in parts): return True     # operators only: "Renfe-SNCF"
    return (n or '').lower() in ops or s in ops or strip_no(n or '').lower() in ops
def base_key(n):
    """Name for grouping; a bare route ("Waterloo - Guildford via Epsom") as its unordered ends."""
    n = clean_name(n); parts = re.split(r'\s*[-–]\s*', re.sub(r'\s+via\s+.*$', '', n)) if not re.search(r'\w-\w', n) else re.split(r'\s+[-–]\s+', n)
    if len(parts) > 1: n = '-'.join(sorted(re.sub(r'(?i)(?:\s+(?:' + CATEGORY + r'))+$', '', p) for p in parts))    # "Konya - Ankara YHT Hattı"
    return re.sub(r'[\s\-–·・•()（）\[\]"\'.,]', '', n).lower()
def good_ref(r, name=''):
    """A short line code; not a long or descriptive ref, nor a copy of a non-Latin name ("JR水戸線"), nor a train number."""
    r = first(r)
    m = re.match(r'(\d\w{0,2}|[A-Z]\d{0,2})\s+\S', r)
    if len(r) > 12 and m: r = m.group(1)      # "1 Холодногірсько-Заводська" -> "1"
    if len(r) > 12 and not (len(r) <= 20 and r.isascii() and r.lower() in clean_name(name).lower()): return ''    # "Waterloo & City",
                                                                                            # "Empire Service" of "Amtrak Empire Service"
    if not r or '(' in r or re.search(r'[\u3100-\u312f]', r) or re.search(ARROW + r'|\w\s*[–—]\s*\w', r) or (r == clean_name(name) and not r.isascii()): return ''
    if re.fullmatch(r'(?:[A-Za-zА-Яа-я]{1,5}\s*)?\d{3,6}[A-Za-zА-Яа-я]?(?:\s*[/,]\s*\d{1,6}[A-Za-zА-Яа-я]?)*', r): return ''   # train numbers
    if re.fullmatch(r'\d[Xx]{2}', r) or re.fullmatch(CATEGORY, r, re.I) or PRODUCT_NAME.fullmatch(r): return ''     # Caltrain 1XX, "MEMU", "AVE"
    return r
STN_WORDS = r'(?i)(?:\s*[,(-]\s*|\s+)(?:railway|railroad|train|rail|metro|mrt|lrt|subway|monorail|tram|light rail)\s+station\)?$|\s+Sta\.?$|-eki$'
PLATFORM = (r'(?i)\s*[-–,(.\[]?\s*\b(?:v[íi]a|and[ée]n|track|platform|plataforma|gleis|gl\.|voie|quai|binario|bin\.|peron|tor|kolej|'
            r'spoor|bahnsteig|perron|plattform|spår)\s*\d+[a-z]?(?:\s*(?:[-–/+&,]|and|und|et|y|i)\s*\d+[a-z]?)*\b\)?.*$|'
            r'\s*[-–,(]?\s*\b(?:voie|quai|track|platform|gleis|binario)\s+[A-Z]\d{0,2}\b\)?.*$|\s*\d+番線.*$|\s*站台$')      # "Tours - Voie A1"
PLATFORM_ONLY = re.compile(r'(?i)(?:v[íi]a|and[ée]n|track|platform|plataforma|gleis|voie|quai|binario|peron|tor|spoor|bahnsteig|perron|spår)'
                           r'(?:\s*[\dA-Z]{1,3}(?:\s*(?:bis|ter))?)?')        # a stop named only after its platform ("Voie B", "Voie 2 bis"): snapped by distance
def stn_name(t):
    """Station name without platform / track / pole numbers, codes and station words: "Central, Platform 9" -> "Central",
    "Atocha - Vía 3" -> "Atocha", "Stop 3: Lincoln Square" -> "Lincoln Square", "ایستگاه راه آهن تهران" -> "تهران"."""
    n = re.sub(r'\s+', ' ', first(t.get('name')) or '').strip()
    if not n: return ''
    if ABJAD.search(n) or TIFINAGH.search(n):     # "Alger ⴷⵣⴰⵢⴻⵔ الجزائر": the Latin part if there is one
        lat = re.findall(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ0-9' .\-]*[A-Za-zÀ-ɏ0-9]", n)
        if lat and len(max(lat, key=len)) >= 3: n = max(lat, key=len)
        else: n = re.sub(r'[ⴰ-⵿]+', '', n).strip(' -/')
    n = re.sub(r'^(?:ایستگاه(?:\s*(?:راه\s*آهن|مترو|قطار))?|محطة(?:\s*(?:قطار|القطار|مترو|سكة الحديد))?|توقفگاه|ریلوے اسٹیشن|'
               r'ស្ថានីយ៍(?:រថភ្លើង)?|гара|станция|станція|залізнична станція|ж\.?\s?д\.? станция|Estaci[óo]n(?= (?!de\b|del\b)[A-Z]))\s+', '', n)
    n = re.sub(r'^(?:قطار [^-–]+?|[^-–]*High[ -]Speed Rail\w*)\s*[-–]\s*', '', n)      # "Haramain High Speed Railway - Makkah"
    n = re.sub(r'^(?:Stop|Halt|Arrêt|Haltestelle)\s+[A-Z]?\d+[A-Za-z]?\s*[:.-]\s*|^Tranvía\s*-\s*Nº\s*\d+\s*-\s*|^[A-Z]{1,3}\d{1,3}[A-Z]?\s+(?=\S{3})', '', n)
    n = re.sub(PLATFORM, '', re.sub(r'(?i)^(?:platform|plataforma|gleis|voie|binario|peron|quai)\s*\d+[a-z]?\s*[,:-]\s*', '', n))
    n = re.sub(r'(?i)\s*\((?:KTM|ERL|LRT|MRT|BRT|Monorail|KG|KA\d+|north|south|east|west)(?:bound)?\)|\s+(?:Arrival|Departure|Arrivals|Departures)$|'
               r'\s*\((?:Arrival|Departure|(?:north|south|east|west)bound)\)|\s*[-–]\s*[^-–]*(?:Clockwise|Loop)[^-–]*$|\s*\((?:unfinished|closed)\)|\s*\([^()]*\blines?\)$|\s*\[[^\]]*\]|'
               r'\s*\((?:tief\w*|Tiefgleise|oben|unten|S-Bahn|U-Bahn|Fernbahn|RER|Métro|Metro|Cercanías|Rodalies|Terminal \d+)\)', '', n)
    if len(n) >= 3 and (n.endswith('駅') and not n.endswith('の駅') or n.endswith('역') or (n.endswith('站') and not n.endswith('車站') and not n.endswith('车站'))): n = n[:-1]
    n = re.sub(STN_WORDS, '', n).strip(' ,-–')
    return n or first(t.get('name'))
def skey(n): return re.sub(r'[\W_]+', '', plain(n.lower()).replace('&', 'and').replace('ʼ', "'").replace('’', "'"))   # "Hayes & Harlington" = "Hayes and Harlington"
GENERIC_STN = {'station', 'stn', 'sta', 'railway', 'rail', 'train', 'gare', 'bahnhof', 'bf', 'bhf', 'estacion', 'estación', 'estação',
               'estacao', 'stazione', 'stacja', 'dworzec', 'pkp', 'sncf', 'international', 'intl', 'de', 'la', 'le', 'del', 'di', 'do', 'da',
               'cercanías', 'cercanias', 'rodalies', 'sbahn', 's-bahn', 'ubahn', 'u-bahn', 'metro', 'subway', 'tram', 'tramway', 'mrt', 'lrt',
               'ktm', 'erl', 'dlr', 'rer', 'tube', 'underground', 'overground', 'vokzal', 'вокзал', 'станция', 'ст', 'жд', 'гара'}
CENTRAL = {'c', 'h', 's', 'central', 'centraal', 'centrale', 'centralstation', 'centralen', 'sentralstasjon', 'sentral', 'hovedbanegård',
           'hovedbanegaard', 'hovedstasjon', 'hbf', 'hauptbahnhof', 'hb', 'hlavní', 'glavny', 'glavnyi', 'главный', 'central station'}
PNAME = defaultdict(set)          # 1° cell -> skeys of place names (for city prefixes of station names)
for lon, lat, kind, name, en, pop in PLACES.values():
    for x in (name, en):
        if x: PNAME[(math.floor(lon), math.floor(lat))].add(skey(re.sub(r'(?:市|区|區|町|村)$', '', x)))
def mkey(name, lon, lat):
    """Name for matching stations: no city prefix, generic words, dots; central-station words as one:
    "London St. Pancras International" = "St Pancras", "Uppsala C" = "Uppsala central", "Praha hlavní nádraží" = "Hlavní nádraží"."""
    ws = [w for w in re.split(r'[\s\-–/,()]+', plain(name.lower()).replace('.', ' ').replace("'", '')) if w]
    near = set().union(*(PNAME.get((math.floor(lon) + i, math.floor(lat) + j), ()) for i in (-1, 0, 1) for j in (-1, 0, 1)))
    for k in (3, 2, 1):
        if len(ws) > k and (skey(''.join(ws[:k])) in near or k == 1 and ws[0].endswith('s') and skey(ws[0][:-1]) in near): ws = ws[k:]; break
    ws = ['central' if w in CENTRAL else w for w in ws if w not in GENERIC_STN] or ws
    return ' '.join(ws)
def related(a, b):
    """Two match keys name the same place: equal, or one is the other plus words ("st pancras" / "london st pancras")."""
    if a == b: return 2
    if min(len(a), len(b)) < 4: return 0
    s, l = (a, b) if len(a) < len(b) else (b, a)
    return 1 if re.search(r'(?<![\w])' + re.escape(s) + r'(?![\w])', l) else 0

CSS = dict(zip(('black silver gray grey white maroon red purple fuchsia magenta green lime olive yellow navy blue teal aqua cyan orange '
                'darkgreen darkblue darkred darkorange darkviolet lightblue lightgreen skyblue gold pink brown violet indigo turquoise '
                'crimson salmon coral tomato orchid khaki beige tan chocolate firebrick forestgreen seagreen royalblue steelblue '
                'dodgerblue deepskyblue midnightblue slateblue slategray darkgray lightgray deeppink hotpink plum limegreen '
                'yellowgreen olivedrab chartreuse springgreen mediumseagreen darkcyan cadetblue powderblue mediumpurple '
                'rebeccapurple darkmagenta mediumvioletred orangered goldenrod darkgoldenrod sienna peru saddlebrown lavender').split(),
               ('000000 C0C0C0 808080 808080 FFFFFF 800000 FF0000 800080 FF00FF FF00FF 008000 00FF00 808000 FFFF00 000080 0000FF 008080 '
                '00FFFF 00FFFF FFA500 006400 00008B 8B0000 FF8C00 9400D3 ADD8E6 90EE90 87CEEB FFD700 FFC0CB A52A2A EE82EE 4B0082 40E0D0 '
                'DC143C FA8072 FF7F50 FF6347 DA70D6 F0E68C F5F5DC D2B48C D2691E B22222 228B22 2E8B57 4169E1 4682B4 1E90FF 00BFFF 191970 '
                '6A5ACD 708090 A9A9A9 D3D3D3 FF1493 FF69B4 DDA0DD 32CD32 9ACD32 6B8E23 7FFF00 00FF7F 3CB371 008B8B 5F9EA0 B0E0E6 9370DB '
                '663399 8B008B C71585 FF4500 DAA520 B8860B A0522D CD853F 8B4513 E6E6FA').split()))
def colour(c):
    c = first(c).lower().replace(' ', '')
    c = CSS.get(c, c).lower()
    m = re.fullmatch(r'#?([0-9a-f]{6}|[0-9a-f]{3})', c)
    if not m: return ''
    h = m.group(1) if len(m.group(1)) == 6 else ''.join(x * 2 for x in m.group(1))
    return '' if min(int(h[i:i + 2], 16) for i in (0, 2, 4)) >= 0xF0 else '#' + h.upper()   # white would not show

# ---------------------------------------------------------------- operators
# not operators: fare associations and zones, national / generic networks, infrastructure managers, service groupings
NOT_OP = re.compile(r'verbund|tarif|\b(?:VOR|VBB|VRR|VRS|VRN|KVV|RMV|HVV|MVV|VVS|VGN|VMS|VVO|MDV|ZVV|OÖVV|VVT|SVV|NVV|AVV|VBN|GVH|NAH\.SH|'
                    r'VMT|VPE|VRT|VGF|VBL|TNW|OVV|Libero|Mobilis|Onde Verte|A-Welle|Ostwind|Passepartout|TransReno|Tarifverbund|Unireso|'
                    r'Frimobil|Arcobaleno|Engadin Mobil|ch-integral|CH-VS|Z-Pass|Takst|NRW-Tarif)\b|^national|^regional|'
                    r'network rail|trafikverket|bane nor|infrabel|prorail|sncf réseau|rfi\b|adif\b|db netz|infraestruturas|'
                    r'unknown|unbekannt|hoofdrailnet|national rail|area$|エリア|地区|ネットワーク|系統|線系|作戦|近郊区間|'
                    r'^[A-Z]{2}[:\-_]|rail network|railway network|nahverkehr|verkehrsgemeinschaft|^\w{1,2}$|^(?:'
                    r'national rail|regional|express|superfast|superfast express|long_distance|commuter|local|high_speed|international|'
                    r'ir|jr|passenger|memu|demu|emu|dmu|mail|rajdhani express|duronto express|shatabdi express|vande bharat express|'
                    r'double decker express|daily express|garib rath|jan shatabdi|humsafar express|sampark kranti|antyodaya express|'
                    r'train|tren|trains)$', re.I)
OPNAME = defaultdict(Counter)    # operator / network wikidata id -> names seen (one display name per company)
NAME2Q = defaultdict(Counter)    # and the other way round, for relations that give only the name
for r in RELS.values():
    t = r['tags']
    for k in ('operator', 'network', 'brand'):
        q = first(t.get(k + ':wikidata'))
        if re.fullmatch(r'Q\d+', q) and t.get(k):
            for v in {first(t.get(k)), first(t.get(k + ':en'))} - {''}:
                OPNAME[q][v] += 1; NAME2Q[v][q] += 1
def op_display(v, q):
    q = q or (NAME2Q[v].most_common(1)[0][0] if v in NAME2Q else '')
    if q in OPNAME:
        names = OPNAME[q].most_common()
        return next((n for n, c in names if latin(n) and not NOT_OP.search(n)), names[0][0]), q
    return v, q
def operator_of(ts, urban):
    """Display name and logo key: for trains the operator (operator:en, else operator; else brand, else network),
    for urban lines the network brand, else the operator; fare associations and service groupings skipped; the logo
    from the same tag set's wikidata id; one name per company (its most common Latin spelling)."""
    keys = ('network', 'brand', 'operator') if urban else ('operator', 'brand', 'network')
    for key in keys:
        cnt = Counter()
        for t in ts:
            v = first(t.get(key + ':en')) if latin(first(t.get(key + ':en'))) else first(t.get(key))
            if v and not NOT_OP.search(v) and not (LINE_REL.search(v) and key != 'operator') and \
                    v.lower() not in {first(t.get('name')).lower(), clean_name(t.get('name')).lower()}:      # "Great Western Railway" operates
                cnt[(v, first(t.get(key + ':wikidata')) if re.fullmatch(r'Q\d+', first(t.get(key + ':wikidata'))) else '')] += 1
        if cnt:
            (v, q), _ = max(cnt.items(), key=lambda kv: (kv[1], bool(kv[0][1])))
            if not q: q = next((q for (v2, q) in cnt if v2 == v and q), '')
            v, q = op_display(v, q)
            return v, ('wd:' + q if q else '')
    return '', ''
def families(ts):
    """Normalised network / operator / brand values and their wikidata ids (also the id a value carries elsewhere) of the
    tag sets, fare associations and zones left out: two lines are of one operator when these intersect."""
    out = set()
    for t in ts:
        for k in ('network', 'operator', 'brand', 'network:en', 'operator:en', 'operator:short', 'network:short'):
            base = k.split(':')[0]
            for v in (t.get(k) or '').split(';'):
                v = v.strip()
                if len(v) < 2 or NOT_OP.search(v): continue
                out.add(re.sub(r'\W+', '', plain(v.lower())))
                if v in NAME2Q: out.add(NAME2Q[v].most_common(1)[0][0])
            if k == base and not NOT_OP.search(t.get(base) or ''):
                out.update(q.strip() for q in (t.get(base + ':wikidata') or '').split(';') if re.fullmatch(r'Q\d+', q.strip()))
    return out

# ---------------------------------------------------------------- stations and stops
DEAD_NAME = re.compile(r'(?i)폐지|폐역|廃止|廃駅|建設中|工事中|under construction|unfinished|\(closed\)|\bdisused\b|abandoned|\bproposed\b|'
                       r'planned|projected|en construcción|en construction|proyectado|\(u/?c\)|строящ|проект')
YARD = re.compile(r'(?i)triaj|\bdepou?\b|d[ée]p[ôo]t\b|^مخزن|депо|разпредел|razpr|\bРП\b|\bPost \d|teretna|товарн|сортировоч|Tovarn|Sortirov|Werkstatt|'
                  r'\byard\b|goods|marchandise|triage|rangierbahnhof|güterbahnhof|gbf\b|freight|marshalling|\bloop\b|siding|'
                  r'Betriebshof|Autoverladung|Autozug|motorail|car terminal|khadan|colliery|貨物|货运|화물|信号場|信号所|定点|'
                  r'Signal(?:ling)? Station|Signal box|Teiten|Blockstelle|Abzweigstelle|Überleitstelle|Betriebsbahnhof')
def dead(t):
    return t.get('railway') in ('proposed', 'construction', 'abandoned', 'disused', 'razed') or bool(DEAD_NAME.search(t.get('name') or ''))
def stationish(t):
    return (t.get('railway') in ('station', 'halt', 'tram_stop') or t.get('public_transport') == 'station') and not dead(t)
def stoplike(t):
    return t.get('railway') in ('station', 'halt', 'stop', 'tram_stop', 'platform') or t.get('public_transport') in ('station', 'stop_position', 'platform')
def mode_ok(t, mode):
    st = t.get('station')
    if mode == 'train': return t.get('railway') in ('station', 'halt') and st in (None, 'train') and not is_urban_station(t) or t.get('train') == 'yes'
    if mode == 'tram': return t.get('railway') == 'tram_stop' or t.get('tram') == 'yes' or st == 'tram'
    if mode in ('funicular', 'light_rail', 'monorail', 'subway') and (st in ('funicular', 'light_rail', 'monorail', 'subway') or t.get(mode) == 'yes'):
        return st == mode or t.get(mode) == 'yes' or mode in ('funicular', 'monorail') or st in ('funicular', 'monorail')
    return st == mode or t.get(mode) == 'yes'
def is_urban_station(t):
    return t.get('station') in ('subway', 'light_rail', 'monorail', 'tram', 'funicular') or t.get('railway') == 'tram_stop' or \
        (any(t.get(k) == 'yes' for k in ('subway', 'light_rail', 'monorail', 'tram', 'funicular')) and t.get('train') != 'yes')
def active(t):
    """A passenger station still in use (for stations taken from the track, where nothing else says trains stop there)."""
    return not (t.get('disused') or t.get('abandoned') or t.get('historic') or t.get('railway:historic') or dead(t) or
                t.get('station') in ('freight', 'yard') or t.get('usage') in ('freight', 'industrial') or YARD.search(t.get('name') or '') or
                t.get('railway:traffic_mode') == 'freight' or t.get('passenger') == 'no' or t.get('train') == 'no')
SGRID = Grid(((n, lon, lat) for n, (lon, lat, t) in NODES.items() if stationish(t) and t.get('name')), 0.003)
STOPGRID = Grid(((n, lon, lat) for n, (lon, lat, t) in NODES.items() if stoplike(t) and not stationish(t) and t.get('name') and
                 t.get('railway') in ('stop', 'halt') and not dead(t) and not PLATFORM_ONLY.fullmatch(stn_name(t))), 0.003)   # named stop
                                                                                            # positions (Tren Maya has no stations), not "Voie 1"
_sn, _mk = {}, {}
def sname(n):
    if n not in _sn: _sn[n] = stn_name(NODES[n][2])
    return _sn[n]
def smkey(n):
    if n not in _mk: _mk[n] = mkey(sname(n), *NODES[n][:2]) if sname(n) else ''
    return _mk[n]
_snap = {}
def snap(nid, mode):
    """Stop member (stop_position / platform / station node) -> its station node: a station whose name matches (same,
    then same without city and station words, then one containing the other) within 400 m, same mode first; else a
    station of this mode within 100 m; else the stop itself if it is a named stop; an unnamed stop -> the nearest
    station of the mode within 300 m."""
    key = (nid, mode)
    if key in _snap: return _snap[key]
    out = None
    if nid in NODES and not dead(NODES[nid][2]):
        lon, lat, t = NODES[nid]; name = sname(nid) if not PLATFORM_ONLY.fullmatch(sname(nid)) else ''
        if stationish(t) and name and mode_ok(t, mode): out = nid
        elif name:
            near = SGRID.near(lon, lat, 500); k, mk = skey(name), smkey(nid)
            for test in (lambda n: skey(sname(n)) == k, lambda n: smkey(n) == mk,     # a longer name only of this mode ("Central Grand Concourse")
                         lambda n: related(smkey(n), mk) and mode_ok(NODES[n][2], mode)):
                same = [(not mode_ok(NODES[n][2], mode), d, n) for d, n in near if test(n)]
                if same: out = min(same)[2]; break
            if out is not None and YARD.search(NODES[out][2].get('name') or ''): out = None
            elif out is None:
                close = [n for d, n in near if d <= 100 and mode_ok(NODES[n][2], mode)]
                out = nid if stoplike(t) else (close[0] if close else None)
                if out == nid and close and not t.get('name'): out = close[0]
        else:
            near = [(d, n) for d, n in SGRID.near(lon, lat, 300) if n != nid and not PLATFORM_ONLY.fullmatch(sname(n))]
            close = [n for d, n in near if mode_ok(NODES[n][2], mode)] or [n for d, n in near if d <= 150]
            out = close[0] if close else None
    _snap[key] = out
    return out

# ---------------------------------------------------------------- which relations are passenger services
FREIGHT = re.compile(r'freight|güter|fret\b|goods|merci\b|mercanc|cargo|carga\b|industri|\bmines?\b|mining|colliery|coal|kohle|phosphate|'
                     r'\bore\b|iron ore|bauxite|manganese|export line|marchandise|\bport\b|harbou?r|hafen|quarry|steelworks|dienstbahn|'
                     r'werk?sbahn|industriebahn|anschlussbahn|raccordo|binario industriale|貨物|货运|専用線|탄광|광산|화물|товарн|грузов|'
                     r'Hudut|тарифна дільниця|дільниця \d|tronson|contruction|construction|u/c\b|project|Projesi|\(proje|Subdivision|'
                     r'intermodal train|\btupik|тупик', re.I)
FREIGHT_SVC = re.compile(r'freight|güter|\bfret\b|\bgoods\b|cargo|intermodal train|container train|貨物|货运|товарн|грузов', re.I)
FREIGHT_OP = re.compile(r'Ferromex|Ferrosur|KCSM|Kansas City Southern|CPKC|Canadian Pacific|Canadian National|\bCN\b|Union Pacific|\bUP\b|'
                        r'BNSF|CSX|Norfolk Southern|Genesee|Trenes Argentinos Cargas|Belgrano Cargas|Nuevo Central Argentino|\bNCA\b|'
                        r'Ferroexpreso|FEPASA|FCAB|Antofagasta|Rumo\b|\bVLI\b|MRS Log|Transnet Freight|Aurizon|Pacific National|'
                        r'Arc Infrastructure|Fortescue|Rio Tinto|BHP|COMILOG|\bCBG\b|Vale\b|Transnordestina|Freight', re.I)
LIFECYCLE = ('construction', 'proposed', 'disused', 'abandoned', 'razed', 'demolished', 'removed', 'planned')
def lifecycle(t):
    """Not open: a lifecycle tag or key prefix, a lifecycle word in the name, or an opening date still to come."""
    if t.get('state') in LIFECYCLE or t.get('railway') in LIFECYCLE or any(k.split(':')[0] in LIFECYCLE for k in t) or \
            t.get('disused') == 'yes' or t.get('abandoned') == 'yes' or t.get('historic') or t.get('railway:historic'): return True
    if DEAD_NAME.search(' '.join(t.get(k, '') for k in ('name', 'name:en'))): return True
    for k in ('opening_date', 'start_date'):
        v = t.get(k) or ''
        m = re.fullmatch(r'(\d{1,2})[/.](\d{1,2})[/.](\d{4})', v)
        iso = f'{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}' if m else v[:10]
        if re.match(r'\d{4}', iso) and iso > DATE: return True
    m = re.search(r'\((?:18|19|20)\d\d\s*[-–]\s*((?:18|19|20)\d\d)\)', t.get('name') or '')      # "Trem Azul (1975-1997)"
    # closed: an end date before this year (Sangritana, 1973; this year's can be a diversion's, on Karlsruhe's tram 1)
    return bool(m and m.group(1) < DATE[:4] or re.match(r'\d{4}', t.get('end_date') or '') and t['end_date'][:4] < DATE[:4])
HERITAGE = re.compile(r'museum|musée|museo|muzeum|muzeal|muzeální|museu|музей|museibana|museispårväg|museumslinjen|heritage|vintage|veteran(?!s\b)|'
                      r'histori\w* (?:railway|tram\w*|trolley|train|line|bahn)|histórico|historique|historische|preservation|preserved|nostalg|носталг|'
                      r'parkeisenbahn|park ?railway|kolejka parkowa|parkowa|kleinbahn im|freizeitpark|walt disney world|disneyland railroad|disney parks|'
                      r'europa-park|legoland|linnanmäki|rasti-land|'
                      r'phantasialand|heide park|efteling|hansa-park|pairi daiza|tiergartenbahn|zoobahn|zoo railway|liliput|feldbahn|'
                      r'grubenbahn|mine railway|schaubergwerk|theme ?park|six flags|busch gardens|cedar point|seaworld|universal studios|'
                      r"dollywood|knott'?s|kings island|amusement|eisenbahnfreunde|förderverein|\be\.\s?v\.|stichting|preservation society|"
                      r'railway society|miłośników|amigos del|ferroclub|stoomtrein|stoomtram|museumstoom|dampfbahn|dampfzug|steam train|steam railway|'
                      r'à vapeur|tren de vapor|tourist train|tourist tram|rail tour|touristique|tren turístico|turystyczn|туристическ|туристичн|'
                      r'trenino verde|treno natura|trenoblu|scenic rail\w*|scenic valley|wine train|dinner train|draisine|детск\w* ж|\w?ДЖД\b|'
                      r'малая \w+ железная|ММЖД|дзіцяч|дитяч\w* залізн|gyermekvasút|dziecięc|dětská|kindereisenbahn|pioniereisenbahn|'
                      r"pioneer railway|children'?s railway|пещер|cave railway|aeromovel|agrowisata|taman mini|遊園|動物園|五分車|skanzen|"
                      r'vasútmúzeum|forest railway|erdei vasút|ferrocarril minero|jungle jim|mandalay bay|aria express|'
                      r"fort edmonton|motat|capitol subway|architect of the capitol|alton towers|thorpe park|chessington world|gardaland|"
                      r"portaventura (?:world|park)|tivoli (?:gardens|friheden)|parc d.attractions?|attraktionspark|vergnügungspark|amusement park|luna ?park|"
                      r'dirksen|senate subway|disney resort line|ディズニーリゾートライン|(?<!アーバン)パークライン|トロッコ|財団|\ba\.?s\.?b\.?l\b|\bvzw\b|minièr', re.I)
AERIAL = re.compile(r'chairlift|chair lift|sesselbahn|sessellift|(?<!stand)seilbahn|gondola|gondel|téléphérique|teleférico|'
                    r'telecabina|ropeway|aerial tram|リフト|ロープウェイ|ゴンドラ|索道|케이블카|канатн', re.I)
AIRSIDE = re.compile(r'airside|skymetro|airport skytrain|concourse|plane train|gate ?link|track transit system|aerotrain|stansted airport transit|'
                     r'terminal \d+ apm|\bt\d apm|apm t\d|satellite|skyway|\bAGTS\b|underground (?:yellow|blue|green)|sea underground', re.I)   # known ones
TOURIST_OK = re.compile(r'glacier express|bernina|cremallera|zahnradbahn|zugspitz|wendelstein|schafberg|schneeberg|brocken|pilatus|rigi|'
                        r'gornergrat|jungfrau|achensee|wengernalp|montenvers|núria|nuria|montserrat|sóller|soller|inselbahn|flåm|flam|'
                        r'ghan|indian pacific|overland|spirit of|tren a las nubes|expreso del sur|white pass|'
                        r'funicul|funicolare|standseilbahn|incline|harzer|brockenbahn|molli|fichtelberg|lößnitz|weißeritz|rhb', re.I)
PUBLIC_OK = re.compile(r'Walt Disney World Monorail|Epcot Monorail|Resort Monorail|Express Monorail|disney resort line|'
                       r'ディズニーリゾートライン|舞浜リゾートライン', re.I)
def own_name(n):
    """A route's name without its route description, which names stops ("Метро Салтівська лінія: Історичний музей => …")."""
    parts = re.split(r'\s*[:：]\s*', n or '', 1)
    return (parts[0] if len(parts) > 1 or not re.search(ARROW, parts[0]) else '') + ' ' + clean_name(n)
def junk(t):
    """Not a public passenger line: freight, museum / heritage / park / children's railways, theme parks, airside people
    movers, aerial lifts, depot and event runs, through coaches. Freight, heritage and tourist words are matched in the
    route's own name, operator, network and brand (and its description when it is of no network: Karlsruhe's tram E
    notes "Einrücker abends"), not in the names of its stops; depot and airside words in the whole name."""
    txt = ' '.join([own_name(t.get('name')), own_name(t.get('name:en'))] + [t.get(k, '') for k in ('operator', 'network', 'brand', 'official_name')] +
                   ([t.get('description', '')] if not t.get('network') else []) + ([t['ref']] if len(t.get('ref', '')) > 12 else []))   # ref
                   # as a name: "Детская Восточно-Сибирская железная дорога"
    full = txt + ' ' + t.get('name', '') + ' ' + t.get('name:en', '')
    svc = {s.strip() for s in (t.get('service') or '').split(';')}
    if t.get('usage') == 'tourism' or t.get('tourism') == 'attraction': svc.add('tourism')
    if t.get('service') == 'car_shuttle' or t.get('passenger') == 'no' or t.get('railway:traffic_mode') == 'freight' or \
            t.get('usage') in ('freight', 'industrial', 'military') or FREIGHT_SVC.search(txt): return 'freight'
    if re.match(r'(?i)maintenance line', t.get('name', '')): return 'event / depot run'
    if PUBLIC_OK.search(full): return ''        # public transit run by a resort: free or fare-paying, no park admission
    if t.get('attraction') or t.get('leisure') == 'amusement_park' or 'admission' in (t.get('fee') or '').lower() or \
            t.get('access') in ('private', 'customers', 'no', 'permit') and not t.get('network'):
        return 'private'           # a ride, or a private line of no public network (Charleroi's métro is access=no, of TEC)
    if AERIAL.search(txt) and not re.search(r'funicul|standseil|incline', txt, re.I): return 'aerial lift'
    if AIRSIDE.search(full) or t.get('airside') == 'yes': return 'airside'
    if re.search(r'Betriebshof|Einsetz|Einrück|Aussetz|turnaround|uniquement.*match|Pre-Game|Post-Game|\bevents?\b|Sonderverkehr|'
                 r'Stammstrecke|Беспересадочн|Бесперасадачн|through coach|Kurswagen|depot run|excursion', full, re.I) or 'events' in svc: return 'event / depot run'
    if TOURIST_OK.search(txt): return ''
    if HERITAGE.search(txt) and not SUBURBAN.search(' '.join(t.get(k, '') for k in ('network', 'operator'))): return 'heritage'   # Metra Heritage Corridor
    if svc & {'tourism', 'touristic', 'tourist', 'heritage', 'museum', 'excursion'} and t.get('route') != 'funicular' and not (
            t.get('network') and first(t.get('network')) != first(t.get('operator')) or good_ref(t.get('ref'))): return 'tourist only'
    return ''
INFRA_NAME = re.compile(r'^KBS\b|^Kursbuchstrecke|^Ligne d[e\']|Bahnstrecke|^Linea .+ ?[-–] ?|^Línea .+ ?[-–] ?|železniční trať|trať \d|'
                        r'^Railway line|^Železniška proga|^Pruga|^Linia kolejowa|^Secția|^Magistrala|^Main line$|railway line$|'
                        r'Железопътна линия|Залізнична лінія|железнодорожная линия', re.I)
def is_infra(r):
    """A route=train relation that maps a railway line rather than a service (timetable route, line number)."""
    t = r['tags']
    return bool(INFRA_NAME.search(t.get('name', ''))) or not any(t.get(k) for k in ('network', 'ref', 'service', 'operator', 'from', 'to', 'brand')) \
        and not any(typ == 'n' for typ, _, _ in r['members'])
def route_ways(r): return [ref for typ, ref, role in r['members'] if typ == 'w' and ref in WAYS and not re.search(r'platform|stop', role)]
def open_share(r):
    """Share of the relation's track members that are open rail (construction / disused ways are not extracted)."""
    ws = [(ref in WAYS) for typ, ref, role in r['members'] if typ == 'w' and not re.search(r'platform|stop', role)]
    return (sum(ws) / len(ws)) if len(ws) >= 3 else 1.0
STOP_ROLE = re.compile(r'(?:^|[:_])(?:stop|platform)|^station$')
GUESSED = set()      # (route, station) snapped from an unnamed stop member: checked against the route's track
def route_stops(rid, r, mode):
    out = []
    for typ, ref, role in r['members']:
        if typ != 'n' or ref not in NODES: continue
        t = NODES[ref][2]
        if STOP_ROLE.search(role) or role == '' and (stationish(t) or t.get('public_transport') == 'stop_position' or
                                                      t.get('railway') in ('stop', 'station', 'halt', 'tram_stop')):
            s = snap(ref, mode)
            if s is not None and (not t.get('name') or PLATFORM_ONLY.fullmatch(sname(ref))) and s != ref: GUESSED.add((rid, s))
            if s is not None and (not out or out[-1] != s): out.append(s)
    return out
def passenger(t): return t.get('type') == 'route' and t.get('route') in MODES and '直通' not in t.get('name', '')

SUBURBAN = re.compile(r'S-Bahn|\bRER\b|Transilien|Cercan[ií]as|Rodalies|Suburban|Suburbano|suburbain|Proastiakos|Προαστιακός|'
                      r'Commuter|Regional Rail|Pendelt[åa]g|Pendeltog|S-tog|Overground|Elizabeth line|Merseyrail|\bMetra\b|Metro-North|'
                      r'Long Island Rail Road|\bLIRR\b|NJ ?Transit|\bMARC\b|Caltrain|Metrolink|Sounder|Tri-Rail|SunRail|GO Transit|'
                      r'\bexo\b|Trem Metropolitano|Metrotr[eé]n|Tren Urbano|CPTM|SuperVia|Commuterline|\bKRL\b|Metrorail|Sydney Trains|'
                      r'Metro Trains|Transperth|Citytrain|Adelaide Metro|Léman Express|Servizio ferroviario (?:suburbano|metropolitano)|'
                      r'Szybka Kolej Miejska|\bSKM\b|МЦД|Центральн\w+ диаметр|МЦК|Пригородн|электричк|Городск\w* (?:электро|дизель)поезд|'
                      r'Приміськ|Прыгарадн|Электрацягнік|РЭКС|\w*ППК\b|пригород|SEPTA|\bVRE\b|Virginia Railway Express|Trinity Railway Express|'
                      r'\bTRE\b|COASTER|FrontRunner|\bRTD\b|\bSMART\b|CTrail|Shore Line East|West Coast Express|UP Express|Rail Runner|'
                      r'TEXRail|A-train|WeGo Star|MBTA|Keolis Commuter|RRTS|RapidX|Namo Bharat|Komuter|KTM Komuter|\bFGC\b|'
                      r'Ferrocarrils de la Generalitat|Koleje Mazowieckie|Elron|Ferrovie Laziali|\bSKA\d|\bPKM\b|IZBAN|Marmaray|Banliyö|'
                      r'Gaziray|Başkentray|Circular Railway|SRT (?:Dark |Light )?Red|Métro Léger|Kolkata Suburban|Mumbai Suburban|'
                      r'Chennai Suburban|MMTS|Lähijuna|HSL\b|Esko|Lokaltog|Nærtrafik|Réseau Express|Aeroexpress|Аэроэкспресс|'
                      r'Airport Express|Arlanda Express|Flytoget|Heathrow Express|Gatwick Express|Light Rail Transit|\bLRT\b', re.I)
HSR = re.compile(r'InOui|Ouigo(?!\s*(?:Train )?Classique)|\bAvlo\b|\bIryo\b|Frecciarossa|Frecciargento|\bItalo\b|\.italo|Eurostar|Thalys|\bLyria\b|'
                 r'Shinkansen|新幹線|THSR|高鐵|高速鐵路|\bAcela\b|Yüksek Hızlı|Sapsan|Сапсан|\bAllegro\b|Haramain|Al Boraq|Al-Boraq|البراق|'
                 r'Fuxing|复兴号|復興號|和谐号|Hexie|Afrosiyob|Whoosh|고속철도', re.I)
HSR_CS = re.compile(r'(?<![a-zà-ÿ] )\bTGV\b|\bICE\b|\bAVE\b|\bKTX|\bYHT\b|\bAvant\b')

# ---------------------------------------------------------------- routes
EXPRESS = re.compile(r'express|expreso|exprés|expresso|ekspres|экспресс|експрес|特急|急行|快速|快特|ライナー|limited|tokkyu|\brapid\b|'
                     r'semi-?fast|TGV|InOui|Ouigo|Intercit|InterCit|Interregio|Sapsan|Сапсан|Ласточка|Frecci|\bItalo\b|\bAvlo\b|Alvia|Euromed|'
                     r'Talgo|\bIryo\b|railjet|EuroCity|EuroNight|Nightjet|Eurostar|Thalys|Lyria|Superfast|Rajdhani|Shatabdi|Duronto|'
                     r'Vande Bharat|Garib Rath|Humsafar|Tejas|скор\w+ поезд|фирменн|пассажирск\w+ поезд|Rapide|Schnellzug|Shinkansen|'
                     r'新幹線|無窮花|무궁화|새마을|누리로|自強|莒光|普悠瑪|太魯閣|高鐵|THSR|Acela|Allegro|Pendolino|Snabbtåg|Flytoget|'
                     r'成田エクスプレス|Aeroexpress|Аэроэкспресс|night train|nattåg|nachtzug|Intercités|YHT|Yüksek Hızlı|Hızlı Tren|Ekspresi|Mavi Tren', re.I)
EXPRESS_CS = re.compile(r"\b(?:KTX|ITX|SRT|ICE|IC|ICN|EC|EN|NJ|RE|IRE|IR|RJX?|AVE|Avant|YHT|Mail|SJ|Rx)\b ?\d*")
LINE_REL = re.compile(r'(?:선|線|本線|ライン|\bLine|\bline|Linie|Linia|Linea|Línea|Ligne|Railway|Railroad|лінія|линия|Hattı|Đường sắt [\w ]+|HSL)$')
def expressish(t):
    """A service that stops only at some stations: its missing stops are not taken from the track."""
    n, raw = clean_name(t.get('name')) or '', t.get('name', '')
    if LINE_REL.search(n) and not EXPRESS.search(raw) and not EXPRESS_CS.search(raw): return False          # a line relation ("경부선", "Haramain High Speed Line")
    if re.fullmatch(r'(?:JR)?[\u3040-\u30ffー・]{2,10}(?:列車|号)?', n): return True                     # a named train ("しおさい", "つばさ")
    svc = {s.strip() for s in (t.get('service') or '').split(';')}
    if svc & {'long_distance', 'high_speed', 'night', 'international', 'express', 'national', 'intercity', 'highspeed'}: return True
    txt = ' '.join(t.get(k, '') for k in ('name', 'name:en', 'ref', 'brand', 'network'))
    if svc & {'regional', 'commuter', 'suburban', 'local', 'urban'} and not EXPRESS_CS.search(t.get('ref') or '') and \
            not EXPRESS.search(t.get('name', '')): return False        # but "Электропоезд-экспресс «Ласточка»" skips stations
    return bool(EXPRESS.search(txt) or EXPRESS_CS.search(txt))
ROUTES = {}
nrej = Counter()
DENY = {13035324, 5928466, 7826308, 7826309, 8530208, 15342820}   # dead systems still tagged as running (reviewers checked)
BAD_MASTER = set()          # routes of closed / listed route masters, and of junk ones when they name no operator of their own
for rid, m in RELS.items():   # (an airside mover's: unless they say they run landside, as Changi's Skytrain between terminals)
    if m['tags'].get('type') != 'route_master': continue
    # (a master's end_date can be its timetable's: Toulouse metro)
    bad, why = lifecycle({k: v for k, v in m['tags'].items() if k != 'end_date'}) or rid in DENY or rid in DROP, junk(m['tags'])
    if bad or why:
        BAD_MASTER.update(x for typ, x, role in m['members'] if typ == 'r' and x in RELS and (bad or not any(RELS[x]['tags'].get(k) for k in (
            'operator', 'network')) or why == 'airside' and not re.search(r'(?i)landside', RELS[x]['tags'].get('name', ''))))
# running services that OSM maps only as an outdated relation: tags that make them the current line (checked by hand)
FORCE = {2752701: {'name': 'ירושלים – תל אביב', 'name:he': 'ירושלים – תל אביב', 'name:en': 'Jerusalem – Tel Aviv',
                   'service': 'high_speed', 'network': 'Israel Railways', 'operator': 'Israel Railways'}}   # the A1, Navon – airport
FORCE_STOPS = {2752701: [7144868421, 3982712778, 3978658308, 2930618402, 2930618401]}   # Navon (deep underground, off the
# track's snapping range), Ben Gurion Airport, Tel Aviv HaHagana, Savidor Center, University: the A1's Jerusalem – Tel Aviv run
for rid, tags in FORCE.items():
    if rid in RELS:
        RELS[rid]['tags'] = {k: v for k, v in RELS[rid]['tags'].items() if k != 'fixme'} | tags
        RELS[rid]['members'] = [m for m in RELS[rid]['members'] if m[0] != 'n'] + [('n', n, 'stop') for n in FORCE_STOPS.get(rid, [])]
INFRA_T = []          # route=train relations that map a railway line (handled with the fallback)
for rid, r in RELS.items():
    t = r['tags']
    if t.get('type') == 'route_master' and t.get('route_master') in MODES and not any(typ == 'r' for typ, _, _ in r['members']) and \
            any(typ == 'n' for typ, _, _ in r['members']):
        t = r['tags'] = dict(t, type='route', route=t['route_master'])     # a route_master with its own members (Tren Maya)
    if not passenger(t): continue
    why = 'lifecycle' if lifecycle(t) else junk(t) or ('listed as dead' if rid in DENY else '') or ('audit' if rid in DROP else '') or \
        ('master' if rid in BAD_MASTER else '')
    if rid in FORCE_FB and rid not in DROP: why = ''         # an audit found it running
    if not why and t['route'] == 'train' and is_infra(r): INFRA_T.append(rid); why = 'infrastructure'
    if why:
        nrej[why] += 1
        if why != 'infrastructure': drop_log(why, rid, t.get('name'), t.get('operator'), DROP.get(rid, ''))
        continue
    ROUTES[rid] = r
log('routes', len(ROUTES), 'rejected', dict(nrej))
LISTED = {rid: route_stops(rid, r, r['tags']['route']) for rid, r in ROUTES.items()}
OPEN = near_track(sorted({s for v in LISTED.values() for s in v}))
for rid, st in LISTED.items():      # drop stops away from any open track (extensions still being built)
    st = [s for s in st if s in OPEN]
    LISTED[rid] = [s for i, s in enumerate(st) if i == 0 or s != st[i - 1]]
for rid in [rid for rid, r in ROUTES.items() if open_share(r) < 0.3]:
    # most of its track is not open rail (under construction, disused): a line still being built, unless its stops lie on
    # the open part (a relation cut at the edge of a regional extract)
    ws, st = route_ways(ROUTES[rid]), LISTED[rid]
    if not ws or not st or sum(dist_to_ways(ws, *NODES[s][:2]) < 300 for s in st) < 0.5 * len(st):
        nrej['track not open'] += 1; drop_log('track not open', rid, ROUTES[rid]['tags'].get('name')); del ROUTES[rid], LISTED[rid]
log('snapped stops', len(_snap))

# nearest main-line way of every passenger station (a station is on a line's track when its nearest main way is the line's)
PAXN = [n for n, (lon, lat, t) in NODES.items() if t.get('name') and (t.get('railway') in ('station', 'halt') or
        t.get('public_transport') == 'station' or t.get('railway') == 'stop' and t.get('train') == 'yes') and not is_urban_station(t)]
MAIN_NEAR = defaultdict(set)
if PAXN and MAIN_W:
    X = np.array([NODES[n][:2] for n in PAXN]); sc = np.unique((cells(X[:, 0], X[:, 1])[:, None] + OFFS[None, :]).ravel())
    for i in range(0, len(MAIN_W), 200000):
        P, wi = samples(MAIN_W[i:i + 200000]); cl = cells(P[:, 0], P[:, 1]); m = np.isin(cl, sc)
        for kw in np.unique(cl[m] * 262144 + wi[m]).tolist(): MAIN_NEAR[kw // 262144].add(MAIN_W[i + kw % 262144])
_nm = {}
def nearest_main(n):
    if n not in _nm:
        lon, lat = NODES[n][:2]; k = int(cells(np.array([lon]), np.array([lat]))[0])
        ws = {w for o in OFFS.tolist() for w in MAIN_NEAR.get(k + o, ())}
        _nm[n] = min(((dist_to_ways([w], lon, lat), w) for w in ws), default=(1e18, None))
    return _nm[n]
def fast_way(t):      # high-speed track: 250 km/h, or highspeed=yes without a lower speed
    return t.get('highspeed') == 'yes' and speed(t) == 0 or speed(t) >= 250
def speed(t):
    m = re.match(r'\d+', t.get('maxspeed') or '')
    return 0 if not m else int(m.group()) * (1.609 if 'mph' in t.get('maxspeed', '') else 1)
def along(ways, mode, tr, evidence=None, strict=False, lim_urban=60):
    """Stations of this mode on the ways, in track order: within 300 m (station nodes of trains, often on the building),
    80 m (halts, stops) or 60 m (urban; 150 m funiculars); a train station only when no other main line is nearer.
    -> [(node, distance along the track)]."""
    P, _ = samples(ways, 0.0005)
    C = np.unique(np.floor(P / SGRID.s).astype(np.int64), axis=0)
    C = np.unique((C[:, None, :] + np.array([(i, j) for i in (-1, 0, 1) for j in (-1, 0, 1)])[None]).reshape(-1, 2), axis=0)
    ks = list(map(tuple, C.tolist()))
    grids = (SGRID, STOPGRID) if mode == 'train' else (SGRID,)
    cand = sorted({n for g in grids for k in ks for n, x, y in g.c.get(k, ()) if (mode_ok(NODES[n][2], mode) or mode == 'light_rail' and
                   NODES[n][2].get('railway') == 'tram_stop') and active(NODES[n][2]) and (evidence is None or evidence(n))})     # Utsunomiya LRT
    if not cand: return []
    _, d = NearIdx(P, SGRID.s).near([NODES[n][:2] for n in cand])
    wset, out = set(ways), []
    for n, dn in zip(cand, d.tolist()):
        t = NODES[n][2]
        lim = (300 if t.get('railway') == 'station' or t.get('public_transport') == 'station' else 80) if mode == 'train' else 150 if mode == 'funicular' else lim_urban
        if dn > lim: continue
        if mode == 'train':
            dm, w = nearest_main(n)
            if w is not None and w not in wset and dn > dm + (5 if strict else 30): continue
            if w is not None and WAYS[w][0].get('tunnel') and dm > 50: continue        # above a tunnel, not on the line
        if t.get('railway') == 'stop' and SGRID.near(*NODES[n][:2], 300): continue       # a stop position of a mapped station
        out.append(n)
    if not out: return []
    tt, _ = tr.locate([NODES[n][:2] for n in out])
    return sorted(zip(out, tt.tolist()), key=lambda a: a[1])
def by_track(nodes, tr):
    """Stops in track order, in the direction the relation lists them; None when the track does not explain them."""
    tt, dd = tr.locate([NODES[n][:2] for n in nodes])
    if dd.max() > 1500: return None
    o = np.argsort(tt, kind='stable')
    if (np.diff(o) > 0).all(): return nodes
    rank = np.empty(len(o), int); rank[o] = np.arange(len(o))
    if np.corrcoef(np.arange(len(o)), rank)[0, 1] < 0: o = o[::-1]
    return [nodes[i] for i in o]
def pieces(ways):
    """Connected groups of ways (sharing a node), longest first."""
    parent, first_at = list(range(len(ways))), {}
    def find(i):
        while parent[i] != i: parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for i, w in enumerate(ways):
        for n in WAYS[w][2].tolist():
            j = first_at.setdefault(n, i)
            if j != i: parent[find(i)] = find(j)
    g = defaultdict(list)
    for i, w in enumerate(ways): g[find(i)].append(w)
    return sorted(g.values(), key=lambda ws: -sum(map(way_len, ws)))
def branch_split(stops, ways, tr):
    """Stops of a route whose track forks (a train that divides, one relation for a line's services): the ones on its
    main path in track order, the others in branches along the stretches of track they lie on, each from the main stop
    nearest its near end -> (main, [branch]) of nodes; None when the ways do not explain 80% of the stops."""
    _, dd = tr.locate([NODES[n][:2] for n in stops])
    on, off = [n for n, d in zip(stops, dd.tolist()) if d <= 1500], [n for n, d in zip(stops, dd.tolist()) if d > 1500]
    main, out = by_track(on, tr) if len(on) >= 2 else None, []
    if main is None: return None
    far = [w for w, d in zip(ways, tr.locate([WAYS[w][1][len(WAYS[w][1]) // 2] for w in ways])[1].tolist()) if d > 1000]
    for ws in pieces(far)[:8]:
        tb = track(ws) if len(off) >= 2 else None
        if tb is None: continue
        pb, db = tb.locate([NODES[n][:2] for n in off])
        o = [off[i] for i in np.argsort(pb, kind='stable') if db[i] <= 1500]
        if len(o) < 2: continue
        off = [n for n, d in zip(off, db.tolist()) if d > 1500]
        a, b = NODES[o[0]][:2], NODES[o[-1]][:2]
        ja = min(main, key=lambda n: metres(*NODES[n][:2], *a)); jb = min(main, key=lambda n: metres(*NODES[n][:2], *b))
        out.append([ja] + o if metres(*NODES[ja][:2], *a) <= metres(*NODES[jb][:2], *b) else [jb] + o[::-1])
    return (main, out) if len(off) <= 0.2 * len(stops) else None
def trim(ways, tr, t0, t1):
    """Ways of the route between its first and last stop (a route drawn past its end stops is cut there)."""
    if t0 > 1000 or tr.L - t1 > 1000:
        mid, _ = tr.locate([WAYS[w][1][len(WAYS[w][1]) // 2] for w in ways])
        return [w for w, m in zip(ways, mid.tolist()) if t0 - 300 <= m <= t1 + 300]
    return ways

# --cache DIR: the loop's result for a route is kept (DIR/routes.pickle) under a digest of what the loop reads for it (its
# tags, members, listed and guessed stops), valid while the raw pickle and the code the loop reaches are the same: the
# top-level statements that bind the names it uses, transitively (not the route filters, which only choose ROUTES). A
# change to the filters or to later stages reruns only the routes it lets in.
CACHE = sys.argv[sys.argv.index('--cache') + 1] if '--cache' in sys.argv else None
def loop_version(after):
    import ast, hashlib, builtins
    src = open(__file__).read(); body = ast.parse(src).body
    loop = next(st for st in body if st.lineno > after and isinstance(st, ast.For))
    MUT = {'update', 'add', 'append', 'extend', 'setdefault', 'pop', 'discard', 'remove', 'clear', 'insert', 'popitem'}
    def base(n):
        while isinstance(n, (ast.Subscript, ast.Attribute)): n = n.value
        return n.id if isinstance(n, ast.Name) else None
    def scan(st):     # the module-level names a statement reads and binds (or mutates); function / comprehension locals aside
        loads, binds, own = set(), set(), set()
        def visit(n, loc):
            if isinstance(n, (ast.FunctionDef, ast.Lambda)):
                g = {x for y in ast.walk(n) if isinstance(y, ast.Global) for x in y.names}
                inner = {a.arg for a in ast.walk(n.args) if isinstance(a, ast.arg)} | \
                    {x.id for x in ast.walk(n) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)} - g
                binds.update(g)
                for c in (n.body if isinstance(n.body, list) else [n.body]): visit(c, (loc or set()) | inner)
                return
            if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                inner = (loc or set()) | {x.id for g in n.generators for x in ast.walk(g.target) if isinstance(x, ast.Name)}
                for c in ast.iter_child_nodes(n): visit(c, inner)
                return
            if isinstance(n, ast.Name):
                if isinstance(n.ctx, ast.Load): loads.add(n.id) if loc is None or n.id not in loc else None
                elif loc is None: binds.add(n.id); own.add(n.id)
            elif isinstance(n, (ast.Subscript, ast.Attribute)) and isinstance(n.ctx, (ast.Store, ast.Del)) or \
                    isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in MUT:
                x = base(n.func.value if isinstance(n, ast.Call) else n)
                if x and (loc is None or x not in loc): binds.add(x)
            for c in ast.iter_child_nodes(n): visit(c, loc)
        if isinstance(st, (ast.FunctionDef, ast.ClassDef)): binds.add(st.name); visit(st, None) if isinstance(st, ast.FunctionDef) else [visit(c, set()) for c in st.body]
        elif isinstance(st, (ast.Import, ast.ImportFrom)): binds.update((a.asname or a.name).split('.')[0] for a in st.names)
        else:
            visit(st, None)
            if not isinstance(st, (ast.Assign, ast.AugAssign, ast.AnnAssign)): loads -= own     # a loop's own variables
        return loads, binds
    binds, uses = defaultdict(list), {}
    for st in body:
        uses[id(st)], b = scan(st)
        for x in b: binds[x].append(st)
    data = {'ROUTES', 'LISTED', 'GUESSED', 'R', 'PASS2', 'nfill', 'DBG', 'CACHE'}     # in the key, or the loop's output
    seen, todo, keep = set(), list(uses[id(loop)]), {id(loop): loop}
    while todo:
        x = todo.pop()
        if x in seen or x in data or hasattr(builtins, x) and x not in binds: continue
        seen.add(x)
        for st in binds.get(x, ()):
            if st.lineno < loop.lineno and id(st) not in keep: keep[id(st)] = st; todo += uses[id(st)]
    h = hashlib.blake2b(f'{os.path.getsize(sys.argv[1])}:{os.stat(sys.argv[1]).st_mtime_ns}'.encode(), digest_size=16)
    for st in sorted(keep.values(), key=lambda st: st.lineno): h.update(ast.get_source_segment(src, st).encode())
    for m in ('translit', 'english', 'countries'): h.update(open(os.path.join(os.path.dirname(__file__), m + '.py'), 'rb').read())
    if VERBOSE: print('route cache: the loop reaches the code at lines', sorted(st.lineno for st in keep.values()))
    return h.hexdigest()
def rkey(rid, r):
    import hashlib
    st = LISTED[rid]
    return hashlib.blake2b(pickle.dumps((rid, sorted(r['tags'].items()), r['members'], st, [x for x in st if (rid, x) in GUESSED]), protocol=5),
                           digest_size=16).digest()
RMEMO, RNEW = {}, {}
if CACHE:
    RVER = loop_version(sys._getframe().f_lineno)
    try: RMEMO = (lambda c: c['routes'] if c['version'] == RVER else {})(pickle.load(open(os.path.join(CACHE, 'routes.pickle'), 'rb')))
    except OSError: pass
R = {}               # rid -> route: mode, tags, stops (nodes), ways, exp, loop, cand (stations on the track, for pass 2)
nfill = Counter()
for rid, r in ROUTES.items():
    rk = rkey(rid, r) if CACHE and rid not in DBG else None
    if rk in RMEMO:
        R[rid] = {k: r['tags'] if k == 't' else v for k, v in RMEMO[rk][0].items()}; nfill.update(RMEMO[rk][1]); RNEW[rk] = RMEMO[rk]
        if RMEMO[rk][2]: PASS2[rid] = track(route_ways(r))
        continue
    n0 = nfill.copy()
    t = r['tags']; mode = t['route']; stops = LISTED[rid]; ways = route_ways(r)
    if mode == 'train' and ways and any((rid, x) in GUESSED for x in stops):     # a station guessed for an unnamed stop must be on the track
        stops = [x for i, x in enumerate(stops) if (rid, x) not in GUESSED or dist_to_ways(ways, *NODES[x][:2]) <= (
            300 if i in (0, len(stops) - 1) else min(nearest_main(x)[0] + 30, 300))]      # a terminus: the track may end short of it
    exp = mode == 'train' and expressish(t)
    if mode != 'train' and len(stops) >= 4:      # an urban route that lists the stations of a railway far away too (Teresina's
        g = [metres(*NODES[a][:2], *NODES[b][:2]) for a, b in zip(stops, stops[1:])]       # light rail, to Parnaíba): its
        cut = [k + 1 for k, d in enumerate(g) if d > max(20000, 8 * float(np.percentile(g, 25)))]  # densest run of 3+ stops
        runs = [(x, y) for x, y in zip([0] + cut, cut + [len(stops)]) if y - x >= 3]
        if cut and runs: x, y = min(runs, key=lambda r: float(np.median(g[r[0]:r[1] - 1]))); stops = stops[x:y]; nfill['urban far stops'] += 1
    loop = t.get('roundtrip') == 'yes' or len(stops) > 3 and (stops[0] == stops[-1] or metres(*NODES[stops[0]][:2], *NODES[stops[-1]][:2]) < 300)
    tr = (track(ways) if rid not in DBG else Track(ways, True)) if ways and (len(stops) >= 3 or len(stops) < 2 or mode == 'train') else None
    cand, bst = [], []
    if tr and not loop:
        if len(stops) >= 3:
            o = by_track(stops, tr)
            if o is None and mode == 'train' and (sp := branch_split(stops, ways, tr)): (o, bst), nfill['split'] = sp, nfill['split'] + 1
            if o is not None: nfill['reordered'] += o != stops; stops = o
        ends, _ = tr.locate([NODES[stops[0]][:2], NODES[stops[-1]][:2]]) if len(stops) >= 2 else (np.zeros(2), 0)
        sparse = len(stops) <= 3 or tr.L / max(len(stops) - 1, 1) > 6000        # few stops for its length: maybe under-mapped
        few_tram = mode in ('tram', 'light_rail') and len(stops) >= 2 and tr.L / (len(stops) - 1) > 1500     # an under-mapped tram route:
        if len(stops) < 2 or few_tram or mode == 'train' and (not exp and sparse or min(ends) > 1500 or tr.L - max(ends) > 1500):
            cand = along(ways, mode, tr, lim_urban=30 if few_tram else 60)                              # the stops beside its track
            cn = [n for n, _ in cand]
            local = {x.strip() for x in (t.get('service') or '').split(';')} & {'commuter', 'suburban', 'local', 'urban'} or \
                t.get('passenger') in ('suburban', 'urban', 'local') or re.fullmatch(r'S ?\d{1,2}', t.get('ref') or '') or \
                SUBURBAN.search(' '.join(t.get(k, '') for k in ('name', 'network', 'operator'))) or few_tram
            long = tr.L > 150000 and len(cn) > 30 and not local and not LINE_REL.search(clean_name(t.get('name')) or '')   # a long-distance train
            if long and len(stops) <= 3: exp = True
            if not exp and (len(stops) < 2 or len(stops) <= 3 and len(cn) >= len(stops) + 2 or local and len(stops) < 0.5 * len(cn)):
                keep = [s for s in stops if s not in set(cn)]
                if len(stops) >= 2:          # an urban line: not far past its listed end stops (Teresina's light rail,
                    nfill['filled'] += 1       # whose relation runs on along the railway to Parnaíba)
                    (lo, hi), ed = tr.locate([NODES[stops[0]][:2], NODES[stops[-1]][:2]]); lo, hi = min(lo, hi), max(lo, hi); mg = max(5000, 0.5 * (hi - lo))
                    if mode != 'train' and tr.L > 60000: cn = [n for n, x in cand if lo - mg <= x <= hi + mg]
                merged = cn + keep
                tt, dd = tr.locate([NODES[n][:2] for n in merged])
                stops = [n for n, _, d in sorted(zip(merged, tt.tolist(), dd.tolist()), key=lambda a: a[1]) if d <= 500 or n in cn]
                if len(stops) >= 2 and len(LISTED[rid]) >= 2 and (LISTED[rid][0] in stops and LISTED[rid][-1] in stops) and \
                        stops.index(LISTED[rid][0]) > stops.index(LISTED[rid][-1]): stops = stops[::-1]
            elif len(stops) >= 2 and cand:        # termini at the ends of the track
                (a, b), _ = tr.locate([NODES[stops[0]][:2], NODES[stops[-1]][:2]])
                have, up = set(stops), a <= b
                A, B = NODES[stops[0]][:2], NODES[stops[-1]][:2]; AB = metres(*A, *B)
                pre = [(n, x) for n, x in cand if (x < a - 300 if up else x > a + 300) and n not in have and metres(*NODES[n][:2], *B) > AB]
                post = [(n, x) for n, x in cand if (x > b + 300 if up else x < b - 300) and n not in have and metres(*NODES[n][:2], *A) > AB]
                if mode != 'train' and tr.L > 60000:     # an urban relation running on far past its end stops (Teresina): not past half its length
                    mg = max(5000, 0.5 * abs(b - a)); pre = [p for p in pre if abs(p[1] - a) <= mg]; post = [p for p in post if abs(p[1] - b) <= mg]
                pre.sort(key=lambda p: p[1], reverse=not up); post.sort(key=lambda p: p[1], reverse=not up)
                if exp:          # an express: only a station at the very end of its track
                    pre = pre[:1] if pre and min(pre[0][1], tr.L - pre[0][1]) < 2000 else []
                    post = post[-1:] if post and min(post[-1][1], tr.L - post[-1][1]) < 2000 else []
                if pre or post: nfill['termini'] += 1; stops = [n for n, _ in pre] + stops + [n for n, _ in post]
        if len(stops) >= 2:
            ts_, _ = tr.locate([NODES[stops[0]][:2], NODES[stops[-1]][:2]])
            ways = trim(ways, tr, *sorted(ts_.tolist()))
    few = exp and cand and len(stops) <= 3         # an express with (nearly) no stops: pass 2 adds the major stations on its track
    if few: PASS2[rid] = tr
    R[rid] = {'mode': mode, 't': t, 'stops': stops, 'bstops': bst, 'ways': ways, 'exp': exp, 'loop': loop, 'cand': cand if few else [], 'n0': len(LISTED[rid])}
    if rid in DBG: print('  DBG route', rid, t.get('name'), 'exp', exp, 'loop', loop, 'listed', [sname(n) for n in LISTED[rid]], '->', [sname(n) for n in stops])
    if rk is not None: RNEW[rk] = ({k: None if k == 't' else v for k, v in R[rid].items()}, nfill - n0, bool(few))
if CACHE:
    os.makedirs(CACHE, exist_ok=True)
    pickle.dump({'version': RVER, 'routes': RNEW}, open(os.path.join(CACHE, 'routes.pickle'), 'wb'), protocol=5)
    log('routes reused', sum(k in RMEMO for k in RNEW), 'of', len(ROUTES))
del RMEMO, RNEW
log('routes processed', dict(nfill))

# ---------------------------------------------------------------- station records: one per place name
for n, (lon, lat, t) in NODES.items():      # readings for Japanese names (translit.ja_latn)
    if KANA.search(t.get('name') or '') or HAN.search(t.get('name') or ''):
        for k in ('name:ja-Latn', 'name:ja_rm', 'name:en'):
            if latin(t.get(k)): ja_word(stn_name(t), stn_name({'name': t[k]})); break
for lon, lat, kind, name, en, pop in PLACES.values():
    if name and latin(en) and (KANA.search(name) or HAN.search(name)): ja_word(re.sub(r'(?:市|区|町|村)$', '', name), en)
NUMS = defaultdict(list)       # pole / platform numbered names ("Banacha 05", "Richmond 1"): base name -> nodes
_POLE = re.compile(r'(.+?\D)\s+0?\d{1,2}[A-Za-z]?$')
for n, (lon, lat, t) in NODES.items():
    if stoplike(t) and t.get('name'):
        m = _POLE.match(sname(n))
        if m and not re.search(r'(?i)(?:terminal|gate|pier|level|halle|ebene|sektor|linie|line|\bt|zone|exit|ausgang|parking|p)$', m.group(1)):
            NUMS[m.group(1)].append(n)
for n, (lon, lat, t) in NODES.items():
    if stoplike(t) and t.get('name') and sname(n) in NUMS: NUMS[sname(n)].append(n)
def pole_free(n, name):
    m = _POLE.match(name) or re.match(r'(.+\d)\s+0\d$', name); t = NODES[n][2]      # also "Bitwy Warszawskiej 1920 03"
    if m and not (t.get('railway') in ('station', 'halt') or t.get('public_transport') == 'station') and (
            re.search(r'\s0\d$', name) or t.get('public_transport') in ('platform', 'stop_position') or t.get('railway') in ('platform', 'stop')) and \
            not re.search(r'(?i)(?:terminal|gate|pier|level|halle|ebene|sektor|linie|line|\bt|zone|exit|ausgang|parking|p|nr|no)$', m.group(1)):
        return m.group(1)       # "Banacha 05", "Richmond 1": a pole or platform of a stop
    if m and m.group(1) in NUMS and len(NUMS[m.group(1)]) < 5000:
        lon, lat = NODES[n][:2]
        if any(o != n and metres(lon, lat, *NODES[o][:2]) < 300 for o in NUMS[m.group(1)]): return m.group(1)
    return name
def prio(t): return 3 if t.get('railway') == 'station' else 2 if t.get('railway') in ('halt', 'tram_stop') or t.get('public_transport') == 'station' else 1
_cc = {}
def cc_of(n):
    if n not in _cc: _cc[n] = country_code(*NODES[n][:2])
    return _cc[n]
stations, st_index, rec_mk, rec_pri = [], {}, [], []
RGRID = Grid([], 0.005)
def station_for(nid):
    """Station record of a stop node: the record of the same place name within 400 m (city prefixes, station words and
    platform numbers ignored: "London St Pancras" = "St Pancras International"; 800 m for train stops); else a new record."""
    if nid in st_index: return st_index[nid]
    lon, lat, t = NODES[nid]
    name = pole_free(nid, sname(nid)) if sname(nid) else stn_name({'name': t.get('name:en')})
    mk = mkey(name, lon, lat) if name else ''
    best = None
    if mk:
        best = next((j for d, j in RGRID.near(lon, lat, 400 if is_urban_station(t) else 800) if rec_mk[j] == mk), None)   # Tokyo's platforms
    if best is not None:
        st_index[nid] = best
        if prio(t) > rec_pri[best] and name:       # a station node names the record better than a stop position
            s = stations[best]; s[0] = name; s[1] = english_station(name, t, lon, lat, s[8]); rec_pri[best] = prio(t)
        return best
    cc = cc_of(nid)
    j = st_index[nid] = len(stations)
    stations.append([name, english_station(name, t, lon, lat, cc) if name else '', round(lon, 5), round(lat, 5), '', [], [], -1, cc])
    rec_mk.append(mk); rec_pri.append(prio(t)); RGRID.c[(math.floor(lon / RGRID.s), math.floor(lat / RGRID.s))].append((j, lon, lat))
    return j
def conv(seq):
    out = [s for s in map(station_for, seq) if stations[s][0] or stations[s][1]]
    return [s for i, s in enumerate(out) if i == 0 or s != out[i - 1]]
for x in R.values(): x['sids'] = conv(x['stops']); x['branches'] = [b for b in map(conv, x.pop('bstops')) if len(b) >= 2]
for rid in DBG & set(R): print('  DBG sids', rid, [(s, stations[s][0]) for s in R[rid]['sids']], R[rid]['branches'])
def rsids(x): return x['sids'] + [s for b in x.get('branches', []) for s in b]
SERVED = {s for x in R.values() if len(set(x['sids'])) >= 2 for s in rsids(x)}
MAJOR = {s for x in R.values() if x['exp'] and len(set(x['sids'])) >= 2 for s in x['sids']}
# long-distance trains (ends 300+ km apart) in countries where they are most of the mapped services (India, Pakistan, ...):
# a railway line whose stations only they call at is not already a line of the map (Rajdhanis on the Lucknow - Varanasi line)
def _span(x): return metres(*stations[x['sids'][0]][2:4], *stations[x['sids'][-1]][2:4]) if len(x['sids']) >= 2 else 0
SERVED_LOCAL = {s for x in R.values() if len(set(x['sids'])) >= 2 and _span(x) < 300000 for s in rsids(x)}
_ex = defaultdict(lambda: [0, 0])
for x in R.values():
    if x['mode'] == 'train' and len(x['sids']) >= 2: e = _ex[stations[x['sids'][0]][8]]; e[0] += 1; e[1] += _span(x) >= 300000
EXPRESSY = {cc for cc, (n, k) in _ex.items() if n >= 20 and k > 0.5 * n}
def covered(s): return s in SERVED_LOCAL or s in SERVED and stations[s][8] not in EXPRESSY
# pass 2: expresses and long-distance trains with 0-3 stop members -> their stops plus the stations on their track other
# long-distance trains stop at (or on high-speed track), and the stations at its ends; failing that, served stations,
# then all stations (track no other service uses: El Chepe; also on 80+ km stretches without a picked station)
n2 = Counter()
TRAIN_SERVED = {s for x in R.values() if x['mode'] == 'train' and len(set(x['sids'])) >= 2 for s in x['sids']}
for rid, x in R.items():
    if not x['cand']: continue
    tr = PASS2.pop(rid); cand = x['cand']; have = set(x['stops'])
    def keep(n, major):
        w = nearest_main(n)[1]
        return n in have or station_for(n) in major or NODES[n][2].get('railway') == 'station' and w is not None and fast_way(WAYS[w][0])
    pick = [(n, p) for n, p in cand if keep(n, MAJOR)]
    if len(pick) < 3: pick = [(n, p) for n, p in cand if keep(n, TRAIN_SERVED)]
    if len(pick) < 3: pick = [(n, p) for n, p in cand if NODES[n][2].get('railway') == 'station' and active(NODES[n][2])]    # no other service there
    edges = sorted([0.0, tr.L] + [p for _, p in pick]); gaps = [(a, b) for a, b in zip(edges, edges[1:]) if b - a > 80000]
    pick += [(n, p) for n, p in cand if any(a < p < b for a, b in gaps) and NODES[n][2].get('railway') == 'station' and
             active(NODES[n][2]) and (n, p) not in pick]      # nor on a long stretch of it (Sousse - Sfax - Gabès)
    for n, p in (cand[0], cand[-1]):
        if min(p, tr.L - p) < 2000 and (n, p) not in pick: pick.append((n, p))
    for p, (lon, lat) in ((0.0, tr.P[0]), (tr.L, tr.P[-1])):      # a served terminus beside the track's end (Madrid Atocha)
        if not any(abs(q - p) < 2000 for _, q in pick):
            e = next((n for d, n in SGRID.near(lon, lat, 1000) if mode_ok(NODES[n][2], 'train') and station_for(n) in TRAIN_SERVED), None)
            if e is not None: pick.append((e, p))
    got = {n for n, _ in pick}
    if x['stops']:
        tt, dd = tr.locate([NODES[n][:2] for n in x['stops']])
        pick += [(n, p) for n, p, d in zip(x['stops'], tt.tolist(), dd.tolist()) if n not in got and d <= 1000]
    pick.sort(key=lambda a: a[1])
    s0 = x['stops'][0] if x['stops'] else None
    x['stops'] = [n for n, _ in pick]
    if s0 is not None and s0 in x['stops'][len(x['stops']) // 2 + 1:]: x['stops'] = x['stops'][::-1]
    x['sids'] = conv(x['stops'])
    if len(set(x['sids'])) >= 2:
        x['ways'] = trim(x['ways'], tr, pick[0][1], pick[-1][1]); n2['filled'] += 1
    else: n2['under 2 stops'] += 1; drop_log('express without stops', rid, x['t'].get('name'))
for rid in [rid for rid, x in R.items() if len(set(x['sids'])) < 2]:
    if R[rid]['n0'] >= 2 or R[rid]['ways']: drop_log('under 2 stations', rid, R[rid]['t'].get('name'))
    del R[rid]
log('pass 2', dict(n2), 'routes with 2+ stations', len(R), 'station records', len(stations))
del LISTED, PASS2
for x in R.values(): x['cand'] = None

# ---------------------------------------------------------------- FALLBACK: railway lines where services are not mapped
# route=railway relations (and route=train relations that map a railway line: timetable numbers, "Ligne de … à …") become
# lines when they are not freight / closed, 3+ stations with passenger evidence lie on their own track (a station counts
# when its nearest main line is the relation's), and service lines miss a stretch of them (under 50% served, or under
# 80% with 3 unserved stations in a row); where a country's services are mapped, half its stations need more than a
# public_transport tag (the rest are freight lines). A partly served trunk line (200+ stations or 1500+ km) gives only its
# unserved stretches (3+ stations, with the served stations at their ends). High-speed infrastructure relations
# (Shinkansen, THSR) are anchors: always a line, and their services join them.
ANCHOR = re.compile(r'新幹線|Shinkansen|高速鐵路|高速铁路|고속철도|高鐵', re.I)
def infra_freight(t, ways):
    if t.get('usage') in ('freight', 'industrial', 'military', 'tourism') or t.get('railway:traffic_mode') == 'freight' or t.get('passenger') == 'no': return True
    if FREIGHT.search(' '.join(t.get(k, '') for k in ('name', 'name:en', 'alt_name', 'official_name', 'description'))) or FREIGHT_OP.search(t.get('operator', '')): return True
    L = [(way_len(w), WAYS[w][0]) for w in ways]; tot = sum(l for l, _ in L) or 1
    return sum(l for l, wt in L if wt.get('usage') in ('freight', 'industrial') or wt.get('railway:traffic_mode') == 'freight') > 0.5 * tot
def evidence(n):
    """Signs that passengers use a station: a service stops there, or it is tagged as a public transport station."""
    t = NODES[n][2]
    return station_for(n) in SERVED or t.get('public_transport') in ('station', 'stop_position') or t.get('train') == 'yes' or \
        bool(t.get('uic_ref')) or t.get('railway') == 'station' and bool(t.get('wikidata'))
def strong(s, n):
    """More than a bulk-tagged public_transport=station: a service stops there, or train=yes, codes, wikidata, an operator."""
    t = NODES[n][2]
    return s in SERVED or t.get('train') == 'yes' or any(t.get(k) for k in ('uic_ref', 'railway:ref', 'wikidata', 'operator', 'network'))
MAPPED = Counter(stations[s][8] for s in TRAIN_SERVED)      # countries whose train services are mapped (30+ stations)
KEY_WAYS = defaultdict(set)
for w, (t, c, refs) in WAYS.items():
    if is_main(t) and t.get('name'): KEY_WAYS[re.sub(r'\s|\(.*?\)', '', t['name']).lower()].add(w)
def grow(ways, name):
    """Extend along connected main-line ways carrying the line's name (relations often miss stretches of their line)."""
    same = KEY_WAYS.get(re.sub(r'\s|\(.*?\)', '', name or '').lower())
    if not same or len(same) > 20000: return ways
    node_ways = defaultdict(list)
    for w in same:
        for n in WAYS[w][2].tolist(): node_ways[n].append(w)
    have = set(ways); frontier = [n for w in ways for n in WAYS[w][2].tolist()]
    while frontier:
        for w in node_ways.get(frontier.pop(), ()):
            if w not in have: have.add(w); frontier.extend(WAYS[w][2].tolist())
    return sorted(have)
USED = {w for x in R.values() for w in x['ways']}
# ---------------------------------------------------------------- track that no relation maps
# Where train services are mostly unmapped (India, Pakistan, much of Africa and Asia: under 60% of the country's main-line
# track is used by passenger service relations), railway lines that no route relation covers become synthetic railway
# relations - connected main-line ways of one name ("Varanasi–Sultanpur–Lucknow line"), and connected unnamed stretches away
# from mapped track - and the fallback below judges them like the mapped ones (stations with passenger evidence, freight
# names, lines services already cover).
_inrel = {w for r in RELS.values() if r['tags'].get('type') == 'route' for typ, w, role in r['members'] if typ == 'w'}
_ccg = {}
def _wcc(w):
    c = WAYS[w][1][len(WAYS[w][1]) // 2]; k = (round(float(c[0]), 1), round(float(c[1]), 1))
    if k not in _ccg: _ccg[k] = country_code(*k)
    return _ccg[k]
_tot, _cov = Counter(), Counter()
for w in MAIN_W:
    cc = _wcc(w); _tot[cc] += way_len(w)
    if w in USED: _cov[cc] += way_len(w)
# (not in the Americas: unmapped track there is freight, named after the railway company that runs it - "FC Roca",
# "Ferrovia Centro-Atlântica" - an audit found every synthetic line there wrong)
AMERICAS = {'AR', 'BO', 'BR', 'BZ', 'CA', 'CL', 'CO', 'CR', 'CU', 'DO', 'EC', 'GF', 'GT', 'GY', 'HN', 'HT', 'JM', 'MX', 'NI', 'PA', 'PE', 'PR',
            'PY', 'SR', 'SV', 'TT', 'US', 'UY', 'VE'}
SPARSE = {cc for cc in _tot if cc not in EXCLUDE and cc not in AMERICAS and _cov[cc] < 0.6 * _tot[cc]}
# (also Italy, whose regional services OSM mostly leaves unmapped: the Adriatic line, Cagliari - Golfo Aranci, the Pontremolese;
# elsewhere in Europe and Russia unmapped track with stations is closed to passengers, freight (Bovanenkovo, the Magistrala
# Węglowa) or heritage, as an audit of every country's unused main-line track found)
SYN_CC = SPARSE | {'IT'}
_free = [w for w in MAIN_W if w not in _inrel and (_wcc(w) in SYN_CC or FORCE_WAYS and _wcc(w) not in EXCLUDE) and
         WAYS[w][0].get('usage') not in ('industrial', 'military', 'test', 'tourism')]
_mapped = [w for w in MAIN_W if w in _inrel]
_mc = np.unique(cells(*samples(_mapped, 0.0005)[0].T)) if _mapped else np.zeros(0, np.int64)
def _key(w): return re.sub(r'\s|\(.*?\)', '', WAYS[w][0].get('name') or '').lower()
_un = [w for w in _free if not _key(w)]      # unnamed: the second track of a mapped line when most of it lies in mapped cells
_near = set()
if _un:
    pts, wi = samples(_un, 0.0005)
    hit = np.isin(cells(*pts.T), _mc)
    share = np.bincount(wi, weights=hit, minlength=len(_un)) / np.maximum(np.bincount(wi, minlength=len(_un)), 1)
    _near = {w for w, f in zip(_un, share.tolist()) if f > 0.6 and w not in FORCE_WAYS}
_par = {}
def _f(x):
    while _par.get(x, x) != x: _par[x] = _par.get(_par[x], _par[x]); x = _par[x]
    return x
_by_node = defaultdict(list)
_free = [w for w in _free if w not in _near]
_ff = set(_free)
if FORCE_WAYS: log('forced ways', len(FORCE_WAYS), 'not free (in a route relation, not main line):', sorted(w for w in FORCE_WAYS if w in WAYS and w not in _ff)[:80])
_fw = {w for w in FORCE_WAYS if w in WAYS and w not in _ff}      # forced track in a railway relation: that relation (a line's,
for rid, r in RELS.items():                                       # not a whole network's: under 400 ways)
    t = r['tags']
    if rid in DROP or t.get('type') != 'route' or not (t.get('route') == 'railway' or rid in INFRA_T): continue
    ws = [x for typ, x, role in r['members'] if typ == 'w']
    hit = next((x for x in ws if x in _fw), None)
    if hit and len(ws) < 400 and open_share(r) >= 0.9 and not re.search(r'(?i)construct|proposed|planned|projet|projekt', t.get('name', '')):
        FORCE_FB.setdefault(rid, FORCE_FB[-hit])
log('forced relations', len(FORCE_FB))
for w in _free:
    for n in (WAYS[w][2][0], WAYS[w][2][-1]): _by_node[(int(n), _key(w))].append(w)
for ws in _by_node.values():
    for w in ws[1:]: _par[_f(w)] = _f(ws[0])
def _wild(w):       # a stretch whose name is not its line's: unnamed, or a bridge / tunnel ("Galleria Monte Totila")
    t = WAYS[w][0]
    return not _key(w) or t.get('bridge', 'no') not in ('no', '') or t.get('tunnel', 'no') not in ('no', '')
_wp = {}
def _g(x):
    while _wp.get(x, x) != x: _wp[x] = _wp.get(_wp[x], _wp[x]); x = _wp[x]
    return x
_wl = [w for w in _free if _wild(w)]
_wn = defaultdict(list)
for w in _wl:
    for n in (int(WAYS[w][2][0]), int(WAYS[w][2][-1])): _wn[n].append(w)
for ws in _wn.values():
    for w in ws[1:]: _wp[_g(w)] = _g(ws[0])
_runs = defaultdict(list)
for w in _wl: _runs[_g(w)].append(w)
_named_at = defaultdict(set)      # node -> names of the named free ways ending there
for w in _free:
    if not _wild(w):
        for n in (int(WAYS[w][2][0]), int(WAYS[w][2][-1])): _named_at[n].add(_key(w))
njoin = 0
for run in _runs.values():        # a run between two stretches of one name (or a short one at its end) joins that line
    touch = defaultdict(set)
    for w in run:
        for n in (int(WAYS[w][2][0]), int(WAYS[w][2][-1])):
            for k in _named_at.get(n, ()): touch[k].add(n)
    if len(touch) != 1: continue
    k, nodes = next(iter(touch.items()))
    if len(nodes) < 2 and sum(map(way_len, run)) > 2000: continue
    a = next(x for n in nodes for x in _by_node[(n, k)])
    for w in run: _par[_f(w)] = _f(a)
    njoin += 1
_comp = defaultdict(list)
for w in _free: _comp[_f(w)].append(w)
def _ends(ws):
    """Dead ends of connected ways: 2 on a line, many on a network ("FC Roca": a railway company's whole network)."""
    ep = Counter(int(n) for w in ws for n in (WAYS[w][2][0], WAYS[w][2][-1]))
    inner = set(np.concatenate([WAYS[w][2][1:-1] for w in ws]).tolist())
    return sum(1 for n, k in ep.items() if k == 1 and n not in inner)
nsyn, nnet = 0, 0
for ws in _comp.values():         # id: minus its lowest way id (stable across builds; drops.json names one of its ways)
    forced = any(w in FORCE_WAYS for w in ws)
    if not forced and Counter(_wcc(w) for w in ws).most_common(1)[0][0] not in SYN_CC: continue
    if sum(map(way_len, ws)) < (2000 if forced else 8000): continue
    if not forced and (sum(map(way_len, ws)) > 1.2e6 or _ends(ws) > 6): nnet += 1; continue
    if forced: FORCE_FB[-min(ws)] = next(FORCE_FB[-w] for w in ws if w in FORCE_WAYS)
    name = Counter()
    for w in ws:
        if WAYS[w][0].get('name') and not _wild(w): name[WAYS[w][0]['name']] += way_len(w)
    name = name.most_common(1)
    why = next((DROP_WAYS[w] for w in ws if w in DROP_WAYS), None)
    if why: DROP[-min(ws)] = why
    nsyn += 1; RELS[-min(ws)] = {'tags': {'type': 'route', 'route': 'railway', 'name': name[0][0] if name else '', '_synthetic': '1'},
                                 'members': [('w', w, '') for w in ws]}
log('synthetic railway relations from unmapped track', nsyn, '(', len(SPARSE), 'sparse countries;', nnet, 'networks left out;', njoin, 'bridge / tunnel / unnamed runs joined )')
# track of services an audit dropped as not running (closed, suspended, freight, tourist): a railway relation along it is no
# passenger line either (Cape Town - De Aar under the suspended Shosholoza Meyl, the Main South Line under Dunedin's excursions)
_DW = set()
for rid, why in DROP.items():
    r = RELS.get(rid)
    if rid > 0 and r and r['tags'].get('type') == 'route' and r['tags'].get('route') == 'train' and \
            not re.search(r'(?i)dup|artefact|artifact|fragment|stub|historic|partial|corridor', why):
        _DW.update(x for typ, x, role in r['members'] if typ == 'w')
FB, nfb = [], Counter()        # fallback lines: dict(rid, t, stops (records), branches, ways, anchor)
ORPHAN = []                    # (served station, station no service stops at, served station) along a railway line
for rid in [rid for rid, r in RELS.items() if r['tags'].get('type') == 'route' and r['tags'].get('route') == 'railway'] + INFRA_T:
    r = RELS[rid]; t = r['tags']
    ways = [w for typ, w, role in r['members'] if typ == 'w' and w in WAYS and is_main(WAYS[w][0])]
    anchor = bool(ANCHOR.search(t.get('name', '') + t.get('name:en', ''))) or t.get('service') == 'high_speed'
    if not ways or sum(map(way_len, ways)) < 2000: continue
    if rid in DROP: nfb['audit'] += 1; drop_log('fallback audit ' + DROP[rid], rid); continue
    forced = rid in FORCE_FB
    if not forced and (lifecycle(t) or open_share(r) < 0.5 or rid in DENY): nfb['closed'] += 1; continue
    if not forced and (infra_freight(t, ways) or junk(t)): nfb['freight / junk'] += 1; drop_log('fallback freight / junk', rid, t.get('name'), infra_freight(t, ways), junk(t)); continue
    if not forced and not anchor and sum(way_len(w) for w in ways if w in _DW) > 0.5 * sum(map(way_len, ways)):
        nfb['dropped service track'] += 1; drop_log('fallback on a dropped service track', rid, t.get('name')); continue
    c = WAYS[ways[len(ways) // 2]][1]; rcc = country_code(*c[len(c) // 2])
    if rcc in EXCLUDE: continue
    if rid > 0: ways = grow(ways, t.get('name'))    # (a synthetic relation already has all its unmapped track of that name: growing
    tr = track(ways) if rid not in DBG else Track(ways, True)       # it over mapped track makes "FC Roca" a 1,700 km line)
    if tr is None: continue
    own = [snap(ref, 'train') for typ, ref, role in r['members'] if typ == 'n']
    own = [s for s in own if s is not None and active(NODES[s][2]) and (evidence(s) or forced) and not is_urban_station(NODES[s][2])]
    ev = evidence if not forced else (lambda n: bool(NODES[n][2].get('name')) and NODES[n][2].get('railway') in ('station', 'halt'))
    got = {n: p for n, p in along(ways, 'train', tr, ev, strict=anchor)} if not (anchor and len(own) >= 2) else {}
    for s in own:          # its own station members (all a high-speed line has, when it lists them), stations at its ends
        if s not in got:
            p, d = tr.locate([NODES[s][:2]])
            if d[0] < 500: got[s] = float(p[0])
    for end, p in ((tr.P[0], 0.0), (tr.P[-1], tr.L)):
        n = next((n for d, n in SGRID.near(float(end[0]), float(end[1]), 500) if mode_ok(NODES[n][2], 'train') and active(NODES[n][2]) and ev(n)), None)
        if n is not None and n not in got: got[n] = p
    jn_add = []                 # (added to the line's stops once it is taken: they count for none of the tests below)
    # (railway relations where services are sparsely mapped: in Europe a railway relation ends where its line does)
    if not anchor and (rid < 0 or forced or rcc in SPARSE):   # track that ends where it meets another line, away from any
        k = max(1, len(tr.P) // 4)  # station (a branch mapped up to the junction's points): that line's station at the junction (within 6 km, a station a
                                    # service stops at, beyond the end, not back along the track) ends it. Unmapped track
                                    # that runs into mapped track ends there, often a station short of the junction: when its
                                    # end station has no service, the served station beyond it (within 12 km) ends it
        for end, p, inner in ((tr.P[0], 0.0, tr.P[k]), (tr.P[-1], tr.L, tr.P[-1 - k])):
            at = [n for n, q in got.items() if abs(q - p) < 600]
            if any(covered(station_for(n)) for n, q in got.items() if abs(q - p) < 1500): continue     # it ends at a served
            e, i_ = (float(end[0]), float(end[1])), (float(inner[0]), float(inner[1]))              # station (a big one's node
                                                                                                   # can be 1 km from the track)
            ck = int(cells(np.array([e[0]]), np.array([e[1]]))[0]); j = int(np.searchsorted(_mc, ck))     # (binary search)
            if rid < 0 and j < len(_mc) and int(_mc[j]) == ck and not any(covered(station_for(n)) for n in at): reach = 12000
            elif at: continue
            else: reach = 6000
            for d, n in SGRID.near(*e, reach):
                if n in got or not (mode_ok(NODES[n][2], 'train') and active(NODES[n][2]) and NODES[n][2].get('name')): continue
                if metres(*NODES[n][:2], *i_) <= metres(*e, *i_): continue
                if not covered(station_for(n)): continue
                jn_add.append((n, p)); break
    rec = {}
    for n, p in sorted(got.items(), key=lambda a: a[1]):
        s = station_for(n)
        if stations[s][0] and s not in rec: rec[s] = (p, n)
    if rid in DBG: print('  DBG fallback', rid, t.get('name'), len(ways), 'ways', len(got), 'stations', len(rec), 'records', [stations[s][1] for s in rec][:20])
    if len(rec) < (2 if forced else 3): nfb['under 3 stations'] += 1; continue
    served, prev = sum(covered(s) for s in rec) / len(rec), None
    run = best = 0
    for s in rec: run = 0 if covered(s) else run + 1; best = max(best, run)
    if rid in DBG: print('  DBG fallback served', round(served, 2), 'unserved run', best, [stations[s][0] for s in rec if not covered(s)][:12])
    if not anchor and not forced and not (served < 0.5 or served < 0.8 and best >= 3):
        nfb['duplicates services'] += 1; run = []         # its few stations no service stops at, and the served ones around them
        for s in rec:
            if not covered(s): run.append(s); continue
            if run and prev is not None: ORPHAN.append((prev, [x for x in run if strong(x, rec[x][1])], s))
            prev, run = s, []
        continue
    if not anchor and not forced and MAPPED[stations[next(iter(rec))][8]] >= 30 and sum(strong(s, n) for s, (p, n) in rec.items()) < 0.5 * len(rec):
        nfb['no passenger evidence'] += 1; drop_log('fallback without passenger evidence', rid, t.get('name')); continue
    # stations off the main path (a second branch of the relation) -> a branch from the nearest main station
    pts = [NODES[n][:2] for p, n in rec.values()]; _, dd = tr.locate(pts)
    main = [s for (s, (p, n)), d in zip(rec.items(), dd.tolist()) if d <= 1000]
    off = [(s, n) for (s, (p, n)), d in zip(rec.items(), dd.tolist()) if d > 1000]
    branches = []
    if len(off) >= 2 and len(main) >= 2:
        far = [w for w, m in zip(ways, tr.locate([WAYS[w][1][len(WAYS[w][1]) // 2] for w in ways])[1].tolist()) if m > 1000]
        tb = Track(far) if far else None
        if tb is not None and tb.ok:
            pb, _ = tb.locate([NODES[n][:2] for s, n in off]); o = [off[i][0] for i in np.argsort(pb)]
            a, b = stations[o[0]][2:4], stations[o[-1]][2:4]
            ja = min(main, key=lambda s: metres(*stations[s][2:4], *a)); jb = min(main, key=lambda s: metres(*stations[s][2:4], *b))
            branches = [[ja] + o] if metres(*stations[ja][2:4], *a) <= metres(*stations[jb][2:4], *b) else [[jb] + o[::-1]]
    if len(main) < (2 if forced else 3): continue
    g = [metres(*stations[x][2:4], *stations[y][2:4]) for x, y in zip(main, main[1:])]
    if not anchor and not branches and len(g) >= 5 and np.median(g) < 5000:      # an end station far out past a dense line (DL&W
        while len(main) > 3 and g[-1] > max(50000, 20 * np.median(g)): main, g = main[:-1], g[:-1]    # Mainline: Hackettstown, then
        while len(main) > 3 and g[0] > max(50000, 20 * np.median(g)): main, g = main[1:], g[1:]       # Scranton's trolley museum)
    # a long line its services mostly cover (Trans-Siberian; St Petersburg - Warsaw, services at 80%+ of its 100+ stations): whole or
    # not at all (the Czech corridors, services at 3/4 of their stations, stay: their local stops are on no other line); its stations
    # no service stops at go into the services that call at the served stations on either side of them
    if not anchor and ((len(main) > 200 or tr.L > 1.5e6) and served >= 0.5 or len(main) > 100 and served >= 0.8):
        for q in [main] + branches:
            cur, prev = [], None
            for s in q:
                if not covered(s): cur.append(s); continue
                if cur and prev is not None: ORPHAN.append((prev, [x for x in cur if strong(x, rec[x][1])], s))
                prev, cur = s, []
        nfb['long line served'] += 1; drop_log('fallback long line served', rid, t.get('name'), len(main), round(served, 2)); continue
    cuts = [k + 1 for k, (x, y) in enumerate(zip(main, main[1:])) if not anchor and any(b - a > 100000 and min(rec[x][0], rec[y][0]) <= a and
                                                                                     b <= max(rec[x][0], rec[y][0]) for a, b in tr.gaps)]
    if cuts:        # a relation whose pieces lie far apart (Howrah-Nagpur-Mumbai line): a line per piece of 3+ stations, the
        runs = [(0, q) for q in (main[x:y] for x, y in zip([0] + cuts, cuts + [len(main)])) if len(q) >= 3] + [(1, q) for q in branches if len(q) >= 3]
        far = [w for w, m in zip(ways, tr.locate([WAYS[w][1][len(WAYS[w][1]) // 2] for w in ways])[1].tolist()) if m > 1000]
        big = max(runs, key=lambda r: len(r[1]), default=None)         # longest named as the relation (Inlandsbanan)
        for k, q in runs:
            (a, b), _ = tr.locate([stations[q[0]][2:4], stations[q[-1]][2:4]])
            FB.append({'rid': rid, 't': t if q is big[1] else dict(t, **{'name': '', 'name:en': '', 'ref': ''}), 'stops': q, 'branches': [],
                       'ways': trim(ways, tr, min(a, b), max(a, b)) if k == 0 else far, 'anchor': False})
        nfb['split at a gap'] += len(runs); continue
    (a, b), _ = tr.locate([stations[main[0]][2:4], stations[main[-1]][2:4]])
    for n, p in jn_add:         # the junction stations beyond its ends
        s = station_for(n)
        if s in main or not stations[s][0]: continue
        main = [s] + main if p < 0.5 * tr.L else main + [s]; nfb['junction end'] += 1
    FB.append({'rid': rid, 't': t, 'stops': main, 'branches': branches, 'ways': trim(ways, tr, min(a, b), max(a, b)) if not branches else ways,
               'anchor': anchor})
    nfb['anchor' if anchor else 'added'] += 1
log('fallback railway relations', dict(nfb))
_fg = {x['rid'] for x in FB if x['rid'] in FORCE_FB}
log('forced lines', len(_fg), 'of', len(FORCE_FB), 'forced relations and ways; not made:', sorted(k for k in FORCE_FB if k not in _fg and k > 0)[:60])

# ---------------------------------------------------------------- lines: routes clustered around representatives
NIGHT = re.compile(r'\bnight|nacht|nuit|noct|nocn|ночн|нічн|夜行|심야|nattåg|\bNL? ?\d', re.I)
def canon(ref):     # a line code for comparing: "SN6.4" = "SN6", "BRCA" = "CABR" (codes of the two directions)
    k = re.sub(r'\W', '', re.sub(r'\.\d+$', '', ref)).upper()
    return min(k, k[2:] + k[:2]) if re.fullmatch(r'[A-Z]{4}', k) else k
# train categories given as a ref ("RE" without a number, "Os"), and a ref that 3+ route masters of one operator share
# (Polregio's "R"): not line codes, never a key for grouping
CATREF = {'R', 'RE', 'RB', 'RS', 'RX', 'REX', 'IR', 'IRE', 'IC', 'ICE', 'ICN', 'EC', 'EN', 'NJ', 'RJ', 'RJX', 'OS', 'PASS', 'REGIO', 'SP', 'TER', 'TLK'}
_rm = defaultdict(set)
for rid, r in RELS.items():
    if r['tags'].get('type') == 'route_master' and r['tags'].get('ref'): _rm[(first(r['tags']['ref']).upper(), first(r['tags'].get('operator') or r['tags'].get('network')))].add(rid)
CATREF |= {k for k, v in _rm.items() if len(v) >= 3 and not re.search(r'\d', k[0])}
def catref(t):
    r = first(t.get('ref')).upper()
    return 'train' in (t.get('route'), t.get('route_master')) and (r in CATREF or (r, first(t.get('operator') or t.get('network'))) in CATREF)
def refkey(ts):
    c = Counter(canon(good_ref(t.get('ref'), t.get('name', ''))) for t in ts if not catref(t))
    c.pop('', None)
    return c.most_common(1)[0][0] if c else ''
def unit(kind, uid, routes, mt):
    ts = ([mt] if mt else []) + [R[x]['t'] for x in routes]
    main = max(routes, key=lambda x: (len(set(R[x]['sids'])), -routes.index(x)))
    name = next((clean_name(t.get('name')) for t in ts if clean_name(t.get('name'))), '')
    return {'kind': kind, 'id': uid, 'routes': routes, 'mt': mt, 'sids': {s for x in routes for s in rsids(R[x])}, 'main': main,
            'ends': frozenset((R[main]['sids'][0], R[main]['sids'][-1])),
            'fam': families(ts), 'ref': refkey([mt]) if mt and refkey([mt]) else refkey(ts), 'base': '' if generic(name, ts) else base_key(name),
            'hs': max(2 if HSR.search(t.get('name', '') + ' ' + t.get('brand', '')) or HSR_CS.search(t.get('name', '') + ' ' + t.get('brand', '') + ' ' +
                              t.get('ref', '')) else int(t.get('service') == 'high_speed' or t.get('highspeed') == 'yes') for t in ts[:3]),
            'mode': Counter(R[x]['mode'] for x in routes).most_common(1)[0][0],
            'span': metres(*stations[R[main]['sids'][0]][2:4], *stations[R[main]['sids'][-1]][2:4]) if len(R[main]['sids']) >= 2 else 0,
            'night': any(t.get('service') == 'night' or t.get('by_night') == 'only' or NIGHT.search(' '.join((t.get('ref') or '', t.get('name') or '')))
                         for t in ts[:2])}
def overlap(a, b):
    """(containment of the smaller in the other, Jaccard)"""
    com = len(a & b)
    return com / (min(len(a), len(b)) or 1), com / (len(a | b) or 1)
SPLIT_MASTERS = {4585872}    # masters whose routes are separate lines (Walt Disney World Monorail: Express, Resort, Epcot)
units, in_master = [], set()
for mid, m in RELS.items():
    t = m['tags']
    if t.get('type') != 'route_master': continue
    rs = [x for typ, x, role in m['members'] if typ == 'r' and x in R and x not in in_master]
    if not rs or '直通' in t.get('name', '') or mid in SPLIT_MASTERS: continue
    groups = []           # a master of unrelated routes (disjoint stations) -> one unit per group
    for x in sorted(rs, key=lambda x: -len(set(R[x]['sids']))):
        g = next((g for g in groups if overlap(set(R[x]['sids']), set(R[g[0]]['sids']))[0] >= 0.3), None)
        if g is None: groups.append([x])
        else: g.append(x)
    if generic(clean_name(t.get('name')), [t]) and not refkey([t]):
        # a master that only names the operator or a category: its routes stand alone; unless it is one line named
        # after its network ("Caltrain", not its service pattern "Local Weekend")
        if len(groups) > 1 or not first(t.get('name')) or first(t.get('name')) not in {first(t.get(k)) for k in ('network', 'operator', 'brand')}: continue
        t = m['tags'] = dict(t, _system='1')
        if VERBOSE: print('  one-line master named after its network:', mid, t.get('name'))
    in_master.update(rs)
    for g in groups: units.append(unit('m', mid, g, t))
for rid in R:
    if rid not in in_master: units.append(unit('r', rid, [rid], None))
for i, f in enumerate(FB):
    if f['anchor']:
        key = ('a', f['rid']); R[key] = {'mode': 'train', 't': f['t'], 'stops': [], 'sids': f['stops'], 'ways': f['ways'], 'exp': False,
                                         'loop': False, 'cand': [], 'n0': 0, 'branches': f['branches']}
        units.append(unit('a', key, [key], None))
FB = [f for f in FB if not f['anchor']]
URB = set(URBAN)
def compat(u, v):
    if v['kind'] == 'a': return u['mode'] == 'train'
    if u['ref'] and v['ref'] and u['ref'] != v['ref'] or u['hs'] != v['hs']: return False
    if u['fam'] and v['fam'] and not u['fam'] & v['fam']: return False
    return u['mode'] == v['mode'] or u['mode'] in URB and v['mode'] in URB and u['ref'] and u['ref'] == v['ref']
units.sort(key=lambda u: (u['kind'] != 'a', u['kind'] != 'm', u['night'], not u['ref'], -len(u['sids'])))
clusters, at = [], defaultdict(list)
for u in units:
    best, bs = None, None
    for ci in {ci for s in u['sids'] for ci in at[s]}:
        rep = clusters[ci][0]
        if not compat(u, rep): continue
        cont, jac = len(u['sids'] & rep['sids']) / len(u['sids']), overlap(u['sids'], rep['sids'])[1]
        # one line: its ref (sharing a quarter of its stations: TER 01 Lyon - Paris is not TER 01 Grenoble - Lyon), its name, its ends
        same = bool(u['ref'] and u['ref'] == rep['ref'] and (u['fam'] & rep['fam'] or not u['fam'] or not rep['fam']) and cont >= 0.25 or
                    u['base'] and u['base'] == rep['base'] and cont >= 0.5 or
                    not u['base'] and not u['ref'] and u['ends'] == rep['ends'] and cont >= 0.5)     # one corridor's train numbers
        # a line within a long-distance train's run is not that train (Varanasi - Sultanpur - Lucknow in a Dibrugarh Rajdhani)
        if not same and rep['span'] >= 300000 and u['span'] < 0.5 * rep['span']: continue
        if same or cont >= 0.8 or jac >= 0.6:
            if bs is None or (same, cont) > bs: best, bs = ci, (same, cont)
    if best is None:
        for s in u['sids']: at[s].append(len(clusters))
        clusters.append([u])
    else: clusters[best].append(u)
log('units', len(units), '-> lines', len(clusters))
for ci, cl in enumerate(clusters):
    for u in cl:
        if set(u['routes']) & DBG: print('  DBG cluster', ci, 'rep', cl[0]['kind'], cl[0]['id'], cl[0]['ref'], 'members', [(v['kind'], v['id'], v['ref']) for v in cl])

def branches_of(main, others):
    """Main stop list plus branches from the other routes: a run of 2+ new stations becomes a branch from the station it
    leaves the main route at (or rejoins it); new stations between neighbouring main stops (one direction's stops, a
    one-way loop) go into the main list."""
    main, out, cover = list(main), [], set(main)
    for seq in others:
        new = [i for i, s in enumerate(seq) if s not in cover]
        runs = [[i] for i in new[:1]]
        for i in new[1:]:
            if i == runs[-1][-1] + 1: runs[-1].append(i)
            else: runs.append([i])
        for run in runs:
            seg = [seq[i] for i in run]; a, b = run[0], run[-1]
            before = seq[a - 1] if a > 0 else None; after = seq[b + 1] if b + 1 < len(seq) else None
            pos = {s: i for i, s in enumerate(main)}
            if before in pos and after in pos and abs(pos[before] - pos[after]) <= 2 and len(seg) <= 3:
                i, j = pos[before], pos[after]
                main[min(i, j) + 1:min(i, j) + 1] = seg if i < j else seg[::-1]; cover |= set(seg); continue
            if len(seg) < 2 or before is None and after is None: continue
            out.append([after] + seg[::-1] if before is None else [before] + seg + ([after] if after is not None else []))
            cover |= set(seg)
    return main, out

# ---------------------------------------------------------------- kinds
REGIONAL = re.compile(r'^(?:RE|RB|S|TER|R|IRE|REX|RS|Os|MEX|RX|SE|Sp|L|RL|RG|RT|C|FL|A|Π)\s?-?\d|Sprinter|\bTER\b|Regionalbahn|'
                      r'Regional-?Express|RegionalExpress|Regionale|Regional\b|Media Distancia|Proximité|Stoptrein|Sprinter|Pågatåg|'
                      r'Krösatåg|Västtrafik|Upptåget|Norrtåg|Östgöta|Tåg i Bergslagen|Mälartåg|elektrichka', re.I)
PRODUCT = re.compile(r'^(?:ICE?|ICN|IR|IRE|EC|ECE|EN|NJ|RJX?|REX?|RB|D|EXT?|TGV|TER|OUIGO|MEX|FLX|IC\d|AVE|Alvia|Frecci\w+)\s?\d*\b', re.I)
def popn(p):
    p = re.sub(r'[\s,.]', '', p or '')
    return int(p) if p.isdigit() else 0
BIG = Grid(((popn(pop), lon, lat) for lon, lat, kind, name, en, pop in PLACES.values() if popn(pop) >= 500000), 1.0)
def near_big(lon, lat):      # within the metropolitan area of a city of 500k+ (10 km + 15 km per sqrt(million))
    return any(d < 1000 * (10 + 15 * math.sqrt(p / 1e6)) for d, p in BIG.near(lon, lat, 80000))
def commuterish(stops):
    """Dense stops mostly within a big city's metropolitan area (a commuter line mapped without saying so)."""
    if len(stops) < 5: return False
    gaps = [metres(*stations[a][2:4], *stations[b][2:4]) for a, b in zip(stops, stops[1:])]
    if np.median(gaps) > 3000 or sum(gaps) > 150000: return False
    return sum(near_big(*stations[s][2:4]) for s in stops) >= 0.7 * len(stops)
def fast_share(ways):
    L = [(way_len(w), WAYS[w][0]) for w in ways]
    tot = sum(l for l, t in L)
    return sum(l for l, t in L if fast_way(t)) / tot if tot else 0
def km_of(stops): return sum(metres(*stations[a][2:4], *stations[b][2:4]) for a, b in zip(stops, stops[1:])) / 1000
def train_kind(ts, main_t, ways, stops, ref):
    """route=train: 'h' for high-speed services (service / highspeed tags, HSR brands, or a long-distance product with
    most of its track at 250 km/h); never for regional or commuter products. 's' for S-Bahn / RER / commuter networks,
    elektrichki, service=commuter / passenger=suburban on a short route, or dense stops around a big city; else 'r'."""
    svc = {s.strip() for s in (main_t.get('service') or ts[0].get('service') or '').split(';')} - {''}
    pas = main_t.get('passenger') or ts[0].get('passenger') or ''
    txt = ' '.join(t.get(k, '') for t in ts[:3] for k in ('network', 'network:en', 'name', 'name:en', 'operator', 'brand', 'ref'))
    sub = bool(SUBURBAN.search(txt)) or pas in ('suburban', 'urban') or bool(svc & {'commuter', 'suburban'}) or \
        bool(re.fullmatch(r'S ?[1-9]\d?[A-Z]?', ref)) and not re.match(r'TER', main_t.get('network', ''))
    night = 'night' in svc or bool(NIGHT.search(txt)) or any(t.get('sleeping') == 'yes' or t.get('by_night') in ('yes', 'only') for t in ts[:3])
    if HSR.search(' '.join(t.get(k, '') for t in ts[:3] for k in ('name', 'name:en', 'brand'))) and not night: return 'h'     # Haramain
    regional = sub or bool(REGIONAL.search(ref) or REGIONAL.search(txt)) or bool(svc & {'regional', 'local'}) or pas in ('regional', 'local')
    km = km_of(stops)
    if not regional:
        tagged = 'high_speed' in svc or 'highspeed' in svc or main_t.get('highspeed') == 'yes' or ts[0].get('highspeed') == 'yes'
        L = sum(map(way_len, ways)) or 1
        known = sum(way_len(w) for w in ways if speed(WAYS[w][0]) or WAYS[w][0].get('highspeed')) / L
        if not night and (HSR.search(txt) or HSR_CS.search(txt) or tagged and (fast_share(ways) >= 0.2 or known < 0.1)): return 'h'
        if km > 100 and fast_share(ways) >= 0.5 and not night: return 'h'
    if sub and (SUBURBAN.search(txt) or km < 200 and (len(stops) < 3 or np.median([metres(*stations[a][2:4], *stations[b][2:4])
                                                                                  for a, b in zip(stops, stops[1:])]) < 10000)):
        return 's'
    if svc & {'long_distance', 'night', 'international', 'tourism', 'touristic', 'regional_express'} or PRODUCT.match(ref): return 'r'
    return 's' if commuterish(stops) else 'r'
FUNI = re.compile(r'funicul|\bfunic?\b|standseilbahn|funiculaire|funicolare|füniküler|фуникул|фунікул|ケーブル|incline\b|케이블', re.I)
def kind_of(mode, ts, main_t, ways, stops, ref):
    txt = ' '.join(t.get(k, '') for t in ts[:3] for k in ('network', 'network:en', 'name', 'name:en', 'operator', 'brand', 'network:metro'))
    if FUNI.search(txt) and mode != 'subway': return 'f'
    if mode in ('light_rail', 'subway') and (re.search(r'S-Bahn|s-bahn|S-tog|\bRER\b|RRTS|RapidX|Namo Bharat|Circular Railway|SRT (?:Dark |Light )?Red', txt)
                                            or (main_t.get('passenger') or '') == 'suburban'): return 's'
    return URBAN.get(mode) or train_kind(ts, main_t, ways, stops, ref)

cand = []     # dict(kind, name, en, ref, colour, stops, branches, loop, ways, ts, net, fallback, master)
for cl in clusters:
    rep = cl[0]; routes = [x for u in cl for x in u['routes']]
    mt = rep['mt']; ts = ([mt] if mt else []) + [R[x]['t'] for x in routes] + [u['mt'] for u in cl[1:] if u['mt'] and u['mt'] is not mt]
    main_rid = rep['main']
    if mt:         # the master's own route when its name gives the line's ends
        ends = [skey(e) for e in re.split(r' – ', desc(mt.get('name', ''))) if e] if desc(mt.get('name', '')) else []
        if len(ends) >= 2:
            big = len(set(R[main_rid]['sids']))
            fit = [x for x in rep['routes'] if len(set(R[x]['sids'])) >= 0.6 * big and
                   {skey(stations[R[x]['sids'][0]][0]), skey(stations[R[x]['sids'][-1]][0])} & {ends[0], ends[-1]} == {ends[0], ends[-1]}]
            if fit: main_rid = max(fit, key=lambda x: len(set(R[x]['sids'])))
    main_t = R[main_rid]['t']
    others = sorted((x for x in routes if x != main_rid), key=lambda x: -len(set(R[x]['sids'])))
    main = R[main_rid]['sids']
    h = len(main) // 2
    if len(main) > 4 and main[0] == main[-1] and len(set(main[1:h]) & set(main[h:-1])) >= 0.8 * (h - 1):
        main = main[:h + 1]      # out and back, not a loop
    loop = len(main) > 3 and (main[0] == main[-1] or R[main_rid]['loop'] and metres(*stations[main[0]][2:4], *stations[main[-1]][2:4]) < 3000)
    if main[0] == main[-1]: main = main[:-1]
    main = list(dict.fromkeys(main))
    main, branches = branches_of(main, [list(dict.fromkeys(R[x]['sids'])) for x in others] + [b for x in routes for b in R[x].get('branches', [])])
    ref = rep['ref'] and next((good_ref(t.get('ref'), t.get('name', '')) for t in ts if canon(good_ref(t.get('ref'), t.get('name', ''))) == rep['ref']), '')
    ways = sorted({w for x in routes for w in R[x]['ways']})
    kind = kind_of(rep['mode'], ts, main_t, ways, main, ref)
    op, logo = operator_of(ts, kind in 'mltf')
    cand.append({'kind': kind, 'ref': ref, 'ts': ts, 'mt': mt, 'main_t': main_t, 'mode': rep['mode'], 'stops': main, 'branches': branches,
                 'masters': [u['mt'] for u in cl[1:] if u['mt'] and u['mt'] is not mt], 'rids': set(routes) | {u['id'] for u in cl},
                 'loop': int(loop), 'ways': ways, 'op': op, 'logo': logo, 'master': bool(mt) or rep['kind'] == 'a', 'anchor': rep['kind'] == 'a',
                 'fallback': False, 'night': rep['night'], 'fam': set().union(*(u['fam'] for u in cl)), 'exp': all(R[x]['exp'] for x in routes),
                 'colour': next((colour(t.get('colour')) for t in ts if colour(t.get('colour'))), ''),
                 'net': first(next((t.get('network') or t.get('operator') for t in ts if t.get('network') or t.get('operator')), ''))})
for f in FB:
    t = f['t']; ways = f['ways']; ref = good_ref(t.get('ref'), t.get('name', ''))
    kind = 'h' if fast_share(ways) >= 0.5 else 's' if commuterish(f['stops']) else 'r'
    op, logo = operator_of([t], False)
    cand.append({'kind': kind, 'ref': ref, 'ts': [t], 'mt': None, 'main_t': t, 'mode': 'train', 'stops': f['stops'], 'branches': f['branches'], 'masters': [], 'rids': {f['rid']}, 'exp': False,
                 'loop': 0, 'ways': ways, 'op': op, 'logo': logo, 'master': False, 'anchor': False, 'fallback': True, 'night': False,
                 'fam': families([t]), 'colour': colour(t.get('colour')), 'net': first(t.get('network') or t.get('operator'))})
log('candidate lines', len(cand), Counter(c['kind'] for c in cand))

# ---------------------------------------------------------------- countries
def allst(c): return set(c['stops']) | {s for b in c['branches'] for s in b}
def line_cc(c):
    cnt = Counter(stations[s][8] for s in c['stops'] + [x for b in c['branches'] for x in b]).most_common()
    if len(cnt) > 1 and cnt[0][1] == cnt[1][1]:       # a tie: the country of the middle station
        mid = stations[c['stops'][len(c['stops']) // 2]][8]
        if mid in (cnt[0][0], cnt[1][0]): return mid
    return cnt[0][0] if cnt else ''
for c in cand: c['cc'] = line_cc(c)
for c in cand:
    if c['rids'] & DBG: print('  DBG candidate', c['kind'], c['cc'], c['ts'][0].get('name'), len(c['stops']), [stations[s][0] for s in c['stops']][:12])
cand = [c for c in cand if c['cc'] not in EXCLUDE and len(c['stops']) >= 2]
# one kind per commuter network: most of a network's trains suburban -> its other regional trains too (SEPTA, RTD, FGC)
votes = defaultdict(list)
for i, c in enumerate(cand):
    if c['mode'] == 'train' and c['kind'] in 'rs' and not c['fallback'] and c['net'] and not NOT_OP.search(c['net']): votes[(c['cc'], c['net'])].append(i)
for ids in votes.values():
    if len(ids) >= 3 and sum(cand[i]['kind'] == 's' for i in ids) >= 0.6 * len(ids):
        for i in ids:
            c = cand[i]
            if c['kind'] == 'r' and km_of(c['stops']) <= 150 and not PRODUCT.match(c['ref']) and not any(EXPRESS_CS.search(t.get('ref') or '') for t in c['ts'][:2]):
                c['kind'] = 's'
log('outside China', len(cand), 'countries', len({c['cc'] for c in cand}))
# operators in English and their logos (tools/world/english.py line_operator()): the first of the operator build chose,
# the other operator / network / brand tags of the line and its route masters, the operator its stations' nodes name,
# whose Wikidata item (tagged, curated in ref/operators.json, or of that name in the line's country) has a logo (Commons,
# its parent company's, its English or native Wikipedia infobox's); a domestic train naming none: the national operator.
# Then a line still without one takes the logo 75%+ of the others of its network (or city and kind, below) show.
nlogo = Counter()
STN_OPS, MASTER_OF = defaultdict(set), {}          # station record -> operators its nodes name; route -> its master's tags
for nid, j in st_index.items():
    if first(NODES[nid][2].get('operator')): STN_OPS[j].add(first(NODES[nid][2].get('operator')))
for mid, m in RELS.items():
    if m['tags'].get('type') == 'route_master': MASTER_OF.update({x: m['tags'] for typ, x, role in m['members'] if typ == 'r'})
def fill_logos(groups, why):
    """Lines of a group without a logo take the one 75%+ (2+) of the group's lines with a logo show."""
    for ids in groups.values():
        cnt = Counter((cand[i]['op'], cand[i]['logo']) for i in ids if cand[i]['logo'])
        if cnt and cnt.most_common(1)[0][1] >= max(2, 0.75 * sum(cnt.values())):
            for i in ids:
                if not cand[i]['logo']: cand[i]['op'], cand[i]['logo'] = cnt.most_common(1)[0][0]; nlogo[why] += 1
for c in cand:
    dom = c['kind'] in 'hrs' and c['mode'] == 'train' and len({stations[s][8] for s in allst(c)}) == 1
    stn = Counter(v for s in allst(c) for v in STN_OPS.get(s, ()))
    ts = c['ts'] + [MASTER_OF[x] for x in sorted(x for x in c['rids'] if x in MASTER_OF)]
    name, url, q, wiki = line_operator(c['op'], c['logo'][3:], ts, c['cc'], c['kind'], dom, stn)
    c['op'], c['logo'] = name, 'wd:' + q if q and (url or wiki) else ''
    nlogo['url' if url else 'wiki' if wiki and c['logo'] else 'item without logo' if q else 'no item'] += 1
_g = defaultdict(list)
for i, c in enumerate(cand):
    if c['net'] and not NOT_OPERATOR.search(c['net']): _g[(c['cc'], c['net'], c['kind'] in 'mltf')].append(i)
fill_logos(_g, 'network')
# a train that names no operator at all: the logo of the trains of its country at its stations (intercity for intercity,
# suburban or regional for suburban) when lines with it stop at 40%+ (2+) of its stations and it is 75%+ of what they
# show; a "JR…" line: the JR company of the JR lines at its stations
JR_OP = re.compile(r'Japan Railway|Hokkaido Railway|Shikoku Railway|Kyushu Railway|^JR ')
_at = defaultdict(set)
for i, c in enumerate(cand):
    for s in allst(c): _at[s].add(i)
for i, c in enumerate(cand):
    if c['logo'] or c['kind'] not in 'hrs' or line_values(c['ts'], False): continue
    stn = Counter(v for s in allst(c) for v in STN_OPS.get(s, ()))
    net = infra_network(c['cc'], c['kind'], stn, c['ts'])     # a regional train on the national network (RFI's, Adif's stations)
    jr, sts, cov = c['cc'] == 'JP' and first(c['ts'][0].get('name')).startswith('JR') or bool(net), allst(c), defaultdict(set)
    for s in sts:
        for j in _at[s] - {i}:
            o = cand[j]
            if o['logo'] and o['kind'] in ('sr' if c['kind'] == 's' else 'hr') and o['cc'] == c['cc'] and (not jr or JR_OP.search(o['op'])):
                cov[(o['op'], o['logo'])].add(s)
    if cov:
        k, best = max(cov.items(), key=lambda kv: len(kv[1]))
        if len(best) >= 0.75 * sum(map(len, cov.values())) and (jr or len(best) >= max(2, 0.4 * len(sts))):
            c['op'], c['logo'] = k; nlogo['stations'] += 1; continue
    if net and c['mode'] == 'train':
        name, url, q, wiki = english_operator('', net, c['cc'])
        if url: c['op'], c['logo'] = name, 'wd:' + q; nlogo['national network'] += 1
log('operator logos', dict(nlogo))

# ---------------------------------------------------------------- duplicates
# A line whose stations (nearly) all lie on another line of the same group (intercity + suburban, or one urban kind) is
# dropped: the same stop set; 90% of it within a line of the same operator and ref; a night or event variant of a day
# line; a fallback line on service lines (80%); a fragment of 2-3 stops without master or ref along a stretch of a line.
def code(ref): return canon(ref)
def group(k): return 'i' if k in 'hrs' else k
# a fallback line on a service line's track (80% of its length) that stops at 60% of its stations there: the stations the
# service misses go into its stop list (between two of its stops, not past its ends; not into an express where services
# are well mapped), and it is dropped unless 3+ of its stations are still on no line (Sitarail without Agboville)
by_way, gone, nmerge = defaultdict(set), set(), Counter()
for j, m in enumerate(cand):
    if not m['fallback'] and m['kind'] in 'hrs':
        for w in m['ways']: by_way[w].add(j)
for i, c in enumerate(cand):
    if not c['fallback']: continue
    share, tot = Counter(), sum(map(way_len, c['ways'])) or 1
    for w in c['ways']:
        for j in by_way.get(w, ()): share[j] += way_len(w)
    ok = sorted((j for j, l in share.items() if l >= 0.8 * tot), key=lambda j: (cand[j]['exp'], -share[j]))
    if not ok or (cand[ok[0]]['exp'] or any(EXPRESS.search(t.get('name', '')) for t in cand[ok[0]]['ts'][:3])) and MAPPED[cand[ok[0]]['cc']] >= 30: continue
    m = cand[ok[0]]; st = m['stops']; have = allst(m); left = 0
    d = dict(zip(c['stops'], NearIdx(samples(sorted(set(m['ways']) & set(c['ways'])))[0], 0.005).near([stations[s][2:4] for s in c['stops']])[1].tolist()))
    on = [s for s in c['stops'] if d[s] <= 300]
    if sum(s in have for s in on) < 0.6 * len(on): continue       # the service stops at few of them: a fast train, not their service
    extra = [s for s in c['stops'] if s not in have]
    for s in extra:
        P = stations[s][2:4]
        if d[s] > 300 or len(st) < 2: left += 1; continue
        k = min(range(len(st) - 1), key=lambda k: metres(*stations[st[k]][2:4], *P) + metres(*P, *stations[st[k + 1]][2:4]) - metres(*stations[st[k]][2:4], *stations[st[k + 1]][2:4]))
        a, b = stations[st[k]][2:4], stations[st[k + 1]][2:4]
        if metres(*a, *P) + metres(*P, *b) - metres(*a, *b) <= max(2000, 0.3 * metres(*a, *b)): st.insert(k + 1, s); nmerge['stations'] += 1
        else: left += 1
    if left < 3: gone.add(i); nmerge['lines'] += 1
    if VERBOSE: print('  fallback merged:', len(extra) - left, 'stations of', c['ts'][0].get('name'), 'into', m['ts'][0].get('name'), m['cc'], '| left', left)
cand = [c for i, c in enumerate(cand) if i not in gone]
# a station of a railway line that no service stops at (Arboga, Skövde) goes into the stopping services that call at the
# served stations on either side of it one after the other
nxt = defaultdict(list)
for c in cand:
    if c['mode'] == 'train' and c['kind'] in 'rs' and not c['exp'] and not c['fallback']:
        for q in [c['stops']] + c['branches']:
            for k in range(len(q) - 1): nxt[frozenset(q[k:k + 2])].append(q)
for a, xs, b in ORPHAN:
    for q in nxt.get(frozenset((a, b)), ()):
        k = next((k for k in range(len(q) - 1) if {q[k], q[k + 1]} == {a, b}), None)
        if k is not None and xs and not set(xs) & set(q):
            q[k + 1:k + 1] = xs if q[k] == a else xs[::-1]; nmerge['stations between'] += len(xs)
log('fallback lines merged into services', dict(nmerge))
for c in cand: c['all'] = allst(c)
order = sorted(range(len(cand)), key=lambda i: (cand[i]['fallback'], cand[i]['night'], -cand[i]['master'], -bool(cand[i]['ref']),
                                                  -bool(cand[i]['colour']), -len(cand[i]['all']), i))
kept, at, ndup = [], defaultdict(list), Counter()
for i in order:
    c = cand[i]; si = c['all']; dup = None
    for j in {j for s in si for j in at[s]}:
        m = cand[j]
        if group(m['kind']) != group(c['kind']): continue
        sj = m['all']; cont = len(si & sj) / len(si)
        fam = not c['fam'] or not m['fam'] or bool(c['fam'] & m['fam'])
        refs = not (code(c['ref']) and code(m['ref']) and code(c['ref']) != code(m['ref']))
        if c['fallback'] and cont >= 0.8: dup = 'fallback on a service line'
        elif c['night'] and not m['night'] and fam and cont >= 0.9: dup = 'night variant'
        elif len(si) <= 3 and cont == 1 and not c['master'] and not c['ref'] and any(
                max(q.index(x) for x in si) - min(q.index(x) for x in si) <= len(si) for q in [m['stops']] + m['branches'] if si <= set(q)):
            dup = 'fragment'
        elif refs and fam and (c['kind'] == 'h') == (m['kind'] == 'h') and (si == sj or cont >= 0.9 and not (c['master'] and m['master'] and c['ref'] and m['ref'])): dup = 'duplicate'
        if dup: break
    if not dup and c['night'] and c['kind'] in 'mltf' and si <= {x for j in {j for s in si for j in at[s]} if not cand[j]['night'] and
                                                                 group(cand[j]['kind']) == group(c['kind']) for x in cand[j]['all']}:
        dup, m = 'night line on day lines', c
    if c['rids'] & DBG: print('  DBG dedupe', c['kind'], c['cc'], c['ts'][0].get('name'), len(si), dup, m['ts'][0].get('name') if dup else '')
    if dup:
        ndup[dup] += 1
        if VERBOSE: print('  duplicate (%s):' % dup, c['kind'], c['ts'][0].get('name'), c['ref'], '| of', m['ts'][0].get('name'), m['ref'])
        continue
    kept.append(i)
    for s in si: at[s].append(i)
cand = [cand[i] for i in sorted(kept)]
log('after duplicates', len(cand), dict(ndup), Counter(c['kind'] for c in cand))

# ---------------------------------------------------------------- stop order and direction
def untangle(seq, k=1.3):
    """A stop list that zigzags (listed out of order: no track to order it by, or one the track did not explain; its path
    over k times its extent) -> the shortest path through its stops."""
    if len(seq) < 4 or len(seq) > 300: return seq
    X = np.array([stations[s][2:4] for s in seq]); D = dmatrix(X)
    plen = D[np.arange(len(seq) - 1), np.arange(1, len(seq))].sum()
    if plen <= k * D.max(): return seq
    o = path_order(X)
    if D[o[:-1], o[1:]].sum() > 0.85 * plen: return seq
    return [seq[i] for i in (o if D[o[0], 0] <= D[o[-1], 0] else o[::-1])]
def dmatrix(X):
    P = np.column_stack([X[:, 0] * math.cos(math.radians(float(X[:, 1].mean()))), X[:, 1]]) * 111195
    return np.hypot(*(P[:, None, :] - P[None, :, :]).transpose(2, 0, 1))
def path_order(X):
    """Order points (lon, lat) along a corridor: start at the one farthest from the centre, walk to the nearest
    unvisited one, then untangle with 2-opt (a shortest open path). Returns indices."""
    if len(X) < 3: return list(range(len(X)))
    D = dmatrix(X)
    start = int(np.argmax(np.hypot(*(X - X.mean(0)).T)))
    order, left = [start], set(range(len(X))) - {start}
    while left:
        u = order[-1]; v = min(left, key=lambda j: D[u, j]); order.append(v); left.discard(v)
    n = len(X); D = np.pad(D, (0, 1)); o = np.array(order + [n])    # a free end: node n, 0 m from everything
    for _ in range(50):
        improved = False
        for a in range(n - 2):
            b = np.arange(a + 2, n)
            gain = D[o[a], o[a + 1]] + D[o[b], o[b + 1]] - D[o[a], o[b]] - D[o[a + 1], o[b + 1]]
            k = int(np.argmax(gain))
            if gain[k] > 1: o[a + 1:b[k] + 1] = o[a + 1:b[k] + 1][::-1].copy(); improved = True
        if not improved: break
    return o[:n].tolist()
for c in cand:
    if c['loop'] and len(c['stops']) >= 4:      # a loop's stops go round once (~pi x its diameter), not back and forth
        D = dmatrix(np.array([stations[s][2:4] for s in c['stops']]))
        c['loop'] = int(D[np.arange(len(D)), np.roll(np.arange(len(D)), -1)].sum() <= 4 * D.max())
    if not c['loop']: c['stops'] = untangle(c['stops'], 1.3 if not c['ways'] else 2)
def fit(s, x):
    """How well station record s matches a place name x from a line name or from= / to= tag."""
    kx = skey(stn_name({'name': x})) if x else ''
    if len(kx) < 2: return 0
    ks = {skey(stations[s][0]), skey(stations[s][1]), skey(rec_mk[s])} - {''}
    if kx in ks: return 3
    if any(len(k) >= 4 and len(kx) >= 4 and (kx in k or k in kx) for k in ks): return 2
    return 1 if any(k[:4] == kx[:4] for k in ks if len(kx) >= 4) else 0
def ends_of(c):
    """(start, end) name pairs the line's name and tags give: "A – B" descriptions (native and English), from= / to=; the
    tag set the line's name comes from, else the main route's (the stop list reads in the name's direction)."""
    for t in (c['nt'], c['main_t']):
        out = []
        for k in ('name', 'name:en'):
            p = desc(t.get(k)).split(' – ') if desc(t.get(k)) else []
            if len(p) >= 2: out += [(p[0], p[-1])] * 2
        for sfx in ('', ':en'):
            if t.get('from' + sfx) and t.get('to' + sfx): out.append((t['from' + sfx], t['to' + sfx]))
        if out: return out
    return []
_ol = defaultdict(set)         # operator / network values that several lines carry: an operator's, not a line's name
for i, c in enumerate(cand):
    for t in c['ts']:
        for k in OPKEYS:
            if t.get(k): _ol[first(t.get(k)).lower()].add(i)
OPLIKE.update(v for v, x in _ol.items() if len(x) >= 3 and not LINE_REL.search(v))
for c in cand:      # the tag set that names the line: the first with a name that is not only a category, number or operator
    tss = ([c['mt']] if c['mt'] else []) + ([c['main_t']] if c['anchor'] else []) + c['masters'] + [c['main_t']] + c['ts']
    c['nt'] = c['mt'] if c['mt'] and c['mt'].get('_system') else \
        next((t for t in tss if clean_name(t.get('name')) and not generic(strip_no(clean_name(t.get('name'))), c['ts'])), c['main_t'])
nrev = 0
for c in cand:
    st = c['stops']; fw = rv = 0
    for a, b in ends_of(c):
        fw += fit(st[0], a) + fit(st[-1], b); rv += fit(st[0], b) + fit(st[-1], a)
    if rv > fw: c['stops'] = st[::-1]; nrev += 1
log('reversed to their names', nrev)

# ---------------------------------------------------------------- stations: one English spelling per name within 3 km ("Tōkyō" / "Tokyo")
by_name = defaultdict(list)
for i, s in enumerate(stations): by_name[skey(s[0])].append(i)
for name, ids in by_name.items():
    if len(ids) < 2 or not name: continue
    for i in ids:
        grp = [j for j in ids if abs(stations[j][2] - stations[i][2]) < 0.03 and abs(stations[j][3] - stations[i][3]) < 0.03]
        ens = [stations[j][1] for j in grp if stations[j][1] and stations[j][1] != stations[j][0] or latin(stations[j][0])]
        if len(set(ens)) > 1:
            best = max(set(ens), key=lambda e: (ens.count(e), e.isascii(), -len(e)))
            for j in grp: stations[j][1] = best

# ---------------------------------------------------------------- names
MODE_WORD = {'t': 'Tram', 'f': 'Funicular', 'm': 'Line', 'l': 'Line'}
SEN = defaultdict(Counter)          # native station name -> its English names (for line names built from place names)
for st in stations:
    if st[0] and latin(st[1]) and not latin(st[0]): SEN[st[0]][st[1]] += 1
CAT_WORDS = {'трамвай': 'Tram', 'тролейбус': 'Trolleybus', 'троллейбус': 'Trolleybus', 'метро': 'Metro', 'лінія': 'Line', 'линия': 'Line',
             'фунікулер': 'Funicular', 'фуникулёр': 'Funicular', 'фуникулер': 'Funicular', 'маршрут': 'Route', 'електричка': 'Commuter train',
             'электричка': 'Commuter train', 'поїзд': 'Train', 'поезд': 'Train', 'tramvaj': 'Tram', 'tramvay': 'Tram', 'τρένο': 'Train'}
CAT_EN = re.compile(r'(?i)(?<![\w-])(?:' + '|'.join(sorted(map(re.escape, CAT_WORDS), key=len, reverse=True)) + r')(?![\w-])')
def en_name(c, n):
    """English form of a non-Latin line name: its stations' English names ("בית שמש - נתניה" -> "Beit Shemesh – Netanya",
    not for Chinese / Japanese, whose line names reuse place names read differently), the rest transliterated."""
    if latin(n): return n
    if not (HAN.search(n) or KANA.search(n)):
        for a, b in sorted({(stations[s][0], stations[s][1]) for s in c['all'] if latin(stations[s][1]) and not latin(stations[s][0])},
                           key=lambda p: -len(p[0])):
            n = re.sub(r'(?<![\w-])' + re.escape(a) + r'(?![\w-])', ' ' + b + ' ', n)
    n = re.sub(r'\s+', ' ', CAT_EN.sub(lambda m: ' ' + CAT_WORDS[m.group(0).lower()] + ' ', n).replace('№', ' ')).strip()     # "Tram 26"
    n = ' – '.join(SEN[p].most_common(1)[0][0] if p in SEN else p for p in re.split(r'\s+[-–]\s+', n))
    out = english_line(desc(n) if ' - ' in n else n, {}, c['cc'])
    return re.sub(r'\s+', ' ', out).strip() if out and latin(out) else ''
KMPOST = re.compile(r'(?i)^(?:\w{1,4}\.\s*)?\d+\s*(?:км|km)\b')        # "340 км", "Пл. 59 км": a halt named by its distance
def termini(c):
    st = c['stops']       # the line's end stations, a km-post end as the first named station inside it
    i = next((i for i in range(len(st)) if not KMPOST.match(stations[st[i]][0])), 0)
    j = next((j for j in range(len(st) - 1, -1, -1) if not KMPOST.match(stations[st[j]][0])), len(st) - 1)
    a, b = (stations[st[i]], stations[st[j]]) if i < j else (stations[st[0]], stations[st[-1]])
    if c['loop']:
        b = stations[max(c['stops'], key=lambda s: metres(*a[2:4], *stations[s][2:4]))]
        return f'{a[0]} – {b[0]} – {a[0]}', f'{a[1] or a[0]} – {b[1] or b[0]} – {a[1] or a[0]}'
    for x, y in ends_of(c)[:1]:      # the ends its relation names when the line has them (Lyria "Nice - Genève", not "Nice - Tenay")
        pos = {s: i for i, s in enumerate(st)}; mid = len(st) / 2       # ties: the station nearest that end of the list
        sx = max(sorted(c['all']), key=lambda s: (fit(s, x), -pos.get(s, mid)))
        sy = max(sorted(c['all']), key=lambda s: (fit(s, y), pos.get(s, mid)))
        if sx != sy and fit(sx, x) >= 2 and fit(sy, y) >= 2: a, b = stations[sx], stations[sy]
    return f'{a[0]} – {b[0]}', f'{a[1] or a[0]} – {b[1] or b[0]}'
# a branch named after the junction it leaves ("Ratangarh – Sardarshahr railway") whose own track stops short of it: the
# junction's station sits on the main line's track, so no stop was taken from it. The named station within 25 km of the
# nearer end becomes that end (the tile step routes the gap along the rail network).
_SK = defaultdict(list)
for i, st in enumerate(stations):
    for k in {skey(st[0]), skey(st[1])} - {''}: _SK[k].append(i)
END_WORDS = r'(?i)\s*(?:\(.*?\)|railway line|rail line|railway|railroad|section|branch line|branch|line|new line|rail route|रेल मार्ग.*)\s*$'
_HUB = Counter(s for c in cand for s in allst(c))       # lines at each station
def reach_named_ends(c):
    if c['kind'] not in 'hrs' or c['loop'] or len(c['stops']) < 2: return 0
    names = ' '.join(t.get(k, '') for t in (c['nt'], c['main_t']) for k in ('name', 'name:en'))
    # a railway line; or a service ("Napoli – Cassino") whose end stations are small (a named train ends at its city's
    # main station, which need not carry the city's name: Howrah – Mumbai Mail at CSMT)
    line = c['fallback'] or re.search(r'(?i)railway|rail line|\bline\b|branch|section|lijn|linie|ligne|línea|linea|linia', names)
    added = 0
    ends = []
    for t in (c['nt'], c['main_t']):
        for k in ('name:en', 'name'):
            n = re.sub(END_WORDS, '', t.get(k) or '').strip(' -–')
            p = [q.strip() for q in re.split(r'\s*[–—]\s*|\s+-\s+|(?<=[a-z])-(?=[A-Z])', n) if q.strip()]
            if len(p) >= 2: ends += [p[0], p[-1]]
    for x0 in dict.fromkeys(ends):
        for x in [x0] + [a.strip() for a in x0.split('/') if a.strip() and '/' in x0]:
            if any(fit(s, x) >= 2 for s in c['all']): break
        else:
          for x in [x0] + [a.strip() for a in x0.split('/') if a.strip() and '/' in x0]:
            k = skey(stn_name({'name': x}))
            if len(k) < 3: continue
            cands = {s for sfx in ('', 'junction', 'jn', 'jct', 'jnc', 'city', 'cantt') for s in _SK.get(k + sfx, ())}
            tips = (c['stops'][0], c['stops'][-1])
            best = min(((min(metres(*stations[e][2:4], *stations[s][2:4]) for e in tips), s) for s in cands), default=None)
            if not best or best[0] > (25000 if line else 12000): continue
            if not line:
                tip = min(tips, key=lambda e: metres(*stations[e][2:4], *stations[best[1]][2:4]))
                if _HUB[tip] > 3 or c['mode'] != 'train': continue
            s = best[1]
            if s in c['all']: continue
            if metres(*stations[tips[0]][2:4], *stations[s][2:4]) < metres(*stations[tips[1]][2:4], *stations[s][2:4]): c['stops'] = [s] + c['stops']
            else: c['stops'] = c['stops'] + [s]
            c['all'] = allst(c); added += 1
            break
    return added
log('named junction ends added', sum(reach_named_ends(c) for c in cand))
def name_ends(c):
    """(start, end) of a railway line's name without spaces round its dashes ("Delhi–Shamli–Saharanpur line"), which
    ends_of() does not split."""
    out = []
    for t in (c['nt'], c['main_t']):
        for k in ('name:en', 'name'):
            n = re.sub(END_WORDS, '', t.get(k) or '').strip(' -–')
            p = [q.strip() for q in re.split(r'\s*[–—]\s*|\s+-\s+|(?<=[a-z])-(?=[A-Z])', n) if q.strip()]
            if len(p) >= 2: out.append((p[0], p[-1]))
        if out: return out
    return []
nrev = 0
for c in cand:          # the stop list in its name's direction again (junction ends added; names ends_of() missed)
    if c['loop'] or c['kind'] not in 'hrs' or len(c['stops']) < 2: continue
    st = c['stops']; fw = rv = 0
    for a, b in ends_of(c) or name_ends(c):
        fw += fit(st[0], a) + fit(st[-1], b); rv += fit(st[0], b) + fit(st[-1], a)
    if rv > fw: c['stops'] = st[::-1]; nrev += 1
log('reversed to their names after junction ends', nrev)
NETN, OPB = Counter((c['cc'], c['net']) for c in cand), OPLIKE | broad()
PATTERN = re.compile(r'(?i)(?:local|limited|express|baby bullet|bullet|rapid|skip[- ]stop|all[- ]stops|weekend|weekday|peak|off-peak|regular)'
                     r'(?:[ /-](?:local|limited|express|weekend|weekday|service|train))*')
for c in cand:
    ts = c['ts']; tss = ([c['mt']] if c['mt'] else []) + c['masters'] + [c['main_t']] + ts; nt = c['nt']
    raw = nt.get('name') or next((t.get('name') for t in tss if clean_name(t.get('name'))), '') or ''
    name = clean_name(raw); w = name.split(' ')
    k = next((k for k in (3, 2, 1) if len(w) > k + 1 and ' '.join(w[:k]).lower() in OPB), 0)
    if k and c['mode'] == 'train' and (PRODUCT.match(' '.join(w[k:])) or REGIONAL.match(' '.join(w[k:]))):
        name = ' '.join(w[k:])        # an operator before a product and number: "SNCF Voyageurs TER 26 Ussel - Limoges"
    ens = [clean_name(t.get(k)) for t in [nt] + tss[:3] for k in ('name:en', 'int_name') if latin(t.get(k))]
    en = next((e for e in ens if e and not generic(strip_no(e), ts)), '')
    if c['ref'] and name.startswith(c['ref'] + ' ') and re.search(r' [-–] |\w[–—]\w', name): name = c['ref']      # "IC-16 Bruxelles - Namur - Luxembourg"
    if re.fullmatch(r'[^:()]+?(?:\s[-–]\s[^:()]+?)+', name) and not re.search(r'\d', name): name = desc(name)     # "Paddington - Greenford"
    stem = strip_no(name)
    c['desc'], c['desc_en'] = termini(c)
    rest = re.sub(r'(?<![\w])' + re.escape(c['ref']) + r'(?![\w])', '', name).strip(' -–:№#') if c['ref'] else name
    if c['kind'] in 'mltf' and c['ref'] and (not rest or re.fullmatch(r'(?:' + CATEGORY + r')(?:\s+(?:' + CATEGORY + r'))*', rest, re.I)):
        # an urban line named by its number: "Tram 9", "Linie 2", "U2", "F Train" stay; a bare number becomes "Line 3"
        if not rest and not re.search(r'[^\W\d]', c['ref']): name = f"{MODE_WORD[c['kind']]} {c['ref']}"
        elif not rest: name = c['ref']
        ref = c['ref'] if latin(c['ref']) else latinize(c['ref'], c['cc']).upper()
        if not en or generic(strip_no(en), ts) and not latin(name): en = name if latin(name) else f"{MODE_WORD[c['kind']]} {ref}"
    elif (generic(stem, ts) or not name) and not nt.get('_system'):
        brand = next((first(t.get('brand')) for t in [nt, c['main_t']] + tss[:1] if PRODUCT_NAME.fullmatch(first(t.get('brand')))), '')
        prod = stem if PRODUCT_NAME.fullmatch(stem) else brand
        if re.fullmatch(r'(?i)IC|ICN|EC|EN|TER|Regio|REX|Intercity|InterCity', prod): prod = ''
        if prod: name, en = f"{prod}: {c['desc']}", f"{latinize(prod, c['cc'])}: {c['desc_en']}"
        else: name, en = c['desc'], c['desc_en']
    elif stem != name and stem:        # "Bolan Mail 4DN" -> "Bolan Mail"
        name = stem; en = strip_no(en) if en else en
    if not en: en = name if latin(name) else en_name(c, name) or (c['desc_en'] if latin(c['desc_en']) else name)
    if c['kind'] == 's' and PATTERN.fullmatch(name) and SUBURBAN.fullmatch(c['net']) and NETN[(c['cc'], c['net'])] <= 3:
        name = en = c['net']            # a commuter network of one line, named after a service pattern: "Local Weekend" -> "Caltrain"
    c['name'], c['en'] = name, en
# the same name for different lines in one country / city ("DLR", "Southern", "London Trams") -> add the termini
def operatorish(c):       # "GWR", "Southern", "c2c": the name is only the operator's
    n = c['name']
    return bool(re.fullmatch(r'[A-Za-z0-9]{2,5}', n) and not re.search(r'\d', n) and n.upper() == n) or n.lower() in OPLIKE

# ---------------------------------------------------------------- cities for urban lines
PL = [(nid, lon, lat, name or en, en, popn(pop)) for nid, (lon, lat, kind, name, en, pop) in PLACES.items() if name or en]
PLX = np.array([(p[1], p[2]) for p in PL]) if PL else np.zeros((0, 2))
PLP = np.array([max(p[5], 20000) for p in PL], float)
PGRID = Grid(((i, p[1], p[2]) for i, p in enumerate(PL)), 0.5)
_pcc, _mpop = {}, {}
def pcc(i):
    if i not in _pcc: _pcc[i] = country_code(PL[i][1], PL[i][2])
    return _pcc[i]
def mpop(i):          # metropolitan population: the place and the places within 20 km
    if i not in _mpop: _mpop[i] = PLP[i] + sum(PLP[j] for d, j in PGRID.near(PL[i][1], PL[i][2], 20000) if j != i and PLP[j] < PLP[i])
    return _mpop[i]
def radius(i): return 1000 * min(10 + 15 * math.sqrt(mpop(i) / 1e6), 60)       # metropolitan area
# a place near a much bigger one is part of it (Tokyo's wards, Kawasaki and Yokohama; London's boroughs)
owner = list(range(len(PL)))
for j in np.argsort(-PLP):
    if PLP[j] < 300000: break
    d = np.hypot((PLX[:, 0] - PLX[j, 0]) * math.cos(math.radians(PLX[j, 1])), PLX[:, 1] - PLX[j, 1]) * 111.195
    for i in np.nonzero((d < 8 * math.sqrt(mpop(int(j)) / 1e6)) & (PLP * 3 <= PLP[j]))[0]:
        if owner[i] == i and pcc(int(i)) == pcc(int(j)): owner[i] = owner[int(j)]
ALIAS = {'都営': '東京', 'Toei': 'Tokyo', 'MTA': 'New York', 'MBTA': 'Boston', 'SEPTA': 'Philadelphia', 'WMATA': 'Washington',
         'CTA': 'Chicago', 'BART': 'San Francisco', 'MARTA': 'Atlanta', 'TTC': 'Toronto', 'STM': 'Montréal', 'RATP': 'Paris',
         'BVG': 'Berlin', 'MVG': 'München', 'HVV': 'Hamburg', 'Hochbahn': 'Hamburg', 'Wiener Linien': 'Wien', 'ZVV': 'Zürich',
         'GVB': 'Amsterdam', 'RET': 'Rotterdam', 'HTM': 'Den Haag', 'STIB': 'Bruxelles', 'MIVB': 'Bruxelles', 'TMB': 'Barcelona',
         'FGC': 'Barcelona', 'ATAC': 'Roma', 'SMRT': 'Singapore', 'SBS Transit': 'Singapore', 'BTS': 'Bangkok', 'MRTA': 'Bangkok',
         'TfL': 'London', 'Transport for London': 'London', 'DLR': 'London', 'PATH': 'New York', 'LIRR': 'New York',
         'Metro-North': 'New York', 'NJ Transit': 'New York', 'Metra': 'Chicago', 'Caltrain': 'San Francisco', 'Metrolink': 'Los Angeles',
         'TNW': 'Basel', 'BVB': 'Basel', 'BLT': 'Basel', 'TPG': 'Genève', 'Unireso': 'Genève', 'VBZ': 'Zürich', 'Bernmobil': 'Bern',
         'Rapid KL': 'Kuala Lumpur', 'RapidKL': 'Kuala Lumpur', 'Prasarana': 'Kuala Lumpur', 'Klang Valley': 'Kuala Lumpur',
         'HRT': 'Norfolk', 'Hampton Roads Transit': 'Norfolk', 'DART': 'Dallas', 'Trinity Railway Express': 'Dallas',
         'McKinney Avenue Transit Authority': 'Dallas', 'Tri-Rail': 'Miami', 'MARC': 'Washington', 'VRE': 'Washington',
         'Virginia Railway Express': 'Washington', 'Tren Urbano': 'San Juan', 'SITEUR': 'Guadalajara', 'Mi Tren': 'Guadalajara',
         'GZM': 'Katowice', 'Koleje Śląskie': 'Katowice', 'IDS JMK': 'Brno', 'Jihomoravsk': 'Brno', 'ODIS': 'Ostrava',
         'Metro Manila': 'Manila', 'LRTA': 'Manila', 'Jabodetabek': 'Jakarta', 'Jabodebek': 'Jakarta', 'Movia': 'København',
         'DSB S-tog': 'København', 'S-tog': 'København', 'Hovedstadens Letbane': 'København', 'Ruter': 'Oslo', 'HSL': 'Helsinki',
         'Skyss': 'Bergen', 'Västtrafik': 'Göteborg', 'Skånetrafiken': 'Malmö', 'SL': 'Stockholm', 'Sound Transit': 'Seattle',
         'TriMet': 'Portland', 'RTD': 'Denver', 'UTA': 'Salt Lake City', 'VTA': 'San Jose', 'SacRT': 'Sacramento', 'MTS': 'San Diego',
         'NCTD': 'San Diego', 'Metro Transit': 'Minneapolis', 'Valley Metro': 'Phoenix', 'CATS': 'Charlotte', 'METRORail': 'Houston',
         'GO Transit': 'Toronto', 'Metrolinx': 'Toronto', 'exo': 'Montréal', 'TransLink': 'Vancouver', 'CPTM': 'São Paulo',
         'SuperVia': 'Rio de Janeiro', 'Trenes Argentinos': 'Buenos Aires', 'EFE': 'Santiago', 'Metrorrey': 'Monterrey',
         'KAI Commuter': 'Jakarta', 'KRL': 'Jakarta', 'Keretapi Tanah Melayu': 'Kuala Lumpur', 'ЦППК': 'Москва', 'МЦД': 'Москва',
         'Московский метрополитен': 'Москва', 'СЗППК': 'Санкт-Петербург', 'IZBAN': 'İzmir', 'Metro Istanbul': 'İstanbul',
         'Marmaray': 'İstanbul', 'Başkentray': 'Ankara', 'Gaziray': 'Gaziantep', 'Metrovalencia': 'València', 'FGV': 'València',
         'Cercanías Madrid': 'Madrid', 'Rodalies': 'Barcelona', 'Metro de Madrid': 'Madrid', 'Transilien': 'Paris', 'RER': 'Paris',
         'S-Bahn Berlin': 'Berlin', 'S-Bahn Hamburg': 'Hamburg', 'S-Bahn München': 'München', 'Rhein-Main': 'Frankfurt am Main',
         'Mumbai Suburban': 'Mumbai', 'Kolkata Suburban': 'Kolkata', 'Chennai Suburban': 'Chennai', 'MMTS': 'Hyderabad'}
CJK_CITY = r'(?:特別市|特别市|廣域市|광역시|특별시|특별자치시|都|府|市|区|區|시|县|縣)$'
def place_names(p):
    n = p[3] or ''
    out = {n, p[4] or ''}
    if (HAN.search(n) or HANGUL.search(n) or KANA.search(n)) and len(n) >= 3: out.add(re.sub(CJK_CITY, '', n))
    return {x for x in out if len(x) >= 2}
def city_of(c):
    """Urban and suburban lines: the place its network / operator names (whole words, aliases for acronyms: "BVG" ->
    Berlin; fare associations and national networks ignored; the line's own name only when nothing else names a place)
    when the line reaches that place's metropolitan area; else the big place near the line: least (distance from the
    line's nearest station, at least 2 km) / sqrt(population) among the places of its country whose metropolitan area it
    reaches; a place near a bigger one stands for the bigger one."""
    P = np.array([(stations[s][2], stations[s][3]) for s in c['stops']]); lon, lat = P.mean(0); k = math.cos(math.radians(lat))
    near = [i for d, i in PGRID.near(lon, lat, 150000 if c['kind'] == 's' else 80000)]
    if not near: return None
    Q = PLX[near]; dmin = np.hypot((Q[:, None, 0] - P[None, :, 0]) * k, Q[:, None, 1] - P[None, :, 1]).min(1) * 111195
    reach_any = {i: d for i, d in zip(near, dmin.tolist()) if d <= radius(i)}
    reach = {i: d for i, d in reach_any.items() if pcc(i) == c['cc']}       # the distance rule stays in the line's country
    vals = [first(t.get(key)) for t in c['ts'][:3] for key in ('network', 'network:en', 'operator', 'operator:en', 'brand')]
    txt = ' '.join(v for v in vals if v and not NOT_OP.search(v))
    if not txt.strip(): txt = ' '.join(clean_name(t.get(key)) for t in c['ts'][:2] for key in ('name', 'name:en'))
    full = txt + ' ' + ' '.join(t.get(k) or '' for t in c['ts'][:2] for k in ('name', 'ref'))      # "Tren Urbano" -> San Juan
    for a, b in ALIAS.items():
        if re.search(r'(?<![\w])' + re.escape(a) + r'(?![\w])', full): txt += ' ' + b
    best = None
    for i in reach_any:
        if PLP[i] < 50000 and owner[i] == i: continue
        for n in place_names(PL[i]):
            hit = HAN.search(n) or KANA.search(n) or HANGUL.search(n) or CYR.search(n)
            if n in txt and (hit or re.search(r'(?<![\w])' + re.escape(n) + r'(?![\w])(?!\s+(?:Valley|Avenue|Street|Roads|County|Region))', txt)):
                if best is None or PLP[owner[i]] > PLP[owner[best]]: best = i
    if best is not None: return owner[best] if owner[best] in reach_any or PLP[best] < 1e6 else best
    if not reach: return None
    return owner[min(reach, key=lambda i: max(reach[i], 2000) / math.sqrt(PLP[owner[i]]))]
place = {i: city_of(c) for i, c in enumerate(cand) if c['kind'] not in 'hr'}
# the suburban, metro and light rail lines of one network go to its main city (LIRR's Montauk Branch to New York, S-Bahn
# Bern's S1 from Thun to Bern) when they reach its metropolitan area; not national networks, not away from big cities
by_net = defaultdict(list)
for i, j in place.items():
    if j is not None and cand[i]['net'] and cand[i]['kind'] in 'sml' and not NOT_OP.search(cand[i]['net']):
        by_net[(cand[i]['cc'], cand[i]['net'])].append(i)
for ids in by_net.values():
    top, n = Counter(place[i] for i in ids).most_common(1)[0]
    if len(ids) < 3 or n < 0.5 * len(ids): continue
    X = np.array([stations[s][2:4] for i in ids for s in cand[i]['stops']])
    if metres(X[:, 0].min(), X[:, 1].min(), X[:, 0].max(), X[:, 1].max()) > 400000: continue       # a national network
    for i in ids:
        if place[i] == top or PLP[place[i]] >= 300000: continue
        if sum(metres(*stations[s][2:4], PL[top][1], PL[top][2]) <= radius(top) for s in cand[i]['stops']) >= 0.3 * len(cand[i]['stops']):
            place[i] = top
cities, city_idx, city_nets = [], {}, defaultdict(Counter)
for i, c in enumerate(cand):
    c['city'] = -1; j = place.get(i)
    if j is None: continue
    if j not in city_idx:
        p = PL[j]; cc = pcc(j)
        native = p[3]
        if (HAN.search(native) or HANGUL.search(native)) and len(native) >= 3: native = re.sub(CJK_CITY, '', native)
        en = p[4] if latin(p[4]) else latinize(native, cc)
        city_idx[j] = len(cities); cities.append([native, en, round(p[1], 5), round(p[2], 5), p[5], 0, cc, ''])
    c['city'] = city_idx[j]; cities[c['city']][5] += 1
    if c['logo']: city_nets[c['city']][c['logo']] += {'m': 10, 'l': 3, 't': 3}.get(c['kind'], 1)   # the metro's logo first
for ci, cnt in city_nets.items(): cities[ci][7] = cnt.most_common(1)[0][0]
_g = defaultdict(list)
for i, c in enumerate(cand):
    if c['kind'] in 'mltf' and c['city'] >= 0: _g[(c['cc'], c['city'], c['kind'])].append(i)
fill_logos(_g, 'city'); log('operator logos by city', nlogo['city'])
log('cities', len(cities))
same = defaultdict(list)
for i, c in enumerate(cand): same[(c['cc'], c['city'], c['name'])].append(i)
for (cc, city, name), ids in same.items():
    one = cand[ids[0]]
    if len(ids) < 2 and not (one['kind'] not in 'mlt' and operatorish(one)): continue
    if len({cand[i]['desc'] for i in ids}) < len(ids) and all(cand[i]['ref'] for i in ids) and len({cand[i]['ref'] for i in ids}) == len(ids):
        for i in ids:
            c = cand[i]
            if c['ref'] not in name: c['name'] = f"{name} ({c['ref']})"; c['en'] = f"{c['en']} ({c['ref']})"
        continue
    for i in ids:
        c = cand[i]
        if c['desc'] and c['desc'] != name and c['desc'] not in name:
            c['name'] = f"{name}: {c['desc']}"; c['en'] = f"{c['en']}: {c['desc_en']}" if c['desc_en'] not in c['en'] else c['en']

# ---------------------------------------------------------------- line records, station kinds, transfers, complexes
lines, geometry = [], []
for c in cand:
    km = round(km_of(c['stops']) * 1.06) if c['kind'] in 'hr' else 0
    lines.append([c['kind'], c['name'], c['en'], c['ref'], c['colour'], c['city'], c['stops'], c['branches'], c['loop'], km, 0, '',
                  c['cc'], c['op'], c['logo']])
    geometry.append(c['ways'])
for li, l in enumerate(lines):
    for s in {x for q in [l[6]] + l[7] for x in q}: stations[s][5].append(li)
for s in stations:
    ks = sorted({lines[li][0] for li in s[5]}, key=lambda k: RANK[k])
    s[4] = ks[0] if ks else 'm'
# transfers: other stations within 300 m, or the same name within 800 m
used = [i for i, s in enumerate(stations) if s[5]]
U = np.array(used, dtype=np.int64); UX = np.array([stations[i][2:4] for i in used]).reshape(-1, 2)
cell = defaultdict(list)
for k, (x, y) in enumerate(np.floor(UX / 0.01).astype(int).tolist()): cell[(x, y)].append(k)
for (cx, cy), ks in cell.items():
    nx = int(0.8 / (1.1 * max(math.cos(math.radians(cy * 0.01)), 0.05))) + 1
    nb = np.array([k for i in range(-nx, nx + 1) for j in (-1, 0, 1) for k in cell.get((cx + i, cy + j), ())])
    A, B = UX[ks], UX[nb]
    d = np.hypot((A[:, None, 0] - B[None, :, 0]) * math.cos(math.radians(cy * 0.01)), A[:, None, 1] - B[None, :, 1]) * 111195
    for a, b in zip(*np.nonzero(d <= 800)):
        i, j = int(U[ks[a]]), int(U[nb[b]])
        if i != j and (d[a, b] <= 300 or (stations[i][0] and skey(stations[i][0]) == skey(stations[j][0]))): stations[i][6].append(j)
# complexes: rail stations (h r s) whose names are related (one contains the other: Shinjuku / Seibu-Shinjuku) within
# 600 m; each urban station with the rail station its name matches within 500 m (Hlavní nádraží, Киевская / Киевский
# вокзал), else the nearest within 250 m; urban stations with each other within 150 m or by name; never two rail
# stations through an urban one; a group more than 1.2 km across is a chain through a dense centre, not one place
parent = list(range(len(stations)))
def find(x):
    while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
    return x
rails = defaultdict(set)          # component -> its rail stations
for a in used:
    if stations[a][4] in 'hrs': rails[a].add(a)
def union(a, b, rail_ok=False):
    ra, rb = find(a), find(b)
    if ra == rb: return
    if rails[ra] and rails[rb] and not rail_ok: return
    parent[ra] = rb; rails[rb] |= rails.pop(ra, set())
def stem(n): return {w[:5] for w in re.split(r'[\W_]+', plain(n.lower())) if len(w) >= 5}
for a in used:
    s = stations[a]
    if s[4] not in 'hrs': continue
    for j in s[6]:
        t = stations[j]
        if t[4] in 'hrs' and related(rec_mk[a], rec_mk[j]) and metres(s[2], s[3], t[2], t[3]) <= 600: union(a, j, True)
near_rail = {}
for a in used:
    s = stations[a]
    if s[4] in 'hrs': continue
    best = None
    for d, j in sorted((metres(s[2], s[3], stations[j][2], stations[j][3]), j) for j in s[6] if stations[j][4] in 'hrs'):
        named = related(rec_mk[a], rec_mk[j]) or stem(s[0]) & stem(stations[j][0])
        if named and d <= 500 or d <= 250:
            if best is None or named and not best[0]: best = (bool(named), j)
    if best: near_rail[a] = best[1]
for a, j in near_rail.items(): union(a, j)
for a in used:
    s = stations[a]
    if s[4] in 'hrs': continue
    for j in s[6]:
        t = stations[j]
        if t[4] in 'hrs': continue
        if metres(s[2], s[3], t[2], t[3]) <= 150 or (s[0] and skey(s[0]) == skey(t[0])): union(a, j)
comp = defaultdict(list)
for a in used: comp[find(a)].append(a)
ncx = 0
for members in comp.values():
    if len(members) < 2: continue
    X = np.array([(stations[m][2], stations[m][3]) for m in members])
    if metres(X[:, 0].min(), X[:, 1].min(), X[:, 0].max(), X[:, 1].max()) > 1200: continue
    rep = max(members, key=lambda i: (stations[i][4] in 'hrs', stations[i][4] == 'h', stations[i][4] == 'r', len(stations[i][5])))
    for m in members: stations[m][7] = rep
    ncx += 1
log('transfers', sum(len(s[6]) for s in stations) // 2, 'complexes', ncx)

# ---------------------------------------------------------------- output (only stations some line uses)
del _snap, SGRID, STOPGRID, MAIN_NEAR, KEY_WAYS, _len, ROUTES, R, units, clusters, NODES, _nm
for r in RELS.values(): r['members'] = None
gc.collect()
keep = [i for i, s in enumerate(stations) if s[5]]
new = {o: n for n, o in enumerate(keep)}
S = []
for o in keep:
    s = stations[o]
    S.append([s[0], s[1], s[2], s[3], s[4], s[5], [new[t] for t in s[6] if t in new], new.get(s[7], -1), s[8]])
for l in lines:
    l[6] = [new[s] for s in l[6]]; l[7] = [[new[s] for s in b] for b in l[7]]
os.makedirs(OUT, exist_ok=True)
json.dump({'lines': lines, 'stations': S, 'cities': cities}, open(os.path.join(OUT, 'network.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
json.dump([sorted({x[1] if isinstance(x, tuple) else x for x in c['rids']}) for c in cand],       # the OSM relations of each line (audits)
          open(os.path.join(OUT, 'sources.json'), 'w'), separators=(',', ':'))
ways = {w: (v[1], v[0], v[2]) for w, v in WAYS.items() if v[0].get('railway') in ('rail', 'narrow_gauge')}
for g in geometry:
    for w in g: ways[w] = (WAYS[w][1], WAYS[w][0], WAYS[w][2])
with open(os.path.join(OUT, 'geometry.pickle'), 'wb') as f:
    pk = pickle.Pickler(f, protocol=5); pk.fast = True       # no memo: it would take GBs for millions of arrays
    pk.dump({'geometry': geometry, 'ways': ways})
# the way lists alone, for assemble.py --lean, with what identifies the ways (the raw pickle and the way ids): a build
# with other lines on the same ways keeps make_tiles.py's rail network store and routes
import hashlib
pickle.dump({'geometry': geometry, 'network': hashlib.blake2b(open(os.path.join(OUT, 'network.json'), 'rb').read(), digest_size=16).hexdigest(),
             'src': f'raw {os.path.getsize(sys.argv[1])}:{os.stat(sys.argv[1]).st_mtime_ns} ways ' +
                    hashlib.blake2b(np.sort(np.fromiter(ways, np.int64, len(ways))).tobytes(), digest_size=16).hexdigest()},
            open(os.path.join(OUT, 'lines.pickle'), 'wb'), protocol=5)
log('lines', len(lines), dict(Counter(l[0] for l in lines)), 'stations', len(S), 'cities', len(cities),
    'countries', dict(Counter(l[12] for l in lines).most_common(12)))
