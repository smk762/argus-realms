# CLAUDE.md — atlas-realms

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md).

## What this is

The browser game that demonstrates argus-atlas, plus the static map demo, Atlas, at https://atlas.dragonhound.dev (`site/`). realms.dragonhound.dev is reserved
for the game and redirects to Atlas until the game is hosted.
The design doc (not public) is the source of truth for rules and numbers; `Tunables` and the tests mirror it.

The game consumes argus-atlas as a pip package (and, later, through a GPU bake worker). General map
features go into argus-atlas; anything game-specific stays here. argus-atlas's own scope is "no game".

## Layout

`src/atlas_realms/rules/` is pure: no database, clock or I/O. The tick engine will call it.

- `tunables.py` — `Tunables`, one frozen model with every number the doc marks tunable. Rule code takes a
  `Tunables` argument (default `DEFAULT`) instead of hard-coding values.
- `dice.py` — HMAC battle seeds and a SHA-256 counter stream with rejection sampling. The stream is part of the
  public contract (players re-run battles from logs); changing it invalidates every published log.
- `beacon.py` — drand quicknet constants and round maths. A battle seed is HMAC(daily secret, tick:battle:beacon), so
  neither the server (knows the secret) nor players (know the round) can predict rolls. `beacon=None` keeps the old
  `tick:battle` message, so earlier logs stay valid; the solo arena has no beacon.
- `combat.py` — `resolve_battle()` returns a `BattleResult` that is the battle-log record. `simulate()` and
  `python -m atlas_realms.rules.combat` reproduce the doc's rate table.
- `movement.py` — `hex_minutes()` (walk time per hex from `walk_kmh` and the resolution), `find_path()` (A* over
  `h3.grid_ring`; its `floor` must not exceed the cheapest cost) and `advance()` (one tick of stepping). Callers supply
  the per-cell cost and contact checks; zones and ownership live outside.
- `classes.py` — player classes as `ClassSpec` modifiers (movement cost, attack/defence, healing). Classes are free for
  everyone. Subscriber perks are cosmetics and convenience: faster class swaps, shield looks and schedule,
  full battle history with replays, nemesis (aimed only at other subscribers and bots). The test: in the same fight a free player and a subscriber are equally
  hard to kill. Flag any proposed perk that fails it.
- `chunks.py` — hex → chunk tiles. Tile maths is imported from argus-atlas (`argus_atlas.mercator`,
  `argus_atlas.chunks`), never reimplemented, so the game and the bake worker agree on addressing. A hex is
  playable only when every chunk in `cell_chunks()` is ready. argus-atlas's base install is pure Python; GDAL is
  only needed where bakes run (the GPU host), never on the game server.

`src/atlas_realms/bakeplan.py` — `python -m atlas_realms.bakeplan` prints a bash script of argus-atlas commands for a
block of chunks (`dem` for one GLO-30 GeoTIFF, one Overpass call for the controls and one for place names, recipes,
`chunk` per tile per style in north-to-south, west-to-east order, then `labels --job` on the first style's chunks).
It bakes with an image built at the argus-atlas commit in `docs/atlas/sync.json` (the docs Atlas publishes; step 0 builds it from a clone, refuses to start without it, and refuses to bake beside jobs of another build); the pip pin stays on the release. `--tiles` takes an exact chunk list instead of a block. Every area's plan is generated from `bakes/plans.json` by `make bake-plans` (`scripts/bake_plans.py`; `tests/test_bakeplan.py` fails on a stale or hand-edited plan, so a docs import and the plans move together). The script reads the host layout from the environment: DEMs and OSM under `$ATLAS_HOST/dem`, recipes in
`$ATLAS_HOST/recipes`, the model tree at `$ATLAS_MODELS`, finished chunks at `$ATLAS_MAPS/areas/<area>/<style>/<style>-<z>-<x>-<y>/`
(one dir per style is also what argus-atlas seams against). Atlas and the arena paint from the same bakes and exports (`var/tiles-areas/<area>`). Generated plans are committed under `bakes/` and run on
the GPU host. Never commit host-specific paths, host names or LAN addresses.
`data/` holds the region outlines (see `data/README.md`). The starting region is New Zealand (Cook Strait 7×7 block, H3 res 5).

`src/atlas_realms/tick/` — the tick. `engine.run_tick(world, orders, secret)` is pure and returns a `TickOutput`
(new world, battles, ledger, bake requests, order results); its steps run in the doc's order and placeholders mark
what later phases add (capitals, over-cap choices, garrisons, clan spawning: M4; AI events: M5). `world.py` holds
the dataclasses, `store.py` loads/saves (only changed rows), `runner.py` runs one tick per transaction under
`pg_try_advisory_xact_lock` and skips if it is held. After loading orders it waits for the next drand round
(`tick/beacon.py`, stored in `tick_beacons` with its signature) and skips the tick if no relay has it; tests pass a stub
`beacon=` so they never touch the network. Money is integer milli-coins. Unit floats are double precision
in the DB because a replay must load exactly what the tick saved (`test_world_round_trips_exactly`).
Every city has a garrison (`hexes.garrison_hp`: `city_garrison_hp`, or `garrison_hp` for a user capital). Entering
an empty enemy city is a battle against it (`siege`); the capture timer only starts once it is at 0. Garrisons
never heal; the `repair` order restores them at `fee_repair` per HP. Claims start full, captures start at 0.
Shields: the `shield` order (`{"on": false}` drops it) gives a unit `shield_hp_mult` × max HP of shield. As defender
the battle is fought on HP + shield and the shield takes the damage first; a shielded unit can't attack. The same for
everyone: toggled within `shield_daily_hours` a game day, `shield_cooldown_minutes` between, `shield_breach_cooldown_hours`
after a breach. Subscribers may schedule it (`shield_schedule`), from the same budget. Bots count shields in `odds()`.
Revenge: `pursue` chases a user's unit, re-pathing each tick, until it falls or a move/cancel replaces it.
Nemesis (subscribers): `nemesis` picks one target, only a subscriber, a bot, or a clan (its subscribed and bot members
count), changed once per `nemesis_change_hours`. The hunter gets `nemesis_bonus` in their fights (`nemesis_of`), so
free players never face it. `hunt` pursues the nearest nemesis unit or land for `hunt_minutes_daily` a game day.
Nemeses are public: `state()["nemesis_table"]` lists every target and its hunters.
Bot services (everyone, paid in coins): `hire` a bot as mercenary (`merc_minutes`, against the hirer's nemesis or a
target), guard (`guard_minutes`, patrols the hirer's land) or protection (`protect_minutes`, a truce). Contracts live
in `World.contracts` (table `contracts`, ids assigned by the tick); an active one makes bot and hirer `allied`. One
merc/guard job per bot at a time, the rest queue (a start can be scheduled); the merc's target buys it off at
`outbid_factor` × its price. Jobs split kill loot evenly with the hirer. `buy_city` buys a bot's city. Prices:
`quote()`/`city_price()` = list × `price_factor()` (buyer's income vs the average, clamped), to push towards balance.

Solo arena (the first playable mode; PvP on the shared Postgres world comes later and reuses the client):
- `bots.py` — personalities (settler, raider, warden, corsair, sapper, jackal, rival, reaver). Bots only issue
  orders through the engine, like players. They judge fights with `odds()` (the engine's `battle_modifiers` +
  `resolve_battle`, memoised Monte Carlo) and build up below their `risk` threshold. Push-back on the leader: the
  dominance target, the rival's counter-pick (`ANSWERED_BY`), the reaver raiding the top 10% of earners. Bot units
  are `veteran`: the engine gives them +5% attack/defence per kill (cap 30%) and pays a capped bounty to their killer.
  Bots move at `bot_speed` (half) except on a mercenary or guard job.
- `news.py` — "while you were away": `events_of()` (battles, captures, bounties per tick) and `digest()`, a seeded,
  template-written report in a sports-desk voice (`/api/solo/{sid}/report?since=`). Your own stories always run;
  other repeats go into round-ups.
- `solo.py` — `Session` (hex cap = claimable land / players × `cap_share`, set in `Session.new`; everyone starts
  with `start_coins`; it ticks `PACE` (1.5×) faster than its speed setting): an in-memory World built from the tile manifest + outline, lazy catch-up ticking, `state()`.
- `api.py` — FastAPI: `/api/manifest`, `/api/meta`, `/api/solo/...`, static `/tiles` and `web/`.
- `tiles.py` — exports `<maps>/areas/<area>/<style>/<job>/fantasy.pmtiles` (one area at a time, `make tiles`) to static `<style>/<z>/<x>/<y>.png` + `manifest.json`,
  and merges the chunks' `labels.geojson` (argus-atlas place names) into one `labels.geojson` the client draws.
- `seed.py` — hexes from a manifest + outline (`plan_hexes`), into Postgres or (via `solo.build_world`) memory.
- `web/` — the MapLibre client, no build step (`index.html`, `app.js`, `style.css`).
- `protocol.py` — the solo API's request models, `meta()` and `SPAWNS`, without FastAPI (shared by `api.py` and `arena/`).

`arena/` — the solo arena as a static site (realms.dragonhound.dev), design in `arena/DESIGN.md`. Pyodide runs the
unchanged Python in a Web Worker: `arena/py/arena_bridge.py` answers the client's `/api/...` requests in-process (the
wall clock passed in), `arena/src/app.js` is `web/app.js` talking to the worker and saving to localStorage.
`arena/build.py` zips `src/atlas_realms` + argus-atlas + structlog, copies Pyodide (pinned in `arena/package.json`) and
sha256-checked wheels, the regions (`arena/regions.json`: per map a world manifest in `arena/world/`, an outline in
`data/`, spawns and the tile export to paint from; the client picks one at new game, the save records it), optional
tiles (per region, packed by
`arena/sea_fill.py` with the open sea painted in, then `arena/tiles_webp.py` into XYZ folders of WebP q88 files:
never PMTiles, since Pages serves no range requests; `test/no-range.test.mjs` guards it), and writes `_headers` (CSP
with `'wasm-unsafe-eval'`). `make arena-test` = build + `arena/test/parity.test.mjs` (Pyodide vs CPython, same state
every tick, and save/restore, on every region). A region baked at another chunk zoom (Iceland: z7) is mapped onto the
game's z10 by `rules.chunks.at_zoom`. The tick copies the world with `World.snapshot()`, not deepcopy (95% of a tick
on Iceland); a new mutable field on Hex/Unit/User/Contract must be copied there (`tests/test_world_snapshot.py`). Any engine change is picked up by rebuilding; the parity test fails on a stale bundle.
Don't edit `site/vendor/dh` from here: the arena copies it at build time (its pasted strip may differ only in the
label suffix, which `build.py` allows). Loading quotes live in `arena/src/quotes.json`: genuine, sourced words only.

`src/atlas_realms/db.py` — psycopg autocommit connection (`ATLAS_REALMS_DSN`) and a forward-only migration runner over
`src/atlas_realms/migrations/*.sql`, recorded in `schema_migrations`. Never edit an applied migration; add a new file.
`db/Dockerfile` builds Postgres 16 + PostGIS + h3-pg from `postgres:16-bookworm` (the postgis/postgis images lack
h3-pg). h3-pg function names differ from h3-py's: e.g. `h3_grid_disk` exists, `h3_grid_ring` is `h3_grid_ring_unsafe`.

## Commands

```bash
make install   # uv venv --python 3.12 + editable install with [dev]
make test      # includes @slow statistical checks (~2 s)
make lint      # ruff 0.15.16 check + format --check
make format
make sim
make db-up     # compose database on 127.0.0.1:5433
make tick      # one tick against the database (LOOP=1 keeps ticking)
make test-db   # adds the DB tests, which SKIP unless ATLAS_REALMS_TEST_DSN is set
make preview   # export var/maps/areas -> var/tiles-areas and serve the solo arena on http://127.0.0.1:8110
```

## Conventions

- When a rule changes in the doc, change `Tunables` or the rule and the test that pins it in the same commit.
  `tests/test_combat.py::DOC_RATES` mirrors the doc's table; if combat changes, re-run `make sim` and update both.
- Everything a tick does must be a pure function of DB state plus the tick seed, so ticks can be replayed.
- Ruff line-length 120, Pydantic v2, structlog, hatch-vcs versions from git tags. Matches argus-atlas tooling.
- Remote: https://github.com/smk762/argus-realms (default branch `dev`; `main` is the release line).
- **Modern Web Guidance.** `.claude/settings.json` enables Chrome's [modern-web-guidance](https://github.com/GoogleChrome/modern-web-guidance)
  plugin (the local copy from `npx modern-web-guidance install` is gitignored). Search it before writing UI, CSS or
  browser-API code in `site/` or `web/`, and re-run `npx -y modern-web-guidance@latest legacy-scan site` after big changes.
  `site/` is the static demo: no inline scripts (the page logic is `site/app.js`), MapLibre comes from cdnjs with SRI
  hashes (bump the hash with the version), and `scripts/build_static.py` writes the CSP into `_headers`, hashing the
  inline `<style>`. A new third-party origin needs a CSP change there. The docs page (`/docs/`, `docs/index.html` in the build) is rendered at build time from the
  argus-atlas docs imported into `docs/atlas/` (`scripts/atlas_docs.py`, `make atlas-docs`; `sync.json` records the atlas
  commit, version and file hashes): never edit them, re-import. Its styles live in `site/docs/docs.css`, so it adds no CSP
  hash. `make static` refuses tiles baked at another atlas commit than the docs (docs/deploy.md §5); "How it was made"
  and the style tooltips read the bake from the export manifest (`atlas_realms.tiles.bake_summary`), never hard-code it.
  `site/vendor/dh/` is the vendored Dragonhound shared layer (tokens, demo strip, badge, `dh-theme.js`), pinned by
  `.dh-shared.json`: re-sync it from dragonhound-site (`node scripts/sync-shared.mjs <this repo> --dest site/vendor/dh`)
  and check it with `node site/vendor/dh/sync-shared.mjs --check .`; never edit the copies. The build inlines its
  stylesheets into `index.html` (hashed by the CSP) and fails if the strip pasted into `index.html` drifts from
  `dh-strip.html` (pasted unedited; `data-dh-site="atlas"` on `<html>` marks Atlas current, and `dh-theme.js`
  adds `aria-current` and keeps the toggle's `aria-pressed` in sync). Never set `font` on `html` (the tokens are rem);
  panel text uses the `--dh-text-*` sizes, 12 px (`--dh-text-2xs`) at the smallest. Favicons are the argus-atlas set from argus-cortex `brand/packages/atlas/favicon/`, copied unchanged (all rights reserved).
