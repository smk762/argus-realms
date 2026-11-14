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
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO units (hex, hp) VALUES (%s, 100)", (WELLINGTON,))
