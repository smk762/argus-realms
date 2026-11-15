"""Build the browser arena into arena/dist: a static site, no server.

    uv run --no-sync python arena/build.py                      # placeholder basemap (no painted tiles)
    uv run --no-sync python arena/build.py --tiles var/tiles    # with the Cook Strait tiles from `make tiles`
    uv run --no-sync python arena/build.py --check              # page checks only (CI): strip, marker, CSP

What goes in:
- the page (arena/src), with the Dragonhound shared layer copied from site/vendor/dh (one vendored copy in git);
- Pyodide (from node_modules/pyodide, pinned in package.json) and the wheels the rules need (h3, pydantic and their
  dependencies), downloaded once into arena/.cache and checked against the sha256 in Pyodide's lock file;
- the game's Python as one zip: atlas_realms from src/, arena_bridge, and the pure-Python parts of argus-atlas and
  structlog from this environment (so the browser runs the versions the tests ran);
- the world manifest (arena/world/cook-strait.json) and, with --tiles, the painted tiles packed into one PMTiles file
  per style as WebP q88 (tiles_pack.py; cached in arena/.cache/tiles), read by MapLibre with the pmtiles protocol;
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


# The strip's label suffix ("recorded output") is fixed in dh-shared 1.2.2; the arena says what it is instead.
# Only this span may differ from the vendored strip.
LABEL_MORE = re.compile(r'(<span class="dh-strip-more">)[^<]*(</span>)')
ARENA_LABEL_MORE = " · runs in your browser"


def check_page(html: str) -> None:
    """The family strip is pasted unedited (bar the label suffix), <html> marks Realms current, and there is no inline
    script or style."""
    want = LABEL_MORE.sub(lambda m: m[1] + ARENA_LABEL_MORE + m[2], (DH / "dh-strip.html").read_text())
    if _strip_markup(want) not in _strip_markup(html):
        sys.exit("arena/src/index.html: the strip differs from site/vendor/dh/dh-strip.html; paste it again, unedited")
    if not re.search(r'<html\b[^>]*\sdata-dh-site="realms"', html):
        sys.exit('arena/src/index.html: <html> needs data-dh-site="realms"')
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


def python_zip(dest: Path, manifest: dict) -> str:
    """Zip what the worker unpacks next to Pyodide; returns its file name (content-hashed)."""
    tmp = dest / "realms.zip"
    dest.mkdir(parents=True)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        _add_tree(z, ROOT / "src" / "atlas_realms", "atlas_realms", ("__pycache__", "_version.py"))
        _add_tree(z, _package_dir("argus_atlas"), "argus_atlas", ATLAS_SKIP)
        _add_tree(z, _package_dir("structlog"), "structlog", ("__pycache__",))
        z.write(HERE / "py" / "arena_bridge.py", "arena_bridge.py")
        z.write(HERE / "py" / "parity_scenario.py", "parity_scenario.py")
        z.write(ROOT / "data" / "nz_mainland.geojson", "data/nz_mainland.geojson")
        z.writestr("data/manifest.json", json.dumps(manifest))
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()[:12]
    name = f"realms-{digest}.zip"
    tmp.rename(dest / name)
    return name


# -- tiles -----------------------------------------------------------------------------------------


def world_manifest(tiles: Path | None) -> dict:
    m = json.loads((HERE / "world" / "cook-strait.json").read_text())
    m.pop("_comment", None)
    if tiles is None:
        m["styles"], m["placeholder"] = [], True
    else:
        have = {s["name"] for s in json.loads((tiles / "manifest.json").read_text())["styles"]}
        m["styles"] = [s for s in m["styles"] if s["name"] in have and (tiles / s["name"]).is_dir()]
        m["placeholder"] = not m["styles"]
    m["tile_url"] = None  # XYZ PNGs are no longer served; see pmtiles_url
    m["pmtiles_url"] = "tiles/{style}.pmtiles"
    m["tile_format"] = f"webp q{TILE_QUALITY}"
    m["labels_url"] = "tiles/labels.geojson" if tiles and (tiles / "labels.geojson").exists() else None
    return m


def copy_tiles(tiles: Path, dest: Path, manifest: dict) -> tuple[int, int]:
    """One PMTiles per style (WebP q88: indistinguishable from the PNGs at device scale 2 and ~10x smaller, see
    DESIGN.md), packed once per set of source tiles and cached."""
    from tiles_pack import PAGES_MAX, pack

    dest.mkdir(parents=True, exist_ok=True)
    n = size = 0
    for s in manifest["styles"]:
        src = sorted((tiles / s["name"]).rglob("*.png"))
        key = hashlib.sha256(
            f"webp{TILE_QUALITY}".encode()
            + b"".join(f"{p}:{p.stat().st_size}:{p.stat().st_mtime_ns}".encode() for p in src)
        ).hexdigest()[:16]
        cached = HERE / ".cache" / "tiles" / f"{s['name']}-{key}.pmtiles"
        if not cached.exists():
            print(f"  packing {s['name']} ({len(src)} tiles, WebP q{TILE_QUALITY})")
            pack(tiles, s["name"], cached, "webp", TILE_QUALITY, manifest["bounds"])
        if cached.stat().st_size > PAGES_MAX:
            sys.exit(f"{cached.name} is over the Pages 25 MiB limit")
        shutil.copy2(cached, dest / f"{s['name']}.pmtiles")
        n, size = n + len(src), size + cached.stat().st_size
    if manifest["labels_url"]:
        shutil.copy2(tiles / "labels.geojson", dest / "labels.geojson")
    return n, size


# -- main ------------------------------------------------------------------------------------------


def build(out: Path, tiles: Path | None) -> None:
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
    # The pmtiles protocol for MapLibre (pinned in package.json), served from 'self'.
    shutil.copy2(HERE / "node_modules" / "pmtiles" / "dist" / "pmtiles.js", out / "vendor" / "pmtiles.js")
    for f in ("favicon.ico", "icon.svg", "apple-icon.png"):  # the argus-atlas favicon set, as site/ uses it
        shutil.copy2(ROOT / "site" / f, out / f)
    manifest = world_manifest(tiles)
    packages = copy_pyodide(out / "pyodide")
    py = python_zip(out / "py", manifest)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
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
    if tiles is not None and not manifest["placeholder"]:
        n, size = copy_tiles(tiles, out / "tiles", manifest)
        print(
            f"  tiles: {n} source tiles -> {size / 2**20:.1f} MiB of PMTiles ({', '.join(s['name'] for s in manifest['styles'])})"
        )
    else:
        print("  tiles: none (placeholder basemap)")
    files = [p for p in out.rglob("*") if p.is_file()]
    biggest = max(files, key=lambda p: p.stat().st_size)
    total = sum(p.stat().st_size for p in files)
    print(f"arena/dist: {len(files)} files, {total / 2**20:.1f} MiB; largest {biggest.relative_to(out)} "
          f"{biggest.stat().st_size / 2**20:.1f} MiB")  # fmt: skip
    if biggest.stat().st_size > 25 * 2**20 or len(files) > 20_000:
        sys.exit("over the Cloudflare Pages limits (25 MiB per file, 20,000 files)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tiles", type=Path, help="exported tiles (make tiles): <style>/<z>/<x>/<y>.png + manifest.json")
    p.add_argument("--out", type=Path, default=HERE / "dist")
    p.add_argument("--check", action="store_true", help="only check the page and render the headers")
    a = p.parse_args()
    if a.check:
        check_page((SRC / "index.html").read_text())
        assert "wasm-unsafe-eval" in headers()
        print("arena/: strip, marker, no inline scripts or styles, CSP ok")
        return
    if a.tiles is not None and not (a.tiles / "manifest.json").exists():
        sys.exit(f"{a.tiles}/manifest.json not found (make tiles exports it)")
    build(a.out, a.tiles)


if __name__ == "__main__":
    main()
