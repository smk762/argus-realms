"""Pack exported XYZ tiles (`make tiles`: <style>/<z>/<x>/<y>.png) into one PMTiles file per style, re-encoded.

    uv run --no-sync python arena/tiles_pack.py var/tiles out/ --format webp --quality 88

One file per style is served from Cloudflare Pages with HTTP range requests (MapLibre + the pmtiles protocol), so it
must stay under Pages' 25 MiB per file. Measured on the Cook Strait export (DESIGN.md): PNG is 60-66 MiB per style,
WebP q88 6-7 MiB. Needs Pillow and pmtiles (the project's `server` extra).
"""

import argparse
import io
import json
import math
from multiprocessing import Pool
from pathlib import Path

FORMATS = {"webp": "WEBP", "avif": "AVIF", "png": "PNG"}
PAGES_MAX = 25 * 2**20


def encode(args: tuple[Path, str, int]) -> bytes:
    from PIL import Image

    path, fmt, quality = args
    if fmt == "png":
        return path.read_bytes()
    im = Image.open(path)
    im = im.convert("RGB") if im.mode == "RGBA" and im.getextrema()[3] == (255, 255) else im  # opaque: drop alpha
    b = io.BytesIO()
    if fmt == "webp":
        im.save(b, "WEBP", quality=quality, method=6)
    else:
        im.save(b, "AVIF", quality=quality, speed=6)
    return b.getvalue()


def pack(tiles: Path, style: str, out: Path, fmt: str = "webp", quality: int = 88, bounds=None) -> dict:
    """Write <out> (a .pmtiles) from tiles/<style>; returns its zooms, tile count and size."""
    from pmtiles.tile import Compression, TileType, zxy_to_tileid
    from pmtiles.writer import Writer

    files = []
    for p in (tiles / style).rglob("*.png"):
        z, x, y = int(p.parts[-3]), int(p.parts[-2]), int(p.stem)
        files.append((zxy_to_tileid(z, x, y), z, p))
    files.sort()
    with Pool() as pool:
        data = pool.map(encode, [(p, fmt, quality) for _, _, p in files], chunksize=8)
    zooms = [z for _, z, _ in files]
    w_, s_, e_, n_ = bounds or (-180, -85, 180, 85)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as fh:
        w = Writer(fh)
        for (tid, _, _), d in zip(files, data, strict=True):
            w.write_tile(tid, d)
        w.finalize(
            {
                "tile_type": getattr(TileType, FORMATS[fmt]),
                "tile_compression": Compression.NONE,  # images are compressed already
                "min_zoom": min(zooms),
                "max_zoom": max(zooms),
                "min_lon_e7": math.floor(w_ * 1e7),
                "min_lat_e7": math.floor(s_ * 1e7),
                "max_lon_e7": math.ceil(e_ * 1e7),
                "max_lat_e7": math.ceil(n_ * 1e7),
                "center_zoom": (min(zooms) + max(zooms)) // 2,
                "center_lon_e7": round((w_ + e_) / 2 * 1e7),
                "center_lat_e7": round((s_ + n_) / 2 * 1e7),
            },
            {"name": f"cook-strait-{style}", "format": fmt, "quality": quality},
        )
    size = out.stat().st_size
    return {"style": style, "tiles": len(files), "minzoom": min(zooms), "maxzoom": max(zooms), "bytes": size}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("tiles", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--format", choices=sorted(FORMATS), default="webp")
    p.add_argument("--quality", type=int, default=88)
    a = p.parse_args()
    manifest = json.loads((a.tiles / "manifest.json").read_text())
    for s in manifest["styles"]:
        r = pack(a.tiles, s["name"], a.out / f"{s['name']}.pmtiles", a.format, a.quality, manifest.get("bounds"))
        flag = "" if r["bytes"] <= PAGES_MAX else "  OVER the Pages 25 MiB limit"
        print(f"{s['name']}: {r['tiles']} tiles, z{r['minzoom']}-{r['maxzoom']}, {r['bytes'] / 2**20:.2f} MiB{flag}")


if __name__ == "__main__":
    main()
