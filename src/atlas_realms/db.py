"""Database connection and migrations.

Migrations are plain SQL files in `migrations/`, applied in name order, each in its own
transaction, and recorded in `schema_migrations`. There is no down-migration: fix forward.
"""

import os
from importlib.resources import files

import psycopg
import structlog

log = structlog.get_logger()

DEFAULT_DSN = "postgresql://atlas:atlas@127.0.0.1:5433/atlas_realms"


def dsn() -> str:
    return os.environ.get("ATLAS_REALMS_DSN", DEFAULT_DSN)


def connect(url: str | None = None) -> psycopg.Connection:
    """An autocommit connection: callers open explicit `conn.transaction()` blocks.

    In psycopg's default mode the first statement opens an implicit transaction, and every later
    `conn.transaction()` becomes a savepoint inside it that only commits when the connection closes.
    """
    return psycopg.connect(url or dsn(), autocommit=True)


def migration_files() -> list[tuple[str, str]]:
    root = files("atlas_realms") / "migrations"
    return sorted((p.name, p.read_text()) for p in root.iterdir() if p.name.endswith(".sql"))


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply pending migrations; returns the names applied."""
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for name, sql in migration_files():
        if name in done:
            continue
        with conn.transaction():
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
        log.info("migration_applied", name=name)
        applied.append(name)
    return applied


if __name__ == "__main__":
    with connect() as c:
        print("\n".join(migrate(c)) or "up to date")
