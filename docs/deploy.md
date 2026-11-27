# Building and deploying Atlas and Realms

This repo produces two static sites on Cloudflare Pages:

| Site | Source | Pages project | Domain |
|---|---|---|---|
| Atlas, the map demo | `site/` + `var/tiles-areas/` | `atlas-realms` | atlas.dragonhound.dev |
| Realms, the solo arena in the browser | `arena/` + each region's tiles (`arena/regions.json`) | `argus-realms` | realms.dragonhound.dev |

The production branch of both projects is `main`. A deploy with any other `--branch` is a preview.

The shared world (Postgres, the API, the tick workers) is a server deployment instead: §8 runs it on Kubernetes. It
isn't hosted yet.

## 1. Machine setup

- **Repo:** clone `github.com/smk762/argus-realms` and branch off `dev`. Older private clones carry history that must not be pushed here; don't add them as remotes.
- **Commit identity:** commit as `dragonhound <35845239+smk762@users.noreply.github.com>`, with `TZ=UTC` set so commit times don't carry a local offset.
- **Python:** `uv` and Python 3.12, then `make install`. This makes a venv with `[server,dev]`.
- **Node 22 or newer** (CI uses 22): `npm ci --prefix arena` for the arena build. Pyodide is pinned in `arena/package.json`.
- **Check:** `make check` runs lint, tests, the static page checks and the arena parity test. This is what CI runs.

## 2. Data that isn't in git

`var/` is git-ignored. Copy it from a machine that already has it, fetch the published tiles (`make tiles-fetch`, §6), or rebuild it from bakes:

| Path | Size (Nov 2026) | What it is |
|---|---|---|
| `var/maps/` | ~1.5 GB | argus-atlas chunk archives, `areas/<area>/<style>/<job>/` for every area: Atlas and the arena use the same bakes |
| `var/tiles-areas/` | ~510 MB | Tiles exported from `var/maps/areas` (`make tiles`), one folder per area; the arena's regions point here too |

The bakes are the expensive part, because each chunk is a GPU painting. Copy `var/maps/` rather than re-baking whenever you can; `var/tiles-areas/` can be re-exported from it on any machine. A `var/maps/<style>/` or `var/tiles/` left from before the shared layout is no longer read.

## 3. Baking new areas or styles (GPU)

Bakes run the argus-atlas Docker image on the GPU host. They need ComfyUI answering on `127.0.0.1:8188` on that host. Every area has a plan, `bakes/<area>.sh`, generated from `bakes/plans.json` by `make bake-plans` with the image at the docs' commit: the baked areas (cook-strait, iceland, reunion) and the planned arena regions (bonifacio, seto, salish, cape), all seven styles each. Never edit a plan by hand; change `plans.json` and regenerate. After a docs import (`make atlas-docs`) the commit moves, and `make test` fails until `make bake-plans` re-pins every plan.

1. **Plan:** `python -m atlas_realms.bakeplan --name <area> --center <lat,lng> --rings <n> --zoom <z> --styles <list> --land <land.geojson> --area` prints the DEM, OSM and per-chunk commands. With `--land`, it skips all-sea chunks; `--tiles z/x/y,...` bakes an exact chunk list instead (the islands: Natural Earth's outline misses an islet the paintings include). Step 0 builds the image at the commit in `docs/atlas/sync.json` from the host's argus-atlas checkout (the checkout itself: a worktree's `.git` points outside the build context, so the build can't record the commit).
2. **Run on the GPU host:** set `ATLAS_HOST` (dir holding `dem/` and `recipes/`), `ATLAS_MAPS` (output share) and `ATLAS_MODELS` (model tree). Each line skips chunks already baked, so a stopped run resumes where it left off. A re-bake at a new commit goes into a **fresh** `ATLAS_MAPS` (for example `<share>/maps-<sha7>`): `chunk` seams against any baked neighbour, so the script refuses to start beside jobs of another argus-atlas build. It also refuses chunks of another zoom (their tiles would overlap at export): for a finer re-bake of an area, move its old chunks aside first. DEMs and OSM extracts under `ATLAS_HOST` are reused.
3. **Through argus-iris, at background priority.** `scripts/run_bakes.sh [area ...]` runs the plans with `ARGUS_GPU_COORDINATOR=broker` and `ATLAS_GPU_PRIORITY=background` (argus-atlas 0.4+). Each chunk takes its own lease, waits in iris's queue behind every other tenant (up to `ARGUS_GPU_LEASE_TIMEOUT_S`, a week by default), and runs on capacity nobody else wants. A plan that stops (a lease timed out, ComfyUI was idle-stopped while the chunk waited, a bake failed) is run again after `RETRY_S`, so the run resumes where it stopped. While iris is down the runner waits rather than baking ungated, and `touch $ATLAS_MAPS/STOP` ends the run after the current chunk. Start it detached on the GPU host, with the broker's caller token from the host's `.env`:

   ```bash
   ATLAS_HOST=... ATLAS_MAPS=<fresh dir> ATLAS_MODELS=... ARGUS_GPU_BROKER_TOKEN=... \
     nohup setsid bash scripts/run_bakes.sh > run_bakes.log 2>&1 &
   ```

   Queueing needs the broker in `admission: enforce`; in `observe` iris grants every lease at once, and the runner warns. Without iris, run a plan directly (`bash bakes/<area>.sh`) when the GPU is idle.
4. **New styles:** a style is an argus-atlas recipe in `$ATLAS_HOST/recipes/`. The seven presets are antique, tolkien, imhof, watercolor, cyberpunk, minecraft and pixel. A new theme is a new recipe; keep `"z_factor": "auto"` and `"vertical_exaggeration": 1` as the other recipes do.
5. **argus-atlas version:** the bakes use the image built at the docs' commit (§5); this repo's pip pin (`argus-atlas>=0.4.1,<0.5`, for chunk addressing in the game, from the light `argus_atlas.addressing`) is separate. Chunks of different reliefs or builds can't be neighbours, so every area and style is baked with one build.

## 4. Adding an area to Atlas

[regions.md](regions.md) walks a new region through every stage, from its outline to the shared world; this
section and §8 hold the details.

1. Give it an entry in `bakes/plans.json` and in `AREA_INFO` (label and frame) in `scripts/build_static.py`, run `make bake-plans`, and bake it into `var/maps/areas/<area>/<style>/<job>/`. bonifacio, seto, salish and cape are already planned this far.
2. Once it is baked, add it to `AREAS` in the Makefile, with an `OVERVIEW_MIN_<area>` (the lowest zoom shown): that is what publishes it.
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
- `make atlas-docs` (`ATLAS_DIR=<argus-atlas checkout>`, `ATLAS_REF=origin/dev` by default) imports them at a commit; follow it with `make bake-plans`, so every plan bakes at that commit. It reads with `git show`, so the checkout's working tree doesn't matter, and it refuses a doc that fails the opsec gate (private addresses, home paths, emails, host names, `OPSEC_DENYLIST`). `docs/your-first-bake.md` is never imported.
- `make static` refuses tiles whose bakes record another argus-atlas commit than `sync.json`, and tiles from a build before 0.4, which recorded none. It checks this before touching `dist/static`, so a refused build leaves the last good one in place. Re-bake with an image built at the docs' commit, or import the docs at the bakes' commit, and commit both together.
- The `argus-atlas drift` workflow runs daily and opens an issue (or comments on the open one) when argus-atlas `dev` moves past `sync.json`.
- `make static-check` (CI) checks the import offline: the record, the opsec gate, the render, and every anchor the panel links to. The gate's private names come from `OPSEC_DENYLIST` (a repository secret in CI; locally the environment or a gitignored `.opsec-denylist` file) and are never committed; unset, only that check is skipped.

## 6. Build and deploy

`.github/workflows/pages.yml` deploys both sites from every `dev` commit that passes CI. It needs the repository secrets `CLOUDFLARE_API_TOKEN` (Pages: Edit) and `CLOUDFLARE_ACCOUNT_ID`, and a published tile set. It refuses tiles that don't match `data/tiles.lock.json`, tiles from another argus-atlas commit than `sync.json`, and an arena with any region or style unpainted.

**Publishing a tile set** (after a bake, on a machine with `var/tiles-areas/` or `var/maps/`):
```bash
make tiles-pack                      # dist/tiles-<sha>.tar, without patron-only styles, and data/tiles.lock.json
tag=$(jq -r .tag data/tiles.lock.json)
gh release create "$tag" "dist/$tag.tar" --target dev --latest=false --title "Map tiles $tag" --notes "Painted tiles for Atlas and Realms."
```
Commit `data/tiles.lock.json` with the docs import it belongs to, after the release exists. Each set gets its own tag: never replace a published asset. The release is public. `make tiles-fetch` downloads the set into an empty `var/tiles-areas/` on any machine.

The manual commands below are the fallback.

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

## 8. The shared world on Kubernetes

`deploy/k8s/` runs the shared world: Postgres 16 + PostGIS + h3-pg through [CloudNativePG](https://cloudnative-pg.io/),
the API (`atlas_realms.api --world`), a migrate job, and one tick worker per region. The tiles stay where they are
(Pages, or object storage): nothing in the cluster serves them.

```
deploy/k8s/base/           the Cluster (db.yaml), the API with its Service and HPA, the migrate Job, NetworkPolicies
deploy/k8s/overlays/dev/   regions.txt, and regions.yaml generated from it: each region's Database and tick worker
deploy/k8s/kind.yaml       CI's kind cluster (Calico, so NetworkPolicy is enforced)
db/cnpg/Dockerfile         the CloudNativePG operand image plus PostGIS and h3-pg
Dockerfile                 the app image: API, workers and migrate job
```

### Run it on a laptop

You need Docker, [k3d](https://k3d.io/) and kubectl, and about 4 GB of free RAM.

```bash
make k8s-up      # build both images, create the k3d cluster `realms`, import the images, deploy, wait for every rollout
make k8s-smoke   # health, both regions ticking, a scripted order, a worker killed mid-game, NetworkPolicy
kubectl -n realms port-forward service/realms-api 8110:80   # then http://127.0.0.1:8110/api/world
make k8s-down    # remove the cluster, and the database with it
```

`make k8s-images` tags each image with its own image id (`atlas-realms:<12 hex>`) and writes `var/k8s/`, an overlay
that pins those tags. The committed manifests only hold the placeholders `atlas-realms:unpinned` and
`atlas-realms-db:unpinned`, so applying `overlays/dev` directly fails to pull. That is deliberate: nothing runs a
floating tag. CloudNativePG itself is pinned by version and by the sha256 of its release manifest (`scripts/k8s.py`).

### Why one worker per region

Each region is a shard. It has its own database on the shared server (`realms_<region>`, a CloudNativePG `Database`
that also installs PostGIS and h3-pg), so it keeps its own ticks, players and battles. The schema stays as it was,
and so does the tick lock: `pg_try_advisory_xact_lock` is scoped to its database, so one region's slow tick never
makes another region skip. A worker is a `Deployment` with one replica and the `Recreate` strategy, so even a rollout
never runs two. If it ever did, the lock would make the second one skip.

A tick is a pure function of the database, the orders and the drand round, and it runs in one transaction. So killing
a worker loses nothing: its open tick rolls back, the replacement pod loads the same state and orders, and play
carries on. `make k8s-smoke` checks exactly this. It kills a region's worker in the middle of a game, then checks
that the tick ids carry on unbroken and the player's walk continues. The API keeps no state either, which is why it
can scale on CPU while each worker stays at one replica.

### Add a region

(The whole path, from outline to bake to arena to here, is in [regions.md](regions.md).)

1. The region must already be in the arena (`arena/regions.json`, §4): the shared world plays the arena's maps,
   world manifests and spawns.
2. Add its id as a line in `deploy/k8s/overlays/dev/regions.txt`, run `make k8s-regions`, and commit both files.
   `tests/test_k8s.py` fails if `regions.yaml` is stale or names a region the arena doesn't have.
3. Re-deploy (`make k8s-up`, or `make k8s-deploy` on a cluster that has the images). The new `Database` is created,
   the migrate job seeds it, and its worker's init container waits until then.

Removing a line removes the worker and the `Database` resource, but not the data: `databaseReclaimPolicy: retain`.

### Secrets

The only secret is the database owner's password: the Secret `realms-db-app` (type `kubernetes.io/basic-auth`, with
`username: realms` and `password`). It is created out of band and never committed. `scripts/k8s.py deploy` creates
it with a random password if it is missing, sending it over stdin so it never appears in argv or on disk. To set your
own:

```bash
kubectl create namespace realms
kubectl -n realms create secret generic realms-db-app --type=kubernetes.io/basic-auth \
  --from-literal=username=realms --from-file=password=<file holding the password>
```

The pods read it as `PGUSER` and `PGPASSWORD`. `ATLAS_REALMS_DSN` only names the host
(`host=realms-db-rw port=5432 sslmode=require`), and `atlas_realms.regions.region_dsn` adds each region's `dbname`,
so the password never sits in a connection string.

### Network

Ingress is denied by default. Postgres admits only pods labelled `realms.dragonhound.dev/db-client=true` (the API,
the workers and the migrate job), its own instances, and the CloudNativePG operator. The workers admit nothing. They
may reach the database, DNS, and HTTPS on public addresses (the drand relays); the API and the migrate job may reach
only the database and DNS. k3s enforces NetworkPolicy itself. kind's default CNI does not, so CI's cluster runs
Calico, and the smoke test fails if a pod without the label can reach Postgres.

### Health

The API's `/health` reports `starting` (503) until every region's database is migrated and seeded. That is its
readiness probe, so it gets no traffic before the migrate job has run. Once ready it reports `ok`, or `degraded`
(still 200) when a region stops answering, so a database restart doesn't restart the API. `/health?checks=0` skips
the database check. The liveness probe is a TCP check, because `/health` is 503 while the regions are being prepared.
A worker touches a heartbeat file after every tick attempt, and its liveness probe restarts it after five minutes
without one.

`/health` is written by hand for now. argus-atlas 0.5 pins `argus-cortex<0.15`, which has no
`argus_cortex.service.install_health`. Switch to `install_health` once argus-atlas 0.6 (cortex 0.16) is released;
the route carries a `nosemgrep: handrolled-health` comment that says so.

### CI

The `k8s` job in `.github/workflows/ci.yml` creates a kind cluster with Calico, builds and loads both images, deploys
with `make k8s-deploy`, and runs `make k8s-smoke`. If anything fails, it dumps pods, events and logs.

### What Compose is still fine for

`compose.yaml` stays the development database for `make db-up`, `make test-db` and `make tick`: one Postgres on a
laptop, no operator. Compose would also do for a single-host deployment with one or two regions and little
traffic: one tick loop per region (`python -m atlas_realms.tick.runner --loop --region <id>`) beside one API process.
Kubernetes earns its place when the world grows in either direction, more regions (each another worker) or more
players (more API replicas), and when a dead worker should come back by itself.

Out of scope for now: painting fog-of-war on demand as GPU jobs (a queue plus an argus-iris lease), multi-node HA,
GitOps and managed clouds. [argus-halo](https://github.com/smk762/argus-halo) is the suite's Terraform for hosted
demos. If the shared world is hosted there, it runs these manifests or their images, pinned the same way.
