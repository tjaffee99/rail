#!/bin/bash
# Build a raw pickle for one openstreetmap.fr extract, through the same two passes as the planet:
#   tools/world/prepare_region.sh europe/switzerland /home/user/world/samples
set -e
R=$1; OUT=$2; N=$(basename $R); D=$(cd "$(dirname "$0")" && pwd); mkdir -p $OUT; cd $OUT
curl -sL --fail -o $N.pbf https://download.openstreetmap.fr/extracts/$R-latest.osm.pbf
cat $N.pbf | $D/planet_filter.sh $N.1.pbf
python3 $D/planet_ids.py $N.1.pbf $N.ids >/dev/null
osmium getid $N.pbf -i $N.ids --overwrite -o $N.2.pbf 2>/dev/null || true
osmium merge --overwrite $N.1.pbf $N.2.pbf -o $N.m.pbf
python3 $D/extract_world.py $N.m.pbf $N.pickle
rm -f $N.pbf $N.1.pbf $N.2.pbf $N.ids
