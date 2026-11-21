"""Splitting the grid to a finer H3 resolution (5 -> 6, later 6 -> 7).

Each held hex becomes its 7 children. A player keeps their capital and the hexes adjacent
to it, grown outward one ring at a time through their own children, up to the new hex cap:
the old one plus split_cap_growth (50%), rounded up, so 3 -> 5 and 12 -> 18 at the first split.
Everything else goes neutral. Children that turn out to be sea at the finer resolution are
lost: coastal attrition, by design.

Interpretations (not spelled out in the design decision):
- The new capital is the old capital's centre child; if that child is sea, the land child of
  the old capital nearest its centre. A capital with no land children is lost.
- "Adjacent" means connected through the player's own land children. Holdings cut off from
  the capital are lost even if the cap has room.
- A player without a capital keeps the same shape around the child nearest their unit,
  up to the no-capital cap.
"""

import math
from collections.abc import Callable, Iterable

import h3
from pydantic import BaseModel

from atlas_realms.rules.tunables import DEFAULT, Tunables

IsLand = Callable[[str], bool]


class SplitResult(BaseModel):
    kept: list[str]  # in the order they were kept, capital first when there is one
    capital: str | None


def after_split(t: Tunables, res: int) -> Tunables:
    """The tunables for the finer grid: the new resolution and the grown caps."""
    grow = t.split_cap_growth
    return t.model_copy(
        update={
            "h3_resolution": res,
            # Rounded first: 10 * 1.1 is 11.000000000000002 in floating point, which must still be 11.
            "hex_cap": math.ceil(round(t.hex_cap * grow, 9)),
            "hex_cap_with_capital": math.ceil(round(t.hex_cap_with_capital * grow, 9)),
        }
    )


def _nearest(cells: Iterable[str], to: str) -> str | None:
    return min(cells, key=lambda c: (h3.grid_distance(c, to), c), default=None)


def _grow(seed: str, pool: set[str], cap: int) -> list[str]:
    """Breadth-first from seed through pool, nearest ring first, ties by cell id."""
    kept, seen, ring = [seed], {seed}, [seed]
    while ring and len(kept) < cap:
        nxt = sorted({n for c in ring for n in h3.grid_ring(c, 1) if n in pool and n not in seen})
        nxt.sort(key=lambda c: (h3.grid_distance(c, seed), c))
        for c in nxt:
            if len(kept) == cap:
                break
            kept.append(c)
            seen.add(c)
        ring = nxt
    return kept


def split_holdings(
    held: Iterable[str],
    capital: str | None,
    unit_hex: str | None,
    is_land: IsLand,
    res: int,
    t: Tunables = DEFAULT,
) -> SplitResult:
    """One player's holdings after splitting to resolution `res`.

    `t` is the tunables before the split; caps come from `after_split(t, res)`.
    `is_land` is the land scoring at the new resolution.
    """
    new = after_split(t, res)
    held = set(held)
    pool = {c for p in held for c in h3.cell_to_children(p, res) if is_land(c)}
    if not pool:
        return SplitResult(kept=[], capital=None)

    new_capital = None
    if capital is not None and capital in held:
        centre = h3.cell_to_center_child(capital, res)
        own = [c for c in h3.cell_to_children(capital, res) if c in pool]
        new_capital = centre if centre in pool else _nearest(own, centre)

    if new_capital is not None:
        return SplitResult(kept=_grow(new_capital, pool, new.hex_cap_with_capital), capital=new_capital)

    anchor = h3.cell_to_center_child(unit_hex, res) if unit_hex and h3.get_resolution(unit_hex) < res else unit_hex
    seed = _nearest(pool, anchor) if anchor else min(pool)
    return SplitResult(kept=_grow(seed, pool, new.hex_cap), capital=None)
