# /// script
# requires-python = ">=3.11"
# dependencies = ["mapbox-vector-tile>=2.1", "numpy", "pillow", "scipy"]
# ///
"""Sea mask for one area of the static demo: where the painted tiles may show over the basemap's ocean.

    uv run scripts/sea_mask.py --tiles dist/static/tiles --area iceland --out data/masks

The painted tiles cover whole chunks, open sea included, so over the basemap's lighter ocean they show as a stepped
block. The page multiplies each painted tile by this mask (alpha): opaque on land and on the sea within KEEP metres
of the coast, so each style keeps its own coastal water, then fading out with a smoothstep by FADE metres. It also
fades out towards the painted block's outer edge, in case land or the coastal band runs into it.

The ocean comes from the basemap itself: the `water` layer (class `ocean`) of the OpenFreeMap vector tiles (OpenMapTiles
schema, data © OpenStreetMap contributors, ODbL), fetched at chunk zoom + 2 and rasterised at that zoom; lakes and
rivers count as land, as they do in the paintings. The mask is written at the chunk zoom, 256 px per chunk (the band
is smooth, so the page scales it up for deeper tiles), as a grey-alpha PNG plus a JSON with its origin and the tiles that are wholly
clear (skipped: the page never fetches them) or wholly opaque (drawn as they are).
"""

import argparse
import hashlib
import io
import json
import math
import os
import time
import urllib.request
from pathlib import Path

import mapbox_vector_tile
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

TILEJSON = "https://tiles.openfreemap.org/planet"
EARTH = 2 * math.pi * 6378137.0  # metres around the equator, Web Mercator


CACHE: Path | None = None


def fetch(url: str) -> bytes:
    if CACHE and url.endswith(".pbf"):
        _, planet, rest = url.partition("/planet/")
        name = rest.replace("/", "_") if planet else hashlib.sha256(url.encode()).hexdigest()[:24] + ".pbf"
        hit = CACHE / name
        if hit.exists():
            return hit.read_bytes()
        data = _fetch(url)
        hit.parent.mkdir(parents=True, exist_ok=True)
        tmp = hit.with_name(f"{hit.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, hit)  # the cache only ever holds whole tiles, even if a run is cut off mid-write
        return data
    return _fetch(url)


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "atlas-realms sea mask"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except OSError:
            if attempt == 3:
                raise
            time.sleep(2**attempt)
    raise AssertionError


def ocean_tile(template: str, z: int, x: int, y: int) -> np.ndarray:
    """1 where the vector tile's ocean polygons cover a pixel (256 x 256), else 0."""
    data = fetch(template.format(z=z, x=x, y=y))
    img = Image.new("L", (256, 256), 0)
    if not data:
        return np.zeros((256, 256), np.uint8)
    layers = mapbox_vector_tile.decode(data, default_options={"y_coord_down": True})
    water = layers.get("water")
    if not water:
        return np.zeros((256, 256), np.uint8)
    k = 256 / water["extent"]
    draw = ImageDraw.Draw(img)
    for f in water["features"]:
        if f["properties"].get("class") != "ocean":
            continue
        g = f["geometry"]
        polys = (
            [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"] if g["type"] == "MultiPolygon" else []
        )
        for rings in polys:
            for i, ring in enumerate(rings):  # exterior, then holes (islands)
                if len(ring) >= 3:
                    draw.polygon([(px * k, py * k) for px, py in ring], fill=0 if i else 1)
    return np.asarray(img, np.uint8)


def smoothstep(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tiles", type=Path, required=True, help="tile tree with areas.json and <area>/<style>/index.json")
    ap.add_argument("--area", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--keep", type=float, default=1000, help="metres of painted sea kept at full strength")
    ap.add_argument("--fade", type=float, default=3000, help="metres from the coast where the painted sea is gone")
    ap.add_argument("--edge", type=float, default=8, help="fade towards the painted block's edge, in mask pixels")
    ap.add_argument("--cache", type=Path, help="keep the fetched vector tiles here for reruns")
    args = ap.parse_args()
    if not args.fade > args.keep >= 0:
        ap.error("--fade must be greater than --keep, and --keep at least 0")
    if not args.edge > 0:
        ap.error("--edge must be greater than 0")
    global CACHE
    CACHE = args.cache

    meta = {a["id"]: a for a in json.loads((args.tiles / "areas.json").read_text())}[args.area]
    cz, styles = meta["chunk_zoom"], meta["styles"]
    keys = json.loads((args.tiles / args.area / styles[0] / "index.json").read_text())
    for s in styles[1:]:
        if sorted(json.loads((args.tiles / args.area / s / "index.json").read_text())) != sorted(keys):
            raise SystemExit(f"{args.area}: styles cover different chunks; the mask assumes one set")
    chunks = {tuple(map(int, k.split("/")[1:])) for k in keys if int(k.split("/")[0]) == cz}
    cx0, cy0 = min(c[0] for c in chunks), min(c[1] for c in chunks)
    cw, ch = max(c[0] for c in chunks) - cx0 + 1, max(c[1] for c in chunks) - cy0 + 1

    # Ocean at chunk zoom + 2 over the chunks' bounding box (each chunk is 4 x 4 vector tiles).
    fz, n = cz + 2, 4
    template = json.loads(fetch(TILEJSON))["tiles"][0]
    ocean = np.ones((ch * n * 256, cw * n * 256), np.uint8)  # outside the painted chunks counts as sea
    for cx, cy in sorted(chunks):
        for dy in range(n):
            for dx in range(n):
                x, y = cx * n + dx, cy * n + dy
                r, c = (y - cy0 * n) * 256, (x - cx0 * n) * 256
                ocean[r : r + 256, c : c + 256] = ocean_tile(template, fz, x, y)
    # Metres per pixel by row (Web Mercator shrinks with latitude), from the row's centre.
    rows = (cy0 * n * 256 + np.arange(ocean.shape[0]) + 0.5) / (256 * 2**fz)
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * rows))))
    mpp = EARTH * np.cos(np.radians(lat)) / (256 * 2**fz)
    dist = ndimage.distance_transform_edt(ocean) * mpp[:, None]  # metres to the nearest land pixel
    alpha = 1 - smoothstep((dist - args.keep) / (args.fade - args.keep))

    # Down to the chunk zoom (box average), then fade towards chunks that are not painted.
    h, w = alpha.shape
    alpha = alpha.reshape(h // n, n, w // n, n).mean(axis=(1, 3))
    painted = np.zeros((ch, cw), bool)
    for cx, cy in chunks:
        painted[cy - cy0, cx - cx0] = True
    painted = np.kron(painted, np.ones((256, 256), bool))
    edge = ndimage.distance_transform_edt(np.pad(painted, 1))[1:-1, 1:-1]  # pad: outside the box is unpainted
    alpha *= smoothstep((edge - 0.5) / args.edge)
    a8 = np.round(alpha * 255).astype(np.uint8)

    mz = cz
    tiles_clear, tiles_full = [], []
    for k in sorted(keys, key=lambda s: tuple(map(int, s.split("/")))):
        z, x, y = map(int, k.split("/"))
        s = 2.0 ** (mz - z) * 256
        r0, c0 = y * s - cy0 * 256, x * s - cx0 * 256
        # One pixel of margin: the page scales the mask up with bilinear filtering.
        rs = slice(max(0, math.floor(r0) - 1), min(a8.shape[0], math.ceil(r0 + s) + 1))
        cs = slice(max(0, math.floor(c0) - 1), min(a8.shape[1], math.ceil(c0 + s) + 1))
        win = a8[rs, cs]
        if win.size == 0 or win.max() == 0:
            tiles_clear.append(k)
        elif win.min() == 255:
            tiles_full.append(k)

    args.out.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    Image.fromarray(np.dstack([np.zeros_like(a8), a8]), "LA").save(buf, "PNG", optimize=True)
    (args.out / f"{args.area}.png").write_bytes(buf.getvalue())
    (args.out / f"{args.area}.json").write_text(
        json.dumps(
            {
                "zoom": mz,
                "x": cx0,
                "y": cy0,
                "keep_m": args.keep,
                "fade_m": args.fade,
                "tiles": len(keys),
                "clear": tiles_clear,
                "full": tiles_full,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    print(
        f"{args.area}: mask {a8.shape[1]}x{a8.shape[0]} at z{mz}, {len(buf.getvalue()) // 1024} KiB; "
        f"{len(tiles_clear)} clear, {len(tiles_full)} full, {len(keys) - len(tiles_clear) - len(tiles_full)} masked tiles"
    )


if __name__ == "__main__":
    main()
