# Rail Atlas

Interactive map of every passenger rail line in the world: high-speed and conventional
railways, metro, suburban rail, light rail, trams and funiculars, from OpenStreetMap.
Intended to be served at <https://rail.theojaffee.net>.

A fully static site: no build step, no API keys.

```
index.html          page shell
style.css           UI styles (light + dark)
app.js              map style, search, country / city / line / station panels
favicon.svg
vendor/             MapLibre GL JS 5.24.0 and mapbox-gl-rtl-text 0.3.0 (self-hosted)
data/index.json     totals, countries, cities and the shard table (loaded at start)
data/net/*.json     lines and stations, one file per country or part of one (loaded when a view needs them)
data/search.json    names for search (loaded when the search box is first used)
data/logos.json     hand-checked operator logos (China)
data/manifest.json  index into the rail tile chunks
data/tiles/*.bin    rail vector tiles (z0–12), packed into chunk files
data/glyphs/        Noto Sans label glyphs (Latin/Greek/punctuation ranges)
CNAME               rail.theojaffee.net
tools/              data pipeline (tools/world/FORMAT.md describes every file above)
```

The page starts with `data/manifest.json` and `data/index.json` only: the home panel (world
totals, the countries by number of lines) comes from the index. A country, city, line or
station view loads the network shards it needs (a line brings in its stations, the lines at
those stations and their transfers), and so do map clicks and the route bullets under station
labels; on phones, shards no view has used for a while are dropped again. Rail tiles are read
from the chunk files by HTTP range request (a server that ignores ranges gets asked for whole
files instead). Below zoom 3 each city with urban rail is also drawn as a dot.

Rail station names win every label collision on the map (MapLibre places the top layers' symbols
first, so the base map's labels sit below them), one per station complex, by rank: the biggest
hubs from the world view, more as you zoom in. Road numbers only appear from zoom 12, small and grey.

The base map (streets, buildings, water, parks, place and POI labels, worldwide,
zoom 0–14 overzoomed to 19) streams from [OpenFreeMap](https://openfreemap.org),
which is free and needs no key. Glyph ranges not bundled locally (Cyrillic, Thai, Arabic,
etc.) also come from OpenFreeMap; Chinese, Japanese and Korean use the system font, and
Arabic and Hebrew labels are shaped by the RTL text plugin, fetched only once such a label
is on screen. Labels can be shown in English, in the local language, or both.

The map position and the open view are kept in the URL (`#view=zoom/lat/lng&line=<id>-<name>`,
or `stn=`, `city=`, `cc=<ISO code>`), so links are shareable; each view is a browser history
entry, so Back and Forward work. A link whose id no longer matches its name (after a data
rebuild) is looked up by the name.

Search (`data/search.json`, loaded on first use and indexed a slice at a time) matches names in
any script word by word, folds common abbreviations ("Hbf", "HB", "St", "Stn"), and ranks
results by how well they match and how much the place matters: a station by its lines, a city
by its population, then lines. Countries are also found by their own names ("Deutschland", "日本").

## Data curation

This is a passenger rail atlas, built from OpenStreetMap by four scripts in `tools/`:

1. `extract_osm.py` reads the China extract (mainland, Hong Kong, Macau) from
   <https://download.openstreetmap.fr/extracts/asia/china-latest.osm.pbf> and keeps the rail
   route relations, rail ways, station nodes and city places.
2. `build_network.py` turns those into lines, stations and cities. Urban lines come from
   route masters (one line per master, branches kept). Intercity lines come from
   `route=railway` relations; their stops are the relation's stations plus passenger stations
   on the line's own track. A station only counts if the nearest main-line track is the line's
   own, so a line passing over or beside another line's station doesn't pick it up. Stations
   with no open track nearby (lines still under construction) are left out. Relations often
   miss stretches of their own line, so each line is extended along connected track that
   carries its name.
3. `curate.py` cleans `data/network.json` in place (below).
4. `make_tiles.py` draws each listed line's track and packs the vector tiles. If a line has no
   track of its own between two stops, it is routed along the rail network (shortest path)
   and that track is drawn as part of the line (e.g. Beijing–Hong Kong between Lushan and
   Nanchang East). Any pieces of a line still apart are joined along the rail network
   (station tracks included); a piece that can't be joined and has no station is dropped.
   Track shared by a high-speed and a conventional line is coloured by its own OSM tags.

- **Removed:** freight-only railways (list in `tools/names.py`, from news and Wikipedia
  research; the low-confidence ones are marked), port, mine and coal branches, depot and
  yard tracks (动车段/动车所/走行线), bridges mapped as lines (特大桥), connectors and reversing
  spurs (联络线/疏解线/直通线/立折线), unnamed industrial track, lines outside China, duplicate relations (e.g. Batong Line,
  now part of Line 1), and stubs with fewer than two mapped stops. Their track is not drawn.
- **Stop order:** lists that OSM has out of order are re-sequenced, and intercity lines are
  listed in the order their name reads (Beijing–Shanghai starts at Beijing).
- **Station complexes:** a railway station and the metro stations built into it (OSM
  transfers, or metro within 400 m) are one place, with one label, one panel and all lines.
- **Station names:** one spelling per station (e.g. "Fangshandong" and "Fangshan East" both become
  "Fangshan East").
- **English names:** hand-checked names for high-speed, trunk and named urban lines
  (`tools/names.py`); "N号线" becomes "Line N"; auto-romanised pinyin is re-spelled word by
  word.

```
pip install osmium numpy shapely jieba pypinyin mapbox-vector-tile protobuf
curl -LO https://download.openstreetmap.fr/extracts/asia/china-latest.osm.pbf   # ~1.8 GB
python3 tools/extract_osm.py china-latest.osm.pbf raw.pickle                     # ~15 min
python3 tools/build_network.py raw.pickle geometry.pickle
python3 tools/curate.py -v      # -v lists every removed line and why
python3 tools/make_tiles.py geometry.pickle
```

## Operator logos

Each line names its operator and a logo key (cities have one too). A key like `wd:Q…` is a
Wikidata item: the page asks the Wikidata Query Service for the logo images (P154) of up to
40 items at once and shows 120 px thumbnails from Wikimedia Commons. Intercity lines show
their operator's logo in lists (a high-speed / rail badge when there is none), and the selected
station gets a callout with up to three logos (not on phones). Other keys are entries of `data/logos.json`,
hand-checked logos of China's operators: a Wikimedia image URL, or the English Wikipedia
article whose infobox logo is used. Looked-up logos are cached in the browser for 30 days,
and any logo that fails to load is simply left out. China Railway's logo is the rail station
icon in China; elsewhere (and as the fallback) the icon is a drawn train.

## Run locally

```
python3 -m http.server 8000   # then open http://localhost:8000
```

## Deploying

Served by GitHub Pages from the `main` branch (root). The `CNAME` file sets the
custom domain; DNS has a `CNAME chinarail -> tjaffee99.github.io` record.
Push to `main` and the site updates within a minute or two.
