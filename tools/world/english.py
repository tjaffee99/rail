"""English names of stations, lines and operators for tools/world/build_world.py, from OSM tags, the Wikidata caches of
tools/world/wikidata.py (read only; run that first) and transliteration.

    from english import english_station, english_station_src, english_line, english_operator, latinize

english_station(native, tags, lon, lat, cc) -> str
    English name of a station node: native is its cleaned native name (build_world.stn_name), tags its OSM tags, cc its
    ISO country. A Latin name is returned as it is; for other scripts the first of
      wd-tag    the English label of the node's wikidata= item: a station item within 1.5 km (whose native label, if it
                has one in the name's script, is like the name, or whose English label is like the transliteration), or
                any item within 3 km whose native label is the name; labels cleaned of "Station", "railway station",
                "(Tokyo Metro)" ... ("Shin-Ōsaka Station" -> "Shin-Osaka": no macrons in Japan)
      wd-near   the English label of a Wikidata station within 500 m whose native label is the name (0.85 similar)
      tag       name:en, official_name:en, int_name, name:<lang>-Latn / _rm / name:latin, the Latin part of a mixed name
                ("कुबेरपुर (Kuberpur)"); for Arabic, Hebrew, Thai ... scripts also name:fr / de / es / it / pt
      place     for scripts whose transliteration guesses (Arabic, Hebrew, Brahmic, Thai, Lao, Khmer, Burmese, Ethiopic,
                kanji, Taiwanese Chinese): the English label of the nearest item within 10 km whose native label is the
                name (the town or quarter a station is named after)
      translit  latinize()
english_station_src(...) -> (name, source)    the same and its source: latin wd-tag wd-near tag place translit
english_line(native, tags, cc) -> str         line / route name: name:en, int_name, name:<lang>-Latn, else latinize()
                                              ("1호선" / "1号线" -> "Line 1", "สายสีม่วง" -> "Purple Line")
english_operator(name, q, cc, kind='') -> (name, logo url, qid, wiki)
    name: the curated English name (ref/operators.json), the operator's own name when Latin, else its Wikidata English
    label, else latinize(); q: its wikidata id, else the item of that name in that country (wikidata.op_by_name: the
    curated one, exact labels, the entity search), else for a train line (kind h r s) that names no operator the
    country's national operator (national_operator); a non-Latin name without an English label is transliterated without
    its legal form ("АО «ФПК»" -> "FPK"); logo url: 120 px (wikidata.op: Commons, curated file, English then native
    Wikipedia infobox, parent company's) or ''; wiki: when there is no logo url, the English Wikipedia article whose
    infobox logo the site can look up ({name, wiki} entry of data/logos.json), else ''. The name stays '' for a line that
    names no operator.
line_operator(op, q, ts, cc, kind, dom, stn) -> (name, logo url, qid, wiki)
    the operator of a line (build_world.py): the first of build_world's choice, the other operator / network / brand tags
    (line_values: fare associations, infrastructure managers and generic words left out) and the leading operator of its
    stations' nodes whose item has a logo; a domestic train that names none: the national operator (national_operator:
    ref/operators.json "national", else wikidata.default_operator). infra_network(cc, kind, stn, ts): the operator of the
    regional trains on the national network (ref/operators.json "infra": Trenitalia on RFI's stations, Renfe on Adif's).
latinize(s, cc) -> str
    Readable Latin form of any name, never '' for a non-empty name and never with non-Latin letters left: translit.py
    for Japanese, Chinese, Korean, Cyrillic, Greek; here Thai (pythainlp's thai2rom_onnx model on segmented words, its
    royin rules without onnxruntime), Devanagari / Bengali / Gurmukhi / Gujarati / Oriya (IAST, inherent vowels dropped
    as spoken), Telugu / Kannada / Malayalam / Sinhala (IAST), Arabic / Persian / Urdu and Hebrew (letters with the
    short vowels guessed), Armenian, Georgian, Ethiopic, Tamil, Lao, Khmer, Burmese (anyascii). Words of a second script
    are dropped ("Жанжин клуб ᠵᠠᠩᠵᠤᠨ"), station words dropped and junction / km words in English ("ত্রিমোহনী জংশন" ->
    "Trimohni Junction").
Also: script(s) -> main script of a name ('LATIN', 'CYRILLIC', 'JAPANESE', 'CJK', 'HANGUL', 'ARABIC', ...),
    base(name) -> the name without station words (for comparing and for wikidata.fetch_places), stationish(tags), MODES,
    stn_clean(name) = build_world.stn_name({'name': name}).
Needs: pip install pythainlp onnxruntime indic_transliteration (plus translit.py's jieba pykakasi pypinyin anyascii).
"""
import difflib, os, re, sys, unicodedata
from collections import Counter
from functools import lru_cache
from anyascii import anyascii
sys.path.insert(0, os.path.dirname(__file__))
from translit import translit, latin, plain, cap
import wikidata as wd

MODES = ('train', 'subway', 'light_rail', 'tram', 'monorail', 'funicular', 'railway')
_STN = {}
def stn_clean(n):
    """build_world.stn_name() of a name (for fetching what the build will ask), taken from its source so the two never
    drift: build_world.py cannot be imported (it loads the planet at module level)."""
    if not _STN:
        import ast
        from translit import ABJAD, TIFINAGH
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'build_world.py')).read()
        _STN.update(re=re, ABJAD=ABJAD, TIFINAGH=TIFINAGH)
        for node in ast.parse(src).body:
            names = {node.name} if isinstance(node, ast.FunctionDef) else {t.id for t in getattr(node, 'targets', []) if isinstance(t, ast.Name)}
            if names & {'first', 'STN_WORDS', 'PLATFORM', 'stn_name'}: exec(ast.get_source_segment(src, node), _STN)
    return _STN['stn_name']({'name': n})
def stationish(t):
    return t.get('railway') in ('station', 'halt', 'tram_stop', 'stop', 'platform') or t.get('public_transport') in ('station', 'stop_position', 'platform')

@lru_cache(maxsize=None)
def _chscript(ch):
    try: w = unicodedata.name(ch).split()
    except ValueError: return ''
    if w[0] in ('HIRAGANA', 'KATAKANA') or ch in '々〆ヶ': return 'JAPANESE'
    if w[0] == 'CJK' or w[:2] == ['KANGXI', 'RADICAL']: return 'CJK'
    return {'MODIFIER': 'LATIN', 'OLD': '', 'NEW': 'TAI'}.get(w[0], w[0]) if ch.isalpha() else ''
def script(s):
    """Main script of a name: most of its letters (Japanese when it has kana)."""
    n = {}
    for ch in s or '':
        k = _chscript(ch)
        if k: n[k] = n.get(k, 0) + 1
    if 'JAPANESE' in n: return 'JAPANESE'
    return max(n, key=n.get) if n else 'LATIN'
WEAK = {'ARABIC', 'HEBREW', 'SYRIAC', 'THAANA', 'THAI', 'LAO', 'KHMER', 'MYANMAR', 'ETHIOPIC', 'DEVANAGARI', 'BENGALI', 'GURMUKHI', 'GUJARATI',
        'ORIYA', 'TAMIL', 'TELUGU', 'KANNADA', 'MALAYALAM', 'SINHALA', 'TIBETAN', 'MONGOLIAN', 'JAPANESE', 'CJK'}
MAIN = {'MN': 'CYRILLIC', 'LA': 'LAO', 'CN': 'CJK', 'TW': 'CJK', 'HK': 'CJK', 'MO': 'CJK', 'JP': 'JAPANESE', 'KZ': 'CYRILLIC', 'KH': 'KHMER',
        'MM': 'MYANMAR', 'TH': 'THAI', 'IL': 'HEBREW', 'IN': 'DEVANAGARI', 'BD': 'BENGALI', 'LK': 'SINHALA', 'GE': 'GEORGIAN', 'AM': 'ARMENIAN'}

# ---------------------------------------------------------------- station words
STATION_WORDS = re.compile(
    r'^(?:สถานี(?:รถไฟ)?|ที่หยุดรถ(?:ไฟ)?|ป้ายหยุดรถ(?:ไฟ)?|ສະຖານີ(?:ລົດໄຟ)?|ស្ថានីយ៍(?:រថភ្លើង)?|محطة(?:\s*(?:قطار|القطار|مترو|سكة الحديد))?|ایستگاه(?:\s*(?:راه\s*آهن|مترو|قطار))?|'
    r'(?:תחנת(?:\s*רכבת)?|станция|станція|гара|σταθμός)\s)\s*|'
    r'\s*(?:駅|车站|車站|站|역|ရထားဘူတာ(?:ရုံ)?|ဘူတာ(?:ရုံ)?|রেলওয়ে স্টেশন|রেল স্টেশন|স্টেশন|रेलवे स्टेशन|रेल्वे स्टेशन|स्टेशन|ரயில் நிலையம்|நிலையம்|'
    r'రైల్వే స్టేషన్|ರೈಲು ನಿಲ್ದಾಣ|റെയിൽവേ സ്റ്റേഷൻ|දුම්රිය ස්ථානය|ස්ථානය|სადგური|կայարան|կառամատույց|ጣቢያ|өртөө|'
    r'zheleznodorozhnaya stantsiya|railway station)$', re.I)
def base(n):
    """A name without its station words ("ស្ថានីយ៍ភ្នំពេញ" -> "ភ្នំពេញ", "新宿駅" -> "新宿")."""
    n = re.sub(r'\s+', ' ', n or '').strip()
    for _ in range(2):
        m = STATION_WORDS.sub('', n).strip(' -–')
        if len(m) >= 2: n = m
    return n
@lru_cache(maxsize=200000)
def key(s):
    """Comparison key: lower case, no spaces, punctuation or station words, variant letters folded."""
    s = base(re.sub(r'[(（][^()（）]*[)）]', '', s or '')).lower()
    s = s.translate(str.maketrans('臺ёйأإآىةیكۀ', '台еиااايهيکه'))
    return re.sub(r'[\W_]+', '', s)
def sim(a, b):
    a, b = key(a), key(b)
    if not a or not b: return 0.0
    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()
def asc(s): return re.sub(r'[^a-z0-9]', '', anyascii(plain(s or '')).lower())

# ---------------------------------------------------------------- Wikidata labels
LABEL_TAIL = re.compile(
    r'(?i)(?:\s+|\s*-\s*)(?:(?:railway|railroad|rail|train|metro|subway|underground|MRT|LRT|BTS|SRT|ARL|KTM|MTR|MRTS|'
    r'light[ -]rail|tram|tramway|monorail|funicular|commuter|S-Bahn|U-Bahn|rapid transit|high[ -]speed(?: rail(?:way)?)?|passenger|suburban|'
    r'elevated|interchange|metro and railway|railway and metro)\s+)?'
    r'(?:stati+on|stop|halt|platform|stopping point|passing loop|passing point|block post|train station|rail station|railway terminal|'
    r'rail terminal|train terminal|terminus|stn\.?)$')
KEEP_TAIL = re.compile(r'(?i)\b(?:bus|coach|police|fire|power|pumping|filling|gas|petrol|research|radio|space|polar|lifeboat|old|new)$')
LABEL_HEAD = re.compile(r'(?i)^(?:(?:railway|train|metro|tram|subway)\s+(?:station|halt|stop)|stopping point)\s+(?=\S)')
GENERIC_EN = re.compile(r'(?i)^(?:the\s+)?(?:(?:railway|train|metro|tram|subway|light rail)\s+)?(?:station|halt|stop|platform|terminal|junction|depot|yard|line)s?$|'
                        r'^(?:railway|railroad|rail|train|metro|subway|tram|light rail)$')
def clean_label(label, cc=''):
    """A Wikidata English label as a station name: "Kyiv-Pasazhyrskyi railway station" -> "Kyiv-Pasazhyrskyi",
    "Mitsukoshimae Station (Tokyo Metro)" -> "Mitsukoshimae", "Taipei Main Station" stays; '' when nothing is left."""
    s = label = re.sub(r'\s+', ' ', label or '').strip()
    s = re.sub(r'\s*[(\[][^()\[\]]*[)\]]', '', s).strip()                   # "(Tokyo Metro)", "(Line 2)", "(Saitama)"
    s = re.sub(r'(?i)(\s(?:railway |train |rail |metro )?(?:station|halt|stop|platform|passing loop)),\s.*$', r'\1', s)     # "…station, Bashkortostan"
    s = re.sub(r',\s*[^,]*\b(?:District|Oblast|Region|Krai|Raion|Province|Prefecture|Governorate|Republic|Municipality|County)$', '', s)   # "90 km, Selizharovsky District"
    if not re.search(r'(?i)\b(?:main|central|union|grand central)\s+station$', s):
        for _ in range(2):
            x = LABEL_TAIL.sub('', s).strip(' ,-–')
            if not KEEP_TAIL.search(x): s = x                                   # "Bus station" stays
    s = LABEL_HEAD.sub('', s).strip(' ,-–')
    s = re.sub(r'\s+(?:TRA|T?HSR|MRT|LRT|BTS|SRT|KTX|MTR|KTM|BRT|JR|Signal)$', '', s)                  # "Hsinchu TRA", "Taichung HSR"
    if re.fullmatch(r'["«“„][^"«»“”„]*["»”“]', s): s = s[1:-1]                                         # «Grazhdansky prospekt»
    if re.fullmatch(r'(?i)(?:new|old|central|main|north|south|east|west|upper|lower|grand|union)(?:\s+railway)?', s):
        s = re.sub(r'\s*[(\[][^()\[\]]*[)\]]|,.*$', '', label).strip()                              # "New railway station" stays
    if cc == 'JP': s = plain(s)
    if not s or not latin(s) or not re.search(r'[A-Za-z]{2}', s) or GENERIC_EN.match(s) or re.match(r'[a-z]', s): return ''
    return s
PLACE_TAIL = re.compile(r'(?i)\s+(?:rural district|district|subdistrict|province|governorate|municipality|county|upazila|tehsil|taluka?|'
                        r'ward|neighbou?rhood|village|township|(?:city|town|village) (?:council|district))$')
def clean_place(s, cc=''):
    s = re.sub(r'\s*[(\[][^()\[\]]*[)\]]', '', s or '').split(',')[0].strip()
    s = re.sub(r'\s+(?:city|town|village)$', '', PLACE_TAIL.sub('', s)).strip()          # "Kafr El-Battikh city"
    if cc == 'JP': s = plain(s)
    return s if s and latin(s) and re.search(r'[A-Za-z]{2}', s) and len(s.split()) <= 5 and not GENERIC_EN.match(s) else ''
def same_script_labels(labels, sc):
    return [v for v in (labels or {}).values() if script(v) == sc or (sc in ('JAPANESE', 'CJK') and script(v) in ('JAPANESE', 'CJK'))]

def _asim(a, b): return difflib.SequenceMatcher(None, asc(a), asc(b)).ratio()
RELIABLE = {'CYRILLIC', 'GREEK', 'HANGUL', 'GEORGIAN', 'ARMENIAN'}         # scripts whose transliteration is the usual English form
ENGLISH_WORD = re.compile(r'(?i)\b(?:street|square|avenue|lane|road|highway|bridge|park|garden|gardens|plant|factory|works|mill|mine|school|'
                          r'college|institute|university|academy|hospital|clinic|polyclinic|market|shop|store|centre|center|mall|theat(?:er|re)|'
                          r'stadium|museum|library|cemetery|church|monastery|district|microdistrict|quarter|settlement|village|town|city|'
                          r'island|lake|river|dam|port|harbou?r|airport|terminal|depot|station|line|house|hall|palace|monument|memorial|'
                          r'gate|embankment|boulevard|prospect|highway|crossing|junction|farm|camp|base|pass|hill|forest|beach|bay|new|old|'
                          r'north|south|east|west|upper|lower|central|main|railway|company|complex|cinema|pool|administration|office|'
                          r'department|agency|overpass|combine|yard|stop|circle|ring|field|grove)\b')
def stale(name, native, labels, t, cc, sc):
    """An English label that is not this name: left from a former name - it reads like the old_name tag or like another
    language's label that is not the name ("Новая Охта" whose English label is still "Murino") - or, in a script with a
    usual transliteration, neither like it nor a descriptive translation - two or more words with an English noun
    ("Lenin Street" stays; "Youth" for Молодёжная and "Feed" for Лента go) - or a Turkic-language label given as English."""
    tr = _asim(name, latinize(native, cc))
    if tr >= 0.5: return False
    old = [t.get('old_name', '')] + [v for v in same_script_labels(labels, sc) if sim(native, v) < 0.5]
    if any(o and _asim(name, latinize(o, cc)) >= 0.8 for o in old): return True
    if cc in ('RU', 'UA') and re.search('[ıİñşğâäöüç]', name): return True                    # a Tatar / Crimean Tatar label
    return sc in RELIABLE and tr < 0.35 and not re.fullmatch(r'[A-Z0-9\W]+', name) and not (len(name.split()) >= 2 and ENGLISH_WORD.search(name))
def _from_tag(native, t, lon, lat, cc, sc):
    for q in (t.get('wikidata') or '').split(';'):
        it = wd.item(q.strip()) if re.fullmatch(r'Q\d+', q.strip()) else None
        if not it or not it[0]: continue
        en, labels, ilon, ilat, is_stn = it
        name = clean_label(en, cc)
        if not name: continue
        d = wd.metres(lon, lat, ilon, ilat) if ilon is not None else None
        nat = max((sim(native, v) for v in same_script_labels(labels, sc)), default=None)
        if stale(name, native, labels, t, cc, sc): continue
        if is_stn and (d is None or d <= 1500):
            if nat is None or nat >= 0.5: return name
            if _asim(name, latinize(native, cc)) >= 0.5: return name
        elif d is not None and d <= 3000 and nat is not None and nat >= 0.8: return name
    return ''
def _from_near(native, t, lon, lat, cc, sc):
    best = None
    for d, q, en, labels in wd.nearby(lon, lat, cc, 500):
        name = clean_label(en, cc)
        if not name: continue
        s = max((sim(native, v) for v in same_script_labels(labels, sc)), default=0)
        if s >= 0.85 and (best is None or (s, -d) > best[:2]) and not stale(name, native, labels, t, cc, sc): best = (s, -d, name)
    return best[2] if best else ''
def latin_part(n):
    """The Latin half of a bilingual name ("कुबेरपुर (Kuberpur)", "Kasba Peth कसबा पेठ"): a run of whole Latin words, 4+ letters
    and a quarter of the letters; not a code glued to the name ("YRP野比", "Minsk-Паўднёвы")."""
    toks = [x for x in re.split(r'[\s()（）/|]+', n) if x]
    runs, cur = [], []
    for x in toks + ['\x00']:
        if x != '\x00' and latin(x) and re.search(r'[A-Za-z]', x): cur.append(x)
        elif cur: runs.append(cur); cur = []
    if not runs or len(runs) == 1 and len(runs[0]) == len(toks): return ''
    best = ' '.join(max(runs, key=lambda r: len(''.join(r))))
    letters = lambda s: len(re.sub(r'\W|\d|_', '', s))
    return best if letters(best) >= 4 and letters(best) >= 0.25 * letters(n) else ''
TAG_KEYS = ('name:en', 'official_name:en', 'int_name')
LATN_KEY = re.compile(r'^name:[a-z]{2,3}(?:[-_](?:Latn|rm|pinyin|tr|translit|ALA-LC|BGN))$|^name:latin$')
def _from_tags(native, t, sc):
    for k in TAG_KEYS + tuple(sorted(k for k in t if LATN_KEY.match(k))):
        v = re.sub(r'\s+', ' ', (t.get(k) or '').split(';')[0]).strip()
        if v and latin(v) and re.search(r'[A-Za-z]', v) and not GENERIC_EN.match(v): return clean_label(v) or v
    v = latin_part(native)
    if v: return clean_label(v) or v
    if sc in WEAK and sc not in ('JAPANESE', 'CJK'):
        for k in ('name:fr', 'name:de', 'name:es', 'name:it', 'name:pt'):
            v = (t.get(k) or '').split(';')[0].strip()
            if v and latin(v) and re.search(r'[A-Za-z]', v): return v
    return ''
def _from_place(native, lon, lat, cc):
    best = None
    for q, plon, plat, en in wd.places(base(native), cc) + (wd.places(native, cc) if base(native) != native else []):
        d = wd.metres(lon, lat, plon, plat); name = clean_place(en, cc)
        if name and d <= 10000 and (best is None or d < best[0]): best = (d, name)
    return best[1] if best else ''

def english_station_src(native, tags=None, lon=None, lat=None, cc=''):
    native, t = re.sub(r'\s+', ' ', native or '').strip(), tags or {}
    if not native: return '', ''
    sc = script(native)
    if latin(native) or all(_chscript(c) == 'LATIN' for c in native if _chscript(c)) and any(_chscript(c) for c in native): return native, 'latin'
    if lon is not None:
        v = _from_tag(native, t, lon, lat, cc, sc)
        if v: return v, 'wd-tag'
        v = _from_near(native, t, lon, lat, cc, sc)
        if v: return v, 'wd-near'
    v = _from_tags(native, t, sc)
    if v: return v, 'tag'
    if not any(_chscript(c) for c in native): return unicodedata.normalize('NFKC', native), 'latin'       # "２１"
    if lon is not None and (sc in WEAK and not (sc == 'CJK' and cc not in ('TW', 'JP'))):
        v = _from_place(native, lon, lat, cc)
        if v: return v, 'place'
    return latinize(native, cc), 'translit'
def english_station(native, tags=None, lon=None, lat=None, cc=''): return english_station_src(native, tags, lon, lat, cc)[0]

TH_COLOUR = {'แดง': 'Red', 'เขียว': 'Green', 'น้ำเงิน': 'Blue', 'ม่วง': 'Purple', 'ชมพู': 'Pink', 'เหลือง': 'Yellow', 'ส้ม': 'Orange', 'ทอง': 'Gold',
             'เขียวอ่อน': 'Light Green', 'เขียวเข้ม': 'Dark Green', 'แดงอ่อน': 'Light Red', 'แดงเข้ม': 'Dark Red', 'ฟ้า': 'Light Blue', 'เทา': 'Grey'}
def english_line(native, tags=None, cc=''):
    native, t = native or '', tags or {}
    if not native or latin(native): return native
    for k in ('name:en', 'int_name') + tuple(sorted(k for k in t if LATN_KEY.match(k))):
        v = (t.get(k) or '').strip()
        if v and latin(v) and re.search(r'[A-Za-z]', v): return v
    n = re.sub(r'(\d+)\s*(?:호선|号线|號線|号線)', r' Line \1 ', native)                                   # "1호선" -> "Line 1"
    n = re.sub(r'สายสี(' + '|'.join(sorted(TH_COLOUR, key=len, reverse=True)) + ')', lambda m: ' ' + TH_COLOUR[m.group(1)] + ' Line ', n)
    return latinize(n, cc)
def english_operator(name, q='', cc='', kind=''):
    q = q or (wd.op_by_name(name, cc) if name else '')
    if not q and not name and kind in ('h', 'r', 's'): q = national_operator(cc, kind)
    en, url = wd.op(q) if q else ('', '')
    return (wd.curated_entry(name, cc).get('en') or (name if latin(name) else cap(en) or latinize(wd.norm_op(name) or name, cc))), url, q, \
        (wd.wiki(q) if q and not url else '')

# ---------------------------------------------------------------- the operator of a line
# not the operator: fare associations and zones, infrastructure managers and station owners, generic words, OSM codes
NOT_OPERATOR = re.compile(r'(?i)verbund|tarif|\btakst|\bfare\b|unknown|unbekannt|national rail|hoofdrailnet|^[A-Z]{2}[:_]|network rail|'
                          r'^(?:VOR|VBB|VRR|VRS|VRN|KVV|RMV|HVV|MVV|VVS|VGN|VMS|VVO|MDV|ZVV|OÖVV|VVT|SVV|NVV|AVV|VBN|GVH|NAH\.SH|VMT|VPE|VRT|TNW|'
                          r'OVV|Libero|Mobilis|Onde Verte|A-Welle|Ostwind|Passepartout|TransReno|Unireso|Frimobil|Arcobaleno|Engadin Mobil|'
                          r'ch-integral|CH-VS|Z-Pass)$|'
                          r'trafikverket|bane ?nor\b|infrabel|prorail|sncf r[ée]seau|\brfi\b|rete ferroviaria italiana|\badif\b|db netz|'
                          r'infrago|db station|station ?& ?service|infraestruturas|polskie linie kolejowe|pkp plk|správa železnic|'
                          r'infra(?:struct|strukt|strutt)|инфраструктур|інфраструктур|\bНКЖИ\b|^(?:national|regional|local|city|urban|commuter|suburban|intercity|express|train|trains|tren|'
                          r'railway|rail|metro|tram|bus|none|no|yes|public|private)$|エリア|地区|ネットワーク|系統|線系|近郊区間')
def _first(v): return (v or '').split(';')[0].strip()
def line_values(ts, urban):
    """[(value, wikidata id)] of a line's operator / network / brand tags (and their :en forms), urban lines network
    first, trains operator first; fare associations, infrastructure managers and generic words left out."""
    out = []
    for k in (('network', 'brand', 'operator') if urban else ('operator', 'brand', 'network')):
        for t in ts[:8]:
            q = _first(t.get(k + ':wikidata'))
            for v in (_first(t.get(k)), _first(t.get(k + ':en'))):
                if v and not NOT_OPERATOR.search(v): out.append((v, q if re.fullmatch(r'Q\d+', q) else ''))
    return list(dict.fromkeys(out))
def national_operator(cc, kind):
    """The one passenger operator of the country's domestic trains of this kind: ref/operators.json "national", else the
    operator of 90%+ of its train services (wikidata.default_operator); '' when there is none."""
    wd._ref()
    n = wd._C['refnat'].get(cc)
    if n: return n['q'] if kind in n.get('kinds', 'hrs') else ''
    return wd.default_operator(cc)[0] if kind in 'hrs' else ''
def infra_network(cc, kind, stn, ts):
    """The operator item (ref/operators.json "infra") of the regional trains of this kind on the country's national network,
    when the line's stations are the infrastructure manager's (the operator most of their nodes name) or its only operator
    tag names that manager; '' otherwise."""
    wd._ref(); n = wd._C['refinfra'].get(cc)
    if not n or kind not in n['kinds']: return ''
    top = (stn or Counter()).most_common(1)
    tags = {_first(t.get('operator')) for t in ts[:8]} - {''}
    return n['q'] if top and re.search(n['stations'], top[0][0]) or tags and all(re.search(n['stations'], v) for v in tags) else ''
def line_operator(op, q, ts, cc, kind, dom=False, stn=None):
    """(name, logo url, qid, wiki) of a line: the first of its operator candidates whose item (its wikidata tag, the
    curated one, the one of that name: wikidata.op_by_name) has a logo: the operator build_world chose (op, q), the other
    operator / network / brand values of its tags (line_values), the leading operator of its stations' nodes (stn:
    Counter of values, 2+ nodes and 40%+ of them), for a domestic train (dom) the country's national operator (only when
    the line names none, or ref/operators.json says "named": its zones are that railway's). Else the first candidate
    with an item, else op in English."""
    if op and NOT_OPERATOR.search(op): op, q = '', ''      # an infrastructure manager, fare association ...: not the operator
    own = {_first(t.get(k + ':wikidata')) for t in ts for k in ('operator', 'network', 'brand')}
    o = wd._item(q)[1] if q and q not in own else None     # an id build_world took from another line of that name ("SETRAM"
    if o and o['cc'] and cc not in {wd.iso(c) for c in o['cc']}: q = ''      # of Le Mans for Algeria's trams): of this country only
    cands = ([(op, q)] if op else []) + line_values(ts, kind in 'mltf')
    tot = sum((stn or {}).values())
    cands += [(v, '') for v, n in (stn or Counter()).most_common(3) if n >= 2 and n >= 0.4 * tot and not NOT_OPERATOR.search(v)]
    got = None
    for v, vq in dict.fromkeys(cands):
        if vq and wd.infra(wd._item(vq)[1]): vq = ''      # "Trenitalia" tagged with the infrastructure manager's id
        r = english_operator(v, vq, cc)
        if r[2] and wd.infra(wd._item(r[2])[1]): continue
        if r[1]: return r
        if r[2] and got is None: got = r
    nat = national_operator(cc, kind) if dom and kind in 'hrs' else ''
    if nat and (not got and not op or wd._C['refnat'].get(cc, {}).get('named')):
        r = english_operator('', nat, cc)
        if r[1]: return (got[0] if got else english_operator(op, q, cc)[0] if op else r[0]) or r[0], r[1], r[2], r[3]
    return got or ((english_operator(op, q, cc)[0] if op else ''), '', '', '')

# ---------------------------------------------------------------- transliteration of the other scripts
WORDS = {'জংশন': 'Junction', 'जंक्शन': 'Junction', 'जंकशन': 'Junction', 'ਜੰਕਸ਼ਨ': 'Junction', 'જંક્શન': 'Junction', 'சந்திப்பு': 'Junction',
         'జంక్షన్': 'Junction', 'ಜಂಕ್ಷನ್': 'Junction', 'ജംഗ്ഷൻ': 'Junction', 'လမ်းဆုံ': 'Junction', 'रोड': 'Road', 'রোড': 'Road',
         'ரோடு': 'Road', 'हॉल्ट': 'Halt', 'হল্ট': 'Halt', 'ক্যান্টনমেন্ট': 'Cantonment', 'कैंट': 'Cantt', 'বিমানবন্দর': 'Airport', 'हवाई अड्डा': 'Airport',
         'สนามบิน': 'Airport', 'مطار': 'Airport', 'مترو': 'Metro', 'কলেজ': 'College', 'فرودگاه': 'Airport', 'שדה התעופה': 'Airport', 'км': 'km', 'კმ': 'km', 'կմ': 'km', 'กม.': 'km ',
         'χλμ': 'km', 'ኪ.ሜ': 'km', 'по требованию': 'request stop', 'на вимогу': 'request stop'}
_WORDS = re.compile(r'(?<![^\W\d_])(?:' + '|'.join(sorted(map(re.escape, WORDS), key=len, reverse=True)) + r')(?![^\W\d_])')     # whole words
_AR = dict(zip('ابتثجحخدذرزسشصضطظعغفقكلمنهةوىيءئؤپچژگکیٹڈڑںےھۃۓڤڨأإآٱ',
               "a b t th j h kh d dh r z s sh s d t z ' gh f q k l m n h a w a y ' ' ' p ch zh g k y t d r n e h a e v g a i a a".split()))
_HE = dict(zip('אבגדהוזחטיכלמנסעפצקרשתךםןףץ', "' v g d h v z ch t y kh l m n s ' f ts k r sh t kh m n f ts".split()))
def _abjad_word(w, table, cc, hebrew=False):
    """A consonant skeleton read with the short vowels guessed: an "a" into each cluster, و / ي (ו / י) as u / i
    between consonants ("الزقازيق" -> "Al-Zaqaziq", "دالبندین" -> "Dalbandin", "אביב" -> "Aviv")."""
    pre = ''
    if not hebrew and len(w) > 3 and w.startswith('ال'): pre, w = ('El ' if cc in ('DZ', 'TN', 'MA', 'LY', 'MR') else 'El-' if cc == 'EG' else 'Al-'), w[2:]
    if hebrew and len(w) > 3 and w[0] == 'ה': pre, w = 'Ha', w[1:]
    units = []                                   # (text, is_vowel)
    for i, ch in enumerate(w):
        nxt = w[i + 1] if i + 1 < len(w) else ''
        if ch in ('ّ', 'ּ'): continue
        if hebrew:
            if ch in 'בכפ' and i == 0: units.append(({'ב': 'b', 'כ': 'k', 'פ': 'p'}[ch], False)); continue
            if ch in "'׳" and units: units[-1] = ({'g': 'j', 'ts': 'ch', 'z': 'zh'}.get(units[-1][0], units[-1][0]), False); continue
            if ch in 'אע': units.append(('a' if i == 0 else '', i == 0)); continue
            if ch == 'ה' and i == len(w) - 1: units.append(('a', True)); continue
        if ch in 'اآأإٱ' and i or ch == 'ع' and i == 0: units.append(('a', True)); continue
        if ch in 'وו' and i and (i == len(w) - 1 or nxt and nxt not in 'اوىيیאו'):
            units.append(('o' if hebrew else 'u', True)); continue
        if ch in 'يیىי' and i and (i == len(w) - 1 or nxt and nxt not in 'اوىيیאו'):
            units.append(('i' if ch != 'ى' else 'a', True)); continue
        if ch in 'ةۃ' or (ch == 'ه' and i == len(w) - 1 and cc in ('IR', 'AF', 'PK', 'TJ') and i): units.append(('eh' if ch == 'ه' else 'a', True)); continue
        x = (table.get(ch) if ch in table else anyascii(ch)) or ''
        units.append((x, x in ('a', 'i', 'e', 'o', 'u')))
    out = ''
    for i, (x, v) in enumerate(units):
        if not x: continue
        before = out[-1:]; out += x
        if v or x == "'": continue
        nxt = next((u for u in units[i + 1:] if u[0]), None)
        if nxt and not nxt[1] and nxt[0] != "'" and before not in ('a', 'e', 'i', 'o', 'u'): out += 'a'     # a cluster: "mghrar" -> "maghrar"
    return pre + cap(out.replace("''", "'").strip("'"))
def abjad_latn(s, cc='', hebrew=False):
    table = _HE if hebrew else _AR
    return ' '.join(_abjad_word(w, table, cc, hebrew) for w in re.split(r'\s+', s) if w)

_IND = {'DEVANAGARI': 'devanagari', 'BENGALI': 'bengali', 'GURMUKHI': 'gurmukhi', 'GUJARATI': 'gujarati', 'ORIYA': 'oriya', 'TELUGU': 'telugu',
        'KANNADA': 'kannada', 'MALAYALAM': 'malayalam', 'SINHALA': 'sinhala'}
_SCHWA = {'DEVANAGARI', 'BENGALI', 'GURMUKHI', 'GUJARATI', 'ORIYA'}
_SUFFIX_EN = {'nagara': 'nagar', 'pura': 'pur', 'gañja': 'ganj', 'ganja': 'ganj', 'ābāda': 'abad', 'grāma': 'gram', 'hāṭa': 'hat', 'ghāṭa': 'ghat',
              'gaḍha': 'garh', 'gaṛha': 'garh', 'bājāra': 'bazar', 'bāzāra': 'bazar', 'ḍāṅgā': 'danga', 'pāḍā': 'para'}
_SUFFIX = re.compile('(?:' + '|'.join(_SUFFIX_EN) + ')$')
_C = r'(?:kh|gh|ch|jh|ṭh|ḍh|th|dh|ph|bh|[kgṅcjñṭḍṇtdnpbmyrlvśṣshḻṟ])'
_V = r'[aāiīuūeēoōṛèò]'
def indic_latn(s, sc):
    from indic_transliteration import sanscript
    s = s.replace('‍', '').replace('‌', '')
    if sc == 'BENGALI': s = s.translate(str.maketrans({'ড়': 'র', 'ঢ়': 'র', 'য়': 'য'})).replace('ড়', 'র').replace('ঢ়', 'র').replace('য়', 'য')
    x = sanscript.transliterate(s, _IND[sc], sanscript.IAST).lower()
    words = []
    for w in x.split():
        if sc in _SCHWA:
            m = _SUFFIX.search(w) if len(w) > 6 else None                     # place-name suffixes on their own: hāsāna|pura
            tail = _SUFFIX_EN[m.group(0)] if m else ''
            if m: w = w[:m.start()]
            if len(re.findall(_V, w)) >= 2 or tail: w = re.sub(r'(?<=' + _V + r')(ṃ?' + _C + r')a$', r'\1', w)   # final inherent vowel: rāmapura -> rāmapur
            w = re.sub(r'(' + _V + r'ṃ?' + _C + r')a(' + _C + _V + r')', r'\1\2', w) + tail                  # V C a C V: rāmapur -> rāmpur
        if sc == 'BENGALI': w = re.sub(r'^y', 'j', w.replace('v', 'b').replace('ph', 'f'))
        w = re.sub(r'ṃ(?=[pb])', 'm', w.replace('ṃm', 'm').replace('ññ', 'nj').replace('ṅṅ', 'ng')).replace('ṃ', 'n')
        w = w.replace('ch', '\x01').replace('c', 'ch').replace('\x01', 'chh')
        w = w.translate(str.maketrans('āīūṭḍṇñṅḷēōèòḥṁ', 'aiutdnnnleoeohn')).replace('ṛ', 'ri').replace('ṝ', 'ri').replace('ś', 'sh').replace('ṣ', 'sh')
        w = w.replace('ḻ', 'zh').replace('ṟ', 'r')
        words.append(cap(re.sub(r'[^\x00-\x7f]', '', w)))
    return ' '.join(words)
THAI_ENGINE = ['thai2rom_onnx']       # pythainlp's seq2seq model (onnxruntime; downloads ~1 MB once), else its royin rules
def thai_latn(s):
    from pythainlp.tokenize import word_tokenize
    from pythainlp.transliterate import romanize
    if THAI_ENGINE[0] != 'royin':
        try: romanize('ไทย', engine=THAI_ENGINE[0])
        except Exception: THAI_ENGINE[0] = 'royin'
    out = []
    for w in word_tokenize(s, engine='newmm'):
        if not w.strip(): out.append(' '); continue
        r = w if latin(w) else romanize(w, engine=THAI_ENGINE[0])
        out.append(re.sub(r'[฀-๿]', '', r) + ' ')
    return ' '.join(cap(w) for w in re.sub(r'-(\w)', lambda m: '-' + m.group(1).upper(), ''.join(out)).split())
def hy_latn(s):
    s = re.sub(r'(^|\s)([Եե])', lambda m: m.group(1) + ('Ye' if m.group(2) == 'Ե' else 'ye'), s.replace('ու', 'u').replace('Ու', 'U').replace('և', 'yev'))
    return anyascii(s).replace("'", '')

def _one(s, sc, cc):
    """Latin form of a run of one script."""
    if sc in ('JAPANESE', 'CJK', 'HANGUL', 'CYRILLIC', 'GREEK'): return translit(s, 'JP' if sc == 'JAPANESE' else cc) or anyascii(s)
    if sc in ('ARABIC', 'SYRIAC'): return abjad_latn(s, cc)
    if sc == 'HEBREW': return abjad_latn(s, cc, hebrew=True)
    if sc in _IND: return indic_latn(s, sc)
    if sc == 'THAI':
        try: return thai_latn(s)
        except Exception: pass
    if sc == 'ARMENIAN': return ' '.join(cap(w) for w in hy_latn(s).split())
    return ' '.join(cap(w) for w in anyascii(s).split())
_HG = 'aceiopxyABCEHIKMOPTX'
HOMOGLYPH = str.maketrans(_HG, 'асеіорхуАВСЕНІКМОРТХ')
def latinize(s, cc=''):
    s = re.sub(r'\s+', ' ', (s or '').replace('«', '"').replace('»', '"')).strip()
    s = re.sub(r'(?<=[\u0370-\u04ff])[\u0300\u0301]', '', s)            # stress marks: "Убы́ть"
    if not s or latin(s): return s
    s = ' '.join(w.translate(HOMOGLYPH) if re.search('[а-яёіїєґА-ЯЁІЇЄҐ]', w) and not re.search(f'[^\\W\\d_{_HG}а-яёіїєґА-ЯЁІЇЄҐ]', w) else w
                 for w in s.split(' '))        # Latin look-alikes typed into a Cyrillic word ("Укрзалiзниця", "Tрамвай")
    toks = s.split(' ')
    fam = lambda w: {'JAPANESE': 'CJK'}.get(script(w), script(w))
    scripts = {fam(w) for w in toks} - {'LATIN'}
    if len(scripts) > 1:                        # "霍吉尔特 قوجىرتى", "Таван шар ᠲᠠᠪᠤᠨ": keep the country's script, else the first
        keep = {'JAPANESE': 'CJK'}.get(MAIN.get(cc), MAIN.get(cc))
        keep = keep if keep in scripts else fam(next(w for w in toks if fam(w) != 'LATIN'))
        s = ' '.join(w for w in toks if fam(w) in ('LATIN', keep)) or s
    junction = bool(re.match(r'ชุมทาง', s)); s = re.sub(r'^ชุมทาง', '', s)
    s = _WORDS.sub(lambda m: ' ' + WORDS[m.group(0)].strip() + ' ', base(s)).strip()
    words = []
    for w in s.split():
        runs = []                                   # runs of one script; marks stay with their letter
        for ch in w:
            k = _chscript(ch)
            if not k and (unicodedata.category(ch)[0] == 'M' or ch in '\u200c\u200d') and runs: k = runs[-1][1]
            k = '' if k == 'LATIN' else k
            if runs and runs[-1][1] == k: runs[-1][0] += ch
            else: runs.append([ch, k])
        parts = [(_one(t, k, cc), 1) if k else (t, 0) for t, k in runs]
        words.append(''.join(p if not i or not (x or parts[i - 1][1]) or not (p[:1].isalnum() and parts[i - 1][0][-1:].isalnum()) else ' ' + p
                             for i, (p, x) in enumerate(parts)))          # "YRP Nobi", "Kusatsu 3 Ban"
    r = ' '.join(words) + (' Junction' if junction else '')
    r = ''.join(ch if latin(ch) else anyascii(ch) for ch in r.replace('〈', ' (').replace('〉', ')'))
    r = re.sub(r'\s+', ' ', re.sub(r'([(\[])\s+', r'\1', re.sub(r'\s+([,.)\]])', r'\1', r))).strip(' -–/,;:')
    r = ' '.join(w if re.search(r'[aeiouyAEIOUY0-9]', w) or len(w) < 3 or not w.isalpha() or w.isupper() else w[0] + 'a' + w[1:] for w in r.split())
    return r or anyascii(s) or s
