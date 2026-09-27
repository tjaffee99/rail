"""Turn the merged rail extract (pass 1 + pass 2 nodes) into the pickle the world builder reads.

    python3 tools/world/extract_world.py merged.pbf raw_world.pickle

Output (same layout as tools/extract_osm.py, but geometry is numpy to fit the whole planet in memory):
  relations  {rid: {'tags': {...}, 'members': [(type, ref, role)]}}   route / route_master relations
  ways       {wid: (tags, coords float64[N,2] lon/lat, refs int64[N])}  railway ways (no locations -> skipped)
  stations   {nid: (lon, lat, tags)}   station / halt / stop / platform nodes and relation member nodes
  places     {nid: (lon, lat, place, name, name:en, population)}      city and town nodes
"""
import pickle, sys, time
import numpy as np
import osmium

WAY_KEYS = ('railway', 'usage', 'service', 'highspeed', 'maxspeed', 'railway:traffic_mode', 'name', 'name:en', 'ref',
            'tunnel', 'bridge', 'electrified', 'gauge', 'passenger', 'railway:preferred_direction', 'operator')
NODE_KEYS = {'name', 'railway', 'station', 'train', 'subway', 'light_rail', 'tram', 'monorail', 'funicular', 'public_transport',
             'usage', 'railway:traffic_mode', 'passenger', 'disused', 'abandoned', 'historic', 'railway:historic', 'operator',
             'network', 'official_name', 'ref', 'railway:ref', 'uic_ref', 'wikidata', 'int_name', 'short_name', 'level', 'layer'}
NAME_LANGS = {'en', 'zh', 'zh-Hans', 'zh-Hant', 'ja', 'ja-Latn', 'ja_rm', 'ko', 'ko-Latn', 'ru', 'ru-Latn', 'uk', 'ar', 'fa',
              'he', 'hi', 'th', 'el', 'ka', 'hy', 'latin', 'de', 'fr', 'es', 'it', 'pt'}
STN = {'station', 'halt', 'stop', 'tram_stop', 'platform'}
RAILISH = ('train', 'subway', 'light_rail', 'tram', 'monorail', 'funicular')   # public_transport stops of these modes only (no bus stops)

def keep_tags(t):
    out = {}
    for k, v in t:
        if k in NODE_KEYS or (k.startswith('name:') and k[5:] in NAME_LANGS): out[k] = v
    return out

def main(src, dst):
    t0 = time.time()
    rels, member_nodes = {}, set()
    for r in osmium.FileProcessor(src, osmium.osm.RELATION):
        tp = r.tags.get('type')
        if tp in ('route', 'route_master'):
            members = [(m.type, m.ref, m.role) for m in r.members]
            rels[r.id] = {'tags': dict(r.tags), 'members': members}
            member_nodes.update(ref for typ, ref, role in members if typ == 'n')
    print('relations', len(rels), round(time.time() - t0), 's', flush=True)
    ways, stations, places = {}, {}, {}
    fp = osmium.FileProcessor(src, osmium.osm.NODE | osmium.osm.WAY).with_locations().with_filter(osmium.filter.EmptyTagFilter())
    for o in fp:
        if o.is_node():
            t = o.tags
            if not o.location.valid(): continue
            railish = t.get('railway') in STN or any(t.get(k) == 'yes' for k in RAILISH) or t.get('station') in RAILISH
            if railish or o.id in member_nodes:
                stations[o.id] = (o.location.lon, o.location.lat, keep_tags(t))
            pl = t.get('place')
            if pl in ('city', 'town'):
                places[o.id] = (o.location.lon, o.location.lat, pl, t.get('name'), t.get('name:en'), t.get('population'))
        elif o.tags.get('railway') in ('rail', 'light_rail', 'subway', 'tram', 'monorail', 'narrow_gauge', 'funicular'):
            try: c = np.array([(n.lon, n.lat) for n in o.nodes], dtype=np.float64)
            except osmium.InvalidLocationError: continue
            if len(c) < 2: continue
            ways[o.id] = ({k: o.tags.get(k) for k in WAY_KEYS if o.tags.get(k) is not None}, c,
                          np.array([n.ref for n in o.nodes], dtype=np.int64))
    print('ways', len(ways), 'stations', len(stations), 'places', len(places), round(time.time() - t0), 's', flush=True)
    pickle.dump({'relations': rels, 'ways': ways, 'stations': stations, 'places': places}, open(dst, 'wb'), protocol=5)

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
