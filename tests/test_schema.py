"""Schema tests against a real Postgres (see tests/conftest.py)."""

import psycopg
import pytest

from atlas_realms.db import migrate, migration_files
from tests.conftest import needs_db

pytestmark = needs_db

WELLINGTON = "87bb2955affffff"


def _user(c, name="ada") -> int:
    return c.execute(
        "INSERT INTO users (oauth_provider, oauth_subject, display_name) VALUES ('discord', %s, %s) RETURNING id",
        (name, name),
    ).fetchone()[0]


def test_migrate_is_idempotent(conn):
    assert migrate(conn) == []
    names = [r[0] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name")]
    assert names == [n for n, _ in migration_files()]


def test_h3_extension_works(conn):
    cell = conn.execute("SELECT h3_lat_lng_to_cell(POINT(174.7762, -41.2865), 7)::text").fetchone()[0]
    assert cell == WELLINGTON
    assert (
        conn.execute("SELECT h3_grid_distance(%s::h3index, %s::h3index)", (WELLINGTON, WELLINGTON)).fetchone()[0] == 0
    )


def test_only_land_can_be_owned(conn):
    uid = _user(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO hexes (h3, zone, terrain, owner_id) VALUES (%s, 'water', 'water', %s)", (WELLINGTON, uid)
        )


def test_one_user_capital_per_player(conn):
    uid = _user(conn)
    other = conn.execute(
        "SELECT c::text FROM h3_grid_disk(%s::h3index, 1) c WHERE c <> %s::h3index LIMIT 1", (WELLINGTON, WELLINGTON)
    ).fetchone()[0]
    for cell in (WELLINGTON, other):
        conn.execute("INSERT INTO hexes (h3, zone, terrain, owner_id) VALUES (%s, 'land', 'flat', %s)", (cell, uid))
    conn.execute("UPDATE hexes SET is_user_capital = true WHERE h3 = %s", (WELLINGTON,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("UPDATE hexes SET is_user_capital = true WHERE h3 = %s", (other,))


def test_unit_is_player_or_ai_not_both(conn):
    conn.execute("INSERT INTO hexes (h3, zone, terrain) VALUES (%s, 'spawn', 'flat')", (WELLINGTON,))
    uid = _user(conn)
    ai = conn.execute(
        "INSERT INTO ai_events (target_user_id, announced_tick, starts_tick, ends_tick) VALUES (%s, 0, 0, 1) RETURNING id",
        (uid,),
    ).fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():  # neither
        conn.execute("INSERT INTO units (hex, hp) VALUES (%s, 100)", (WELLINGTON,))
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():  # both
        conn.execute(
            "INSERT INTO units (hex, hp, user_id, ai_event_id) VALUES (%s, 100, %s, %s)", (WELLINGTON, uid, ai)
        )
    conn.execute("INSERT INTO units (hex, hp, user_id) VALUES (%s, 100, %s)", (WELLINGTON, uid))  # one is fine


def _fresh_db():
    import uuid

    from tests.conftest import TEST_DSN

    name = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    return name, psycopg.conninfo.make_conninfo(TEST_DSN, dbname=name)


def _drop_db(name):
    from tests.conftest import TEST_DSN

    with psycopg.connect(TEST_DSN, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_migrations_commit_on_a_connection_that_is_not_autocommit():
    name, url = _fresh_db()
    try:
        with psycopg.connect(url) as c:  # psycopg's default: implicit transactions
            assert migrate(c)
            c.rollback()  # nothing may be left pending for the caller to commit
        with psycopg.connect(url, autocommit=True) as c:
            assert migrate(c) == []
    finally:
        _drop_db(name)


def test_concurrent_migrations_apply_each_file_once():
    from concurrent.futures import ThreadPoolExecutor

    name, url = _fresh_db()
    try:

        def run():
            with psycopg.connect(url, autocommit=True) as c:
                return migrate(c)

        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: run(), range(4)))
        assert sorted(n for r in results for n in r) == [n for n, _ in migration_files()]
    finally:
        _drop_db(name)
