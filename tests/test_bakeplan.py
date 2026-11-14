import io
from contextlib import redirect_stdout
from pathlib import Path

from atlas_realms.bakeplan import canvas_bbox, dem_bbox, main, raster_order

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
