import json

import pytest
from fastapi.testclient import TestClient

from atlas_realms.api import create_app

READY = [[10, x, y] for x in range(1006, 1013) for y in range(638, 645)]


@pytest.fixture
def client(tmp_path):
    tiles = tmp_path / "tiles"
    (tiles / "antique" / "10" / "1009").mkdir(parents=True)
    (tiles / "antique" / "10" / "1009" / "641.png").write_bytes(b"\x89PNG")
    manifest = {"styles": [{"name": "antique", "patron_only": False}], "ready_chunks": READY,
                "minzoom": 10, "maxzoom": 12, "bounds": [173, -42.5, 176.5, -40], "tile_url": "tiles/{style}/{z}/{x}/{y}.png",
                "attribution": "test"}  # fmt: skip
    (tiles / "manifest.json").write_text(json.dumps(manifest))
    return TestClient(create_app(tiles))


def test_static_and_meta(client):
    assert client.get("/").status_code == 200
    assert client.get("/app.js").status_code == 200
    assert client.get("/tiles/antique/10/1009/641.png").content == b"\x89PNG"
    meta = client.get("/api/meta").json()
    assert "marine" in meta["classes"] and "reaver" in meta["personalities"]


def test_solo_session_flow(client):
    sid = client.post("/api/solo", json={"name": "tester", "unit_class": "tank", "tick_every": 0.25}).json()["id"]
    hexes = client.get(f"/api/solo/{sid}/hexes").json()
    assert len(hexes["features"]) > 50
    target = next(f["properties"]["h3"] for f in hexes["features"] if f["properties"]["zone"] == "land")
    assert client.post(f"/api/solo/{sid}/orders", json={"kind": "move", "payload": {"to": target}}).status_code == 200
    st = client.get(f"/api/solo/{sid}/state").json()
    me = next(u for u in st["units"] if u["user_id"] == st["you"])
    assert me["class"] == "tank" and len(st["players"]) == 9
    assert client.post(f"/api/solo/{sid}/control", json={"paused": True}).json()["paused"] is True


def test_bad_input(client):
    assert client.post("/api/solo", json={"unit_class": "wizard"}).status_code == 422
    assert client.post("/api/solo", json={"bots": ["nobody"]}).status_code == 422
    assert client.get("/api/solo/nope/state").status_code == 404
    sid = client.post("/api/solo", json={}).json()["id"]
    bad = client.post(f"/api/solo/{sid}/orders", json={"kind": "class", "payload": {"class": "wizard"}})
    assert bad.status_code == 422


def test_patron_perks_are_looks_and_history_only(client):
    free = client.post("/api/solo", json={}).json()["id"]
    assert client.post(f"/api/solo/{free}/looks", json={"shield": "gold"}).status_code == 403
    assert client.post(f"/api/solo/{free}/looks", json={"shield": "plain"}).status_code == 200
    assert client.get(f"/api/solo/{free}/state").json()["history"] is None
    patron = client.post("/api/solo", json={"subscriber": True}).json()["id"]
    assert client.post(f"/api/solo/{patron}/looks", json={"shield": "gold"}).status_code == 200
    st = client.get(f"/api/solo/{patron}/state").json()
    me = next(p for p in st["players"] if p["id"] == st["you"])
    assert me["shield_look"] == "#f2c14e" and st["history"] == []


def test_solo_cap_shares_the_land_out_and_everyone_starts_with_ten_coins(client):
    sid = client.post("/api/solo", json={}).json()["id"]
    st = client.get(f"/api/solo/{sid}/state").json()
    land = sum(1 for h in st["hexes"] if h["zone"] == "land" and h["playable"])
    assert st["hex_cap"] == int(land / len(st["players"]) * 1.68)
    assert all(p["treasury"] == 10 for p in st["players"])
    few = client.post("/api/solo", json={"bots": ["settler"]}).json()["id"]
    assert client.get(f"/api/solo/{few}/state").json()["hex_cap"] == int(land / 2 * 1.68)


def test_a_full_arena_refuses_new_games_instead_of_evicting_live_ones(client, monkeypatch):
    import atlas_realms.api as api

    monkeypatch.setattr(api, "MAX_SESSIONS", 2)
    a = client.post("/api/solo", json={}).json()["id"]
    b = client.post("/api/solo", json={}).json()["id"]
    assert client.post("/api/solo", json={}).status_code == 503
    assert (
        client.get(f"/api/solo/{a}/state").status_code == 200 and client.get(f"/api/solo/{b}/state").status_code == 200
    )
    monkeypatch.setattr(api, "IDLE_SECONDS", 0)  # now both count as idle: the least recently used one goes
    client.get(f"/api/solo/{a}/state")
    c = client.post("/api/solo", json={})
    assert c.status_code == 200
    assert (
        client.get(f"/api/solo/{b}/state").status_code == 404 and client.get(f"/api/solo/{a}/state").status_code == 200
    )


def test_the_bot_roster_is_bounded(client):
    from atlas_realms.bots import PERSONALITIES

    assert client.post("/api/solo", json={"bots": ["settler"] * (len(PERSONALITIES) + 1)}).status_code == 422
