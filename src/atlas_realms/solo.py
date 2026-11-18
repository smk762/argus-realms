"""The solo arena: one human against bots, in memory, sped up.

A session owns a World built straight from the tile manifest and the land outline (no database), a
random daily secret, and its bots. Time is lazy: each request catches up the ticks that are due at the
session's speed, so there is no background thread. The world is plain dataclasses, so a session can
be snapshotted to JSON later without changing the engine.
"""

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import h3

from atlas_realms.bots import PERSONALITIES, Bot, decide
from atlas_realms.fog import fog_geojson
from atlas_realms.news import STRAIT, digest, events_of
from atlas_realms.rules.chunks import at_zoom
from atlas_realms.rules.classes import CLASSES
from atlas_realms.rules.tunables import DEFAULT, Tunables
from atlas_realms.seed import land_cells, plan_hexes, sea_chunks
from atlas_realms.tick.engine import (
    SERVICES,
    BattleRecord,
    city_base,
    flip_needed,
    garrison_max,
    hex_income_per_hour,
    hunt_left,
    nemesis_of,
    price_factor,
    run_tick,
    service_base,
    shield_left,
    shield_ready_in,
)
from atlas_realms.tick.world import Hex, Order, Unit, User, World

HUMAN = 1
# Shield looks: "plain" for everyone, the rest a patron cosmetic. Name -> bubble colour.
SHIELD_LOOKS = {"plain": "#4aa3ff", "gold": "#f2c14e", "ember": "#ff6b3d", "aurora": "#5be0b0", "violet": "#b47cff"}
PACE = 1.5  # the arena runs this much faster than its speed setting says (every setting, 0.25x to 4x)
HISTORY = 200  # battles involving the human kept for the patron history
EVENTS = 5000  # news events kept for 'while you were away' reports
# The solo arena's rules: the shared ones; Session.new sets the hex cap from the map (PvP keeps DEFAULT's).
SOLO = DEFAULT
MAX_CATCH_UP = 120  # ticks run per request at most, so a long-idle tab doesn't stall the server
MAX_PENDING = 100  # the human's orders queued for one tick (a paused game queues them all for the next)
RESULTS = 50  # order results kept for state() (it shows the last 10)
DEFAULT_ROSTER = ("settler", "raider", "warden", "corsair", "sapper", "jackal", "rival", "reaver")


def build_world(manifest: dict, outline: Path, spawns: list[tuple[float, float]], t: Tunables = DEFAULT) -> World:
    """The solo world: the baked chunks plus the open sea inside the painted block (`sea_chunks`), which needs no
    painting, so a strait between baked coasts is open water to play on rather than fog. Chunks baked at another zoom
    (Iceland's z7) are mapped onto the game's chunk zoom first."""
    baked = at_zoom(manifest["ready_chunks"], t.chunk_zoom)
    ready = baked | sea_chunks(baked, outline, t.chunk_zoom)
    rows = plan_hexes(ready, land_cells(outline, t.h3_resolution), spawns, t)
    hexes = {
        r["h3"]: Hex(h3=r["h3"], zone=r["zone"], terrain=r["terrain"], tier=r["tier"], playable=r["playable"])
        for r in rows
    }
    return World(tick=0, hexes=hexes, units={}, users={}, ready_chunks=ready)


@dataclass
class Session:
    world: World
    bots: list[Bot]
    names: dict[int, str]
    secret: bytes = field(default_factory=lambda: os.urandom(32))
    tick_every: float = 2.0  # the speed setting: nominal wall seconds per game tick (the real period is / PACE)
    paused: bool = False
    t: Tunables = SOLO
    region: str = ""  # which map (the arena's regions.json id); the server's solo mode has only Cook Strait
    desk: dict = field(default_factory=lambda: dict(STRAIT))  # the news desk's paper and place names
    battles: list[tuple[int, BattleRecord]] = field(default_factory=list)  # (tick, record), newest last
    results: dict[int, str] = field(default_factory=dict)  # the human's order id -> result
    history: list[tuple[int, BattleRecord]] = field(default_factory=list)  # the human's own battles, newest last
    looks: dict[int, str] = field(default_factory=dict)  # user -> shield look (cosmetic)
    events: list[dict] = field(default_factory=list)  # news: battles, captures, bounties
    _pending: list[Order] = field(default_factory=list)
    _next_order: int = 1
    _last: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def new(
        cls,
        world: World,
        name: str,
        unit_class: str = "barbarian",
        roster: tuple[str, ...] = DEFAULT_ROSTER,
        subscriber: bool = False,  # preview the patron perks (no account in the solo arena)
        **kw,
    ) -> "Session":
        spawns = sorted(c for c, h in world.hexes.items() if h.zone == "spawn")
        if not spawns:
            raise ValueError("the world has no spawn zone")
        names = {HUMAN: name}
        t = kw.pop("t", SOLO)
        coins = round(t.start_coins * 1000)
        world.users[HUMAN] = User(id=HUMAN, treasury=coins, subscriber=subscriber)
        world.units[HUMAN] = Unit(id=HUMAN, user_id=HUMAN, hex=spawns[0], hp=DEFAULT.max_hp, unit_class=unit_class)
        bots = []
        for i, key in enumerate(roster, start=2):
            p = PERSONALITIES[key]
            home = spawns[(i - 1) % len(spawns)]  # alternate sides, the human's side first
            world.users[i] = User(id=i, treasury=coins, bot=True)
            world.units[i] = Unit(id=i, user_id=i, hex=home, hp=DEFAULT.max_hp, unit_class=p.unit_class, veteran=True)
            bots.append(Bot(user_id=i, personality=p, home=home))
            names[i] = key.title()
        # The cap shares the claimable land out among the players, with room to spare (cap_share).
        land = sum(1 for h in world.hexes.values() if h.zone == "land" and h.playable)
        t = t.model_copy(update={"hex_cap": max(1, int(land / len(world.users) * t.cap_share))})
        return cls(world=world, bots=bots, names=names, t=t, **kw)

    def submit(self, kind: str, payload: dict) -> int:
        """Queue one of the human's orders for the next tick; returns its id."""
        if kind == "class" and payload.get("class") not in CLASSES:
            raise ValueError("unknown class")
        if len(self._pending) >= MAX_PENDING:
            raise ValueError("too many orders queued")
        oid = self._next_order
        self._next_order += 1
        self._pending.append(Order(id=oid, user_id=HUMAN, kind=kind, payload=payload, seq=oid))
        return oid

    def catch_up(self, now: float | None = None) -> int:
        """Run the ticks due since the last call. Returns how many ran."""
        now = time.monotonic() if now is None else now
        if self.paused:
            self._last = now
            return 0
        period = self.tick_every / PACE
        due = int((now - self._last) / period)
        n = min(due, MAX_CATCH_UP)
        for _ in range(n):
            self.step()
        self._last = now if due > n else self._last + due * period
        return n

    def step(self) -> None:
        """One tick: bots decide, then the engine runs with everyone's orders."""
        orders = list(self._pending)
        self._pending.clear()
        seed = self.secret.hex()
        for bot in self.bots:
            for o in decide(self.world, bot, HUMAN, seed, self.t):
                oid = self._next_order
                self._next_order += 1
                orders.append(Order(id=oid, user_id=o.user_id, kind=o.kind, payload=o.payload, seq=oid))
        before = self.world
        out = run_tick(before, orders, self.secret, self.t)
        self.world = out.world
        self.events.extend(events_of(before, out))
        del self.events[:-EVENTS]
        mine = {o.id for o in orders if o.user_id == HUMAN}
        self.results.update({k: v for k, v in out.order_results.items() if k in mine})
        for k in list(self.results)[:-RESULTS]:
            del self.results[k]
        self.battles.extend((self.world.tick, b) for b in out.battles)
        del self.battles[:-50]
        units = self.world.units
        self.history.extend(
            (self.world.tick, b) for b in out.battles if HUMAN in (units[b.attacker_unit].user_id, b.defender_user)
        )
        del self.history[:-HISTORY]

    def report(self, since: int) -> dict:
        """'While you were away': the news since tick `since`."""
        w = self.world
        cities = {c: (h.name, h.tier or 1) for c, h in w.hexes.items()}
        return digest(self.events, self.names, HUMAN, since, w.tick, cities, self.desk)

    def set_look(self, look: str) -> None:
        if look not in SHIELD_LOOKS:
            raise ValueError("unknown look")
        if look != "plain" and not self.world.users[HUMAN].subscriber:
            raise PermissionError("patron perk")
        self.looks[HUMAN] = look

    def _battle(self, tick: int, b: BattleRecord, rolls: bool = False) -> dict:
        w = self.world
        out = {
            "tick": tick,
            "hex": b.hex,
            "attacker": w.units[b.attacker_unit].user_id,
            "defender": b.defender_user,
            "garrison": b.defender_unit is None,
            "outcome": b.result.outcome.value,
            "rolls": len(b.result.rolls),
            "shield": b.shield_absorbed,
        }
        if rolls:  # the replay: every roll, and what each side brought to it
            r = b.result
            out["replay"] = {
                "attacker_hp": r.attacker.hp,
                "defender_hp": r.defender.hp,
                "attacker_mods": {m.source: m.value for m in r.attacker.modifiers},
                "defender_mods": {m.source: m.value for m in r.defender.modifiers},
                "rolls": [
                    [
                        x.attacker_d100,
                        x.defender_d100,
                        round(x.attacker_strength),
                        round(x.defender_strength),
                        x.winner,
                        x.damage,
                    ]
                    for x in r.rolls
                ],  # fmt: skip
            }
        return out

    def state(self) -> dict:
        w, t = self.world, self.t
        held: dict[int, int] = {}
        for h in w.hexes.values():
            if h.owner_id is not None:
                held[h.owner_id] = held.get(h.owner_id, 0) + 1
        personality = {b.user_id: b.personality for b in self.bots}
        return {
            "tick": w.tick,
            "playable_hexes": sum(1 for h in w.hexes.values() if h.playable),  # refetch the fog when it changes
            "paused": self.paused,
            "tick_every": self.tick_every,
            "you": HUMAN,
            "players": [
                {
                    "id": uid,
                    "name": self.names[uid],
                    "subscriber": w.users[uid].subscriber,
                    "clan": w.users[uid].clan_id,
                    "nemesis": _nemesis(w.users[uid]),
                    "nemesis_ready_in": _nemesis_ready_in(w.users[uid], w.tick, t),
                    "targeted_by": sorted(x for x in w.users if nemesis_of(w, x, uid)),
                    "shield_look": SHIELD_LOOKS[self.looks.get(uid, "plain")],
                    "bot": uid in personality,
                    "personality": personality[uid].blurb if uid in personality else None,
                    "hexes": held.get(uid, 0),
                    "treasury": w.users[uid].treasury / 1000,
                }
                for uid in sorted(w.users)
            ],
            "units": [
                {
                    "id": u.id,
                    "user_id": u.user_id,
                    "hex": u.hex,
                    "hp": u.hp,
                    "on_map": u.on_map,
                    "path": u.path,
                    "mode": u.mode,
                    "class": u.unit_class,
                    "defence_bonus": u.defence_bonus,
                    "attack_bonus": u.attack_bonus,
                    "battles_since_class": u.battles_since_class,
                    "kills": u.kills,
                    "veteran": u.veteran,
                    "respawn_in": None if u.on_map else u.respawn_deadline - w.tick,
                    "shield": u.shield_hp,
                    "shield_max": t.shield_hp_mult * t.max_hp,
                    "shield_left": shield_left(u, w.tick, t),  # minutes of shield left today
                    "shield_ready_in": shield_ready_in(u, w.tick + 1, t),
                    "shield_schedule": None if u.shield_from is None else [u.shield_from, u.shield_to],
                    "pursue": u.pursue,
                    "hunting": u.hunting,
                    "hunt_left": hunt_left(u, w.tick, t),  # minutes of hunt mode left today
                }
                for u in sorted(w.units.values(), key=lambda u: u.id)
            ],
            "hexes": [
                {
                    "h3": c,
                    "zone": h.zone,
                    "playable": h.playable,
                    "owner": h.owner_id,
                    "flip": h.flip_progress,
                    "garrison": h.garrison_hp,
                }
                for c, h in sorted(w.hexes.items())
            ],
            "cities": self._cities(),
            "nemesis_table": self._nemesis_table(),
            "hex_cap": t.hex_cap,
            "capture": self._capture(),
            **self._market(),
            "battles": [self._battle(tick, b) for tick, b in reversed(self.battles[-15:])],
            # Patron perk: every battle you were in (up to HISTORY), with its replay.
            "history": [self._battle(tick, b, rolls=True) for tick, b in reversed(self.history)]
            if w.users[HUMAN].subscriber
            else None,
            "results": dict(list(self.results.items())[-10:]),  # at most RESULTS
        }

    def _market(self) -> dict:
        """Bot services and cities for sale, priced for the human, and every contract (they're public)."""
        w, t = self.world, self.t
        factor = price_factor(w, HUMAN, t)
        open_ = [c for c in w.contracts.values() if c.status in ("queued", "active")]

        def free_at(bot: int) -> int:
            """The tick its mercenary/guard queue runs out."""
            jobs = [c for c in open_ if c.bot == bot and c.kind != "protect"]
            end = w.tick
            for c in sorted(jobs, key=lambda c: c.id):
                end = c.began + c.minutes if c.status == "active" else max(end, c.start) + c.minutes
            return end

        bots = {}
        for b in self.bots:
            bots[b.user_id] = {
                "quotes": {k: round(service_base(w, b.user_id, k, t) * factor, 1) for k in SERVICES},
                "free_in": free_at(b.user_id) - w.tick,  # minutes until a new job would start
            }
        city_prices = {
            c: round(city_base(h, w.tick, t) * factor, 1)
            for c, h in w.hexes.items()
            if h.owner_id is not None and w.users[h.owner_id].bot
        }
        contracts = [
            {
                "id": c.id,
                "bot": c.bot,
                "hirer": c.hirer,
                "kind": c.kind,
                "status": c.status,
                "price": c.price / 1000,
                "target": c.target_user,
                "target_clan": c.target_clan,
                "starts_in": None if c.status == "active" else max(0, c.start - w.tick),
                "ends_in": c.began + c.minutes - w.tick if c.status == "active" else None,
            }
            for c in sorted(w.contracts.values(), key=lambda c: c.id)
            if c.status in ("queued", "active") or (c.hirer == HUMAN and c.id > max(w.contracts, default=0) - 20)
        ]
        return {
            "price_factor": round(factor, 2),
            "bots_for_hire": bots,
            "city_prices": city_prices,
            "contracts": contracts,
        }

    def _capture(self) -> dict | None:
        """Where the human stands on someone else's land: the garrison left, or the capture's progress."""
        w = self.world
        u = w.units[HUMAN]
        h = w.hexes[u.hex] if u.on_map else None
        if h is None or h.zone != "land" or h.owner_id in (None, HUMAN):
            return None
        mine = h.flip_user_id == HUMAN
        return {
            "owner": h.owner_id,
            "garrison": h.garrison_hp or 0,
            "progress": h.flip_progress if mine else 0,
            "need": flip_needed(h, w.tick + 1, self.t),
        }

    def _nemesis_table(self) -> list[dict]:
        """Every nemesis in play: the target (a player or a clan) and who is hunting it, most hunted first."""
        rows: dict[tuple[str, int], list[int]] = {}
        for uid, u in sorted(self.world.users.items()):
            n = _nemesis(u) if u.subscriber else None
            if n:
                rows.setdefault((n["kind"], n["id"]), []).append(uid)
        return sorted(
            ({"kind": k, "id": i, "hunters": hunters} for (k, i), hunters in rows.items()),
            key=lambda r: (-len(r["hunters"]), r["kind"], r["id"]),
        )

    def _cities(self) -> list[dict]:
        """The human's cities, richest first: income, how long held, garrison, and what a full repair costs."""
        w, t = self.world, self.t
        fee = t.fee_repair or 0
        sieging = {u.hex: u.user_id for u in w.units.values() if u.on_map and u.user_id != HUMAN}
        out = []
        for c, h in w.hexes.items():
            if h.owner_id != HUMAN:
                continue
            g, top = h.garrison_hp or 0, garrison_max(h, t)
            out.append(
                {
                    "h3": c,
                    "name": h.name,
                    "since": h.owner_since_tick or 0,
                    "tier": h.tier or 1,
                    "capital": h.is_user_capital,
                    "income_per_hour": round(hex_income_per_hour(h, w.tick, t), 2),
                    "held_hours": round((w.tick - (h.owner_since_tick or w.tick)) * t.tick_seconds / 3600, 1),
                    "garrison": g,
                    "garrison_max": top,
                    "repair_cost": (top - g) * fee / 1000,
                    "besieged_by": sieging.get(c),
                    "capture_progress": h.flip_progress if h.flip_user_id is not None else 0,
                }
            )
        return sorted(out, key=lambda r: (-r["income_per_hour"], r["h3"]))

    def fog(self) -> dict:
        return fog_geojson(c for c, h in self.world.hexes.items() if h.playable)

    def hex_geojson(self) -> dict:
        """Static hex outlines: fetched once, joined to `state()["hexes"]` by h3 id in the client."""
        feats = []
        for c, h in sorted(self.world.hexes.items()):
            ring = [[lng, lat] for lat, lng in h3.cell_to_boundary(c)]
            lat, lng = h3.cell_to_latlng(c)
            feats.append(
                {
                    "type": "Feature",
                    "id": int(c, 16),
                    "properties": {"h3": c, "zone": h.zone, "lat": lat, "lng": lng},
                    "geometry": {"type": "Polygon", "coordinates": [ring + ring[:1]]},
                }
            )
        return {"type": "FeatureCollection", "features": feats}


def _nemesis(u: User) -> dict | None:
    if u.nemesis_user is not None:
        return {"kind": "user", "id": u.nemesis_user}
    if u.nemesis_clan is not None:
        return {"kind": "clan", "id": u.nemesis_clan}
    return None


def _nemesis_ready_in(u: User, tick: int, t: Tunables) -> int:
    """Ticks until the nemesis can be changed again (as of the next tick)."""
    if u.nemesis_tick is None:
        return 0
    return max(0, u.nemesis_tick + round(t.nemesis_change_hours * 3600 / t.tick_seconds) - tick - 1)
