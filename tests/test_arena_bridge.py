"""The browser arena's in-process API (arena/py/arena_bridge.py) under CPython. The Pyodide side is checked against
this by arena/test/parity.test.mjs."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "arena" / "py"))

from arena_bridge import Arena, HttpError  # noqa: E402

MANIFEST = json.loads((ROOT / "arena" / "world" / "cook-strait.json").read_text())
OUTLINE = ROOT / "data" / "nz_mainland.geojson"
SEED = "ab" * 32


@pytest.fixture
def arena():
    a = Arena(MANIFEST, OUTLINE)
    r = a.call("POST", "/api/solo", {"name": "t", "unit_class": "tank", "tick_every": 0.25, "seed": SEED}, now=100.0)
    assert r == {"id": "local", "seed": SEED}
    return a


def test_routes_like_the_server(arena):
    assert "marine" in arena.call("GET", "/api/meta")["classes"]
    assert arena.call("GET", "/api/manifest")["ready_chunks"]
    assert len(arena.call("GET", "/api/solo/local/hexes")["features"]) > 50
    st = arena.call("GET", "/api/solo/local/state", now=100.0)
    assert st["tick"] == 0 and len(st["players"]) == 9
    st = arena.call("GET", "/api/solo/local/state", now=101.0)  # 1 s at 0.25 / PACE per tick = 6 ticks
    assert st["tick"] == 6
    assert arena.call("POST", "/api/solo/local/control", {"paused": True}, now=101.0)["paused"] is True
    assert arena.call("GET", "/api/solo/local/state", now=500.0)["tick"] == 6
    assert arena.call("GET", "/api/solo/local/report?since=0", now=500.0)["headline"]


@pytest.mark.parametrize(
    ("method", "url", "body", "status"),
    [
        ("GET", "/api/solo/other/state", None, 404),
        ("GET", "/api/nope", None, 404),
        ("POST", "/api/solo/local/orders", {"kind": "class", "payload": {"class": "wizard"}}, 422),
        ("POST", "/api/solo/local/looks", {"shield": "gold"}, 403),
        ("POST", "/api/solo/local/control", {"tick_every": 0.01}, 422),
        ("POST", "/api/solo", {"unit_class": "wizard"}, 422),
    ],
)
def test_errors(arena, method, url, body, status):
    with pytest.raises(HttpError) as e:
        arena.call(method, url, body, now=100.0)
    assert e.value.status == status


def test_same_seed_same_game():
    def play() -> dict:
        a = Arena(MANIFEST, OUTLINE)
        a.call("POST", "/api/solo", {"tick_every": 0.25, "seed": SEED}, now=0.0)
        return a.call("GET", "/api/solo/local/state", now=20.0)  # 120 ticks

    assert play() == play()


def test_save_restore(arena):
    saved_at = arena.call("GET", "/api/solo/local/state", now=110.0)["tick"]
    blob = arena.save()
    b = Arena(MANIFEST, OUTLINE)
    assert b.restore(blob, now=110.0)["seed"] == SEED
    assert b.call("GET", "/api/solo/local/state", now=120.0) == arena.call("GET", "/api/solo/local/state", now=120.0)
    # "When away: pause" keeps the closed time out of the game.
    c = Arena(MANIFEST, OUTLINE)
    c.restore(blob, now=900.0, away="pause")
    assert c.call("GET", "/api/solo/local/state", now=900.0)["tick"] == saved_at


def test_save_from_another_build_is_refused(arena):
    blob = arena.save()
    b = Arena(MANIFEST, OUTLINE)
    b.build = "something-else"
    with pytest.raises(HttpError) as e:
        b.restore(blob, now=0.0)
    assert e.value.status == 409
    with pytest.raises(HttpError):
        b.restore("not a save", now=0.0)


def test_handle_is_json(arena):
    r = json.loads(arena.handle(json.dumps({"op": "call", "method": "GET", "url": "/api/solo/x/state", "now": 0})))
    assert r == {"status": 404, "body": {"detail": "no such session"}}
    assert json.loads(arena.handle(json.dumps({"op": "save"})))["status"] == 200
