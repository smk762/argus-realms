UV ?= uv

TEST_DSN ?= postgresql://atlas:atlas@127.0.0.1:5433/postgres

.PHONY: help install lint format test test-db check static-check atlas-docs bake-plans sim db-up db-down migrate tick tiles static sea-masks preview clean \
	tiles-pack tiles-fetch arena arena-check arena-painted arena-test arena-dev arena-tiles-test \
	k8s-regions k8s-images k8s-up k8s-deploy k8s-smoke k8s-down

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Create venv and install with test extras
	$(UV) venv --python 3.12
	$(UV) pip install -e ".[server,dev]"

lint:  ## Ruff check + format check
	$(UV)x ruff@0.15.16 check src/ tests/ arena/
	$(UV)x ruff@0.15.16 format --check src/ tests/ arena/

format:  ## Ruff autoformat + autofix
	$(UV)x ruff@0.15.16 format src/ tests/ arena/
	$(UV)x ruff@0.15.16 check --fix src/ tests/ arena/

test:  ## Run the test suite
	$(UV) run --no-sync pytest --tb=short -q

test-db:  ## Run the suite including schema tests against the compose database
	ATLAS_REALMS_TEST_DSN=$(TEST_DSN) $(UV) run --no-sync pytest --tb=short -q

ATLAS_DIR ?= ../argus-atlas
ATLAS_REF ?= origin/dev
bake-plans:  ## Regenerate every bakes/<area>.sh from bakes/plans.json at the docs' argus-atlas commit (after atlas-docs)
	$(UV) run --no-sync python scripts/bake_plans.py

atlas-docs:  ## Import the argus-atlas docs (docs/atlas/) at ATLAS_REF from the checkout in ATLAS_DIR: with the re-bake
	$(UV) run --no-sync python scripts/atlas_docs.py import --atlas $(ATLAS_DIR) --ref $(ATLAS_REF)

static-check:  ## Check site/ without tiles (strip, marker, no inline scripts, CSP, imported atlas docs); and the shared layer
	$(UV) run --no-sync python scripts/build_static.py --check
	node site/vendor/dh/sync-shared.mjs --check .

check: lint test static-check arena-test  ## What CI runs

db-up:  ## Start the local Postgres + PostGIS + h3-pg on 127.0.0.1:5433
	docker compose up -d --build --wait db

db-down:  ## Stop the local database (data volume kept)
	docker compose down

migrate:  ## Apply pending migrations to $$ATLAS_REALMS_DSN (default: the compose database)
	$(UV) run --no-sync python -m atlas_realms.db

tick:  ## Run one game tick against $$ATLAS_REALMS_DSN (LOOP=1 to keep ticking)
	$(UV) run --no-sync python -m atlas_realms.tick.runner $(if $(LOOP),--loop,)

MAPS ?= var/maps

tiles:  ## Export every area's chunks ($$MAPS/areas/<area>/<style>/<job>) to var/tiles-areas/<area>: Atlas and the arena
	$(foreach a,$(AREAS),$(if $(wildcard $(MAPS)/areas/$(a)),$(UV) run --no-sync python -m atlas_realms.tiles $(MAPS)/areas/$(a) var/tiles-areas/$(a) --overview-min $(or $(OVERVIEW_MIN_$(a)),5),echo "tiles: no bake in $(MAPS)/areas/$(a), keeping var/tiles-areas/$(a)") &&) true

# Atlas's published areas: the baked ones. bonifacio, seto, salish and cape have plans (bakes/<id>.sh) and AREA_INFO in
# scripts/build_static.py; add each here once baked. The arena's regions (arena/regions.json) paint from the same exports.
AREAS ?= iceland reunion cook-strait
# Overview tiles go one level below each area's chunk zoom (Iceland z7, Reunion z11).
OVERVIEW_MIN_iceland ?= 6
OVERVIEW_MIN_reunion ?= 10
OVERVIEW_MIN_cook-strait ?= 9
OVERVIEW_MIN_bonifacio ?= 9
OVERVIEW_MIN_seto ?= 9
OVERVIEW_MIN_salish ?= 9
OVERVIEW_MIN_cape ?= 9

tiles-pack: tiles  ## Pack the public tiles into dist/tiles-<sha>.tar for a GitHub release, and write data/tiles.lock.json
	$(UV) run --no-sync python scripts/tiles_release.py pack --areas "$(AREAS)"

tiles-fetch:  ## Download the tiles data/tiles.lock.json names, checked against its sha256, into var/tiles-areas
	$(UV) run --no-sync python scripts/tiles_release.py fetch --areas "$(AREAS)"

static: tiles  ## Static map demo (no game, areas in $$MAPS/areas/<area>/<style>/<job>) in dist/static, e.g. for Cloudflare Pages
	$(UV) run --no-sync python scripts/build_static.py --tiles var/tiles-areas --out dist/static --areas "$(AREAS)"

sea-masks: static  ## Remake data/masks (where painted sea may show) from the OpenFreeMap ocean, then rebuild dist/static
	$(foreach a,$(AREAS),$(UV) run scripts/sea_mask.py --tiles dist/static/tiles --area $(a) --out data/masks &&) true
	$(UV) run --no-sync python scripts/build_static.py --tiles var/tiles-areas --out dist/static --areas "$(AREAS)"

preview: tiles  ## Solo arena in the browser at http://127.0.0.1:8110
	$(UV) run --no-sync python -m atlas_realms.api --tiles var/tiles-areas/cook-strait --port 8110

# The browser arena (arena/): the solo game in Pyodide, a static site, one map per region in arena/regions.json.
# TILES=1 (the default when var/tiles-areas exists) paints each region from its `tiles` export; a region
# without one gets a placeholder basemap. Needs `npm ci` in arena/ once.
TILES ?= $(if $(wildcard var/tiles-areas),1,)

arena:  ## Build the browser arena into arena/dist (each region's painted tiles unless TILES=)
	$(UV) run --no-sync python arena/build.py $(if $(TILES),--tiles,)

arena-painted:  ## Fail if arena/dist lacks any region's painted styles (a deploy must not ship the placeholder)
	$(UV) run --no-sync python scripts/tiles_release.py check-arena

arena-check:  ## Check arena/index.html without building: pasted strip, marker, no inline scripts or styles, CSP
	$(UV) run --no-sync python arena/build.py --check

arena-test:  ## Build the arena without tiles; parity test (Pyodide vs CPython) and the no-range-requests check
	$(UV) run --no-sync python arena/build.py
	cd arena && PYTHON=$(CURDIR)/.venv/bin/python npm test

arena-tiles-test: arena  ## Painted tiles load from a server without range support, like Pages (Playwright, local)
	cd arena && node test/tiles.mjs

arena-dev: arena  ## Build and serve the arena on http://127.0.0.1:8120 (static, with the CSP from _headers)
	node arena/scripts/serve.mjs arena/dist 8120

# The shared world on Kubernetes (deploy/k8s/, docs/deploy.md "Kubernetes"): Postgres through CloudNativePG, the API,
# and one tick worker per line of the overlay's regions.txt. `make k8s-up` runs it on k3d (about 4 GB of RAM).
K8S_CLUSTER ?= realms
K8S_VERSION ?= $(or $(shell git describe --tags --match 'v[0-9]*' --abbrev=0 2>/dev/null | sed 's/^v//'),0.0.0)+g$(shell git rev-parse --short HEAD)

k8s-regions:  ## Regenerate deploy/k8s/overlays/dev/regions.yaml from its regions.txt (commit both)
	$(UV) run --no-sync python scripts/k8s.py regions

k8s-images:  ## Build the app and Postgres images, tag them by image id and pin them in var/k8s/
	docker build -q --build-arg VERSION=$(K8S_VERSION) -t atlas-realms:build .
	docker build -q -t atlas-realms-db:build db/cnpg
	$(UV) run --no-sync python scripts/k8s.py pin

k8s-up: k8s-images  ## Create a k3d cluster (if needed), import the images, deploy, wait for every rollout
	k3d cluster get $(K8S_CLUSTER) >/dev/null 2>&1 || k3d cluster create $(K8S_CLUSTER) --agents 0 --no-lb --k3s-arg '--disable=traefik@server:0' --wait
	k3d image import -c $(K8S_CLUSTER) $$(cat var/k8s/images.txt)
	$(UV) run --no-sync python scripts/k8s.py deploy

k8s-deploy:  ## Apply var/k8s/ to the current kubectl context (images already loaded) and wait for it
	$(UV) run --no-sync python scripts/k8s.py deploy

k8s-smoke:  ## Smoke test the deployed stack: health, ticking, an order, a worker killed mid-game, NetworkPolicy
	$(UV) run --no-sync python scripts/k8s.py smoke

k8s-down:  ## Remove the k3d cluster, and the database with it
	k3d cluster delete $(K8S_CLUSTER)

sim:  ## Re-run the combat simulation table from the design doc
	$(UV) run --no-sync python -m atlas_realms.rules.combat

clean:  ## Remove build + cache artifacts
	rm -rf dist build .venv *.egg-info .pytest_cache .ruff_cache src/atlas_realms/_version.py
