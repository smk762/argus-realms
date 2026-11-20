"""The tick against a real database: load -> engine -> save, locking and replay."""

import datetime as dt

import h3
import psycopg

from atlas_realms.rules.beacon import Beacon, round_after
from atlas_realms.rules.chunks import cell_chunks, lookahead
from atlas_realms.rules.dice import secret_commitment
from atlas_realms.rules.movement import hex_minutes
from atlas_realms.rules.tunables import DEFAULT
from atlas_realms.tick.engine import run_tick
from atlas_realms.tick.runner import LOCK_KEY, run_once
from atlas_realms.tick.store import daily_secret, load_world
from tests.conftest import needs_db

pytestmark = needs_db

CENTRE = h3.latlng_to_cell(-41.2865, 174.7762, DEFAULT.h3_resolution)
RING1 = sorted(h3.grid_ring(CENTRE, 1))
DAY = dt.datetime(2026, 10, 26, tzinfo=dt.UTC)


def stub_beacon(loaded_at: float) -> Beacon:
    """A fixed fake round, so DB tests never call drand."""
    return Beacon(round_after(loaded_at), b"\x01" * 48)


def tick(c: psycopg.Connection, **kw):
    return run_once(c, now=DAY, beacon=stub_beacon, **kw)


def seed(c: psycopg.Connection, players: int = 2) -> list[int]:
    for cell in h3.grid_disk(CENTRE, 2):
        zone = "spawn" if cell == CENTRE else "land"
        c.execute(
            "INSERT INTO hexes (h3, zone, terrain, tier, playable) VALUES (%s, %s, 'flat', 1, true)", (cell, zone)
        )
    ids = []
    for i in range(players):
        uid = c.execute(
            "INSERT INTO users (oauth_provider, oauth_subject, display_name) VALUES ('test', %s, %s) RETURNING id",
            (f"p{i}", f"p{i}"),
        ).fetchone()[0]
        c.execute("INSERT INTO units (user_id, hex, hp) VALUES (%s, %s, 100)", (uid, CENTRE))
        ids.append(uid)
    c.execute("INSERT INTO styles (name) VALUES ('antique'), ('tolkien')")
    return ids


def order(c, uid: int, kind: str, payload: dict) -> int:
    return c.execute(
        "INSERT INTO orders (user_id, kind, payload) VALUES (%s, %s, %s) RETURNING id",
        (uid, kind, psycopg.types.json.Jsonb(payload)),
    ).fetchone()[0]


def test_ticks_move_claim_and_pay(db_url):
    with psycopg.connect(db_url, autocommit=True) as c:
        a, _ = seed(c)
        oid = order(c, a, "move", {"to": RING1[0]})
        r = tick(c)
        assert r is not None and r.tick == 1 and r.orders == 1
        assert c.execute("SELECT applied_tick, result FROM orders WHERE id = %s", (oid,)).fetchone() == (1, "ok")
        for _ in range(int(hex_minutes())):
            tick(c)
        assert c.execute("SELECT hex::text FROM units WHERE user_id = %s", (a,)).fetchone()[0] == RING1[0]
        assert c.execute("SELECT owner_id FROM hexes WHERE h3 = %s::h3index", (RING1[0],)).fetchone()[0] == a
        tick(c)
        treasury, ledger = c.execute(
            "SELECT u.treasury, (SELECT sum(amount) FROM ledger WHERE user_id = u.id) FROM users u WHERE id = %s", (a,)
        ).fetchone()
        assert treasury == ledger > 0
        ticks = c.execute("SELECT count(*), min(duration_ms) >= 0 FROM ticks").fetchone()
        assert ticks == (1 + int(hex_minutes()) + 1, True)


def test_world_round_trips_exactly(db_url):
    """What a tick saves is what the next tick loads: the condition for replaying from the database."""
    with psycopg.connect(db_url, autocommit=True) as c:
        a, b = seed(c)
        order(c, a, "move", {"to": RING1[0]})
        order(c, b, "mode", {"mode": "double_march"})
        c.execute("UPDATE units SET battles_since_class = 1 WHERE user_id = %s", (b,))
        c.execute("UPDATE users SET subscriber_tier = 'patron' WHERE id = %s", (b,))
        swap = order(c, b, "class", {"class": "marine"})
        bot = c.execute(
            "INSERT INTO users (oauth_provider, oauth_subject, display_name, bot, treasury) "
            "VALUES ('bot', 'warden', 'Warden', true, 0) RETURNING id"
        ).fetchone()[0]
        c.execute("UPDATE users SET treasury = 100000 WHERE id = %s", (a,))
        hire = order(c, a, "hire", {"bot": bot, "kind": "protect"})
        world, orders = load_world(c)
        assert world.users[b].subscriber
        secret = daily_secret(c, DAY.date())
        expected = run_tick(world, orders, secret, beacon=stub_beacon(0).randomness).world
        tick(c)
        loaded, pending = load_world(c)
        assert loaded == expected and pending == []
        assert loaded.units[b].unit_class == "marine" and loaded.units[b].class_changed_tick == 1
        assert c.execute("SELECT result FROM orders WHERE id = %s", (swap,)).fetchone()[0] == "ok"
        assert c.execute("SELECT result FROM orders WHERE id = %s", (hire,)).fetchone()[0] == "ok"
        assert [(x.kind, x.status, x.bot, x.hirer) for x in loaded.contracts.values()] == [
            ("protect", "active", bot, a)
        ]


def test_skips_while_another_tick_holds_the_lock(db_url):
    with psycopg.connect(db_url, autocommit=True) as c, psycopg.connect(db_url) as other:
        seed(c)
        other.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        assert tick(c) is None
        other.rollback()
        assert tick(c).tick == 1


def test_battles_are_logged_and_replayable_from_the_log(db_url):
    from atlas_realms.rules.combat import Side, resolve_battle
    from atlas_realms.rules.dice import Dice, battle_seed

    with psycopg.connect(db_url, autocommit=True) as c:
        a, b = seed(c)
        c.execute("UPDATE units SET hex = %s::h3index WHERE user_id = %s", (RING1[0], b))
        c.execute(
            "UPDATE units SET path = ARRAY[%s::h3index], progress = %s WHERE user_id = %s", (RING1[0], hex_minutes(), a)
        )
        tick(c)
        tick_id, seed_bytes, log = c.execute("SELECT tick, seed, log FROM battles").fetchone()
        secret, commitment = c.execute("SELECT secret, commitment FROM daily_secrets").fetchone()
        sig, randomness = c.execute(
            "SELECT signature, randomness FROM tick_beacons WHERE tick = %s", (tick_id,)
        ).fetchone()
        # Anyone holding the revealed secret and the tick's drand round can check the seed and every roll.
        assert secret_commitment(bytes(secret)) == commitment
        assert Beacon(0, bytes(sig)).randomness == bytes(randomness)
        assert battle_seed(bytes(secret), tick_id, 0, bytes(randomness)) == bytes(seed_bytes)
        again = resolve_battle(Side(**log["attacker"]), Side(**log["defender"]), Dice(bytes(seed_bytes)))
        assert again.model_dump(mode="json") == log


def test_fog_queues_bakes_for_every_live_style(db_url):
    with psycopg.connect(db_url, autocommit=True) as c:
        a, b = seed(c)
        fogged = h3.grid_ring(CENTRE, 2)[0]
        c.execute("UPDATE hexes SET playable = false WHERE h3 = %s::h3index", (fogged,))
        near = sorted(n for n in h3.grid_ring(fogged, 1) if n in set(h3.grid_disk(CENTRE, 2)))[:2]
        for uid, cell in zip((a, b), near, strict=True):
            c.execute("UPDATE hexes SET owner_id = %s, owner_since_tick = 0 WHERE h3 = %s::h3index", (uid, cell))
        tick(c)
        jobs = c.execute("SELECT count(*), count(DISTINCT style), max(priority) FROM bake_jobs").fetchone()
        chunks = set(cell_chunks(fogged, DEFAULT.chunk_zoom))
        assert jobs[1] == 2 and jobs[2] == 2
        queued = set(c.execute("SELECT style, z, x, y FROM bake_jobs").fetchall())
        styles = {s for s, *_ in queued}
        around = {n for ch in chunks for n in lookahead(ch, DEFAULT.lookahead)}
        assert {(s, *ch) for s in styles for ch in chunks} <= queued  # every chunk of the fogged hex, in both styles
        assert queued <= {(s, *ch) for s in styles for ch in around}  # and nothing beyond their lookahead
        assert c.execute("SELECT playable FROM hexes WHERE h3 = %s::h3index", (fogged,)).fetchone()[0] is False


def test_skips_when_no_beacon_and_keeps_the_orders(db_url):
    from atlas_realms.tick.beacon import BeaconUnavailable

    def down(loaded_at: float) -> Beacon:
        raise BeaconUnavailable("relays down")

    with psycopg.connect(db_url, autocommit=True) as c:
        a, _ = seed(c)
        oid = order(c, a, "move", {"to": RING1[0]})
        assert run_once(c, now=DAY, beacon=down) is None
        assert c.execute("SELECT count(*) FROM ticks").fetchone()[0] == 0
        assert c.execute("SELECT applied_tick FROM orders WHERE id = %s", (oid,)).fetchone()[0] is None
        assert tick(c).orders == 1
