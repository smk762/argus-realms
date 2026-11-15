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

Cloudflare Pages allows 25 MiB per file and 20,000 files, so a lossless PMTiles per style (60–66 MiB) doesn't fit. Every lossy
encoding below fits with room to spare. Each one was packed into one PMTiles file per style (`tiles_pack.py`, z5–12,
658 tiles, method 6 for WebP, speed 6 for AVIF). PSNR is measured against the PNGs over the 480 z12 tiles:

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

**Pick: WebP q88**, one PMTiles per style (5.7 and 7.2 MiB). It looks the same as the PNGs at the zoom where detail
matters, and it is 10× smaller.
- q92 costs 27% more bytes for a difference nobody will see.
- AVIF is about 10–15% smaller at the same PSNR, but it softens the hatching more at equal size, encodes about 10× slower,
  and decodes slower on low-end phones. MapLibre and pmtiles do handle it, so AVIF is a possible later switch.
- Lossless WebP for the low zooms only would add about 1 MiB for zooms where q88 is already indistinguishable.
- The painted tiles have no text in them (this export has no `labels.geojson`). Place names come from the basemap,
  under the fog, so label legibility doesn't depend on the tile encoding.

How it is served:
- `build.py --tiles var/tiles` packs `dist/tiles/<style>.pmtiles`. The pack is cached in `arena/.cache/tiles`, keyed on
  the source tiles, and the build fails above 25 MiB.
- The client registers the `pmtiles://` protocol, from `vendor/pmtiles.js` (pmtiles 4.5.0, pinned, served from `'self'`).
- Pages answers range requests on static files. `scripts/serve.mjs` does the same locally.
- Without tiles, the build uses the OpenFreeMap basemap and labels it as a placeholder.

R2 isn't needed. If tiles must ever be lossless, the options are R2 (a public bucket on a custom domain, with CORS for
range requests) or z ≤ 11 PNG (16–18 MiB).

Licensing: the painted tiles are ODbL Produced Works from OpenStreetMap data plus Copernicus GLO-30. The footer always
shows the manifest's attribution ("Map painted by argus-atlas from Copernicus GLO-30 and © OpenStreetMap
contributors") and the basemap's OpenFreeMap / OpenMapTiles / OpenStreetMap links.

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
The strip's label suffix reads "runs in your browser" instead of dh-shared's fixed "recorded output". dh-shared 1.2.2
has no option for it (`labelMore` is a constant in `dh-strip.mjs`, and the CSS only hides the span on phones). So the
arena pastes the strip with that one span changed, and `build.py` allows that difference and no other. The upstream fix
would be a per-site label suffix in `dh-strip.mjs`, for example a `labelMore` override or a `data-dh-label` attribute.
The strip doesn't list Realms-the-game yet: the current Realms link moves to Atlas, and a Realms link is added when the
game ships. Reduced motion: camera moves don't animate and CSS transitions are off under `prefers-reduced-motion`.

## Commands

```bash
make install && npm ci --prefix arena
make arena                  # build arena/dist (painted tiles from var/tiles when it exists)
make arena-dev              # build and serve on http://127.0.0.1:8120 with the _headers CSP
make arena-test             # build without tiles + the parity test (in `make check` and CI)
node arena/test/playtest.mjs http://127.0.0.1:8120/ review   # Playwright play-test, local only
```
