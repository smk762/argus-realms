"""What the arena draws where there is no painted tile: fog outside the baked block, painted sea inside it.

The Cook Strait block is 7 x 7 chunks (z10). `bakes/cook-strait.sh` baked the 30 with land and skipped the 19 whose
area holds no land at all (the bake plan's own test against `data/nz_mainland.geojson`). So, within the block, a
missing chunk is open sea, and outside the block nothing is painted:

- `fog_image()` writes one image that covers the map's pan limits (the block plus a margin). It is an opaque dark mist
  outside the block, fading to clear about one hex inside its edge, and the client draws it over everything else.
- `sea_sample()` takes each style's own sea from the painted chunks' edges that face a skipped chunk. It returns a
  median colour, plus a seamless texture from the calmest z12 sea tile, so the gaps are painted sea and not a flat
  flood.

Pillow only (the project's `server` extra), run at build time.
"""

import io
import math
import random
import statistics
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

MARGIN = 0.3  # the pan limits: the block plus this share of its size on every side
FEATHER_KM = 17.0  # the fog's soft edge runs about one H3 res-5 hex (~17 km across) into the block
MIST = ("#0b0d11", "#2b313b")  # the game's fog is #0d0f13; the mist wisps lighten it a little
EDGE_PX = 12  # sea samples: this many pixels along a painted tile's edge that faces open sea


def _merc(lon: float, lat: float) -> tuple[float, float]:
    return lon / 360 + 0.5, 0.5 - math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) / (2 * math.pi)


def _lonlat(x: float, y: float) -> tuple[float, float]:
    return (x - 0.5) * 360, math.degrees(2 * math.atan(math.exp((0.5 - y) * 2 * math.pi)) - math.pi / 2)


def fog_image(bounds: list[float], width: int = 1024, seed: int = 7) -> tuple[bytes, list[float], list[list[float]]]:
    """The mist as WebP (with alpha), the pan limits [w, s, e, n], and the image's corner coordinates for MapLibre
    (top-left, top-right, bottom-right, bottom-left). The image is laid out in Web Mercator, as MapLibre draws it."""
    w, s, e, n = bounds
    (x0, y0), (x1, y1) = _merc(w, n), _merc(e, s)
    dx, dy = (x1 - x0) * MARGIN, (y1 - y0) * MARGIN
    X0, Y0, X1, Y1 = x0 - dx, y0 - dy, x1 + dx, y1 + dy
    height = round(width * (Y1 - Y0) / (X1 - X0))
    px = width / (X1 - X0)  # pixels per mercator unit
    block = [(x0 - X0) * px, (y0 - Y0) * px, (x1 - X0) * px, (y1 - Y0) * px]

    # Opaque outside the block, clear from about one hex inside its edge: a step inset by half the feather, blurred.
    mid_lat = math.radians((s + n) / 2)
    km_per_px = 40075 * math.cos(mid_lat) * (X1 - X0) / width  # one mercator unit spans the Earth's circumference
    feather = FEATHER_KM / km_per_px
    mask = Image.new("L", (width, height), 255)
    inset = feather * 0.5
    ImageDraw.Draw(mask).rectangle([block[0] + inset, block[1] + inset, block[2] - inset, block[3] - inset], fill=0)
    mask = mask.filter(ImageFilter.GaussianBlur(feather * 0.25))

    # Mist: two octaves of smooth noise, coloured between the game's fog and a lighter haze.
    random.seed(seed)
    octaves = []
    for cells, weight in ((12, 0.65), (40, 0.35)):
        small = Image.new("L", (cells, max(2, round(cells * height / width))))
        small.putdata([random.randint(0, 255) for _ in range(small.width * small.height)])
        octaves.append((small.resize((width, height), Image.Resampling.BICUBIC), weight))
    noise = Image.blend(octaves[0][0], octaves[1][0], octaves[1][1])
    noise = ImageOps.autocontrast(noise.filter(ImageFilter.GaussianBlur(10)), cutoff=2)
    mist = ImageOps.colorize(noise, black=MIST[0], white=MIST[1]).convert("RGBA")
    mist.putalpha(mask)

    buf = io.BytesIO()
    mist.save(buf, "WEBP", quality=80, method=6)
    corners = [_lonlat(X0, Y0), _lonlat(X1, Y0), _lonlat(X1, Y1), _lonlat(X0, Y1)]
    limits = [corners[0][0], corners[3][1], corners[1][0], corners[0][1]]
    return buf.getvalue(), [round(v, 6) for v in limits], [[round(a, 6), round(b, 6)] for a, b in corners]


def _sea_edges(ready: set[tuple[int, int, int]], z: int, block: tuple[int, int, int, int]):
    """(z10 chunk, side) for every painted chunk side that faces a skipped (all-sea) chunk inside the block."""
    bx0, by0, bx1, by1 = block
    for _, x, y in sorted(ready):
        for side, (nx, ny) in {"w": (x - 1, y), "e": (x + 1, y), "n": (x, y - 1), "s": (x, y + 1)}.items():
            if bx0 <= nx <= bx1 and by0 <= ny <= by1 and (z, nx, ny) not in ready:
                yield (x, y), side


def sea_sample(tiles: Path, style: str, manifest: dict) -> tuple[str, bytes] | None:
    """The style's sea: a median colour (#rrggbb) and a seamless 512 px texture (PNG), from the z12 tiles along the
    painted chunks' edges that face open sea. None if the export has no such edge."""
    z = manifest["chunk_zoom"]
    ready = {tuple(c) for c in manifest["ready_chunks"]}
    xs, ys = [c[1] for c in ready], [c[2] for c in ready]
    k = 2 ** (manifest["maxzoom"] - z)  # z12 tiles per chunk side
    pixels, calm = [], []
    for (x, y), side in _sea_edges(ready, z, (min(xs), min(ys), max(xs), max(ys))):
        for i in range(k):
            tx, ty = {"w": (x * k, y * k + i), "e": (x * k + k - 1, y * k + i),
                      "n": (x * k + i, y * k), "s": (x * k + i, y * k + k - 1)}[side]  # fmt: skip
            p = tiles / style / str(manifest["maxzoom"]) / str(tx) / f"{ty}.png"
            if not p.exists():
                continue
            im = Image.open(p).convert("RGB")
            box = {"w": (0, 0, EDGE_PX, im.height), "e": (im.width - EDGE_PX, 0, im.width, im.height),
                   "n": (0, 0, im.width, EDGE_PX), "s": (0, im.height - EDGE_PX, im.width, im.height)}[side]  # fmt: skip
            pixels += list(im.crop(box).getdata())
            spread = sum(statistics.pstdev(band.getdata()) for band in im.split())
            calm.append((spread, str(p), im))
    if not pixels:
        return None
    colour = "#" + "".join(f"{round(statistics.median(px[c] for px in pixels)):02x}" for c in range(3))
    tile = min(calm, key=lambda t: (t[0], t[1]))[2]
    # Seamless: the tile and its mirror images, 2 x 2.
    texture = Image.new("RGB", (tile.width * 2, tile.height * 2))
    texture.paste(tile, (0, 0))
    texture.paste(ImageOps.mirror(tile), (tile.width, 0))
    texture.paste(ImageOps.flip(tile), (0, tile.height))
    texture.paste(ImageOps.flip(ImageOps.mirror(tile)), (tile.width, tile.height))
    # Pull the texture's mean onto the median edge colour, so the gap meets the painted edge without a step.
    mean = [statistics.fmean(b.getdata()) for b in texture.split()]
    target = [int(colour[i : i + 2], 16) for i in (1, 3, 5)]
    texture = Image.merge(
        "RGB",
        [
            b.point(lambda v, d=t - m: max(0, min(255, round(v + d))))
            for b, t, m in zip(texture.split(), target, mean, strict=True)
        ],
    )
    buf = io.BytesIO()
    texture.save(buf, "PNG", optimize=True)
    return colour, buf.getvalue()
