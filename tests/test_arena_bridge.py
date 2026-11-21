"""The browser arena's in-process API (arena/py/arena_bridge.py) under CPython. The Pyodide side is checked against
this by arena/test/parity.test.mjs."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "arena" / "py"))

from arena_bridge import Arena, HttpError, load_regions  # noqa: E402

REGIONS = load_regions(ROOT / "arena" / "regions.json", ROOT)
SEED = "ab" * 32


@pytest.fixture
def arena():
    a = Arena(REGIONS)
    r = a.call("POST", "/api/solo", {"name": "t", "unit_class": "tank", "tick_every": 0.25, "seed": SEED}, now=100.0)
    assert r == {"id": "local", "seed": SEED, "region": "cook-strait"}
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
        ("POST", "/api/solo", {"region": "atlantis"}, 422),
        ("GET", "/api/manifest?region=atlantis", None, 422),
    ],
)
def test_errors(arena, method, url, body, status):
    with pytest.raises(HttpError) as e:
        arena.call(method, url, body, now=100.0)
    assert e.value.status == status


def test_same_seed_same_game():
    def play() -> dict:
        a = Arena(REGIONS)
        a.call("POST", "/api/solo", {"tick_every": 0.25, "seed": SEED}, now=0.0)
        return a.call("GET", "/api/solo/local/state", now=20.0)  # 120 ticks

    assert play() == play()


def test_save_restore(arena):
    saved_at = arena.call("GET", "/api/solo/local/state", now=110.0)["tick"]
    blob = arena.save()
    b = Arena(REGIONS)
    assert b.restore(blob, now=110.0)["seed"] == SEED
    assert b.call("GET", "/api/solo/local/state", now=120.0) == arena.call("GET", "/api/solo/local/state", now=120.0)
    # "When away: pause" keeps the closed time out of the game.
    c = Arena(REGIONS)
    c.restore(blob, now=900.0, away="pause")
    assert c.call("GET", "/api/solo/local/state", now=900.0)["tick"] == saved_at


def test_save_from_another_build_is_refused(arena):
    blob = arena.save()
    b = Arena(REGIONS)
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


def test_regions_are_listed_and_a_game_keeps_its_region():
    a = Arena(REGIONS)
    listed = a.call("GET", "/api/regions")
    assert listed["default"] == "cook-strait"
    assert [r["id"] for r in listed["regions"]] == [r.id for r in REGIONS]
    assert a.call("GET", "/api/manifest?region=iceland")["chunk_zoom"] == 8
    r = a.call("POST", "/api/solo", {"tick_every": 0.25, "seed": SEED, "region": "iceland"}, now=0.0)
    assert r["region"] == "iceland"
    assert a.call("GET", "/api/manifest")["chunk_zoom"] == 8  # the session's own map
    zones = [f["properties"]["zone"] for f in a.call("GET", "/api/solo/local/hexes")["features"]]
    assert zones.count("spawn") == 2 and zones.count("land") > 400
    a.call("GET", "/api/solo/local/state", now=5.0)
    assert a.call("GET", "/api/solo/local/report?since=0", now=5.0)["paper"] == "The Saga Courier"
    b = Arena(REGIONS)
    assert b.restore(a.save(), now=5.0)["region"] == "iceland"


def test_a_save_from_a_missing_region_is_refused():
    a = Arena(REGIONS)
    a.call("POST", "/api/solo", {"seed": SEED, "region": "iceland"}, now=0.0)
    with pytest.raises(HttpError) as e:
        Arena([r for r in REGIONS if r.id != "iceland"]).restore(a.save(), now=0.0)
    assert e.value.status == 409


@pytest.mark.parametrize("url", ["/api/solo/local/report?since=abc", "/api/solo/local/report?since=1.5"])
def test_a_malformed_since_is_a_422(arena, url):
    with pytest.raises(HttpError) as e:
        arena.call("GET", url, now=100.0)
    assert e.value.status == 422


@pytest.mark.parametrize("seed", [12, True, ["ab"]])
def test_a_non_string_seed_is_a_422(seed):
    with pytest.raises(HttpError) as e:
        Arena(REGIONS).call("POST", "/api/solo", {"seed": seed}, now=0.0)
    assert e.value.status == 422


def test_handle_answers_even_when_the_rules_raise(arena, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(arena, "call", boom)
    r = json.loads(arena.handle(json.dumps({"op": "call", "method": "GET", "url": "/api/meta", "now": 0})))
    assert r["status"] == 500 and "bug" in r["body"]["detail"]


def test_a_save_cannot_run_code(arena, tmp_path):
    import base64
    import pickle
    import zlib

    marker = tmp_path / "pwned"

    class Evil:
        def __reduce__(self):
            return (open, (str(marker), "w"))

    blob = base64.b64encode(zlib.compress(pickle.dumps({"v": 3, "build": arena.build, "session": Evil()}))).decode()
    with pytest.raises(HttpError) as e:
        Arena(REGIONS).restore(blob, now=0.0)
    assert e.value.status == 422 and not marker.exists()


def test_a_shield_restore_that_is_refused_still_loads_the_game(arena):
    from atlas_realms.solo import MAX_PENDING

    arena.call("POST", "/api/solo/local/control", {"paused": True}, now=100.0)
    for _ in range(MAX_PENDING):
        arena.call("POST", "/api/solo/local/orders", {"kind": "cancel"}, now=100.0)
    b = Arena(REGIONS)
    assert b.restore(arena.save(), now=200.0, away="shield")["id"] == "local"
    assert b.session is not None
