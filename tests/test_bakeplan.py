import io
import json
import subprocess
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
    assert f"git switch --detach {commit} && \\\n#   docker build -t argus-atlas:{commit[:7]}" in script
    assert "git clone" not in script  # built from the host's checkout, not a clone
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
    with pytest.raises(SystemExit):
        main(["--name", "ice", "--tiles", "7/57/31,7/57/31", "--zoom", "7"])


def test_a_plan_refuses_chunks_of_another_zoom(tmp_path):
    """A finer re-bake beside the old chunks would overlap them: the plan stops before baking, not at the export."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--name", "ice", "--tiles", "8/113/69", "--zoom", "8", "--area", "--styles", "antique"])
    lines = buf.getvalue().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("other="))
    guard = "set -euo pipefail\n" + "\n".join(lines[start : start + 2]) + "\necho clear\n"

    def run(*jobs):
        maps = tmp_path / str(len(list(tmp_path.iterdir())))
        for j in jobs:
            (maps / "areas" / "ice" / "antique" / j).mkdir(parents=True)
            (maps / "areas" / "ice" / "antique" / j / "job.json").write_text("{}")
        maps.mkdir(exist_ok=True)
        env = {"ATLAS_MAPS": str(maps), "PATH": "/usr/bin:/bin"}
        return subprocess.run(["bash", "-c", guard], env=env, capture_output=True, text=True)

    assert run().stdout == "clear\n"
    assert run("antique-8-113-69").stdout == "clear\n"
    r = run("antique-8-113-69", "antique-7-56-34")
    assert r.returncode == 1 and "antique-7-56-34" in r.stdout


def test_a_dem_over_the_default_tile_budget_raises_it():
    assert glo30_tiles((-25.67, 62.74, -10.89, 67.75)) == 16 * 6  # Iceland
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--name", "ice", "--tiles", "7/55/32,7/59/33", "--zoom", "7", "--area"])
    assert "--allow-missing --max-tiles " in buf.getvalue()


def test_the_committed_plans_are_current():
    """bakes/<name>.sh match bakes/plans.json at the docs' argus-atlas commit: a docs import that moves the commit
    fails here until `make bake-plans` re-pins every plan."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    done = subprocess.run(
        [sys.executable, str(root / "scripts" / "bake_plans.py"), "--check"], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    commit = synced_commit()
    for plan in (root / "bakes").glob("*.sh"):
        assert f"argus-atlas:{commit[:7]}" in plan.read_text(), plan.name


def test_every_plan_is_an_atlas_area():
    import json
    import sys

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "scripts"))
    import build_static

    plans = {k for k in json.loads((root / "bakes" / "plans.json").read_text()) if not k.startswith("_")}
    assert plans == {p.stem for p in (root / "bakes").glob("*.sh")}
    assert plans == {a["id"] for a in build_static.AREA_INFO}  # baked or not, each has its label and frame


def test_plans_forward_the_gpu_lease_settings_by_name():
    """Bakes take argus-iris leases when the host sets ARGUS_GPU_*: forwarded by name, never written into a plan."""
    from atlas_realms.bakeplan import GPU_ENV

    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--name", "t", "--center=-41.29,174.78", "--rings", "1", "--land", str(NZ), "--area"])
    script = buf.getvalue()
    run = next(line for line in script.splitlines() if line.startswith("RUN="))
    for name in GPU_ENV:
        assert f" -e {name} " in run, name
    assert "ATLAS_GPU_PRIORITY" in GPU_ENV and "TOKEN=" not in run
    assert "/services/comfyui/warm" in script  # an idle-stopped ComfyUI is woken through iris


@pytest.mark.parametrize(
    "extra",
    [
        ["--name", "a b"],
        ["--name", "x;rm -rf ~"],
        ["--styles", "antique,a/b"],
        ["--styles", "$(id)"],
        ["--layers", "water,ro ads"],
        ["--image", "img; echo hi"],
        ["--styles", ""],
        ["--styles", ","],
        ["--size", "128"],
        ["--size", "768"],
    ],
)
def test_values_that_would_break_the_script_are_refused(extra):
    args = ["--name", "ok", "--tiles", "10/1009/641", *extra]
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        main(args)
    assert e.value.code == 2 and buf.getvalue() == ""  # refused before a line of the plan is printed


def test_whole_degree_and_antimeridian_boxes_count_their_dem_tiles():
    assert glo30_tiles((10.0, 20.0, 10.0, 21.0)) == 1  # zero width on a whole degree
    assert glo30_tiles((179.5, -17.5, -179.5, -16.5)) == 2 * 2  # 179..180 and -180..-179


@pytest.mark.parametrize("how", ["empty-tiles", "all-sea"])
def test_a_plan_with_no_chunks_is_refused(how, tmp_path):
    if how == "empty-tiles":
        args = ["--name", "ok", "--tiles", ","]
    else:  # land only far away from the block
        land = tmp_path / "land.geojson"
        ring = [[0, 0], [0.1, 0], [0.1, 0.1], [0, 0.1], [0, 0]]
        polygon = {"type": "Polygon", "coordinates": [ring]}
        land.write_text(
            json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": polygon}]})
        )
        args = ["--name", "ok", "--center", "-41.3,174.8", "--rings", "0", "--land", str(land)]
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        main(args)
    assert e.value.code == 2 and buf.getvalue() == ""
