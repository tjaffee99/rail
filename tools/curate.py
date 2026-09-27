"""Curate build/china/network.json (or the file given) in place.

    python3 tools/curate.py [network.json] [-v]

The network was extracted from OpenStreetMap (25 Sep 2026). OSM route relations
include things that are not passenger routes (depot tracks, bridges, reversing
spurs, yard wiring), duplicate relations, and names that are auto-romanised
pinyin blobs. This script hides the former and fixes the latter. It is
idempotent: run it again after editing the tables in names.py.

Line record: [kind, zh, en, ref, colour, city, stations, branches, loop, km, hidden, label, network, operator]
  hidden: 0 shown · 1 connector (original extract) · 2 hidden by this script
  label:  short English name drawn along the line on the map
  network, operator: the OSM tags, as mapped
Station record: [zh, en, lon, lat, kind, lines, transfers, complex, country]
  complex: id of the complex's main station, or -1 · country: CN, HK, MO, or the neighbour's ISO code
City record: [zh, en, lon, lat, population, urban lines]
"""
import json, re, sys, os
sys.path.insert(0, os.path.dirname(__file__))
import jieba, pypinyin
from names import LINE_EN, LINE_ZH, URBAN, STATION_EN, FREIGHT, FREIGHT_AUDIT, NOT_PUBLIC
FREIGHT = list(FREIGHT) + FREIGHT_AUDIT
jieba.setLogLevel(60)

ROOT = os.path.join(os.path.dirname(__file__), '..')
P = next((a for a in sys.argv[1:] if a.endswith('.json')), os.path.join(ROOT, 'build', 'china', 'network.json'))
d = json.load(open(P))
L, S, C = d['lines'], d['stations'], d['cities']
URBAN_K = set('mlstf')
reasons = {}

def hide(i, why):
    if not L[i][10]:
        L[i][10] = 2
        reasons[i] = why

for l in L:
    while len(l) < 14: l.append('')
    if l[10] == 2: l[10] = 0          # re-evaluate on every run

# ---------------------------------------------------------------- pinyin helper
DIR = {'东': 'East', '西': 'West', '南': 'South', '北': 'North'}
def py(zh):
    """Readable pinyin for a Chinese place name: words separated, capitalised."""
    zh = re.sub(r'\s+', '', zh)
    suffix = ''
    for k, v in (('火车站', ' Railway Station'), ('站', '')):
        if zh.endswith(k) and len(zh) > len(k) + 1: zh, suffix = zh[:-len(k)], v; break
    if not suffix and len(zh) >= 3 and zh[-1] in DIR and zh[-2] not in DIR:
        zh, suffix = zh[:-1], ' ' + DIR[zh[-1]]
    words = []
    for w in jieba.cut(zh):
        if re.fullmatch(r'[一-鿿]+', w):
            s = ''.join(p.replace('lv', 'lü').replace('nv', 'nü') for p in pypinyin.lazy_pinyin(w))
            words.append(s[:1].upper() + s[1:])
        elif w.strip(): words.append(w)
    out = ' '.join(words)
    out = re.sub(r"([aeiouv])(a|e|o)", lambda m: m.group(0), out)
    return out + suffix

BLOB = re.compile(r'[A-Za-z]{15,}')
CAMEL = re.compile(r'^(?:[A-Z][a-z]+){2,}$')

# ---------------------------------------------------------------- stations
TYPO = [('Raliway', 'Railway'), ('Exhibiation', 'Exhibition'), ('Shaungdian', 'Shuangdian'),
        ('BaoquanRoad', 'Baoquan Road'), ('TangParadise', 'Tang Paradise'), ('HaiZhou', 'Haizhou')]
for s in S:
    en = s[1] or ''
    for a, b in TYPO: en = en.replace(a, b)
    # "Xi'an North Railway Station" -> "Xi'an North", except stops named after a station ("火车站")
    if not s[0].endswith('车站'): en = re.sub(r'(?i)\s+(?:railway\s+|train\s+)?station$', '', en)
    if s[0] in STATION_EN: en = STATION_EN[s[0]]
    elif s[8] in ('HK', 'MO'):        # Cantonese / Portuguese names: never Mandarin pinyin
        en = en if en and not re.search(r'[一-鿿]', en) else s[0]
    elif re.search(r'[一-鿿]', s[0]) and (not en or en == s[0] or re.search(r'[一-鿿]', en)
                                                  or BLOB.fullmatch(en.replace(' ', '')) and ' ' not in en
                                                  or CAMEL.match(en)):
        en = py(s[0])
    s[1] = en

# one English spelling per station: "Fangshandong" / "Fangshan Dong" / "Fangshan East" -> "Fangshan East"
DIRWORD = {'Dong': 'East', 'Xi': 'West', 'Nan': 'South', 'Bei': 'North'}
for s in S:
    en, zh = s[1] or '', s[0]
    if len(zh) >= 3 and zh[-1] in DIR and s[8] not in ('HK', 'MO'):
        m = re.fullmatch(r"(.+?) ?(Dong|Xi|Nan|Bei)", en, re.I)
        if m and m.group(2).capitalize() in DIRWORD and DIRWORD[m.group(2).capitalize()] == DIR[zh[-1]]:
            base = m.group(1)
            en = (base[:1].upper() + base[1:]) + ' ' + DIR[zh[-1]]
    s[1] = en
by_name = {}
for i, s in enumerate(S): by_name.setdefault(s[0], []).append(i)
for zh, ids in by_name.items():
    if len(ids) < 2: continue
    for i in ids:     # nodes of the same station (same name within 3 km) share the most common spelling
        grp = [j for j in ids if abs(S[j][2] - S[i][2]) < 0.03 and abs(S[j][3] - S[i][3]) < 0.03]
        names = [S[j][1] for j in grp if S[j][1]]
        if len(set(names)) > 1:
            best = max(set(names), key=lambda n: (names.count(n), ' ' in n, -len(n)))
            for j in grp: S[j][1] = best

# ---------------------------------------------------------------- hide non-routes
NONROUTE = re.compile(r'特大桥|大桥|隧道|动车段|动车所|动车走行|车辆段|机务段|出入段|出入库|出入场|停车场|联络线|联线|疏解|'
                      r'走行线|绕行线|直通线|发车线|立折线|折返线|货车|货运|货线|专用线|附属线|下联线|上联线|进港|港口|码头|'
                      r'[\u4e00-\u9fff][A-H]\d线|示范段|宽轨')
# closed, trackless (智轨), private campus, tourist and theme-park lines; public tourist lines are kept
JUNK = re.compile(r'已停运|停运|智轨|华为|园区|观光|文旅|旅游|乐园|樂園|公园|公園|绿博园|花博|长隆|雪山|景区|小火车')
PUBLIC_TOURIST = ('凤凰磁浮', '都江堰M-TR')
KEEP_FEW = {'淮安有轨电车1号线'}
HANGUL = re.compile(r'[가-힯]')
def complete(l):
    """A two-stop 'A–B' line whose ends are A and B ('防东铁路': 防城港北 – 东兴市)."""
    a, b = S[l[6][0]][0], S[l[6][-1]][0]
    return len(l[1]) > 2 and {a[:1], b[:1]} == {l[1][0], l[1][1]}
for i, l in enumerate(L):
    if l[10]: continue
    if l[1] in LINE_ZH: l[1] = LINE_ZH[l[1]]
    zh, en = l[1], l[2]
    # lines mostly outside China (North Korea's Pyongui and Hambuk lines) belong to the world data
    ccs = [S[x][8] for x in l[6]]
    abroad = ccs and sum(c not in ('CN', 'HK', 'MO') for c in ccs) > 0.5 * len(ccs)
    if HANGUL.search(zh) or HANGUL.search(en) or re.search(r'[ŏŭ\u0400-\u04ff]', zh + en) or zh in ('白茂线',) or abroad:
        hide(i, 'outside China'); continue
    if (zh or en) in NOT_PUBLIC:
        hide(i, NOT_PUBLIC[zh or en]); continue
    if l[0] in URBAN_K and JUNK.search(zh + l[13]) and not zh.startswith(PUBLIC_TOURIST):
        hide(i, 'closed, trackless, private or tourist line'); continue
    base = re.sub(r'^\(原\)', '', re.sub(r'(重载铁路|铁路|线)$', '', zh))
    if l[0] == 'r' and (base in FREIGHT or re.search(r'港(?:线|铁路|支线|二线)$|煤|矿|钢', zh) or re.search(r'煤|矿|钢铁', l[13])):
        hide(i, 'freight only'); continue
    if l[0] in 'hr' and not re.search(r'[一-鿿]', zh):
        hide(i, 'intercity line without a name (industrial or mine track)'); continue
    if l[0] in URBAN_K and not (zh or en or l[3]):
        hide(i, 'urban line without a name or number'); continue
    if l[0] in URBAN_K and not (zh or en):
        l[2] = en = (f'Tram {l[3]}' if l[0] == 't' else f'Line {l[3]}')
    if NONROUTE.search(zh) and '城际' not in zh:
        hide(i, 'non-passenger track'); continue
    if l[0] == 'r' and (len(set(l[6])) < 3 or '旧线' in zh):
        hide(i, 'conventional line with under three stops, or an old alignment'); continue
    if len(set(l[6])) < 2 and not (l[0] in URBAN_K and zh in KEEP_FEW):
        hide(i, 'fewer than two stations mapped'); continue
    if l[0] == 'h' and len(set(l[6])) < 3 and (l[9] or 0) < 50 and not complete(l):
        hide(i, 'short high-speed fragment: two stops that are not its ends'); continue

# ---------------------------------------------------------------- duplicates
def group(k): return 'u' if k in URBAN_K else k
order = sorted(range(len(L)), key=lambda i: (-len(L[i][6]), -(L[i][9] or 0), i))
kept = []
JUNK_WHY = ('closed, trackless', *set(NOT_PUBLIC.values()))
for i in order:
    l = L[i]
    if l[10]:      # a copy of a line hidden as not public (another relation of it) goes too
        if reasons.get(i, '').startswith(JUNK_WHY): kept.append(i)
        continue
    si = set(l[6] + [s for b in l[7] for s in b])
    for j in kept:
        m = L[j]
        if group(m[0]) != group(l[0]): continue
        sj = set(m[6] + [s for b in m[7] for s in b])
        common = len(si & sj)
        if l[0] in URBAN_K:
            # urban: only the same mode; lines that are a stale/merged subset of a longer line (for trams and
            # light rail only when their numbers agree: services such as 614 and 614P overlap legitimately),
            # or any relation whose stops are identical to another's
            if m[0] != l[0]: continue
            same = si == sj
            num = lambda x: re.sub(r'^T|号?线$', '', x[3] or '')
            subset = common >= 0.95 * len(si) and l[5] == m[5] and (l[0] == 'm' or not (num(l) and num(m) and num(l) != num(m)))
            sameref = l[3] and l[3] == m[3] and l[5] == m[5] and common >= 0.8 * len(si)
            if not (same or subset or sameref): continue
        elif common < 0.8 * len(si):
            continue
        hide(i, f'duplicate of {m[1]}'); break
    else:
        kept.append(i)

# ---------------------------------------------------------------- stop order
# Some OSM relations list stops out of order (segments concatenated, or both directions
# interleaved). Re-sequence them along the shortest path when that is clearly shorter.
import math
def dist(a, b):
    A, B = S[a], S[b]
    return math.hypot((A[2] - B[2]) * math.cos(math.radians((A[3] + B[3]) / 2)), A[3] - B[3])
def plen(seq): return sum(dist(a, b) for a, b in zip(seq, seq[1:]))
def two_opt(p):
    p = list(p); improved = True
    while improved:
        improved = False
        for a in range(len(p) - 2):
            for b in range(a + 2, len(p)):
                d0 = dist(p[a], p[a + 1]) + (dist(p[b], p[b + 1]) if b + 1 < len(p) else 0)
                d1 = dist(p[a], p[b]) + (dist(p[a + 1], p[b + 1]) if b + 1 < len(p) else 0)
                if d1 < d0 - 1e-9:
                    p[a + 1:b + 1] = reversed(p[a + 1:b + 1]); improved = True
    return p
def or_opt(p):
    """Move runs of 1-3 stops (either way round) to wherever they fit best."""
    p = list(p); improved = True
    def cost(q): return plen(q)
    while improved:
        improved = False
        base = cost(p)
        for k in (1, 2, 3):
            for a in range(len(p) - k + 1):
                seg = p[a:a + k]; rest = p[:a] + p[a + k:]
                for b in range(len(rest) + 1):
                    if b == a: continue
                    for sg in (seg, seg[::-1]):
                        q = rest[:b] + sg + rest[b:]
                        c = cost(q)
                        if c < base - 1e-9: p, base, improved = q, c, True; break
                    if improved: break
                if improved: break
            if improved: break
    return p
def resequence(p):
    prev = None
    while prev != p:
        prev = p; p = or_opt(two_opt(p))
    return p
def shortest(seq):
    pts = list(dict.fromkeys(seq)); best = None
    for start in pts:
        rem = set(pts); rem.discard(start); p = [start]
        while rem:
            n = min(rem, key=lambda r: dist(p[-1], r)); p.append(n); rem.discard(n)
        if best is None or plen(p) < plen(best): best = p
    return resequence(best)
reordered = 0
for l in L:
    if l[10] or l[8] or len(l[6]) < 4 or len(l[6]) > 150: continue
    seq = list(dict.fromkeys(l[6]))
    local = resequence(seq)                   # fixes local reversals and strays, keeps OSM's overall order
    if (plen(seq) - plen(local)) * 111 > 0.3: seq = local
    b = shortest(seq)                          # badly scrambled lists: rebuild from scratch
    if plen(seq) > 1.2 * plen(b): seq = b
    if seq != l[6]:
        if dist(seq[0], l[6][0]) > dist(seq[-1], l[6][0]): seq.reverse()   # keep the original direction
        reordered += seq != l[6]
        l[6] = seq
    # drop a stop repeated back-to-back (same station, or same name on both sides of the street)
    out = []
    for x in l[6]:
        if out and (x == out[-1] or S[x][0] == S[out[-1]][0]): continue
        out.append(x)
    l[6] = out
print('stop lists re-sequenced:', reordered)

# ---------------------------------------------------------------- names
CN_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
def cn2int(s):
    if s.isdigit(): return int(s)
    if s == '十': return 10
    if s.startswith('十'): return 10 + CN_NUM[s[1]]
    if len(s) == 2 and s[1] == '十': return CN_NUM[s[0]] * 10
    if len(s) == 3: return CN_NUM[s[0]] * 10 + CN_NUM[s[2]]
    return CN_NUM[s]

for i, l in enumerate(L):
    k, zh = l[0], l[1]
    if k in URBAN_K:
        if zh in URBAN:
            en, ref = URBAN[zh]
            l[2] = en
            if ref is not None: l[3] = ref
        else:
            m = re.search(r'([A-Z]?)([0-9]+|[一二三四五六七八九十]+)号线', zh) or not l[2] and re.search(r'([A-Z]?)([0-9]+)线', zh)
            if m:
                n = m.group(1) + str(cn2int(m.group(2)))
                if '有轨电车' in zh or k == 't':
                    l[2], l[3] = f'Tram Line {n}', ('T' + n if n.isdigit() else n)
                else:
                    l[2] = f'Line {n}' + (' Branch' if '支线' in zh else '')
                    if not re.fullmatch(r'[A-Z]?\d+[A-Z]?', l[3] or ''): l[3] = n
            elif re.fullmatch(r'.*?(\d+)路', zh):     # Dalian's tram routes: 大连公交202路
                l[2] = ('Tram ' if k == 't' else 'Line ') + re.fullmatch(r'.*?(\d+)路', zh).group(1)
            else:
                en = l[2]
                en = re.sub(r'^(?:Beijing|Shanghai|Guangzhou|Shenzhen|Chengdu|Wuhan|Xi.an|Tianjin|Nanjing|Hangzhou|Chongqing|'
                            r'Changsha|Suzhou|Shenyang|Qingdao|Jinan|Zhengzhou|Kunming|Hefei|Ningbo|Dalian|Changchun|Harbin|'
                            r'Fuzhou|Xiamen|Nanning|Nanchang|Guiyang|Wuxi|Xuzhou|Foshan|Dongguan)\s+'
                            r'(?:Subway|Metro|Rail Transit|Urban Rail Transit)\s+', '', en)
                en = re.sub(r'\s*\((?:[A-Z][a-z]+ )?(?:Metro|Rail Transit|Chongqing Rail Transit)\)$', '', en)
                en = re.sub(r"^[A-Z][a-zü]+(?:['’][a-z]+)?\s+(?:Subway|Metro|Rail Transit|Urban Rail Transit|Tram)\s+(?=\S)", '', en)
                en = re.sub(r'^(?:Subway|Metro|MTR)\s+', '', en)
                en = re.sub(r'^Line ([A-Za-z]+)$', lambda m: m.group(1).capitalize() + ' Line', en)
                l[2] = re.sub(r'^(T\d+)$', r'Tram \1', en)
        l[2] = l[2].replace('line', 'Line') if re.fullmatch(r'.* line', l[2]) else l[2]
        l[11] = ''
    else:
        if zh in LINE_EN:
            l[2] = LINE_EN[zh]
        elif BLOB.search(l[2]) or not l[2] or re.search(r'[一-鿿]', l[2]):
            base = re.sub(r'(高速铁路|高速线|高铁|客运专线|客专线|城际铁路|城际线|城际|铁路|线)$', '', zh)
            tail = ('High-Speed Railway' if re.search(r'高速|高铁|客专|客运专线', zh) else
                    'Intercity Railway' if '城际' in zh else 'Railway')
            l[2] = f'{py(base)} {tail}'
        l[2] = l[2].replace('_', ' ').replace(' - ', '–').replace('-', '–').replace('Highspeed', 'High-Speed').replace('Guanzhou', 'Guangzhou') \
                   .replace('High Speed', 'High-Speed').replace('high-speed railway', 'High-Speed Railway') \
                   .replace('High–speed', 'High-Speed').replace('High–Speed', 'High-Speed').replace('intercity railway', 'Intercity Railway')
        lab = l[2]
        lab = re.sub(r'High-Speed (?:Railway|Line)|Passenger (?:Railway|Dedicated Line)|PDL', 'HSR', lab)
        lab = re.sub(r'Intercity (?:Railway|Line)', 'Intercity', lab)
        lab = re.sub(r'\s*\(.*?\)', '', lab)
        l[11] = lab

# ---------------------------------------------------------------- direction
# List stops in the order the name reads: "Beijing–Shanghai …" starts at Beijing.
def norm_en(x): return re.sub(r"[’'\s-]", '', x or '').lower()
flipped = 0
for l in L:
    if l[10] or l[0] not in 'hr' or len(l[6]) < 2: continue
    m = re.match(r"^([A-Z][^–(]*?)–(?:.*–)?([A-Z][^–(]*?)\s+(?i:High-Speed|Intercity|Railway|Express|Rail|PDL|Passenger)", l[2])
    if not m: continue
    a, b = norm_en(m.group(1)), norm_en(m.group(2))
    names = [norm_en(S[x][1]) for x in l[6]]
    ia = [i for i, n in enumerate(names) if n.startswith(a)]
    ib = [i for i, n in enumerate(names) if n.startswith(b)]
    if ia and ib and min(ia) > max(ib) or (ia and not ib and min(ia) > len(names) / 2) or (ib and not ia and max(ib) < len(names) / 2):
        l[6] = l[6][::-1]; flipped += 1
print('stop lists flipped to match their name:', flipped)

# ---------------------------------------------------------------- station complexes
# A railway station and the metro stations built into it are one place: same label, same panel, all lines
# listed together. Linked when they have the same name, or one's is the other's plus a word for a station
# ("上海" / "上海火车站", "七贤岭" / "七贤岭地铁"), within 800 m of a rail station or 400 m otherwise; or when a
# rail station and a metro / light-rail station or another rail station are within 400 m / 300 m, unless
# that would join two stops of one line (Hong Kong's tram stops). Never a tram stop by distance alone,
# stations in different territories (Lo Wu and Shenzhen), or into a group more than 900 m across.
def metres(a, b): return dist(a, b) * 111000
served = [any(not L[x][10] for x in s[5]) for s in S]
parent = list(range(len(S)))
members_of = {i: [i] for i in range(len(S))}
glines = {i: {x for x in S[i][5] if not L[x][10]} for i in range(len(S))}
def find(x):
    while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
    return x
def union(a, b, by_name):
    a, b = find(a), find(b)
    if a == b or S[a][8] != S[b][8] or not by_name and glines[a] & glines[b]: return
    if max(metres(x, y) for x in members_of[a] for y in members_of[b]) > 900: return
    parent[a] = b; members_of[b] += members_of.pop(a); glines[b] |= glines.pop(a)
def key(i): return S[i][0].rstrip('站')
STATION_WORD = re.compile(r'(?:地铁|城铁|快铁|高铁|轻轨|火车|动车)?站?(?:[东西南北]?广场)?|[（(].*[）)]')
def same_place(a, b): return a == b or len(a) >= 2 and a in b and STATION_WORD.fullmatch(b.replace(a, '', 1))
import bisect
pairs = []
order = sorted((S[i][2], i) for i in range(len(S)) if served[i])
xs = [o[0] for o in order]
for x, i in order:
    for _, j in order[bisect.bisect_left(xs, x - 0.01):bisect.bisect_right(xs, x + 0.01)]:
        if j <= i: continue
        m, a, b = metres(i, j), key(i), key(j)
        rail = (S[i][4] in 'hr') + (S[j][4] in 'hr')
        if a and b and (same_place(a, b) or same_place(b, a)) and m < (800 if rail or a == b else 400): pairs.append((0, m, i, j))
        elif rail == 1 and m < 400 and 't' not in (S[i][4], S[j][4]) or rail == 2 and m < 300: pairs.append((1, m, i, j))
for p, m, i, j in sorted(pairs): union(i, j, p == 0)
def rank(i): s = S[i]; return (s[4] == 'h', s[4] == 'r', sum(not L[x][10] for x in s[5]))
ncx = 0
for s in S:
    while len(s) < 8: s.append(-1)
    s[7] = -1
for members in members_of.values():
    if len(members) < 2: continue
    rep = max(sorted(members), key=rank); ncx += 1
    for m in members: S[m][7] = rep
print('station complexes:', ncx)

# English names: plain readable Latin - no soft hyphens, tone marks, full-width brackets, stray non-Latin letters or
# all-lower-case words ("Chengxin\u00addadao", "Běishān", "硃山湖（北/南）", "Huo Jierte ق و ج ى ر ت ى", "kaili")
import unicodedata
def plain_en(en, zh):
    en = (en or '').replace('\u00ad', '').replace('（', ' (').replace('）', ')')
    en = ''.join(c for c in unicodedata.normalize('NFD', en) if not unicodedata.combining(c))
    en = re.sub(r"[^\x20-\x7e\u00c0-\u024f]", ' ', en)
    en = re.sub(r'\([^A-Za-z]*\)', '', re.sub(r'\s+', ' ', en)).strip()
    if re.search(r'[一-鿿]', en) or not re.search('[A-Za-z]', en): en = py(re.sub(r'（.*?）|\(.*?\)', '', zh)) if zh else en
    return ' '.join(w[:1].upper() + w[1:] if w[:1].islower() else w for w in en.split(' '))
for st in S:
    if st[1] or st[0]: st[1] = plain_en(st[1], st[0])
json.dump(d, open(P, 'w'), ensure_ascii=False, separators=(',', ':'))
from collections import Counter
print('hidden by curation:', Counter(v.split(' of ')[0] for v in reasons.values()))
if '-v' in sys.argv:
    for i, v in sorted(reasons.items()): print(i, L[i][0], L[i][1], '|', v)
