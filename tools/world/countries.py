"""Country of a point: Natural Earth 1:10m borders (world-atlas TopoJSON, tools/world/ref/countries-10m.json),
falling back to the nearest GeoNames city (pop >= 15k) for points just off a coast or in a gap.

    from countries import country_code, country_name
    country_code(114.17, 22.30) -> 'HK'

Hong Kong and Macau use their OpenStreetMap boundaries (relations 913110 and 1867188, tools/world/ref/hk-mo.json):
the 1:10m lines are off by up to a few km at the Shenzhen River and around Zhuhai, and every point near them
that is in neither is China. Disputed areas follow the internationally recognised borders, as Natural Earth
already does for Donbas (UA), Abkhazia and South Ossetia (GE), Transnistria (MD), Nagorno-Karabakh (AZ) and
East Jerusalem (PS). Two overrides make that consistent: its de-facto Crimea (RU) is UA, and West Jerusalem,
which its coarse West Bank outline takes in up to Mount Herzl, is IL (a box west of the 1949 line).
"""
import json, math, os
from collections import defaultdict
import numpy as np
import geonamescache
from shapely.affinity import translate
from shapely.geometry import Polygon, MultiPolygon, Point, box
from shapely.strtree import STRtree

_gc = geonamescache.GeonamesCache()
COUNTRIES = _gc.get_countries()
_NUM = {int(c['isonumeric']): iso for iso, c in COUNTRIES.items() if c.get('isonumeric') not in (None, '')}
_NAME = {c['name']: iso for iso, c in COUNTRIES.items()}
_EXTRA = {'Kosovo': 'XK', 'N. Cyprus': 'CY', 'Somaliland': 'SO', 'Taiwan': 'TW', 'Hong Kong': 'HK', 'Macao': 'MO'}

def _load():
    t = json.load(open(os.path.join(os.path.dirname(__file__), 'ref', 'countries-10m.json')))
    sx, sy = t['transform']['scale']; tx, ty = t['transform']['translate']
    arcs = []
    for a in t['arcs']:
        x = y = 0; pts = []
        for dx, dy in a:
            x += dx; y += dy; pts.append((x * sx + tx, y * sy + ty))
        arcs.append(pts)
    def ring(ix):
        pts = []
        for i in ix:
            seg = arcs[i] if i >= 0 else arcs[~i][::-1]
            pts.extend(seg[1:] if pts else seg)
        return pts
    geoms, codes = [], []
    for g in t['objects']['countries']['geometries']:
        iso = _NUM.get(int(g['id'])) if str(g.get('id', '')).isdigit() else None
        iso = iso or _EXTRA.get(g['properties'].get('name')) or _NAME.get(g['properties'].get('name'))
        if not iso: continue
        polys = [g['arcs']] if g['type'] == 'Polygon' else g['arcs'] if g['type'] == 'MultiPolygon' else []
        for p in polys:
            rings = [ring(r) for r in p]
            # world-atlas stitches rings across the antimeridian (Fiji, Chukotka, the Aleutians): unwrap them
            # to 0..360 and cut them back into the two halves, or they become bands around the globe
            wrap = any(abs(a[0] - b[0]) > 180 for a, b in zip(rings[0], rings[0][1:]))
            if wrap: rings = [[(x + 360 if x < 0 else x, y) for x, y in r] for r in rings]
            try:
                poly = Polygon(rings[0], rings[1:])
                if not poly.is_valid: poly = poly.buffer(0)
            except Exception: continue
            parts = [poly] if not wrap else [poly.intersection(box(-180, -90, 180, 90)),
                                             translate(poly.intersection(box(180, -90, 540, 90)), -360)]
            for g in parts:
                if not g.is_empty: geoms.append(g); codes.append(iso)
    for cc, polys in json.load(open(os.path.join(os.path.dirname(__file__), 'ref', 'hk-mo.json'))).items():
        _EXACT[cc] = MultiPolygon([Polygon(p[0], p[1:]) for p in polys]).buffer(0)
    return geoms, codes
_EXACT = {}     # OSM boundaries that override the 1:10m polygons nearby
_EXACT_BOX = (113.3, 21.9, 114.7, 22.8)
_CRIMEA_BOX = (32.3, 44.3, 36.6, 46.3)
_WEST_JERUSALEM = (35.10, 31.74, 35.225, 31.81)
_GEOMS, _CODES = _load()
_TREE = STRtree(_GEOMS)

_CITIES = list(_gc.get_cities().values())
_CXY = np.array([(c['longitude'], c['latitude']) for c in _CITIES])
_CCC = [c['countrycode'] for c in _CITIES]
_GRID = defaultdict(list)
for _i, (_x, _y) in enumerate(_CXY): _GRID[(int(math.floor(_x)), int(math.floor(_y)))].append(_i)

def _nearest_city_cc(lon, lat):
    gx, gy = int(math.floor(lon)), int(math.floor(lat))
    ring = lambda r: [i for dx in range(-r, r + 1) for dy in range(-r, r + 1) if max(abs(dx), abs(dy)) == r for i in _GRID.get((gx + dx, gy + dy), ())]
    cand = []
    for r in range(30):
        cand += ring(r)
        if cand:
            cand += ring(r + 1)
            c = np.array(cand); d = ((_CXY[c, 0] - lon) * math.cos(math.radians(lat))) ** 2 + (_CXY[c, 1] - lat) ** 2
            return _CCC[int(c[int(np.argmin(d))])]
    return ''

_cache = {}
def country_code(lon, lat):
    key = (round(lon, 3), round(lat, 3))
    if key in _cache: return _cache[key]
    p = Point(lon, lat)
    x0, y0, x1, y1 = _EXACT_BOX
    if x0 <= lon <= x1 and y0 <= lat <= y1:
        cc = next((c for c, g in _EXACT.items() if g.contains(p)), None)
        if cc: _cache[key] = cc; return cc
    hits = sorted(_TREE.query(p, predicate='intersects'), key=lambda i: _GEOMS[i].area)   # enclaves over their host
    if not len(hits):   # just off the coast: nearest border within ~5 km
        near = _TREE.query(p.buffer(0.05))
        if len(near): hits = [min(near, key=lambda i: _GEOMS[i].distance(p))]
    cc = _CODES[hits[0]] if len(hits) else _nearest_city_cc(lon, lat)
    if cc in _EXACT and x0 <= lon <= x1 and y0 <= lat <= y1: cc = 'CN'
    x0, y0, x1, y1 = _CRIMEA_BOX
    if cc == 'RU' and x0 <= lon <= x1 and y0 <= lat <= y1: cc = 'UA'
    x0, y0, x1, y1 = _WEST_JERUSALEM
    if cc == 'PS' and x0 <= lon <= x1 and y0 <= lat <= y1: cc = 'IL'
    _cache[key] = cc
    return cc

def country_name(cc):
    return COUNTRIES.get(cc, {}).get('name', cc)

if __name__ == '__main__':
    for q in [(114.17, 22.30), (113.55, 22.19), (118.09, 24.48), (118.32, 24.44), (7.59, 47.55), (7.58, 47.59), (121.5, 25.05), (-73.99, 40.75), (139.7, 35.69),
              (145.771, -16.926), (25.258, -16.932), (178.44, -18.14), (177.5, 64.7), (-171.0, 66.0), (33.95, 44.95), (37.5, 55.75), (114.113, 22.528), (114.112, 22.534), (113.544, 22.218), (113.545, 22.141)]:
        print(q, country_code(*q))
