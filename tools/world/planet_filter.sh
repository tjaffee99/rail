#!/bin/bash
# pass 1: rail ways, rail route relations, station/stop/platform nodes, city/town places (no references)
osmium tags-filter -F pbf - -R --overwrite -o "$1" \
  w/railway=rail,light_rail,subway,tram,monorail,narrow_gauge,funicular \
  r/route=train,railway,subway,light_rail,tram,monorail,funicular r/route_master=train,subway,light_rail,tram,monorail,funicular \
  n/railway=station,halt,stop,tram_stop,platform n/public_transport=station,stop_position,platform n/place=city,town \
  w/public_transport=platform w/railway=platform
