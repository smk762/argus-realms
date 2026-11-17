import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from atlas_realms.bakeplan import canvas_bbox, dem_bbox, glo30_tiles, main, raster_order, synced_commit

NZ = Path(__file__).parent.parent / "data" / "nz_mainland.geojson"


def test_raster_order_is_north_to_south_then_west_to_east():
    tiles = [(10, 1010, 641), (10, 1008, 642), (10, 1009, 640), (10, 1008, 640)]
    assert raster_order(tiles) == [(10, 1008, 640), (10, 1009, 640), (10, 1010, 641), (10, 1008, 642)]


def test_dem_bbox_pads_past_the_canvas():
    # argus-atlas `dem` snaps inward to its 1 arc-second lattice; the pad must outweigh that.
    west, south, east, north = canvas_bbox([(10, 1009, 641)], 1024, 128)
    w, s, e, n = dem_bbox((west, south, east, north))
    assert min(west - w, south - s, e - east, n - north) > 1 / 3600


def test_canvas_bbox_includes_margin():
    west, south, east, north = canvas_bbox([(10, 1009, 641)], 1024, 128)
    w0, s0, e0, n0 = canvas_bbox([(10, 1009, 641)], 1024, 0)
    assert west < w0 and south < s0 and east > e0 and north > n0


def test_plan_skips_sea_and_writes_one_chunk_per_style():
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(
            ["--name", "t", "--center=-41.29,174.78", "--rings", "1", "--styles", "antique,tolkien", "--land", str(NZ)]
        )
    script = buf.getvalue()
    assert "7 chunks x 2 styles" in script
    assert script.count("argus-atlas chunk ") == 14
    assert "--out /data/out/atlas/tolkien/tolkien-10-1009-641" in script
    assert "-v $ATLAS_MAPS:/data/out/atlas" in script
    assert ': "${ATLAS_HOST:?}" "${ATLAS_MAPS:?}" "${ATLAS_MODELS:?}"' in script
    assert "10/1008/642" not in script.split("# 4.")[1]  # all sea
    assert "argus-atlas dem --bbox " in script and "--dem /data/dem/t_glo30.tif " in script
    # Place names: their own extract, never a control layer, labelled once per chunk on the first style.
    assert "--layers places --out /data/dem/osm/t-places" in script
    assert "--layer places=" not in script
    assert script.count("argus-atlas labels --job /data/out/atlas/antique/") == 7
    assert "labels --job /data/out/atlas/tolkien/" not in script


def test_area_plan_writes_under_areas_with_a_prebuilt_image():
    buf = io.StringIO()
    commit = "b" * 40
    with redirect_stdout(buf):
        main(
            ["--name", "t", "--center=-41.29,174.78", "--rings", "1", "--styles", "antique,tolkien", "--land", str(NZ)]
            + ["--area", "--image", "ghcr.io/smk762/argus-atlas:0.4.0", "--commit", commit]
        )
    script = buf.getvalue()
    assert "-v $ATLAS_MAPS:/data/out/atlas ghcr.io/smk762/argus-atlas:0.4.0" in script
    assert "docker build" not in script  # a prebuilt image is pulled, not built
    assert 'mkdir -p "$ATLAS_MAPS"/areas/t/tolkien' in script
    assert "--out /data/out/atlas/areas/t/tolkien/tolkien-10-1009-641" in script
    assert f'grep -L {commit} "$ATLAS_MAPS"/areas/t/*/*/job.json' in script  # no chunks beside another build's


def test_the_default_build_is_the_docs_commit():
    """Tiles and docs from one argus-atlas build: the plan builds the image at docs/atlas/sync.json's commit."""
    commit = synced_commit()
    assert commit and len(commit) == 40
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--name", "t", "--center=-41.29,174.78", "--rings", "1", "--land", str(NZ), "--area"])
    script = buf.getvalue()
    assert f"git checkout {commit} && docker build -t argus-atlas:{commit[:7]}" in script
    assert f"docker image inspect argus-atlas:{commit[:7]}" in script
    assert f"-v $ATLAS_MAPS:/data/out/atlas argus-atlas:{commit[:7]}" in script
    assert script.index("docker image inspect") < script.index("argus-atlas dem")


def test_a_chunk_list_bakes_exactly_those_chunks():
    buf = io.StringIO()
    tiles = "7/57/31,7/55/32,7/56/32"
    with redirect_stdout(buf):
        main(["--name", "ice", "--tiles", tiles, "--zoom", "7", "--area", "--styles", "antique"])
    chunks = [
        line.split("--tile ")[1].split()[0] for line in buf.getvalue().splitlines() if "argus-atlas chunk" in line
    ]
    assert chunks == ["7/57/31", "7/55/32", "7/56/32"]  # north to south, then west to east
    assert "chunks listed with --tiles: 3" in buf.getvalue()
    with pytest.raises(SystemExit):
        main(["--name", "ice", "--tiles", "8/57/31", "--zoom", "7"])
    with pytest.raises(SystemExit):
        main(["--name", "ice", "--zoom", "7"])


def test_a_dem_over_the_default_tile_budget_raises_it():
    assert glo30_tiles((-25.67, 62.74, -10.89, 67.75)) == 16 * 6  # Iceland
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--name", "ice", "--tiles", "7/55/32,7/59/33", "--zoom", "7", "--area"])
    assert "--allow-missing --max-tiles " in buf.getvalue()
