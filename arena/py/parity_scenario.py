"""A scripted solo game through `arena_bridge`, for the parity test.

CPython (the reference, from `src/`) and Pyodide (the browser bundle) both run this file; `arena/test/parity.test.mjs`
compares their output. Same seed, same orders at the same ticks, so every tick's state must hash the same. It also
saves mid-game, restores into a fresh Arena and plays on, which must end where the uninterrupted game ends.

    python arena/py/parity_scenario.py --ticks 400 --region iceland > cpython.json
"""

import hashlib
import json
from pathlib import Path

import h3
from arena_bridge import Arena, load_regions

SEED = "5ea1" * 16  # 32 bytes
TICK_EVERY = 0.25  # the fastest setting: period = 0.25 / PACE seconds
T0 = 1_700_000_000.0  # a wall clock in seconds; any fixed value


def _orders(st: dict, hexes: dict) -> list[tuple[str, dict]]:
    """The human's orders for this tick: a fixed script, deterministic given the state."""
    tick, me = st["tick"], next(u for u in st["units"] if u["user_id"] == st["you"])
    land = sorted(c for c, z in hexes.items() if z == "land")
    if tick == 0:
        far = [c for c in land if h3.grid_distance(me["hex"], c) == 3]
        return [("move", {"to": far[0]}), ("mode", {"mode": "double_march"})]
    if tick == 40:
        return [("mode", {"mode": "none"})]
    if tick == 60 and me["on_map"]:  # go for the nearest bot
        bots = [u for u in st["units"] if u["user_id"] != st["you"] and u["on_map"]]
        if bots:
            target = min(bots, key=lambda u: (h3.grid_distance(me["hex"], u["hex"]), u["id"]))
            return [("move", {"to": target["hex"]})]
    if tick == 150:
        return [("mode", {"mode": "heal"})]
    if tick == 200:
        return [("shield", {"on": True})]
    if tick == 230:
        return [("shield", {"on": False}), ("class", {"class": "marine"})]
    if tick == 260 and not me["on_map"]:
        return [("respawn", {})]
    if tick % 50 == 25 and me["on_map"]:  # keep claiming: the nearest open land hex
        open_ = {h["h3"] for h in st["hexes"] if h["zone"] == "land" and h["owner"] is None and h["playable"]}
        if open_:
            dest = min(sorted(open_), key=lambda c: h3.grid_distance(me["hex"], c))
            return [("move", {"to": dest})]
    return []


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def _fog_shape(fog: dict) -> str:
    """The fog's rings, each rotated to start at its smallest vertex: h3 versions differ in where
    cells_to_geo starts a ring (the bundle's h3 is Pyodide's, the reference's whatever pip resolved)."""
    rings = []
    for poly in fog["geometry"]["coordinates"]:
        for ring in poly:
            pts = [tuple(round(c, 9) for c in pt) for pt in ring[:-1]]
            i = pts.index(min(pts))
            rings.append(pts[i:] + pts[:i])
    return _digest(sorted(rings))


def play(arena: Arena, ticks: int, start: float, hexes: dict, out: list) -> float:
    """Advance tick by tick via the clock, issuing the script's orders. Returns the clock it ended at."""
    period = TICK_EVERY / 1.5
    now = start
    st = arena.call("GET", "/api/solo/local/state", now=now)
    while st["tick"] < ticks:
        for kind, payload in _orders(st, hexes):
            try:
                arena.call("POST", "/api/solo/local/orders", {"kind": kind, "payload": payload}, now)
            except Exception as e:  # noqa: BLE001 - a refused order is part of the record
                out.append({"tick": st["tick"], "refused": kind, "why": str(e)})
        now += period
        st = arena.call("GET", "/api/solo/local/state", now=now)
        out.append({"tick": st["tick"], "state": _digest(st)})
    return now


def run(index: Path, root: Path, region: str, ticks: int = 400) -> dict:
    regions = load_regions(index, root)
    a = Arena(regions)
    a.new({"name": "Parity", "unit_class": "tank", "tick_every": TICK_EVERY, "seed": SEED, "region": region}, T0)
    hexes = {f["properties"]["h3"]: f["properties"]["zone"] for f in a.call("GET", "/api/solo/local/hexes")["features"]}
    trace: list = []
    half = ticks // 2
    now = play(a, half, T0, hexes, trace)
    blob = a.save()
    end = play(a, ticks, now, hexes, trace)
    final = a.call("GET", "/api/solo/local/state", now=end)
    report = a.call("GET", "/api/solo/local/report?since=0", now=end)

    # The same game, saved at the half and restored into a fresh Arena.
    b = Arena(regions)
    b.restore(blob, now)
    resumed: list = []
    end_b = play(b, ticks, now, hexes, resumed)
    final_b = b.call("GET", "/api/solo/local/state", now=end_b)

    return {
        "build": a.build,
        "region": region,
        "ticks": final["tick"],
        "hexes": len(hexes),
        "trace": trace,
        "final": final,
        "report": report,
        "fog": _fog_shape(a.call("GET", "/api/solo/local/fog")),
        "save_resume_matches": _digest(final_b) == _digest(final),
    }


if __name__ == "__main__":
    import argparse

    root = Path(__file__).resolve().parents[2]
    p = argparse.ArgumentParser()
    p.add_argument("--ticks", type=int, default=400)
    p.add_argument("--region", default="cook-strait")
    p.add_argument("--index", type=Path, default=root / "arena" / "regions.json")
    a = p.parse_args()
    print(json.dumps(run(a.index, root, a.region, a.ticks), sort_keys=True))
