# Realms in the browser: design

realms.dragonhound.dev becomes the Realms minigame, fully in the browser: a static site on Cloudflare Pages with no
game server, no GPU and nothing exposed. This folder is that site. `web/` + `atlas_realms.api` (the server version)
stay as they are; PvP on the shared Postgres world will still need a server later.

## Scope

In: the **solo arena** only. You against up to eight bots around Cook Strait, the same rules, bots, bounties,
veterancy, shields, contracts, nemesis preview, news desk and patron-perk preview as `make preview`.

Out: accounts, PvP, the shared world (Postgres, drand beacon, tick runner), baking (the fog never lifts past the baked
chunks, as in `make preview`), and anything that needs a server.

## Porting approach: Pyodide, not a TypeScript port

The task's prior was a TypeScript port if the solo-arena rules are compact. They aren't: the solo path is about
2,700 lines of Python that is still moving.

| Module | Lines | What a port would have to reproduce exactly |
|---|---|---|
| `tick/engine.py` | 1,017 | every tick step in the doc's order: movement, battles, sieges, garrisons, shields, contracts, nemesis, hunts, loot, bounties |
| `bots.py` | 382 | eight personalities; `random.Random(f"{seed}:{tick}:{bot}")`, i.e. CPython's Mersenne Twister with SHA-512 string seeding, `random()`, `choice()` via `getrandbits` rejection; memoised Monte Carlo odds |
| `solo.py`, `news.py` | 704 | session, catch-up timing, state shape; the news desk's seeded templates |
| `rules/*` | ~550 | HMAC/SHA-256 dice stream (a public contract), combat, A* over h3, pydantic `Tunables` and classes |

Comparison:

| | TypeScript port | Pyodide (chosen) |
|---|---|---|
| Download | ~150 KB of JS + h3-js (~0.5 MB) | 15.3 MB of Pyodide, wheels and game (~8.6 MB gzipped on the wire); cached (immutable) after the first visit |
| Cold start | instant | ~7 s on a desktop from localhost (WebAssembly compile + packages, measured in Chromium and Node); add the download on a real network. The map and a loading card with real download progress show while it loads. |
| Per tick | <1 ms | 16.5 ms on average over 600 ticks (CPython: ~8 ms), in the worker, off the main thread; the arena runs at most 6 ticks/s (4×) |
| Fidelity | a second implementation of every rule; drifts with every engine change unless each one is ported too | the same source files as the server; no fork |
| Determinism | needs bit-exact MT19937 + Python string seeding + float formatting in JS | identical by construction; the parity test confirms it across Python 3.12 → 3.14 and h3 4.5 → 4.4 |
| Testability | needs fixtures for every rule | the existing pytest suite covers the rules; one parity test covers the bundle |

Pyodide wins on fidelity and maintenance; it loses on first-load weight. The weight is a one-off per visitor, it runs in
a Web Worker, and the page shows the map and a loading line while it loads. If the load time proves to be a problem,
the parity harness (below) is exactly what a later TS port would need to be checked against, so this choice doesn't
close that door.

How it fits together:

- `src/worker.mjs` loads Pyodide (self-hosted under `/pyodide/`, pinned by `package.json`), the `h3` and `pydantic`
  wheels from Pyodide's distribution (sha256-checked against its lock file at build time), and one zip of the game's
  Python (`atlas_realms` from `src/`, the pure-Python parts of `argus-atlas` and `structlog` from the project
  environment, `py/arena_bridge.py`, the NZ outline and the world manifest).
- `py/arena_bridge.py` answers the same requests `atlas_realms.api` serves (`/api/meta`, `/api/solo/...`) as a
  function call, with the wall clock passed in, so lazy catch-up ticking works exactly as on the server. The request
  models and `meta()` moved from `api.py` into `atlas_realms/protocol.py` so both use the same validation.
  `psycopg` has no Pyodide build; `seed.py` and `db.py` import it at module level for the shared world's DB path only,
  so the bridge installs a stub module that raises if anything tries to connect.
- `src/app.js` is `web/app.js` with `fetch()` replaced by a message to the worker, a localStorage save, the strip,
  relative tile URLs, the placeholder basemap notice, and reduced-motion camera moves.

## Seeding

drand is only for the shared world. The solo session's battle secret is the game seed: 32 random bytes from
`os.urandom` (Pyodide maps it to `crypto.getRandomValues`), or `?seed=<64 hex>` in the URL to replay one. Battles
(HMAC dice), bot decisions and the news desk all derive from it, so the same seed plus the same orders at the same
ticks is the same game.

Finding: the server's game state was already deterministic for a given secret, independent of `PYTHONHASHSEED`
(checked over 300 ticks with four hash seeds); only the pickled save bytes vary with set ordering, and nothing
compares those.

## Save state

`Arena.save()` pickles the whole `Session` (minus its lock), zlib-compresses and base64-encodes it, tagged with a save
format version and a hash of the `atlas_realms` sources. The client stores it in `localStorage["arena-save"]` every 3 s
of play and when the tab is hidden (about 16 KB after 600 ticks); every access is in try/catch, and if storage is off or full the player is told once
that the game ends with the tab. A save from another rules build is refused (409) and the player starts a new game,
because unpickling old dataclasses into changed code is unsafe; a JSON save format can replace this once the rules
settle. Pickle only ever reads the player's own save from their own origin.

The "When away" setting survives a closed tab: as the page hides, the client records what it would have done; on the
next load `restore(..., away="pause")` keeps the closed time out of the game, and `away="shield"` raises the shield
before the missed ticks are caught up (at most 120 per request, as on the server).

## Parity test

`test/parity.test.mjs` runs `py/parity_scenario.py` twice: under CPython from `src/` (the reference) and in Pyodide
from the built `dist/` (Node). The scenario starts a fixed-seed game, issues scripted orders (move, march, attack the
nearest bot, heal, shield on/off, class swap, respawn, keep claiming land) driven by the wall clock at 4×, saves at the
halfway tick, plays on, then restores the save into a fresh Arena and plays the second half again. It asserts:

- the same rules build hash (the zip holds exactly `src/atlas_realms`);
- the same state digest on every tick, the same final state, news report and fog shape;
- save + restore ends exactly where the uninterrupted game ends, in both runtimes.

Result: identical over 400 ticks (CI, ~20 s) and 2,000 ticks (local run). The one difference found: h3 4.5.0 (CPython,
from pip) and 4.4.2 (Pyodide) start `cells_to_geo` rings at different vertices, so the fog GeoJSON differs in vertex
order but not shape; the test compares the fog by shape. Nothing in the game state depends on it.

`tests/test_arena_bridge.py` covers the bridge's routing, error codes, same-seed replay and save/restore under pytest.

## Tiles

The painted Cook Strait tiles are baked on the GPU host by `bakes/cook-strait.sh` and are not in git. The local export
(`make tiles`: `var/maps/<style>/<job>/fantasy.pmtiles` → `var/tiles/<style>/<z>/<x>/<y>.png`) measures:

| | antique | tolkien | both |
|---|---|---|---|
| XYZ PNG, z5–12 | 658 files, 59.6 MiB | 658 files, 65.8 MiB | 1,316 files, 125.3 MiB; largest tile 163 KiB |
| of which z12 | 480 files, 43.2 MiB | 480 files, 47.6 MiB | |
| one PMTiles, PNG, z5–12 | 59.6 MiB | 65.8 MiB | over the 25 MiB file limit |
| one PMTiles, PNG, z5–11 | 16.4 MiB | 18.2 MiB | fits, but loses the native z12 detail |
| Source chunks (`var/maps`, 60 `fantasy.pmtiles`) | | | 122.7 MiB |

The encoding was chosen by packing each candidate into one PMTiles file per style (z5–12, 658 tiles, method 6 for
WebP, speed 6 for AVIF), which also gives the per-style totals. PSNR is measured against the PNGs over the 480 z12
tiles. (The arena now serves the same tiles as plain files instead; see "How it is served".)

| Encoding | antique | tolkien | both | z12 PSNR (antique / tolkien) |
|---|---|---|---|---|
| PNG (original) | 59.6 MiB | 65.8 MiB | 125.3 MiB | lossless |
| WebP lossless | 36.7 MiB | 37.9 MiB | 74.6 MiB | lossless, still over 25 MiB |
| WebP q92 | 7.35 MiB | 9.06 MiB | 16.4 MiB | 42.4 / 42.9 dB |
| **WebP q88 (chosen)** | **5.73 MiB** | **7.18 MiB** | **12.9 MiB** | **40.7 / 41.0 dB** |
| WebP q85 | 4.90 MiB | 6.20 MiB | 11.1 MiB | 39.7 / 39.9 dB |
| WebP q80 | 3.97 MiB | 5.09 MiB | 9.1 MiB | 38.4 / 38.3 dB |
| AVIF q70 | 4.43 MiB | 5.20 MiB | 9.6 MiB | 40.1 / 39.9 dB |
| AVIF q60 | 3.32 MiB | 3.87 MiB | 7.2 MiB | 38.5 / 37.7 dB |

The side-by-side renders (`arena-tiles-<style>-<view>.png` in the review folder) show both styles at device scale 2,
with the same views of each variant: z8 (the whole strait), z10 (the Marlborough Sounds coast), and z12 (the Wellington
harbour coast, and Tararua Range relief). Tolkien's pen hatching is the hardest content. At q80 and AVIF q70 the
fine strokes soften visibly at z12. At q85 the difference only shows when you flip between images. At q88 I couldn't
tell it from the PNG.

**Pick: WebP q88** (5.7 and 7.2 MiB per style before the sea fill). It looks the same as the PNGs at the zoom where detail
matters, and it is 10× smaller.
- q92 costs 27% more bytes for a difference nobody will see.
- AVIF is about 10–15% smaller at the same PSNR, but it softens the hatching more at equal size, encodes about 10× slower,
  and decodes slower on low-end phones. MapLibre does handle it, so AVIF is a possible later switch.
- Lossless WebP for the low zooms only would add about 1 MiB for zooms where q88 is already indistinguishable.
- The painted tiles have no text in them (this export has no `labels.geojson`). Place names come from the basemap,
  under the fog, so label legibility doesn't depend on the tile encoding.

How it is served: **plain XYZ folders, one WebP file per tile** (`tiles/<style>/<z>/<x>/<y>.webp`).
- `build.py --tiles var/tiles` paints in the open sea (`sea_fill.py`) and re-encodes every tile (`tiles_webp.py`). The
  result is cached in `arena/.cache/tiles`, keyed on the source tiles and the fill version.
- MapLibre reads them with a plain raster source: a `tiles` URL template, with the same minzoom, maxzoom and bounds as
  before.
- Measured: 2,128 tiles (1,064 per style), 14.7 MiB (antique 6.2, tolkien 8.2), largest tile 32 KiB. The whole dist
  is 2,158 files and 30.2 MiB.
- The build enforces Pages' limits. It fails if any file exceeds 25 MiB, if the deployment reaches 20,000 files, or if
  any `.pmtiles` file is present.
- Without tiles, the build uses the OpenFreeMap basemap and labels it as a placeholder.

**Why not PMTiles.** The first deploy used one PMTiles archive per style, and the painted map failed live. PMTiles
reads its archive in HTTP range requests, but Cloudflare Pages doesn't answer them on `*.pages.dev`: every file comes
back as 200 with the whole body. On a custom domain, ranges only work through the zone cache, for file extensions it
caches, and `.pmtiles` isn't one of them. The local dev server had emulated ranges, which hid this.

Guards against it coming back:
- `scripts/serve.mjs` now behaves like Pages: it ignores `Range` and always answers 200 with the whole file.
- `test/no-range.test.mjs` runs in CI. It fails if runtime code mentions PMTiles, a `Range` header, `bytes=` or a
  custom tile protocol, or if a built dist breaks the Pages limits.
- `test/tiles.mjs` (Playwright, local, needs the tiles) serves the built dist with `python -m http.server`, which has
  no range support. At four views (z8 to z12) it checks that painted tiles load (58 tile responses, all 200 and
  `image/webp`), that no request carries a `Range` header, and that there are no console errors.

R2 isn't needed. If tiles ever had to be one archive, it would need R2 (a public bucket on a custom domain, with CORS
for range requests).

Licensing: the painted tiles are ODbL Produced Works from OpenStreetMap data plus Copernicus GLO-30. The footer always
shows the manifest's attribution ("Map painted by argus-atlas from Copernicus GLO-30 and © OpenStreetMap
contributors") and the basemap's OpenFreeMap / OpenMapTiles / OpenStreetMap links.

## Fog and sea: no basemap showing

The painted block is 7 × 7 chunks, but only 30 were baked. The other 19 were skipped by the bake plan because they hold
no land (its test: no `data/nz_mainland.geojson` land cell touches the chunk). The first version drew the OpenFreeMap
basemap under everything, so grey showed around the block and in the sea gaps between the painted rectangles. Painted
builds now draw no basemap at all.

- **Outside the block: fog.** `sea_fog.py` writes `fog.webp`, a dark mist (the game fog's `#0d0f13` with soft lighter
  wisps) laid out in Web Mercator over the block plus a 30% margin.
  - It is opaque outside the block and fades to clear about one hex (17 km) inside its edge.
  - MapLibre draws it as an image layer above the painting.
  - `maxBounds` is the mist's extent. MapLibre won't zoom out past the point where those limits fill the view, so the
    start view (about z8 on a desktop) is also the furthest out you can go. No part of the world beyond the mist shows.
- **Inside the block, where nothing was baked: baked sea tiles.**
  - **The problem:** a flat texture behind the gaps showed as rectangles. Each painted chunk's sea is a slightly
    different blue, so the fill matched none of them; neighbouring painted chunks also met in visible straight lines
    out at sea.
  - **The fix:** `sea_fill.py` paints the open-sea chunks (`seed.sea_chunks`, the same rule that makes them playable)
    into the tile set at build time, on the CPU, as ordinary tiles. They render exactly like painted ones at every
    zoom.
  - **How:** it works on one z12 canvas of the block, in five steps.
    1. Level the seams between painted chunks out at sea. Each side shifts by half the colour step, easing out over
       256 px. Sea pixels only, low frequencies only, so the grain stays as painted and the coasts keep their colour.
    2. Fill the gaps by push-pull interpolation from the painted sea next to them.
    3. Add seeded grain that matches the painted sea's own noise and dark speckles, measured from it. It never repeats
       visibly, and builds stay reproducible.
    4. Mirror each painted neighbour's sea across the shared edge, easing into the fill over 320 px, so colour and
       grain are continuous at the edge itself.
    5. Write the block's tiles at every zoom from the canvas.
  - **Cost:** about 40 s per style at build time (cached), and about +1.2 MiB of tiles.
  - **Checked:**
    - Before/after crops at z9.6, z11 and z12 across five boundaries (painted to gap, gap to gap), both styles:
      `arena-seams-<style>-<boundary>.png` in the review folder.
    - At z11 and z12 the boundaries are no longer visible.
    - At z9.6 the strait now shades gradually between the painted chunks' differing blues, where before it stepped.
      Tolkien's speckle density still varies a little from one painted chunk to the next; that is in the painting
      itself.
  - **Fallback:** the style's sea colour and texture are still the map background, where no tile covers.
- **In play: the strait is open water.** `seed.sea_chunks()` is in the Python rules, so the server and the browser agree.
  - Inside the bounding box of the baked chunks, a chunk with no land counts as ready. "No land" is the bake plan's own
    test: no res-7 land cell touches the chunk. Open sea needs no painting.
  - `solo.build_world` adds these chunks, so their hexes are playable water. For Cook Strait that is exactly the bake
    plan's 19 skipped chunks, and 80 playable water hexes instead of 16.
  - A land chunk whose bake hasn't finished never counts as sea.
  - The rules already treat water as crossing-only: it has a movement cost (marines are fast on it), and nobody can
    claim it or fight on it.
  - Bot behaviour, checked over 1,500 ticks with three seeds against the closed strait: unchanged. No water hex is ever
    owned. The narrows were already baked, so routes across existed before.
  - The DB seed for the shared world is unchanged; PvP will need the same rule.
  - Saves from before this (save version 1) are refused.
- **Place names:** town and city names (OpenFreeMap `place` layer, city and town only, from z8) are drawn on the
  painting in the parchment style, inside the block only and under the fog.
- **Placeholder build (no tiles):** keeps the OpenFreeMap basemap and says so, with the same mist and pan limits
  outside the block.

## Touch

Touch wording and long-press follow the input device, not the screen width: `(pointer: coarse), (hover: none)`.
- **Hint:** on touch it reads "Tap a hex to move there. Tap one with units or a city, or long-press, for more."
- **Long-press:** one finger held 500 ms without moving opens the same hex menu a right-click does. iOS Safari fires
  no `contextmenu` and MapLibre has no long-press, so `wireLongPress` implements it, and the tap that ends a long-press
  doesn't also move you.
- **Test:** `test/mobile.mjs` checks the wording on phones and desktop, and long-presses a hex through CDP touch
  events.

## Phones

At 640 px wide or less, and on landscape phones (height 500 px or less), the map gets the screen.
- **Top:** under the shared strip sits a slim title bar with the clock and two toggles, Players and Game. Nothing ever
  covers it.
- **Players toggle:** the side panel (players, cities, battles, rivals) opens as a bottom sheet. It is closed by
  default, with a ✕ to close it; Escape or a tap on the map closes it too.
- **Game toggle:** the map, speed, away, pause and new-game options open as a menu.
- **Bottom bar:** one row of 44 px actions (Walk, March, Heal, Stop, Shield, ⋯). "⋯" adds a row with the shield
  options and the class swap. The credits fold into ⓘ, as on Atlas.
- **Zoom:** the zoom buttons are hidden on phones, where pinch and double-tap zoom work.
- **Desktop:** unchanged.

`test/mobile.mjs` checks 390 × 844, 320 × 568 and 844 × 390, plus desktop:
- no sideways scroll;
- the title bar is never covered, even with the sheet open;
- every visible control is at least 44 px;
- a tap on the map moves your unit;
- the sheet, menu, "⋯" and ⓘ each open and close.

## Loading

The worker first downloads everything Pyodide will need, as listed in `arena.json` with sizes (15.3 MB). It counts
bytes, so the bar shows real progress for the download, which takes up three quarters of the bar. Pyodide's own requests
for those files are then answered from memory. This matters because, measured in Chromium, the 9 MB wasm otherwise
downloaded twice: the HTTP cache didn't keep it. The steps after that (start Python, load h3 and pydantic, import the
rules and build the world) can't be measured, so each one shows as a shimmering segment of the bar.

The card shows a quote from `src/quotes.json`, in the style of Civilization's loading quotes: italic text with
"— Name" beneath, and the source as a tooltip. Each entry names the work it comes from, and only well-attested words are
included. The quote crossfades every 5.5 s, starting at random and never showing the same quote twice in a row.

Accessibility: the status line is a polite live region, and its text changes once per step, not on every byte. The bar
is a `progressbar` (no value while a step is indeterminate). The quotes are not a live region. Under reduced motion
there is no shimmer and no fade, and one quote stays put. The card uses the shared tokens, so it follows the light/dark
theme. It fades out (or is simply hidden, under reduced motion) once the game is ready.

## Static build and CSP

`build.py` writes `dist/` and `_headers`. Like `site/`: MapLibre from cdnjs with SRI, OpenFreeMap for the basemap, no
inline script, and here no inline style either (the page links the vendored stylesheets). The CSP adds
`'wasm-unsafe-eval'` (WebAssembly compilation only, no JS `eval`) and `worker-src 'self' blob:` (the game worker and
MapLibre's). The play-test checks there are no CSP violations. Pyodide, the wheels and the Python zip (content-hashed
name) are served `immutable`; the page, `arena.json` and `manifest.json` are `no-cache`.

The Dragonhound shared layer is copied from `site/vendor/dh` at build time (one vendored copy in git, pinned by
`.dh-shared.json`); `<html data-dh-site="realms">` marks Realms current, and the build fails if the pasted strip drifts.
The strip's label suffix reads "runs in your browser" instead of the default "recorded output": `data-dh-label="browser"`
on `<html>` picks it (dh-shared 1.2.4; the pasted strip carries both suffixes and `dh-strip.css` shows one), so the
strip is pasted unedited and `build.py` checks it exactly. The strip (dh-shared 1.2.5) lists Lightbox, Studio, Atlas,
Cull, Realms, Safelight, Assay and Sightline; below 600 px its links scroll sideways and `dh-theme.js` brings Realms
into view. Reduced motion: camera moves don't animate and CSS transitions are off under `prefers-reduced-motion`.

## Commands

```bash
make install && npm ci --prefix arena
make arena                  # build arena/dist (painted tiles from var/tiles when it exists)
make arena-dev              # build and serve on http://127.0.0.1:8120 with the _headers CSP
make arena-test             # build without tiles + the parity test (in `make check` and CI)
node arena/test/playtest.mjs http://127.0.0.1:8120/ review   # Playwright play-test, local only
```
