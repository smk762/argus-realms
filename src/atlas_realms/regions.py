"""The shared world's regions: one database each, on one Postgres server.

    python -m atlas_realms.regions prepare cook-strait,iceland   # migrate and seed each region's database
    python -m atlas_realms.regions wait cook-strait              # block until it is migrated and seeded

A region is a shard. It has its own database (`db_name`), so its ticks, players, battles and tick lock are its own:
`pg_try_advisory_xact_lock` is scoped to the database, so one region's tick never skips another's. The region's
world (manifest, outline, spawns) is the arena's entry in `arena/regions.json`, so the browser arena and the shared
world play on the same maps.

The databases themselves are created by whoever runs Postgres (on Kubernetes, a CloudNativePG `Database` per region,
which also installs PostGIS and h3-pg, since neither is a trusted extension). `prepare` only migrates and seeds.
"""

import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg
import structlog

from atlas_realms.db import connect, dsn, migrate, migration_files
from atlas_realms.seed import seed

log = structlog.get_logger()

ROOT = Path(__file__).resolve().parents[2]
REGIONS_FILE = ROOT / "arena" / "regions.json"
REGION_ID = re.compile(r"^[a-z][a-z0-9-]{0,40}[a-z0-9]$")


@dataclass(frozen=True, slots=True)
class Region:
    id: str
    manifest: Path
    outline: Path
    spawns: tuple[tuple[float, float], ...]


def load(path: Path = REGIONS_FILE) -> dict[str, Region]:
    """Every region the arena knows, by id. Paths in the file are relative to the repo root."""
    root = path.resolve().parents[1]
    out = {}
    for r in json.loads(path.read_text())["regions"]:
        if not REGION_ID.match(r["id"]):
            raise ValueError(f"region id {r['id']!r} is not a lowercase slug")
        out[r["id"]] = Region(
            id=r["id"],
            manifest=root / r["manifest"],
            outline=root / r["outline"],
            spawns=tuple((float(lat), float(lng)) for lat, lng in r["spawns"]),
        )
    return out


def db_name(region: str) -> str:
    """`cook-strait` -> `realms_cook_strait`: a database name that needs no quoting."""
    if not REGION_ID.match(region):
        raise ValueError(f"region id {region!r} is not a lowercase slug")
    return "realms_" + region.replace("-", "_")


def region_dsn(region: str, base: str | None = None) -> str:
    """`base` (default `ATLAS_REALMS_DSN`) pointed at the region's database. Anything `base` leaves out (host, user,
    password) libpq takes from the PG* environment, so a password never has to sit in a connection string."""
    return psycopg.conninfo.make_conninfo(dsn() if base is None else base, dbname=db_name(region))


def is_ready(conn: psycopg.Connection) -> bool:
    """Every migration applied and the world seeded. Its own transaction (rolled back on a fresh database), so it
    never leaves a connection aborted or idle in a transaction, autocommit or not."""
    try:
        with conn.transaction():
            done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            seeded = conn.execute("SELECT EXISTS (SELECT 1 FROM hexes)").fetchone()[0]
    except psycopg.errors.UndefinedTable:
        return False
    return done >= {name for name, _ in migration_files()} and seeded


def prepare(conn: psycopg.Connection, region: Region) -> int:
    """Migrate the region's database and (re-)seed its world. Safe to re-run: seeding never touches ownership."""
    applied = migrate(conn)
    n = seed(conn, json.loads(region.manifest.read_text()), region.outline, list(region.spawns))
    log.info("region_prepared", region=region.id, migrations=len(applied), hexes=n)
    return n


def _retrying(region: str, fn, timeout: float, every: float = 3.0):
    """Run fn(conn) on the region's database until it returns a truthy value, reconnecting while the database is
    still being created (on Kubernetes the `Database` and the server come up alongside this)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with connect(region_dsn(region)) as conn:
                result = fn(conn)
                if result:
                    return result
        except psycopg.OperationalError as e:  # not up yet, or its database not created yet
            log.info("region_waiting", region=region, reason=type(e).__name__)
        if time.monotonic() > deadline:
            raise TimeoutError(f"region {region} not ready after {timeout:.0f} s")
        time.sleep(every)


def main() -> None:
    p = argparse.ArgumentParser(description="Prepare or wait for the shared world's region databases")
    p.add_argument("action", choices=("prepare", "wait"))
    p.add_argument("regions", nargs="+", help="Region ids from arena/regions.json (separate args or comma-separated)")
    p.add_argument("--timeout", type=float, default=600.0, help="Seconds to wait for each database")
    a = p.parse_args()
    known = load()
    regions = [r for arg in a.regions for r in arg.split(",") if r]
    unknown = sorted(set(regions) - set(known))
    if unknown:
        raise SystemExit(f"unknown regions {unknown}: arena/regions.json has {sorted(known)}")
    for r in regions:
        if a.action == "prepare":
            _retrying(r, lambda conn, r=r: prepare(conn, known[r]) is not None, a.timeout)
        else:
            _retrying(r, is_ready, a.timeout)
            log.info("region_ready", region=r)


if __name__ == "__main__":
    main()
