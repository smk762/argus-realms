"""Which painted chunks a hex needs.

A chunk is one XYZ tile baked by argus-atlas (`atlas chunk --tile z/x/y`). Tile maths comes from
argus-atlas itself so the game and the bake worker can never disagree about addressing.

A hex is playable only once every chunk its outline touches is ready in every live style, so a
hex near a chunk edge never shows half painted and half fog.
"""

import h3
from argus_atlas.addressing import chunk_job_id, neighbour_tiles
from argus_atlas.mercator import lonlat_to_mercator, tiles_covering

Tile = tuple[int, int, int]

__all__ = ["Tile", "at_zoom", "chunk_job_id", "home_chunk", "cell_chunks", "lookahead"]


def _tile_at(lat: float, lng: float, z: int) -> Tile:
    mx, my = lonlat_to_mercator(lng, lat)
    x, y, _, _ = tiles_covering((mx, my, mx, my), z)
    return z, x, y


def home_chunk(cell: str, z: int) -> Tile:
    """The chunk holding the hex's centre: what `hexes.chunk_*` records."""
    return _tile_at(*h3.cell_to_latlng(cell), z)


def cell_chunks(cell: str, z: int) -> list[Tile]:
    """Every chunk the hex's outline touches (1 to 4 at the doc's sizes), sorted.

    Checking the centre and the six vertices is enough while a hex is far smaller than a
    chunk: a straight edge whose ends sit in one tile column can't leave it.
    """
    points = [h3.cell_to_latlng(cell), *h3.cell_to_boundary(cell)]
    return sorted({_tile_at(lat, lng, z) for lat, lng in points})


def at_zoom(tiles, z: int) -> set[Tile]:
    """Ready chunks as the game's chunk zoom sees them. A region baked in coarser chunks (Iceland: z7) is ready in
    every z-tile inside them; a finer chunk only counts once all of the z-tile's children are ready."""
    out: set[Tile] = set()
    finer: dict[tuple[Tile, int], set[Tile]] = {}  # (parent, child zoom) -> children
    for tz, x, y in (tuple(t) for t in tiles):
        if tz <= z:
            k = 2 ** (z - tz)
            out |= {(z, x * k + i, y * k + j) for i in range(k) for j in range(k)}
        else:
            k = 2 ** (tz - z)
            finer.setdefault(((z, x // k, y // k), tz), set()).add((tz, x, y))
    out |= {parent for (parent, tz), kids in finer.items() if len(kids) == 4 ** (tz - z)}
    return out


def lookahead(tile: Tile, rings: int) -> list[Tile]:
    """The tile plus every tile within `rings` rings of it (no wrap across the antimeridian), sorted."""
    seen = {tile}
    edge = {tile}
    for _ in range(rings):
        edge = {n for t in edge for n in neighbour_tiles(*t)} - seen
        seen |= edge
    return sorted(seen)
