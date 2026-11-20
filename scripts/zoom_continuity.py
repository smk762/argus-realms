"""Measure how far each zoom level drifts from the level above it, per area and style.

    python3 scripts/zoom_continuity.py var/tiles-areas [--json out.json]

For every pair (parent zoom p, child zoom p+k) the child's tiles over the parent's footprint are stitched,
box-downsampled by 2**k to the parent's resolution and compared with the parent mosaic, on pixels both
paint (alpha > 0): SSIM on luminance (Gaussian window, sigma 1.5) and a colour-histogram distance
(Hellinger over a joint 8x8x8 RGB histogram; 0 = identical, 1 = disjoint). Needs numpy and Pillow.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

TILE = 256


def mosaic(style: Path, z: int, x0: int, y0: int, nx: int, ny: int) -> np.ndarray:
    out = np.zeros((ny * TILE, nx * TILE, 4), np.float64)
    for x in range(x0, x0 + nx):
        for y in range(y0, y0 + ny):
            p = style / str(z) / str(x) / f"{y}.png"
            if p.exists():
                with Image.open(p) as im:
                    out[(y - y0) * TILE : (y - y0 + 1) * TILE, (x - x0) * TILE : (x - x0 + 1) * TILE] = np.asarray(
                        im.convert("RGBA"), np.float64
                    )
    return out


def gauss(img: np.ndarray, sigma: float = 1.5) -> np.ndarray:
    r = int(3 * sigma + 0.5)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    img = np.apply_along_axis(lambda v: np.convolve(v, k, "same"), 0, img)
    return np.apply_along_axis(lambda v: np.convolve(v, k, "same"), 1, img)


def ssim(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> float:
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ma, mb = gauss(a), gauss(b)
    va, vb, cov = gauss(a * a) - ma * ma, gauss(b * b) - mb * mb, gauss(a * b) - ma * mb
    s = ((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma * ma + mb * mb + c1) * (va + vb + c2))
    inner = gauss(mask.astype(float)) > 0.999  # windows wholly inside the painted area
    return float(s[inner].mean())


def hellinger(a: np.ndarray, b: np.ndarray) -> float:
    def hist(px):
        q = (px[:, :3] // 32).astype(int)
        h = np.bincount(q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2], minlength=512).astype(float)
        return h / h.sum()

    return float(np.sqrt(max(0.0, 1 - np.sqrt(hist(a) * hist(b)).sum())))


def lum(rgba: np.ndarray) -> np.ndarray:
    return rgba[..., 0] * 0.299 + rgba[..., 1] * 0.587 + rgba[..., 2] * 0.114


def compare(style: Path, p: int, c: int) -> dict | None:
    """Parent zoom `p` against child zoom `c` over the parent's footprint; None if `p` has no tiles."""
    tiles = [(int(t.parent.name), int(t.stem)) for t in style.glob(f"{p}/*/*.png")]
    if not tiles:
        return None
    xs, ys = [t[0] for t in tiles], [t[1] for t in tiles]
    x0, y0, nx, ny = min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
    f = 2 ** (c - p)
    parent = mosaic(style, p, x0, y0, nx, ny)
    child = mosaic(style, c, x0 * f, y0 * f, nx * f, ny * f)
    child = child.reshape(ny * TILE, f, nx * TILE, f, 4).mean(axis=(1, 3))  # box filter
    mask = (parent[..., 3] > 0) & (child[..., 3] > 254)
    return {
        "parent": p,
        "child": c,
        "pixels": int(mask.sum()),
        "ssim": round(ssim(lum(parent), lum(child), mask), 4),
        "hist_hellinger": round(hellinger(parent[mask], child[mask]), 4),
        "mean_abs_rgb": round(float(np.abs(parent[mask][:, :3] - child[mask][:, :3]).mean()), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tiles", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    rows = []
    for man in sorted(a.tiles.glob("*/manifest.json")):
        m = json.loads(man.read_text())
        base, top = m["chunk_zoom"], m["maxzoom"]
        for st in m["styles"]:
            style = man.parent / st["name"]
            for p, c in [(base, base + 1), (base + 1, base + 2), (base, base + 2), (base - 1, base)]:
                if c <= top:
                    got = compare(style, p, c)
                    if got is None:
                        print(f"{style}: no z{p} tiles, z{p}->z{c} skipped", file=sys.stderr)
                        continue
                    r = {"area": man.parent.name, "style": st["name"], **got}
                    rows.append(r)
                    print(json.dumps(r), file=sys.stderr)
    if a.json:
        a.json.write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
