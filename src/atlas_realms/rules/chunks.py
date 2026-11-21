"""Which painted chunks a hex needs.

A chunk is one XYZ tile baked by argus-atlas (`atlas chunk --tile z/x/y`). Tile maths comes from
argus-atlas itself so the game and the bake worker can never disagree about addressing.

A hex is playable only once every chunk its outline touches is ready in every live style, so a
hex near a chunk edge never shows half painted and half fog.
"""

from collections import Counter

import h3
from argus_atlas.addressing import chunk_job_id, neighbour_tiles
from argus_atlas.mercator import lonlat_to_mercator, tile_bounds, tiles_covering

Tile = tuple[int, int, int]

__all__ = ["Tile", "at_zoom", "chunk_job_id", "home_chunk", "cell_chunks", "lookahead"]


def _tile_at(lat: float, lng: float, z: int) -> Tile:
    mx, my = lonlat_to_mercator(lng, lat)
    x, y, _, _ = tiles_covering((mx, my, mx, my), z)
    return z, x, y


def home_chunk(cell: str, z: int) -> Tile:
    """The chunk holding the hex's centre: what `hexes.chunk_*` records."""
    return _tile_at(*h3.cell_to_latlng(cell), z)


def _clip_area(ring: list[tuple[float, float]], box: tuple[float, float, float, float]) -> float:
    """The area of the polygon `ring` (open, Mercator metres) inside `box` (Sutherland-Hodgman, then the shoelace)."""
    xmin, ymin, xmax, ymax = box
    sides = (
        (lambda p: p[0] >= xmin, lambda a, b: (xmin, a[1] + (b[1] - a[1]) * (xmin - a[0]) / (b[0] - a[0]))),
        (lambda p: p[0] <= xmax, lambda a, b: (xmax, a[1] + (b[1] - a[1]) * (xmax - a[0]) / (b[0] - a[0]))),
        (lambda p: p[1] >= ymin, lambda a, b: (a[0] + (b[0] - a[0]) * (ymin - a[1]) / (b[1] - a[1]), ymin)),
        (lambda p: p[1] <= ymax, lambda a, b: (a[0] + (b[0] - a[0]) * (ymax - a[1]) / (b[1] - a[1]), ymax)),
    )
    pts = ring
    for inside, cross in sides:
        out = []
        for i, b in enumerate(pts):
            a = pts[i - 1]
            if inside(b):
                if not inside(a):
                    out.append(cross(a, b))
                out.append(b)
            elif inside(a):
                out.append(cross(a, b))
        pts = out
        if not pts:
            return 0.0
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1], strict=True))) / 2


def cell_chunks(cell: str, z: int) -> list[Tile]:
    """Every chunk the hex's outline touches (1 to 4 at the doc's sizes), sorted.

    The tiles of the centre and the six vertices, plus any other tile in their box that the outline
    overlaps: where a hex straddles a tile corner, an edge between diagonal tiles can clip a third.
    """
    boundary = h3.cell_to_boundary(cell)
    found = {_tile_at(lat, lng, z) for lat, lng in [h3.cell_to_latlng(cell), *boundary]}
    xs, ys = [t[1] for t in found], [t[2] for t in found]
    if max(xs) - min(xs) > 1 or max(ys) - min(ys) > 1:  # wrapped round the antimeridian: no box to search
        return sorted(found)
    ring = [lonlat_to_mercator(lng, lat) for lat, lng in boundary]
    for x in range(min(xs), max(xs) + 1):
        for y in range(min(ys), max(ys) + 1):
            if (z, x, y) not in found and _clip_area(ring, tile_bounds(z, x, y)) > 0:
                found.add((z, x, y))
    return sorted(found)


def at_zoom(tiles, z: int) -> set[Tile]:
    """Ready chunks as the game's chunk zoom sees them. A region baked in coarser chunks (Iceland: z7) is ready in
    every z-tile inside them; finer chunks only count once they cover the whole z-tile, at one zoom or several."""
    out: set[Tile] = set()
    finer: dict[int, set[tuple[int, int]]] = {}  # zoom -> (x, y) of the ready tiles finer than z
    for tz, x, y in (tuple(t) for t in tiles):
        if tz <= z:
            k = 2 ** (z - tz)
            out |= {(z, x * k + i, y * k + j) for i in range(k) for j in range(k)}
        else:
            finer.setdefault(tz, set()).add((x, y))
    # From the deepest zoom up: a parent whose four children are ready (baked or promoted) is ready itself.
    for tz in range(max(finer, default=z), z, -1):
        parents = Counter((x // 2, y // 2) for x, y in finer.get(tz, ()))
        full = {xy for xy, n in parents.items() if n == 4}
        if tz - 1 == z:
            out |= {(z, x, y) for x, y in full}
        else:
            finer.setdefault(tz - 1, set()).update(full)
    return out


def lookahead(tile: Tile, rings: int) -> list[Tile]:
    """The tile plus every tile within `rings` rings of it (no wrap across the antimeridian), sorted."""
    seen = {tile}
    edge = {tile}
    for _ in range(rings):
        edge = {n for t in edge for n in neighbour_tiles(*t)} - seen
        seen |= edge
    return sorted(seen)
