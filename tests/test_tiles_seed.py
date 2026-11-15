import json
from pathlib import Path

import h3
import pytest
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

from atlas_realms.rules.chunks import cell_chunks
from atlas_realms.seed import land_cells, plan_hexes, sea_chunks
from atlas_realms.tiles import export

NZ = Path(__file__).parent.parent / "data" / "nz_mainland.geojson"
WELLINGTON = (10, 1009, 641)


def _png(t, payload: bytes) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (256, 256), (t[1] % 256, t[2] % 256, len(payload))).save(buf, "PNG")
    return buf.getvalue()


def write_chunk(root: Path, style: str, tile: tuple[int, int, int], payload: bytes = b"") -> None:
    z, x, y = tile
    job = root / style / f"{style}-{z}-{x}-{y}"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"chunk": {"z": z, "x": x, "y": y}}))
    tiles = [(z, x, y)] + [(z + 1, 2 * x + dx, 2 * y + dy) for dx in (0, 1) for dy in (0, 1)]
    with (job / "fantasy.pmtiles").open("wb") as f:
        w = Writer(f)
        for t in sorted(tiles, key=lambda t: zxy_to_tileid(*t)):
            w.write_tile(zxy_to_tileid(*t), _png(t, payload))
        w.finalize(
            {
                "tile_type": TileType.PNG,
                "tile_compression": Compression.NONE,
                "min_zoom": z,
                "max_zoom": z + 1,
                "min_lon_e7": 1740000000,
                "min_lat_e7": -413000000,
                "max_lon_e7": 1750000000,
                "max_lat_e7": -412000000,
                "center_zoom": z,
                "center_lon_e7": 1745000000,
                "center_lat_e7": -412500000,
            },
            {},
        )


def test_export_writes_static_tiles_and_manifest(tmp_path):
    maps, out = tmp_path / "maps", tmp_path / "tiles"
    write_chunk(maps, "antique", WELLINGTON)
    write_chunk(maps, "antique", (10, 1010, 641))
    write_chunk(maps, "tolkien", WELLINGTON)
    m = export(maps, out, patron_only={"tolkien"})
    assert (out / "antique/10/1009/641.png").read_bytes().startswith(b"\x89PNG")
    assert (out / "antique/11/2019/1283.png").exists()
    # 10 chunk tiles, plus overviews: 2 at zoom 9 (x 504, 505), then 1 each at zooms 8 to 5.
    assert len(list((out / "antique").rglob("*.png"))) == 10 + 2 + 4
    assert (out / "antique/5/31/20.png").exists()
    assert m["ready_chunks"] == [WELLINGTON]  # the only chunk baked in both styles
    assert [s["name"] for s in m["styles"]] == ["antique", "tolkien"] and m["styles"][1]["patron_only"]
    assert (m["minzoom"], m["chunk_zoom"], m["maxzoom"]) == (5, 10, 11)
    assert json.loads((out / "manifest.json").read_text())["tile_url"] == "tiles/{style}/{z}/{x}/{y}.png"
    assert m["labels_url"] is None  # no chunk was labelled


def _labels(job: Path, *names: tuple[str, float, float]) -> None:
    features = [
        {
            "type": "Feature",
            "properties": {"name": n, "rank": 1, "kind": "town"},
            "geometry": {"type": "Point", "coordinates": [x, y]},
        }
        for n, x, y in names
    ]
    (job / "labels.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}))


def test_export_merges_chunk_labels_once(tmp_path):
    maps, out = tmp_path / "maps", tmp_path / "tiles"
    write_chunk(maps, "antique", WELLINGTON)
    write_chunk(maps, "antique", (10, 1010, 641))
    write_chunk(maps, "tolkien", WELLINGTON)
    _labels(maps / "antique/antique-10-1009-641", ("Wellington", 174.7762, -41.2865))
    _labels(maps / "antique/antique-10-1010-641", ("Masterton", 175.6573, -40.9511))
    _labels(maps / "tolkien/tolkien-10-1009-641", ("Wellington", 174.7762, -41.2865))  # same name, other style
    m = export(maps, out)
    assert m["labels_url"] == "tiles/labels.geojson"
    names = [f["properties"]["name"] for f in json.loads((out / "labels.geojson").read_text())["features"]]
    assert sorted(names) == ["Masterton", "Wellington"]


def test_export_refuses_tiles_that_disagree(tmp_path):
    maps = tmp_path / "maps"
    write_chunk(maps, "antique", WELLINGTON)
    write_chunk(maps / "dup", "antique", WELLINGTON, payload=b"other")
    (maps / "dup" / "antique" / "antique-10-1009-641").rename(maps / "antique" / "antique-10-1009-641-b")
    with pytest.raises(ValueError, match="disagrees"):
        export(maps, tmp_path / "tiles")


def test_plan_hexes_fog_land_and_spawns():
    land = land_cells(NZ, 5)
    ready = {(10, x, y) for x in range(1007, 1012) for y in range(639, 644)}
    rows = plan_hexes(ready, land, [(-41.2865, 174.7762)])
    by = {r["h3"]: r for r in rows}
    spawn = [r for r in rows if r["zone"] == "spawn"]
    assert len(spawn) == 1 and spawn[0]["h3"] in land  # snapped off Wellington's water hex
    for r in rows:
        assert r["playable"] == all(c in ready for c in cell_chunks(r["h3"], 10))
        assert (r["zone"] == "water") == (r["h3"] not in land)
    assert any(not r["playable"] for r in rows) and any(r["playable"] for r in rows)
    wellington_water = h3.latlng_to_cell(-41.2865, 174.7762, 5)
    assert by[wellington_water]["zone"] == "water"


def test_plan_hexes_rejects_spawn_far_from_land():
    land = land_cells(NZ, 5)
    with pytest.raises(ValueError, match="not on painted land"):
        plan_hexes({WELLINGTON}, land, [(-45.0, 178.0)])


# -- open sea inside the painted block ------------------------------------------------------------

COOK_STRAIT = json.loads((Path(__file__).parent.parent / "arena" / "world" / "cook-strait.json").read_text())
BAKED = {tuple(c) for c in COOK_STRAIT["ready_chunks"]}


def test_sea_chunks_are_the_ones_the_bake_plan_skipped_as_all_sea():
    plan = (Path(__file__).parent.parent / "bakes" / "cook-strait.sh").read_text()
    skipped = plan.split("all-sea chunks skipped: 19 ")[1].split("]")[0]
    want = {tuple(int(v) for v in s.strip(" '[").split("/")) for s in skipped.split(",")}
    assert sea_chunks(BAKED, NZ, 10) == want


def test_an_unbaked_land_chunk_is_not_sea():
    partial = BAKED - {WELLINGTON}  # a land chunk whose bake hasn't finished
    sea = sea_chunks(partial, NZ, 10)
    assert WELLINGTON not in sea and len(sea) == 19
    assert sea_chunks(set(), NZ, 10) == set()
    assert sea_chunks({WELLINGTON}, NZ, 10) == set()  # a one-chunk block has no candidates


def test_the_strait_is_open_water_in_the_solo_world():
    from atlas_realms.solo import build_world

    w = build_world(COOK_STRAIT, NZ, [(-41.2865, 174.7762), (-41.2906, 174.0010)])
    sea = sea_chunks(BAKED, NZ, 10)
    assert w.ready_chunks == BAKED | sea
    mid_strait = h3.latlng_to_cell(-41.6, 174.6, 5)  # chunk 10/1008/642: never baked, all sea
    assert w.hexes[mid_strait].playable and w.hexes[mid_strait].zone == "water"
    assert all(h.zone == "water" for c, h in w.hexes.items() if all(ch in sea for ch in cell_chunks(c, 10)))
