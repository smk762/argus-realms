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
CONNECT_TIMEOUT = 10  # seconds: a server behind a NetworkPolicy that drops the packets must not hang the caller


def dsn() -> str:
    return os.environ.get("ATLAS_REALMS_DSN", DEFAULT_DSN)


def connect(url: str | None = None) -> psycopg.Connection:
    """An autocommit connection: callers open explicit `conn.transaction()` blocks.

    In psycopg's default mode the first statement opens an implicit transaction, and every later
    `conn.transaction()` becomes a savepoint inside it that only commits when the connection closes.
    """
    return psycopg.connect(url or dsn(), autocommit=True, connect_timeout=CONNECT_TIMEOUT)


def migration_files() -> list[tuple[str, str]]:
    root = files("atlas_realms") / "migrations"
    return sorted((p.name, p.read_text()) for p in root.iterdir() if p.name.endswith(".sql"))


MIGRATION_LOCK = 0x61746C61_6D696772  # pg_advisory_lock key ("atlamigr"): one migrator at a time


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply pending migrations; returns the names applied.

    Each statement runs inside an explicit `conn.transaction()`, so no implicit transaction is left open even on a
    connection that isn't autocommit (which would turn every migration into a savepoint of it). A session advisory lock
    serialises concurrent starts; the applied set is read after taking it, so the second process sees the first's work.
    """
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("migrate() needs a connection with no transaction open")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
    try:
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
    finally:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))


if __name__ == "__main__":
    with connect() as c:
        print("\n".join(migrate(c)) or "up to date")
