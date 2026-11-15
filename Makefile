UV ?= uv

TEST_DSN ?= postgresql://atlas:atlas@127.0.0.1:5433/postgres

.PHONY: help install lint format test test-db check static-check sim db-up db-down migrate tick tiles static sea-masks preview clean \
	arena arena-check arena-test arena-dev

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

static-check:  ## Check site/ without tiles: pasted strip, marker, no inline scripts, CSP; and the vendored shared layer
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

tiles:  ## Export baked chunks ($$MAPS/<style>/<job>/fantasy.pmtiles) to var/tiles
	$(UV) run --no-sync python -m atlas_realms.tiles $(MAPS) var/tiles

AREAS ?= iceland reunion
# Overview tiles go one level below each area's chunk zoom (Iceland z7, Reunion z11).
OVERVIEW_MIN_iceland ?= 6
OVERVIEW_MIN_reunion ?= 10

static:  ## Static map demo (no game, areas in $$MAPS/areas/<area>/<style>/<job>) in dist/static, e.g. for Cloudflare Pages
	$(foreach a,$(AREAS),$(UV) run --no-sync python -m atlas_realms.tiles $(MAPS)/areas/$(a) var/tiles-areas/$(a) --overview-min $(or $(OVERVIEW_MIN_$(a)),5) &&) true
	$(UV) run --no-sync python scripts/build_static.py --tiles var/tiles-areas --out dist/static

sea-masks: static  ## Remake data/masks (where painted sea may show) from the OpenFreeMap ocean, then rebuild dist/static
	$(foreach a,$(AREAS),$(UV) run scripts/sea_mask.py --tiles dist/static/tiles --area $(a) --out data/masks &&) true
	$(UV) run --no-sync python scripts/build_static.py --tiles var/tiles-areas --out dist/static

preview: tiles  ## Solo arena in the browser at http://127.0.0.1:8110
	$(UV) run --no-sync python -m atlas_realms.api --tiles var/tiles --port 8110

# The browser arena (arena/): the solo game in Pyodide, a static site. TILES defaults to var/tiles when it exists
# (make tiles); without it the build uses a placeholder basemap. Needs `npm ci` in arena/ once.
TILES ?= $(wildcard var/tiles)

arena:  ## Build the browser arena into arena/dist (painted tiles from $$TILES if set)
	$(UV) run --no-sync python arena/build.py $(if $(TILES),--tiles $(TILES),)

arena-check:  ## Check arena/index.html without building: pasted strip, marker, no inline scripts or styles, CSP
	$(UV) run --no-sync python arena/build.py --check

arena-test:  ## Build the arena without tiles and run the parity test (Pyodide bundle vs CPython)
	$(UV) run --no-sync python arena/build.py
	cd arena && PYTHON=$(CURDIR)/.venv/bin/python npm test

arena-dev: arena  ## Build and serve the arena on http://127.0.0.1:8120 (static, with the CSP from _headers)
	node arena/scripts/serve.mjs arena/dist 8120

sim:  ## Re-run the combat simulation table from the design doc
	$(UV) run --no-sync python -m atlas_realms.rules.combat

clean:  ## Remove build + cache artifacts
	rm -rf dist build .venv *.egg-info .pytest_cache .ruff_cache src/atlas_realms/_version.py
