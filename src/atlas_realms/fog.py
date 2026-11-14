"""The fog of war as one GeoJSON layer: the whole world, with a hole for every playable area.

Works for any number of unconnected areas (two spawn regions in PvP each get their own hole). Fog
enclosed by playable land (a pocket still waiting on its chunks) comes back as its own polygon.
MapLibre tells holes from outer rings by winding, so the world ring runs counter-clockwise and every
hole clockwise.
"""

from collections.abc import Iterable

import h3

WORLD = [[-180.0, -85.0], [180.0, -85.0], [180.0, 85.0], [-180.0, 85.0], [-180.0, -85.0]]


def _ring(coords, clockwise: bool) -> list[list[float]]:
    ring = [[float(x), float(y)] for x, y in coords]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    area = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:], strict=False))
    return ring[::-1] if (area < 0) != clockwise else ring  # area < 0 means clockwise


def fog_geojson(playable: Iterable[str]) -> dict:
    cells = sorted(set(playable))
    polygons = [[WORLD]]
    if cells:
        g = h3.cells_to_geo(cells)
        shapes = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        polygons[0] += [_ring(shape[0], clockwise=True) for shape in shapes]
        polygons += [[_ring(inner, clockwise=False)] for shape in shapes for inner in shape[1:]]
    return {
        "type": "Feature",
        "properties": {"playable_hexes": len(cells)},
        "geometry": {"type": "MultiPolygon", "coordinates": polygons},
    }
