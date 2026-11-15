"""Make the strait read as one sea: the block's open-sea chunks become baked tiles, CPU only, at build time.

The bake plan skipped the chunks with no land (`atlas_realms.seed.sea_chunks`, the same rule that makes them
playable). A flat texture behind them showed as rectangles, because each painted chunk's sea is a slightly different
blue. This module paints them in as ordinary tiles, so they render exactly like painted ones at every zoom:

1. Lay the block's painted z12 tiles on one canvas, and find the painted sea (`_sea_mask`).
2. Level the seams between painted chunks out at sea (`_level_painted_seams`): each side's sea shifts by half the
   colour step, easing back to nothing over LEVEL_BAND px. Only the low frequencies of sea pixels change.
3. Fill the gaps with a colour field interpolated (push-pull) from the painted sea next to them.
4. Add grain generated to match the painted sea's own: fine noise with its spread, plus its dark speckles at its
   density. The grain is seeded, so it never repeats visibly and builds are reproducible.
5. Mirror each painted neighbour's sea across the shared edge, easing into the fill over MIRROR px, so colour and
   grain are continuous right at the edge.
6. Write every tile of the block, at every zoom, from the canvas; the coarser zooms are downsampled from it.

Pillow only. `fill()` writes a complete tile tree for `tiles_webp.export`.
"""

import random
import shutil
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

VERSION = "7"  # part of the build cache key: bump when the output changes
TILE = 256
SEA_DISTANCE = 42  # RGB distance from the style's sea colour within which a painted pixel counts as sea
GRAIN_BLUR = 6  # px: what counts as grain (finer than this) in the painted sea


def _canvas(tiles: Path, style: str, z: int, x0: int, y0: int, nx: int, ny: int) -> Image.Image:
    canvas = Image.new("RGBA", (nx * TILE, ny * TILE), (0, 0, 0, 0))
    for tx in range(x0, x0 + nx):
        for ty in range(y0, y0 + ny):
            p = tiles / style / str(z) / str(tx) / f"{ty}.png"
            if p.exists():
                canvas.paste(Image.open(p).convert("RGBA"), ((tx - x0) * TILE, (ty - y0) * TILE))
    return canvas


def _sea_mask(canvas: Image.Image, sea: tuple[int, int, int]) -> Image.Image:
    """255 where the canvas holds painted sea, else 0. Judged on a blurred copy, so the sea's own dark speckles count
    as sea (they are part of its average colour), while land, even a sliver at a chunk edge, does not."""
    # Blurred premultiplied, so painted pixels at a chunk edge aren't darkened by the empty (black) gap beside them.
    rgb = canvas.convert("RGBa").filter(ImageFilter.GaussianBlur(GRAIN_BLUR)).convert("RGBA").convert("RGB")
    flat = Image.new("RGB", canvas.size, sea)
    diff = ImageChops.difference(rgb, flat)
    r, g, b = diff.split()
    # Chebyshev-ish distance: the largest channel difference, thresholded.
    near = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v <= SEA_DISTANCE else 0)
    return ImageChops.multiply(near, canvas.getchannel("A").point(lambda a: 255 if a == 255 else 0))


def _push_pull(rgb: Image.Image, mask: Image.Image) -> Image.Image:
    """Fill the pixels where mask is 0 by a smooth interpolation of the pixels where it is 255."""
    levels = []
    cur = rgb.convert("RGBA")
    cur.putalpha(mask)
    cur = cur.convert("RGBa")  # premultiplied, so resizing averages only the known pixels
    while min(cur.size) > 4:
        levels.append(cur)
        cur = cur.resize((max(1, cur.width // 2), max(1, cur.height // 2)), Image.Resampling.BOX)
    levels.append(cur)
    out = cur.convert("RGBA")
    # The coarsest level: anything still unknown gets the mean of what is known.
    known = out.getchannel("A")
    mean = tuple(round(v) for v in ImageStat.Stat(out.convert("RGB"), mask=known.point(lambda a: 255 if a else 0)).mean)
    base = Image.new("RGB", out.size, mean)
    base.paste(out.convert("RGB"), mask=known)
    out = base
    for lvl in reversed(levels[:-1]):
        # A little blur at every level keeps the coarse levels from showing as blocks.
        up = out.resize(lvl.size, Image.Resampling.BICUBIC).filter(ImageFilter.GaussianBlur(1.5))
        here = lvl.convert("RGBA")  # un-premultiplied: the average colour of the known pixels, alpha = coverage
        up.paste(here.convert("RGB"), mask=here.getchannel("A"))
        out = up
    return out


def _grain_stats(canvas: Image.Image, mask: Image.Image) -> tuple[float, float, float]:
    """The painted sea's grain, from the canvas tiles that are all sea: the spread of its fine noise (luminance
    high-pass, standard deviation), and how many dark speckles it has (share of pixels) and how dark they are."""
    stds, dens, depths = [], [], []
    for x in range(0, canvas.width, TILE):
        for y in range(0, canvas.height, TILE):
            box = (x, y, x + TILE, y + TILE)
            if ImageStat.Stat(mask.crop(box)).mean[0] < 254.5:
                continue
            lum = canvas.crop(box).convert("L")
            high = ImageChops.subtract(lum, lum.filter(ImageFilter.GaussianBlur(GRAIN_BLUR)), offset=128)
            hist = high.histogram()
            # The noise floor: the spread of the middle 90 % (speckles would inflate a plain standard deviation).
            total, acc, lo, hi = sum(hist), 0, None, None
            for v, n in enumerate(hist):
                acc += n
                if lo is None and acc >= total * 0.05:
                    lo = v
                if hi is None and acc >= total * 0.95:
                    hi = v
            std = (hi - lo) / 3.29  # 90 % of a normal distribution spans 3.29 standard deviations
            cut = round(128 - 4 * max(std, 1))
            dark = sum(hist[:cut])
            stds.append(std)
            dens.append(dark / total)
            depths.append(sum((128 - v) * n for v, n in enumerate(hist[:cut])) / dark if dark else 0)
    if not stds:
        return 2.0, 0.0, 0.0
    mid = lambda xs: sorted(xs)[len(xs) // 2]  # noqa: E731
    return mid(stds), mid(dens), mid([d for d in depths if d] or [0])


def _procedural_grain(size: tuple[int, int], std: float, density: float, depth: float, seed: int) -> Image.Image:
    """Grain like the painted sea's, generated so it never repeats visibly: fine noise with the measured spread, plus
    dark speckles at the measured density and depth. Deterministic (seeded), so builds are reproducible."""
    rnd = random.Random(seed)
    cell = 1024
    base = Image.new("L", (cell, cell))
    base.putdata([max(0, min(255, round(rnd.gauss(128, 40)))) for _ in range(cell * cell)])
    base = base.filter(ImageFilter.GaussianBlur(0.8))
    spread = ImageStat.Stat(base).stddev[0] or 1
    k = std / spread
    base = base.point(lambda v: max(0, min(255, round(128 + (v - 128) * k))))
    grain = Image.new("L", size, 128)
    variants = [base, base.transpose(Image.Transpose.FLIP_LEFT_RIGHT), base.transpose(Image.Transpose.FLIP_TOP_BOTTOM),
                base.transpose(Image.Transpose.ROTATE_180), base.transpose(Image.Transpose.TRANSPOSE)]  # fmt: skip
    for x in range(0, size[0], cell):
        for y in range(0, size[1], cell):
            grain.paste(rnd.choice(variants), (x, y))
    if density and depth:
        dots = Image.new("L", size, 0)
        draw = ImageDraw.Draw(dots)
        for _ in range(int(density * size[0] * size[1] / 4)):  # each dot darkens ~4 px
            x, y, r = rnd.uniform(0, size[0]), rnd.uniform(0, size[1]), rnd.uniform(0.7, 1.6)
            draw.ellipse((x - r, y - r, x + r, y + r), fill=min(255, round(depth * rnd.uniform(0.6, 1.3))))
        grain = ImageChops.subtract(grain, dots.filter(ImageFilter.GaussianBlur(0.6)))
    return Image.merge("RGB", (grain, grain, grain))


def _add_grain(fill: Image.Image, grain: Image.Image) -> Image.Image:
    # fill + (grain - 128), clamped: ImageChops.add with offset does (a + b) / scale + offset.
    return ImageChops.add(fill, grain, scale=1.0, offset=-128)


SOURCE_BAND = 192  # px at z12: painted sea this close to a gap sets the gap's colour
MIRROR = 320  # px at z12: how far a painted chunk's own sea is mirrored into a neighbouring sea chunk


def _ramp(length: int, across: int, vertical: bool) -> Image.Image:
    """An L mask: 255 at the start, easing (smoothstep) to 0 at `length`."""
    line = Image.new("L", (1, length) if vertical else (length, 1))
    vals = [round(255 * (1 - (3 * t * t - 2 * t * t * t))) for t in (i / (length - 1) for i in range(length))]
    line.putdata(vals)
    return line.resize((across, length) if vertical else (length, across))


def _mirror_edges(synth, canvas, mask, sea, ready, origin, size) -> None:
    """Next to each painted chunk, the sea chunk starts as that chunk's own sea, mirrored across the shared edge, and
    eases into the synthesised fill over MIRROR px. Colour and grain are then continuous at the edge itself. Land in
    the mirrored band (a coast along the edge) is left out, so no land is reflected into the sea."""
    cx0, cy0 = origin
    d = MIRROR
    ramps = {"v": _ramp(d, size, True), "h": _ramp(d, size, False)}
    flip_v, flip_h = Image.Transpose.FLIP_TOP_BOTTOM, Image.Transpose.FLIP_LEFT_RIGHT
    for z, x, y in sorted(sea):
        X, Y = (x - cx0) * size, (y - cy0) * size
        sides = {
            (x, y - 1): ((X, Y - d, X + size, Y), flip_v, (X, Y), ramps["v"]),
            (x, y + 1): ((X, Y + size, X + size, Y + size + d), flip_v, (X, Y + size - d), ramps["v"].transpose(flip_v)),
            (x - 1, y): ((X - d, Y, X, Y + size), flip_h, (X, Y), ramps["h"]),
            (x + 1, y): ((X + size, Y, X + size + d, Y + size), flip_h, (X + size - d, Y), ramps["h"].transpose(flip_h)),
        }  # fmt: skip
        for (nx, ny), (box, flip, at, ramp) in sides.items():
            if (z, nx, ny) not in ready:
                continue
            band = canvas.crop(box).convert("RGB").transpose(flip)
            alpha = ImageChops.multiply(ramp, mask.crop(box).transpose(flip))
            synth.paste(band, at, alpha)


LEVEL_BAND = 256  # px at z12: how far either side of a seam between two painted chunks their sea is levelled
SEGMENT = 32  # px: the seam is measured in segments this long, then smoothed along it


def _seam_profile(canvas, mask, a_box, b_box, along: int) -> list[tuple[float, float, float]]:
    """Half the colour step across a seam, per SEGMENT along it, smoothed: from side a's edge strip to side b's,
    counting only sea pixels on both sides (0 where either side is land)."""

    def means(box):
        strip = canvas.crop(box).convert("RGB").convert("RGBA")
        strip.putalpha(mask.crop(box))
        w, h = strip.size
        small = strip.convert("RGBa").resize(
            (1, along // SEGMENT) if h > w else (along // SEGMENT, 1), Image.Resampling.BOX
        )
        return [px for px in small.convert("RGBA").getdata()]

    steps = []
    for (ar, ag, ab, aa), (br, bg, bb, ba) in zip(means(a_box), means(b_box), strict=True):
        ok = aa > 160 and ba > 160
        steps.append(((br - ar) / 2, (bg - ag) / 2, (bb - ab) / 2, 1.0 if ok else 0.0))
    out = []
    for i in range(len(steps)):  # smooth along the seam (Gaussian, about 3 segments), ignoring land segments
        acc, wsum = [0.0, 0.0, 0.0], 0.0
        for j in range(max(0, i - 6), min(len(steps), i + 7)):
            w = steps[j][3] * pow(2.718, -((i - j) ** 2) / 18)
            wsum += w
            acc = [a + w * v for a, v in zip(acc, steps[j][:3], strict=True)]
        out.append(tuple(a / wsum for a in acc) if wsum > 0.05 else (0.0, 0.0, 0.0))
    return out


def _level_painted_seams(canvas, mask, ready, origin, size) -> Image.Image:
    """Each painted chunk's sea is a slightly different blue, so two painted chunks can meet in a visible straight
    line out at sea. Level them: across each shared edge, shift each side's sea by half the colour step, easing back
    to nothing over LEVEL_BAND px. Only sea pixels move (a soft sea mask, so coasts keep their colour) and only the
    low frequencies change: the grain stays as painted. Returns the corrected canvas."""
    cx0, cy0 = origin
    b, strip = LEVEL_BAND, 16
    offset = Image.new("RGB", canvas.size, (128, 128, 128))  # signed colour shifts, centred on 128
    flip_v, flip_h = Image.Transpose.FLIP_TOP_BOTTOM, Image.Transpose.FLIP_LEFT_RIGHT
    ramp_h = _ramp(b, size, False)  # 255 at its left edge, easing to 0 at b
    ramp_v = _ramp(b, size, True)  # 255 at its top edge
    for z, x, y in sorted(ready):
        X, Y = (x - cx0) * size, (y - cy0) * size
        for (nx, ny), vertical in (((x + 1, y), True), ((x, y + 1), False)):
            if (z, nx, ny) not in ready:
                continue
            if vertical:  # the seam is the line x = X + size, running down
                e = X + size
                prof = _seam_profile(canvas, mask, (e - strip, Y, e, Y + size), (e, Y, e + strip, Y + size), size)
                line = Image.new("RGB", (1, len(prof)))
            else:  # the seam is y = Y + size, running across
                e = Y + size
                prof = _seam_profile(canvas, mask, (X, e - strip, X + size, e), (X, e, X + size, e + strip), size)
                line = Image.new("RGB", (len(prof), 1))
            line.putdata([tuple(max(0, min(255, round(128 + v))) for v in p) for p in prof])
            if vertical:
                shift = line.resize((b, size), Image.Resampling.BICUBIC)  # +step: the a (left) side moves towards b
                flat = Image.new("RGB", shift.size, (128, 128, 128))
                left = Image.composite(shift, flat, ramp_h.transpose(flip_h))  # strongest at the seam (right end)
                right = ImageChops.invert(Image.composite(shift, flat, ramp_h))  # the b side moves the other way
                right = ImageChops.add(right, Image.new("RGB", right.size, (1, 1, 1)))  # invert maps 128 to 127
                boxes = ((left, (e - b, Y)), (right, (e, Y)))
            else:
                shift = line.resize((size, b), Image.Resampling.BICUBIC)
                flat = Image.new("RGB", shift.size, (128, 128, 128))
                top = Image.composite(shift, flat, ramp_v.transpose(flip_v))
                bottom = ImageChops.add(
                    ImageChops.invert(Image.composite(shift, flat, ramp_v)), Image.new("RGB", shift.size, (1, 1, 1))
                )
                boxes = ((top, (X, e - b)), (bottom, (X, e)))
            for img, at in boxes:
                region = (at[0], at[1], at[0] + img.width, at[1] + img.height)
                offset.paste(ImageChops.add(offset.crop(region), img, offset=-128), at)
    rgb = canvas.convert("RGB")
    moved = ImageChops.add(rgb, offset, offset=-128)
    soft = mask.filter(ImageFilter.GaussianBlur(3))
    out = canvas.copy()
    out.paste(Image.composite(moved, rgb, soft), (0, 0), canvas.getchannel("A"))
    return out


def fill(tiles: Path, style: str, manifest: dict, sea: set[tuple[int, int, int]], sea_colour: str, out: Path) -> dict:
    """Write tiles/<style> plus the synthesised sea into out/<style>. Returns counts for the build log."""
    z = manifest["maxzoom"]
    cz = manifest["chunk_zoom"]
    k = 2 ** (z - cz)
    ready = {tuple(c) for c in manifest["ready_chunks"]}
    chunks = ready | set(sea)
    cx0, cy0 = min(c[1] for c in chunks), min(c[2] for c in chunks)
    cx1, cy1 = max(c[1] for c in chunks), max(c[2] for c in chunks)
    x0, y0, nx, ny = cx0 * k, cy0 * k, (cx1 - cx0 + 1) * k, (cy1 - cy0 + 1) * k
    colour = tuple(int(sea_colour[i : i + 2], 16) for i in (1, 3, 5))

    canvas = _canvas(tiles, style, z, x0, y0, nx, ny)
    mask = _sea_mask(canvas, colour)
    canvas = _level_painted_seams(canvas, mask, ready, (cx0, cy0), k * TILE)
    # The fill's colour comes only from the painted sea within SOURCE_BAND px of the gaps. Further in, near the
    # coasts, painted water is lighter (shallows), and pulling that in made the middle of the strait too pale.
    near = Image.new("L", canvas.size, 0)
    draw = ImageDraw.Draw(near)
    size = k * TILE
    for _, x, y in sea:
        X, Y = (x - cx0) * size, (y - cy0) * size
        draw.rectangle((X - SOURCE_BAND, Y - SOURCE_BAND, X + size + SOURCE_BAND, Y + size + SOURCE_BAND), fill=255)
    smooth = _push_pull(canvas.convert("RGB"), ImageChops.multiply(mask, near))
    std, density, depth = _grain_stats(canvas, mask)
    synth = _add_grain(smooth, _procedural_grain(smooth.size, std, density, depth, seed=sum(map(ord, style))))
    _mirror_edges(synth, canvas, mask, sea, ready, (cx0, cy0), k * TILE)
    # Painted pixels stay; the synthesised sea shows only where nothing was painted.
    full = synth.convert("RGBA")
    full.alpha_composite(canvas)

    # Copy the exported tree, then add or replace tiles inside the block.
    dest = out / style
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(tiles / style, dest)
    written = 0
    for zoom in range(manifest["minzoom"], z + 1):
        f = 2 ** (z - zoom)
        level = (
            full
            if f == 1
            else full.resize((max(1, full.width // f), max(1, full.height // f)), Image.Resampling.LANCZOS)
        )
        ox, oy = x0 * TILE // f, y0 * TILE // f  # the block's top-left in this zoom's pixel space
        for tx in range(ox // TILE, (ox + level.width - 1) // TILE + 1):
            for ty in range(oy // TILE, (oy + level.height - 1) // TILE + 1):
                p = dest / str(zoom) / str(tx) / f"{ty}.png"
                # Inside the block the tile comes from the canvas (levelled seams, filled sea); outside it, a coarse
                # tile keeps what was exported.
                tile = Image.open(p).convert("RGBA") if p.exists() else Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0))
                left, top = tx * TILE - ox, ty * TILE - oy
                piece = level.crop(
                    (max(0, left), max(0, top), min(level.width, left + TILE), min(level.height, top + TILE))
                )
                tile.paste(piece, (max(0, -left), max(0, -top)))
                p.parent.mkdir(parents=True, exist_ok=True)
                tile.save(p, "PNG")
                written += 1
    return {"style": style, "sea_chunks": len(sea), "tiles_written": written}
