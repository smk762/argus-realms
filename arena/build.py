"""Build the browser arena into arena/dist: a static site, no server.

    uv run --no-sync python arena/build.py                      # placeholder basemap (no painted tiles)
    uv run --no-sync python arena/build.py --tiles              # with each region's painted tiles, where present
    uv run --no-sync python arena/build.py --check              # page checks only (CI): strip, marker, CSP

What goes in:
- the page (arena/src), with the Dragonhound shared layer copied from site/vendor/dh (one vendored copy in git);
- Pyodide (from node_modules/pyodide, pinned in package.json) and the wheels the rules need (h3, pydantic and their
  dependencies), downloaded once into arena/.cache and checked against the sha256 in Pyodide's lock file;
- the game's Python as one zip: atlas_realms from src/, arena_bridge, and the pure-Python parts of argus-atlas and
  structlog from this environment (so the browser runs the versions the tests ran);
- the regions (arena/regions.json): per region a manifest (regions/<id>.json, from arena/world/<id>.json), its mist
  (fog-<id>.webp) and, with --tiles, the painted tiles from the region's `tiles` export as plain XYZ folders of WebP
  q88 files (tiles/<id>/<style>/<z>/<x>/<y>.webp; tiles_webp.py, cached in arena/.cache/tiles), with the block's
  open sea painted in (sea_fill.py). Plain files on purpose: Pages doesn't serve HTTP range requests (no PMTiles).
  A region without its tiles here gets the placeholder basemap;
- _headers with the CSP and cache rules for Cloudflare Pages.

Needs the project venv (`make install`) and `npm ci` in arena/.
"""

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent
SRC = HERE / "src"
DH = ROOT / "site" / "vendor" / "dh"
PYODIDE = HERE / "node_modules" / "pyodide"
CACHE = HERE / ".cache" / "wheels"
PYODIDE_CORE = ("pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json")
PACKAGES = ("h3", "pydantic")  # loadPackage() pulls their dependencies from the lock file
TILE_QUALITY = 88  # WebP quality for the painted tiles (DESIGN.md: q80-q92 and AVIF compared)
DH_FILES = ("dh-theme.js", "dh-tokens.css", "dh-strip.css", "dh-badge.css")
# argus-atlas parts the browser never imports (servers, CLI, model backends, templates).
ATLAS_SKIP = ("server", "backends", "templates", "cli.py", "__pycache__")

MAPLIBRE = "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl"
BASEMAP = "https://tiles.openfreemap.org"


# -- page checks ---------------------------------------------------------------------------------


def _strip_markup(html: str) -> str:
    return re.sub(r">\s+<", "><", re.sub(r"<!--.*?-->", "", html, flags=re.S)).strip()


def check_page(html: str) -> None:
    """The family strip is pasted unedited, <html> marks Realms current and picks the "runs in your browser" label
    suffix (dh-shared 1.2.4 data-dh-label), and there is no inline script or style."""
    if _strip_markup((DH / "dh-strip.html").read_text()) not in _strip_markup(html):
        sys.exit("arena/src/index.html: the strip differs from site/vendor/dh/dh-strip.html; paste it again, unedited")
    if not re.search(r'<html\b[^>]*\sdata-dh-site="realms"', html):
        sys.exit('arena/src/index.html: <html> needs data-dh-site="realms"')
    if not re.search(r'<html\b[^>]*\sdata-dh-label="browser"', html):
        sys.exit('arena/src/index.html: <html> needs data-dh-label="browser" (strip label: "runs in your browser")')
    if re.search(r"<script\b(?![^>]*\bsrc=)[^>]*>", html, re.I):
        sys.exit("arena/src/index.html has an inline <script>: the CSP allows none")
    if re.search(r"<style\b|\sstyle=", html, re.I):
        sys.exit("arena/src/index.html has inline styles: put them in style.css (the CSP allows none)")


def headers() -> str:
    """Cloudflare Pages _headers. 'wasm-unsafe-eval' lets Pyodide compile its WebAssembly (it allows no JS eval);
    the game runs in a same-origin module worker, MapLibre's tile worker from a blob: URL."""
    csp = "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' 'wasm-unsafe-eval' {MAPLIBRE}.js",
            f"style-src 'self' {MAPLIBRE}.css",
            f"img-src 'self' data: blob: {BASEMAP}",
            f"connect-src 'self' {BASEMAP}",
            "worker-src 'self' blob:",
            "child-src 'self' blob:",
            "font-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        ]
    )
    return f"""/*
  Content-Security-Policy: {csp}
  X-Frame-Options: DENY
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: camera=(), geolocation=(), microphone=()
  Cross-Origin-Opener-Policy: same-origin
  Strict-Transport-Security: max-age=31536000
/pyodide/*
  Cache-Control: public, max-age=31536000, immutable
/py/*
  Cache-Control: public, max-age=31536000, immutable
/tiles/*
  Cache-Control: public, max-age=86400, stale-while-revalidate=604800
/arena.json
  Cache-Control: no-cache
/regions.json
  Cache-Control: no-cache
/regions/*
  Cache-Control: no-cache
/
  Cache-Control: no-cache
"""


# -- Pyodide and wheels ----------------------------------------------------------------------------


def _wheels(lock: dict) -> list[dict]:
    """PACKAGES and everything they depend on, from Pyodide's lock file."""
    pkgs, want, seen = lock["packages"], list(PACKAGES), set()
    while want:
        name = want.pop().lower().replace("_", "-")
        if name in seen:
            continue
        seen.add(name)
        want += pkgs[name].get("depends", [])
    return [pkgs[n] for n in sorted(seen)]


def copy_pyodide(dest: Path) -> list[str]:
    if not PYODIDE.exists():
        sys.exit("arena/node_modules/pyodide is missing: run `npm ci` in arena/")
    version = json.loads((PYODIDE / "package.json").read_text())["version"]
    dest.mkdir(parents=True)
    for f in PYODIDE_CORE:
        shutil.copy2(PYODIDE / f, dest / f)
    lock = json.loads((PYODIDE / "pyodide-lock.json").read_text())
    CACHE.mkdir(parents=True, exist_ok=True)
    for pkg in _wheels(lock):
        cached = CACHE / pkg["file_name"]
        if not cached.exists() or hashlib.sha256(cached.read_bytes()).hexdigest() != pkg["sha256"]:
            url = f"https://cdn.jsdelivr.net/pyodide/v{version}/full/{pkg['file_name']}"
            print(f"  fetching {pkg['file_name']}")
            data = urllib.request.urlopen(url, timeout=60).read()  # noqa: S310 - fixed https URL
            if hashlib.sha256(data).hexdigest() != pkg["sha256"]:
                sys.exit(f"{pkg['file_name']}: sha256 does not match pyodide-lock.json")
            cached.write_bytes(data)
        shutil.copy2(cached, dest / pkg["file_name"])
    return list(PACKAGES)


# -- the game's Python -----------------------------------------------------------------------------


def _package_dir(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        sys.exit(f"{name} is not installed here: run `make install` and build with the project venv")
    return Path(spec.origin).parent


def _add_tree(z: zipfile.ZipFile, src: Path, arc: str, skip: tuple[str, ...] = ()) -> None:
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if p.is_dir() or any(part in skip for part in rel.parts) or p.suffix not in (".py", ".sql", ".json"):
            continue
        z.write(p, f"{arc}/{rel.as_posix()}")


def load_index() -> list[dict]:
    return json.loads((HERE / "regions.json").read_text())["regions"]


def python_zip(dest: Path, regions: list[dict]) -> str:
    """Zip what the worker unpacks next to Pyodide; returns its file name (content-hashed). The regions go in as
    data/regions.json with the committed world manifests (never dist's, whose styles depend on the tiles), so the
    browser and the CPython reference build the same worlds."""
    tmp = dest / "realms.zip"
    dest.mkdir(parents=True)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        _add_tree(z, ROOT / "src" / "atlas_realms", "atlas_realms", ("__pycache__", "_version.py"))
        _add_tree(z, _package_dir("argus_atlas"), "argus_atlas", ATLAS_SKIP)
        _add_tree(z, _package_dir("structlog"), "structlog", ("__pycache__",))
        z.write(HERE / "py" / "arena_bridge.py", "arena_bridge.py")
        z.write(HERE / "py" / "parity_scenario.py", "parity_scenario.py")
        index = []
        for r in regions:
            outline = Path(r["outline"]).name
            z.write(ROOT / r["outline"], f"data/{outline}")
            z.write(ROOT / r["manifest"], f"data/world/{r['id']}.json")
            index.append({**_public(r), "manifest": f"world/{r['id']}.json", "outline": outline})
        z.writestr("data/regions.json", json.dumps({"regions": index}))
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()[:12]
    name = f"realms-{digest}.zip"
    tmp.rename(dest / name)
    return name


# -- tiles -----------------------------------------------------------------------------------------


def _public(r: dict) -> dict:
    """What the page and the worker are told about a region (no build paths)."""
    return {k: r[k] for k in ("id", "title", "place", "spawns")}


def region_tiles(r: dict, use_tiles: bool) -> Path | None:
    """The region's exported tiles, if the build uses tiles and they are here."""
    tiles = ROOT / r["tiles"]
    return tiles if use_tiles and (tiles / "manifest.json").exists() else None


def world_manifest(r: dict, tiles: Path | None) -> dict:
    m = json.loads((ROOT / r["manifest"]).read_text())
    m.pop("_comment", None)
    m["region"] = _public(r)
    if tiles is None:
        m["styles"], m["placeholder"] = [], True
    else:
        have = {s["name"] for s in json.loads((tiles / "manifest.json").read_text())["styles"]}
        m["styles"] = [s for s in m["styles"] if s["name"] in have and (tiles / s["name"]).is_dir()]
        m["placeholder"] = not m["styles"]
    base = f"tiles/{r['id']}"
    m["tile_url"] = base + "/{style}/{z}/{x}/{y}.webp"  # plain files: Pages serves no range requests
    m["tile_format"] = f"webp q{TILE_QUALITY}"
    m["labels_url"] = f"{base}/labels.geojson" if tiles and (tiles / "labels.geojson").exists() else None
    return m


def copy_tiles(tiles: Path, dest: Path, manifest: dict, outline: Path) -> tuple[int, int]:
    """An XYZ folder of WebP q88 tiles per style (indistinguishable from the PNGs at device scale 2 and ~10x smaller,
    see DESIGN.md), with the block's open-sea chunks painted in (sea_fill.py), built once per set of source tiles and
    cached. Returns the source tile count and the bytes written."""
    from sea_fill import VERSION as FILL_VERSION
    from sea_fill import fill
    from sea_fog import sea_sample
    from tiles_webp import export

    from atlas_realms.seed import sea_chunks

    baked = {tuple(c) for c in manifest["ready_chunks"]}
    sea = sea_chunks(baked, outline, manifest["chunk_zoom"])
    dest.mkdir(parents=True, exist_ok=True)
    n = size = 0
    for s in manifest["styles"]:
        src = sorted((tiles / s["name"]).rglob("*.png"))
        key = hashlib.sha256(
            f"xyz:webp{TILE_QUALITY}:fill{FILL_VERSION}:{sorted(sea)}".encode()
            + b"".join(f"{p}:{p.stat().st_size}:{p.stat().st_mtime_ns}".encode() for p in src)
        ).hexdigest()[:16]
        cached = HERE / ".cache" / "tiles" / f"{s['name']}-{key}"
        if not (cached / "done").exists():
            root = tiles
            got = sea_sample(tiles, s["name"], manifest)
            if got and sea:
                root = HERE / ".cache" / "filled"
                r = fill(tiles, s["name"], manifest, sea, got[0], root)
                print(f"  painted {r['sea_chunks']} open-sea chunks into {s['name']} ({r['tiles_written']} tiles)")
            print(f"  encoding {s['name']} ({len(src)} source tiles, WebP q{TILE_QUALITY})")
            if cached.exists():
                shutil.rmtree(cached)
            export(root, s["name"], cached, TILE_QUALITY)
            (cached / "done").write_text("")
        shutil.copytree(cached, dest / s["name"], ignore=shutil.ignore_patterns("done"))
        n += len(src)
        size += sum(p.stat().st_size for p in (dest / s["name"]).rglob("*.webp"))
    if manifest["labels_url"]:
        shutil.copy2(tiles / "labels.geojson", dest / "labels.geojson")
    return n, size


def add_fog(out: Path, manifest: dict) -> None:
    """The mist outside the region's baked block, and the pan limits it covers (sea_fog.py)."""
    from sea_fog import fog_image

    name = f"fog-{manifest['region']['id']}.webp"
    image, limits, corners = fog_image(manifest["bounds"])
    (out / name).write_bytes(image)
    manifest["fog"] = {"image": name, "coordinates": corners, "limits": limits}


def add_sea(tiles: Path, dest: Path, manifest: dict, prefix: str) -> None:
    """Each style's own sea (colour and texture) for the block's unbaked, all-sea chunks (sea_fog.py)."""
    from sea_fog import sea_sample

    for s in manifest["styles"]:
        got = sea_sample(tiles, s["name"], manifest)
        if got is None:
            continue
        s["sea"], texture = got
        (dest / f"sea-{s['name']}.png").write_bytes(texture)
        s["sea_texture"] = f"{prefix}/sea-{s['name']}.png"
        print(f"  sea for {s['name']}: {s['sea']} ({len(texture) / 1024:.0f} KiB texture)")


# -- main ------------------------------------------------------------------------------------------


def build(out: Path, use_tiles: bool) -> None:
    html = (SRC / "index.html").read_text()
    check_page(html)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for p in SRC.iterdir():
        shutil.copy2(p, out / p.name)
    (out / "vendor" / "dh").mkdir(parents=True)
    for f in DH_FILES:
        shutil.copy2(DH / f, out / "vendor" / "dh" / f)
    for f in ("favicon.ico", "icon.svg", "apple-icon.png"):  # the argus-atlas favicon set, as site/ uses it
        shutil.copy2(ROOT / "site" / f, out / f)
    regions = load_index()
    packages = copy_pyodide(out / "pyodide")
    py = python_zip(out / "py", regions)
    # Everything the worker downloads before Python starts, with sizes, so the loading bar shows real progress.
    preload = [f"pyodide/{f}" for f in PYODIDE_CORE] + sorted(
        f"pyodide/{w.name}" for w in (out / "pyodide").glob("*.whl")
    )
    preload.append(f"py/{py}")
    (out / "arena.json").write_text(
        json.dumps(
            {
                "python": f"py/{py}",
                "packages": packages,
                "preload": [{"url": u, "bytes": (out / u).stat().st_size} for u in preload],
            }
        )
    )
    (out / "_headers").write_text(headers())
    index = []
    (out / "regions").mkdir()
    for r in regions:
        tiles = region_tiles(r, use_tiles)
        manifest = world_manifest(r, tiles)
        add_fog(out, manifest)
        if tiles is not None and not manifest["placeholder"]:
            prefix = f"tiles/{r['id']}"
            n, size = copy_tiles(tiles, out / prefix, manifest, ROOT / r["outline"])
            add_sea(tiles, out / prefix, manifest, prefix)
            names = ", ".join(s["name"] for s in manifest["styles"])
            print(f"  {r['id']}: {n} source tiles -> {size / 2**20:.1f} MiB of WebP tiles ({names})")
        else:
            print(f"  {r['id']}: no tiles (placeholder basemap)")
        # After the tiles: they add each style's sea.
        (out / "regions" / f"{r['id']}.json").write_text(json.dumps(manifest, indent=1))
        index.append(
            {
                **_public(r),
                "manifest": f"regions/{r['id']}.json",
                "bounds": manifest["bounds"],
                "styles": [s["name"] for s in manifest["styles"]],
                "placeholder": manifest["placeholder"],
            }
        )
    (out / "regions.json").write_text(json.dumps({"default": regions[0]["id"], "regions": index}, indent=1))
    files = [p for p in out.rglob("*") if p.is_file()]
    biggest = max(files, key=lambda p: p.stat().st_size)
    total = sum(p.stat().st_size for p in files)
    print(f"arena/dist: {len(files)} files, {total / 2**20:.1f} MiB; largest {biggest.relative_to(out)} "
          f"{biggest.stat().st_size / 2**20:.1f} MiB")  # fmt: skip
    check_pages_limits(out, files)


def check_pages_limits(out: Path, files: list[Path]) -> None:
    """Cloudflare Pages: at most 25 MiB per file and 20,000 files per deployment. And plain files only: Pages doesn't
    answer range requests, so nothing may need them (no .pmtiles)."""
    from tiles_webp import PAGES_FILES, PAGES_MAX

    big = [p for p in files if p.stat().st_size > PAGES_MAX]
    if big:
        sys.exit(f"over the Pages 25 MiB per-file limit: {', '.join(str(p.relative_to(out)) for p in big)}")
    if len(files) >= PAGES_FILES:
        sys.exit(f"{len(files)} files: over the Pages limit of {PAGES_FILES:,} per deployment")
    ranged = [p for p in files if p.suffix == ".pmtiles"]
    if ranged:
        sys.exit(
            f"range-request archives can't be served from Pages: {', '.join(str(p.relative_to(out)) for p in ranged)}"
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tiles", action="store_true", help="paint each region from its `tiles` export where present")
    p.add_argument("--out", type=Path, default=HERE / "dist")
    p.add_argument("--check", action="store_true", help="only check the page and render the headers")
    a = p.parse_args()
    if a.check:
        check_page((SRC / "index.html").read_text())
        assert "wasm-unsafe-eval" in headers()
        print("arena/: strip, marker, no inline scripts or styles, CSP ok")
        return
    build(a.out, a.tiles)


if __name__ == "__main__":
    main()
