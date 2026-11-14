"""Pathing and per-tick stepping over H3 cells.

The rules don't know about zones or owners: callers pass `cost(cell) -> float | None`, the
minutes of progress a walking unit needs to enter that cell (None = can't enter, e.g. fog
or a hex that isn't playable yet). Double march doubles the progress a unit earns per tick
rather than halving every cost, which gives the doc's speed table.
"""

import heapq
import math
from collections.abc import Callable

import h3
from pydantic import BaseModel

from atlas_realms.rules.tunables import DEFAULT, Tunables

CostFn = Callable[[str], float | None]
ContactFn = Callable[[str], bool]


def hex_minutes(t: Tunables = DEFAULT) -> float:
    """Minutes to walk one hex of the current resolution on neutral land at walk_kmh.

    Uses H3's global mean edge length, so the cost is the same everywhere and needs no
    geometry at tick time. Centre to centre across a flat side is sqrt(3) x edge.
    """
    km = math.sqrt(3) * h3.average_hexagon_edge_length(t.h3_resolution, "km")
    return km / t.walk_kmh * 60


def cost_floor(t: Tunables = DEFAULT) -> float:
    """The lowest cost any hex can have: the A* heuristic's per-hex floor."""
    return hex_minutes(t) * min(t.cost_land, t.cost_own_land, t.cost_water)


def find_path(
    start: str, goal: str, cost: CostFn, floor: float | None = None, max_expansions: int = 50_000
) -> list[str] | None:
    """Cheapest route from start to goal, excluding start. None if unreachable.

    A* with grid distance x the cheapest cost seen so far is not admissible once a cheaper
    cell turns up, so the heuristic uses a fixed floor (default: `cost_floor()`), which must
    not exceed the cheapest cost `cost` can return.
    """
    if start == goal:
        return []
    if cost(goal) is None:
        return None
    if floor is None:
        floor = cost_floor()
    frontier: list[tuple[float, float, str]] = [(0.0, 0.0, start)]
    came_from: dict[str, str] = {}
    best: dict[str, float] = {start: 0.0}
    expansions = 0
    while frontier:
        _, g, cell = heapq.heappop(frontier)
        if cell == goal:
            return _rebuild(came_from, goal, start)
        if g > best.get(cell, float("inf")):
            continue
        expansions += 1
        if expansions > max_expansions:
            return None
        for nxt in h3.grid_ring(cell, 1):
            c = cost(nxt)
            if c is None:
                continue
            ng = g + c
            if ng < best.get(nxt, float("inf")):
                best[nxt] = ng
                came_from[nxt] = cell
                heapq.heappush(frontier, (ng + floor * h3.grid_distance(nxt, goal), ng, nxt))
    return None


def _rebuild(came_from: dict[str, str], goal: str, start: str) -> list[str]:
    path = [goal]
    while path[-1] in came_from and came_from[path[-1]] != start:
        path.append(came_from[path[-1]])
    path.reverse()
    return path


class StepResult(BaseModel):
    steps: list[str]  # cells entered this tick, in order
    path: list[str]  # what is left of the route
    progress: float
    contact: str | None = None  # next cell holds a unit to fight; the unit has not entered it
    blocked: bool = False  # next cell became unenterable; the route needs replanning


def advance(
    path: list[str],
    progress: float,
    cost: CostFn,
    contact: ContactFn,
    minutes: float = 1.0,
    march: bool = False,
) -> StepResult:
    """Earn one tick of progress and take as many steps as it pays for.

    At cost 0.25 (own land, marching) that is up to 4 steps, and contact is checked before
    each one. Reaching the end of the route, or making contact, drops leftover progress.
    """
    progress += minutes * (2 if march else 1)
    path = list(path)
    steps: list[str] = []
    while path:
        nxt = path[0]
        c = cost(nxt)
        if c is None:
            return StepResult(steps=steps, path=path, progress=0.0, blocked=True)
        if progress < c:
            break
        if contact(nxt):
            return StepResult(steps=steps, path=path, progress=0.0, contact=nxt)
        progress -= c
        steps.append(path.pop(0))
    if not path:
        progress = 0.0
    return StepResult(steps=steps, path=path, progress=progress)
