"""Run ticks against the database.

    python -m atlas_realms.tick.runner          # one tick (for cron)
    python -m atlas_realms.tick.runner --loop   # tick on every tick_seconds boundary

Each tick is one transaction under a transaction-level advisory lock. If the lock is taken
(the previous tick is still running) the tick is skipped, never overlapped. After loading its
orders the tick waits for the next drand round and mixes it into its battle seeds; if no relay
has it, the tick is skipped too and the orders wait for the next one.
"""

import argparse
import datetime as dt
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

import psycopg
import structlog
from psycopg.types.json import Jsonb

from atlas_realms.db import connect, migrate
from atlas_realms.rules.beacon import Beacon
from atlas_realms.rules.tunables import DEFAULT, Tunables
from atlas_realms.tick.beacon import BeaconUnavailable, next_beacon
from atlas_realms.tick.engine import run_tick
from atlas_realms.tick.store import daily_secret, load_world, save

log = structlog.get_logger()

LOCK_KEY = int.from_bytes(b"ATLRTICK", "big") & 0x7FFF_FFFF_FFFF_FFFF


@dataclass(frozen=True, slots=True)
class TickReport:
    tick: int
    duration_ms: int
    orders: int
    battles: int
    bake_requests: int


def run_once(
    conn: psycopg.Connection,
    t: Tunables = DEFAULT,
    now: dt.datetime | None = None,
    beacon: Callable[[float], Beacon] = next_beacon,
) -> TickReport | None:
    """One tick. Returns None when another tick holds the lock or the beacon can't be had.

    `beacon(loaded_at)` returns the first drand round after the orders were loaded (tests pass a stub).
    """
    now = now or dt.datetime.now(dt.UTC)
    start = time.perf_counter()
    with conn.transaction():
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (LOCK_KEY,)).fetchone()[0]:
            log.warning("tick_skipped", reason="previous tick still running")
            return None
        world, orders = load_world(conn, t)
        try:
            b = beacon(time.time())
        except BeaconUnavailable as e:
            log.warning("tick_skipped", reason=str(e))
            return None
        tick = world.tick + 1
        conn.execute("INSERT INTO ticks (id, tunables) VALUES (%s, %s)", (tick, Jsonb(t.model_dump(mode="json"))))
        conn.execute(
            "INSERT INTO tick_beacons (tick, round, signature, randomness) VALUES (%s, %s, %s, %s)",
            (tick, b.round, b.signature, b.randomness),
        )
        out = run_tick(world, orders, daily_secret(conn, now.date()), t, b.randomness)
        save(conn, world, out, tick)
        ms = round((time.perf_counter() - start) * 1000)
        conn.execute("UPDATE ticks SET finished_at = clock_timestamp(), duration_ms = %s WHERE id = %s", (ms, tick))
    report = TickReport(tick, ms, len(orders), len(out.battles), len(out.bake_requests))
    log.info("tick", **asdict(report))
    return report


def main() -> None:
    p = argparse.ArgumentParser(description="Run game ticks")
    p.add_argument("--loop", action="store_true", help="Keep ticking on every tick_seconds boundary")
    a = p.parse_args()
    with connect() as conn:
        migrate(conn)
        if not a.loop:
            run_once(conn)
            return
        while True:
            run_once(conn)
            period = DEFAULT.tick_seconds
            time.sleep(period - time.time() % period)


if __name__ == "__main__":
    main()
