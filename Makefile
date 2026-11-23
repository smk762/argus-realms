UV ?= uv

TEST_DSN ?= postgresql://atlas:atlas@127.0.0.1:5433/postgres

.PHONY: help install lint format test test-db check static-check atlas-docs bake-plans sim db-up db-down migrate tick tiles static sea-masks preview clean \
	arena arena-check arena-test arena-dev arena-tiles-test

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

arena-check:  ## Check arena/index.html without building: pasted strip, marker, no inline scripts or styles, CSP
	$(UV) run --no-sync python arena/build.py --check

arena-test:  ## Build the arena without tiles; parity test (Pyodide vs CPython) and the no-range-requests check
	$(UV) run --no-sync python arena/build.py
	cd arena && PYTHON=$(CURDIR)/.venv/bin/python npm test

arena-tiles-test: arena  ## Painted tiles load from a server without range support, like Pages (Playwright, local)
	cd arena && node test/tiles.mjs

arena-dev: arena  ## Build and serve the arena on http://127.0.0.1:8120 (static, with the CSP from _headers)
	node arena/scripts/serve.mjs arena/dist 8120

sim:  ## Re-run the combat simulation table from the design doc
	$(UV) run --no-sync python -m atlas_realms.rules.combat

clean:  ## Remove build + cache artifacts
	rm -rf dist build .venv *.egg-info .pytest_cache .ruff_cache src/atlas_realms/_version.py
