"""Build the static map demo (realms.dragonhound.dev) from exported tiles.

    python scripts/build_static.py --tiles var/tiles-areas --out dist/static

Copies `site/` and each area's exported XYZ tiles (`python -m atlas_realms.tiles <maps>/areas/<area>
var/tiles-areas/<area>`, see `make static`) into one directory any static host can serve, writes
`tiles/areas.json` for the area switcher, renders `og.jpg` from the tiles, and checks the Cloudflare Pages
limits (25 MiB per file, 20,000 files). Nothing here runs the game: the solo arena needs its server.
"""

import argparse
import base64
import hashlib
import json
import math
import re
import shutil
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MAX_FILE = 25 * 1024 * 1024
MAX_FILES = 20_000
OG_W, OG_H = 1200, 630

# Cloudflare Pages: real 404s for missing tiles (without a 404.html it serves index.html, which MapLibre
# would try to decode as a PNG), long cache on tiles, short on the page and manifest.
HEADERS = """/tiles/*
  Cache-Control: public, max-age=86400, stale-while-revalidate=604800
  Access-Control-Allow-Origin: *
/tiles/areas.json
  Cache-Control: public, max-age=300
/
  Cache-Control: public, max-age=300
"""

# MapLibre is pinned on cdnjs with SRI in site/index.html; the CSP allows exactly those two files. The basemap
# style, sprites, glyphs and vector tiles all come from tiles.openfreemap.org, and MapLibre runs its worker from a
# blob: URL. No Trusted Types: MapLibre's attribution control writes innerHTML.
MAPLIBRE = "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl"
BASEMAP = "https://tiles.openfreemap.org"
INLINE_STYLE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.S | re.I)
INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


# The demo's areas, in switcher order. `frame` is the island's outline extent (Natural Earth 10m, rounded outward),
# which the page fits on load; the tiles' own bounds include the chunks' sea around it.
AREAS = [
    {"id": "iceland", "label": "Iceland", "frame": [-24.55, 63.39, -13.49, 66.57]},
    {"id": "reunion", "label": "Réunion", "frame": [55.22, -21.38, 55.86, -20.86]},
]
ATTRIBUTION = (
    'Painted by <a href="https://github.com/smk762/argus-atlas">argus-atlas</a> from '
    '<a href="https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model">Copernicus DEM GLO-30</a> '
    "© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the EU and ESA, "
    'and <a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>'
)


def _sha256(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def security_headers(html: str) -> str:
    """Response headers for every path: a CSP that hashes the page's inline <style>, plus the companion headers."""
    if INLINE_SCRIPT.search(html):
        sys.exit("site/index.html has an inline <script>: move it into site/app.js (the CSP allows no inline scripts)")
    styles = " ".join(_sha256(body) for body in INLINE_STYLE.findall(html))
    csp = "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' {MAPLIBRE}.js",
            f"style-src 'self' {MAPLIBRE}.css {styles}".strip(),
            f"img-src 'self' data: blob: {BASEMAP}",
            f"connect-src 'self' {BASEMAP}",
            "worker-src blob:",
            "child-src blob:",
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
"""


# The vendored Dragonhound shared layer (site/vendor/dh, from dragonhound-site's scripts/sync-shared.mjs). Only the
# files the page loads are published (dh-theme.js); the stylesheets are inlined by inline_shared_css(), and the rest
# (README, manifest, the sync script, the strip source, its types and pasted HTML) stay in git.
SHARED_SKIP = ("README.md", "manifest.json", "sync-shared.mjs", "favicons", "dh-mark.svg")
SHARED_SKIP += ("dh-strip.html", "dh-strip.mjs", "dh-strip.d.mts")
SHARED_SKIP += ("dh-tokens.css", "dh-strip.css", "dh-badge.css")
SHARED_LINK = re.compile(r'<link rel="stylesheet" href="vendor/dh/(dh-[a-z]+\.css)">')


def inline_shared_css(html: str) -> str:
    """Swap the page's <link>s to the vendored stylesheets for <style> blocks with the same content: three fewer
    render-blocking requests on a page that is mostly a map. The CSP hashes them like the page's own <style>."""
    return SHARED_LINK.sub(lambda m: f"<style>{(ROOT / 'site' / 'vendor' / 'dh' / m[1]).read_text()}</style>", html)


SKIP_LINK = '<a class="skip" href="#panel">Skip to the map controls</a>'


def _strip_markup(html: str) -> str:
    return re.sub(r">\s+<", "><", re.sub(r"<!--.*?-->", "", html, flags=re.S)).strip()


def check_strip(html: str) -> None:
    """site/index.html carries dh-strip.html inline (no JS, no fetch), unedited, and marks Realms current with
    data-dh-site="realms" on <html> (dh-strip.css shows it, dh-theme.js adds aria-current). Fail the build if the
    pasted strip drifted from the vendored copy (generated from dh-strip.mjs) or the marker is missing."""
    want = SKIP_LINK + _strip_markup((ROOT / "site" / "vendor" / "dh" / "dh-strip.html").read_text())
    if want not in _strip_markup(html):
        sys.exit(
            "site/index.html: the demo strip differs from site/vendor/dh/dh-strip.html or does not follow the skip "
            "link; paste it again, unedited, right after the skip link"
        )
    if not re.search(r'<html\b[^>]*\sdata-dh-site="realms"', html):
        sys.exit('site/index.html: <html> needs data-dh-site="realms" to mark Realms current in the strip')


def tile_xy(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = 2**z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def render_og(tiles: Path, style: str, bounds: list[float], out: Path, z: int = 9) -> None:
    """Stitch one zoom of `style` and fit the whole of `bounds` into 1200x630, widening the shorter side.

    Where no tile is painted (all-sea chunks are not baked) the ground is the painting's own median edge colour,
    which is its sea, so the card shows no blocks of another colour."""
    w, s, e, n = bounds
    fx0, fy0 = tile_xy(w, n, z)
    fx1, fy1 = tile_xy(e, s, z)
    x0, y0, x1, y1 = int(fx0), int(fy0), int(fx1), int(fy1)
    canvas = Image.new("RGBA", ((x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256), (0, 0, 0, 0))
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            p = tiles / style / str(z) / str(x) / f"{y}.png"
            if p.exists():
                with Image.open(p) as im:
                    canvas.paste(im.convert("RGBA"), ((x - x0) * 256, (y - y0) * 256))
    px = canvas.load()
    edge = [px[i, j] for i in range(0, canvas.width, 4) for j in (0, canvas.height - 1)]
    edge += [px[i, j] for j in range(0, canvas.height, 4) for i in (0, canvas.width - 1)]
    edge = [p[:3] for p in edge if p[3] == 255] or [(226, 214, 186)]
    ground = tuple(sorted(c[k] for c in edge)[len(edge) // 2] for k in range(3))
    # The frame in canvas pixels, grown by 3% and then to the card's aspect ratio around its centre.
    cx, cy = ((fx0 + fx1) / 2 - x0) * 256, ((fy0 + fy1) / 2 - y0) * 256
    fw, fh = (fx1 - fx0) * 256 * 1.03, (fy1 - fy0) * 256 * 1.03
    fw, fh = max(fw, fh * OG_W / OG_H), max(fh, fw * OG_H / OG_W)
    left, top = round(cx - fw / 2), round(cy - fh / 2)
    card = Image.new("RGB", (round(fw), round(fh)), ground)
    card.paste(canvas, (-left, -top), canvas)
    card.resize((OG_W, OG_H), Image.LANCZOS).save(out, "JPEG", quality=85, optimize=True)


def copy_mask(area: str, dest: Path, have: list[str]) -> str | None:
    """Copy the area's sea mask (data/masks, made by scripts/sea_mask.py) next to its tiles, named by its content
    hash so a new mask is fetched at once. Skipped, with a warning, if it was made for a different set of tiles."""
    src = ROOT / "data" / "masks"
    meta, png = src / f"{area}.json", src / f"{area}.png"
    if not (meta.exists() and png.exists()):
        print(f"warning: no sea mask for {area}; its painted chunks show whole (uv run scripts/sea_mask.py)")
        return None
    m = json.loads(meta.read_text())
    if m.get("tiles") != len(have) or not set(m["clear"] + m["full"]) <= set(have):
        print(f"warning: the sea mask for {area} was made for other tiles; rerun scripts/sea_mask.py")
        return None
    tag = hashlib.sha256(meta.read_bytes() + png.read_bytes()).hexdigest()[:10]
    shutil.copy2(meta, dest / f"mask-{tag}.json")
    shutil.copy2(png, dest / f"mask-{tag}.png")
    return f"tiles/{area}/mask-{tag}"


def check_limits(out: Path) -> tuple[int, int]:
    files = [p for p in out.rglob("*") if p.is_file()]
    big = [p for p in files if p.stat().st_size > MAX_FILE]
    if big:
        sys.exit(f"over 25 MiB (needs object storage): {', '.join(str(p) for p in big)}")
    if len(files) > MAX_FILES:
        sys.exit(f"{len(files)} files, over the Pages limit of {MAX_FILES}")
    return len(files), sum(p.stat().st_size for p in files)


def build(tiles: Path, out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    check_strip((ROOT / "site" / "index.html").read_text())
    shutil.copytree(
        ROOT / "site", out, ignore=lambda d, names: [n for n in names if d.endswith("dh") and n in SHARED_SKIP]
    )
    listing = []
    for area in AREAS:
        src = tiles / area["id"]
        manifest_path = src / "manifest.json"
        if not manifest_path.exists():
            sys.exit(f"{manifest_path} not found: export the area's tiles first (make static)")
        manifest = json.loads(manifest_path.read_text())
        public = [st["name"] for st in manifest["styles"] if not st.get("patron_only")]
        for name in public:
            shutil.copytree(src / name, out / "tiles" / area["id"] / name)
            # The block is not a rectangle of land: list the tiles that exist so the page asks for no others.
            have = sorted(str(t.relative_to(src / name).with_suffix("")) for t in (src / name).rglob("*.png"))
            (out / "tiles" / area["id"] / name / "index.json").write_text(json.dumps(have, separators=(",", ":")))
        # Place names: the export merges every chunk's `argus-atlas labels` output into one GeoJSON per area.
        labels = None
        if manifest.get("labels_url"):
            shutil.copy2(src / "labels.geojson", out / "tiles" / area["id"] / "labels.geojson")
            labels = f"tiles/{area['id']}/labels.geojson"
        mask = copy_mask(area["id"], out / "tiles" / area["id"], have)
        listing.append(
            {
                **area,
                "styles": public,
                "minzoom": manifest["minzoom"],
                "chunk_zoom": manifest["chunk_zoom"],
                "maxzoom": manifest["maxzoom"],
                "bounds": manifest["bounds"],
                "labels": labels,
                "mask": mask,
                # Cache-busting key for the tile tree (see site/app.js): changes with every export.
                "version": manifest["generated_at"],
                "attribution": ATTRIBUTION,
            }
        )
    (out / "tiles" / "areas.json").write_text(json.dumps(listing, indent=1, ensure_ascii=False))
    Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(out / "tiles" / "empty.png", optimize=True)
    (out / "404.html").write_text("<!doctype html><title>Not found</title><p>Not found.</p>\n")
    page = inline_shared_css((ROOT / "site" / "index.html").read_text())
    (out / "index.html").write_text(page)
    (out / "_headers").write_text(HEADERS + security_headers(page))
    first = listing[0]
    render_og(tiles / first["id"], first["styles"][0], first["frame"], out / "og.jpg", z=first["chunk_zoom"] + 1)
    count, size = check_limits(out)
    print(f"{out}: {count} files, {size / 1024 / 1024:.1f} MiB, areas {', '.join(a['id'] for a in listing)}")


def check() -> None:
    """The build's page checks without the tiles (they are baked on a GPU host and are not in git)."""
    html = (ROOT / "site" / "index.html").read_text()
    check_strip(html)
    page = inline_shared_css(html)
    if SHARED_LINK.search(page):
        sys.exit("site/index.html: a vendored stylesheet link was not inlined")
    security_headers(page)
    print("site/: strip, marker, inline styles and CSP ok")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tiles", type=Path, default=ROOT / "var" / "tiles-areas", help="Exported tiles, one dir per area")
    p.add_argument("--out", type=Path, default=ROOT / "dist" / "static")
    p.add_argument(
        "--check",
        action="store_true",
        help="Only check site/ (pasted strip, <html> marker, no inline scripts, CSP renders); needs no tiles. For CI.",
    )
    a = p.parse_args()
    if a.check:
        check()
        return
    build(a.tiles, a.out)


if __name__ == "__main__":
    main()
