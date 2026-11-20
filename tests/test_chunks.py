import h3
from argus_atlas.addressing import chunk_grids, chunk_native_zoom, validate_chunk
from argus_atlas.mercator import tile_bounds

from atlas_realms.rules.chunks import cell_chunks, chunk_job_id, home_chunk, lookahead
from atlas_realms.rules.tunables import DEFAULT

Z = DEFAULT.chunk_zoom
# The chunk geometry tests use res 7, where a hex is far smaller than a chunk.
RES = 7
WELLINGTON = h3.latlng_to_cell(-41.2865, 174.7762, RES)


def test_default_chunk_is_one_argus_atlas_accepts():
    validate_chunk(Z, DEFAULT.chunk_size, DEFAULT.chunk_margin)
    canvas, inner = chunk_grids(*home_chunk(WELLINGTON, Z), size=DEFAULT.chunk_size, margin=DEFAULT.chunk_margin)
    assert inner.width == DEFAULT.chunk_size
    assert canvas.width == DEFAULT.chunk_size + 2 * DEFAULT.chunk_margin <= 1536
    # The doc: zoom 10 tile at 1024 px is native zoom 12, about 38 m/px at the equator.
    assert chunk_native_zoom(Z, DEFAULT.chunk_size) == 12
    assert 38 < inner.pixel_width < 39


def test_wellington_home_chunk():
    # Wellington at z10: the centre of the Cook Strait starting block.
    assert home_chunk(WELLINGTON, Z) == (10, 1009, 641)
    assert chunk_job_id("antique", *home_chunk(WELLINGTON, Z)) == "antique-10-1009-641"


def test_home_chunk_contains_hex_centre():
    from argus_atlas.mercator import lonlat_to_mercator

    lat, lng = h3.cell_to_latlng(WELLINGTON)
    mx, my = lonlat_to_mercator(lng, lat)
    xmin, ymin, xmax, ymax = tile_bounds(*home_chunk(WELLINGTON, Z))
    assert xmin <= mx < xmax and ymin < my <= ymax


def test_interior_hex_needs_one_chunk():
    assert cell_chunks(WELLINGTON, Z) == [home_chunk(WELLINGTON, Z)]


def test_hex_on_a_chunk_edge_needs_both_chunks():
    # Walk east from Wellington until a hex straddles the next tile column.
    z, x, y = home_chunk(WELLINGTON, Z)
    cell = WELLINGTON
    for _ in range(40):
        chunks = cell_chunks(cell, Z)
        if len(chunks) > 1:
            break
        lat, lng = h3.cell_to_latlng(cell)
        cell = h3.latlng_to_cell(lat, lng + 0.02, RES)
    assert len(chunks) > 1
    assert home_chunk(cell, Z) in chunks
    assert all(abs(cx - x) <= 1 and abs(cy - y) <= 1 for _, cx, cy in chunks)


def test_lookahead_rings():
    t = (10, 1009, 641)
    assert lookahead(t, 0) == [t]
    assert len(lookahead(t, 1)) == 9
    assert len(lookahead(t, 2)) == 25
    assert len(lookahead((10, 0, 0), 1)) == 4  # clipped at the world edge, no wrap


def test_res5_hex_can_span_two_chunks():
    # A 17.6 km res-5 hex in a ~30 km chunk: Wellington's straddles the 10/1008 | 10/1009 edge.
    cell = h3.latlng_to_cell(-41.2865, 174.7762, 5)
    assert cell_chunks(cell, Z) == [(10, 1008, 641), (10, 1009, 641)]


def test_at_zoom_maps_coarse_and_fine_chunks_onto_the_game_zoom():
    from atlas_realms.rules.chunks import at_zoom

    assert at_zoom([(10, 5, 6)], 10) == {(10, 5, 6)}
    assert at_zoom([[9, 2, 3]], 10) == {(10, 4, 6), (10, 5, 6), (10, 4, 7), (10, 5, 7)}
    assert len(at_zoom([(7, 57, 31)], 10)) == 64
    kids = [(11, 8, 8), (11, 9, 8), (11, 8, 9)]
    assert at_zoom(kids, 10) == set()  # one child short
    assert at_zoom([*kids, (11, 9, 9)], 10) == {(10, 4, 4)}
