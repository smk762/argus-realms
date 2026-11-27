# Adding a region

A region is one map, played in three places: on Atlas (the map demo), in the Realms arena (the solo game in the
browser), and in the shared world (one database and one tick worker on Kubernetes). All three paint from the same
bakes, so a region is added in that order, and each stage builds on the one before.

| Stage | What it adds | Files |
|---|---|---|
| 1. Outline | The land test for the bake plan and the game's hexes | `data/<id>.geojson` |
| 2. Plan | The chunks to paint, in every style | `bakes/plans.json`, `bakes/<id>.sh`, `AREA_INFO` |
| 3. Bake | The paintings (GPU host) | `var/maps/areas/<id>/` (not in git) |
| 4. Atlas | The area in the map demo's switcher | `Makefile` `AREAS`, `OVERVIEW_MIN_<id>`, `data/masks/` |
| 5. Arena | A map to start a solo game on | `arena/world/<id>.json`, `arena/regions.json` |
| 6. Shared world | A database and a tick worker for the region | `deploy/k8s/overlays/dev/regions.txt`, `regions.yaml` |

Stages 1 and 2 are done for `bonifacio`, `seto`, `salish` and `cape`; `cook-strait` and `iceland` have gone through
all six. The example below uses `bonifacio`.

## Before you start: choosing a region

- **Neutral and well known:** islands, straits, mountain ranges. Nothing that ties the project to a person.
- **Big enough to play:** about 50 land hexes or more at H3 resolution 5 (a hex is ~17 km across). Smaller islands
  don't make a game. `bakeplan --land` reports the count (stage 2).
- **The id** is a lowercase slug (`[a-z][a-z0-9-]*`). It names the outline, the bake plan, the tile export, the arena
  map and the region's database (`realms_<id with _ for ->`), so choose it once.

## 1. Outline

The outline is the land: the bake plan skips chunks with none, and the game makes a hex land when its centre is
inside it. Make it from Natural Earth with `scripts/land_outline.py`, as `data/README.md` describes (download the
10m `land` and `minor_islands` zips into one directory first):

```bash
uv run scripts/land_outline.py --ne <dir> --bbox=7.5,38.8,10.5,43.2 --out data/bonifacio.geojson
```

Add the command and the licence row to `data/README.md`.

## 2. Plan the bake

1. Add an entry to `bakes/plans.json`: a centre and `rings` (a block of chunks around it), or an exact `tiles` list
   for islands (Natural Earth misses islets the paintings include). Give the chunk `zoom`, the `land` outline, the
   OSM `layers` and the `styles`. Copy a neighbour's entry and change what differs.
2. Add a label and a frame (west, south, east, north) to `AREA_INFO` in `scripts/build_static.py`.
3. `make bake-plans` writes `bakes/<id>.sh` at the argus-atlas commit in `docs/atlas/sync.json`. Never edit the
   plan by hand; `make test` fails if it differs from `plans.json`.

Check the land count before baking:

```bash
uv run python -m atlas_realms.bakeplan --name bonifacio --center=41.37,9.0 --rings 3 --zoom 10 \
  --styles antique --land data/bonifacio.geojson --area | head
```

## 3. Bake (GPU host)

Run the plan on the GPU host through argus-iris, at background priority: [deploy.md §3](deploy.md#3-baking-new-areas-or-styles-gpu)
has the environment and the commands. The chunks land in `$ATLAS_MAPS/areas/<id>/<style>/<job>/`. Copy them to
`var/maps/areas/<id>/` on the machine that builds the sites. A stopped run resumes where it left off.

## 4. Atlas

1. Add the id to `AREAS` in the `Makefile`, with an `OVERVIEW_MIN_<id>` one zoom below the chunk zoom (z9 for z10
   chunks).
2. `make static` exports the tiles into `var/tiles-areas/<id>/` and builds `dist/static/`; the area appears in the
   switcher.
3. `make sea-masks` makes `data/masks/<id>.png`, so painted sea only shows near the coast.
4. Publish a new tile set (`make tiles-pack` and a GitHub release, [deploy.md §6](deploy.md#6-build-and-deploy)).
   Commit `data/tiles.lock.json` once the release exists; CI's deploy refuses tiles that don't match it.

## 5. Arena

1. Commit the export's manifest as the region's world: `cp var/tiles-areas/bonifacio/manifest.json arena/world/bonifacio.json`.
   It holds the styles, the ready chunks, the zooms and the bounds; the game builds its hexes from it and the outline.
2. Add the region to `arena/regions.json`:

   ```json
   {
     "id": "bonifacio",
     "title": "Strait of Bonifacio",
     "place": "Corsica and Sardinia",
     "manifest": "arena/world/bonifacio.json",
     "outline": "data/bonifacio.geojson",
     "tiles": "var/tiles-areas/bonifacio",
     "spawns": [[41.39, 9.16], [41.21, 9.40]],
     "news": {"paper": "The Strait Gazette", "on": "on the Strait", "where": "the Strait"}
   }
   ```

   Two spawns (lat, lng) on land, far enough apart for two starts. A spawn on a coastal water hex moves to the
   nearest painted land hex; one with no land within two rings is an error. `news` names the paper and the place in
   the "while you were away" report.
3. `make arena-test` checks that every region plays the same game in Pyodide and CPython, tick for tick, and survives
   save and restore. `make arena` paints it; `make arena-painted` fails a deploy that would ship the placeholder.

## 6. Shared world (Kubernetes)

The shared world plays the arena's maps, so a region must be in `arena/regions.json` first.

1. Add its id as a line in `deploy/k8s/overlays/dev/regions.txt`.
2. `make k8s-regions` regenerates `regions.yaml`: the region's CloudNativePG `Database` (`realms_bonifacio`, with
   PostGIS and h3-pg) and its tick worker (one replica, never two). Commit both files; `tests/test_k8s.py` fails if
   `regions.yaml` is stale or names a region the arena doesn't have.
3. Deploy: `make k8s-up` on a laptop, or `make k8s-deploy` on a cluster that already has the images. The migrate job
   migrates and seeds the new database, and the worker's init container waits for it. The API lists the region once
   it is ready: `/health` stays 503 until then, so the API isn't sent traffic before the new region is ready.
4. `make k8s-smoke` checks every region ticks, the new one included.

Removing a line removes the region's worker and its `Database` resource, but not its data
(`databaseReclaimPolicy: retain`). [deploy.md §8](deploy.md#8-the-shared-world-on-kubernetes) covers why each region
is its own database and worker.

## Checklist

- [ ] `data/<id>.geojson` and its `data/README.md` row
- [ ] `bakes/plans.json` entry, `AREA_INFO`, `make bake-plans`, about 50 or more land hexes
- [ ] Baked in every style, copied to `var/maps/areas/<id>/`
- [ ] `AREAS` and `OVERVIEW_MIN_<id>` in the `Makefile`, `make static`, `make sea-masks`, a new tile release and lock
- [ ] `arena/world/<id>.json`, `arena/regions.json` entry, `make arena-test`
- [ ] `regions.txt` line, `make k8s-regions`, `make k8s-smoke`
