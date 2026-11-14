# Data

| File | Source | Licence |
|---|---|---|
| `nz_mainland.geojson` | [Natural Earth](https://www.naturalearthdata.com/) 10m admin 0 countries, `ADM0_A3 = 'NZL'`, clipped to 166–179°E, 47.8–34°S (North, South and Stewart islands plus near-shore islands; no Chathams, Kermadecs or subantarctic islands), one polygon per feature, properties dropped | Public domain |
| `masks/<area>.png`, `masks/<area>.json` | Sea mask for the static demo, made by `scripts/sea_mask.py` (`make sea-masks`) from the `water` layer (class `ocean`) of the [OpenFreeMap](https://openfreemap.org/) vector tiles, [OpenMapTiles](https://openmaptiles.org/) schema: opaque on land and within 1 km of the coast, fading out by 3 km | Data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL (credited in the demo's attribution) |

Rebuild:

```bash
ogr2ogr -f GeoJSON nz_mainland.geojson \
  /vsizip//vsicurl/https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip \
  -where "ADM0_A3='NZL'" -clipsrc 166 -47.8 179 -34 -explodecollections
```
