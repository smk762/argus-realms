# Data

| File | Source | Licence |
|---|---|---|
| `nz_mainland.geojson` | [Natural Earth](https://www.naturalearthdata.com/) 10m admin 0 countries, `ADM0_A3 = 'NZL'`, clipped to 166–179°E, 47.8–34°S (North, South and Stewart islands plus near-shore islands; no Chathams, Kermadecs or subantarctic islands), one polygon per feature, properties dropped | Public domain |
| `iceland.geojson`, `bonifacio.geojson`, `seto.geojson`, `salish.geojson`, `cape.geojson` | [Natural Earth](https://www.naturalearthdata.com/) 10m land + minor islands, clipped to a box around each region by `scripts/land_outline.py` (one polygon per feature, no properties): the land test for `bakeplan --land` and the game's hexes | Public domain |
| `masks/<area>.png`, `masks/<area>.json` | Sea mask for the static demo, made by `scripts/sea_mask.py` (`make sea-masks`) from the `water` layer (class `ocean`) of the [OpenFreeMap](https://openfreemap.org/) vector tiles, [OpenMapTiles](https://openmaptiles.org/) schema: opaque on land and within 1 km of the coast, fading out by 3 km | Data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL (credited in the demo's attribution) |

Rebuild:

```bash
ogr2ogr -f GeoJSON nz_mainland.geojson \
  /vsizip//vsicurl/https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip \
  -where "ADM0_A3='NZL'" -clipsrc 166 -47.8 179 -34 -explodecollections
```

The region outlines (download `ne_10m_land.zip` and `ne_10m_minor_islands.zip` from
`https://naciscdn.org/naturalearth/10m/physical/` into one directory first):

```bash
uv run scripts/land_outline.py --ne <dir> --bbox=-25.5,62.9,-12.9,67.6 --out data/iceland.geojson
uv run scripts/land_outline.py --ne <dir> --bbox=7.5,38.8,10.5,43.2 --out data/bonifacio.geojson
uv run scripts/land_outline.py --ne <dir> --bbox=131.5,33.2,135.5,35.2 --out data/seto.geojson
uv run scripts/land_outline.py --ne <dir> --bbox=-125.6,47.0,-121.4,49.9 --out data/salish.geojson
uv run scripts/land_outline.py --ne <dir> --bbox=16.8,-35.6,20.4,-32.6 --out data/cape.geojson
```
