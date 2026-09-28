"""Deploy a small fix in 1-3 minutes instead of a full rebuild (8-25 min): a change to tools/world/ref/overrides.json
(recolour, rename, merge, re-stop or drop a line; schema in tools/world/assemble.py), or to the world builder's rules.

    python3 tools/update.py PREV OUT [--overrides FILE] [--world RAW] [--world-dir build/world] [--lean build/lean]

PREV  the last build: PREV/build (full.json, ids.json, tiles.cache) and PREV/data. Made by this script, or seeded by a
      full run with the options that keep what an update reuses:
        tools/world/assemble.py --world build/world/network.json build/world/geometry.pickle --lean build/lean \\
                                --out-data PREV/data --out-build PREV/build [--prev <the build before, for its ids>]
        tools/make_tiles.py PREV/build/full.json PREV/build/geometry.pickle PREV/data --store build/lean/store \\
                            --cache PREV/build/tiles.cache
OUT   a new directory: OUT/build and OUT/data (the next update's PREV) and OUT/site, the tree to deploy
--world RAW  rebuild the world network first (the builder's code or ref files changed): build_world.py RAW <world dir>
             --cache build/lean/build_world, which reuses each route's processing from its last run with that cache

Steps, timed:
  world     (--world) build_world.py: ~13 min, ~8 min when its route cache holds (a change to the filters or later stages)
  assemble  assemble.py --lean --prev PREV/build: the lines' way lists from build_world.py's lines.pickle or build/lean
            (no geometry pickle loaded), the ids of PREV kept                                                     ~15 s
  tiles     make_tiles.py on OUT/build/lines.pickle, the rail network memory-mapped from build/lean/store, --prev:
            routes of unchanged lines and unchanged chunks reused, only the .bin files with changed chunks written  ~1 min
  site      OUT/site: the site files of this tree, OUT/data hard-linked
When the ways changed (a new raw pickle or China build, a world build whose lines use other ways), assemble loads the
geometry pickles once more and make_tiles runs in full, still keeping unchanged chunks' files (~8 min). Jobs over 3 GB
run under flock /home/user/world/heavy.lock (make_tiles needs about 3.6 GB even when nothing changed).
"""
import hashlib, json, os, pickle, shutil, subprocess, sys, time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
def arg(name, default): return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
PREV, OUT = (os.path.abspath(a) for a in sys.argv[1:3])
LEAN = os.path.abspath(arg('--lean', os.path.join(ROOT, 'build', 'lean')))
LOCK = os.environ.get('HEAVY_LOCK', '/home/user/world/heavy.lock')
WORLD, CHINA = os.path.abspath(arg('--world-dir', os.path.join(ROOT, 'build', 'world'))), os.path.join(ROOT, 'build', 'china')
T0, TIMES = time.time(), []

def run(name, cmd, heavy=False):
    t = time.time()
    if heavy and os.path.exists(LOCK): cmd = ['flock', LOCK, 'nice'] + cmd
    print(f'== {name}:', ' '.join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True, env=dict(os.environ, PYTHONHASHSEED='0'))   # build_world breaks some ties by set order
    TIMES.append((name, time.time() - t)); print(f'== {name}: {time.time() - t:.0f} s', flush=True)

def src_of(name, path):   # what identifies the ways of this geometry pickle, as assemble.py's geometry_of() says
    side = os.path.join(os.path.dirname(path), 'lines.pickle')
    if name == 'world' and os.path.exists(side):
        side = pickle.load(open(side, 'rb'))
        if side['network'] == hashlib.blake2b(open(os.path.join(WORLD, 'network.json'), 'rb').read(), digest_size=16).hexdigest(): return side['src'], True
    src = f'{os.path.getsize(path)}:{os.stat(path).st_mtime_ns}'
    try: return src, pickle.load(open(os.path.join(LEAN, f'{name}_lines.pickle'), 'rb'))['src'] == src
    except (OSError, EOFError, KeyError): return src, False

for f in ('build/full.json', 'build/ids.json', 'build/tiles.cache', 'data/manifest.json'):
    if not os.path.exists(os.path.join(PREV, f)): sys.exit(f'{PREV}/{f} is missing: seed PREV with a full run (see the top of this file)')
if os.path.exists(OUT) and os.listdir(OUT): sys.exit(f'{OUT} is not empty')
os.makedirs(os.path.join(OUT, 'build')); os.makedirs(os.path.join(OUT, 'data'))
py = [sys.executable]
if '--world' in sys.argv:
    run('world', py + ['tools/world/build_world.py', os.path.abspath(arg('--world', '')), WORLD, '--cache', os.path.join(LEAN, 'build_world')], heavy=True)
wg = os.path.join(WORLD, 'geometry.pickle')
(cs, cok), (ws, wok) = src_of('china', os.path.join(CHINA, 'geometry.pickle')), src_of('world', wg)
try: store = json.load(open(os.path.join(LEAN, 'store', 'meta.json')))['src']
except OSError: store = None
lean = cok and wok and store == f'{cs} {ws}'      # else the ways changed: load the pickles, make the store again
if not lean: print('== the ways changed since', LEAN, 'was made: full assemble and tiles this time', flush=True)
ov = ['--overrides', os.path.abspath(arg('--overrides', ''))] if '--overrides' in sys.argv else []
run('assemble', py + ['tools/world/assemble.py', '--world', os.path.join(WORLD, 'network.json'), wg, '--lean', LEAN] + ([] if lean else ['--ways']) +
    ['--prev', os.path.join(PREV, 'build'), '--out-data', os.path.join(OUT, 'data'), '--out-build', os.path.join(OUT, 'build')] + ov,
    heavy=not lean)
B = os.path.join(OUT, 'build')
run('tiles', py + ['tools/make_tiles.py', os.path.join(B, 'full.json'), os.path.join(B, 'lines.pickle' if lean else 'geometry.pickle'),
                   os.path.join(OUT, 'data'), '--store', os.path.join(LEAN, 'store'), '--cache', os.path.join(B, 'tiles.cache'),
                   '--prev', os.path.join(PREV, 'build', 'tiles.cache'), os.path.join(PREV, 'data')], heavy=True)
if not lean: os.remove(os.path.join(B, 'geometry.pickle'))      # the store in build/lean has its ways now

t = time.time()
SITE = os.path.join(OUT, 'site')
os.makedirs(SITE)
for f in ('index.html', 'app.js', 'style.css', 'favicon.svg', 'CNAME', 'README.md'):
    if os.path.exists(os.path.join(ROOT, f)): shutil.copy2(os.path.join(ROOT, f), SITE)
open(os.path.join(SITE, '.nojekyll'), 'w').close()
shutil.copytree(os.path.join(ROOT, 'vendor'), os.path.join(SITE, 'vendor'))
shutil.copytree(os.path.join(OUT, 'data'), os.path.join(SITE, 'data'), copy_function=os.link)
if os.path.isdir(os.path.join(ROOT, 'data', 'glyphs')): shutil.copytree(os.path.join(ROOT, 'data', 'glyphs'), os.path.join(SITE, 'data', 'glyphs'))
TIMES.append(('site', time.time() - t))
print('== done in', f'{time.time() - T0:.0f} s:', ', '.join(f'{n} {s:.0f} s' for n, s in TIMES), '->', SITE)
