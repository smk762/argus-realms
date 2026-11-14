import h3

from atlas_realms.rules.split import after_split, split_holdings
from atlas_realms.rules.tunables import DEFAULT, Tunables

SPLIT = after_split(DEFAULT, 6)

CAPITAL = h3.latlng_to_cell(-41.2865, 174.7762, 5)  # Wellington
RING = h3.grid_ring(CAPITAL, 1)


def land(_cell: str) -> bool:
    return True


def _connected(cells: list[str]) -> bool:
    seen, todo = {cells[0]}, [cells[0]]
    while todo:
        c = todo.pop()
        for n in h3.grid_ring(c, 1):
            if n in cells and n not in seen:
                seen.add(n)
                todo.append(n)
    return seen == set(cells)


def test_capital_becomes_centre_child_and_keeps_cap():
    r = split_holdings([CAPITAL, *RING[:3]], CAPITAL, None, land, 6)
    assert r.capital == h3.cell_to_center_child(CAPITAL, 6) == r.kept[0]
    assert len(r.kept) == SPLIT.hex_cap_with_capital
    assert _connected(r.kept)
    # The capital's own 7 children are the nearest, so they are all kept.
    assert set(h3.cell_to_children(CAPITAL, 6)) <= set(r.kept)


def test_kept_grow_outward_from_capital():
    r = split_holdings([CAPITAL, *RING], CAPITAL, None, land, 6)
    dists = [h3.grid_distance(r.capital, c) for c in r.kept]
    assert dists == sorted(dists)


def test_sea_children_are_lost():
    sea = set(h3.cell_to_children(RING[0], 6))
    r = split_holdings([CAPITAL, RING[0]], CAPITAL, None, lambda c: c not in sea, 6)
    assert not sea & set(r.kept)
    assert len(r.kept) == 7  # only the capital's own children remain


def test_capital_on_sea_centre_moves_to_nearest_land_child():
    centre = h3.cell_to_center_child(CAPITAL, 6)
    r = split_holdings([CAPITAL], CAPITAL, None, lambda c: c != centre, 6)
    assert r.capital is not None and r.capital != centre
    assert h3.cell_to_parent(r.capital, 5) == CAPITAL


def test_capital_with_no_land_children_is_lost():
    sea = set(h3.cell_to_children(CAPITAL, 6))
    r = split_holdings([CAPITAL, RING[0]], CAPITAL, None, lambda c: c not in sea, 6)
    assert r.capital is None
    assert len(r.kept) == SPLIT.hex_cap


def test_disconnected_holdings_are_lost():
    far = h3.grid_ring(CAPITAL, 3)[0]
    r = split_holdings([CAPITAL, far], CAPITAL, None, land, 6)
    assert len(r.kept) == 7
    assert not set(h3.cell_to_children(far, 6)) & set(r.kept)


def test_no_capital_keeps_cap_around_the_unit():
    unit = h3.cell_to_center_child(RING[2], 6)
    r = split_holdings([CAPITAL, *RING[:3]], None, unit, land, 6)
    assert r.capital is None
    assert r.kept[0] == unit and len(r.kept) == SPLIT.hex_cap
    assert _connected(r.kept)


def test_deterministic():
    a = split_holdings([CAPITAL, *RING], CAPITAL, None, land, 6)
    b = split_holdings(list(reversed([CAPITAL, *RING])), CAPITAL, None, land, 6)
    assert a == b


def test_all_sea_keeps_nothing():
    assert split_holdings([CAPITAL], CAPITAL, None, lambda c: False, 6).kept == []


def test_caps_grow_half_again_rounded_up():
    assert (SPLIT.h3_resolution, SPLIT.hex_cap, SPLIT.hex_cap_with_capital) == (6, 5, 18)
    again = after_split(SPLIT, 7)
    assert (again.hex_cap, again.hex_cap_with_capital) == (8, 27)
    assert after_split(Tunables(split_cap_growth=1.0), 6).hex_cap == DEFAULT.hex_cap
