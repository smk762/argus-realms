"""Plan a block of argus-atlas chunk bakes and print the commands to run on the GPU host.

    python -m atlas_realms.bakeplan --name cook-strait --center=-41.29,174.78 --rings 1 \
        --styles antique,tolkien --land nz_mainland.geojson > bake-cook-strait.sh

Chunks are listed north to south, west to east, so every chunk after the first has its
already-baked west and north neighbours to seam against. Chunks with no land are skipped
when a land polygon is given. Each finished chunk is then labelled with place names from OSM
(`labels.geojson` beside its archive, which `atlas_realms.tiles` merges for the client).
Host paths come from the environment when the script runs: ATLAS_HOST (a dir with dem/ and recipes/),
ATLAS_MAPS (where finished chunks go) and ATLAS_MODELS (the model tree holding the checkpoint).
"""

import argparse
import json
import math
import sys
from pathlib import Path

import h3
from argus_atlas.chunks import chunk_grids
from argus_atlas.mercator import mercator_to_bbox

from atlas_realms.rules.chunks import Tile, cell_chunks, home_chunk, lookahead
from atlas_realms.rules.tunables import DEFAULT

# Measured on the bake host, a 3x3 block (argus-atlas docs/first-bake.md): 45-55 s and ~21 MB per 1024 px chunk.
SEC_PER_CHUNK = 50
MB_PER_CHUNK = 21

# Shell variables the generated script reads (it refuses to start without them).
HOST = '"$ATLAS_HOST"'
# Finished chunks live under <MAPS>/<style>/<style>-<z>-<x>-<y>/.
# One dir per style is also what argus-atlas seams against: --neighbours defaults to the parent of --out.
MAPS = '"$ATLAS_MAPS"'
# `dem` and `labels` are on argus-atlas main after 0.2.0a1 (the pip pin, which the game server needs, is
# unchanged). Move to the release tag once it is cut.
ATLAS_REF = "32a5d98"
IMAGE = f"argus-atlas:{ATLAS_REF}"
RUN = (
    'RUN="docker run --rm -u $(id -u):$(id -g) -w /tmp --add-host host.docker.internal:host-gateway '
    "-e COMFYUI_BASE_URL=http://host.docker.internal:8188 -e ATLAS_ALLOW_BAKE=1 "
    "-e ATLAS_RECIPES_DIR=/data/recipes -e ATLAS_MODELS_DIR=/data/models -e ATLAS_JOBS_DIR=/data/out/atlas "
    "-v $ATLAS_HOST/dem:/data/dem -v $ATLAS_HOST/recipes:/data/recipes:ro -v $ATLAS_MODELS:/data/models:ro "
    f'-v $ATLAS_MAPS:/data/out/atlas {IMAGE}"'
)
OSM_LAYERS = "water,rivers,roads,forest,urban"
# `dem` snaps its window inward to the GLO-30 lattice (1 arc-second); padding keeps the canvas edges covered.
DEM_PAD_DEG = 0.01


def raster_order(tiles: list[Tile]) -> list[Tile]:
    return sorted(tiles, key=lambda t: (t[2], t[1]))


def canvas_bbox(tiles: list[Tile], size: int, margin: int) -> tuple[float, float, float, float]:
    """lon/lat (west, south, east, north) of every chunk's canvas, margins included."""
    xs, ys = [], []
    for t in tiles:
        canvas, _ = chunk_grids(*t, size=size, margin=margin)
        xs += [canvas.ulx, canvas.ulx + canvas.width * canvas.pixel_width]
        ys += [canvas.uly, canvas.uly + canvas.height * canvas.pixel_height]
    b = mercator_to_bbox(min(xs), min(ys), max(xs), max(ys))
    return b.west, b.south, b.east, b.north


def dem_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    west, south, east, north = bbox
    return west - DEM_PAD_DEG, south - DEM_PAD_DEG, east + DEM_PAD_DEG, north + DEM_PAD_DEG


def land_cells(path: Path, res: int) -> set[str]:
    cells: set[str] = set()
    for f in json.loads(path.read_text())["features"]:
        cells |= set(h3.geo_to_cells(f["geometry"], res))
    return cells


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--name", required=True, help="Region name: OSM extract dir and job group")
    p.add_argument("--center", required=True, help="lat,lng of the block's centre chunk")
    p.add_argument("--rings", type=int, default=1, help="Chunk rings around the centre (1 = 3x3, 2 = 5x5)")
    p.add_argument("--zoom", type=int, default=DEFAULT.chunk_zoom)
    p.add_argument("--size", type=int, default=DEFAULT.chunk_size)
    p.add_argument("--margin", type=int, default=DEFAULT.chunk_margin)
    p.add_argument("--styles", default="antique", help="Comma-separated recipe names")
    p.add_argument("--land", type=Path, help="Land polygons (GeoJSON, EPSG:4326): skip all-sea chunks, count hexes")
    p.add_argument("--hex-res", type=int, default=DEFAULT.h3_resolution, help="H3 resolution for hex counts")
    a = p.parse_args(argv)

    lat, lng = (float(v) for v in a.center.split(","))
    centre = home_chunk(h3.latlng_to_cell(lat, lng, 9), a.zoom)
    block = lookahead(centre, a.rings)
    styles = [s for s in a.styles.split(",") if s]

    notes = []
    if a.land:
        fine = land_cells(a.land, 7)
        with_land = {t for c in fine for t in cell_chunks(c, a.zoom)}
        skipped = sorted(set(block) - with_land)
        block = [t for t in block if t in with_land]
        hexes = [c for c in land_cells(a.land, a.hex_res) if home_chunk(c, a.zoom) in set(block)]
        notes.append(f"all-sea chunks skipped: {len(skipped)} {[f'{z}/{x}/{y}' for z, x, y in skipped]}")
        notes.append(
            f"land hexes at H3 res {a.hex_res}: {len(hexes):,} (room for {len(hexes) // DEFAULT.hex_cap:,} players at {DEFAULT.hex_cap} each)"
        )
    tiles = raster_order(block)
    bbox = canvas_bbox(tiles, a.size, a.margin)
    jobs = len(tiles) * len(styles)
    bbox_s = ",".join(f"{v:.4f}" for v in bbox)
    native = a.zoom + int(math.log2(a.size // 256))

    out = sys.stdout.write
    out("#!/usr/bin/env bash\n")
    out(
        f"# Bake plan '{a.name}': {len(tiles)} chunks x {len(styles)} styles = {jobs} jobs at z{a.zoom} ({a.size} px, native z{native})\n"
    )
    out(f"# Estimate: {jobs * SEC_PER_CHUNK / 60:.0f} min GPU, {jobs * MB_PER_CHUNK / 1024:.1f} GB on the share\n")
    for n in notes:
        out(f"# {n}\n")
    out("set -euo pipefail\n")
    out(': "${ATLAS_HOST:?}" "${ATLAS_MAPS:?}" "${ATLAS_MODELS:?}"\n\n')
    out(
        f"# 0. Image at the argus-atlas commit this plan needs (once)\n# cd <argus-atlas checkout> && git fetch && git checkout {ATLAS_REF} && \\\n"
    )
    out(f'#   docker build -t {IMAGE} --build-arg EXTRAS="server,cli,tiles" .\n')
    out(RUN + "\n\n")
    dem = f"/data/dem/{a.name}_glo30.tif"
    places = f"/data/dem/osm/{a.name}-places/places.geojson"
    out("# 1. DEM: Copernicus GLO-30 for the block plus margins, one GeoTIFF (all-sea tiles are filled with 0 m)\n")
    out(f"[ -f {HOST}/dem/{a.name}_glo30.tif ] || \\\n  ")
    out(f"$RUN argus-atlas dem --bbox {','.join(f'{v:.4f}' for v in dem_bbox(bbox))} --out {dem} --allow-missing\n\n")
    out(
        "# 2. OSM for the whole block in one Overpass call, and its place names (a separate extract: never a control)\n"
    )
    out(f"[ -f {HOST}/dem/osm/{a.name}/osm.json ] || \\\n  ")
    out(f"$RUN argus-atlas osm --bbox {bbox_s} --zoom {native} --layers {OSM_LAYERS} --out /data/dem/osm/{a.name}\n")
    out(f"[ -f {HOST}/dem/osm/{a.name}-places/places.geojson ] || \\\n  ")
    out(f"$RUN argus-atlas osm --bbox {bbox_s} --zoom {native} --layers places --out /data/dem/osm/{a.name}-places\n\n")
    out("# 3. Recipes: a missing <style>.json is copied from antique.json with its name and preset swapped\n")
    out(f'test -f {HOST}/recipes/antique.json || {{ echo "missing $ATLAS_HOST/recipes/antique.json"; exit 1; }}\n')
    for s in (s for s in styles if s != "antique"):
        out(
            f'test -f {HOST}/recipes/{s}.json || sed \'s/"antique"/"{s}"/g\' {HOST}/recipes/antique.json > {HOST}/recipes/{s}.json\n'
        )
    layer_args = " ".join(f"--layer {n}=/data/dem/osm/{a.name}/{n}.geojson" for n in OSM_LAYERS.split(","))
    out("\n# 4. Chunks, one style at a time, north to south, west to east. A re-run skips chunks that have tiles.\n")
    out(
        "curl -sf -m 5 http://127.0.0.1:8188/system_stats >/dev/null || { echo 'ComfyUI is not answering on :8188'; exit 1; }\n"
    )
    for s in styles:
        out(f"mkdir -p {MAPS}/{s}\n")
        for z, x, y in tiles:
            out(f"[ -f {MAPS}/{s}/{s}-{z}-{x}-{y}/fantasy.pmtiles ] || \\\n  ")
            out(
                f"$RUN argus-atlas chunk --tile {z}/{x}/{y} --recipe {s} --dem {dem} "
                f"--size {a.size} --margin {a.margin} --sea-level 0 {layer_args} "
                f"--out /data/out/atlas/{s}/{s}-{z}-{x}-{y}\n"
            )
    # Names don't depend on the style, so one style's chunks carry them for all (atlas_realms.tiles merges them).
    out(f"\n# 5. Place names for every chunk, on the {styles[0]} jobs (a re-run skips labelled ones)\n")
    for z, x, y in tiles:
        job = f"{styles[0]}/{styles[0]}-{z}-{x}-{y}"
        out(
            f"[ -f {MAPS}/{job}/labels.geojson ] || $RUN argus-atlas labels --job /data/out/atlas/{job} --source {places}\n"
        )
    out("\n# 6. Report timings: $RUN argus-atlas inspect /data/out/atlas/<style>/<style>-<z>-<x>-<y>\n")


if __name__ == "__main__":
    main()
