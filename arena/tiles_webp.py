"""Re-encode exported XYZ tiles (`make tiles`: <style>/<z>/<x>/<y>.png) as an XYZ tree of WebP files.

    uv run --no-sync python arena/tiles_webp.py var/tiles-areas/cook-strait out/ --quality 88

The arena serves plain tile folders: one small file per tile, fetched whole. Cloudflare Pages doesn't answer HTTP
range requests on *.pages.dev, and a custom domain only does so for extensions the zone caches, so a single-file
archive read in ranges (PMTiles) fails there. WebP q88 is indistinguishable from the PNGs at device scale 2 and about
10x smaller (DESIGN.md has the comparison). Needs Pillow (the project's `server` extra).
"""

import argparse
import io
import json
from multiprocessing import Pool
from pathlib import Path

PAGES_MAX = 25 * 2**20  # Cloudflare Pages: largest file
PAGES_FILES = 20_000  # Cloudflare Pages: files per deployment


def encode(args: tuple[Path, Path, int]) -> int:
    """Write one tile as WebP; returns its size."""
    from PIL import Image

    src, dest, quality = args
    im = Image.open(src)
    if im.mode == "RGBA" and im.getextrema()[3] == (255, 255):
        im = im.convert("RGB")  # opaque: no alpha channel needed
    b = io.BytesIO()
    im.save(b, "WEBP", quality=quality, method=6)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b.getvalue())
    return len(b.getvalue())


def export(tiles: Path, style: str, out: Path, quality: int = 88) -> dict:
    """tiles/<style>/<z>/<x>/<y>.png -> out/<z>/<x>/<y>.webp; returns the tile count, zooms and bytes.

    Only <z>/<x>/<y>.png counts as a tile: any other PNG under the style (a legend, a preview) is left out."""
    root = tiles / style
    jobs, zooms = [], set()
    for p in sorted(root.rglob("*.png")):
        rel = p.relative_to(root).parts
        if len(rel) != 3 or not all(v.isdigit() for v in (rel[0], rel[1], p.stem)):
            continue
        z, x, y = rel[0], rel[1], p.stem
        jobs.append((p, out / z / x / f"{y}.webp", quality))
        zooms.add(int(z))
    if not jobs:
        raise FileNotFoundError(f"no <z>/<x>/<y>.png tiles under {root}")
    with Pool() as pool:
        sizes = pool.map(encode, jobs, chunksize=8)
    zooms = sorted(zooms)
    return {"style": style, "tiles": len(jobs), "minzoom": zooms[0], "maxzoom": zooms[-1], "bytes": sum(sizes)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("tiles", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--quality", type=int, default=88)
    a = p.parse_args()
    for s in json.loads((a.tiles / "manifest.json").read_text())["styles"]:
        r = export(a.tiles, s["name"], a.out / s["name"], a.quality)
        print(f"{s['name']}: {r['tiles']} tiles, z{r['minzoom']}-{r['maxzoom']}, {r['bytes'] / 2**20:.2f} MiB")


if __name__ == "__main__":
    main()
