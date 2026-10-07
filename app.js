(() => {
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// ---------------------------------------------------------------- rail tile archive
// The rail network is a pre-built vector tileset packed into a few binary chunk
// files (see data/manifest.json); the base map is streamed from OpenFreeMap.
let manifest = null;
const chunkCache = new Map(), fileCache = new Map();   // chunks and whole files read lately, most recent last
// a promise from a small cache: made on a miss, moved to the end on a hit, dropped when it fails or grows old
function cached(m, key, max, make) {
  let p = m.get(key);
  if (p) m.delete(key); else p = make().catch(e => { if (m.get(key) === p) m.delete(key); throw e; });
  m.set(key, p);
  while (m.size > max) m.delete(m.keys().next().value);
  return p;
}
function parseChunk(buf, off) {
  const dv = new DataView(buf, off); const n = dv.getUint32(4, true);
  const base = off + 8 + n * 12; const idx = new Map();
  for (let i = 0; i < n; i++) { const o = 8 + i * 12; idx.set(dv.getUint32(o, true), [base + dv.getUint32(o + 4, true), dv.getUint32(o + 8, true)]); }
  return { buf, idx };
}
function fetchBuf(url, init) {
  return fetch(url, init).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.arrayBuffer(); });
}
const loadFile = fn => cached(fileCache, fn, 8, () => fetchBuf('data/tiles/' + fn));
// a chunk is read by its byte range (GitHub Pages answers 206); a server that sends the whole file instead
// (python's http.server) is asked for whole files from then on, and the chunk copied out of the file. The first
// request finds out which it is.
let ranged = null, probe = null;
async function chunkBytes([fn, off, len]) {
  if (ranged === null && probe) await probe.catch(() => {});
  if (ranged !== false) {
    const p = fetch('data/tiles/' + fn, { headers: { Range: `bytes=${off}-${off + len - 1}` } });
    if (ranged === null) probe = p;
    const r = await p;
    if (!r.ok) throw new Error('HTTP ' + r.status);
    if (r.status === 206) { ranged = true; return r.arrayBuffer(); }
    ranged = false;
    cached(fileCache, fn, 8, () => r.arrayBuffer());
  }
  return (await loadFile(fn)).slice(off, off + len);
}
function loadChunk(src, key) {
  const info = manifest[src].chunks[key];
  if (!info) return Promise.resolve(null);
  return cached(chunkCache, src + '/' + key, 64, () => chunkBytes(info).then(buf => parseChunk(buf, 0)));
}
// chunk of a tile (tools/world/FORMAT.md): by zoom group ("g" chunks: the tiles of zooms zmin-zmax under one
// ancestor tile at zoom az); older manifests key low zooms by their ancestor at lz ("L") and the rest at rz
function chunkKey(m, z, x, y) {
  if (m.groups) { const g = m.groups.findIndex(r => z >= r[0] && z <= r[1]), az = m.groups[g][2]; return 'g' + g + '_' + (x >> (z - az)) + '_' + (y >> (z - az)); }
  if (z > m.lowmax) return (x >> (z - m.rz)) + '_' + (y >> (z - m.rz));
  return m.lz == null ? 'low' : 'L' + (x >> (z - m.lz)) + '_' + (y >> (z - m.lz));
}
async function getTile(src, z, x, y) {
  const m = manifest[src];
  if (!m || z < m.minzoom || z > m.maxzoom) return null;
  const ch = await loadChunk(src, chunkKey(m, z, x, y));
  if (!ch) return null;
  const e = ch.idx.get(((z << 26) | (x << 13) | y) >>> 0);
  return e ? new Uint8Array(ch.buf, e[0], e[1]) : null;
}
async function gunzip(u8) {
  if (u8[0] !== 0x1f || u8[1] !== 0x8b) return u8.slice().buffer;
  const s = new Blob([u8]).stream().pipeThrough(new DecompressionStream('gzip'));
  return await new Response(s).arrayBuffer();
}
// Glyphs: Latin, Greek and punctuation ranges are served locally; any other
// range (Cyrillic, Thai, Arabic…) comes from OpenFreeMap's Noto Sans. Chinese,
// Japanese and Korean are drawn with the system font (localIdeographFontFamily).
const OFM = 'https://tiles.openfreemap.org';
const GLYPH_RANGES = new Set(['0-255', '256-511', '512-767', '768-1023', '8192-8447', '8448-8703']);
const REMOTE_FONT = { NotoSansMedium: 'Noto Sans Bold', NotoSansRegular: 'Noto Sans Regular' };
maplibregl.addProtocol('tpg', async (params) => {
  const m = params.url.match(/^tpg:\/\/([^/]+)\/([\d-]+)/);
  if (!m) return { data: new ArrayBuffer(0) };
  const font = decodeURIComponent(m[1]).split(',')[0].trim();
  try {
    if (GLYPH_RANGES.has(m[2])) return { data: await fetchBuf('data/glyphs/' + font + '/' + m[2] + '.pbf') };
    const remote = REMOTE_FONT[font] || 'Noto Sans Regular';
    // a font server that does not answer would hold back every tile with a label in that range
    return { data: await fetchBuf(OFM + '/fonts/' + encodeURIComponent(remote) + '/' + m[2] + '.pbf', { signal: AbortSignal.timeout(10000) }) };
  } catch (_) { return { data: new ArrayBuffer(0) }; }
});
maplibregl.addProtocol('tp', async (params) => {
  const m = params.url.match(/^tp:\/\/(\w+)\/(\d+)\/(\d+)\/(\d+)/);
  const t = await getTile(m[1], +m[2], +m[3], +m[4]);
  return { data: t ? await gunzip(t) : new ArrayBuffer(0) };
});
// Arabic and Hebrew labels need the right-to-left shaping plugin, fetched once such a label shows up
maplibregl.setRTLTextPlugin('vendor/mapbox-gl-rtl-text.js', true).catch(() => {});
// ---------------------------------------------------------------- theme + palette
function isDark() {
  const a = document.documentElement.getAttribute('data-theme');
  if (a === 'dark') return true; if (a === 'light') return false;
  return matchMedia('(prefers-color-scheme: dark)').matches;
}
const PAL = {
  light: { land:'#F2F0EB', urban:'#EAE7E0', water:'#A7D2F2', waterLine:'#8FC3EA', park:'#CDE8C0', reserve:'#DDEEDA', airport:'#E6E4EC', uni:'#EEE6DC',
           road:'#FFFFFF', roadCase:'#D6D1C7', motor:'#FFFFFF', motorCase:'#D2CDC3', roadLow:'#D9D5CC', bnd:'#B0A99E', bnd4:'#C9C3B9', bndGlow:'#DDD7CC',
           hsr:'#2E63D6', rail:'#8657D8', track:'#BDB7AE', hsrText:'#1F4BB0', railText:'#6A3FC0', casing:'#FFFFFF', label:'#3A3A3C', label2:'#6C6C72', halo:'rgba(255,255,255,0.92)',
           city:'#2C2C2E', town:'#5A5A60', building:'#E3DFD7', buildingEdge:'#D6D1C7', wood:'#DCEAD2', sand:'#EEE8D8', ice:'#FBFBFD', road2:'#FDFBF6', path:'#CFC9BE', poi:'#7A7A80', waterLabel:'#4A7FAE', stnFill:'#FFFFFF', stnStroke:'#2C2C2E', sel:'#0A7AFF', dimOp:0.22 },
  dark:  { land:'#1F2124', urban:'#26282C', water:'#1A3957', waterLine:'#22476B', park:'#1F3829', reserve:'#1D2B22', airport:'#282830', uni:'#2A2724',
           road:'#3A3D43', roadCase:'#2B2D31', motor:'#43464D', motorCase:'#2F3136', roadLow:'#35383D', bnd:'#6E7177', bnd4:'#46494E', bndGlow:'#2C2F33',
           hsr:'#6D98FF', rail:'#AE8BF5', track:'#55585F', hsrText:'#9DBBFF', railText:'#C7AEFA', casing:'#1E2023', label:'#E9E9EE', label2:'#A1A1A8', halo:'rgba(24,25,28,0.92)',
           city:'#F2F2F7', town:'#C4C4CA', building:'#2B2D31', buildingEdge:'#35383D', wood:'#1E2E23', sand:'#2A2823', ice:'#2C3036', road2:'#36393F', path:'#44474D', poi:'#8E8E95', waterLabel:'#7FA8D0', stnFill:'#1E2023', stnStroke:'#E9E9EE', sel:'#3D9BFF', dimOp:0.2 }
};
let P = PAL[isDark() ? 'dark' : 'light'];

// ---------------------------------------------------------------- state
// map chips: the line kinds each one shows, and its swatch
const MODES = [
  { k: 'h', name: 'High-speed', kinds: 'h', sw: 'var(--hsr)' },
  { k: 'r', name: 'Rail', kinds: 'r', sw: 'var(--rail)' },
  { k: 's', name: 'Suburban', kinds: 's', sw: 'linear-gradient(90deg,#00985F 0 50%,#E2231A 50%)' },
  { k: 'm', name: 'Metro', kinds: 'm', sw: 'linear-gradient(90deg,#E4002B 0 50%,#0057B8 50%)' },
  { k: 'l', name: 'Light rail', kinds: 'l', sw: 'linear-gradient(90deg,#00A3E0 0 50%,#7F3F98 50%)' },
  { k: 't', name: 'Tram', kinds: 'tf', sw: '#3FA34D' },
];
const vis = { h: true, r: true, s: true, m: true, l: true, t: true };
let labelMode = 'both';   // en · both · local (native names)
let sel = null;          // selected line id
let selStation = null;
const view = { stack: [] };
let nav = 0;             // bumped by every navigation: a view whose data arrives after the next one started is dropped

// ---------------------------------------------------------------- style
const W = (pairs) => ['interpolate', ['exponential', 1.5], ['zoom'], ...pairs.flat()];
// "Both" labels add the local name only when it is in another script ("Tokyo / 東京", but not "Warsaw Central /
// Warszawa Centralna"): tiles give that second line as b (FORMAT v2.1); older tiles are judged by the first letter
const nonLatin = s => ['let', 'c', ['slice', s, 0, 1], ['all', ['>=', ['var', 'c'], 'ʰ'], ['any', ['<', ['var', 'c'], 'Ḁ'], ['>', ['var', 'c'], 'ỿ']]]];
function nameParts() {
  const n = ['get', 'n'], z = ['get', 'z'];
  if (labelMode === 'en') return [['case', ['==', n, ''], z, n]];
  if (labelMode === 'local') return [z];
  const b = ['coalesce', ['get', 'b'], ['case', ['all', ['!=', z, n], nonLatin(z)], z, '']];
  return [['case', ['==', n, ''], z, n], b];
}
function nameField(bullets = false) {
  const [a, b] = nameParts(), sec = { 'font-scale': 0.86, 'text-color': P.label2 };
  const img = bullets ? ['\n', {}, ['image', ['concat', 'bs|', ['to-string', ['coalesce', ['get', 'cx'], ['get', 'i']]]]], {}] : [];
  if (!b) return ['format', a, {}, ...img];
  return ['case', ['==', b, ''], ['format', a, {}, ...img], ['format', a, {}, '\n', {}, b, sec, ...img]];
}
// station name with a row of route bullets underneath (image generated on demand: see styleimagemissing)
const nameWithBullets = () => nameField(true);
// POI categories: [OpenMapTiles classes, light label colour, dark label colour]; dot colour = light colour
const POI_COLORS = {
  food: ['#F08A24', '#C8660B', '#F7A95A'], shop: ['#E6A700', '#A87A00', '#F2C94C'], hotel: ['#8E5CE6', '#7443CC', '#B596F2'],
  health: ['#E5484D', '#C53034', '#F07B7F'], edu: ['#9B7652', '#7D5A38', '#C6A27E'], park: ['#35A457', '#1F8440', '#6CCB86'],
  culture: ['#D8508F', '#B53574', '#EC89B7'], transport: ['#2E80E6', '#1C62BA', '#72A9F0'], other: ['#8E8E93', '#6C6C72', '#A1A1A8'],
};
const POI_CAT = ['match', ['get', 'class'],
  ['restaurant', 'fast_food', 'cafe', 'bar', 'beer', 'ice_cream', 'bakery', 'food_court', 'pub'], 'food',
  ['shop', 'grocery', 'clothing_store', 'supermarket', 'mall', 'department_store', 'jewelry', 'books', 'gift', 'shoes', 'hardware', 'mobile_phone', 'electronics', 'furniture', 'convenience', 'alcohol_shop', 'bag', 'cosmetics', 'florist', 'hairdresser', 'optician'], 'shop',
  ['lodging', 'hotel'], 'hotel',
  ['hospital', 'pharmacy', 'doctors', 'dentist', 'clinic', 'veterinary'], 'health',
  ['college', 'school', 'library', 'kindergarten', 'university'], 'edu',
  ['park', 'garden', 'playground', 'zoo', 'campsite', 'golf', 'stadium', 'pitch', 'sports_centre', 'swimming_pool', 'attraction'], 'park',
  ['museum', 'art_gallery', 'theatre', 'cinema', 'music', 'castle', 'monument', 'place_of_worship', 'religion', 'historic', 'arts_centre'], 'culture',
  ['airport', 'harbor', 'ferry_terminal', 'parking', 'fuel', 'car', 'bicycle_rental'], 'transport',
  'other'];
function makePoiDot(color) {
  const s = 13 * DPR; const [c, ctx] = canvas(s, s);
  ctx.beginPath(); ctx.arc(s / 2, s / 2, 5.3 * DPR, 0, 7); ctx.fillStyle = color; ctx.fill();
  ctx.lineWidth = 1.5 * DPR; ctx.strokeStyle = '#FFFFFF'; ctx.stroke();
  ctx.beginPath(); ctx.arc(s / 2, s / 2, 1.7 * DPR, 0, 7); ctx.fillStyle = '#FFFFFF'; ctx.fill();
  return img(c);
}
// base map (OpenMapTiles schema) names
const EN = ['coalesce', ['get', 'name:en'], ['get', 'name_en'], ['get', 'name:latin'], ['get', 'name']];
const LOCAL = ['coalesce', ['get', 'name'], ['get', 'name:latin']];
function baseName(both = true) {
  if (labelMode === 'local') return LOCAL;
  if (labelMode === 'en' || !both) return EN;
  return ['case', ['any', ['==', EN, LOCAL], ['!', nonLatin(LOCAL)]], EN, ['format', EN, {}, '\n', {}, LOCAL, { 'font-scale': 0.86, 'text-color': P.label2 }]];
}
function modeFilter(ks) { return ['in', ['get', 'k'], ['literal', ks]]; }
// line kinds currently switched on (Tram covers funiculars)
const shownKinds = () => MODES.flatMap(m => vis[m.k] ? [...m.kinds] : []);
// a station node is shown when a switched-on mode serves it (ks); a complex label when any mode serves the complex (kc)
// (false, not ['==', 1, 0], which MapLibre would read as a legacy filter and so the whole filter)
const servedBy = (prop, ks) => ks.length ? ['any', ...ks.map(k => ['in', k, ['coalesce', ['get', prop], '']])] : false;
function buildStyle() {
  const selKey = sel == null ? '|none|' : '|' + sel + '|';
  const dim = sel == null ? 1 : P.dimOp;
  const L = [];
  L.push({ id: 'bg', type: 'background', paint: { 'background-color': P.land } });
  // ---- base map: OpenFreeMap / OpenMapTiles
  const B = { source: 'base' };
  const cls = (...c) => ['in', ['get', 'class'], ['literal', c]];
  L.push({ ...B, id: 'landcover', type: 'fill', 'source-layer': 'landcover', filter: cls('wood', 'forest', 'grass', 'wetland', 'sand', 'ice', 'glacier', 'rock'),
    paint: { 'fill-color': ['match', ['get', 'class'], ['wood', 'forest'], P.wood, ['sand', 'rock'], P.sand, ['ice', 'glacier'], P.ice, P.park], 'fill-opacity': ['interpolate', ['linear'], ['zoom'], 3, 0.45, 10, 0.7, 14, 0.85] } });
  L.push({ ...B, id: 'landuse', type: 'fill', 'source-layer': 'landuse', filter: cls('residential', 'suburb', 'neighbourhood', 'commercial', 'industrial', 'retail', 'railway', 'quarry'),
    paint: { 'fill-color': P.urban, 'fill-opacity': ['interpolate', ['linear'], ['zoom'], 4, 0.6, 9, 1, 15, 0.8] } });
  L.push({ ...B, id: 'landuse-civic', type: 'fill', 'source-layer': 'landuse', minzoom: 11, filter: cls('school', 'university', 'college', 'hospital', 'kindergarten', 'stadium', 'pitch', 'cemetery', 'playground', 'zoo', 'theme_park'),
    paint: { 'fill-color': ['match', ['get', 'class'], ['stadium', 'pitch', 'playground', 'zoo', 'theme_park', 'cemetery'], P.park, P.uni] } });
  L.push({ ...B, id: 'park', type: 'fill', 'source-layer': 'park',
    paint: { 'fill-color': ['match', ['get', 'class'], ['national_park', 'nature_reserve', 'protected_area'], P.reserve, P.park], 'fill-opacity': ['interpolate', ['linear'], ['zoom'], 4, 0.5, 10, 0.9] } });
  L.push({ ...B, id: 'aeroway-area', type: 'fill', 'source-layer': 'aeroway', minzoom: 10, filter: ['==', ['geometry-type'], 'Polygon'], paint: { 'fill-color': P.airport } });
  L.push({ ...B, id: 'aeroway-runway', type: 'line', 'source-layer': 'aeroway', minzoom: 11, filter: ['all', ['==', ['geometry-type'], 'LineString'], cls('runway', 'taxiway')],
    paint: { 'line-color': P.roadCase, 'line-width': ['interpolate', ['exponential', 1.5], ['zoom'], 11, ['match', ['get', 'class'], 'runway', 2, 0.5], 17, ['match', ['get', 'class'], 'runway', 40, 10]] } });
  L.push({ ...B, id: 'waterway', type: 'line', 'source-layer': 'waterway', filter: ['any', cls('river', 'canal'), ['>=', ['zoom'], 13]], layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': P.water, 'line-width': ['interpolate', ['exponential', 1.5], ['zoom'], 8, ['match', ['get', 'class'], 'river', 0.8, 0.4], 14, ['match', ['get', 'class'], ['river', 'canal'], 3, 1], 18, ['match', ['get', 'class'], ['river', 'canal'], 12, 3]] } });
  L.push({ ...B, id: 'water', type: 'fill', 'source-layer': 'water', filter: ['!=', ['get', 'brunnel'], 'tunnel'], paint: { 'fill-color': P.water } });
  L.push({ ...B, id: 'building', type: 'fill', 'source-layer': 'building', minzoom: 14,
    paint: { 'fill-color': P.building, 'fill-outline-color': P.buildingEdge, 'fill-opacity': ['interpolate', ['linear'], ['zoom'], 14, 0, 15, 1] } });
  L.push({ ...B, id: 'bnd4', type: 'line', 'source-layer': 'boundary', minzoom: 4, filter: ['all', ['==', ['get', 'admin_level'], 4], ['!=', ['get', 'maritime'], 1]], layout: { 'line-join': 'round' },
    paint: { 'line-color': P.bnd4, 'line-width': W([[4, 0.6], [10, 1.2], [14, 2]]), 'line-dasharray': [3, 2] } });
  const bnd2 = ['all', ['==', ['get', 'admin_level'], 2], ['!=', ['get', 'maritime'], 1]];
  L.push({ ...B, id: 'bnd2-glow', type: 'line', 'source-layer': 'boundary', filter: ['all', bnd2, ['!=', ['get', 'disputed'], 1]], layout: { 'line-join': 'round' },
    paint: { 'line-color': P.bndGlow, 'line-width': W([[3, 3], [10, 8]]), 'line-opacity': 0.55 } });
  L.push({ ...B, id: 'bnd2', type: 'line', 'source-layer': 'boundary', filter: ['all', bnd2, ['!=', ['get', 'disputed'], 1]], layout: { 'line-join': 'round' },
    paint: { 'line-color': P.bnd, 'line-width': W([[3, 0.9], [10, 1.8]]) } });
  L.push({ ...B, id: 'bnd2-disputed', type: 'line', 'source-layer': 'boundary', filter: ['all', bnd2, ['==', ['get', 'disputed'], 1]], layout: { 'line-join': 'round' },
    paint: { 'line-color': P.bnd, 'line-width': W([[3, 0.9], [10, 1.8]]), 'line-dasharray': [2, 2] } });
  // roads (OpenMapTiles 'transportation'; rail classes are left to our own rail tiles)
  const ROADS = ['motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'minor', 'service'];
  const roadW = (stops) => ['interpolate', ['exponential', 1.5], ['zoom'], ...stops.flatMap(([z, m, t, p, s, te, mi, sv]) => [z, ['match', ['get', 'class'], 'motorway', m, 'trunk', t, 'primary', p, 'secondary', s, 'tertiary', te, 'minor', mi, sv]])];
  const roadF = ['all', ['==', ['geometry-type'], 'LineString'], cls(...ROADS)];
  const notTunnel = ['!=', ['get', 'brunnel'], 'tunnel'];
  L.push({ ...B, id: 'path', type: 'line', 'source-layer': 'transportation', minzoom: 14, filter: ['all', cls('path', 'track'), notTunnel],
    paint: { 'line-color': P.path, 'line-width': W([[14, 0.6], [18, 1.8]]), 'line-dasharray': [2, 1.5] } });
  L.push({ ...B, id: 'road-tunnel', type: 'line', 'source-layer': 'transportation', minzoom: 12, filter: ['all', roadF, ['==', ['get', 'brunnel'], 'tunnel']], layout: { 'line-join': 'round' },
    paint: { 'line-color': P.roadCase, 'line-opacity': 0.6, 'line-dasharray': [3, 2], 'line-width': roadW([[12, 1.6, 1.4, 1.2, 1, 0.8, 0.5, 0.3], [18, 18, 16, 15, 13, 12, 10, 5]]) } });
  L.push({ ...B, id: 'road-case', type: 'line', 'source-layer': 'transportation', minzoom: 11, filter: ['all', roadF, notTunnel], layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': ['match', ['get', 'class'], ['motorway', 'trunk'], P.motorCase, P.roadCase], 'line-width': roadW([[11, 3.2, 2.9, 2.6, 2.2, 1.8, 1.2, 0.6], [14, 7, 6.4, 6, 5.2, 4.4, 3.6, 2], [18, 26, 24, 22, 20, 18, 15, 8]]) } });
  L.push({ ...B, id: 'road', type: 'line', 'source-layer': 'transportation', minzoom: 5, filter: ['all', roadF, notTunnel,
      ['any', ['>=', ['zoom'], 11], cls('motorway', 'trunk', 'primary'), ['all', ['>=', ['zoom'], 8], cls('secondary')], ['all', ['>=', ['zoom'], 9.5], cls('tertiary')]]],
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': ['step', ['zoom'], P.roadLow, 11, ['match', ['get', 'class'], ['minor', 'service'], P.road2, ['motorway', 'trunk'], P.motor, P.road]],
      'line-width': roadW([[5, 0.5, 0.4, 0.3, 0.2, 0.2, 0.2, 0.1], [8, 1, 0.9, 0.7, 0.5, 0.4, 0.3, 0.2], [11, 1.6, 1.4, 1.2, 1, 0.8, 0.5, 0.3], [14, 5, 4.6, 4.2, 3.6, 3, 2.4, 1.2], [18, 22, 20, 18, 16, 14, 12, 6]]) } });
  // intercity rail, casings first (a line beside another is not cut by its casing). Track that high-speed and other
  // trains share is a pair of features (pair 1, same geometry): with both kinds shown they run side by side under one
  // casing, high-speed on the right of the pair's direction
  const on = k => vis[k] ? 'visible' : 'none', both = vis.h && vis.r, pair = ['==', ['get', 'pair'], 1];
  const HW = [[3, 0.9], [5, 1.35], [7, 1.9], [9, 2.6], [12, 3.6], [15, 5.2], [18, 7.5]], RW = [[3, 0.55], [5, 0.85], [7, 1.25], [9, 1.8], [12, 2.8], [15, 4.4], [18, 6.2]];
  const alone = k => both ? ['all', ['==', ['get', 'k'], k], ['!', pair]] : ['==', ['get', 'k'], k];
  const half = (ws, s) => both ? W(ws.map(([z, w]) => [z, ['case', pair, s * w / 2, 0]])) : 0;
  L.push({ id: 'rail-r-case', type: 'line', source: 'rail', 'source-layer': 'rail', minzoom: 8, filter: alone('r'), layout: { visibility: on('r'), 'line-join': 'round' },
    paint: { 'line-color': P.casing, 'line-opacity': dim, 'line-width': W([[8, 3], [12, 5], [15, 7.4], [18, 10]]) } });
  L.push({ id: 'rail-p-case', type: 'line', source: 'rail', 'source-layer': 'rail', minzoom: 6, filter: ['all', pair, ['==', ['get', 'k'], 'h']], layout: { visibility: both ? 'visible' : 'none', 'line-join': 'round' },
    paint: { 'line-color': P.casing, 'line-opacity': dim, 'line-width': W(HW.map(([z, w], i) => [z, w + RW[i][1] + [1.2, 1.4, 1.6, 2, 2.6, 3.2, 4][i]])),
      'line-offset': W(HW.map(([z, w], i) => [z, (w - RW[i][1]) / 2])) } });
  L.push({ id: 'rail-h-case', type: 'line', source: 'rail', 'source-layer': 'rail', minzoom: 6, filter: alone('h'), layout: { visibility: on('h'), 'line-join': 'round' },
    paint: { 'line-color': P.casing, 'line-opacity': dim, 'line-width': W([[6, 3], [9, 4.6], [12, 6.2], [15, 8.4], [18, 11.5]]) } });
  L.push({ id: 'rail-r', type: 'line', source: 'rail', 'source-layer': 'rail', filter: ['==', ['get', 'k'], 'r'], layout: { visibility: on('r'), 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': P.rail, 'line-opacity': dim, 'line-width': W(RW), 'line-offset': half(RW, -1) } });
  L.push({ id: 'rail-h', type: 'line', source: 'rail', 'source-layer': 'rail', filter: ['==', ['get', 'k'], 'h'], layout: { visibility: on('h'), 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': P.hsr, 'line-opacity': dim, 'line-width': W(HW), 'line-offset': half(HW, 1) } });
  // urban rail
  const ucase = { type: 'line', source: 'rail', 'source-layer': 'rail', minzoom: 10, layout: { 'line-join': 'round', 'line-cap': 'round' } };
  // (only for the kinds shown: a hidden suburban line's casing would hide the intercity line on its track)
  L.push(Object.assign({ id: 'u-case', filter: ['in', ['get', 'k'], ['literal', ['m', 'l', 's', 't'].filter(k => shownKinds().includes(k))]], paint: { 'line-color': P.casing, 'line-opacity': sel == null ? 0.95 : 0.3, 'line-width': ['interpolate', ['exponential', 1.5], ['zoom'], 10, ['match', ['get', 'k'], 't', 2.8, 'l', 3.6, 4.4], 14, ['match', ['get', 'k'], 't', 5.5, 'l', 7.5, 9], 17, ['match', ['get', 'k'], 't', 9, 'l', 12, 14]] } }, ucase));
  const uline = (id, k, w, extra = {}) => ({ id, type: 'line', source: 'rail', 'source-layer': 'rail', filter: ['==', ['get', 'k'], k], layout: Object.assign({ visibility: on(id === 'u-f' ? 't' : k), 'line-join': 'round', 'line-cap': 'round' }, extra.layout || {}), paint: Object.assign({ 'line-color': ['coalesce', ['get', 'c'], P.rail], 'line-opacity': dim, 'line-width': W(w) }, extra.paint || {}) });
  L.push(uline('u-t', 't', [[9, 0.8], [12, 1.8], [14, 3], [17, 6]]));
  L.push(uline('u-f', 'f', [[11, 1], [14, 2], [17, 4]], { paint: { 'line-dasharray': [1, 1] } }));
  L.push(uline('u-l', 'l', [[8, 0.8], [11, 1.8], [14, 4.4], [17, 8.5]]));
  L.push(uline('u-s', 's', [[7, 0.8], [10, 1.6], [12, 2.8], [14, 4.8], [17, 9.5]]));
  L.push(uline('u-m', 'm', [[7, 0.9], [9, 1.6], [11, 2.6], [12, 3.4], [14, 5.6], [17, 11]]));
  // selected line
  const selF = ['in', selKey, ['get', 'ls']];
  L.push({ id: 'sel-case', type: 'line', source: 'rail', 'source-layer': 'rail', filter: selF, layout: { 'line-join': 'round', 'line-cap': 'round' }, paint: { 'line-color': P.casing, 'line-width': W([[3, 3.4], [8, 5.6], [12, 8.4], [14, 11], [17, 16]]) } });
  L.push({ id: 'sel-line', type: 'line', source: 'rail', 'source-layer': 'rail', filter: selF, layout: { 'line-join': 'round', 'line-cap': 'round' }, paint: { 'line-color': sel != null ? lineColor(sel) : P.sel, 'line-width': W([[3, 1.8], [8, 3.2], [12, 5], [14, 7], [17, 11]]) } });
  // world overview, below the zoom where the rail tiles show every city's lines: a dot for every city with urban rail
  const z0 = Math.max(3, manifest.rail.minzoom), fade = ['interpolate', ['linear'], ['zoom'], z0 - 0.4, 1, z0 + 0.4, 0];
  L.push({ id: 'city-dot', type: 'circle', source: 'cities', maxzoom: z0 + 0.4,
    paint: { 'circle-color': P.city, 'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, ['interpolate', ['linear'], ['get', 'n'], 1, 1.6, 30, 3.6], z0, ['interpolate', ['linear'], ['get', 'n'], 1, 2.4, 30, 5]],
      'circle-stroke-color': P.casing, 'circle-stroke-width': 1, 'circle-opacity': fade, 'circle-stroke-opacity': fade } });

  // ---- base map labels. MapLibre places symbols from the top layer down, so these go below every rail symbol
  // (rail station names win every collision) and, among themselves, cities are placed first and countries last.
  const halo = { 'text-halo-color': P.halo, 'text-halo-width': 1.5 };
  const waterName = (id, geom, extra) => ({ ...B, id, type: 'symbol', 'source-layer': 'water_name', filter: ['==', ['geometry-type'], geom], ...extra,
    layout: { 'text-field': baseName(), 'text-font': ['NotoSansRegular'], 'text-size': ['match', ['get', 'class'], 'ocean', 14, 'sea', 12.5, 11.5], 'text-max-width': 6, 'text-letter-spacing': 0.08, 'symbol-placement': geom === 'Point' ? 'point' : 'line' },
    paint: { 'text-color': P.waterLabel, ...halo } });
  L.push(waterName('water-name', 'Point', {}));
  L.push(waterName('water-name-line', 'LineString', { minzoom: 10 }));
  L.push({ ...B, id: 'waterway-name', type: 'symbol', 'source-layer': 'waterway', minzoom: 12, filter: cls('river', 'canal'),
    layout: { 'text-field': baseName(), 'text-font': ['NotoSansRegular'], 'text-size': 11, 'symbol-placement': 'line', 'symbol-spacing': 400 },
    paint: { 'text-color': P.waterLabel, ...halo } });
  L.push({ ...B, id: 'road-name', type: 'symbol', 'source-layer': 'transportation_name', minzoom: 13, filter: cls('motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'minor'),
    layout: { 'text-field': baseName(), 'text-font': ['NotoSansRegular'], 'text-size': ['interpolate', ['linear'], ['zoom'], 13, 10, 17, 12.5], 'symbol-placement': 'line', 'symbol-spacing': 350, 'text-max-angle': 30, 'text-padding': 2, 'text-line-height': 1 },
    paint: { 'text-color': P.label2, ...halo } });
  // road numbers: small and grey (China's G/S expressways keep their colours), only once the streets are drawn
  const th = isDark() ? 'D' : 'L';
  L.push({ ...B, id: 'road-shield', type: 'symbol', 'source-layer': 'transportation_name', minzoom: 12,
    filter: ['all', ['has', 'ref'], cls('motorway', 'trunk', 'primary'), ['any', cls('motorway', 'trunk'), ['>=', ['zoom'], 13]]],
    layout: { 'symbol-placement': 'line', 'symbol-spacing': 600, 'icon-rotation-alignment': 'viewport', 'icon-padding': 8,
      'icon-image': ['concat', 'sh|', th, '|', ['match', ['get', 'class'], 'motorway', 'm', 'o'], '|',
        ['let', 'r', ['get', 'ref'], ['slice', ['var', 'r'], 0, ['case', ['>=', ['index-of', ';', ['var', 'r']], 0], ['index-of', ';', ['var', 'r']], ['min', ['length', ['var', 'r']], 7]]]]] },
    paint: { 'icon-opacity': 0.85 } });
  // points of interest: colour-coded by category, label in the same hue (as Apple Maps does)
  const poiF = ['all', ['<=', ['get', 'rank'], ['step', ['zoom'], 3, 16, 12, 17, 30]], ['!', cls('railway', 'bus', 'aerialway')]];
  L.push({ ...B, id: 'poi', type: 'symbol', 'source-layer': 'poi', minzoom: 15, filter: poiF,
    layout: { 'icon-image': ['concat', 'pc|', POI_CAT], 'icon-size': 1, 'text-field': baseName(), 'text-font': ['NotoSansMedium'], 'text-size': 11, 'text-max-width': 8,
      'text-anchor': 'left', 'text-offset': [0.85, 0], 'text-line-height': 1.1, 'text-optional': true, 'symbol-sort-key': ['get', 'rank'], 'text-padding': 3 },
    paint: { 'text-color': ['match', POI_CAT, ...Object.entries(POI_COLORS).flatMap(([k, v]) => [k, isDark() ? v[2] : v[1]]), P.poi], ...halo } });
  L.push({ ...B, id: 'airport', type: 'symbol', 'source-layer': 'aerodrome_label', minzoom: 8, filter: ['has', 'iata'],
    layout: { 'icon-image': 'ap', 'icon-size': ['interpolate', ['linear'], ['zoom'], 8, 0.8, 12, 1], 'text-field': ['step', ['zoom'], ['get', 'iata'], 11, baseName()], 'text-font': ['NotoSansMedium'],
      'text-size': ['interpolate', ['linear'], ['zoom'], 8, 11, 14, 13], 'text-max-width': 8, 'text-anchor': 'left', 'text-offset': [1.05, 0], 'text-optional': true, 'text-padding': 4 },
    paint: { 'text-color': isDark() ? '#6FB1FF' : '#1767D2', ...halo } });
  const place = (id, classes, minzoom, size, font, color, extra = {}) => ({ ...B, id, type: 'symbol', 'source-layer': 'place', minzoom, ...(extra.maxzoom ? { maxzoom: extra.maxzoom } : {}), filter: ['all', cls(...classes), ...(extra.filter || [])],
    layout: Object.assign({ 'text-field': baseName(), 'text-font': [font], 'text-size': size, 'text-max-width': 8, 'text-line-height': 1.1, 'symbol-sort-key': ['get', 'rank'], 'text-padding': 4 }, extra.layout || {}),
    paint: Object.assign({ 'text-color': color, ...halo }, extra.paint || {}) });
  // countries fade out by z7; regions (in English only in "Both" mode) show at z5.5-8, where they no longer hide cities
  L.push(place('place-country', ['country'], 1, ['interpolate', ['linear'], ['zoom'], 1, 11, 5, 15], 'NotoSansMedium', P.label2,
    { layout: { 'text-transform': 'uppercase', 'text-letter-spacing': 0.12, 'text-max-width': 6 }, paint: { 'text-opacity': ['interpolate', ['linear'], ['zoom'], 6, 1, 7, 0] } }));
  L.push(place('place-state', ['state', 'province'], 5.5, ['interpolate', ['linear'], ['zoom'], 5.5, 10.5, 7, 12], 'NotoSansRegular', P.label2,
    { layout: { 'text-field': baseName(false), 'text-transform': 'uppercase', 'text-letter-spacing': 0.1, 'text-max-width': 7 }, paint: { 'text-opacity': ['interpolate', ['linear'], ['zoom'], 5.5, 0.7, 7, 0.7, 8, 0] } }));
  L.push(place('place-s', ['suburb', 'quarter', 'neighbourhood'], 12, ['interpolate', ['linear'], ['zoom'], 12, 10, 16, 12], 'NotoSansRegular', P.town, { layout: { 'text-transform': 'uppercase', 'text-letter-spacing': 0.06 }, paint: { 'text-opacity': 0.75 } }));
  L.push(place('place-v', ['village', 'hamlet'], 12, ['interpolate', ['linear'], ['zoom'], 12, 10.5, 16, 12.5], 'NotoSansRegular', P.town));
  // towns and cities: from z12, where metro stations are named, below them; before that above the metro dots (see below)
  const towns = (id, minzoom, maxzoom) => place(id, ['town'], minzoom, ['interpolate', ['linear'], ['zoom'], 9, 11, 14, 13.5], 'NotoSansRegular', P.town, { maxzoom });
  const cities = (id, minzoom, maxzoom) => place(id, ['city'], minzoom, ['interpolate', ['linear'], ['zoom'], 3, ['case', ['==', ['get', 'capital'], 2], 13, ['<=', ['get', 'rank'], 3], 12, 11], 10, ['case', ['==', ['get', 'capital'], 2], 18, ['<=', ['get', 'rank'], 4], 16, 14], 14, 17], 'NotoSansMedium', P.city,
    { filter: [['<=', ['coalesce', ['get', 'rank'], 99], ['step', ['zoom'], 5, 5, 8, 6, 11, 7, 99]]], maxzoom });
  L.push(towns('place-t', 12), cities('place-c', 12));

  // ---- rail labels, placed before all of the above: line names, route badges, then the stations (below)
  const railKinds = ['h', 'r'].filter(k => vis[k]);
  // (a pair is named once: after its high-speed line while that is shown)
  L.push({ id: 'rail-name', type: 'symbol', source: 'rail', 'source-layer': 'rail', minzoom: 6, maxzoom: 12,
    filter: ['all', ['has', 'nm'], ['in', ['get', 'k'], ['literal', railKinds]], ['!', ['all', pair, ['==', ['get', 'k'], 'r'], vis.h]]],
    layout: { visibility: sel == null ? 'visible' : 'none', 'symbol-placement': 'line', 'symbol-spacing': 520, 'text-field': labelMode === 'local' ? ['get', 'nz'] : ['get', 'nm'],
      'text-font': ['NotoSansMedium'], 'text-size': ['interpolate', ['linear'], ['zoom'], 6, 10.5, 11, 12.5], 'text-max-angle': 28, 'text-letter-spacing': 0.02, 'text-padding': 6,
      'symbol-sort-key': ['match', ['get', 'k'], 'h', 0, 1] },
    paint: { 'text-color': ['match', ['get', 'k'], 'h', P.hsrText, P.railText], 'text-halo-color': P.halo, 'text-halo-width': 2 } });
  // route badges: public line codes only ("U4", "S51", "C-3"), not mapper codes ("SN3.12")
  const ref = ['coalesce', ['get', 'r'], ''];
  L.push({ id: 'badges', type: 'symbol', source: 'rail', 'source-layer': 'rail', minzoom: 12, filter: ['all', modeFilter(['m', 'l', 's']), ['!=', ref, ''], ['<=', ['length', ref], 5], ['!', ['in', '.', ref]]],
    layout: { 'symbol-placement': 'line', 'symbol-spacing': 420, 'icon-image': ['concat', 'b|', ['get', 'c'], '|', ['get', 'r']], 'icon-rotation-alignment': 'viewport', 'icon-size': 1, 'icon-padding': 4, visibility: sel == null ? 'visible' : 'none' } });
  // Stations, in placement order (top first): the selected station, the selected line's stations, rail stations
  // (by rank), town and city names below z12, metro dots (always drawn; later labels avoid them), metro labels. One
  // label per station complex, on its main station (rep). Rail stations: white dots ringed in the line colour, hubs
  // ringed dark, from z11 a train glyph (China Railway's logo in China); suburban stations ringed in their line's colour.
  // The selected station keeps its own marker, a size up (a rail station its train glyph at every zoom), and its name
  // goes under it in bold.
  const one = ['==', ['get', 'i'], selStation ?? -1], grow = v => ['*', v, ['case', one, 1.3, 1]];
  const sk = shownKinds(), rk = ['get', 'rk'], rep1 = ['==', ['get', 'rep'], 1], only = ['all', rep1, ['!', one]];
  const railOn = vis.h || vis.r || vis.s;
  const hub = ['any', ['>=', rk, 10], ['>', ['get', 'x'], 2]];
  const glyph = ['match', ['get', 'k'], 'h', 'st-h', 's', 'st-s', 'st-r'];
  const ud = ['all', ['!', ['in', ['get', 'k'], ['literal', ['h', 'r', 's']]]], servedBy('ks', sk)], uk = ['all', ud, ['!', one]];
  // if rail is switched off, metro stations inside rail hubs label themselves
  const labelled = railOn ? rep1 : ['any', rep1, ...['h', 'r', 's'].map(k => ['in', k, ['coalesce', ['get', 'kc'], '']])];
  const uLabel = (id, minzoom, extra) => ({ id, type: 'symbol', source: 'rail', 'source-layer': 'stn', minzoom, filter: ['all', uk, labelled, extra],
    layout: { visibility: sel == null ? 'visible' : 'none', 'text-field': ['step', ['zoom'], nameField(), 14.5, nameWithBullets()], 'text-font': ['NotoSansMedium'],
      'text-size': ['interpolate', ['linear'], ['zoom'], 12, 11, 16, 13.5], 'text-variable-anchor': ['left', 'right', 'top', 'bottom'], 'text-radial-offset': 0.85, 'text-justify': 'auto',
      'text-max-width': 10, 'text-line-height': 1.2, 'symbol-sort-key': ['-', 0, ['get', 'x']] },
    paint: { 'text-color': P.label, 'text-halo-color': P.halo, 'text-halo-width': 1.6, 'text-opacity': sel == null ? 1 : 0.3 } });
  L.push(uLabel('stn-u1-label', 12.5, ['<=', ['get', 'x'], 1]));
  L.push(uLabel('stn-u-label', 11, ['>', ['get', 'x'], 1]));
  // metro station dots as icons (not circles) so that route badges and metro labels avoid them
  const uDot = (id, minzoom, extra, transfer) => ({ id, type: 'symbol', source: 'rail', 'source-layer': 'stn', minzoom, filter: ['all', ud, extra],
    layout: { 'icon-image': transfer ? 'hubm|' + th : ['concat', 'sd|' + th + '|', ['coalesce', ['get', 'c'], '#888888']],
      'icon-size': ['interpolate', ['linear'], ['zoom'], 11, grow(transfer ? 0.42 : 0.38), 14, grow(transfer ? 0.85 : 0.78), 17, grow(transfer ? 1.3 : 1.15)],
      'icon-allow-overlap': true, 'icon-padding': 0, 'symbol-sort-key': ['-', 0, ['get', 'x']] },
    paint: { 'icon-opacity': sel == null ? 1 : 0.3 } });
  L.push(uDot('stn-u1', 10.5, ['<=', ['get', 'x'], 1], false));
  L.push(uDot('stn-u', 10, ['>', ['get', 'x'], 1], true));
  // town and city names up to z12 (before metro stations are named) go before the metro dots, which would hide them
  L.push(towns('place-t-lz', 9, 12), cities('place-c-lz', 3, 12));
  // rail station names by rank: 11+ below z4, 10+ to z6, 8+ to z8, 5+ to z10, then all
  const railLabel = (min, f) => ['case', min == null ? only : ['all', only, ['>=', rk, min]], f, ''];
  L.push({ id: 'stn-rail', type: 'symbol', source: 'rail', 'source-layer': 'stn', filter: ['all', ['in', ['get', 'k'], ['literal', ['h', 'r', 's']]], servedBy('ks', sk)],
    layout: { 'icon-image': ['step', ['zoom'], ['case', one, glyph, hub, 'hub', ['match', ['get', 'k'], 'h', 'dot-h', 'r', 'dot-r', ['concat', 'ds|', th, '|', ['coalesce', ['get', 'c'], '']]]],
        11, logoReady.cr ? ['case', ['all', ['!=', ['get', 'k'], 's'], inChina()], 'lg|cr', glyph] : glyph],
      'icon-size': ['interpolate', ['linear'], ['zoom'], 4, grow(0.6), 8, grow(0.85), 11, grow(0.8), 14, grow(1)], 'icon-allow-overlap': true, 'icon-ignore-placement': true,
      'symbol-sort-key': ['-', 0, rk],
      'text-field': sel != null ? '' : ['step', ['zoom'], railLabel(11, nameField()), 4, railLabel(10, nameField()), 6, railLabel(8, nameField()), 8, railLabel(5, nameField()),
        10, railLabel(null, nameField()), 14.5, railLabel(null, nameWithBullets())],
      'text-font': ['case', hub, ['literal', ['NotoSansMedium']], ['literal', ['NotoSansRegular']]],
      'text-size': ['interpolate', ['linear'], ['zoom'], 5, ['case', hub, 12, 10.5], 10, ['case', hub, 14, 12], 16, ['case', hub, 16, 13.5]],
      'text-justify': 'auto', 'text-optional': true, 'text-max-width': 12, 'text-line-height': 1.15,
      'text-variable-anchor': ['left', 'right', 'top', 'bottom'], 'text-radial-offset': ['step', ['zoom'], 0.7, 11, 1.05] },
    paint: { 'text-color': ['case', hub, P.label, P.label2], 'text-halo-color': P.halo, 'text-halo-width': 1.8,
      // one dot per complex below z9; below z7 only for the higher ranks
      'icon-opacity': sel != null ? 0.35 : ['step', ['zoom'], ['case', ['any', one, ['all', rep1, ['>=', rk, 8]]], 1, 0], 6, ['case', ['any', one, ['all', rep1, ['>=', rk, 5]]], 1, 0],
        7, ['case', ['any', one, rep1], 1, 0], 9, 1] } });
  // selected line stations (always labelled): white dots, ringed in an urban line's own colour, else dark
  const sl = sel != null && D.lines[sel], ringCol = sl && !'hr'.includes(sl[0]) && sl[4] ? sl[4] : P.stnStroke;
  L.push({ id: 'sel-stn', type: 'circle', source: 'rail', 'source-layer': 'stn', filter: selF,
    paint: { 'circle-color': P.stnFill, 'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 2.5, 10, 4, 14, 6.5, 17, 8], 'circle-stroke-color': ['case', ['>', ['get', 'x'], 1], P.stnStroke, ringCol], 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 5, 1.4, 12, 2.2, 17, 3] } });
  L.push({ id: 'sel-stn-label', type: 'symbol', source: 'rail', 'source-layer': 'stn', filter: selF,
    layout: { 'text-field': nameField(), 'text-font': ['NotoSansMedium'], 'text-size': ['interpolate', ['linear'], ['zoom'], 6, 11, 16, 14], 'text-variable-anchor': ['left', 'right', 'top', 'bottom'], 'text-radial-offset': 0.9, 'text-justify': 'auto', 'text-max-width': 10, 'text-line-height': 1.15, 'symbol-sort-key': ['-', 0, rk] },
    paint: { 'text-color': P.label, 'text-halo-color': P.halo, 'text-halo-width': 1.8 } });
  // the selected station: its name under it (the operators' logos go above it)
  L.push({ id: 'sel-one-label', type: 'symbol', source: 'rail', 'source-layer': 'stn', filter: one,
    layout: { 'text-field': nameField(), 'text-font': ['NotoSansMedium'], 'text-size': ['interpolate', ['linear'], ['zoom'], 6, 13, 16, 15], 'text-anchor': 'top', 'text-offset': [0, 1.3],
      'text-max-width': 12, 'text-line-height': 1.15, 'text-allow-overlap': true },
    paint: { 'text-color': P.label, 'text-halo-color': P.halo, 'text-halo-width': 2 } });
  return {
    version: 8,
    glyphs: 'tpg://{fontstack}/{range}',
    sources: {
      base: { type: 'vector', url: OFM + '/planet' },
      rail: { type: 'vector', tiles: ['tp://rail/{z}/{x}/{y}'], minzoom: manifest.rail.minzoom, maxzoom: manifest.rail.maxzoom },
      cities: { type: 'geojson', data: CITY_DOTS },
    },
    layers: L,
  };
}

// ---------------------------------------------------------------- images (badges, station icons)
const DPR = Math.min(3, Math.max(2, window.devicePixelRatio || 1));
function lum(hex) {
  const h = hex.replace('#', ''); const r = parseInt(h.slice(0, 2), 16) / 255, g = parseInt(h.slice(2, 4), 16) / 255, b = parseInt(h.slice(4, 6), 16) / 255;
  const f = v => v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}
function textOn(hex) { return lum(hex || '#888888') > 0.52 ? '#1C1C1E' : '#FFFFFF'; }
function makeBadge(color, text) {
  const fs = 11 * DPR, h = 17 * DPR, pad = 4.5 * DPR;
  const c = document.createElement('canvas'); const ctx = c.getContext('2d');
  ctx.font = `700 ${fs}px -apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif`;
  const w = Math.max(h, Math.ceil(ctx.measureText(text).width + pad * 2));
  c.width = w + 2 * DPR; c.height = h + 2 * DPR;
  ctx.font = `700 ${fs}px -apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif`;
  const r = 4.5 * DPR;
  ctx.fillStyle = '#FFFFFF';
  rr(ctx, 0, 0, w + 2 * DPR, h + 2 * DPR, r + DPR); ctx.fill();
  ctx.fillStyle = color; rr(ctx, DPR, DPR, w, h, r); ctx.fill();
  ctx.fillStyle = textOn(color); ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(text, DPR + w / 2, DPR + h / 2 + 0.5 * DPR);
  return { width: c.width, height: c.height, data: ctx.getImageData(0, 0, c.width, c.height).data };
}
function rr(ctx, x, y, w, h, r) { ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath(); }
function makeTrainIcon(bg) {
  const s = 20 * DPR; const c = document.createElement('canvas'); c.width = s; c.height = s; const ctx = c.getContext('2d');
  ctx.fillStyle = '#FFFFFF'; rr(ctx, 0, 0, s, s, 5.5 * DPR); ctx.fill();
  ctx.fillStyle = bg; rr(ctx, 1.5 * DPR, 1.5 * DPR, s - 3 * DPR, s - 3 * DPR, 4.5 * DPR); ctx.fill();
  const u = DPR; ctx.fillStyle = '#FFFFFF';
  rr(ctx, 6 * u, 4.2 * u, 8 * u, 9.2 * u, 2.4 * u); ctx.fill();
  ctx.fillStyle = bg; rr(ctx, 7.3 * u, 5.6 * u, 5.4 * u, 3.4 * u, 0.8 * u); ctx.fill();
  ctx.beginPath(); ctx.arc(8.2 * u, 11 * u, 0.85 * u, 0, 7); ctx.arc(11.8 * u, 11 * u, 0.85 * u, 0, 7); ctx.fill();
  ctx.strokeStyle = '#FFFFFF'; ctx.lineWidth = 1.3 * u; ctx.lineCap = 'round';
  ctx.beginPath(); ctx.moveTo(7.6 * u, 14.2 * u); ctx.lineTo(6.2 * u, 16 * u); ctx.moveTo(12.4 * u, 14.2 * u); ctx.lineTo(13.8 * u, 16 * u); ctx.stroke();
  return { width: s, height: s, data: ctx.getImageData(0, 0, s, s).data };
}
function canvas(w, h) { const c = document.createElement('canvas'); c.width = Math.ceil(w); c.height = Math.ceil(h); return [c, c.getContext('2d')]; }
function img(c) { return { width: c.width, height: c.height, data: c.getContext('2d').getImageData(0, 0, c.width, c.height).data }; }
// station dot: white disc with a coloured ring (Apple-style)
function makeDot(ring, r, w, fill) {
  const s = (r + w + 1) * 2 * DPR; const [c, ctx] = canvas(s, s);
  ctx.beginPath(); ctx.arc(s / 2, s / 2, r * DPR, 0, 7); ctx.fillStyle = fill; ctx.fill();
  ctx.lineWidth = w * DPR; ctx.strokeStyle = ring; ctx.stroke();
  return img(c);
}
// row of route bullets for a station label
function makeBulletRow(si) {
  const all = D.stations[si] ? allLinesAt(si) : [];
  const ids = sortLines(all.filter(x => 'mlstf'.includes(D.lines[x][0]))).slice(0, 8);
  const extra = [];
  if (all.some(x => D.lines[x][0] === 'h')) extra.push({ t: 'HSR', col: HSR_BADGE });
  if (all.some(x => D.lines[x][0] === 'r')) extra.push({ t: 'RL', col: RAIL_BADGE });
  if (!ids.length && !extra.length) { const [c] = canvas(1, 1); return img(c); }
  const fs = 10.5 * DPR, h = 15 * DPR, pad = 3.5 * DPR, gap = 2.5 * DPR;
  const [m, mctx] = canvas(1, 1); mctx.font = `700 ${fs}px -apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif`;
  const items = ids.map(i => ({ t: badgeText(D.lines[i]), col: D.lines[i][4] || (D.lines[i][0] === 't' ? '#3FA34D' : '#888888') })).concat(extra)
    .map(it => ({ ...it, w: it.t ? Math.max(h, mctx.measureText(it.t).width + pad * 2) : h * 0.7 }));
  const W = items.reduce((a, b) => a + b.w, 0) + gap * (items.length - 1) + 2 * DPR;
  const [c, ctx] = canvas(W, h + 2 * DPR);
  ctx.font = mctx.font; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  let x = DPR;
  for (const it of items) {
    ctx.fillStyle = '#FFFFFF'; rr(ctx, x - DPR, 0, it.w + 2 * DPR, h + 2 * DPR, 5 * DPR); ctx.fill();
    ctx.fillStyle = it.col;
    if (it.t) { rr(ctx, x, DPR, it.w, h, 4 * DPR); ctx.fill(); ctx.fillStyle = textOn(it.col); ctx.fillText(it.t, x + it.w / 2, DPR + h / 2 + 0.5 * DPR); }
    else { ctx.beginPath(); ctx.arc(x + it.w / 2, DPR + h / 2, it.w / 2, 0, 7); ctx.fill(); }
    x += it.w + gap;
  }
  return img(c);
}
// road shields: small grey boxes; China's expressways green (red band = national G, yellow band = provincial S),
// its other national roads red and provincial ones yellow
function makeShield(dark, kind, ref) {
  const fs = 9 * DPR, h = 13 * DPR, pad = 3.5 * DPR;
  const [m, mctx] = canvas(1, 1); const font = `600 ${fs}px -apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif`; mctx.font = font;
  const w = Math.max(h * 1.2, mctx.measureText(ref).width + pad * 2);
  const [c, ctx] = canvas(w + 2 * DPR, h + 2 * DPR); ctx.font = font; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  const cn = /^[GS]\d/.test(ref), p = ref[0], edge = dark ? '#1E2023' : '#FFFFFF';
  let bg = dark ? '#3A3D43' : '#F7F6F3', fg = dark ? '#A1A1A8' : '#6C6C72', band = null;
  if (cn && kind === 'm') { bg = '#1E7B4A'; fg = '#FFFFFF'; band = p === 'G' ? '#D0202E' : '#F2B705'; }
  else if (cn) { bg = p === 'G' ? '#C8202E' : '#F2B705'; fg = p === 'G' ? '#FFFFFF' : '#1C1C1E'; }
  ctx.fillStyle = edge; rr(ctx, 0, 0, w + 2 * DPR, h + 2 * DPR, 4 * DPR); ctx.fill();
  ctx.fillStyle = bg; rr(ctx, DPR, DPR, w, h, 3 * DPR); ctx.fill();
  if (!cn) { ctx.strokeStyle = dark ? '#55585F' : '#C9C5BD'; ctx.lineWidth = DPR; rr(ctx, 1.5 * DPR, 1.5 * DPR, w - DPR, h - DPR, 3 * DPR); ctx.stroke(); }
  if (band) { ctx.save(); rr(ctx, DPR, DPR, w, h, 3 * DPR); ctx.clip(); ctx.fillStyle = band; ctx.fillRect(DPR, DPR, w, 3 * DPR); ctx.restore(); }
  ctx.fillStyle = fg; ctx.fillText(ref, DPR + w / 2, DPR + h / 2 + (band ? 1.2 : 0.5) * DPR);
  return img(c);
}
// airport: blue tile with a plane
function makeAirport() {
  const s = 18 * DPR, u = DPR; const [c, ctx] = canvas(s, s);
  ctx.fillStyle = '#FFFFFF'; rr(ctx, 0, 0, s, s, 5 * u); ctx.fill();
  ctx.fillStyle = '#1C7CF4'; rr(ctx, 1.2 * u, 1.2 * u, s - 2.4 * u, s - 2.4 * u, 4 * u); ctx.fill();
  ctx.save(); ctx.translate(s / 2, s / 2); ctx.rotate(Math.PI / 4); ctx.fillStyle = '#FFFFFF';
  ctx.beginPath();
  ctx.moveTo(0, -6 * u); ctx.quadraticCurveTo(1 * u, -6 * u, 1 * u, -4.5 * u); ctx.lineTo(1 * u, -1.2 * u); ctx.lineTo(5.5 * u, 1.3 * u); ctx.lineTo(5.5 * u, 2.4 * u);
  ctx.lineTo(1 * u, 1 * u); ctx.lineTo(1 * u, 3.8 * u); ctx.lineTo(2.4 * u, 5 * u); ctx.lineTo(2.4 * u, 5.9 * u); ctx.lineTo(0, 5.1 * u);
  ctx.lineTo(-2.4 * u, 5.9 * u); ctx.lineTo(-2.4 * u, 5 * u); ctx.lineTo(-1 * u, 3.8 * u); ctx.lineTo(-1 * u, 1 * u); ctx.lineTo(-5.5 * u, 2.4 * u); ctx.lineTo(-5.5 * u, 1.3 * u);
  ctx.lineTo(-1 * u, -1.2 * u); ctx.lineTo(-1 * u, -4.5 * u); ctx.quadraticCurveTo(-1 * u, -6 * u, 0, -6 * u); ctx.fill(); ctx.restore();
  return img(c);
}
const HSR_BADGE = '#2E63D6', RAIL_BADGE = '#8657D8', SUB_BADGE = '#2F8A5F';
function addIcons(map, force = false) {
  // add once; on a theme change update in place (same size), which keeps laid-out tiles valid
  const set = (id, im) => { if (!map.hasImage(id)) map.addImage(id, im, { pixelRatio: DPR }); else if (force) map.updateImage(id, im); };
  set('st-h', makeTrainIcon(HSR_BADGE)); set('st-r', makeTrainIcon(RAIL_BADGE)); set('st-s', makeTrainIcon(SUB_BADGE));
  set('dot-h', makeDot(P.hsr, 3.2, 1.7, P.stnFill)); set('dot-r', makeDot(P.rail, 2.8, 1.5, P.stnFill));
  set('hub', makeDot(P.stnStroke, 4.2, 2, P.stnFill)); set('ap', makeAirport());
}
// China Railway logo as the station icon; stays on the drawn train glyph if the image can't be used (CORS/offline)
function loadRailLogo() {
  const u = LOGOS.cr && LOGOS.cr.url; if (!u) return;
  const im = new Image(); im.crossOrigin = 'anonymous'; im.referrerPolicy = 'no-referrer';
  im.onload = () => {
    try {
      const sz = 22 * DPR; const [c, ctx] = canvas(sz, sz);
      ctx.fillStyle = '#FFFFFF'; rr(ctx, 0, 0, sz, sz, 6 * DPR); ctx.fill();
      ctx.strokeStyle = 'rgba(0,0,0,0.18)'; ctx.lineWidth = DPR; rr(ctx, 0.5 * DPR, 0.5 * DPR, sz - DPR, sz - DPR, 5.5 * DPR); ctx.stroke();
      const pad = 3 * DPR, k = Math.min((sz - 2 * pad) / im.width, (sz - 2 * pad) / im.height), w = im.width * k, h = im.height * k;
      ctx.drawImage(im, (sz - w) / 2, (sz - h) / 2, w, h);
      const data = img(c);
      if (!map.hasImage('lg|cr')) map.addImage('lg|cr', data, { pixelRatio: DPR });
      logoReady.cr = true; applyStyle();
    } catch (_) { /* tainted canvas: keep the glyph */ }
  };
  im.src = u;
}
const STATIC_ICONS = new Set(['st-h', 'st-r', 'st-s', 'dot-h', 'dot-r', 'hub', 'ap']);

// ---------------------------------------------------------------- network data
// data/index.json (countries, cities, shard table) loads at start. Lines and stations come from the
// data/net/*.json shards when a view needs them, into the sparse arrays D.lines and D.stations.
// line: [k, native, en, ref, colour, city, stations, branches, loop, km, hidden, label, country, operator, logo]
// station: [native, en, lon, lat, kind, lines, transfers, complex main, country, (complex members)]
// city: [native, en, lon, lat, population, urban lines, country, logo] · country: [iso, en, native, intercity, urban, cities, bbox]
let IX = null, CITY_DOTS = null;
const D = { lines: [], stations: [], cities: [], countries: [] };
const COMPLEX = new Map();     // complex main station -> members, from the shards loaded so far
const shardP = [], loaded = new Set();
const shardUse = [];           // the navigation that last used each shard
const LR = [], SR = [];        // [first id, end, shard] of each shard's lines / stations
const placeReady = new Set();  // stations whose complex, transfers and lines are all loaded
// lines and stations kept loaded; beyond this, shards no view has used for two navigations are dropped
const BUDGET = innerWidth <= 640 || (navigator.deviceMemory || 8) <= 4 ? 30000 : 90000;
function fetchJSON(url) { return fetch(url).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }); }
function shardOf(R, id) {
  let lo = 0, hi = R.length - 1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (id < R[m][0]) hi = m - 1; else if (id >= R[m][1]) lo = m + 1; else return R[m][2]; }
  return -1;
}
function loadShard(k) {
  shardUse[k] = nav;
  if (!shardP[k]) evict();
  return shardP[k] ||= fetchJSON('data/net/' + IX.shards[k][0]).then(d => {
    d.lines.forEach((l, i) => { D.lines[d.l0 + i] = l; });
    d.stations.forEach((s, i) => { D.stations[d.s0 + i] = s; if (s[7] >= 0) { if (!COMPLEX.has(s[7])) COMPLEX.set(s[7], []); COMPLEX.get(s[7]).push(d.s0 + i); } });
    loaded.add(k);
  }).catch(e => { shardP[k] = null; throw e; });
}
function evict() {
  const size = k => IX.shards[k][2] + IX.shards[k][4];
  let n = [...loaded].reduce((a, k) => a + size(k), 0);
  for (const k of [...loaded].sort((a, b) => shardUse[a] - shardUse[b])) {
    if (n <= BUDGET || shardUse[k] >= nav - 1) break;
    const [, l0, nl, s0, ns] = IX.shards[k];
    for (let i = s0; i < s0 + ns; i++) {
      const s = D.stations[i], m = s && s[7] >= 0 && COMPLEX.get(s[7]);
      if (m) { m.splice(m.indexOf(i) >>> 0, 1); if (!m.length) COMPLEX.delete(s[7]); }
      delete D.stations[i];
    }
    for (let i = l0; i < l0 + nl; i++) delete D.lines[i];
    loaded.delete(k); shardP[k] = null; placeReady.clear(); n -= size(k);
  }
}
// load the shards holding these lines and stations
function need(lines, stations = []) {
  const ks = new Set([...lines].map(i => shardOf(LR, i)).concat([...stations].map(i => shardOf(SR, i))));
  ks.delete(-1);
  for (const k of ks) shardUse[k] = nav;
  return Promise.all([...ks].map(loadShard));
}
// what a station's panel, stop row or label bullets show: its complex, the members' transfers and all their lines
async function needStations(ids) {
  const S = i => D.stations[i];
  await need([], ids);
  await need([], ids.filter(S).flatMap(i => [S(i)[7], ...S(i)[6]]).filter(i => i >= 0));
  const place = [...new Set(ids.filter(S).flatMap(i => complexOf(i).members.concat(i)))];
  await need([], place);
  const near = place.filter(S).flatMap(m => S(m)[6]);
  await need([], near);
  await need(place.concat(near).filter(S).flatMap(m => S(m)[5]));
  for (const i of ids) placeReady.add(i);
}
async function needLine(id) {
  await need([id]);
  const l = D.lines[id];
  if (l) await needStations(l[6].concat(...l[7]));
}
// a country's shards are named after its ISO code (cn0.json, cn1.json, hk.json…)
function countryShards(k) {
  const cc = D.countries[k] ? D.countries[k][0].toLowerCase() : '-';
  return IX.shards.flatMap((s, i) => s[0].replace(/\d*\.json$/, '') === cc ? [i] : []);
}
function needCountry(k) { const ks = countryShards(k); for (const x of ks) shardUse[x] = nav; return Promise.all(ks.map(loadShard)); }
// a city's lines: those of its country, and any filed under a neighbouring country (the search index knows them)
async function needCity(ci) {
  await needCountry(D.cities[ci][6]);
  if (loadedLines(l => l[5] === ci).length >= D.cities[ci][5]) return;
  await loadSearch();
  await need(IDX.l.filter(e => e[4] === ci).map(e => e[0]));
}
function loadedLines(f) { const ids = []; D.lines.forEach((l, i) => { if (f(l)) ids.push(i); }); return ids; }
// stations in China, where rail stations show China Railway's logo (ids are contiguous per shard)
function inChina() { return ['any', false, ...IX.shards.filter(s => /^cn\d*\.json$/.test(s[0]) && s[4]).map(s => ['all', ['>=', ['get', 'i'], s[3]], ['<', ['get', 'i'], s[3] + s[4]]])]; }

// ---------------------------------------------------------------- helpers over data
const KNAME = { h: 'High-speed railway', r: 'Railway', m: 'Metro', l: 'Light rail', s: 'Suburban rail', t: 'Tram', f: 'Funicular' };
const alt = (a, b) => a && a !== b ? a : '';    // a second name, if it says something new
const countryName = k => D.countries[k] ? D.countries[k][1] : '';
// a country's own name(s), for index.json files without them: the browser's name for it in its languages (or as given)
const NATIVE = ('AE:ar AF:fa AL:sq AM:hy AO:pt AR:es AT:de AZ:az BA:bs BD:bn BE:nl,fr BF:fr BG:bg BJ:fr BO:es BR:pt BY:be ' +
  'CD:fr CG:fr CH:de,fr,it CI:fr CL:es CN:zh CO:es CR:es CU:es CZ:cs DE:de DJ:fr DK:da DO:es DZ:ar EC:es EE:et EG:ar ER:ti ' +
  'ES:es ET:am FI:fi FR:fr GE:ka GN:fr GR:el GT:es HK:=香港 HR:hr HU:hu ID:id IE:ga IL:he IN:hi IQ:ar IR:fa IT:it JO:ar JP:ja ' +
  'KG:ky KH:km KP:=조선 KR:ko KZ:kk LA:lo LK:si LT:lt LU:lb LV:lv MA:ar MC:fr MD:ro ME:sr-Latn MG:mg MK:mk MM:my MN:mn MO:=澳門 ' +
  'MR:ar MX:es MY:ms MZ:pt NL:nl NO:nb NP:ne PA:es PE:es PH:fil PK:ur PL:pl PR:es PS:=فلسطين PT:pt PY:es QA:ar RO:ro RS:sr ' +
  'RU:ru SA:ar SD:ar SE:sv SI:sl SK:sk SN:fr SY:ar TG:fr TH:th TJ:tg TM:tk TN:ar TR:tr TW:zh-Hant TZ:sw UA:uk UY:es UZ:uz ' +
  'VE:es VN:vi XK:sq').split(' ').reduce((a, x) => (a[x.slice(0, 2)] = x.slice(3), a), {});
function nativeCountry(cc) {
  const v = NATIVE[cc]; if (!v) return '';
  if (v[0] === '=') return v.slice(1);
  try { return [...new Set(v.split(',').map(l => new Intl.DisplayNames([l], { type: 'region' }).of(cc)))].join(' / '); } catch (_) { return ''; }
}
// other names people search for
const COUNTRY_ALIAS = { US: 'USA, United States of America, America', GB: 'UK, Britain, Great Britain, England, Scotland, Wales, Northern Ireland',
  KR: 'Korea, South Korea', KP: 'North Korea, DPRK', CZ: 'Czech Republic', NL: 'Holland', MM: 'Burma', CI: 'Ivory Coast', TR: 'Turkey',
  RU: 'Russian Federation', CN: 'PRC', TW: 'Republic of China', AE: 'UAE, Emirates', CD: 'DR Congo, DRC', MK: 'Macedonia', SZ: 'Swaziland', VN: 'Vietnam' };
// flag emoji from the ISO code (regional indicator letters)
const flag = cc => /^[A-Z]{2}$/.test(cc) ? String.fromCodePoint(...[...cc].map(c => 0x1F1A5 + c.charCodeAt(0))) : '';
const ring = k => k === 'h' ? 'var(--hsr)' : k === 'r' ? 'var(--rail)' : k === 's' ? 'var(--sub)' : 'var(--ink)';
function fmtDate(d) { const m = /^(\d{4})-(\d\d)-(\d\d)$/.exec(d || ''); return m ? `${+m[3]} ${'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(' ')[m[2] - 1]} ${m[1]}` : (d || ''); }
function lineColor(id) { const l = D.lines[id]; return !l ? P.sel : l[4] || (l[0] === 'h' ? P.hsr : P.rail); }
function badgeText(l) {
  const ref = l[3] || '';
  if (ref && ref.length <= 4) return ref;
  const m = /Line\s+([A-Za-z]?\d+[A-Za-z]?)\b/.exec(l[2] || ''); if (m) return m[1];
  if (l[0] === 't') return 'T'; if (l[0] === 'f') return 'F';
  return '';
}
// an intercity line's badge is its operator's logo when there is one (small badges: a high-speed / rail pill)
function badgeHTML(id, cls = '', l = D.lines[id]) {
  if ('hr'.includes(l[0])) {
    const key = cls ? '' : l[14], u = logoUrl(key);
    return `<span class="badge ${l[0] === 'h' ? 'hsr' : 'rl'} cnone ${cls}" aria-hidden="true">${u === '' ? '' : logoImg(key)}<i>${l[0] === 'h' ? 'HSR' : 'RL'}</i></span>`;
  }
  const c = l[4] || '#888888'; const t = badgeText(l);
  return `<span class="badge ${t ? '' : 'round'} ${cls}" style="background:${c};color:${textOn(c)}" aria-hidden="true">${esc(t)}</span>`;
}
function cityName(ci) { const c = D.cities[ci]; return c ? (c[1] || c[0]) : ''; }
function lineTitle(l) { return l[2] || l[1]; }
function termini(id) {
  const st = D.lines[id][6]; if (st.length < 2) return '';
  const a = D.stations[st[0]], b = D.stations[st[st.length - 1]];
  if (D.lines[id][8]) return 'Loop line';
  return a && b ? `${stnName(a)} – ${stnName(b)}` : '';
}
// station glyph for panels: a small train on the station's colour (no hollow rings)
const stnBg = k => 'hrs'.includes(k) ? ring(k) : '#6E6E73';
const TRAIN = '<svg viewBox="0 0 10 10" fill="currentColor"><rect x="2.3" y="0.8" width="5.4" height="6.4" rx="1.4"/><rect x="3.1" y="1.8" width="3.8" height="2" fill="#0004"/><path d="M3 7.6 2 9.4M7 7.6 8 9.4" stroke="currentColor" stroke-width="1" stroke-linecap="round"/></svg>';
function stnName(s) { return s ? s[1] || s[0] : ''; }
// station complexes: a railway station and the metro stations built into it (station[7] = main station id)
function complexOf(si) {
  const s = D.stations[si], rep = s && s[7] >= 0 ? s[7] : si, r = D.stations[rep];
  return { rep, members: (r && r[9]) || COMPLEX.get(rep) || [si] };
}
function allLinesAt(si) {
  // lines serving the whole complex plus lines at linked transfer stations
  const set = new Set(), S = i => D.stations[i];
  for (const m of complexOf(si).members) { if (!S(m)) continue; for (const l of S(m)[5]) set.add(l); for (const t of S(m)[6]) if (S(t)) for (const l of S(t)[5]) set.add(l); }
  return [...set].filter(i => D.lines[i]);
}
// by kind; intercity lines longest first (their refs are internal codes: China Railway's "0007"), urban lines by
// their line number ("S2" before "S10")
const collate = new Intl.Collator(undefined, { numeric: true });
function sortLines(ids) {
  const ord = { m: 0, s: 1, l: 2, t: 3, f: 4, h: 5, r: 6 };
  return ids.sort((a, b) => {
    const A = D.lines[a], B = D.lines[b];
    if (ord[A[0]] !== ord[B[0]]) return ord[A[0]] - ord[B[0]];
    if ('hr'.includes(A[0])) { if (A[9] !== B[9]) return (B[9] || 0) - (A[9] || 0); }
    else if (!A[3] !== !B[3]) return A[3] ? -1 : 1;
    else if (A[3] && A[3] !== B[3]) return collate.compare(A[3], B[3]);
    return collate.compare(lineTitle(A), lineTitle(B));
  });
}

// ---------------------------------------------------------------- operators and logos
// A logo key (line[14], city[7]) is a key of data/logos.json: {name, url}, {name, wiki} (the logo in that English
// Wikipedia article's infobox) or {name} (no logo). The build writes an entry for every "wd:Q…" key (a Wikidata
// item); one it hasn't is looked up at runtime (the item's logo image, P154). Looked-up logos are remembered in the
// browser for 30 days.
let LOGOS = {};
const logoReady = {};
const logoP = {};
let logoCache = {};
try { const c = JSON.parse(localStorage.getItem('ra-logos') || '{}'), old = Date.now() - 30 * 864e5; for (const k in c) if (c[k][1] > old) logoCache[k] = c[k]; } catch (_) {}
const logoName = key => (LOGOS[key] && LOGOS[key].name) || '';
// a logo's url, '' if there is none, undefined while it hasn't been looked up
function logoUrl(key) {
  const lg = key && LOGOS[key];
  if (lg && (lg.url || !lg.wiki)) return lg.url || '';
  if (logoCache[key]) return logoCache[key][0];
  return key && (key.startsWith('wd:') || (lg && lg.wiki)) ? undefined : '';
}
function resolveLogo(key) {
  const u = logoUrl(key);
  if (u !== undefined) return Promise.resolve(u);
  return logoP[key] ||= (key.startsWith('wd:') ? wikidataLogo(key.slice(3)) : lookup(() => wikiLogo(LOGOS[key].wiki))).then(url => {
    logoCache[key] = [url, Date.now()];
    try { localStorage.setItem('ra-logos', JSON.stringify(logoCache)); } catch (_) {}
    return url;
  }, () => '').then(url => { fillLogos(key, url); return url; });
}
// at most four lookups at a time, to be kind to the public APIs
const lookupQ = []; let lookups = 0;
function lookup(fn) { return new Promise((res, rej) => { lookupQ.push(() => fn().then(res, rej).finally(() => { lookups--; pump(); })); pump(); }); }
function pump() { while (lookups < 4 && lookupQ.length) { lookups++; lookupQ.shift()(); } }
// Wikidata logos are asked for in batches: one query to the Wikidata Query Service for up to 40 items (the item API,
// one request per item, soon answers "429 Too Many Requests")
const wdWait = new Map(); let wdT = 0;
function wikidataLogo(q) {
  if (!/^Q\d+$/.test(q)) return Promise.resolve('');
  return new Promise((res, rej) => { wdWait.set(q, (wdWait.get(q) || []).concat([[res, rej]])); clearTimeout(wdT); wdT = setTimeout(wdFlush, 50); });
}
async function wdFlush() {
  const batch = [...wdWait].slice(0, 40); for (const [q] of batch) wdWait.delete(q);
  if (wdWait.size) wdT = setTimeout(wdFlush, 50);
  try {
    const d = await lookup(() => fetchJSON('https://query.wikidata.org/sparql?format=json&query=' + encodeURIComponent(`SELECT ?i ?l ?r WHERE { VALUES ?i { ${batch.map(([q]) => 'wd:' + q).join(' ')} }
      ?i p:P154 ?s. ?s ps:P154 ?l; wikibase:rank ?r. FILTER(?r != wikibase:DeprecatedRank) }`)));
    const got = {};
    for (const b of d.results.bindings) {
      const q = b.i.value.split('/').pop(), pref = /Preferred/.test(b.r.value);
      if (!got[q] || pref) got[q] = decodeURIComponent(b.l.value.split('/Special:FilePath/').pop());
    }
    for (const [q, cbs] of batch) for (const [res] of cbs) res(got[q] ? 'https://commons.wikimedia.org/wiki/Special:FilePath/' + encodeURIComponent(got[q].replace(/ /g, '_')) + '?width=120' : '');
  } catch (e) { for (const [, cbs] of batch) for (const [, rej] of cbs) rej(e); }
}
const WIKI_API = 'https://en.wikipedia.org/w/api.php?origin=*&format=json&formatversion=2&';
async function wikiLogo(title) {
  const p = await fetchJSON(WIKI_API + 'action=parse&prop=wikitext&section=0&redirects=1&page=' + encodeURIComponent(title));
  const wt = (p.parse && p.parse.wikitext) || '';
  const m = /\|\s*logo\s*=\s*(?:\[\[)?\s*(?:File:|Image:)?\s*([^|\]\n{}=]+?\.(?:svg|png|jpe?g|gif))/i.exec(wt);
  if (!m) return '';
  const q = await fetchJSON(WIKI_API + 'action=query&prop=imageinfo&iiprop=url&iiurlwidth=120&titles=' + encodeURIComponent('File:' + m[1].trim()));
  const pg = q.query && q.query.pages && q.query.pages[0], ii = pg && pg.imageinfo && pg.imageinfo[0];
  return ii ? (ii.thumburl || ii.url || '') : '';
}
// <img> of a logo, loaded as it scrolls into view; one still being looked up is filled in (or dropped) when the answer
// comes. Its holder shows the logo (class clogo instead of cnone) once the image has loaded, and keeps its placeholder
// (same size) if it never does.
function logoImg(key, attrs = 'alt=""', lazy = true) {
  const u = logoUrl(key);
  if (u === '') return '';
  if (u === undefined) resolveLogo(key);
  return `<img ${attrs} data-lg="${esc(key)}"${u ? ` src="${esc(u)}"` : ''}${lazy ? ' loading="lazy"' : ''} referrerpolicy="no-referrer" onload="this.parentNode.classList.replace('cnone','clogo')" onerror="this.remove()">`;
}
function fillLogos(key, url) {
  for (const im of document.querySelectorAll('img[data-lg]:not([src])')) {
    if (im.dataset.lg !== key) continue;
    if (url) im.src = url; else im.remove();
  }
}
// operators of these lines as [name, logo key], one per logo, intercity operators first
function operators(ids) {
  const m = new Map();
  for (const i of [...ids].sort((a, b) => 'hr'.includes(D.lines[b][0]) - 'hr'.includes(D.lines[a][0]))) {
    const l = D.lines[i], name = l[13] || logoName(l[14]), k = l[14] || name;
    if (k && !m.has(k)) m.set(k, [name, l[14]]);
  }
  return [...m.values()];
}
function operatorsHTML(ids) {
  const ops = operators(ids);
  return ops.slice(0, 6).map(([name, key]) => `<span class="op">${logoImg(key)}${esc(name)}</span>`).join('') + (ops.length > 6 ? `<span class="op">+${ops.length - 6} more</span>` : '');
}

// ---------------------------------------------------------------- panel rendering
const body = $('#body');
function setBody(html, expand) {
  if (expand) $('#panel').classList.remove('min');
  body.innerHTML = html; body.scrollTop = 0; $('#phead').classList.remove('scrolled');
}
body.addEventListener('scroll', () => $('#phead').classList.toggle('scrolled', body.scrollTop > 2));
// wait for a view's data (showing that it's loading if that takes a moment); false if it failed or another view took over
async function ready(p, t, what = 'Loading…') {
  const tm = setTimeout(() => { if (t === nav) setBody(`<div class="note loading">${what}</div>`); }, 250);
  try { await p; return t === nav; }
  catch (e) { if (t === nav) setBody(`<div class="note">This did not load (${esc(e.message)}). Please try again.</div>`); return false; }
  finally { clearTimeout(tm); }
}
// the city's metro operator logo, or an empty slot (keeps the names aligned) when there isn't one
function cityIcon(ci, big = false) {
  const key = D.cities[ci] && D.cities[ci][7], u = logoUrl(key);
  if (!u && u !== undefined) return big ? '' : '<span class="ico cnone" aria-hidden="true"></span>';
  const name = esc(logoName(key));
  return `<span class="ico cnone${big ? ' big' : ''}">${logoImg(key, `alt="${name}" title="${name}"`, !big)}</span>`;
}
const BACK = '<button class="back" data-act="back"><svg viewBox="0 0 9 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7.5 1.5 2 7l5.5 5.5"/></svg>Back</button>';
const CLOSE = '<button class="ibtn" data-act="close" aria-label="Close"><svg viewBox="0 0 12 12" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M2 2l8 8M10 2l-8 8"/></svg></button>';
const TRAIN_SVG = '<svg viewBox="0 0 10 10" width="10" height="10" fill="currentColor"><rect x="2.3" y="0.8" width="5.4" height="6.4" rx="1.4"/><rect x="3.1" y="1.8" width="3.8" height="2" fill="#0003"/><path d="M3 7.6 2 9.4M7 7.6 8 9.4" stroke="currentColor" stroke-width="1" stroke-linecap="round"/></svg>';
const plural = (n, w) => `${n.toLocaleString()} ${w}${n === 1 ? '' : 's'}`;

function renderHome() {
  nav++; view.stack = [];
  if (map) setURL();
  const st = IX.stats;
  const cs = D.countries.map((c, i) => [i, c[3] + c[4]]).filter(c => c[1] > 0).sort((a, b) => b[1] - a[1] || countryName(a[0]).localeCompare(countryName(b[0])));
  let h = `<div class="stats">` + [[st.urban, 'urban lines'], [st.intercity, 'intercity lines'], [st.stations, 'stations'], [st.countries, 'countries']]
    .map(([n, w]) => `<div><b>${(n || 0).toLocaleString()}</b><span>${w}</span></div>`).join('') + `</div>`;
  h += `<div class="opts"><span>Labels</span><div class="seg" role="group" aria-label="Label language">` +
    [['en', 'English'], ['both', 'Both'], ['local', 'Local']].map(([m, t]) => `<button data-lab="${m}" aria-pressed="${labelMode === m}">${t}</button>`).join('') + `</div></div>`;
  h += `<div class="sec">Key</div><div class="legend">
    <i style="background:var(--hsr);height:5px"></i><span>High-speed railway</span>
    <i style="background:var(--rail)"></i><span>Conventional railway</span>
    <i style="background:linear-gradient(90deg,#00985F 0 50%,#E2231A 50%)"></i><span>Suburban and commuter rail</span>
    <i style="background:linear-gradient(90deg,#E4002B 0 25%,#0057B8 25% 50%,#009A44 50% 75%,#F2A900 75%)"></i><span>Metro, light rail and tram, in official line colours</span></div>`;
  h += `<div class="sec">Countries · ${cs.length}</div>`;
  for (const [k] of cs) h += countryRow(k);
  h += `<div class="note">Data from OpenStreetMap contributors, extracted ${esc(fmtDate(IX.date))}. Lines appear as mapped there; recently opened or reorganised services may differ from timetables.</div>`;
  setBody(h);
}
function countryRow(k) {
  const c = D.countries[k];
  const sub = [alt(c[2], c[1]), c[3] ? c[3].toLocaleString() + ' intercity' : '', c[4] ? c[4].toLocaleString() + ' urban' : ''].filter(Boolean).join(' · ');
  return `<button class="row" data-country="${k}"><span class="ico flag" aria-hidden="true">${flag(c[0])}</span><span class="t"><b>${esc(c[1])}</b><span>${esc(sub)}</span></span><span class="n">${plural(c[3] + c[4], 'line')}</span></button>`;
}
function cityRow(i, showCountry = false) {
  const c = D.cities[i];
  const sub = [alt(c[0], c[1]), showCountry ? countryName(c[6]) : ''].filter(Boolean).join(' · ') || countryName(c[6]);
  return `<button class="row" data-city="${i}">${cityIcon(i)}<span class="t"><b>${esc(c[1] || c[0])}</b><span>${esc(sub)}</span></span><span class="n">${plural(c[5], 'line')}</span></button>`;
}
async function renderCountry(k, push = true, fit = false) {
  const t = ++nav, fitted = fit && fitCountry(k);
  if (!await ready(needCountry(k), t)) return;
  if (fit && !fitted) fitCountry(k);
  const c = D.countries[k];
  const ids = sortLines(loadedLines(l => l[12] === k && 'hr'.includes(l[0])));
  const cities = D.cities.map((x, i) => [i, x]).filter(([, x]) => x[6] === k && x[5] > 0).sort((a, b) => b[1][5] - a[1][5] || b[1][4] - a[1][4]);
  const meta = [c[3] ? plural(c[3], 'intercity line') : '', c[4] ? plural(c[4], 'urban line') : ''].filter(Boolean).join(' · ');
  let h = BACK + `<div class="dhead"><span class="ico flag big" aria-hidden="true">${flag(c[0])}</span><div><h2>${esc(c[1])}</h2>${alt(c[2], c[1]) ? `<div class="nat">${esc(c[2])}</div>` : ''}<div class="meta">${esc(meta)}</div></div>${CLOSE}</div>`;
  if (cities.length) { h += `<div class="sec">Cities with urban rail · ${cities.length}</div>`; for (const [i] of cities) h += cityRow(i); }
  for (const [kk, name] of [['h', 'High-speed rail'], ['r', 'Rail']]) {
    const g = ids.filter(i => D.lines[i][0] === kk);
    if (!g.length) continue;
    h += `<div class="sec">${name} · ${g.length.toLocaleString()}</div>` + lineRows(g);
  }
  if (push) pushView(['country', k]);
  setBody(h, true);
}
async function renderCity(ci, push = true, fly = false) {
  const t = ++nav, c = D.cities[ci];
  if (!await ready(needCity(ci), t)) return;
  const ids = sortLines(loadedLines(l => l[5] === ci));
  if (fly) flyCity(ids, c);
  const groups = { m: [], s: [], l: [], t: [], f: [] };
  for (const i of ids) (groups[D.lines[i][0]] || (groups[D.lines[i][0]] = [])).push(i);
  const meta = [plural(ids.length, 'urban rail line'), countryName(c[6])].filter(Boolean).join(' · ');
  let h = BACK + `<div class="dhead">${cityIcon(ci, true)}<div><h2>${esc(c[1] || c[0])}</h2>${alt(c[0], c[1]) ? `<div class="nat">${esc(c[0])}</div>` : ''}<div class="meta">${esc(meta)}</div></div>${CLOSE}</div>`;
  const GN = { m: 'Metro', s: 'Suburban & commuter rail', l: 'Light rail, monorail & maglev', t: 'Tram', f: 'Funicular' };
  for (const k of ['m', 's', 'l', 't', 'f']) if (groups[k].length) h += `<div class="sec">${GN[k]}</div>` + lineRows(groups[k]);
  if (push) pushView(['city', ci]);
  setBody(h, true);
}
// a line's row; search results pass a stand-in record built from data/search.json until its shard is loaded (and
// leave out the stop count, which only loaded lines know). Lines of the same name also say where they run.
function lineRow(i, showPlace = true, l = D.lines[i], ends = false, stops = true) {
  const urban = !'hr'.includes(l[0]);
  const place = !showPlace ? '' : urban ? cityName(l[5]) : countryName(l[12]);
  const sub = [ends && D.lines[i] ? termini(i) : '', alt(l[1], lineTitle(l)), !urban && l[9] ? l[9].toLocaleString() + ' km' : '', place].filter(Boolean).join(' · ');
  return `<button class="row" data-line="${i}">${badgeHTML(i, '', l)}<span class="t"><b>${esc(lineTitle(l))}</b><span>${esc(sub)}</span></span><span class="n">${stops && l[6].length ? l[6].length + ' stops' : ''}</span></button>`;
}
const sameTitle = ls => { const n = new Map(); for (const l of ls) n.set(lineTitle(l), (n.get(lineTitle(l)) || 0) + 1); return l => n.get(lineTitle(l)) > 1; };
function lineRows(ids, showPlace = false) { const dup = sameTitle(ids.map(i => D.lines[i])); return ids.map(i => lineRow(i, showPlace, D.lines[i], dup(D.lines[i]))).join(''); }
function renderLine(id, push = true) {
  const l = D.lines[id]; const col = lineColor(id);
  const urban = !'hr'.includes(l[0]);
  const meta = [KNAME[l[0]], urban ? cityName(l[5]) : countryName(l[12]), l[6].length ? `${l[6].length} stations` : '', l[9] ? `${l[9].toLocaleString()} km` : ''].filter(Boolean).join(' · ');
  let h = (view.stack.length ? BACK : '') + `<div class="dhead">${badgeHTML(id)}<div><h2>${esc(lineTitle(l))}</h2>${alt(l[1], lineTitle(l)) ? `<div class="nat">${esc(l[1])}</div>` : ''}<div class="meta">${esc(meta)}</div>${termini(id) ? `<div class="meta">${esc(termini(id))}</div>` : ''}<div class="ops">${operatorsHTML([id])}</div></div>${CLOSE}</div>`;
  if (!l[6].length) h += `<div class="note">OpenStreetMap has no station data for this line.</div>`;
  h += stopList(l[6], col, id, !!l[8]);
  for (const br of l[7]) {
    const main = new Set(l[6]); const extra = br.filter(s => !main.has(s));
    if (!extra.length) continue;
    h += `<div class="sec">Branch · ${esc(stnName(D.stations[br[0]]))} – ${esc(stnName(D.stations[br[br.length - 1]]))}</div>` + stopList(br, col, id, false);
  }
  if (push) pushView(['line', id]);
  setBody(h, true);
}
function stopList(st, col, lineId, loop) {
  let h = `<ol class="stops${loop ? ' loop' : ''}" style="--lc:${col}">`;
  for (const si of st) {
    const s = D.stations[si]; if (!s) continue;
    const other = sortLines(allLinesAt(si).filter(x => x !== lineId));
    const urb = other.filter(x => 'mlstf'.includes(D.lines[x][0]));
    const inter = other.filter(x => 'hr'.includes(D.lines[x][0]));
    let tx = urb.slice(0, 6).map(x => badgeHTML(x, 'sm')).join('');
    if (inter.length) {
      // the operator's logo if data/logos.json has one, else a train
      const lg = inter.map(x => LOGOS[D.lines[x][14]]).find(g => g && g.url);
      tx += `<span class="badge sm ${inter.some(x => D.lines[x][0] === 'h') ? 'hsr' : 'rl'}${lg ? ' lgw' : ''}" title="Rail connection">${lg ? `<img src="${esc(lg.url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.parentNode.classList.remove('lgw');this.remove()">` : ''}${TRAIN_SVG}</span>`;
    }
    h += `<li class="stop${other.length ? ' x' : ''}" data-stn="${si}" tabindex="0"><span class="rail"></span><span class="st"><b>${esc(stnName(s))}</b>${alt(s[0], stnName(s)) ? `<span>${esc(s[0])}</span>` : ''}</span><span class="tx">${tx}</span></li>`;
  }
  return h + '</ol>';
}
const STN_KIND = { h: 'High-speed rail', r: 'Railway', m: 'Metro', l: 'Light rail', s: 'Suburban rail', t: 'Tram', f: 'Funicular' };
const UKIND = { m: 'metro', l: 'light rail', t: 'tram', f: 'funicular' };
const andList = a => a.length > 1 ? a.slice(0, -1).join(', ') + ' & ' + a[a.length - 1] : a[0] || '';
function renderStation(si, push = true) {
  const { rep, members } = complexOf(si);
  const s = D.stations[rep], S = members.filter(m => D.stations[m]);
  const lines = sortLines([...new Set(S.flatMap(m => D.stations[m][5]))].filter(i => D.lines[i]));
  const kinds = [...new Set(lines.map(i => D.lines[i][0]))].sort((a, b) => 'hrsmlft'.indexOf(a) - 'hrsmlft'.indexOf(b));
  const kindText = kinds.map(k => STN_KIND[k]).filter(Boolean).join(' · ') + ('hrs'.includes(s[4]) ? ' station' : '');
  const meta = [kindText, countryName(s[8])].filter(Boolean).join(' · ');
  // the other names inside the complex (St Pancras in King's Cross, Lo Wu in Luohu)
  const names = [...new Set(S.map(m => stnName(D.stations[m])))].filter(n => n && n !== stnName(s));
  const also = names.length ? `<div class="meta">Includes ${esc(names.slice(0, 4).join(', '))}${names.length > 4 ? ` and ${names.length - 4} more` : ''}</div>` : '';
  let h = (view.stack.length ? BACK : '') + `<div class="dhead"><span class="ico stn" style="background:${stnBg(s[4])}">${TRAIN}</span><div><h2>${esc(stnName(s))}</h2>${alt(s[0], stnName(s)) ? `<div class="nat">${esc(s[0])}</div>` : ''}<div class="meta">${esc(meta)}</div>${also}<div class="ops">${operatorsHTML(lines)}</div></div>${CLOSE}</div>`;
  const rail = lines.filter(i => 'hr'.includes(D.lines[i][0])), urban = lines.filter(i => !'hr'.includes(D.lines[i][0]));
  if (rail.length) h += `<div class="sec">Rail${rail.length > 12 ? ' · ' + rail.length : ''}</div>` + byOperator(rail);
  // urban lines by city: suburban trains, then metro, light rail and trams
  const byCity = new Map(); for (const i of urban) { const c = cityName(D.lines[i][5]); if (!byCity.has(c)) byCity.set(c, []); byCity.get(c).push(i); }
  for (const [c, ids] of byCity) {
    const sub = ids.filter(i => D.lines[i][0] === 's'), other = ids.filter(i => D.lines[i][0] !== 's');
    if (sub.length) h += `<div class="sec">${esc(c ? c + ' suburban & commuter rail' : 'Suburban & commuter rail')}</div>` + lineRows(sub);
    const what = andList([...new Set(other.map(i => UKIND[D.lines[i][0]]))]);
    if (other.length) h += `<div class="sec">${esc(c ? c + ' ' + what : what.replace(/^./, x => x.toUpperCase()))}</div>` + lineRows(other);
  }
  const inComplex = new Set(members);
  const near = [...new Set(S.flatMap(m => D.stations[m][6]))].filter(t => D.stations[t] && !inComplex.has(t) && complexOf(t).rep !== rep);
  if (near.length) {
    h += `<div class="sec">Connections nearby</div>`;
    for (const t of near) {
      const o = D.stations[t]; const n = o[5].filter(i => D.lines[i]).length;
      h += `<button class="row" data-stn="${t}"><span class="ico stn" style="background:${stnBg(o[4])}">${TRAIN}</span><span class="t"><b>${esc(stnName(o))}</b><span>${esc(STN_KIND[o[4]] || '')}${n ? ' · ' + plural(n, 'line') : ''}</span></span></button>`;
    }
  }
  if (push) pushView(['stn', rep]);
  setBody(h, true);
}
// a long list of intercity lines (Russian and Indian stations list every train) folds each big operator's lines
function byOperator(ids) {
  if (ids.length <= 12) return lineRows(ids);
  const ops = new Map(); for (const i of ids) { const o = D.lines[i][13] || ''; if (!ops.has(o)) ops.set(o, []); ops.get(o).push(i); }
  let h = '', rest = [];
  for (const [o, g] of [...ops].sort((a, b) => b[1].length - a[1].length)) {
    if (g.length < 4) { rest = rest.concat(g); continue; }
    h += `<details class="fold"><summary class="row">${badgeHTML(g[0])}<span class="t"><b>${esc(o || 'Other operators')}</b></span><span class="n">${plural(g.length, 'line')}</span></summary>${lineRows(g)}</details>`;
  }
  return h + lineRows(sortLines(rest));
}
async function renderChooser(ids) {
  const t = ++nav;
  if (!await ready(need(ids), t)) return;
  ids = sortLines(ids.filter(i => D.lines[i]));
  let h = `<div class="dhead"><div><h2>${ids.length} lines here</h2><div class="meta">Choose one to see its route and stations</div></div>${CLOSE}</div>`;
  h += lineRows(ids, true);
  pushView(['choose', ids]);
  setBody(h, true);
}
// ---- the open view in the URL (#view=zoom/lat/lng&line=<id>-<name>, stn=, city=, cc=<ISO>) and in the browser
// history: each view is a history entry holding the panel's view stack, so Back and Forward walk through them
const slug = t => (t || '').normalize('NFKD').replace(/[\u0300-\u036f'’]/g, '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-|-$/g, '').slice(0, 40);
function viewParam(v) {
  if (!v) return '';
  if (v[0] === 'line' && D.lines[v[1]]) return 'line=' + v[1] + '-' + encodeURIComponent(slug(lineTitle(D.lines[v[1]])));
  if (v[0] === 'stn' && D.stations[v[1]]) return 'stn=' + v[1] + '-' + encodeURIComponent(slug(stnName(D.stations[v[1]])));
  if (v[0] === 'city') return 'city=' + v[1] + '-' + encodeURIComponent(slug(cityName(v[1])));
  if (v[0] === 'country') return 'cc=' + D.countries[v[1]][0];
  return '';
}
let replaceNav = -1;   // the navigation that restores a history entry (or opens a link) replaces the entry, not adds one
function setURL(replace = nav === replaceNav) {
  const p = viewParam(view.stack[view.stack.length - 1]);
  const h = '#' + location.hash.slice(1).split('&').filter(x => x && !/^(line|stn|city|cc)=/.test(x)).concat(p ? [p] : []).join('&');
  const d = (history.state && history.state.d) || 0;
  if (replace || h === location.hash) history.replaceState({ ra: view.stack.slice(), d }, '', h);
  else history.pushState({ ra: view.stack.slice(), d: d + 1 }, '', h);
}
function pushView(v) { view.stack.push(v); setURL(); }
// show the last view of a stack, with the rest behind it for the panel's Back button
function openView(stack) {
  view.stack = stack.slice(0, -1);
  const v = stack[stack.length - 1], q = $('#q').value.trim();
  replaceNav = nav + 1;
  if (!v) { clearSelection(); return q ? renderResults(q) : renderHome(); }
  if (v[0] === 'country') { clearSelection(); renderCountry(v[1]); }
  else if (v[0] === 'city') { clearSelection(); renderCity(v[1]); }
  else if (v[0] === 'line') selectLine(v[1], { fit: false });
  else if (v[0] === 'stn') showStation(v[1], { fly: false });
  else if (v[0] === 'choose') { clearSelection(); renderChooser(v[1]); }
}
// the panel's Back button goes back in the browser history when the previous entry is this view stack's
function goBack() {
  const st = history.state;
  if (st && st.d > 0 && st.ra && st.ra.length === view.stack.length) return history.back();
  openView(view.stack.slice(0, -1));
}
// (a link pasted over this one, or an edited URL, is an entry without a state)
addEventListener('popstate', e => {
  if (!IX || !map) return;
  if (e.state && e.state.ra) openView(e.state.ra);
  else if (/[#&](line|stn|city|cc)=/.test(location.hash)) openLink(location.hash);
  else openView([]);
});
// a shared link: the view it names, checked against the name in it (ids change between data builds)
async function openLink(hash) {
  const p = new Map(hash.slice(1).split('&').map(x => [x.slice(0, x.indexOf('=')), decodeURIComponent(x.slice(x.indexOf('=') + 1))]));
  const fit = !p.has('view'), id = v => parseInt(v, 10), name = v => v.replace(/^\d+-?/, '');
  replaceNav = nav + 1;
  if (p.has('cc')) {
    const k = D.countries.findIndex(c => c[0] === p.get('cc').toUpperCase());
    if (k >= 0) return renderCountry(k, true, fit);
  }
  if (p.has('city')) {
    const v = p.get('city'), ci = slug(cityName(id(v))) === name(v) ? id(v) : D.cities.findIndex((c, i) => c[5] && slug(cityName(i)) === name(v));
    if (ci >= 0) return renderCity(ci, true, fit);
  }
  for (const [key, R, title] of [['line', LR, i => lineTitle(D.lines[i])], ['stn', SR, i => stnName(D.stations[i])]]) {
    if (!p.has(key)) continue;
    const v = p.get(key); let i = id(v);
    try {
      if (shardOf(R, i) >= 0) await (key === 'line' ? need([i]) : need([], [i]));
      if (!(key === 'line' ? D.lines[i] : D.stations[i]) || slug(title(i)) !== name(v)) {
        await loadSearch();
        const e = IDX[key === 'line' ? 'l' : 's'].find(e => slug(e[1] || e[2]) === name(v));
        if (!e) return;
        i = e[0];
      }
    } catch (_) { return; }
    replaceNav = nav + 1;
    return key === 'line' ? selectLine(i, { fit }) : showStation(i, { fly: fit });
  }
}

// ---------------------------------------------------------------- search
// data/search.json loads on first use: {l: [[line, en, native, ref, city, kind, country]], s: [[station, en, native, kind, lines, country]]}.
// Names are compared word by word, in lower case without accents and with common abbreviations spelled out ("Hbf" is
// "Hauptbahnhof", "St" is "Saint", so that a word still being typed matches), and results are ranked by how well they
// match times how much the place matters:
// a station by its lines, a city by its population, a line by its kind and city. The index is built a slice at a time
// (the page stays responsive) and dropped after a few idle minutes (phones).
const FOLD = { hbf: 'hauptbahnhof', hb: 'hauptbahnhof', st: 'saint', ste: 'sainte', stn: 'station', mt: 'mount', ft: 'fort',
  centraal: 'central', centrale: 'central', centrala: 'central', centralna: 'central', centralny: 'central', centralnyy: 'central' };
function norm(s, fold = true) {
  const w = (s || '').toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '').replace(/ß/g, 'ss').replace(/['’`]/g, '').split(/[^\p{L}\p{N}\p{M}]+/u).filter(Boolean);
  return w.length ? [''].concat(fold ? w.map(x => FOLD[x] || x) : w).join(' ') : '';   // ' word word', one flat string
}
// IDX: per entry its type (l line, s station, c city, n country), id, names (a, b), other words (x: a line's city, a
// country's other names), code (r: line ref, country ISO), importance (w) and search.json row (e)
let IDX = null, idxP = null, idxUsed = 0;
function loadSearch() {
  idxUsed = Date.now();
  return idxP ||= fetchJSON('data/search.json').then(async d => {
    const X = { l: d.l, s: d.s, t: [], i: [], a: [], b: [], x: [], r: [], w: [], e: [] };
    const add = (t, i, a, b, x, r, w, e) => { X.t.push(t); X.i.push(i); X.a.push(a); X.b.push(b === a ? a : b); X.x.push(x); X.r.push(r); X.w.push(w); X.e.push(e); };
    const lg = Math.log10, cn = D.cities.map((c, i) => norm(cityName(i)));
    const cw = D.countries.map(c => 0.03 * lg(1 + c[3] + c[4]));
    const pop = ci => D.cities[ci] ? lg(1 + D.cities[ci][4]) : 0;
    D.countries.forEach((c, i) => { if (c[3] + c[4] > 0) add('n', i, norm(c[1]), norm(c[2]) || norm(c[1]), norm(COUNTRY_ALIAS[c[0]]), ' ' + c[0].toLowerCase(), 1.3, null); });
    D.cities.forEach((c, i) => { if (c[5] > 0) add('c', i, cn[i], norm(c[0]) || cn[i], '', '', 0.55 + 0.08 * pop(i), null); });
    const slice = async (rows, f) => { for (let k = 0; k < rows.length; k += 2500) { rows.slice(k, k + 2500).forEach(f); await new Promise(r => setTimeout(r)); } };
    await slice(d.l, e => { const a = norm(e[1]); add('l', e[0], a, norm(e[2]) || a, cn[e[4]] || '', norm(e[3]), 0.3 + ({ h: 0.2, r: 0.05, s: 0.1, m: 0.1 }[e[5]] || 0) + 0.02 * pop(e[4]) + (cw[e[6]] || 0), e); });
    await slice(d.s, e => { const b = norm(e[2]); add('s', e[0], norm(e[1]) || b, b, '', '', 0.85 * Math.min(1, Math.log2(1 + e[4]) / Math.log2(41)) + ('hr'.includes(e[3]) ? 0.15 : e[3] === 's' ? 0.08 : 0) + (cw[e[5]] || 0), e); });
    IDX = X; idxUsed = Date.now();
  }).catch(e => { idxP = null; throw e; });
}
setInterval(() => { if (IDX && !$('#q').value && Date.now() - idxUsed > 180e3) IDX = idxP = null; }, 60e3);
function search(q) {
  const n = norm(q); if (!n) return [];
  idxUsed = Date.now();
  // the query as typed too, when it has an abbreviation ("Liverpool St" is also "Liverpool Street")
  const qs = [...new Set([n, norm(q, false)])].map(n => ({ n, words: n.slice(1).split(' '), sw: n.slice(1).split(' ').map(w => ' ' + w) }));
  const num = /\d$/.test(n);
  // q at the start of a word of s, not inside a longer number ("line 1" is not "line 10"); -1 if none
  const at = (s, q) => { let i = s.indexOf(q); while (i >= 0 && num && /\d/.test(s[i + q.length] || '')) i = s.indexOf(q, i + 1); return i; };
  const X = IDX, res = [];
  const match = (j, { n, words, sw }) => {
    const a = X.a[j], b = X.b[j], x = X.x[j], r = X.r[j], ia = at(a, n), ib = b === a ? ia : at(b, n);
    if (a === n || b === n || r === n) return 1;
    if (ia === 0 || ib === 0) return 0.8;
    if (ia > 0 || ib > 0 || at(x, n) >= 0) return 0.7;
    if (sw.length > 1 && sw.every(w => at(a, w) >= 0 || at(b, w) >= 0 || at(x, w) >= 0 || r === w)) return 0.5;
    if (words.every(w => a.includes(w) || b.includes(w) || x.includes(w))) return 0.35;
    return 0;
  };
  for (let j = 0; j < X.t.length; j++) {
    let m = match(j, qs[0]);
    if (qs[1] && m < 1) m = Math.max(m, match(j, qs[1]));
    if (!m) continue;
    // a line named after its termini ("London Victoria – Epsom Downs") after the station; a line whose number is not
    // the one asked for ("U5" is not "U6")
    const r = X.r[j];
    if (X.t[j] === 'l' && m < 0.8) m *= 0.8;
    if (X.t[j] === 'l' && num && r && /\d/.test(r) && !qs[0].words.includes(r.slice(1))) m *= 0.6;
    res.push([m * (1 + 3 * X.w[j]), j]);
  }
  res.sort((p, q) => q[0] - p[0]);
  return res.slice(0, 50).map(([, j]) => ({ t: X.t[j], i: X.i[j], e: X.e[j] }));
}
function resultRow(e, dup) {
  if (e.t === 'n') return countryRow(e.i);
  if (e.t === 'c') return cityRow(e.i, true);
  const r = e.e;
  if (e.t === 'l') { const l = D.lines[e.i] || [r[5], r[2], r[1], r[3], '', r[4], [], [], 0, 0, 0, '', r[6]]; return lineRow(e.i, true, l, dup(l), false); }
  const s = D.stations[e.i], k = s ? s[4] : r[3], name = s ? stnName(s) : r[1] || r[2];
  const ls = s ? sortLines(allLinesAt(e.i)) : [];
  const city = ls.map(x => D.lines[x][5]).find(ci => ci >= 0);
  const sub = [alt(r[2], name), city != null ? cityName(city) : STN_KIND[k], countryName(r[5])].filter(Boolean).join(' · ');
  return `<button class="row" data-stn="${e.i}"><span class="ico stn" style="background:${stnBg(k)}">${TRAIN}</span><span class="t"><b>${esc(name)}</b><span>${esc(sub)}</span></span><span class="tx" style="display:flex;gap:3px">${ls.slice(0, 4).map(x => badgeHTML(x, 'sm')).join('')}</span></button>`;
}
async function renderResults(q) {
  const t = ++nav;
  if (!await ready(loadSearch(), t, 'Loading search…')) return;
  const res = search(q);
  if (!res.length) { setBody(`<div class="note">No lines, stations, cities or countries match “${esc(q)}”.</div>`); return; }
  // one row per station complex, once its shard is loaded (St Pancras International and London St Pancras)
  const draw = () => {
    const seen = new Set(), rows = res.filter(e => { if (e.t !== 's' || !D.stations[e.i]) return true; const r = complexOf(e.i).rep; return !seen.has(r) && seen.add(r); });
    const dup = sameTitle(rows.filter(e => e.t === 'l').map(e => D.lines[e.i] || [0, e.e[2], e.e[1]]));
    setBody(rows.map(e => resultRow(e, dup)).join(''));
  };
  draw();
  // colours and connections of the first results come with their shards (at most three new ones per search)
  const ks = [...new Set(res.slice(0, 20).map(e => e.t === 'l' ? shardOf(LR, e.i) : e.t === 's' ? shardOf(SR, e.i) : -1))].filter(k => k >= 0 && !loaded.has(k));
  const load = ks.filter(k => shardP[k]).concat(ks.filter(k => !shardP[k]).slice(0, 3));
  if (!load.length) return;
  try { await Promise.all(load.map(loadShard)); } catch (_) { return; }
  if (t === nav) draw();
}

// ---------------------------------------------------------------- map
let map;
function applyStyle() { map.setStyle(buildStyle(), { diff: true }); }
function lineBounds(id) {
  let a = null;
  for (const si of D.lines[id][6].concat(...D.lines[id][7])) {
    const s = D.stations[si]; if (!s) continue;
    a = a ? [Math.min(a[0], s[2]), Math.min(a[1], s[3]), Math.max(a[2], s[2]), Math.max(a[3], s[3])] : [s[2], s[3], s[2], s[3]];
  }
  return a;
}
function padding() {
  const mobile = innerWidth <= 640;
  if (mobile) return { top: 60, bottom: $('#panel').classList.contains('min') ? 150 : Math.round(innerHeight * 0.52) + 20, left: 30, right: 30 };
  return { top: 60, bottom: 60, left: Math.min(380, innerWidth * 0.45), right: 70 };
}
async function selectLine(id, { fit = true } = {}) {
  const t = ++nav;
  if (!await ready(needLine(id), t) || !D.lines[id]) return;
  showLogoPopup(null);
  sel = id; selStation = null;
  applyStyle();
  if (fit) {
    const b = lineBounds(id);
    if (b) {
      if (b[2] - b[0] < 0.002 && b[3] - b[1] < 0.002) map.flyTo({ center: [b[0], b[1]], zoom: 14 });
      else map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: padding(), maxZoom: 14.5, duration: 900 });
    }
  }
  renderLine(id);
}
function clearSelection() { showLogoPopup(null); if (sel != null || selStation != null) { sel = null; selStation = null; applyStyle(); } }
// operator logos above the selected station (its name is drawn under it), like a transit map's station callout;
// not on phones, where the panel shows them and the callout would cover the map controls
let logoPopup = null, popTok = 0;
function showLogoPopup(si) {
  const t = ++popTok;
  if (logoPopup) { logoPopup.remove(); logoPopup = null; }
  if (si == null || innerWidth <= 640) return;
  const lines = complexOf(si).members.flatMap(m => D.stations[m] ? D.stations[m][5] : []).filter(i => D.lines[i]);
  const keys = [...new Set(operators(lines).map(o => o[1]).filter(Boolean))];
  // the callout shows (CSS: not while empty) as the first logo arrives; at most three
  const el = document.createElement('div'); el.className = 'logopop';
  logoPopup = new maplibregl.Marker({ element: el, anchor: 'bottom', offset: [0, -20] }).setLngLat([D.stations[si][2], D.stations[si][3]]).addTo(map);
  for (const k of keys) resolveLogo(k).then(u => {
    if (!u || t !== popTok) return;
    const im = new Image(); im.className = 'logo pop'; im.alt = im.title = logoName(k); im.referrerPolicy = 'no-referrer';
    im.onload = () => { if (t === popTok && el.children.length < 3) el.appendChild(im); };
    im.src = u;
  });
}
async function showStation(si, { fly = true } = {}) {
  const t = ++nav;
  if (!await ready(needStations([si]), t)) return;
  si = complexOf(si).rep;
  const s = D.stations[si]; if (!s) return;
  sel = null; selStation = si; applyStyle(); showLogoPopup(si);
  if (fly) {
    const z = Math.max(map.getZoom(), 'hrs'.includes(s[4]) ? 12.5 : 14);
    // centred in the part of the map the panel leaves free (flyTo's padding option would stay on the map)
    const pd = padding();
    map.flyTo({ center: [s[2], s[3]], zoom: z, offset: [(pd.left - pd.right) / 2, (pd.top - pd.bottom) / 2], duration: 800 });
  }
  renderStation(si);
}
function flyCity(ids, c) {
  let a = null;
  for (const id of ids) { const b = lineBounds(id); if (!b) continue; a = a ? [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])] : b; }
  if (a) map.fitBounds([[a[0], a[1]], [a[2], a[3]]], { padding: padding(), maxZoom: 13, duration: 900 });
  else if (c[2] != null) map.flyTo({ center: [c[2], c[3]], zoom: 10.5 });
}
// a country's view box; one wider than 60 degrees (overseas territories, stray stations) gives way, once the country
// is loaded, to the box around the middle 96% of its stations
function countryBox(k) {
  const b = D.countries[k][6];
  if (b[2] - b[0] <= 60 && b[3] - b[1] <= 40) return b;
  if (!countryShards(k).every(x => loaded.has(x))) return null;
  const xs = [], ys = [];
  D.stations.forEach(s => { if (s[8] === k && s[5].length) { xs.push(s[2]); ys.push(s[3]); } });
  if (xs.length < 2) return b;
  const q = (a, f) => a.sort((x, y) => x - y)[Math.round(f * (a.length - 1))];
  return [q(xs, 0.02), q(ys, 0.02), q(xs, 0.98), q(ys, 0.98)];
}
function fitCountry(k) { const b = countryBox(k); if (b) map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: padding(), maxZoom: 10, duration: 900 }); return !!b; }
const WORLD = [[-165, -50], [175, 70]];
// the whole world; a phone can't fit it, so there it shows the visitor's part of it (longitude from the time zone)
function fitWorld(opts) {
  if (innerWidth > 640) map.fitBounds(WORLD, { padding: padding(), ...opts });
  else map.easeTo({ center: [Math.max(-150, Math.min(150, -new Date().getTimezoneOffset() / 4)), 30], zoom: 1, ...opts });
}
// the Home button shows the whole world, or the country whose view is open
function home() {
  const v = view.stack[view.stack.length - 1];
  if (v && v[0] === 'country') fitCountry(v[1]);
  else fitWorld({ duration: 900 });
}
// route bullets under a station's label: blank until the station's lines are loaded, then redrawn
const bulletWait = new Set(); let bulletT = 0;
function bulletRow(id) {
  const si = +id.slice(3);
  if (placeReady.has(si)) return makeBulletRow(si);
  bulletWait.add(si); clearTimeout(bulletT); bulletT = setTimeout(drawBullets, 40);
  return img(canvas(1, 1)[0]);
}
async function drawBullets() {
  const ids = [...bulletWait]; bulletWait.clear();
  try { await needStations(ids); } catch (_) { return; }
  for (const si of ids) { const id = 'bs|' + si; if (map.hasImage(id)) map.removeImage(id); map.addImage(id, makeBulletRow(si), { pixelRatio: DPR }); }
}

// panel clicks
$('#panel').addEventListener('click', e => {
  const b = e.target.closest('[data-line],[data-stn],[data-city],[data-country],[data-act],[data-lab],[data-mode]');
  if (!b) return;
  if (b.dataset.act === 'back') return goBack();
  if (b.dataset.act === 'close') { clearSelection(); view.stack = []; $('#q').value = ''; $('#qclear').classList.remove('show'); return renderHome(); }
  if (b.dataset.lab) { labelMode = b.dataset.lab; try { localStorage.setItem('ra-lab', labelMode); } catch (_) {} applyStyle(); renderHome(); return; }
  if (b.dataset.line != null) return selectLine(+b.dataset.line);
  if (b.dataset.stn != null) return showStation(+b.dataset.stn);
  if (b.dataset.city != null) { clearSelection(); return renderCity(+b.dataset.city, true, true); }
  if (b.dataset.country != null) { clearSelection(); return renderCountry(+b.dataset.country, true, true); }
});
$('#panel').addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('li[data-stn]')) { e.preventDefault(); showStation(+e.target.dataset.stn); }
});
// chips
function renderChips() {
  $('#chips').innerHTML = MODES.map(m => `<button class="chip" data-mode="${m.k}" aria-pressed="${vis[m.k]}"><span class="sw" style="background:${m.sw}"></span>${m.name}</button>`).join('');
}
$('#chips').addEventListener('click', e => {
  const b = e.target.closest('[data-mode]'); if (!b) return;
  vis[b.dataset.mode] = !vis[b.dataset.mode];
  b.setAttribute('aria-pressed', vis[b.dataset.mode]);
  applyStyle();
});
// search
let qt = null;
$('#q').addEventListener('focus', () => { $('#panel').classList.remove('min'); if (IX) loadSearch().catch(() => {}); });
$('#q').addEventListener('input', e => {
  const v = e.target.value; $('#qclear').classList.toggle('show', !!v);
  clearTimeout(qt);
  qt = setTimeout(() => { if (!v.trim()) return renderHome(); if (view.stack.length) { view.stack = []; setURL(); } renderResults(v); }, 120);
});
$('#q').addEventListener('keydown', e => { if (e.key === 'Enter') { const f = body.querySelector('.row'); if (f) f.click(); } if (e.key === 'Escape') { e.target.value = ''; $('#qclear').classList.remove('show'); renderHome(); } });
$('#qclear').addEventListener('click', () => { $('#q').value = ''; $('#qclear').classList.remove('show'); renderHome(); $('#q').focus(); });
$('#grip').addEventListener('click', () => $('#panel').classList.toggle('min'));
$('#zin').addEventListener('click', () => map.zoomIn());
$('#zout').addEventListener('click', () => map.zoomOut());
$('#compass').addEventListener('click', () => map.easeTo({ bearing: 0, pitch: 0 }));
$('#home').addEventListener('click', () => { clearSelection(); home(); });

// ---------------------------------------------------------------- boot
async function boot() {
  try { const lm = localStorage.getItem('ra-lab'); if (['en', 'both', 'local'].includes(lm)) labelMode = lm; } catch (_) {}
  const [m, ix, lg] = await Promise.all([fetchJSON('data/manifest.json'), fetchJSON('data/index.json'), fetchJSON('data/logos.json').catch(() => ({}))]);
  manifest = m; IX = ix; LOGOS = lg;
  D.cities = IX.cities; D.countries = IX.countries;
  for (const c of D.countries) c[2] ||= nativeCountry(c[0]);
  IX.shards.forEach(([, l0, nl, s0, ns], k) => { if (nl) LR.push([l0, l0 + nl, k]); if (ns) SR.push([s0, s0 + ns, k]); });
  LR.sort((a, b) => a[0] - b[0]); SR.sort((a, b) => a[0] - b[0]);
  CITY_DOTS = { type: 'FeatureCollection', features: D.cities.flatMap((c, i) => c[5] > 0 ? [{ type: 'Feature', geometry: { type: 'Point', coordinates: [c[2], c[3]] }, properties: { ci: i, n: c[5] } }] : []) };
  if (IX.date) $('#date').textContent = ' · rail data ' + fmtDate(IX.date);
  renderChips(); renderHome();
  if (innerWidth <= 640) $('#panel').classList.add('min');
  const link = location.hash;
  map = window.__map = new maplibregl.Map({
    container: 'map', style: buildStyle(), bounds: WORLD, fitBoundsOptions: { padding: padding() },
    minZoom: 0.5, maxZoom: 19, maxPitch: 60, attributionControl: false, hash: 'view',
    localIdeographFontFamily: '"PingFang SC","PingFang TC","Hiragino Sans GB","Noto Sans CJK SC","Source Han Sans SC","Microsoft YaHei",' +
      '"Hiragino Sans","Hiragino Kaku Gothic ProN","Yu Gothic","Meiryo","Noto Sans CJK JP","Apple SD Gothic Neo","Malgun Gothic","Noto Sans CJK KR",sans-serif',
    fadeDuration: 150,
  });
  if (innerWidth <= 640 && !/[#&]view=/.test(location.hash)) fitWorld({ duration: 0 });
  if (!history.state) history.replaceState({ ra: [], d: 0 }, '');
  map.once('load', () => openLink(link));
  map.on('styleimagemissing', e => {
    const id = e.id;
    if (id.startsWith('b|')) {
      const [, color, text] = id.split('|');
      if (!map.hasImage(id)) map.addImage(id, makeBadge(color || '#888888', text || ''), { pixelRatio: DPR });
    } else if (id.startsWith('bs|')) {
      if (!map.hasImage(id)) map.addImage(id, bulletRow(id), { pixelRatio: DPR });
    } else if (id.startsWith('sh|')) {
      const [, t, kind, ref] = id.split('|');
      if (!map.hasImage(id)) map.addImage(id, makeShield(t === 'D', kind, ref || ''), { pixelRatio: DPR });
    } else if (id.startsWith('ds|')) {
      const [, t, col] = id.split('|'), pal = t === 'D' ? PAL.dark : PAL.light;
      if (!map.hasImage(id)) map.addImage(id, makeDot(/^#[0-9A-Fa-f]{6}$/.test(col) && col.toUpperCase() !== '#FFFFFF' ? col : pal.stnStroke, 2.8, 1.5, pal.stnFill), { pixelRatio: DPR });
    } else if (id.startsWith('sd|')) {
      const [, t, col] = id.split('|');
      if (!map.hasImage(id)) map.addImage(id, makeDot(col, 5, 2.1, t === 'D' ? PAL.dark.stnFill : PAL.light.stnFill), { pixelRatio: DPR });
    } else if (id.startsWith('hubm|')) {
      const t = id.slice(5), pal = t === 'D' ? PAL.dark : PAL.light;
      if (!map.hasImage(id)) map.addImage(id, makeDot(pal.stnStroke, 6.2, 2.3, pal.stnFill), { pixelRatio: DPR });
    } else if (id.startsWith('pc|')) {
      if (!map.hasImage(id)) map.addImage(id, makePoiDot((POI_COLORS[id.slice(3)] || POI_COLORS.other)[0]), { pixelRatio: DPR });
    } else if (STATIC_ICONS.has(id)) addIcons(map);
  });
  map.on('load', () => { addIcons(map); loadRailLogo(); $('#loading').classList.add('done'); });
  setTimeout(() => $('#loading').classList.add('done'), 15000);   // the rail map, even if the base map does not load
  map.on('rotate', () => { $('#compass svg').style.transform = `rotate(${-map.getBearing()}deg)`; });
  const railLayers = ['u-m', 'u-s', 'u-l', 'u-t', 'u-f', 'rail-h', 'rail-r', 'sel-line'];
  const stnLayers = ['stn-u', 'stn-u1', 'stn-rail', 'sel-stn'];
  const hitBox = (p, r) => [[p.x - r, p.y - r], [p.x + r, p.y + r]];
  const layers = l => l.filter(x => map.getLayer(x));
  map.on('mousemove', e => {
    const f = map.queryRenderedFeatures(hitBox(e.point, 5), { layers: layers([...stnLayers, ...railLayers, 'city-dot']) });
    map.getCanvasContainer().classList.toggle('pointer', f.some(x => x.properties.l !== -1));   // not the overview's lines (l -1)
  });
  map.on('click', e => {
    const dot = map.queryRenderedFeatures(hitBox(e.point, 6), { layers: layers(['city-dot']) });
    if (dot.length) {
      const d = f => { const q = map.project(f.geometry.coordinates); return (q.x - e.point.x) ** 2 + (q.y - e.point.y) ** 2; };
      clearSelection(); view.stack = [];
      return renderCity(dot.sort((a, b) => d(a) - d(b))[0].properties.ci, true, true);
    }
    // a station only when the click is on its drawn dot (else the line under it): stations whose dot is drawn (stn-rail
    // keeps the others' dots transparent: see icon-opacity), within the dot's radius on screen, a few px more for a finger
    const z = map.getZoom(), minRk = z < 6 ? 8 : z < 7 ? 5 : 0;
    const lerp = (stops) => { if (z <= stops[0]) return stops[1]; for (let i = 2; i < stops.length; i += 2) if (z <= stops[i]) {
      const t = (z - stops[i - 2]) / (stops[i] - stops[i - 2]); return stops[i - 1] + t * (stops[i + 1] - stops[i - 1]); } return stops[stops.length - 1]; };
    const radius = f => {        // the dot's outer radius in CSS px, as the style draws it
      const p = f.properties, me = p.i === selStation ? 1.3 : 1;
      if (f.layer.id === 'sel-stn') return lerp([5, 2.5, 10, 4, 14, 6.5, 17, 8]) + lerp([5, 1.4, 12, 2.2, 17, 3]);
      if (f.layer.id === 'stn-rail') {
        const size = lerp([4, 0.6, 8, 0.85, 11, 0.8, 14, 1]) * me;
        if (z >= 11 || p.i === selStation) return 10 * size;                                  // the train glyph
        return size * ((p.rk >= 10 || p.x > 2) ? 6.2 : p.k === 'h' ? 4.9 : 4.3);              // hub / high-speed / other dot
      }
      const transfer = f.layer.id === 'stn-u';
      return (transfer ? 8.5 : 7.1) * lerp(transfer ? [11, 0.42, 14, 0.85, 17, 1.3] : [11, 0.38, 14, 0.78, 17, 1.15]) * me;
    };
    const touch = e.originalEvent && (e.originalEvent.pointerType === 'touch' || (e.originalEvent.sourceCapabilities || {}).firesTouchEvents);
    const slack = touch ? 6 : 1.5;
    const dist = f => { const q = map.project(f.geometry.coordinates); return Math.hypot(q.x - e.point.x, q.y - e.point.y); };
    const st = map.queryRenderedFeatures(hitBox(e.point, 16), { layers: layers(stnLayers) })
      .filter(f => f.layer.id !== 'stn-rail' || sel != null || z >= 9 || f.properties.rep === 1 && f.properties.rk >= minRk)
      .filter(f => dist(f) <= radius(f) + slack);
    if (st.length) {
      st.sort((a, b) => dist(a) - dist(b) || (b.properties.x || 0) - (a.properties.x || 0));
      return showStation(st[0].properties.i, { fly: false });
    }
    const fs = map.queryRenderedFeatures(hitBox(e.point, 6), { layers: layers(railLayers) });
    const ids = new Set();
    for (const f of fs) {
      const ls = String(f.properties.ls || '').split('|').filter(Boolean).map(Number);
      for (const x of ls) ids.add(x);
      if (!ls.length && f.properties.l >= 0) ids.add(f.properties.l);
    }
    let arr = [...ids].filter(i => i >= 0 && i < IX.nLines);
    if (sel != null && arr.includes(sel) && arr.length > 1) arr = arr.filter(i => i !== sel).concat([sel]);
    if (!arr.length) { if (sel != null || selStation != null) { clearSelection(); view.stack = []; $('#q').value = ''; $('#qclear').classList.remove('show'); renderHome(); } return; }
    view.stack = [];
    if (arr.length === 1) selectLine(arr[0], { fit: false });
    else renderChooser(arr);
  });
  // theme changes
  const reTheme = () => { const want = isDark() ? PAL.dark : PAL.light; if (want !== P) { P = want; applyStyle(); addIcons(map, true); } };
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', reTheme);
  new MutationObserver(reTheme).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
}
boot().catch(err => {
  $('#loading div').innerHTML = `<span>The map data did not load (${esc(err.message)}). Reload the page to try again.</span>`;
});
})();
