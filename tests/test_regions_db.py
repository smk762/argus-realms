"""The shared world's regions against a real database: prepare (migrate + seed), readiness, the world API."""

import time

import psycopg
import pytest
from fastapi.testclient import TestClient

import atlas_realms.api as api
from atlas_realms import regions
from atlas_realms.tick.runner import run_once
from tests.conftest import needs_db
from tests.test_tick_db import DAY, stub_beacon

pytestmark = needs_db


def test_prepare_seeds_a_region_and_is_safe_to_rerun(db_url):
    region = regions.load()["cook-strait"]
    with psycopg.connect(db_url, autocommit=True) as c:
        assert not regions.is_ready(c)  # migrated by the fixture, but no world yet
        n = regions.prepare(c, region)
        assert n > 0 and regions.is_ready(c)
        assert c.execute("SELECT count(*) FROM hexes WHERE zone = 'spawn'").fetchone()[0] == len(region.spawns)
        uid = c.execute(
            "INSERT INTO users (oauth_provider, oauth_subject, display_name) VALUES ('t', 'a', 'a') RETURNING id"
        ).fetchone()[0]
        land = c.execute("SELECT h3 FROM hexes WHERE zone = 'land' LIMIT 1").fetchone()[0]
        c.execute("UPDATE hexes SET owner_id = %s WHERE h3 = %s", (uid, land))
        assert regions.prepare(c, region) == n  # a re-run (every deploy) leaves ownership alone
        assert c.execute("SELECT owner_id FROM hexes WHERE h3 = %s", (land,)).fetchone()[0] == uid


def test_a_fresh_database_leaves_the_connection_usable(db_url):
    """On a fresh database the readiness check must not leave a non-autocommit connection aborted (migrate is next)."""
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("DROP TABLE schema_migrations")
    with psycopg.connect(db_url) as c:
        assert not regions.is_ready(c)
        assert c.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        assert c.execute("SELECT 1").fetchone()[0] == 1


def test_db_names_need_no_quoting():
    assert regions.db_name("cook-strait") == "realms_cook_strait"
    with pytest.raises(ValueError):
        regions.db_name("Cook Strait; DROP")
    assert "dbname=realms_iceland" in regions.region_dsn("iceland", "host=db sslmode=require")


def test_the_world_api_is_starting_until_the_region_is_ready_then_reports_its_ticks(db_url, monkeypatch):
    monkeypatch.setattr(api, "region_dsn", lambda region: db_url)
    monkeypatch.setattr(api, "READY_POLL", 0.05)
    with TestClient(api.create_world_app(["cook-strait"])) as client:
        r = client.get("/health")
        assert r.status_code == 503 and r.json()["status"] == "starting"
        with psycopg.connect(db_url, autocommit=True) as c:
            regions.prepare(c, regions.load()["cook-strait"])
            run_once(c, now=DAY, beacon=stub_beacon)
        deadline = time.monotonic() + 5
        while client.get("/health").status_code != 200 and time.monotonic() < deadline:
            time.sleep(0.05)
        body = client.get("/health").json()
        assert body["status"] == "ok" and body["checks"]["cook-strait"]["ok"] and body["service"] == "atlas-realms"
        assert client.get("/health?checks=0").json()["checks"] == {}
        world = client.get("/api/world").json()["regions"]
        assert world[0]["id"] == "cook-strait" and world[0]["tick"] == 1 and world[0]["players"] == 0
        assert client.get("/api/world/atlantis").status_code == 404
