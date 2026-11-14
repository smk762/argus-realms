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
It needs argus-atlas past 0.2.0a1 (`ATLAS_REF`, an image built from that commit); the pip pin stays on the release. The script reads the host layout from the environment: DEMs and OSM under `$ATLAS_HOST/dem`, recipes in
`$ATLAS_HOST/recipes`, the model tree at `$ATLAS_MODELS`, finished chunks at `$ATLAS_MAPS/<style>/<style>-<z>-<x>-<y>/`
(one dir per style is also what argus-atlas seams against). Generated plans are committed under `bakes/` and run on
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
- `tiles.py` — exports `<maps>/<style>/<job>/fantasy.pmtiles` to static `<style>/<z>/<x>/<y>.png` + `manifest.json`,
  and merges the chunks' `labels.geojson` (argus-atlas place names) into one `labels.geojson` the client draws.
- `seed.py` — hexes from a manifest + outline (`plan_hexes`), into Postgres or (via `solo.build_world`) memory.
- `web/` — the MapLibre client, no build step (`index.html`, `app.js`, `style.css`).

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
make preview   # export var/maps -> var/tiles and serve the solo arena on http://127.0.0.1:8110
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
  inline `<style>`. A new third-party origin needs a CSP change there.
  `site/vendor/dh/` is the vendored Dragonhound shared layer (tokens, demo strip, badge, `dh-theme.js`), pinned by
  `.dh-shared.json`: re-sync it from dragonhound-site (`node scripts/sync-shared.mjs <this repo> --dest site/vendor/dh`)
  and check it with `node site/vendor/dh/sync-shared.mjs --check .`; never edit the copies. The build inlines its
  stylesheets into `index.html` (hashed by the CSP) and fails if the strip pasted into `index.html` drifts from
  `dh-strip.html` (pasted unedited; `data-dh-site="atlas"` on `<html>` marks Atlas current, and `dh-theme.js`
  adds `aria-current` and keeps the toggle's `aria-pressed` in sync). Never set `font` on `html` (the tokens are rem);
  panel text uses the `--dh-text-*` sizes, 12 px (`--dh-text-2xs`) at the smallest. Favicons are the argus-atlas set from argus-cortex `brand/packages/atlas/favicon/`, copied unchanged (all rights reserved).
