# Rail Atlas data format (v2, world)

The site covers every passenger rail line in OpenStreetMap worldwide. China (with Hong Kong and
Macau) keeps its curated pipeline (`tools/extract_osm.py`, `build_network.py`, `curate.py`); the rest
of the world comes from the planet (`tools/world/`). `tools/world/assemble.py` merges both.

## Pipeline

```
planet-latest.osm.pbf (streamed twice, never stored)
  pass 1  tools/world/planet_filter.sh   rail ways, rail route relations, station/stop/platform nodes, places -> planet1.pbf
          tools/world/planet_ids.py      node ids of those ways + relation member nodes                   -> planet_ids.txt
  pass 2  osmium getid                   those nodes, with locations                                       -> planet2.pbf
          osmium merge planet1 planet2                                                                    -> merged.pbf
tools/world/extract_world.py merged.pbf build/world/raw.pickle
tools/world/build_world.py   build/world/raw.pickle build/world         -> build/world/network.json, build/world/geometry.pickle,
                                                                            build/world/sources.json (OSM relations per line)
tools/world/wikidata.py wikilogos build/world/network.json              -> Wikipedia infobox logos of the logo keys (cache)
China:  tools/build_network.py + tools/curate.py                         -> build/china/network.json, build/china/geometry.pickle
tools/world/assemble.py --world build/world/network.json build/world/geometry.pickle
                                                                         -> data/index.json, data/net/*.json, data/search.json,
                                                                            build/full.json, build/geometry.pickle
tools/make_tiles.py build/full.json build/geometry.pickle                -> data/tiles/*.bin, data/manifest.json
```

`build/` is not committed. `build/DATE` holds the data date shown on the site (YYYY-MM-DD).

## Raw pickle (`extract_world.py`)

```
relations {rid: {'tags': {...all tags}, 'members': [(type 'n'|'w'|'r', ref, role)]}}   type=route / route_master
ways      {wid: (tags subset, coords float64[N,2] (lon, lat), refs int64[N])}           railway=rail|light_rail|subway|tram|monorail|narrow_gauge|funicular
stations  {nid: (lon, lat, tags subset)}   railway=station|halt|stop|tram_stop|platform, rail public_transport nodes, rail route member nodes
places    {nid: (lon, lat, place, name, name:en, population)}                            place=city|town
```

## Records (build/world/network.json uses these with local ids; country as ISO code string)

line (list):
```
0 kind       h high-speed · r conventional/regional intercity · m metro · s suburban/commuter · l light rail/monorail · t tram · f funicular
1 native     name in the local language (as mapped, cleaned of route descriptions)
2 en         English / Latin name (name:en, else Latin name, else transliteration); may equal native
3 ref        short line code ("1", "S3", "RE 1", "ICE 4") or ''
4 colour     "#RRGGBB" or ''
5 city       city id or -1 (urban kinds only)
6 stations   station ids in running order (main route)
7 branches   [[station ids], ...]
8 loop       1 if a loop
9 km         length in km (int) or 0
10 hidden    always 0 in shipped data (assemble drops hidden lines)
11 label     short text drawn along the track for intercity lines ('' -> en)
12 country   country id (index into countries) — ISO code string in build/world/network.json
13 operator  display name of the operator / network ("Deutsche Bahn", "MTR") or ''
14 logo      logo key: "wd:Q123" (Wikidata item whose P154 logo the page fetches) | key of data/logos.json | ''
```
station (list):
```
0 native, 1 en, 2 lon, 3 lat, 4 kind (main kind at this node, same letters), 5 line ids, 6 transfer station ids (nearby,
linked), 7 complex main station id or -1, 8 country id (ISO string in build/world/network.json)
```
city: `0 native, 1 en, 2 lon, 3 lat, 4 population, 5 number of urban lines, 6 country id, 7 logo key`

country (data/index.json only): `0 ISO code, 1 English name, 2 native name, 3 intercity lines, 4 urban lines, 5 cities with urban rail, 6 bbox [w, s, e, n]`

## Files the site loads

- `data/index.json` (at start): `{v: 2, date, stats: {urban, intercity, stations, cities, countries}, countries: [...], cities: [...], shards: [[file, lineStart, lineCount, stationStart, stationCount], ...], nLines, nStations}`.
  Line and station ids are global and contiguous per shard: id -> shard by the ranges.
- `data/net/<file>.json` (on demand): `{l0, s0, lines: [...], stations: [...]}` — ids `l0 + i`, `s0 + i`.
  One country per shard (large countries split into several). A line lives in the shard of the country
  most of its stations are in; its stations, and lines at its stations, may be in other shards.
- `data/search.json` (on first search): `{l: [[lineId, en, native, ref, cityId, kind, countryId]], s: [[stationId, en ('' if same as native), native, kind, nLines, countryId]]}`.
- `data/manifest.json` + `data/tiles/*.bin`: vector tiles (below).
- `data/logos.json`: `{key: {name, url}}` hand-checked logos (China), plus an entry for every `"wd:Q…"` key the lines and
  cities use, resolved at build time by assemble.py from the `tools/world/wikidata.py` caches (the item's Commons logo, the
  file `tools/world/ref/operators.json` gives it, its English or native-language Wikipedia infobox logo, else its parent
  company's); `{name, wiki}` when only the article is known and was not looked up (hand-checked `{name, wiki}` entries get
  the `url` of that article's infobox logo when it was). The world builder gives a line a `wd:` key only when one of these
  exists, so the site makes no Wikidata calls for them. Which item a line's operator is: its operator / network / brand
  tags (and its route master's), the operator its stations name, the curated names and national operators of
  `ref/operators.json`, then the logo most of its network's (or city's) lines show, or the trains at its stations show.

## Tiles

Layers and properties are unchanged from the China version:
- `rail`: `l` primary line id · `ls` "|id|id|" all lines on the track · `k` kind · `c` colour and `r` badge text (urban) ·
  `nm` / `nz` line name for labels (intercity, z5–11; nz = native)
- `stn`: `i` id · `n` English name · `z` native name · `k` kind · `x` lines at the complex · `c` ring colour · `rk` rank 0–13 ·
  `ls` line ids · `ks` kinds at this node · `kc` kinds at the complex · `cx` complex main id · `rep` 1 on the complex's main station

Archive: each file is a sequence of chunks, each `"TPK1" + uint32 n + n × (uint32 key, uint32 off, uint32 len) + gzipped MVTs`,
key `(z << 26) | (x << 13) | y`. `manifest.json`: `{rail: {minzoom: 3, maxzoom: 12, lowmax: 7, lz: 3, rz: 6, chunks: {key: [file, offset, length]}}}`.
Chunk key: z <= lowmax -> `"L" + (x >> (z - lz)) + "_" + (y >> (z - lz))`; otherwise `(x >> (z - rz)) + "_" + (y >> (z - rz))`.

## Proposed changes (frontend)

All optional and backward compatible; the site works without them.

- **station 9 `members`** (new, optional): on a complex's main station only, the ids of every station in the complex,
  itself included. Members can sit in other shards than their main station (129 of them on the China fixture). Without
  this list the site finds members only in shards it has loaded anyway (for the station's own lines and transfers, which
  covered every case tried on the China fixture, but nothing guarantees it); with it, the members are loaded directly.
- **Shard file names** (already what `assemble.py` writes; please keep it): `<lowercase ISO code>.json`, or
  `<code>0.json`, `<code>1.json`… when a country is split. The site finds a country's shards by name: the country and
  city views load them to list the lines, and China's station ids (from the `cn*.json` shards) get the China Railway icon.
- **`data/logos.json`** entries may be `{name, wiki}` instead of `{name, url}`: the logo is read from that English
  Wikipedia article's infobox at runtime (the 26 Chinese cities whose logos the old site looked up that way).

## Accepted changes (v2.1, coordinator)

- **station 9 `members`** (proposed above) is accepted: assemble.py writes it on every complex main station.
- **Shard file names** stay `<iso>.json` / `<iso><n>.json`.
- **`data/logos.json` `{name, wiki}`** entries are accepted.
- **Tile property `b`** on `stn` features: the second line for the "Both" label mode = the native name when it is not
  Latin script and differs from `n`, else ''. The site shows `n` + `b` in Both mode (falls back to `z` when `b` is absent).
- **Manifest `groups`** (optional; the tile generator may add it): `rail.groups = [[zmin, zmax, az], ...]` covering
  minzoom..maxzoom. For zoom z in group g, the chunk key is `"g" + g + "_" + (x >> (z - az)) + "_" + (y >> (z - az))`.
  Without `groups` the lz/rz scheme above applies. `rail.minzoom` may drop below 3 (e.g. 1) for low-zoom overview tiles.
- **Range requests**: the site fetches a chunk with `Range: bytes=off-(off+len-1)` and falls back to the whole file when
  the server answers 200 (GitHub Pages answers 206; python http.server answers 200).
- **Kind 's' chip**: suburban/commuter lines get their own "Suburban" chip (currently they follow Metro).
