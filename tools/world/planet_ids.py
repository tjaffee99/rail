"""Write the node ids pass 2 must fetch: every node of a pass-1 way, plus relation member nodes."""
import sys, osmium, numpy as np
ids = []
buf = []
for o in osmium.FileProcessor(sys.argv[1], osmium.osm.WAY | osmium.osm.RELATION):
    if o.is_way(): buf.extend(n.ref for n in o.nodes)
    else: buf.extend(m.ref for m in o.members if m.type == 'n')
    if len(buf) > 5_000_000: ids.append(np.array(buf, dtype=np.int64)); buf = []
ids.append(np.array(buf, dtype=np.int64))
a = np.unique(np.concatenate(ids))
with open(sys.argv[2], 'w') as f:
    for chunk in np.array_split(a, max(1, len(a) // 1_000_000)):
        f.write(''.join(f'n{x}\n' for x in chunk.tolist()))
print('node ids', len(a))
