# Building and deploying Atlas and Realms

This repo produces two static sites on Cloudflare Pages:

| Site | Source | Pages project | Domain |
|---|---|---|---|
| Atlas, the map demo | `site/` + `var/tiles-areas/` | `atlas-realms` | atlas.dragonhound.dev |
| Realms, the solo arena in the browser | `arena/` + each region's tiles (`arena/regions.json`) | `argus-realms` | realms.dragonhound.dev |

The production branch of both projects is `main`. A deploy with any other `--branch` is a preview.

## 1. Machine setup

- **Repo:** clone `github.com/smk762/argus-realms` and branch off `dev`. Older private clones carry history that must not be pushed here; don't add them as remotes.
- **Commit identity:** commit as `dragonhound <35845239+smk762@users.noreply.github.com>`, with `TZ=UTC` set so commit times don't carry a local offset.
- **Python:** `uv` and Python 3.12, then `make install`. This makes a venv with `[server,dev]`.
- **Node 22 or newer** (CI uses 22): `npm ci --prefix arena` for the arena build. Pyodide is pinned in `arena/package.json`.
- **Check:** `make check` runs lint, tests, the static page checks and the arena parity test. This is what CI runs.

## 2. Data that isn't in git

`var/` is git-ignored. Copy it from a machine that already has it, or rebuild it from bakes:

| Path | Size (Nov 2026) | What it is |
|---|---|---|
| `var/maps/` | ~1.5 GB | argus-atlas chunk archives, `areas/<area>/<style>/<job>/` for every area: Atlas and the arena use the same bakes |
| `var/tiles-areas/` | ~510 MB | Tiles exported from `var/maps/areas` (`make tiles`), one folder per area; the arena's regions point here too |

The bakes are the expensive part, because each chunk is a GPU painting. Copy `var/maps/` rather than re-baking whenever you can; `var/tiles-areas/` can be re-exported from it on any machine. A `var/maps/<style>/` or `var/tiles/` left from before the shared layout is no longer read.

## 3. Baking new areas or styles (GPU)

Bakes run the argus-atlas Docker image on the GPU host. They need ComfyUI answering on `127.0.0.1:8188` on that host. The plans for the current areas are `bakes/cook-strait.sh`, `bakes/iceland.sh` and `bakes/reunion.sh`, all seven styles each, all with the image at the docs' commit.

1. **Plan:** `python -m atlas_realms.bakeplan --name <area> --center <lat,lng> --rings <n> --zoom <z> --styles <list> --land <land.geojson> --area` prints the DEM, OSM and per-chunk commands. With `--land`, it skips all-sea chunks; `--tiles z/x/y,...` bakes an exact chunk list instead (the islands: Natural Earth's outline misses an islet the paintings include). Step 0 builds the image at the commit in `docs/atlas/sync.json` from a clone (the build needs `.git` to record the commit).
2. **Run on the GPU host:** set `ATLAS_HOST` (dir holding `dem/` and `recipes/`), `ATLAS_MAPS` (output share) and `ATLAS_MODELS` (model tree). Each line skips chunks already baked, so a stopped run resumes where it left off. A re-bake at a new commit goes into a **fresh** `ATLAS_MAPS` (for example `<share>/maps-<sha7>`): `chunk` seams against any baked neighbour, so the script refuses to start beside jobs of another argus-atlas build. DEMs and OSM extracts under `ATLAS_HOST` are reused.
3. **Schedule around other GPU work.** Run overnight or when the GPU is idle. Pause each hour from :50 to :00, when another scheduled job owns the GPU.
4. **New styles:** a style is an argus-atlas recipe in `$ATLAS_HOST/recipes/`. The seven presets are antique, tolkien, imhof, watercolor, cyberpunk, minecraft and pixel. A new theme is a new recipe; keep `"z_factor": "auto"` and `"vertical_exaggeration": 1` as the other recipes do.
5. **argus-atlas version:** the bakes use the image built at the docs' commit (§5); this repo's pip pin (`argus-atlas>=0.2.0a1,<0.3`, for chunk addressing in the game) is separate. Chunks of different reliefs or builds can't be neighbours, so every area and style is baked with one build.

## 4. Adding an area to Atlas

1. Bake it into `var/maps/areas/<area>/<style>/<job>/`.
2. Add it to `AREAS` in the Makefile, with an `OVERVIEW_MIN_<area>` (the lowest zoom shown).
3. `make static` exports the tiles, builds `dist/static/` and writes `tiles/areas.json`, which drives the area switcher.
4. `make sea-masks` remakes `data/masks/` from the OpenFreeMap ocean, so painted sea only shows near the coast.
5. Choose neutral, well-known places: islands, straits, mountain ranges. Don't put demo data anywhere that ties the project to a person.

**Adding a region to the Realms arena** (the same bakes serve both: every arena region is also an Atlas area, Cook Strait and Iceland today):
1. Make its outline with `scripts/land_outline.py` (see `data/README.md`). Check `bakeplan --land` reports about 50 or more land hexes at res 5; smaller islands don't make a game.
2. Bake it with `--area` and add it to Atlas's `AREAS`, then export it with `make tiles` into `var/tiles-areas/<area>`.
3. Commit `arena/world/<area>.json` (the export's `manifest.json`: styles, ready chunks, zooms, bounds) and add the region to `arena/regions.json`, with two spawn points on land.
4. `make arena-test` checks that it plays the same in Pyodide and CPython; `make arena` paints it.

## 5. The argus-atlas docs and the sync rule

Atlas publishes the argus-atlas docs at `/docs/`, and "How it was made" links each step there. The docs, the tiles and the argus-atlas version must agree at argus-atlas `dev` HEAD (argus-studio#169):

- `docs/atlas/` holds the imported docs (the README as the overview, chunks and mosaic, the alignment gate, the CLI reference) and `sync.json`: the argus-atlas commit and version, and a SHA256 per file. Never edit those files; re-import them.
- `make atlas-docs` (`ATLAS_DIR=<argus-atlas checkout>`, `ATLAS_REF=origin/dev` by default) imports them at a commit. It reads with `git show`, so the checkout's working tree doesn't matter, and it refuses a doc that fails the opsec gate (private addresses, home paths, emails, host names, `OPSEC_DENYLIST`). `docs/your-first-bake.md` is never imported.
- `make static` refuses tiles whose bakes record another argus-atlas commit than `sync.json`, and tiles from a build before 0.4, which recorded none. It checks this before touching `dist/static`, so a refused build leaves the last good one in place. Re-bake with an image built at the docs' commit, or import the docs at the bakes' commit, and commit both together.
- The `argus-atlas drift` workflow runs daily and opens an issue (or comments on the open one) when argus-atlas `dev` moves past `sync.json`.
- `make static-check` (CI) checks the import offline: the record, the opsec gate, the render, and every anchor the panel links to.

## 6. Build and deploy

**Credentials:** Wrangler reads `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` from the environment. Keep them in a file outside the repo, mode 600, and load it with `set -a; . <file>; set +a`. Never commit them or print them in logs.

**Atlas:**
```bash
make static                          # or: uv run --no-sync python scripts/build_static.py --tiles var/tiles-areas --out dist/static
npx wrangler@4 pages deploy dist/static --project-name=atlas-realms --branch=main
```

**Realms arena:**
```bash
make arena                           # paints every region in arena/regions.json whose tiles are here
npx wrangler@4 pages deploy arena/dist --project-name=argus-realms --branch=main
```

**Preview first** with `--branch=preview`, then check `https://preview.<project>.pages.dev`.

**After deploying:**
- Open the site with the browser console open: no errors, and no failed tile requests.
- Check the phone layout at 390 px.

## 7. Cloudflare Pages limits

- **No range requests.** Pages doesn't serve HTTP range requests on `*.pages.dev`, and a custom domain serves them only for cached file types. Tiles must stay as `z/x/y` image folders; a PMTiles archive read in ranges fails live.
- **File count and size.** A deployment holds at most **20,000 files**, each at most **25 MiB**. `build_static.py` checks both.
  - Atlas had about 4,500 files with 2 areas × 7 styles.
  - Each extra zoom level multiplies a style's tiles by about 4, so cap the max zoom per area.
  - If the site outgrows Pages, move tiles to R2 behind the same domain.
- **Old deployments keep serving.** Every Pages deployment keeps its own hashed URL. When removing content, delete the old deployments too.
