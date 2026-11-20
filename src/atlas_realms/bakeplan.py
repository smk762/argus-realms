"""Plan a block of argus-atlas chunk bakes and print the commands to run on the GPU host.

    python -m atlas_realms.bakeplan --name cook-strait --center=-41.29,174.78 --rings 1 \
        --styles antique,tolkien --land nz_mainland.geojson > bake-cook-strait.sh

Chunks are listed north to south, west to east, so every chunk after the first has its
already-baked west and north neighbours to seam against. Chunks with no land are skipped
when a land polygon is given. Each finished chunk is then labelled with place names from OSM
(`labels.geojson` beside its archive, which `atlas_realms.tiles` merges for the client).
Host paths come from the environment when the script runs: ATLAS_HOST (a dir with dem/ and recipes/),
ATLAS_MAPS (where finished chunks go) and ATLAS_MODELS (the model tree holding the checkpoint).

The image is built from the argus-atlas commit in `docs/atlas/sync.json` (the docs Atlas publishes), so tiles and docs
come from one build: `make static` refuses any other. A re-bake goes into a fresh ATLAS_MAPS: the script refuses to
add chunks beside jobs from another argus-atlas build, which `chunk` would otherwise seam against. `--tiles` bakes an
explicit chunk list instead of a block (the islands, whose outlines miss an islet the paintings include).
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

import h3
from argus_atlas.addressing import chunk_grids
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
# The argus-atlas build every plan bakes with: the commit the docs were imported at (scripts/atlas_docs.py).
SYNC_RECORD = Path(__file__).resolve().parents[2] / "docs" / "atlas" / "sync.json"


def synced_commit(record: Path = SYNC_RECORD) -> str | None:
    """The argus-atlas commit Atlas's docs were imported at, or None outside a checkout of this repo."""
    try:
        return json.loads(record.read_text())["atlas"]["commit"]
    except (OSError, ValueError, KeyError):
        return None


def image_for(commit: str) -> str:
    return f"argus-atlas:{commit[:7]}"


def build_steps(commit: str) -> str:
    """Shell lines: build the image at *commit* from the host's argus-atlas checkout, so every job.json records this
    commit; then refuse to start without it. The checkout itself, not a git worktree or a git-URL context: the build
    reads the version and commit from .git, which a worktree's .git file points outside the build context."""
    tag, short = image_for(commit), commit[:7]
    return (
        f"# 0. The image at argus-atlas {short}, the commit of the docs Atlas publishes (once per host), built from the\n"
        f"#    argus-atlas checkout itself (a worktree's or a git URL's build can't read the version from .git):\n"
        f"#   cd <argus-atlas checkout> && git fetch && git switch --detach {commit} && \\\n"
        f'#   docker build -t {tag} --build-arg EXTRAS="server,cli,tiles" . && git switch -\n'
        f"docker image inspect {tag} >/dev/null 2>&1 || {{ echo 'build {tag} first (step 0)'; exit 1; }}\n"
    )


def foreign_jobs_guard(prefix: str, commit: str) -> str:
    """Refuse to bake beside jobs of another argus-atlas build: `chunk` seams against any baked neighbour."""
    return (
        f"# Chunks of another argus-atlas build here would be seamed against: re-bake into a fresh ATLAS_MAPS.\n"
        f"foreign=$(grep -L {commit} {MAPS}/{prefix}*/*/job.json 2>/dev/null || true)\n"
        f'[ -z "$foreign" ] || {{ echo "jobs from another argus-atlas build under $ATLAS_MAPS/{prefix}:"; '
        f'echo "$foreign" | head -3; exit 1; }}\n'
    )


GPU_ENV = (
    "ARGUS_GPU_COORDINATOR",
    "ARGUS_GPU_BROKER_URL",
    "ARGUS_GPU_BROKER_TOKEN",
    "ARGUS_GPU_BROKER_FALLBACK",
    "ARGUS_GPU_LEASE_TIMEOUT_S",
    "ATLAS_GPU_PRIORITY",
)

# ComfyUI must answer before the chunks start. Through iris it may be idle-stopped: wake it with iris's warm action
# (on the host, so IRIS_URL is the broker as the host sees it).
COMFYUI_CHECK = (
    "curl -sf -m 5 http://127.0.0.1:8188/system_stats >/dev/null || {\n"
    '  [ "${ARGUS_GPU_COORDINATOR:-}" = broker ] && curl -sf -m 180 -X POST '
    '-H "Authorization: Bearer ${ARGUS_GPU_BROKER_TOKEN:-}" "${IRIS_URL:-http://127.0.0.1:8120}/services/comfyui/warm" '
    ">/dev/null; } || { echo 'ComfyUI is not answering on :8188'; exit 1; }\n"
)


def run_line(image: str) -> str:
    return (
        'RUN="docker run --rm -u $(id -u):$(id -g) -w /tmp --add-host host.docker.internal:host-gateway '
        "-e COMFYUI_BASE_URL=http://host.docker.internal:8188 -e ATLAS_ALLOW_BAKE=1 "
        "-e ATLAS_RECIPES_DIR=/data/recipes -e ATLAS_MODELS_DIR=/data/models -e ATLAS_JOBS_DIR=/data/out/atlas "
        # GPU leases (argus-iris): forwarded by name, so only what the host sets reaches the container and no
        # secret is ever written into a plan. scripts/run_bakes.sh sets them for a background re-bake.
        f"{' '.join(f'-e {name}' for name in GPU_ENV)} -e ARGUS_GPU_TENANT_MANIFEST=/app/gpu-tenant.yaml "
        "-v $ATLAS_HOST/dem:/data/dem -v $ATLAS_HOST/recipes:/data/recipes:ro -v $ATLAS_MODELS:/data/models:ro "
        f'-v $ATLAS_MAPS:/data/out/atlas {image}"'
    )


OSM_LAYERS = "water,rivers,roads,forest,urban"
# The land test: a chunk with no H3 cell of this resolution centred on land holds no land, so it is not baked
# (atlas_realms.seed.sea_chunks counts the same chunks as open sea).
SEA_TEST_RES = 7
# Values written unquoted into the plan (paths, `sed`, `test -f`) must be plain words, so a plan can't be broken or made
# to run something else by a name with a space, `;`, `$()`, a quote or a `/`.
WORD = re.compile(r"[A-Za-z0-9_-]+")
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*")
SHA = re.compile(r"[0-9a-f]{40}")
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


def glo30_tiles(bbox: tuple[float, float, float, float]) -> int:
    """How many 1-degree Copernicus GLO-30 tiles a lon/lat box touches (an upper bound: all-sea ones are absent)."""
    west, south, east, north = bbox
    return (math.ceil(east) - math.floor(west)) * (math.ceil(north) - math.floor(south))


def land_cells(path: Path, res: int) -> set[str]:
    cells: set[str] = set()
    for f in json.loads(path.read_text())["features"]:
        cells |= set(h3.geo_to_cells(f["geometry"], res))
    return cells


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--name", required=True, help="Region name: OSM extract dir and job group")
    p.add_argument("--center", help="lat,lng of the block's centre chunk (or --tiles)")
    p.add_argument("--tiles", help="Comma-separated z/x/y chunks to bake instead of a block (all at --zoom)")
    p.add_argument("--rings", type=int, default=1, help="Chunk rings around the centre (1 = 3x3, 2 = 5x5)")
    p.add_argument("--zoom", type=int, default=DEFAULT.chunk_zoom)
    p.add_argument("--size", type=int, default=DEFAULT.chunk_size)
    p.add_argument("--margin", type=int, default=DEFAULT.chunk_margin)
    p.add_argument("--styles", default="antique", help="Comma-separated recipe names")
    p.add_argument("--land", type=Path, help="Land polygons (GeoJSON, EPSG:4326): skip all-sea chunks, count hexes")
    p.add_argument(
        "--commit", default=synced_commit(), help="argus-atlas commit to bake with (default: docs/atlas/sync.json)"
    )
    p.add_argument("--image", help="A prebuilt argus-atlas image instead (its job.json must record --commit)")
    p.add_argument("--area", action="store_true", help="Write chunks to areas/<name>/<style>/ (Atlas and the arena)")
    p.add_argument("--layers", default=OSM_LAYERS, help="OSM control layers")
    p.add_argument("--hex-res", type=int, default=DEFAULT.h3_resolution, help="H3 resolution for hex counts")
    a = p.parse_args(argv)

    if not WORD.fullmatch(a.name):
        p.error("--name must be letters, digits, '-' or '_'")
    styles = [s for s in a.styles.split(",") if s]
    if not styles:
        p.error("--styles names no style")
    if not all(WORD.fullmatch(v) for v in styles + a.layers.split(",")):
        p.error("--styles and --layers must be comma-separated names of letters, digits, '-' or '_'")
    if a.image and not IMAGE.fullmatch(a.image):
        p.error("--image must be a plain image reference")
    if not a.commit or not SHA.fullmatch(a.commit):
        p.error("--commit must be a full argus-atlas sha (default: docs/atlas/sync.json)")
    if a.tiles:
        block = [tuple(int(v) for v in t.split("/")) for t in a.tiles.split(",") if t]
        if any(len(t) != 3 or t[0] != a.zoom for t in block):
            p.error(f"--tiles must be z/x/y chunks at --zoom {a.zoom}")
        if len(set(block)) != len(block):
            p.error("--tiles lists a chunk twice")
    elif a.center:
        lat, lng = (float(v) for v in a.center.split(","))
        centre = home_chunk(h3.latlng_to_cell(lat, lng, 9), a.zoom)
        block = lookahead(centre, a.rings)
    else:
        p.error("give --center (a block) or --tiles (a chunk list)")
    prefix = f"areas/{a.name}/" if a.area else ""

    notes = []
    if a.land and not a.tiles:
        fine = land_cells(a.land, SEA_TEST_RES)
        with_land = {t for c in fine for t in cell_chunks(c, a.zoom)}
        skipped = sorted(set(block) - with_land)
        block = [t for t in block if t in with_land]
        hexes = [c for c in land_cells(a.land, a.hex_res) if home_chunk(c, a.zoom) in set(block)]
        notes.append(f"all-sea chunks skipped: {len(skipped)} {[f'{z}/{x}/{y}' for z, x, y in skipped]}")
        notes.append(
            f"land hexes at H3 res {a.hex_res}: {len(hexes):,} (room for {len(hexes) // DEFAULT.hex_cap:,} players at {DEFAULT.hex_cap} each)"
        )
    elif a.tiles:
        notes.append(f"chunks listed with --tiles: {len(block)}")
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
    image = a.image or image_for(a.commit)
    if not a.image:
        out(build_steps(a.commit))
    out(foreign_jobs_guard(prefix, a.commit))
    out(run_line(image) + "\n\n")
    dem = f"/data/dem/{a.name}_glo30.tif"
    places = f"/data/dem/osm/{a.name}-places/places.geojson"
    out("# 1. DEM: Copernicus GLO-30 for the block plus margins, one GeoTIFF (all-sea tiles are filled with 0 m)\n")
    out(f"[ -f {HOST}/dem/{a.name}_glo30.tif ] || \\\n  ")
    budget = glo30_tiles(dem_bbox(bbox))
    more = f" --max-tiles {budget}" if budget > 16 else ""  # argus-atlas refuses more than 16 GLO-30 tiles by default
    out(
        f"$RUN argus-atlas dem --bbox {','.join(f'{v:.4f}' for v in dem_bbox(bbox))} --out {dem} --allow-missing{more}\n\n"
    )
    out(
        "# 2. OSM for the whole block in one Overpass call, and its place names (a separate extract: never a control)\n"
    )
    out(f"[ -f {HOST}/dem/osm/{a.name}/osm.json ] || \\\n  ")
    out(f"$RUN argus-atlas osm --bbox {bbox_s} --zoom {native} --layers {a.layers} --out /data/dem/osm/{a.name}\n")
    out(f"[ -f {HOST}/dem/osm/{a.name}-places/places.geojson ] || \\\n  ")
    out(f"$RUN argus-atlas osm --bbox {bbox_s} --zoom {native} --layers places --out /data/dem/osm/{a.name}-places\n\n")
    out("# 3. Recipes: a missing <style>.json is copied from antique.json with its name and preset swapped\n")
    out(f'test -f {HOST}/recipes/antique.json || {{ echo "missing $ATLAS_HOST/recipes/antique.json"; exit 1; }}\n')
    for s in (s for s in styles if s != "antique"):
        out(
            f'test -f {HOST}/recipes/{s}.json || sed \'s/"antique"/"{s}"/g\' {HOST}/recipes/antique.json > {HOST}/recipes/{s}.json\n'
        )
    layer_args = " ".join(f"--layer {n}=/data/dem/osm/{a.name}/{n}.geojson" for n in a.layers.split(","))
    out("\n# 4. Chunks, one style at a time, north to south, west to east. A re-run skips chunks that have tiles.\n")
    out(COMFYUI_CHECK)
    for s in styles:
        out(f"mkdir -p {MAPS}/{prefix}{s}\n")
        for z, x, y in tiles:
            out(f"[ -f {MAPS}/{prefix}{s}/{s}-{z}-{x}-{y}/fantasy.pmtiles ] || \\\n  ")
            out(
                f"$RUN argus-atlas chunk --tile {z}/{x}/{y} --recipe {s} --dem {dem} "
                f"--size {a.size} --margin {a.margin} --sea-level 0 {layer_args} "
                f"--out /data/out/atlas/{prefix}{s}/{s}-{z}-{x}-{y}\n"
            )
    # Names don't depend on the style, so one style's chunks carry them for all (atlas_realms.tiles merges them).
    out(f"\n# 5. Place names for every chunk, on the {styles[0]} jobs (a re-run skips labelled ones)\n")
    for z, x, y in tiles:
        job = f"{prefix}{styles[0]}/{styles[0]}-{z}-{x}-{y}"
        out(
            f"[ -f {MAPS}/{job}/labels.geojson ] || $RUN argus-atlas labels --job /data/out/atlas/{job} --source {places}\n"
        )
    out(f"\n# 6. Report timings: $RUN argus-atlas inspect /data/out/atlas/{prefix}<style>/<style>-<z>-<x>-<y>\n")


if __name__ == "__main__":
    main()
