# /// script
# requires-python = ">=3.11"
# dependencies = ["pyshp>=2.3", "shapely>=2.0"]
# ///
"""Land outline for one region: Natural Earth 10m land + minor islands, clipped to a lon/lat box.

    uv run scripts/land_outline.py --ne <dir with ne_10m_land.zip and ne_10m_minor_islands.zip> \
        --bbox 7.9,40.2,10.4,43.2 --out data/bonifacio.geojson

The game (`seed.land_cells`) and the bake plan (`bakeplan --land`) only need to know which H3 cells hold land, so
Natural Earth's 1:10M coastline is detailed enough for res-5 hexes. One polygon per feature, no properties, coordinates
rounded to 1e-5 degrees (about 1 m). Natural Earth is public domain.
"""

import argparse
import json
import zipfile
from pathlib import Path

import shapefile
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

LAYERS = ("ne_10m_land", "ne_10m_minor_islands")


def read_layer(zip_path: Path):
    with zipfile.ZipFile(zip_path) as z:
        stem = zip_path.stem
        parts = {ext: z.open(f"{stem}.{ext}") for ext in ("shp", "shx", "dbf")}
        with shapefile.Reader(**parts) as r:
            return [shape(s.__geo_interface__) for s in r.shapes()]


def rounded(coords):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], 5), round(coords[1], 5)]
    return [rounded(c) for c in coords]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--ne", type=Path, required=True, help="Directory holding the Natural Earth zips")
    p.add_argument("--bbox", required=True, help="west,south,east,north in degrees")
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()

    clip = box(*(float(v) for v in a.bbox.split(",")))
    geoms = [g for layer in LAYERS for g in read_layer(a.ne / f"{layer}.zip") if g.intersects(clip)]
    land = unary_union([g.intersection(clip) for g in geoms])
    polys = list(getattr(land, "geoms", [land]))
    features = [
        {
            "type": "Feature",
            "properties": {},
            "geometry": {"type": "Polygon", "coordinates": rounded(mapping(g)["coordinates"])},
        }
        for g in sorted(polys, key=lambda g: -g.area)
        if g.geom_type == "Polygon" and not g.is_empty
    ]
    a.out.write_text(json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")) + "\n")
    print(f"{a.out}: {len(features)} polygons")


if __name__ == "__main__":
    main()
