"""Extract the rail network from an OpenStreetMap PBF.

    python3 tools/extract_osm.py china-latest.osm.pbf /path/to/raw.pickle

Source used: https://download.openstreetmap.fr/extracts/asia/china-latest.osm.pbf (mainland China,
Hong Kong and Macau). Output is a pickle with:
  relations  route and route_master relations for trains, metro, light rail, tram, monorail
  ways       railway ways with their tags and coordinates
  stations   station / halt / stop nodes with their tags
  places     city and town nodes (for assigning urban lines to cities)
"""
import pickle, sys, time
import osmium

MODES = {'railway', 'train', 'subway', 'light_rail', 'tram', 'monorail', 'funicular'}
RAIL = {'rail', 'light_rail', 'subway', 'tram', 'monorail', 'narrow_gauge', 'funicular'}
WAY_KEYS = ('railway', 'usage', 'service', 'highspeed', 'maxspeed', 'railway:traffic_mode', 'name', 'name:en',
            'tunnel', 'bridge', 'electrified', 'gauge', 'passenger', 'railway:preferred_direction')
NODE_KEYS = ('name', 'name:en', 'name:zh', 'name:zh-Hans', 'name:zh-Hant', 'railway', 'station', 'train', 'subway', 'light_rail',
             'tram', 'monorail', 'public_transport', 'usage', 'railway:traffic_mode', 'passenger', 'disused', 'abandoned',
             'operator', 'network', 'official_name', 'ref')

def tags(t, keys): return {k: t.get(k) for k in keys if t.get(k) is not None}

def main(src, dst):
    t0 = time.time()
    rels, want_nodes = {}, set()
    for r in osmium.FileProcessor(src, osmium.osm.RELATION):
        tp = r.tags.get('type')
        if (tp == 'route' and r.tags.get('route') in MODES) or (tp == 'route_master' and r.tags.get('route_master') in MODES):
            members = [(m.type, m.ref, m.role) for m in r.members]
            rels[r.id] = {'tags': dict(r.tags), 'members': members}
            want_nodes.update(ref for typ, ref, role in members if typ == 'n')
    print('relations', len(rels), round(time.time() - t0), 's', flush=True)

    ways, stations, places = {}, {}, {}
    fp = osmium.FileProcessor(src, osmium.osm.NODE | osmium.osm.WAY).with_locations()
    for o in fp:
        if o.is_node():
            t = o.tags
            rw = t.get('railway'); pt = t.get('public_transport')
            if rw in ('station', 'halt', 'stop', 'tram_stop') or pt in ('station', 'stop_position') or o.id in want_nodes:
                if o.location.valid():
                    stations[o.id] = (o.location.lon, o.location.lat, tags(t, NODE_KEYS))
            pl = t.get('place')
            if pl in ('city', 'town') and o.location.valid():
                places[o.id] = (o.location.lon, o.location.lat, pl, t.get('name'), t.get('name:en'), t.get('population'))
        elif o.is_way():
            rw = o.tags.get('railway')
            if rw in RAIL:
                try: coords = [(n.lon, n.lat) for n in o.nodes]
                except osmium.InvalidLocationError: continue
                ways[o.id] = (tags(o.tags, WAY_KEYS), coords, [n.ref for n in o.nodes])
    print('ways', len(ways), 'stations', len(stations), 'places', len(places), round(time.time() - t0), 's', flush=True)
    pickle.dump({'relations': rels, 'ways': ways, 'stations': stations, 'places': places}, open(dst, 'wb'), protocol=5)

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
