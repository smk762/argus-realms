# Argus Realms

<!-- Live demo: https://atlas.dragonhound.dev -->

Real islands painted as fantasy maps by [argus-atlas](https://github.com/smk762/argus-atlas), draped over a real basemap. It's the standalone demo for argus-atlas. The hosted demo is **Atlas**, at **[atlas.dragonhound.dev](https://atlas.dragonhound.dev)**. realms.dragonhound.dev is reserved for the Realms minigame; until the game is hosted it redirects to Atlas.

The repo holds two things:

- **Atlas, the map demo** (`site/`), a fully static page served at atlas.dragonhound.dev. Iceland and Réunion are each painted in all seven argus-atlas styles (antique, tolkien, imhof, watercolor, cyberpunk, minecraft, pixel), with OpenStreetMap place names on top. An opacity slider fades the painting into the basemap, which shows that the coastlines stay put.
- **Realms, the minigame** (`src/atlas_realms/`, `web/`), a work-in-progress territory game played on the same painted maps. It needs its own server and isn't hosted yet; it will live at realms.dragonhound.dev. See [The minigame](#the-minigame).

## What the demo shows

- **Seven styles per island.** One painting per chunk serves both the base view and the zoom-in. Coarser tiles are downsampled from it, so zoom levels can't drift apart.
- **Seams that line up.** Each chunk is baked with its already-painted west and north neighbours pasted into the margin, so the joins between chunks don't show.
- **Painted land, real sea.** A sea mask made from the basemap's ocean fades the painting out within a few kilometres of the coast, so only the islands look painted.
- **Place names** from OpenStreetMap, which argus-atlas's `labels` step positions for each chunk.

## How it works

```
GPU host                                   this repo                                      any static host
argus-atlas chunk + labels ──▶ fantasy.pmtiles ──▶ atlas_realms.tiles ──▶ build_static.py ──▶ dist/static/
(bakes/islands.sh)             labels.geojson      XYZ PNG tiles, manifest  site/, tiles, _headers
```

1. **Bake.** `bakes/islands.sh` runs the argus-atlas Docker image on a GPU host with ComfyUI: DEM, OSM extract, then one `chunk` per tile per style, then `labels`. Host paths come from `ATLAS_HOST`, `ATLAS_MAPS` and `ATLAS_MODELS`. `python -m atlas_realms.bakeplan` generates plans like it.
2. **Export.** `python -m atlas_realms.tiles` turns each style's chunk archives into static `<style>/<z>/<x>/<y>.png` tiles plus a `manifest.json`, and merges the chunks' place names into one `labels.geojson`.
3. **Build.** `scripts/build_static.py` copies `site/` and the tiles into `dist/static/`. It writes `tiles/areas.json` for the area switcher, renders the `og.jpg` card from the tiles, and writes a `_headers` file with a CSP that hashes the page's inline styles. It also checks the Cloudflare Pages limits (25 MiB per file, 20,000 files).

The page is plain HTML and JavaScript (`site/index.html`, `site/app.js`) with no build step of its own. MapLibre GL comes from cdnjs, pinned with SRI. The basemap, glyphs and vector tiles come from [OpenFreeMap](https://openfreemap.org/).

## Build and deploy

Needs [uv](https://docs.astral.sh/uv/) (Python 3.12), plus Node 22 for the shared-layer check.

```bash
make install                                   # venv + editable install with [server,dev]
make static-check                              # page checks only, no tiles needed (CI runs this)
```

The painted tiles aren't in git: they come from a bake (above). With chunk archives under `var/maps/areas/<area>/<style>/<job>/`:

```bash
make static          # export var/maps -> var/tiles-areas, then build dist/static
```

`dist/static/` is a complete static site, so any static host can serve it. For Cloudflare Pages:

```bash
npx wrangler pages deploy dist/static --project-name=<your project>
```

`_headers` (CSP, caching, CORS on `/tiles/*`) is in Cloudflare Pages and Netlify format. On other hosts, set the same headers in the server config.

## The minigame

A tick-based territory game on an H3 hex grid over argus-atlas maps. Each player moves one unit across real terrain and holds hexes by being there. The map is painted ahead of time, and new land is painted only where play pushes into the fog of war.

What's here:

- `src/atlas_realms/rules/`: the pure rules layer (tunables, verifiable dice, combat, A* movement, chunk addressing, grid splits, player classes, the drand beacon maths).
- `src/atlas_realms/tick/`: the tick engine. It's a pure function of world state, orders and a seed, replayable from the database. In the shared world each tick mixes a [drand](https://drand.love/) quicknet round into its battle seeds, so neither the server nor players can predict rolls.
- **The solo arena**, playable in the browser (`web/`, served by `atlas_realms.api`): you against eight bots around Cook Strait, New Zealand. Each bot has its own personality, carries a bounty and grows stronger with each kill.
- A Postgres 16 + PostGIS + h3-pg schema and migrations for the shared world. PvP accounts aren't built yet.

The arena needs its server and the Cook Strait tiles from `bakes/cook-strait.sh`:

```bash
make install
make preview         # export var/maps -> var/tiles, serve the solo arena on http://127.0.0.1:8110
```

**In the browser, no server** (`arena/`): the same solo arena as a static site. Pyodide runs the Python rules, engine
and bots in a Web Worker, the game saves to localStorage, and a parity test checks the bundle plays the same game as
CPython tick for tick. See [arena/DESIGN.md](arena/DESIGN.md).

```bash
npm ci --prefix arena
make arena-dev       # build arena/dist (painted tiles from var/tiles if present) and serve it on http://127.0.0.1:8120
make arena-test      # the parity test
```

The painted tiles ship as plain XYZ folders of WebP files (`tiles/<style>/<z>/<x>/<y>.webp`, about 15 MiB for both
styles), not PMTiles. Cloudflare Pages doesn't serve HTTP range requests on `*.pages.dev`, and on a custom domain only
for file types the zone caches, so an archive read in ranges fails live. See `arena/DESIGN.md`.

## Develop

```bash
make test        # pytest (the database tests skip unless ATLAS_REALMS_TEST_DSN is set)
make lint        # ruff check + format --check
make check       # lint + test + the static page checks: what CI runs
make db-up       # Postgres 16 + PostGIS + h3-pg on 127.0.0.1:5433 (docker compose)
make test-db     # the full suite, including schema tests, against that database
make sim         # re-run the combat rate table
```

`site/vendor/dh/` is the vendored Dragonhound shared layer (design tokens, the demo strip, theme toggle), pinned by `.dh-shared.json`. Don't edit the copies. Check them with:

```bash
node site/vendor/dh/sync-shared.mjs --check .
```

## Data and attribution

- Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), available under the [ODbL](https://opendatacommons.org/licenses/odbl/). It covers the water, roads, forest and glacier controls the paintings are made from, the place names, the sea masks in `data/masks/` (themselves ODbL, not MIT), and the OpenFreeMap basemap.
- Terrain: [Copernicus DEM GLO-30](https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model), © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA.
- Basemap: [OpenFreeMap](https://openfreemap.org/), [OpenMapTiles](https://openmaptiles.org/) schema.
- `data/nz_mainland.geojson`: [Natural Earth](https://www.naturalearthdata.com/), public domain.

The demo shows these credits in its map attribution. See [NOTICE](NOTICE) for details, and `data/README.md` for how each data file was made.

## License

The code is [MIT](LICENSE). The argus-atlas favicon set in `site/` (`favicon.ico`, `icon.svg`, `icon-192.png`, `icon-512.png`, `apple-icon.png`) and the Dragonhound mark in `site/vendor/dh/` are brand assets. They are **not** covered by the MIT licence, and all rights are reserved. See [NOTICE](NOTICE).
