"""Load the world from Postgres and write a tick's changes back.

Only rows that changed are written. AI units (no user) are left alone until AI events exist.
"""

import datetime as dt
import hashlib
import os

import psycopg
from psycopg.types.json import Jsonb

from atlas_realms.rules.chunks import cell_chunks
from atlas_realms.rules.tunables import DEFAULT, Tunables
from atlas_realms.tick.engine import TickOutput
from atlas_realms.tick.world import Contract, Hex, Order, Unit, User, World

_HEX_COLS = "zone, terrain, tier, playable, owner_id, owner_since_tick, flip_user_id, flip_progress, is_user_capital, garrison_hp, name"
_CONTRACT_COLS = "bot, hirer, kind, price, start, minutes, target_user, target_clan, status, began"
_UNIT_COLS = (
    "user_id, hex, hp, path, progress, mode, attack_bonus, defence_bonus, arrival_tick, respawn_deadline, order_seq, "
    "unit_class, class_changed_tick, battles_since_class, kills, veteran, shield_hp, shield_ready, shield_used, shield_day, shield_from, shield_to, pursue, hunting, hunt_used, hunt_day"
)


def load_world(conn: psycopg.Connection, t: Tunables = DEFAULT) -> tuple[World, list[Order]]:
    tick = conn.execute("SELECT coalesce(max(id), 0) FROM ticks").fetchone()[0]
    hexes = {}
    for row in conn.execute(f"SELECT h3::text, {_HEX_COLS} FROM hexes"):
        cell, zone, terrain, tier, playable, owner, since, flip_user, flip, capital, garrison, name = row
        hexes[cell] = Hex(
            h3=cell,
            zone=zone,
            terrain=terrain,
            tier=tier,
            playable=playable,
            # Only fogged hexes need their chunk list, and only at unlock time.
            chunks=() if playable else tuple(cell_chunks(cell, t.chunk_zoom)),
            owner_id=owner,
            owner_since_tick=since,
            flip_user_id=flip_user,
            flip_progress=flip,
            is_user_capital=capital,
            garrison_hp=garrison,
            name=name,
        )
    units = {}
    for row in conn.execute(
        "SELECT id, user_id, hex::text, hp, path::text[], progress, mode, attack_bonus, defence_bonus, "
        "arrival_tick, respawn_deadline, order_seq, unit_class, class_changed_tick, battles_since_class, kills, veteran, "
        "shield_hp, shield_ready, shield_used, shield_day, shield_from, shield_to, pursue, hunting, hunt_used, hunt_day "
        "FROM units WHERE user_id IS NOT NULL"
    ):
        (
            uid,
            user,
            cell,
            hp,
            path,
            progress,
            mode,
            att,
            dfn,
            arrival,
            deadline,
            seq,
            cls,
            cls_tick,
            cls_battles,
            kills,
            veteran,
            shield_hp,
            shield_ready,
            shield_used,
            shield_day,
            shield_from,
            shield_to,
            pursue,
            hunting,
            hunt_used,
            hunt_day,
        ) = row
        units[uid] = Unit(
            id=uid,
            user_id=user,
            hex=cell,
            hp=hp,
            path=list(path),
            progress=progress,
            mode=mode,
            attack_bonus=att,
            defence_bonus=dfn,
            arrival_tick=arrival,
            respawn_deadline=deadline,
            order_seq=seq,
            unit_class=cls,
            class_changed_tick=cls_tick,
            battles_since_class=cls_battles,
            kills=kills,
            veteran=veteran,
            shield_hp=shield_hp,
            shield_ready=shield_ready,
            shield_used=shield_used,
            shield_day=shield_day,
            shield_from=shield_from,
            shield_to=shield_to,
            pursue=pursue,
            hunting=hunting,
            hunt_used=hunt_used,
            hunt_day=hunt_day,
        )
    users = {
        uid: User(
            id=uid,
            treasury=treasury,
            clan_id=clan,
            subscriber=sub,
            bot=bot,
            nemesis_user=n_user,
            nemesis_clan=n_clan,
            nemesis_tick=n_tick,
        )
        for uid, treasury, clan, sub, bot, n_user, n_clan, n_tick in conn.execute(
            "SELECT u.id, u.treasury, m.clan_id, u.subscriber_tier IS NOT NULL, u.bot, u.nemesis_user, u.nemesis_clan, "
            "u.nemesis_tick FROM users u LEFT JOIN clan_members m ON m.user_id = u.id WHERE u.deleted_at IS NULL"
        )
    }
    ready = {(z, x, y) for z, x, y in conn.execute("SELECT z, x, y FROM chunks WHERE ready")}
    orders = [
        Order(id=oid, user_id=user, kind=kind, payload=payload, seq=oid)
        for oid, user, kind, payload in conn.execute(
            "SELECT id, user_id, kind, payload FROM orders WHERE applied_tick IS NULL ORDER BY submitted_at, id"
        )
    ]
    contracts = {
        row[0]: Contract(*row) for row in conn.execute(f"SELECT id, {_CONTRACT_COLS} FROM contracts ORDER BY id")
    }
    return World(tick=tick, hexes=hexes, units=units, users=users, ready_chunks=ready, contracts=contracts), orders


def daily_secret(conn: psycopg.Connection, day: dt.date) -> bytes:
    """The day's battle secret, created on first use. Its commitment is what gets published in advance."""
    secret = os.urandom(32)
    conn.execute(
        "INSERT INTO daily_secrets (day, commitment, secret) VALUES (%s, %s, %s) ON CONFLICT (day) DO NOTHING",
        (day, hashlib.sha256(secret).hexdigest(), secret),
    )
    return bytes(conn.execute("SELECT secret FROM daily_secrets WHERE day = %s", (day,)).fetchone()[0])


def save(conn: psycopg.Connection, before: World, out: TickOutput, results_tick: int) -> None:
    w = out.world
    hex_rows = [
        (
            h.zone,
            h.terrain,
            h.tier,
            h.playable,
            h.owner_id,
            h.owner_since_tick,
            h.flip_user_id,
            h.flip_progress,
            h.is_user_capital,
            h.garrison_hp,
            h.name,
            h.h3,
        )  # fmt: skip
        for cell, h in sorted(w.hexes.items())
        if h != before.hexes.get(cell)
    ]
    set_hex = ", ".join(f"{c} = %s" for c in _HEX_COLS.split(", "))
    _executemany(conn, f"UPDATE hexes SET {set_hex} WHERE h3 = %s::h3index", hex_rows)

    unit_rows = [
        (
            u.user_id,
            u.hex,
            u.hp,
            u.path,
            u.progress,
            u.mode,
            u.attack_bonus,
            u.defence_bonus,
            u.arrival_tick,
            u.respawn_deadline,
            u.order_seq,
            u.unit_class,
            u.class_changed_tick,
            u.battles_since_class,
            u.kills,
            u.veteran,
            u.shield_hp,
            u.shield_ready,
            u.shield_used,
            u.shield_day,
            u.shield_from,
            u.shield_to,
            u.pursue,
            u.hunting,
            u.hunt_used,
            u.hunt_day,
            u.id,
        )
        for uid, u in sorted(w.units.items())
        if u != before.units.get(uid)
    ]
    set_unit = ", ".join(
        f"{c} = %s::h3index" if c == "hex" else f"{c} = %s::h3index[]" if c == "path" else f"{c} = %s"
        for c in _UNIT_COLS.split(", ")
    )
    _executemany(conn, f"UPDATE units SET {set_unit} WHERE id = %s", unit_rows)

    cols = _CONTRACT_COLS.split(", ")
    contract_rows = [
        (c.id, *(getattr(c, col) for col in cols))
        for cid, c in sorted(w.contracts.items())
        if c != before.contracts.get(cid)
    ]
    _executemany(
        conn,
        f"INSERT INTO contracts (id, {_CONTRACT_COLS}) VALUES ({', '.join(['%s'] * (len(cols) + 1))}) "
        f"ON CONFLICT (id) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in cols)}",
        contract_rows,
    )

    user_rows = [
        (u.treasury, u.nemesis_user, u.nemesis_clan, u.nemesis_tick, uid)
        for uid, u in sorted(w.users.items())
        if u != before.users.get(uid)
    ]
    _executemany(
        conn,
        "UPDATE users SET treasury = %s, nemesis_user = %s, nemesis_clan = %s, nemesis_tick = %s WHERE id = %s",
        user_rows,
    )

    _executemany(
        conn,
        "INSERT INTO battles (tick, hex, attacker_unit, defender_unit, seed, log, outcome) "
        "VALUES (%s, %s::h3index, %s, %s, %s, %s, %s)",
        [
            (
                w.tick,
                b.hex,
                b.attacker_unit,
                b.defender_unit,
                b.seed,
                Jsonb(b.result.model_dump(mode="json")),
                b.result.outcome.value,
            )  # fmt: skip
            for b in out.battles
        ],
    )
    _executemany(
        conn,
        "INSERT INTO ledger (user_id, tick, amount, reason) VALUES (%s, %s, %s, %s)",
        [(e.user_id, w.tick, e.amount, e.reason) for e in out.ledger],
    )
    _executemany(
        conn,
        "UPDATE orders SET applied_tick = %s, result = %s WHERE id = %s",
        [(results_tick, r, oid) for oid, r in sorted(out.order_results.items())],
    )
    if out.bake_requests:
        styles = [s for (s,) in conn.execute("SELECT name FROM styles WHERE live ORDER BY name")]
        chunks = sorted(out.bake_requests.items())
        _executemany(
            conn, "INSERT INTO chunks (z, x, y) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", [c for c, _ in chunks]
        )
        _executemany(
            conn,
            "INSERT INTO bake_jobs (z, x, y, style, priority) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (z, x, y, style) DO UPDATE SET priority = greatest(bake_jobs.priority, excluded.priority) "
            "WHERE bake_jobs.status = 'queued'",
            [(z, x, y, s, p) for (z, x, y), p in chunks for s in styles],
        )


def _executemany(conn: psycopg.Connection, sql: str, rows: list) -> None:
    if rows:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
