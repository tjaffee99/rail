"""Wikidata for the world build: English station names and operator logos, fetched from the Wikidata Query Service once
and cached as JSON in WD_DIR (default /home/user/world/wd, or env WD_DIR). Every step skips what is cached, so a run can
be stopped and started again; delete a cache file to fetch it anew.

    python3 tools/world/wikidata.py <nodes> <relations.json> [full.json] [steps]
        nodes      raw_world.pickle (its 'stations') or any pickle of {node id: (lon, lat, tags)}
        full.json  build/full.json: its line operators are matched by name too
        steps      any of classes items near places ops logos summary (default: all, in that order)

Caches in WD_DIR:
  classes.json     station classes: the subclass tree of railway station Q55488, metro station Q928830, tram stop Q2175765,
                   railway stop Q55678, halt Q10476836, funicular station Q15915771, train station Q26836219, monorail
                   station Q63125083, railway platform Q325358
  items.json       {qid: [en, {lang: label}, lon, lat, station 0/1, redirect target]} for the wikidata= of every station
                   node ([] when the item is gone); en is the English label (en, en-gb, en-us, en-ca, else a Latin
                   mul label) or ''; labels in the languages of the non-Latin-script countries
  near/<CC>.json   [[qid, lon, lat, en, {lang: label}], ...] every Wikidata station of the country (P17) with coordinates
  places.json      {"name|CC": [[qid, lon, lat, en], ...]} any item with coordinates whose label in the country's languages
                   is exactly this non-Latin station name (the place a station is named after)
  ops.json         {qid: {en, logo, small, cc, cls, ind, desc, parent, r}} operator / network / brand items and the items
                   named like one: English label, Commons file names of the P154 logo and P8972 small logo (preferred
                   rank, else current, else latest), countries P17, classes P31, industries P452, English description,
                   parent organisation P749 / owner P127 / operator P137, redirect target
  ophits.json      {"name|CC": [qid, ...]} items whose label or alias (English or the country's languages) is exactly an
                   operator / network / brand string that has no wikidata id; op_by_name() picks the one rail operator
  thumbs.json      {file: http status of its thumb() url} for a random sample of the logos (upload.wikimedia.org
                   rate-limits scripted clients, so not all of them)
  wiki.json        {qid: English Wikipedia title or ''} of the operator items and their parents
  wikilogos.json   {title: 120 px url of the logo in that article's infobox, or ''}; step wikilogos (run as
                   "wikidata.py wikilogos <network.json> ...": the articles of the logo keys those lines use)
  national.json    {CC: [qid, logo url, share, n]} the operator whose logo 90%+ of the country's 20+ train services with a
                   known operator show
  countries.json   {ISO: qid}
  summary.json     coverage

Module API (reads the caches only, never the network; every function loads them on first use):
  item(qid) -> (en, labels, lon, lat, is_station) or None       the item of a wikidata= tag, redirects followed
  nearby(lon, lat, cc, r=500) -> [(metres, qid, en, labels)]      Wikidata stations of that country within r metres, nearest first
  places(name, cc) -> [(qid, lon, lat, en)]                        items whose native label is exactly this name
  op(qid) -> (en, url)                                             English label and 120 px logo url ('' when none) of an
                                                                   operator / network / brand; the logo of its parent
                                                                   organisation, owner or operator (when that is a
                                                                   transport company) when it has none
  railish(ops entry) -> bool                                       a rail / public transport company, network, authority
  op_by_name(name, cc) -> qid                                      the rail operator item this name stands for, or ''
  wiki_logo(title) -> url                                          the logo of that article's infobox (wikilogos.json), '' when
                                                                   it has none, None when not looked up
  wiki(qid) -> title                                               English Wikipedia article whose infobox logo stands in
                                                                   for a missing Commons logo ('' when not needed / none)
  default_operator(cc) -> (qid, url)                               the operator of nearly all the country's trains, or ('', '')
  thumb(file, px=120) -> url                                       120 px image url of a Commons file (svg: upload.wikimedia.org
                                                                   png thumbnail; bitmap: Special:FilePath?width=120)
  LANGS[cc] -> [wikidata language codes]                           the languages of a country
"""
import hashlib, json, math, os, pickle, re, sys, time, urllib.parse, urllib.request, urllib.error
from collections import Counter, defaultdict
import geonamescache
sys.path.insert(0, os.path.dirname(__file__))
from translit import latin

WD_DIR = os.environ.get('WD_DIR', '/home/user/world/wd')
UA = 'RailAtlasBuilder/1.0 (https://rail.theojaffee.net)'
ROOTS = 'Q55488 Q928830 Q2175765 Q55678 Q10476836 Q15915771 Q26836219 Q63125083 Q325358'.split()
# languages of each country (geonames), as Wikidata label codes; Chinese in both scripts
_GC = geonamescache.GeonamesCache().get_countries()
LANGS = {}
for iso, c in _GC.items():
    ls = []
    for l in (c.get('languages') or '').lower().split(','):
        l = l.strip()
        for x in ((l, 'zh-hant', 'zh-tw', 'zh-hk') if l.startswith('zh') and iso in ('TW', 'HK', 'MO') else (l, 'zh-hans') if l.startswith('zh') else (l,)):
            x = x if x in ('zh-tw', 'zh-hk', 'zh-hant', 'zh-hans') else x.split('-')[0]
            if x and len(x) <= 3 and x not in ls: ls.append(x)
    LANGS[iso] = ls[:8]
LANGS.update({'XK': ['sq', 'sr'], 'UA': ['uk', 'ru'], 'BY': ['be', 'ru'], 'KZ': ['kk', 'ru'], 'KG': ['ky', 'ru'], 'UZ': ['uz', 'ru'],
              'TJ': ['tg', 'ru'], 'TM': ['tk', 'ru'], 'AZ': ['az', 'ru'], 'GE': ['ka', 'ru'], 'AM': ['hy', 'ru'], 'MD': ['ro', 'ru'],
              'IL': ['he', 'ar'], 'PS': ['ar', 'he'], 'IR': ['fa'], 'AF': ['fa', 'ps'], 'PK': ['ur'], 'MN': ['mn', 'ru'], 'KP': ['ko'],
              'EG': ['ar', 'arz'], 'LK': ['si', 'ta']})
NONLATIN = sorted({l for ls in LANGS.values() for l in ls} - set('en fr de es it pt nl pl cs sk sl hr bs ro hu sv da no nb nn fi et lv lt '
                  'is ga cy mt sq tr az uz tk id ms tl vi sw so ca eu gl af lb rm ha yo ig zu xh st tn ny mg om ht qu gn ay'.split()))
EN = ('en', 'en-gb', 'en-us', 'en-ca', 'mul')

def log(*a): print(time.strftime('%H:%M:%S'), *a, file=sys.stderr, flush=True)
def path(*p): return os.path.join(WD_DIR, *p)
def read(name, default):
    try: return json.load(open(path(name)))
    except (OSError, ValueError): return default
def write(name, obj):
    os.makedirs(os.path.dirname(path(name)), exist_ok=True)
    tmp = path(name) + '.tmp'
    json.dump(obj, open(tmp, 'w'), ensure_ascii=False, separators=(',', ':')); os.replace(tmp, path(name))
def qid(uri): return uri.rsplit('/', 1)[-1]
def point(v):
    m = re.match(r'Point\(([-\d.eE]+) ([-\d.eE]+)\)', v)
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)
def metres(lon1, lat1, lon2, lat2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2)); y = math.radians(lat2 - lat1)
    return 6371000 * math.hypot(x, y)
def lit(s, lang): return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"@' + lang

# ---------------------------------------------------------------- query service
_last = [0.0]
def sparql(q, tries=6):
    """Rows of a query as dicts of plain values; waits ~1 s between queries, backs off on 429 / 5xx / timeouts."""
    for k in range(tries):
        time.sleep(max(0, 1.0 - (time.time() - _last[0])))
        req = urllib.request.Request('https://query.wikidata.org/sparql', data=urllib.parse.urlencode({'query': q}).encode(),
                                     headers={'User-Agent': UA, 'Accept': 'application/sparql-results+json'})
        try:
            with urllib.request.urlopen(req, timeout=70) as r: d = json.load(r)
            _last[0] = time.time()
            return [{k2: v['value'] for k2, v in b.items()} for b in d['results']['bindings']]
        except urllib.error.HTTPError as e:
            _last[0] = time.time()
            if e.code == 429: time.sleep(int(e.headers.get('Retry-After') or 30) + 5 * k); continue
            if e.code in (500, 502, 503, 504): time.sleep(10 * (k + 1)); continue
            raise
        except (TimeoutError, OSError, ValueError) as e:
            _last[0] = time.time(); log('  retry', type(e).__name__, str(e)[:80]); time.sleep(10 * (k + 1))
    raise TimeoutError('query failed %d times' % tries)
def batched(xs, n):
    xs = list(xs)
    for i in range(0, len(xs), n): yield xs[i:i + n]

# ---------------------------------------------------------------- steps
def fetch_classes():
    if read('classes.json', None): return
    rows = sparql('SELECT DISTINCT ?c WHERE { VALUES ?r { %s } ?c wdt:P279* ?r }' % ' '.join('wd:' + q for q in ROOTS))
    write('classes.json', sorted({qid(r['c']) for r in rows}, key=lambda q: int(q[1:])))
    log('classes', len(rows))

def _items_query(qs, langs):
    return ('SELECT ?i ?p ?v WHERE { VALUES ?i { %s } { ?i rdfs:label ?v BIND(CONCAT("l:", LANG(?v)) AS ?p) FILTER(LANG(?v) IN (%s)) } '
            'UNION { ?i wdt:P625 ?v BIND("c" AS ?p) } UNION { ?i wdt:P31 ?v BIND("t" AS ?p) } UNION { ?i owl:sameAs ?v BIND("r" AS ?p) } }'
            % (' '.join('wd:' + q for q in qs), ','.join('"%s"' % l for l in langs)))
def fetch_items(nodes):
    """Labels, coordinates and classes of every station node's wikidata item (redirects followed)."""
    cls = set(read('classes.json', []))
    items = read('items.json', {})
    want = {q.strip() for lon, lat, t in nodes.values() for q in (t.get('wikidata') or '').split(';') if re.fullmatch(r'Q\d+', q.strip())}
    langs = list(EN) + NONLATIN
    while True:
        todo = sorted(want - set(items), key=lambda q: int(q[1:]))
        if not todo: break
        log('items', len(items), 'cached,', len(todo), 'to fetch')
        for n, qs in enumerate(batched(todo, 250)):
            got = defaultdict(lambda: ['', {}, None, None, 0, ''])
            for r in sparql(_items_query(qs, langs)):
                g = got[qid(r['i'])]
                if r['p'].startswith('l:'): g[1][r['p'][2:]] = r['v']
                elif r['p'] == 'c' and g[2] is None and 'Point(' in r['v'] and '<' not in r['v']: g[2], g[3] = point(r['v'])
                elif r['p'] == 't' and qid(r['v']) in cls: g[4] = 1
                elif r['p'] == 'r': g[5] = qid(r['v']); want.add(g[5])
            for q in qs:
                if q not in got: items[q] = []; continue
                g = got[q]; ls = g[1]
                g[0] = next((ls[l] for l in EN if latin(ls.get(l, '')) and re.search('[A-Za-z]', ls.get(l, ''))), '')
                for l in EN: ls.pop(l, None)
                items[q] = g
            if n % 20 == 19: write('items.json', items)
        write('items.json', items)
    log('items', len(items))

def country_qids():
    cq = read('countries.json', {})
    if not cq:
        for r in sparql('SELECT ?c ?iso WHERE { ?c wdt:P297 ?iso . ?c wdt:P31 ?t . VALUES ?t { wd:Q3624078 wd:Q6256 wd:Q15634554 wd:Q161243 wd:Q1763527 } }'):
            cq.setdefault(r['iso'], qid(r['c']))
        cq.update({'XK': 'Q1246', 'TW': 'Q865', 'PS': 'Q219060', 'HK': 'Q8646', 'MO': 'Q14773'})
        write('countries.json', cq)
    return cq

def fetch_near(ccs):
    """Every Wikidata station with coordinates of these countries, with its English and native labels."""
    cls, cq = read('classes.json', []), country_qids()
    main = [c for c in ('Q55488', 'Q928830', 'Q55678', 'Q2175765', 'Q10476836') if c in cls]
    for cc in ccs:
        if os.path.exists(path('near', cc + '.json')) or cc not in cq: continue
        langs = list(EN) + [l for l in LANGS.get(cc, []) if l not in EN]
        out = {}
        for group in [[c] for c in main] + [[c for c in cls if c not in main]]:
            for part in batched(group, 150):
                rows = sparql('SELECT ?i ?c ?v (LANG(?v) AS ?lg) WHERE { VALUES ?t { %s } ?i wdt:P31 ?t; wdt:P17 wd:%s; wdt:P625 ?c . '
                              '?i rdfs:label ?v FILTER(LANG(?v) IN (%s)) }' % (' '.join('wd:' + c for c in part), cq[cc], ','.join('"%s"' % l for l in langs)))
                for r in rows:
                    q = qid(r['i'])
                    if q not in out:
                        lon, lat = point(r['c'])
                        if lon is None: continue
                        out[q] = [q, round(lon, 5), round(lat, 5), '', {}]
                    out[q][4][r['lg']] = r['v']
        for g in out.values():
            ls = g[4]
            g[3] = next((ls[l] for l in EN if latin(ls.get(l, '')) and re.search('[A-Za-z]', ls.get(l, ''))), '')
            for l in EN: ls.pop(l, None)
        write(os.path.join('near', cc + '.json'), list(out.values()))
        log('near', cc, len(out))

def fetch_places(names):
    """Items with coordinates whose label in the country's languages is exactly the name: {(name, cc)} -> places.json."""
    pl = read('places.json', {})
    todo = sorted({(n, cc) for n, cc in names if n + '|' + cc not in pl})
    log('places', len(pl), 'cached,', len(todo), 'to fetch')
    for k, part in enumerate(batched(todo, 120)):
        vals, back = [], {}
        for n, cc in part:
            for l in [l for l in LANGS.get(cc, []) if l in NONLATIN] or ['ru']:
                vals.append(lit(n, l)); back[(n, l)] = cc
        rows = sparql('SELECT ?s ?i ?c ?en WHERE { VALUES ?s { %s } ?i rdfs:label ?s; wdt:P625 ?c . '
                      'OPTIONAL { ?i rdfs:label ?en FILTER(LANG(?en) = "en") } }' % ' '.join(vals))
        for n, cc in part: pl[n + '|' + cc] = []
        for r in rows:
            lon, lat = point(r['c'])
            if lon is None or not latin(r.get('en', '')): continue
            for (n, l), cc in back.items():
                if n == r['s'] and [qid(r['i']), round(lon, 5), round(lat, 5), r['en']] not in pl[n + '|' + cc]:
                    pl[n + '|' + cc].append([qid(r['i']), round(lon, 5), round(lat, 5), r['en']])
        if k % 20 == 19: write('places.json', pl); log('  places', len(pl))
    write('places.json', pl)

OPKEYS = ('operator', 'network', 'brand')
_OP_Q = ('SELECT ?i ?p ?v ?rank ?end ?start WHERE { VALUES ?i { %s } '
         '{ ?i p:P154 ?s . ?s ps:P154 ?v; wikibase:rank ?rank . OPTIONAL { ?s pq:P582 ?end } OPTIONAL { ?s pq:P580 ?start } BIND("logo" AS ?p) } '
         'UNION { ?i p:P8972 ?s . ?s ps:P8972 ?v; wikibase:rank ?rank . OPTIONAL { ?s pq:P582 ?end } OPTIONAL { ?s pq:P580 ?start } BIND("small" AS ?p) } '
         'UNION { ?i rdfs:label ?v FILTER(LANG(?v) = "en") BIND("en" AS ?p) } UNION { ?i rdfs:label ?v FILTER(LANG(?v) = "mul") BIND("mul" AS ?p) } '
         'UNION { ?i schema:description ?v FILTER(LANG(?v) = "en") BIND("desc" AS ?p) } '
         'UNION { ?i wdt:P17 ?v BIND("cc" AS ?p) } UNION { ?i wdt:P31 ?v BIND("cls" AS ?p) } UNION { ?i wdt:P452 ?v BIND("ind" AS ?p) } '
         'UNION { ?i wdt:P749 ?v BIND("parent" AS ?p) } UNION { ?i wdt:P127 ?v BIND("parent" AS ?p) } UNION { ?i wdt:P137 ?v BIND("parent" AS ?p) } '
         'UNION { ?i owl:sameAs ?v BIND("r" AS ?p) } }')
def _best(stmts):
    """The logo file of a property's statements: preferred rank, else current (no end date), else the latest start."""
    stmts = [s for s in stmts if not s[1].endswith('DeprecatedRank')]
    if not stmts: return ''
    s = max(stmts, key=lambda s: (s[1].endswith('PreferredRank'), not s[2], s[3] or '', s[0]))
    return urllib.parse.unquote(s[0].rsplit('/Special:FilePath/', 1)[-1]).replace('_', ' ')
def fetch_ops(qs):
    ops = read('ops.json', {})
    while True:
        todo = sorted(set(qs) - set(ops), key=lambda q: int(q[1:]))
        if not todo: break
        log('ops', len(ops), 'cached,', len(todo), 'to fetch')
        for part in batched(todo, 100):
            got = defaultdict(lambda: defaultdict(list))
            for r in sparql(_OP_Q % ' '.join('wd:' + q for q in part)):
                v = r['v']
                got[qid(r['i'])][r['p']].append((v, r.get('rank', ''), r.get('end', ''), r.get('start', '')) if r['p'] in ('logo', 'small') else
                                                 qid(v) if v.startswith('http://www.wikidata.org/entity/') else v)
            for q in part:
                g = got.get(q)
                if not g: ops[q] = {}; continue
                en = g['en'][0] if g['en'] else next((m for m in g['mul'] if latin(m)), '')
                ops[q] = {'en': en, 'logo': _best(g['logo']), 'small': _best(g['small']), 'cc': sorted(set(g['cc'])), 'cls': sorted(set(g['cls'])),
                          'ind': sorted(set(g['ind'])), 'desc': (g['desc'] or [''])[0], 'parent': list(dict.fromkeys(g['parent']))[:6],
                          'r': (g['r'] or [''])[0]}
                qs = list(qs) + [x for x in [ops[q]['r']] if x]
        write('ops.json', ops)
    return ops

LEGAL = (r'(?i)(?:^|\s)(?:АО|ОАО|ЗАО|ПАО|ООО|ГУП|МУП|МП|ФГУП|КП|ПАТ|ПрАТ|ТОВ|АТ|ДП|ЕАД|АД|ЈП|ДОО|АД|Oy|Oyj|AB|AS|ASA|A/S|AG|GmbH|mbH|'
               r'SE|SA|S\.A\.|S\.p\.A\.|SpA|S\.r\.l\.|srl|Sp\. z o\.o\.|a\.s\.|s\.r\.o\.|d\.o\.o\.|d\.d\.|Zrt\.?|Kft\.?|Ltd\.?|Limited|Inc\.?|LLC|plc|'
               r'Co\.,? Ltd\.?|Corp\.?|Corporation|N\.V\.|B\.V\.|e\.V\.|KG|Ges\.m\.b\.H\.|株式会社|（株）|\(株\))(?=\s|$|,)')
def norm_op(s):
    """An operator name without its legal form and quotes: "АО «ФПК»" -> "ФПК", "DB Regio AG" -> "DB Regio"."""
    return re.sub(r'\s+', ' ', re.sub(LEGAL, ' ', re.sub(r'[«»"„“”\'‚‘’]', ' ', s))).strip(' ,-–')
def op_variants(n):
    """Spellings of an operator string to look up: as is, without legal form, upper case, the part before the legal
    form ("DB Regio AG Mitte" -> "DB Regio") and the halves of "A - B", "A / B", "A (B)"."""
    v = {n, norm_op(n), n.upper(), norm_op(n).upper(), n.title(), norm_op(re.split(LEGAL, n)[0])}
    for part in re.split(r'\s+[-–/]\s+|\s*[()]\s*|/', n): v.add(norm_op(part))
    return {x for x in v if len(x) >= 3 or x == n}
RAIL_CLS = set('Q249556 Q17102188 Q18325841 Q5503 Q1268865 Q95723 Q1412403 Q14626453 Q57498564 Q7835189 Q2127330 Q187934 Q639030 Q142031 '
               'Q115267527 Q17377208 Q521458 Q3491904 Q2324835 Q844054 Q514989 Q178512 Q3565868 Q924286 Q740752 Q1192191 Q211382'.split())
RAIL_IND = {'Q3565868', 'Q178512', 'Q1370468', 'Q4127920', 'Q7590'}
RAIL_DESC = re.compile(r'(?i)\b(?:rail(?:way|road)?s?|rail transport|trains?|train operat\w*|metro|subway|underground|tram(?:way)?s?|transit|'
                       r'(?:public|urban|passenger|regional|mass|city) transport(?:ation)?|transport(?:ation)? (?:company|authority|operator|'
                       r'association|network|agency|district|undertaking|committee|department)|S-Bahn|U-Bahn|funicular|monorail|commuter)\b')
def fetch_opnames(pairs):
    """Items whose label or alias in English or the country's languages is exactly an operator string without a
    wikidata id (or that string without its legal form): ophits.json; op_by_name() picks among them."""
    hits = read('ophits.json', {})
    todo = sorted({(n, cc) for n, cc in pairs if n + '|' + cc not in hits})
    log('ophits', len(hits), 'cached,', len(todo), 'to fetch')
    for k, part in enumerate(batched(todo, 30)):
        vals, back = set(), defaultdict(set)
        for n, cc in part:
            hits[n + '|' + cc] = []
            for v in op_variants(n):
                for l in ['en', 'mul'] + LANGS.get(cc, [])[:4]:
                    vals.add(lit(v, l)); back[v].add(n + '|' + cc)
        for r in sparql('SELECT DISTINCT ?s ?i WHERE { VALUES ?s { %s } ?i rdfs:label|skos:altLabel ?s }' % ' '.join(sorted(vals))):
            for key in back.get(r['s'], ()):
                if qid(r['i']) not in hits[key]: hits[key].append(qid(r['i']))
        if k % 20 == 19: write('ophits.json', hits)
    write('ophits.json', hits)
    fetch_ops({q for qs in hits.values() for q in qs})
    return hits

def thumb(file, px=120):
    """120 px url of a Commons file: an svg's png thumbnail on upload.wikimedia.org; for a bitmap, which Commons will not
    scale up, Special:FilePath (the thumbnail, or the original when that is smaller)."""
    if not file: return ''
    f = file.replace(' ', '_'); h = hashlib.md5(f.encode()).hexdigest(); e = urllib.parse.quote(f)
    if not f.lower().endswith('.svg'): return filepath(file, px)
    return f'https://upload.wikimedia.org/wikipedia/commons/thumb/{h[0]}/{h[:2]}/{e}/{px}px-{e}.png'
def filepath(file, px=120):
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{urllib.parse.quote(file.replace(' ', '_'))}?width={px}"
def check_thumbs(files, sample=60, gap=3):
    """HTTP status of a random sample of the files at 120 px through Special:FilePath (the file exists and scales), one
    request every few seconds (upload.wikimedia.org answers scripted bursts with 429): thumbs.json."""
    import random
    st = read('thumbs.json', {})
    todo = sorted(f for f in files if f and f not in st)
    todo = random.Random(1).sample(todo, min(sample, len(todo)))
    log('thumbs', len(st), 'checked,', len(todo), 'to check')
    for f in todo:
        for t in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(filepath(f), headers={'User-Agent': UA}), timeout=30) as r: st[f] = r.status; r.read(1)
            except urllib.error.HTTPError as e:
                st[f] = e.code
                if e.code == 429: time.sleep(30 * (t + 1)); continue
            except OSError: st[f] = 0; time.sleep(5); continue
            break
        time.sleep(gap)
    write('thumbs.json', st)
    log('thumbs', dict(Counter(st.values())))
    return st

# ---------------------------------------------------------------- lookups for the build
_C = {}
def _load():
    if _C: return
    _C['items'] = read('items.json', {}); _C['ops'] = read('ops.json', {}); _C['hits'] = read('ophits.json', {}); _C['on'] = {}
    _C['places'] = read('places.json', {}); _C['thumbs'] = read('thumbs.json', {}); _C['near'] = {}
def item(q):
    """(en, {lang: label}, lon, lat, is_station) of a wikidata= tag's item, redirects followed; None when unknown or gone."""
    _load()
    for _ in range(3):
        g = _C['items'].get(q)
        if not g: return None
        if g[5] and not g[1] and not g[0]: q = g[5]; continue
        return g[0], g[1], g[2], g[3], g[4]
    return None
_GRID = 0.01
def nearby(lon, lat, cc, r=500):
    """[(metres, qid, en, {lang: label})] of the country's Wikidata stations within r metres, nearest first."""
    _load()
    if cc not in _C['near']:
        grid = defaultdict(list)
        for g in read(os.path.join('near', cc + '.json'), []): grid[(int(g[1] // _GRID), int(g[2] // _GRID))].append(g)
        _C['near'][cc] = grid
    grid, out = _C['near'][cc], []
    x, y, k = int(lon // _GRID), int(lat // _GRID), 1 + int(r / 1000 / (_GRID * 111 * max(0.2, math.cos(math.radians(lat)))))
    for i in range(x - k, x + k + 1):
        for j in range(y - k, y + k + 1):
            for g in grid.get((i, j), ()):
                d = metres(lon, lat, g[1], g[2])
                if d <= r: out.append((d, g[0], g[3], g[4]))
    return sorted(out, key=lambda o: o[0])
def places(name, cc):
    _load()
    return [tuple(p) for p in _C['places'].get(name + '|' + cc, [])]
def logo_url(file):
    """thumb() of a logo file; '' for none, a file whose url was checked and failed, or a photo given as the logo (a JPEG
    without "logo" in its name)."""
    _load()
    if not file or re.search(r'(?i)\.jpe?g$', file) and not re.search(r'(?i)logo|emblem|symbol|wordmark', file): return ''
    return thumb(file) if _C['thumbs'].get(file, 200) == 200 else ''
COMPANY = {'Q431289', 'Q4830453', 'Q6881511', 'Q783794', 'Q891723', 'Q270791', 'Q658255', 'Q167037'}     # brand, business, company ...
RAIL_NAME = re.compile(r'(?i)tren|train|rail|bahn|metro|tram|ferrocarril|ferrovi|železn|zelezn|kolej|vlak|trein|\btog|tåg|juna|vonat|'
                       r'chemin|comboio|caminho de ferro|поезд|железн|пригород|метро|трамва')
def railish(o):
    """A rail / public transport company, network or authority (not the city or state that owns one): by class, industry
    or description, or a company / brand with a rail word in its name ("Trenes Argentinos")."""
    return bool(o) and bool(set(o['cls']) & RAIL_CLS or set(o['ind']) & RAIL_IND or RAIL_DESC.search(o['desc']) or
                            set(o['cls']) & COMPANY and RAIL_NAME.search(o['en']))
def op(q):
    """(English label, logo url) of an operator / network / brand item; the logo of its parent organisation, owner or
    operator (one level, when that is a transport company too) when it has none of its own."""
    _load()
    o = _C['ops'].get(q) or {}
    if o.get('r'): o = _C['ops'].get(o['r']) or o
    if not o: return '', ''
    url = logo_url(o.get('logo') or o.get('small'))
    for p in ([] if url else o.get('parent', [])):
        po = _C['ops'].get(p) or {}
        url = logo_url(po.get('logo') or po.get('small')) if railish(po) else ''
        if url: break
    return o.get('en', ''), url
def wiki(q):
    """The English Wikipedia article of an operator item without a Commons logo, or of its transport-company parent,
    whose infobox logo the site can show ({name, wiki} in data/logos.json); '' when it has a logo or no article."""
    _load()
    if 'wiki' not in _C: _C['wiki'] = read('wiki.json', {})
    o = _C['ops'].get(q) or {}
    if o.get('r'): q, o = o['r'], _C['ops'].get(o['r']) or {}
    if not o or op(q)[1]: return ''
    return _C['wiki'].get(q) or next((_C['wiki'].get(p) for p in o.get('parent', []) if railish(_C['ops'].get(p)) and _C['wiki'].get(p)), '')
NOT_OP = {'Q16695773', 'Q4167836', 'Q13406463', 'Q4167410', 'Q55488', 'Q928830', 'Q728937', 'Q15079663'}   # project, category, list, disambiguation, station, line
def op_by_name(name, cc):
    """The rail operator / network item of this name in this country: among the items labelled so (ophits.json), the
    transport companies, networks and authorities (railish) of this country or of none, a rail class before a rail
    industry before a rail description; of a tie the ones with a logo, then the one whose English label is the name,
    or any when they all show the same logo; '' when still ambiguous."""
    _load()
    k = name + '|' + cc
    if k not in _C['on']:
        if 'iso' not in _C: _C['iso'] = {v: k2 for k2, v in read('countries.json', {}).items()}
        ok = {}
        for q in _C['hits'].get(k, []):
            o = _C['ops'].get(q) or {}
            if o.get('r'): q, o = o['r'], _C['ops'].get(o['r']) or {}
            if railish(o) and not set(o['cls']) & NOT_OP and (not o['cc'] or cc in {_C['iso'].get(c) for c in o['cc']}):
                ok[q] = 3 if set(o['cls']) & RAIL_CLS else 2 if set(o['ind']) & RAIL_IND else 1
        top = [q for q in ok if ok[q] == max(ok.values())]
        if len(top) > 1: top = [q for q in top if (_C['ops'][q].get('logo') or _C['ops'][q].get('small'))] or top
        if len(top) > 1: top = [q for q in top if _C['ops'][q]['en'].lower() in (name.lower(), norm_op(name).lower())] or top
        if len({_C['ops'][q].get('logo') or _C['ops'][q].get('small') for q in top}) == 1: top = top[:1]     # a company and its network, one logo
        _C['on'][k] = top[0] if len(top) == 1 else ''
    return _C['on'][k]

def fetch_wiki(qs):
    """English Wikipedia article of each item (for the logos Commons does not have: the site reads the article's infobox
    logo, e.g. a non-free logo on en.wikipedia): wiki.json {qid: title or ''}."""
    wk = read('wiki.json', {})
    todo = sorted(set(qs) - set(wk), key=lambda q: int(q[1:]))
    log('wiki', len(wk), 'cached,', len(todo), 'to fetch')
    for part in batched(todo, 300):
        for q in part: wk[q] = ''
        for r in sparql('SELECT ?i ?t WHERE { VALUES ?i { %s } ?a schema:about ?i; schema:isPartOf <https://en.wikipedia.org/>; schema:name ?t }'
                        % ' '.join('wd:' + q for q in part)):
            wk[qid(r['i'])] = r['t']
    write('wiki.json', wk)
    return wk
# an infobox's logo: its logo / logo_filename, or an image that is one ("ManchesterMetrolinkLogo.svg", an svg that is no map)
INFOBOX_LOGO = re.compile(r'\|\s*(?:logo|logo_filename|logo_file|logo_image|image_logo)\s*=\s*(?:\[\[)?\s*(?:File:|Image:)?\s*([^|\]\n{}=]+?\.(?:svg|png|jpe?g|gif))|'
                          r'\|\s*image\s*=\s*(?:\[\[)?\s*(?:File:|Image:)?\s*([^|\]\n{}=]*?(?:logo|emblem|wordmark|roundel)[^|\]\n{}=]*?\.(?:svg|png|jpe?g|gif)|'
                          r'(?![^|\]\n{}=]*?(?:map|karte|plan|diagram|route|network|scheme|schema|carte|mapa))[^|\]\n{}=]+?\.svg)', re.I)
def wapi(params, tries=5):
    """One English Wikipedia API request, ~1 s after the last, backing off on 429 / 5xx."""
    for k in range(tries):
        time.sleep(max(0, 1.0 - (time.time() - _last[0])))
        url = 'https://en.wikipedia.org/w/api.php?' + urllib.parse.urlencode(dict(params, format='json', formatversion=2))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=60) as r: d = json.load(r)
            _last[0] = time.time(); return d
        except urllib.error.HTTPError as e:
            _last[0] = time.time()
            if e.code in (429, 500, 502, 503, 504): time.sleep(int(e.headers.get('Retry-After') or 10) + 10 * k); continue
            raise
        except (TimeoutError, OSError, ValueError) as e:
            _last[0] = time.time(); log('  retry', type(e).__name__, str(e)[:80]); time.sleep(10 * (k + 1))
    return {}
def fetch_wikilogos(titles):
    """wikilogos.json {English Wikipedia title: 120 px url of the logo in its infobox, or ''}: the logos of wiki()
    articles (often non-free files on en.wikipedia), resolved once here instead of by the site at runtime; 50 articles
    and 50 files per request (the API answers bursts with 429)."""
    wl = read('wikilogos.json', {})
    todo = sorted(set(titles) - set(wl) - {''})
    log('wikilogos', len(wl), 'cached,', len(todo), 'to fetch')
    def mapped(q, xs):          # requested title -> the page title the answer uses (normalised, redirected)
        m = {n['from']: n['to'] for n in q.get('normalized', [])}; r = {n['from']: n['to'] for n in q.get('redirects', [])}
        return {x: r.get(m.get(x, x), m.get(x, x)) for x in xs}
    files, done = {}, []
    for part in batched(todo, 50):
        q = wapi({'action': 'query', 'prop': 'revisions', 'rvprop': 'content', 'rvslots': 'main', 'redirects': 1, 'titles': '|'.join(part)}).get('query') or {}
        text = {p['title']: ((p.get('revisions') or [{}])[0].get('slots') or {}).get('main', {}).get('content', '') for p in q.get('pages', [])}
        if text: done += part                  # not cached when the request failed
        for t, pt in mapped(q, part).items():
            m = INFOBOX_LOGO.search(text.get(pt, '')[:15000])        # the lead infobox, not a template further down
            if m: files[t] = 'File:' + next(g for g in m.groups() if g).strip()
    url = {}
    for part in batched(sorted(set(files.values())), 50):
        q = wapi({'action': 'query', 'prop': 'imageinfo', 'iiprop': 'url', 'iiurlwidth': 120, 'titles': '|'.join(part)}).get('query') or {}
        got = {p['title']: (p.get('imageinfo') or [{}])[0].get('thumburl', '').split('?')[0].replace('//thumb.wikimedia.org/', '//upload.wikimedia.org/')
               for p in q.get('pages', [])}
        url.update({f: got.get(pt, '') for f, pt in mapped(q, part).items()})
    for t in done: wl[t] = url.get(files.get(t), '')
    write('wikilogos.json', wl); log('wikilogos', sum(1 for v in wl.values() if v), 'of', len(wl), 'with a logo')
    return wl
def wiki_logo(title):
    """120 px url of the infobox logo of an English Wikipedia article (wikilogos.json): '' when it has none, None when
    it was not looked up."""
    if 'wl' not in _C: _C['wl'] = read('wikilogos.json', {})
    return _C['wl'].get(title)

def national(rels, country_code):
    """national.json {CC: [qid, url, share, n]}: the operator whose logo most of a country's train services show, when it
    is 90%+ of the 20+ train relations whose operator is known - the logo for its lines that name no operator."""
    _load(); cnt = defaultdict(Counter); qof = {}
    for r in rels:
        t = r['tags']
        if t.get('type') != 'route' or t.get('route') != 'train' or not r.get('center'): continue
        cc = country_code(*r['center'])
        q = (t.get('operator:wikidata') or '').split(';')[0].strip() or op_by_name((t.get('operator') or '').split(';')[0].strip(), cc)
        url = op(q)[1] if q else ''
        if url: cnt[cc][url] += 1; qof[url] = q
    out = {}
    for cc, c in cnt.items():
        (url, k), n = c.most_common(1)[0], sum(c.values())
        if n >= 20 and k >= 0.9 * n and cc not in ('CN', 'HK', 'MO'): out[cc] = [qof[url], url, round(k / n, 2), n]
    write('national.json', out); log('national', out)
    return out
def default_operator(cc):
    """(qid, logo url) of the operator of nearly all of a country's train services (national.json), or ('', '')."""
    _load()
    if 'nat' not in _C: _C['nat'] = read('national.json', {})
    return tuple(_C['nat'].get(cc, ['', ''])[:2])

# ---------------------------------------------------------------- run
def main():
    args = [a for a in sys.argv[1:]]
    if args[:1] == ['wikilogos']:          # wikidata.py wikilogos <network.json | full.json> ...: the logo keys the lines use
        keys = {l[14][3:] for f in args[1:] for l in json.load(open(f))['lines'] if l[14].startswith('wd:')}
        return fetch_wikilogos({wiki(q) for q in keys})
    steps = [a for a in args if a in ('classes', 'items', 'near', 'places', 'ops', 'logos', 'summary')] or \
            ['classes', 'items', 'near', 'places', 'ops', 'logos', 'summary']
    files = [a for a in args if a not in steps]
    nodes = pickle.load(open(files[0], 'rb')); nodes = nodes.get('stations', nodes) if 'stations' in nodes else nodes
    rels = json.load(open(files[1])); full = json.load(open(files[2])) if len(files) > 2 else None
    from countries import country_code
    import english
    country_qids()
    if 'classes' in steps: fetch_classes()
    if 'items' in steps: fetch_items(nodes)
    # the stations whose names need an English form: named, not Latin, station-like
    need = [(n, lon, lat, t) for n, (lon, lat, t) in nodes.items() if t.get('name') and not latin(t['name']) and english.stationish(t)]
    ccs = Counter(); names = set()
    for n, lon, lat, t in need:
        cc = country_code(lon, lat); ccs[cc] += 1; sc = english.script(t['name'])
        if sc in english.WEAK and (sc != 'CJK' or cc in ('JP', 'TW')): names.add((english.base(english.stn_clean(t['name'])), cc))
    log('non-Latin station nodes', len(need), dict(ccs.most_common()))
    if 'near' in steps: fetch_near([cc for cc, k in ccs.most_common() if cc not in ('CN', 'HK', 'MO') and k >= 20])
    if 'places' in steps: fetch_places({(n, cc) for n, cc in names if n and not latin(n)})
    if 'ops' in steps or 'logos' in steps:
        qs, pairs = set(), set()      # operator / network / brand items of the lines, and names without one
        for r in rels:
            t = r['tags']
            if t.get('type') == 'route' and t.get('route') not in english.MODES: continue
            cc = country_code(*r['center']) if r.get('center') else ''
            for k in OPKEYS:
                qs.update(q.strip() for q in (t.get(k + ':wikidata') or '').split(';') if re.fullmatch(r'Q\d+', q.strip()))
                if not t.get(k + ':wikidata'):
                    for v in {(t.get(k) or '').split(';')[0].strip(), (t.get(k + ':en') or '').split(';')[0].strip()} - {''}:
                        if cc: pairs.add((v, cc))
        if full:
            C = [c[0] for c in full['countries']]
            for l in full['lines']:
                if l[13]: pairs.add((l[13], C[l[12]]))
                if l[14].startswith('wd:'): qs.add(l[14][3:])
        fetch_ops(qs)
        hits = fetch_opnames(pairs); _C.clear()
        used = (qs | {op_by_name(*k.rsplit('|', 1)) for k in hits}) - {''}
        ops = read('ops.json', {})
        fetch_ops({p for q in used for p in (ops.get(q) or {}).get('parent', [])}); _C.clear()
        ops = read('ops.json', {})
        fetch_wiki({x for q in used for x in [q, (ops.get(q) or {}).get('r')] + (ops.get(q) or {}).get('parent', []) if x}); _C.clear()
        national(rels, country_code)
    if 'logos' in steps:
        ops = read('ops.json', {})
        used |= {(ops.get(q) or {}).get('r') for q in used} - {'', None}
        files = {(ops.get(p) or {}).get('logo') or (ops.get(p) or {}).get('small') for q in used for p in [q] + (ops.get(q) or {}).get('parent', [])}
        check_thumbs(files - {'', None})
    if 'summary' in steps: summary()

def summary():
    items, ops, hits, th = read('items.json', {}), read('ops.json', {}), read('ophits.json', {}), read('thumbs.json', {})
    near = {f[:-5]: len(read(os.path.join('near', f), [])) for f in sorted(os.listdir(path('near')))} if os.path.isdir(path('near')) else {}
    s = {'items': len(items), 'items_gone': sum(1 for g in items.values() if not g), 'items_en': sum(1 for g in items.values() if g and g[0]),
         'items_station_class': sum(1 for g in items.values() if g and g[4]), 'near_countries': len(near), 'near_items': sum(near.values()), 'near': near,
         'places_names': len(read('places.json', {})), 'places_found': sum(1 for v in read('places.json', {}).values() if v),
         'ops': len(ops), 'ops_logo': sum(1 for o in ops.values() if o and (o.get('logo') or o.get('small'))),
         'ops_logo_or_parent': sum(1 for q in ops if op(q)[1]),
         'opnames': len(hits), 'opnames_matched': sum(1 for k in hits if op_by_name(*k.rsplit('|', 1))),
         'thumbs_checked': len(th), 'thumbs_ok': sum(1 for v in th.values() if v == 200), 'national': read('national.json', {})}
    write('summary.json', s); log(json.dumps({k: v for k, v in s.items() if k != 'near'}))

if __name__ == '__main__':
    main()
