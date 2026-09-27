"""Latin forms of non-Latin station and line names for tools/world/build_world.py.

translit(s, cc) -> the name in Latin script by the usual rules of its language, or '' when no rule gives a readable
result (Arabic, Persian, Urdu and Hebrew write few vowels; Thai, Lao, Khmer and Burmese need word segmentation):
callers then use another tag or keep the native name.
  Japanese  Hepburn without macrons from a dictionary of mapped readings (station name:en / name:ja-Latn, places),
            pykakasi for the rest; line words and katakana loanwords in English ("鹿児島本線" -> "Kagoshima Main Line")
  Chinese   pinyin (Taiwan: jieba words)
  Korean    Revised Romanization with its sound changes (강릉 Gangneung, 신라 Silla, 왕십리 Wangsimni); 선 -> Line
  Cyrillic  by country: Russian (BGN/PCGN, no soft signs), Ukrainian (national 2010), Belarusian, Bulgarian
            (streamlined), Serbian and Macedonian (official Latin), Kazakh
  Greek     ELOT 743
  others    anyascii, all-caps words kept in capitals
"""
import re
import jieba, pykakasi, pypinyin
from anyascii import anyascii
jieba.setLogLevel(60)

LATIN = re.compile(r"^[\x00-ɏḀ-ỿ -⁯℀-⅏ʰ-˿̀-ͯ]*$")
KANA, HAN, HANGUL = re.compile(r'[぀-ヿ]'), re.compile(r'[㐀-䶿一-鿿]'), re.compile(r'[가-힯ᄀ-ᇿ]')
CYR, GREEK = re.compile(r'[Ѐ-ӿ]'), re.compile(r'[Ͱ-Ͽἀ-῿]')
ABJAD = re.compile(r'[֐-׿؀-ۿ܀-ࣿיִ-﷿ﹰ-﻿]')      # Hebrew, Arabic, Syriac, Thaana
SEGMENT = re.compile(r'[฀-໿က-႟ក-៿]')                              # Thai, Lao, Burmese, Khmer
TIFINAGH = re.compile(r'[ⴰ-⵿]')
def latin(s): return bool(s) and bool(LATIN.match(s))
def cap(w): return w[:1].upper() + w[1:]
def plain(s):
    """Macrons and circumflexes of romanised Japanese off: "Tōkyō" -> "Tokyo"."""
    return s.translate(str.maketrans('ōūāēīÔŌŪĀĒĪôûâêî', 'ouaeiOOUAEIouaei'))

# ---------------------------------------------------------------- Japanese
_kks = pykakasi.kakasi()
KATA = {'メトロ': 'Metro', 'ライナー': 'Liner', 'ライン': 'Line', 'モノレール': 'Monorail', 'エクスプレス': 'Express', 'ライトレール': 'Light Rail',
        'ケーブル': 'Cable', 'シャトル': 'Shuttle', 'ニュー': 'New', 'シーサイド': 'Seaside', 'スカイツリー': 'Skytree', 'アーバンパーク': 'Urban Park',
        'ターミナル': 'Terminal', 'センター': 'Center', 'ポート': 'Port', 'タウン': 'Town', 'パーク': 'Park', 'ガーデン': 'Garden', 'ランド': 'Land',
        'ビーチ': 'Beach', 'ヒルズ': 'Hills', 'エアポート': 'Airport', 'リゾート': 'Resort', 'ディズニー': 'Disney', 'シティ': 'City', 'シテイ': 'City',
        'スカイ': 'Sky', 'アクセス': 'Access', '新幹線': 'Shinkansen', '本線': 'Main Line', '支線': 'Branch Line', '地下鉄': 'Subway',
        '電鉄': 'Electric Railway', '鉄道': 'Railway', '空港': 'Airport', '系統': 'Route', '線': 'Line', '駅前': 'Ekimae', '駅': '', '列車': '', '号': ''}
_KATA = re.compile('(' + '|'.join(sorted(KATA, key=len, reverse=True)) + ')')
JA = {}           # known readings: "鹿児島" -> "Kagoshima" (filled by build_world.py from the data)
JA_MAX = [1]
def ja_word(native, latn):
    """Record a mapped reading (station name:ja-Latn / name:en, place name:en) for ja_latn()."""
    latn = plain(re.sub(r'(?i)\s+(?:station|sta\.?)$|-eki$', '', latn or '').strip())
    native = re.sub(r'駅$', '', native or '').strip()
    if len(native) >= 2 and latin(latn) and re.search(r'[a-z]', latn) and not re.search(r'[\sA-Za-z0-9]', native) and len(latn) <= 6 * len(native):
        JA.setdefault(native, latn); JA_MAX[0] = max(JA_MAX[0], len(native))
JA_TAIL = {'前': 'mae', '駅前': 'ekimae', '口': 'guchi', '北': 'kita', '南': 'minami', '東': 'higashi', '西': 'nishi', '町': 'machi'}
def _ja_known(s):
    """Split off known readings, longest first, when what is left is not a lone character: [(text, reading or None)]."""
    out, i, buf = [], 0, ''
    while i < len(s):
        for n in range(min(JA_MAX[0], len(s) - i), 1, -1):
            rest = s[i + n:]
            if s[i:i + n] in JA and (len(rest) != 1 or rest in JA_TAIL or not re.match(r'[\u3400-\u9fff]', rest)):
                if buf: out.append((buf, None)); buf = ''
                out.append((s[i:i + n], JA[s[i:i + n]])); i += n; break
        else: buf += s[i]; i += 1
    if buf: out.append((buf, None))
    return out
def ja_latn(s):
    """Hepburn without macrons, words capitalised; line words and katakana loanwords in English ("京急本線" -> "Keikyu Main Line")."""
    words = []
    for part in _KATA.split(s.replace('・', ' / ')):
        if part in KATA:
            if KATA[part]: words.append(KATA[part])
            continue
        for text, known in _ja_known(part):
            if known: words.append(known); continue
            glue = False
            for p in _kks.convert(text):
                h = re.sub(r'ou|oo', 'o', p['hepburn']).replace('uu', 'u')
                if p['orig'] in ('の', 'ノ', 'が', 'ヶ', 'ケ', 'ッ') and words: words[-1] += h; glue = True      # 江の島 -> Enoshima
                elif re.search(r'[a-z]', h):
                    if glue: words[-1] += h
                    else: words.append(cap(h))
                    glue = False
                elif p['orig'].strip(' ･·'): words.append(p['orig'].strip()); glue = False
    return re.sub(r'\s+', ' ', ' '.join(words)).strip()

# ---------------------------------------------------------------- Chinese (Taiwan)
ZH_DIR = {'東': 'East', '东': 'East', '西': 'West', '南': 'South', '北': 'North'}
def zh_latn(zh):
    """Readable pinyin for a Chinese (Taiwan) name, as curate.py py()."""
    zh = re.sub(r'\s+', '', zh); suffix = ''
    for k, v in (('火車站', ' Railway Station'), ('車站', ''), ('站', ''), ('線', ' Line'), ('线', ' Line')):
        if zh.endswith(k) and len(zh) > len(k) + 1: zh, suffix = zh[:-len(k)], v; break
    if not suffix and len(zh) >= 3 and zh[-1] in ZH_DIR and zh[-2] not in ZH_DIR: zh, suffix = zh[:-1], ' ' + ZH_DIR[zh[-1]]
    words = []
    for w in jieba.cut(zh):
        if HAN.search(w): words.append(cap(''.join(p.replace('lv', 'lü').replace('nv', 'nü') for p in pypinyin.lazy_pinyin(w))))
        elif w.strip(): words.append(w)
    return ' '.join(words) + suffix

# ---------------------------------------------------------------- Korean (Revised Romanization)
K_INI = 'g kk n d tt r m b pp s ss - j jj ch k t p h'.split()
K_VOW = 'a ae ya yae eo e yeo ye o wa wae oe yo u wo we wi yu eu ui i'.split()
K_FIN = ['', 'k', 'k', 'k', 'n', 'n', 'n', 't', 'l', 'k', 'm', 'l', 'l', 'l', 'p', 'l', 'm', 'p', 'p', 't', 't', 'ng', 't', 't', 'k', 't', 'p', 't']
K_LINK = ['', 'g', 'kk', 'ks', 'n', 'nj', 'n', 'd', 'r', 'lg', 'lm', 'lb', 'ls', 'lt', 'lp', 'r', 'm', 'b', 'ps', 's', 'ss', 'ng', 'j', 'ch', 'k', 't', 'p', '']
K_CLASS = {1: 'k', 2: 'k', 3: 'k', 9: 'k', 24: 'k', 7: 't', 19: 't', 20: 't', 22: 't', 23: 't', 25: 't', 27: 't', 17: 'p', 18: 'p', 26: 'p', 14: 'p'}
KO_WORDS = {'노선': '', '선': ' Line', '역': '', '경유': ' via', '행': '', '본선': ' Main Line', '지선': ' Branch Line', '고속철도': ' High Speed Railway', '도시철도': ' Urban Railway'}
def _ko_word(w):
    syl = [(ord(c) - 0xAC00) for c in w]
    out = ''
    for i, x in enumerate(syl):
        L, V, T = x // 588, (x % 588) // 28, x % 28
        ini = K_INI[L]
        if i:          # the previous syllable's final and this initial
            pT = syl[i - 1] % 28; cls = K_CLASS.get(pT)
            if L == 11:                                       # silent ㅇ: the final carries over
                if pT in (7, 25) and V == 20: coda, ini = '', 'j' if pT == 7 else 'ch'   # palatalisation 같이 gachi
                else: coda, ini = ('ng', '') if pT == 21 else ('', K_LINK[pT])
            elif L in (2, 6):                                  # ㄴ ㅁ: nasalisation
                coda = {'k': 'ng', 't': 'n', 'p': 'm'}.get(cls, K_FIN[pT])
                if pT == 8 and L == 2: coda, ini = 'l', 'l'
            elif L == 5:                                       # ㄹ
                if pT in (4, 8): coda, ini = 'l', 'l'
                elif pT in (16, 21): coda, ini = K_FIN[pT], 'n'
                elif cls: coda, ini = {'k': 'ng', 't': 'n', 'p': 'm'}[cls], 'n'
                else: coda = K_FIN[pT]
            elif pT == 27 and L in (0, 3, 12): coda, ini = '', {0: 'k', 3: 't', 12: 'ch'}[L]   # ㅎ + ㄱㄷㅈ: aspirated
            else: coda = K_FIN[pT]
            out += coda
        out += ('' if ini == '-' else ini) + K_VOW[V]
    return out + K_FIN[syl[-1] % 28]
def ko_latn(s):
    words = []
    for w in re.split(r'(\s+|[()\-–/:·,])', s):
        if not w or not HANGUL.search(w): words.append(w); continue
        if w in KO_WORDS: words.append(KO_WORDS[w]); continue
        tail = ''
        for k in sorted(KO_WORDS, key=len, reverse=True):
            if w.endswith(k) and len(w) > len(k): w, tail = w[:-len(k)], KO_WORDS[k]; break
        parts = re.split(r'([가-힯]+)', w)
        words.append(''.join(cap(_ko_word(p)) if HANGUL.search(p) else p for p in parts) + tail)
    return re.sub(r'\s+', ' ', ''.join(words)).strip()

# ---------------------------------------------------------------- Cyrillic
_RU = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя', 'a b v g d e yo zh z i y k l m n o p r s t u f kh ts ch sh shch - y - e yu ya'.split()))
_UK = dict(_RU, г='h', ґ='g', е='e', є='ie', и='y', і='i', ї='i', й='i', щ='shch', ю='iu', я='ia', ь='-')
_UK0 = {'є': 'ye', 'ї': 'yi', 'й': 'y', 'ю': 'yu', 'я': 'ya'}            # word-initial forms
_BE = dict(_RU, г='h', е='ie', ё='io', і='i', й='j', ў='w', ю='iu', я='ia', ы='y', э='e')
_BE0 = {'е': 'ye', 'ё': 'yo', 'ю': 'yu', 'я': 'ya', 'й': 'y'}
_BG = dict(_RU, х='h', щ='sht', ъ='a', ь='y', ю='yu', я='ya', й='y')
_SR = dict(zip('абвгдђежзијклљмнњопрстћуфхцчџш', 'a b v g d đ e ž z i j k l lj m n nj o p r s t ć u f h c č dž š'.split()))
_MK = dict(_SR, ѓ='gj', ж='zh', ѕ='dz', ќ='kj', ц='c', ч='ch', џ='dzh', ш='sh')
_KK = dict(_RU, ә='a', ғ='gh', қ='q', ң='ng', ө='o', ұ='u', ү='u', һ='h', і='i')
CYR_BY_CC = {'UA': (_UK, _UK0), 'BY': (_BE, _BE0), 'BG': (_BG, {}), 'RS': (_SR, {}), 'BA': (_SR, {}), 'ME': (_SR, {}), 'XK': (_SR, {}),
             'MK': (_MK, {}), 'KZ': (_KK, {}), 'KG': (_KK, {}), 'MN': (_KK, {}), 'TJ': (_KK, {})}
def cyr_latn(s, cc=''):
    tab, first = CYR_BY_CC.get(cc, (_RU, {}))
    out = []
    for w in re.split(r'(\W+)', s):
        if not CYR.search(w): out.append(w); continue
        caps = len(w) >= 2 and w.isupper()
        r = ''
        for i, ch in enumerate(w):
            lo = ch.lower()
            x = first.get(lo) if i == 0 and lo in first else tab.get(lo)
            if x is None: x = anyascii(ch).lower() if CYR.search(ch) else ch
            if x == '-': x = ''
            if cc == 'UA' and lo == 'г' and i and w[i - 1].lower() == 'з': x = 'gh'       # зг -> zgh
            r += x
        out.append(r.upper() if caps else cap(r))
    return ''.join(out)

# ---------------------------------------------------------------- Greek (ELOT 743)
_GR2 = {'ου': 'ou', 'αι': 'ai', 'ει': 'ei', 'οι': 'oi', 'μπ': 'mp', 'ντ': 'nt', 'γγ': 'ng', 'γκ': 'gk', 'γξ': 'nx', 'γχ': 'nch'}
_GR = dict(zip('αβγδεζηθικλμνξοπρσςτυφχψωάέήίόύώϊϋΐΰ', 'a v g d e z i th i k l m n x o p r s s t y f ch ps o a e i i o y o i y i y'.split()))
def gr_latn(s):
    out = []
    for w in re.split(r'(\W+)', s):
        if not GREEK.search(w): out.append(w); continue
        lo, r, i = w.lower(), '', 0
        while i < len(lo):
            if lo[i:i + 2] in _GR2: r += _GR2[lo[i:i + 2]]; i += 2; continue
            if lo[i:i + 2] in ('αυ', 'ευ', 'ηυ', 'αύ', 'εύ'): r += {'α': 'av', 'ε': 'ev', 'η': 'iv'}[lo[i]]; i += 2; continue
            r += _GR.get(lo[i], anyascii(lo[i])); i += 1
        out.append(cap(r))
    return ''.join(out)

# ---------------------------------------------------------------- dispatch
def translit(s, cc=''):
    """Latin form of a name, or '' when its script has no readable transliteration here (see the module doc)."""
    if not s or latin(s): return s or ''
    if KANA.search(s) or (HAN.search(s) and cc == 'JP'): return ja_latn(s)
    if HANGUL.search(s): return ko_latn(s)
    if HAN.search(s): return zh_latn(s)
    if ABJAD.search(s) or SEGMENT.search(s) or TIFINAGH.search(s): return ''
    if CYR.search(s): return cyr_latn(s.replace('«', '"').replace('»', '"'), cc)
    if GREEK.search(s): return gr_latn(s)
    return ' '.join(w if latin(w) else (anyascii(w).upper() if len(w) >= 2 and w.isupper() else
                                         re.sub(r'(^|[-"(/])([a-z])', lambda m: m.group(1) + m.group(2).upper(), anyascii(w).lower()))
                    for w in s.split())
