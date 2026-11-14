import h3
import pytest

from atlas_realms.rules.movement import advance, cost_floor, find_path, hex_minutes
from atlas_realms.rules.tunables import DEFAULT, Tunables

# Wellington, NZ (the starting region) at the game's resolution.
ORIGIN = h3.latlng_to_cell(-41.2865, 174.7762, 7)


def _line(n: int) -> list[str]:
    """n cells walking away from ORIGIN in a straight line."""
    far = h3.grid_ring(ORIGIN, n)[0]
    return h3.grid_path_cells(ORIGIN, far)[1:]


def flat(_cell: str) -> float:
    return 1.0


def never(_cell: str) -> bool:
    return False


def test_path_is_shortest_on_flat_ground():
    goal = h3.grid_ring(ORIGIN, 5)[3]
    path = find_path(ORIGIN, goal, flat, floor=1.0)
    assert path is not None and len(path) == 5 and path[-1] == goal
    cells = [ORIGIN, *path]
    assert all(h3.grid_distance(a, b) == 1 for a, b in zip(cells, cells[1:], strict=False))


def test_path_avoids_blocked_cells():
    goal = h3.grid_ring(ORIGIN, 4)[0]
    wall = set(h3.grid_path_cells(ORIGIN, goal)[1:-1])
    path = find_path(ORIGIN, goal, lambda c: None if c in wall else 1.0, floor=1.0)
    assert path is not None and path[-1] == goal
    assert not wall & set(path)


def test_path_prefers_cheap_land_and_avoids_water():
    goal = h3.grid_ring(ORIGIN, 4)[0]
    direct = set(h3.grid_path_cells(ORIGIN, goal)[1:-1])
    path = find_path(ORIGIN, goal, lambda c: 2.0 if c in direct else 1.0, floor=1.0)
    assert path is not None and not direct & set(path)


def test_unreachable_goal():
    goal = h3.grid_ring(ORIGIN, 3)[0]
    assert find_path(ORIGIN, goal, lambda c: None if c == goal else 1.0) is None
    ring = set(h3.grid_ring(ORIGIN, 1))
    assert find_path(ORIGIN, goal, lambda c: None if c in ring else 1.0) is None


def test_trivial_path():
    assert find_path(ORIGIN, ORIGIN, flat) == []


@pytest.mark.parametrize(
    ("cost", "march", "steps"),
    [(1.0, False, 1), (1.0, True, 2), (0.5, False, 2), (0.5, True, 4), (2.0, True, 1)],
)
def test_speed_table(cost, march, steps):
    r = advance(_line(10), 0.0, lambda _c: cost, never, march=march)
    assert len(r.steps) == steps


def test_water_takes_two_ticks_walking():
    path = _line(3)
    r1 = advance(path, 0.0, lambda _c: 2.0, never)
    assert r1.steps == [] and r1.progress == 1.0
    r2 = advance(r1.path, r1.progress, lambda _c: 2.0, never)
    assert r2.steps == path[:1] and r2.progress == 0.0


def test_contact_stops_before_entering():
    path = _line(6)
    enemy = path[2]
    r = advance(path, 0.0, lambda _c: 0.25, lambda c: c == enemy, march=True)
    assert r.steps == path[:2] and r.contact == enemy and r.path[0] == enemy


def test_blocked_mid_route():
    path = _line(4)
    r = advance(path, 0.0, lambda c: None if c == path[1] else 0.5, never)
    assert r.steps == path[:1] and r.blocked


def test_arrival_drops_leftover_progress():
    path = _line(1)
    r = advance(path, 0.0, lambda _c: 0.5, never, march=True)
    assert r.steps == path and r.path == [] and r.progress == 0.0


def test_walking_is_100_kmh():
    # Res 5: ~17.1 km centre to centre (H3 global mean) at 100 km/h.
    assert DEFAULT.walk_kmh == 100
    assert hex_minutes() == pytest.approx(10.24, abs=0.01)
    assert hex_minutes(Tunables(h3_resolution=6)) == pytest.approx(3.87, abs=0.01)
    assert hex_minutes(Tunables(walk_kmh=50)) == pytest.approx(2 * hex_minutes())


def test_res5_hex_takes_eleven_ticks_walking_and_six_marching():
    cost = hex_minutes()
    path = _line(2)
    walk = [advance(path, p, lambda _c: cost, never) for p in (0.0, 9.0, 10.0)]
    assert [len(r.steps) for r in walk] == [0, 0, 1]  # progress 1, 10, 11 minutes
    march = [advance(path, p, lambda _c: cost, never, march=True) for p in (0.0, 8.0, 9.0)]
    assert [len(r.steps) for r in march] == [0, 0, 1]  # progress 2, 10, 11


def test_default_floor_is_cheapest_hex():
    assert cost_floor() == pytest.approx(hex_minutes() * DEFAULT.cost_own_land)
    start = ORIGIN
    goal = h3.grid_ring(start, 4)[0]
    path = find_path(start, goal, lambda _c: hex_minutes())
    assert path is not None and len(path) == 4
