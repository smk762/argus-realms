"""The arena's build-time tools (arena/build.py, sea_fog.py, tiles_webp.py) on small synthetic inputs."""

import json
import os
import statistics
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "arena"))

import build  # noqa: E402
import sea_fog  # noqa: E402
import tiles_webp  # noqa: E402

PIL = pytest.importorskip("PIL.Image")


def _png(path: Path, colour=(20, 60, 120)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    PIL.new("RGB", (16, 16), colour).save(path)


# -- sea_fog ---------------------------------------------------------------------------------------


def test_an_export_with_no_chunks_has_no_sea_sample(tmp_path):
    assert sea_fog.sea_sample(tmp_path, "antique", {"chunk_zoom": 10, "maxzoom": 12, "ready_chunks": []}) is None


@pytest.mark.parametrize("lat", [-90.0, 90.0, -91.0])
def test_mercator_clamps_at_the_poles(lat):
    _, y = sea_fog._merc(0.0, lat)
    assert 0.0 <= y <= 1.0


def test_histogram_median_matches_statistics_median():
    import random

    rng = random.Random(1)
    for n in (1, 2, 7, 100):
        values = [rng.randrange(256) for _ in range(n)]
        counts = [values.count(v) for v in range(256)]
        assert sea_fog._median(counts) == statistics.median(values)


# -- tiles_webp ------------------------------------------------------------------------------------


def test_a_style_with_no_tiles_is_a_clear_error(tmp_path):
    (tmp_path / "antique").mkdir()
    with pytest.raises(FileNotFoundError, match="antique"):
        tiles_webp.export(tmp_path, "antique", tmp_path / "out")


def test_stray_pngs_are_not_tiles(tmp_path):
    _png(tmp_path / "antique" / "10" / "1009" / "641.png")
    _png(tmp_path / "antique" / "legend.png")
    _png(tmp_path / "antique" / "preview" / "a" / "b.png")
    r = tiles_webp.export(tmp_path, "antique", tmp_path / "out")
    assert (r["tiles"], r["minzoom"], r["maxzoom"]) == (1, 10, 10)
    assert [p.relative_to(tmp_path / "out").as_posix() for p in (tmp_path / "out").rglob("*.webp")] == [
        "10/1009/641.webp"
    ]


# -- build -----------------------------------------------------------------------------------------


def _regions(tmp_path: Path) -> list[dict]:
    regions = []
    for rid in ("north", "south"):
        d = tmp_path / rid
        d.mkdir()
        (d / "outline.geojson").write_text(json.dumps({"region": rid}))  # same file name in both
        (d / "world.json").write_text("{}")
        regions.append({"id": rid, "title": rid, "manifest": str(d / "world.json"), "outline": str(d / "outline.geojson"),
                        "spawns": []})  # fmt: skip
    return regions


def test_each_region_keeps_its_own_outline_in_the_zip(tmp_path):
    regions = _regions(tmp_path)
    name = build.python_zip(tmp_path / "out", regions)
    with zipfile.ZipFile(tmp_path / "out" / name) as z:
        assert len(z.namelist()) == len(set(z.namelist()))
        index = json.loads(z.read("data/regions.json"))["regions"]
        for r in index:
            assert json.loads(z.read(f"data/{r['outline']}")) == {"region": r["id"]}


def test_the_zip_name_depends_on_content_not_timestamps(tmp_path):
    regions = _regions(tmp_path)
    a = build.python_zip(tmp_path / "a", regions)
    os.utime(regions[0]["outline"], (1_000_000_000, 1_000_000_000))
    assert build.python_zip(tmp_path / "b", regions) == a


def test_exactly_the_file_limit_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(tiles_webp, "PAGES_FILES", 3)
    files = []
    for i in range(3):
        files.append(tmp_path / f"{i}.txt")
        files[-1].write_text("x")
    build.check_pages_limits(tmp_path, files)  # 3 of 3: fine
    files.append(tmp_path / "3.txt")
    files[-1].write_text("x")
    with pytest.raises(SystemExit):
        build.check_pages_limits(tmp_path, files)


def test_copying_tiles_again_replaces_the_previous_build(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "HERE", tmp_path / "arena")  # keep the encode cache out of the repo
    tiles, dest = tmp_path / "tiles", tmp_path / "dist"
    _png(tiles / "antique" / "10" / "1009" / "641.png")
    manifest = {
        "chunk_zoom": 10,
        "maxzoom": 10,
        "ready_chunks": [],
        "styles": [{"name": "antique"}],
        "labels_url": None,
    }
    outline = ROOT / "data" / "nz_mainland.geojson"
    first = build.copy_tiles(tiles, dest, manifest, outline)
    (dest / "antique" / "9" / "1" / "1.webp").parent.mkdir(parents=True)
    (dest / "antique" / "9" / "1" / "1.webp").write_bytes(b"stale")
    assert build.copy_tiles(tiles, dest, manifest, outline) == first
    assert not (dest / "antique" / "9").exists()
