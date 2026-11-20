"""One tick, as a pure function: `run_tick(world, orders, secret) -> TickOutput`.

The steps run in the design doc's order. Not built yet, and marked where they will go:
over-cap choices (a claim at cap is simply left unclaimed, the doc's default), user and clan
capitals, garrisons, clan spawning, and AI levelling events. Terrain, river and flanking
modifiers are still proposals in the doc, so only the settled modifiers apply.

Veteran units (the bots) gain +veteran_step attack and defence per kill, up to veteran_max, and lose
it when they fall. Whoever kills one loots its purse: bounty_base + bounty_per_kill x kills, capped
at bounty_cap and at what the bot holds, paid out of the bot's own treasury (its hexes' income and
the loot it took from others).

Cities have garrisons (city_garrison_hp; a user capital's is garrison_hp). Entering an empty
enemy city is a battle against its garrison, and the capture timer only starts once it is beaten.
Garrisons never heal; a `repair` order restores them at fee_repair per HP. Claiming open land
comes with a full garrison, capturing a city leaves it at 0.

Shields: a `shield` order raises shield_hp_mult x max_hp of shield (`{"on": false}` drops it). A
defender's shield takes damage before its HP (the battle is fought on the sum), and a shielded unit
can't attack. Every player toggles it within shield_daily_hours a game day, shield_cooldown_minutes
between; a breached shield costs shield_breach_cooldown_hours. Subscribers can schedule it
(`shield_schedule`: raise and drop at set minutes of the game day), from the same budget.

Nemesis (subscribers): a `nemesis` order picks one target (a subscriber, a bot, or a clan, whose
subscribed and bot members count), at most once per nemesis_change_hours. Fights between a hunter and
its nemesis give the hunter nemesis_bonus attack and defence. A `hunt` order chases the nearest
nemesis unit (else its nearest land) for up to hunt_minutes_daily a game day.

Bot services (any player, paid in coins up front; prices from `quote()`, scaled by the buyer's income):
`hire` a bot as a mercenary (against the hirer's nemesis or a chosen target), a guard (patrols the
hirer's land) or for protection (a truce). A bot works one mercenary or guard job at a time; the rest
queue, and a start can be scheduled. While a contract is active, bot and hirer count as allied, and loot
from the bot's kills is split evenly with the hirer. A mercenary's target can buy it off by offering
outbid_factor x its contract (the old hirer is refunded what wasn't served). `buy_city` buys a bot's city.

Revenge: a `pursue` order chases another player's unit, re-pathing each tick it moves, until the
target falls or leaves the map, or a move or cancel order replaces it.

Player classes (rules/classes.py) change movement costs, combat modifiers and healing; a
`class` order swaps class after the cooldown and at least one battle since the last swap.

Open questions decided here for now, each easy to change:
- units heal while moving;
- walking fees (l_walk) are charged per hex entered, clipped to what the player has.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field

import h3

from atlas_realms.rules.chunks import Tile, lookahead
from atlas_realms.rules.classes import DEFAULT_CLASS, ClassSpec
from atlas_realms.rules.combat import BattleResult, Modifier, Outcome, Side, resolve_battle
from atlas_realms.rules.dice import Dice, battle_seed
from atlas_realms.rules.movement import advance, find_path, hex_minutes
from atlas_realms.rules.tunables import DEFAULT, Tunables
from atlas_realms.tick.world import Contract, Hex, Order, Unit, World

MODES = ("none", "double_march", "heal")
CITY_NAME_MAX = 24


@dataclass(slots=True)
class BattleRecord:
    hex: str
    attacker_unit: int
    defender_unit: int | None  # None: the city's garrison defended
    seed: bytes
    result: BattleResult
    defender_user: int | None = None
    shield_absorbed: int = 0  # damage the defender's shield took


@dataclass(slots=True)
class LedgerEntry:
    user_id: int
    amount: int  # milli-coins, negative for fees
    reason: str


@dataclass(slots=True)
class TickOutput:
    world: World
    battles: list[BattleRecord] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    bake_requests: dict[Tile, int] = field(default_factory=dict)  # chunk -> priority
    order_results: dict[int, str] = field(default_factory=dict)  # order id -> "ok" or why not


class _Tick:
    """Working state for one tick. Steps mutate `w`; the caller's world is a deep copy (`World.snapshot`)."""

    def __init__(self, world: World, secret: bytes, t: Tunables, beacon: bytes | None = None) -> None:
        self.w = world.snapshot()
        self.w.tick += 1
        self.tick = self.w.tick
        self.secret = secret
        self.beacon = beacon
        self.t = t
        self.out = TickOutput(world=self.w)
        self.hex_min = hex_minutes(t)
        self.by_user = {u.user_id: u for u in self.w.units.values()}
        self.occ: dict[str, set[int]] = defaultdict(set)
        for u in self.w.units.values():
            if u.on_map:
                self.occ[u.hex].add(u.id)
        self.moved: set[int] = set()
        self.marched: set[int] = set()
        self.attacks: list[tuple[int, str]] = []

    # -- helpers ---------------------------------------------------------------------------

    def spec(self, u: Unit) -> ClassSpec:
        return self.t.classes.get(u.unit_class, self.t.classes[DEFAULT_CLASS])

    def cost_for(self, u: Unit):
        w, t, m, c = self.w, self.t, self.hex_min, self.spec(u)

        def cost(cell: str) -> float | None:
            h = w.hexes.get(cell)
            if h is None or not h.playable or h.zone == "fog":
                return None
            if h.zone == "water":
                return m * t.cost_water * c.water_cost
            if h.zone == "land" and h.owner_id is not None and w.allied(h.owner_id, u.user_id):
                return m * t.cost_own_land * c.land_cost
            return m * t.cost_land * c.land_cost

        return cost

    def floor_for(self, u: Unit) -> float:
        """The A* floor: no hex can cost this unit less."""
        c, t = self.spec(u), self.t
        return self.hex_min * min(t.cost_land * c.land_cost, t.cost_own_land * c.land_cost, t.cost_water * c.water_cost)

    def contact_for(self, u: Unit):
        """Stepping into this cell is an attack: an enemy unit, or an enemy city's garrison, on land."""

        def contact(cell: str) -> bool:
            if self.w.hexes[cell].zone != "land":
                return False
            return bool(self.enemies_on(cell, u.user_id)) or self.garrisoned(cell, u.user_id)

        return contact

    def garrisoned(self, cell: str, user_id: int) -> bool:
        """An enemy city with garrison left: entering it is an attack on the garrison."""
        h = self.w.hexes[cell]
        return h.owner_id is not None and (h.garrison_hp or 0) > 0 and not self.w.allied(h.owner_id, user_id)

    def enemies_on(self, cell: str, user_id: int) -> list[Unit]:
        return [
            self.w.units[i]
            for i in sorted(self.occ.get(cell, ()))
            if self.w.units[i].hp > 0 and not self.w.allied(self.w.units[i].user_id, user_id)
        ]

    def place(self, u: Unit, cell: str) -> None:
        self.occ[u.hex].discard(u.id)
        u.hex = cell
        self.occ[cell].add(u.id)
        u.arrival_tick = self.tick
        self.moved.add(u.id)

    def fall(self, u: Unit) -> None:
        self.occ[u.hex].discard(u.id)
        u.hp = 0
        if u.shield_hp:
            self.end_shield(u, broken=True)
        u.path, u.progress, u.mode, u.pursue, u.hunting = [], 0.0, "none", None, False
        u.respawn_deadline = self.tick + self.t.respawn_timeout

    def kill(self, winner: Unit, loser: Unit) -> None:
        """The loser falls; the winner counts a kill and loots a veteran (bot) unit's purse."""
        self.loot(loser, winner.user_id)
        winner.kills += 1
        loser.kills = 0  # kills since the unit last fell: a veteran loses its edge on death
        self.fall(loser)

    def loot(self, loser: Unit, to_user: int) -> None:
        """A fallen veteran's purse goes to whoever killed it (a unit's player, or a city's owner)."""
        t = self.t
        if not loser.veteran:
            return
        # Loot is paid out of the bot's own treasury (its hexes' income and loot it took), never minted.
        purse = self.w.users[loser.user_id]
        amount = min(purse.treasury, t.bounty_cap, t.bounty_base + t.bounty_per_kill * loser.kills)
        if amount:
            purse.treasury -= amount
            # A bot on a mercenary or guard job splits its loot evenly with whoever hired it.
            job = next((c for c in self.w.contracts.values()
                        if c.status == "active" and c.bot == to_user and c.kind != "protect"), None)  # fmt: skip
            share = amount // 2 if job else 0
            self.w.users[to_user].treasury += amount - share
            self.out.ledger.append(_entry(loser.user_id, -amount, "looted"))
            self.out.ledger.append(_entry(to_user, amount - share, "loot"))
            if share:
                self.w.users[job.hirer].treasury += share
                self.out.ledger.append(_entry(job.hirer, share, "loot_share"))

    def respawn(self, u: Unit, cell: str) -> None:
        u.respawn_deadline = None
        u.hp = self.t.respawn_hp
        u.attack_bonus = self.t.attack_bonus
        self.occ[cell].add(u.id)
        u.hex = cell
        u.arrival_tick = self.tick
        self.moved.add(u.id)

    def charge(self, user_id: int, amount: int, reason: str) -> bool:
        user = self.w.users[user_id]
        if user.treasury < amount:
            return False
        user.treasury -= amount
        self.out.ledger.append(_entry(user_id, -amount, reason))
        return True

    # -- steps -----------------------------------------------------------------------------

    def apply_orders(self, orders: list[Order]) -> None:
        results = self.out.order_results
        for o in sorted(orders, key=lambda o: (o.seq, o.id)):
            u = self.by_user.get(o.user_id)
            if u is None:
                results[o.id] = "no unit"
                continue
            if o.kind == "respawn":
                results[o.id] = self._order_respawn(u, o.payload.get("to"))
            elif o.kind == "class":
                results[o.id] = self._order_class(u, o.payload.get("class"))
            elif o.kind == "repair":
                results[o.id] = self._order_repair(o.user_id, o.payload.get("hex"), o.payload.get("hp"))
            elif o.kind == "shield":
                results[o.id] = self._order_shield(u, o.payload.get("on", True) is not False)
            elif o.kind == "shield_schedule":
                results[o.id] = self._order_schedule(u, o.payload.get("from"), o.payload.get("to"))
            elif o.kind == "hire":
                results[o.id] = self._order_hire(o.user_id, o.payload)
            elif o.kind == "cancel_contract":
                results[o.id] = self._order_cancel_contract(o.user_id, o.payload.get("id"))
            elif o.kind == "buy_city":
                results[o.id] = self._order_buy_city(o.user_id, o.payload.get("hex"))
            elif o.kind == "rename":
                results[o.id] = self._order_rename(o.user_id, o.payload.get("hex"), o.payload.get("name"))
            elif not u.on_map:
                results[o.id] = "respawning"
            elif o.kind == "nemesis":
                results[o.id] = self._order_nemesis(u, o.payload.get("user"), o.payload.get("clan"))
            elif o.kind == "hunt":
                results[o.id] = self._order_hunt(u, o.payload.get("on", True) is not False)
            elif o.kind == "pursue":
                results[o.id] = self._order_pursue(u, o.payload.get("user"), o.seq)
            elif o.kind == "move":
                u.pursue, u.hunting = None, False
                to = str(o.payload.get("to"))
                if to not in self.w.hexes:
                    results[o.id] = "unreachable"
                    continue
                if u.shield_hp and (self.enemies_on(to, u.user_id) or self.garrisoned(to, u.user_id)):
                    results[o.id] = "can't attack while shielded"
                    continue
                path = find_path(u.hex, to, self.cost_for(u), floor=self.floor_for(u))
                if path is None:
                    results[o.id] = "unreachable"
                    continue
                u.path, u.progress, u.order_seq = path, 0.0, o.seq
                results[o.id] = "ok"
            elif o.kind == "cancel":
                u.path, u.progress, u.pursue, u.hunting = [], 0.0, None, False
                results[o.id] = "ok"
            elif o.kind == "mode" and o.payload.get("mode") in MODES:
                u.mode = o.payload["mode"]
                results[o.id] = "ok"
            else:
                results[o.id] = "unknown order"

    def _order_repair(self, user_id: int, cell, hp) -> str:
        """Restore a city's garrison at fee_repair per HP: as much as asked (default: full) and affordable."""
        h = self.w.hexes.get(str(cell))
        if h is None or h.owner_id != user_id:
            return "not your city"
        if self.enemies_on(h.h3, user_id):
            return "under siege"  # no mending the walls with the enemy inside them
        missing = garrison_max(h, self.t) - (h.garrison_hp or 0)
        if missing <= 0:
            return "already full"
        fee = self.t.fee_repair or 0
        if hp is None:
            want = missing
        else:
            try:
                want = max(0, min(int(hp), missing))
            except (TypeError, ValueError, OverflowError):
                return "bad hp"
        afford = want if fee == 0 else min(want, self.w.users[user_id].treasury // fee)
        if afford <= 0:
            return "not enough coins"
        if fee:
            self.charge(user_id, afford * fee, "repair")
        h.garrison_hp = (h.garrison_hp or 0) + afford
        return "ok" if afford == want else f"repaired {afford} of {want} HP"

    def _order_shield(self, u: Unit, on: bool) -> str:
        if not on:
            if not u.shield_hp:
                return "not shielded"
            self.end_shield(u)
            return "ok"
        if not u.on_map:
            return "respawning"
        if u.shield_hp:
            return "already shielded"
        _new_day(u, self.tick, self.t)
        if shield_ready_in(u, self.tick, self.t):
            return "cooldown"
        u.shield_hp = self.t.shield_hp_mult * self.t.max_hp
        u.shield_day = _day(self.tick, self.t)
        u.pursue, u.hunting = None, False  # a shielded unit can't attack
        return "ok"

    def _order_schedule(self, u: Unit, start, stop) -> str:
        """Subscribers: raise the shield at minute `start` of each game day and drop it at `stop` (None clears)."""
        if not self.w.users[u.user_id].subscriber:
            return "patron perk"
        if start is None or stop is None:
            u.shield_from = u.shield_to = None
            return "ok"
        day = _ticks(86400, self.t)
        try:
            start, stop = int(start) % day, int(stop) % day
        except (TypeError, ValueError, OverflowError):
            return "bad time"
        if start == stop:
            return "bad time"
        u.shield_from, u.shield_to = start, stop
        return "ok"

    def _order_nemesis(self, u: Unit, user, clan) -> str:
        w, t = self.w, self.t
        me = w.users[u.user_id]
        if not me.subscriber:
            return "patron perk"
        if user is None and clan is None:
            me.nemesis_user = me.nemesis_clan = None
            u.hunting = False
            return "ok"
        if me.nemesis_tick is not None and self.tick - me.nemesis_tick < _ticks(t.nemesis_change_hours * 3600, t):
            return "cooldown"
        if user is not None:
            them = w.users.get(user) if isinstance(user, int) else None
            if them is None or w.allied(them.id, me.id) or not (them.subscriber or them.bot):
                return "not a valid nemesis"  # only subscribers and bots can be targeted
            me.nemesis_user, me.nemesis_clan = them.id, None
        else:
            clans = {x.clan_id for x in w.users.values() if x.clan_id is not None}
            if not isinstance(clan, int) or clan not in clans or clan == me.clan_id:
                return "not a valid nemesis"
            me.nemesis_user, me.nemesis_clan = None, clan
        me.nemesis_tick = self.tick
        return "ok"

    def _order_hunt(self, u: Unit, on: bool) -> str:
        if not on:
            u.hunting = False
            return "ok"
        me = self.w.users[u.user_id]
        if not me.subscriber:
            return "patron perk"
        if me.nemesis_user is None and me.nemesis_clan is None:
            return "no nemesis"
        if not u.on_map:
            return "respawning"
        if u.shield_hp:
            return "can't attack while shielded"
        _new_hunt_day(u, self.tick, self.t)
        if u.hunt_used >= _ticks(self.t.hunt_minutes_daily * 60, self.t):
            return "no hunt time left today"
        u.hunting = True
        return "ok"

    def hunts(self) -> None:
        """Hunt mode: chase the nearest nemesis unit, or else march on the nearest nemesis land."""
        w, t = self.w, self.t
        budget = _ticks(t.hunt_minutes_daily * 60, t)
        for u in self._units():
            _new_hunt_day(u, self.tick, t)
            if not u.hunting:
                continue
            if not u.on_map or u.shield_hp or u.hunt_used >= budget:
                u.hunting = False
                continue
            u.hunt_used, u.hunt_day = u.hunt_used + 1, _day(self.tick, t)
            current = self.by_user.get(u.pursue) if u.pursue is not None else None
            if current is not None and current.on_map and nemesis_of(w, u.user_id, current.user_id):
                continue
            prey = [v for v in self._units() if v.on_map and nemesis_of(w, u.user_id, v.user_id)]
            if prey:
                u.pursue = min(prey, key=lambda v: (h3.grid_distance(u.hex, v.hex), v.id)).user_id
                continue
            u.pursue = None
            lands = [
                c for c, h in sorted(w.hexes.items()) if h.owner_id is not None and nemesis_of(w, u.user_id, h.owner_id)
            ]
            if lands:
                goal = min(lands, key=lambda c: h3.grid_distance(u.hex, c))
                if goal != u.hex and (not u.path or u.path[-1] != goal):
                    path = find_path(u.hex, goal, self.cost_for(u), floor=self.floor_for(u))
                    if path:
                        u.path, u.progress = path, 0.0

    def _order_pursue(self, u: Unit, target, seq: int) -> str:
        them = self.by_user.get(target) if isinstance(target, int) else None
        if them is None or them.user_id == u.user_id or self.w.allied(them.user_id, u.user_id):
            return "no such enemy"
        if not u.on_map:
            return "respawning"
        if u.shield_hp:
            return "can't attack while shielded"
        if not them.on_map:
            return "not on the map"
        u.pursue, u.order_seq = them.user_id, seq
        return "ok" if self._chase(u) else "unreachable"

    def _chase(self, u: Unit) -> bool:
        """Point a pursuer's path at its target's hex. False (and the pursuit ends) when it can't be reached."""
        them = self.by_user.get(u.pursue)
        if them is None or not them.on_map:
            u.pursue, u.path, u.progress = None, [], 0.0  # the target fell or left: stop where you are
            return False
        if u.path and u.path[-1] == them.hex:
            return True
        path = find_path(u.hex, them.hex, self.cost_for(u), floor=self.floor_for(u))
        if path is None:
            u.pursue = None
            return False
        if not path:  # on the same hex: wait for it to step off (units only fight on entering a hex)
            u.path, u.progress = [], 0.0
            return True
        if not u.path or u.path[0] != path[0]:
            u.progress = 0.0  # a new first step starts from scratch
        u.path = path
        return True

    def pursuits(self) -> None:
        for u in self._units():
            if u.pursue is not None and u.on_map:
                self._chase(u)

    def end_shield(self, u: Unit, broken: bool = False) -> None:
        """Drop the shield and start the wait for the next (longer when it was breached)."""
        t = self.t
        wait = t.shield_breach_cooldown_hours * 3600 if broken else t.shield_cooldown_minutes * 60
        u.shield_hp, u.shield_ready = 0, self.tick + _ticks(wait, t)

    def shields(self) -> None:
        """Shields count against the day's budget and drop when it runs out; subscribers' schedules raise and drop them."""
        t = self.t
        budget, minute = _ticks(t.shield_daily_hours * 3600, t), self.tick % _ticks(86400, t)
        for u in self._units():
            _new_day(u, self.tick, t)
            if u.shield_from is not None and self.w.users[u.user_id].subscriber:
                if minute == u.shield_from and u.on_map and not u.shield_hp and not shield_ready_in(u, self.tick, t):
                    u.shield_hp, u.shield_day, u.pursue = t.shield_hp_mult * t.max_hp, _day(self.tick, t), None
                elif minute == u.shield_to and u.shield_hp:
                    self.end_shield(u)
            if not u.shield_hp:
                continue
            u.shield_used += 1
            if u.shield_used >= budget:
                self.end_shield(u)

    def _order_hire(self, user_id: int, p: dict) -> str:
        w, t = self.w, self.t
        bot, kind = p.get("bot"), p.get("kind")
        if not isinstance(bot, int) or bot not in w.users or not w.users[bot].bot or bot == user_id:
            return "not a bot"
        if kind not in SERVICES:
            return "unknown service"
        me = w.users[user_id]
        c = Contract(id=max(w.contracts, default=0) + 1, bot=bot, hirer=user_id, kind=kind, price=0, start=self.tick,
                     minutes=getattr(t, f"{kind}_minutes"))  # fmt: skip
        active = next(
            (x for x in w.contracts.values() if x.bot == bot and x.status == "active" and x.kind == "merc"), None
        )
        if kind == "merc":
            target = p.get("target")
            if isinstance(target, int) and target in w.users:
                c.target_user = target
            elif me.nemesis_user is not None or me.nemesis_clan is not None:
                c.target_user, c.target_clan = me.nemesis_user, me.nemesis_clan
            elif active is not None and active.covers(me):
                c.target_user = active.hirer  # hunted, with no nemesis of your own: turn it on whoever hired it
            else:
                return "pick a target"
            if c.target_user in (user_id, bot) or (c.target_clan is not None and c.target_clan == me.clan_id):
                return "pick a target"
            targets = [x for x in w.users.values() if c.covers(x)]
            if any(self._contract(bot, x.id, "protect") for x in targets):
                return "target is under this bot's protection"
        if kind == "protect":
            mine = [
                x
                for x in w.contracts.values()
                if x.bot == bot and x.kind == "merc" and x.status in ("queued", "active")
            ]
            if any(x.covers(me) for x in mine):
                return "this bot is hunting you: outbid its contract instead"
        try:
            offer = int(p.get("offer") or 0)
        except (TypeError, ValueError, OverflowError):
            return "bad offer"
        price = max(quote(w, user_id, bot, kind, t), offer * 1000)
        start = p.get("start")
        if isinstance(start, int) and start > self.tick:
            c.start = start
        # Bought off: the target of an active mercenary contract offers outbid_factor x its price.
        broken = active is not None and active.covers(me) and price >= t.outbid_factor * active.price
        if not self.charge(user_id, price, f"hire:{kind}"):
            return "not enough coins"
        c.price = price
        w.contracts[c.id] = c
        if broken:
            served = (self.tick - active.began) / active.minutes if active.minutes else 1
            refund = int(active.price * max(0.0, 1 - served))
            if refund:
                w.users[active.hirer].treasury += refund
                self.out.ledger.append(_entry(active.hirer, refund, "refund"))
            active.status = "broken"
            c.status, c.began = "active", self.tick  # it switches sides now, ahead of anyone queued
            return "ok: contract broken"
        return "ok"

    def _order_cancel_contract(self, user_id: int, cid) -> str:
        """A queued contract can be cancelled for a full refund; an active one runs its course."""
        c = self.w.contracts.get(cid) if isinstance(cid, int) else None
        if c is None or c.hirer != user_id:
            return "no such contract"
        if c.status != "queued":
            return "already started"
        c.status = "cancelled"
        self.w.users[user_id].treasury += c.price
        self.out.ledger.append(_entry(user_id, c.price, "refund"))
        return "ok"

    def _order_buy_city(self, user_id: int, cell) -> str:
        w, t = self.w, self.t
        h = w.hexes.get(str(cell))
        if h is None or h.owner_id is None or not w.users[h.owner_id].bot:
            return "not a bot's city"
        if sum(1 for x in w.hexes.values() if x.owner_id == user_id) >= t.hex_cap:
            return "at your hex cap"
        if self.enemies_on(h.h3, h.owner_id):
            return "under siege"
        price = city_price(w, user_id, h, self.tick, t)
        if not self.charge(user_id, price, "buy_city"):
            return "not enough coins"
        w.users[h.owner_id].treasury += price  # the bot's purse, which killing it loots
        self.out.ledger.append(_entry(h.owner_id, price, "sold_city"))
        h.owner_id, h.owner_since_tick, h.flip_user_id, h.flip_progress = user_id, self.tick, None, 0
        return "ok"

    def _contract(self, bot: int, hirer: int, kind: str | None = None) -> Contract | None:
        return next(
            (c for c in self.w.contracts.values()
             if c.status == "active" and c.bot == bot and c.hirer == hirer and (kind is None or c.kind == kind)),
            None,
        )  # fmt: skip

    def contracts(self) -> None:
        """End contracts that ran their time, then start queued ones: protection whenever due, mercenary and
        guard jobs one at a time per bot, in the order they were hired."""
        w = self.w
        for c in sorted(w.contracts.values(), key=lambda c: c.id):
            if c.status == "active" and c.began + c.minutes <= self.tick:
                c.status = "done"
        busy = {c.bot for c in w.contracts.values() if c.status == "active" and c.kind != "protect"}
        for c in sorted(w.contracts.values(), key=lambda c: c.id):
            if c.status != "queued" or c.start > self.tick:
                continue
            if c.kind != "protect":
                if c.bot in busy:
                    continue
                busy.add(c.bot)
            c.status, c.began = "active", self.tick

    def _order_rename(self, user_id: int, cell, name) -> str:
        """Name one of your cities; an empty name clears it. Whitespace is collapsed."""
        h = self.w.hexes.get(str(cell))
        if h is None or h.owner_id != user_id:
            return "not your city"
        name = " ".join(str(name or "").split())
        if len(name) > CITY_NAME_MAX:
            return f"name longer than {CITY_NAME_MAX} characters"
        h.name = name or None
        return "ok"

    def _order_class(self, u: Unit, name) -> str:
        t = self.t
        if not isinstance(name, str) or name not in t.classes:
            return "unknown class"
        if name == u.unit_class:
            return "already that class"
        if u.battles_since_class < 1:
            return "needs a battle first"
        hours = t.class_swap_hours_subscriber if self.w.users[u.user_id].subscriber else t.class_swap_hours_free
        if u.class_changed_tick is not None and self.tick - u.class_changed_tick < hours * 3600 / t.tick_seconds:
            return "cooldown"
        u.unit_class, u.class_changed_tick, u.battles_since_class = name, self.tick, 0
        return "ok"

    def _order_respawn(self, u: Unit, cell) -> str:
        if u.on_map:
            return "not respawning"
        h = self.w.hexes.get(str(cell))
        if h is None or h.zone != "spawn" or not h.playable:
            return "not a spawn zone"  # user capital and clan hexes come with M4
        self.respawn(u, h.h3)
        return "ok"

    def mode_fees(self) -> None:
        if not self.t.l_march:
            return
        for u in self._units():
            if u.on_map and u.mode != "none" and not self.charge(u.user_id, self.t.l_march, u.mode):
                u.mode = "none"

    def move(self) -> None:
        tick_minutes = self.t.tick_seconds / 60
        working = {c.bot for c in self.w.contracts.values() if c.status == "active" and c.kind != "protect"}
        movers = [u for u in self._units() if u.on_map and u.path]
        movers.sort(key=lambda u: (u.order_seq if u.order_seq is not None else math.inf, u.id))
        for u in movers:
            cost = self.cost_for(u)
            # Bots amble at bot_speed; one on a mercenary or guard job moves like a player.
            slow = self.w.users[u.user_id].bot and u.user_id not in working
            minutes = tick_minutes * (self.t.bot_speed if slow else 1.0)
            r = advance(u.path, u.progress, cost, self.contact_for(u), minutes=minutes, march=u.mode == "double_march")
            for cell in r.steps:
                self.place(u, cell)
            if r.steps and u.mode == "double_march":
                self.marched.add(u.id)
            if r.steps and self.t.l_walk:
                fee = min(self.t.l_walk * len(r.steps), self.w.users[u.user_id].treasury)
                if fee:
                    self.charge(u.user_id, fee, "walk")
            u.path, u.progress = ([] if r.blocked else r.path), r.progress
            if r.contact:
                self.attacks.append((u.id, r.contact))

    def battles(self) -> None:
        # Same-tick entries (the defender also arrived this tick) first, then attacks on settled defenders.
        def same_tick(a: tuple[int, str]) -> bool:
            return any(e.arrival_tick == self.tick for e in self.enemies_on(a[1], self.w.units[a[0]].user_id))

        ordered = [a for a in self.attacks if same_tick(a)] + [a for a in self.attacks if not same_tick(a)]
        for att_id, cell in ordered:
            att = self.w.units[att_id]
            if not att.on_map:
                continue
            if att.shield_hp:  # a shielded unit can't attack: it stops short of the enemy
                att.path, att.progress = [], 0.0
                continue
            defenders = self.enemies_on(cell, att.user_id)
            if not defenders and self.garrisoned(cell, att.user_id):
                self.siege(att, cell)
                continue
            if not defenders:  # beaten by an earlier attacker this tick: walk in
                self.place(att, cell)
                att.path.pop(0)
                continue
            d = max(defenders, key=lambda e: (e.hp + e.shield_hp, -e.id))
            a_mods, d_mods = battle_modifiers(self.w, att, d, cell, self.t, same_tick=d.arrival_tick == self.tick)
            seed = battle_seed(self.secret, self.tick, len(self.out.battles), self.beacon)
            r = resolve_battle(
                Side(unit_id=str(att.id), hp=att.hp, modifiers=a_mods),
                Side(unit_id=str(d.id), hp=d.hp + d.shield_hp, modifiers=d_mods),
                Dice(seed),
                self.t,
            )
            self.out.battles.append(
                BattleRecord(
                    hex=cell, attacker_unit=att.id, defender_unit=d.id, seed=seed, result=r, defender_user=d.user_id
                )
            )
            att.hp = r.attacker_hp_after
            absorbed = min(d.shield_hp, d.hp + d.shield_hp - r.defender_hp_after)
            d.shield_hp -= absorbed
            d.hp = r.defender_hp_after - d.shield_hp
            if absorbed and d.shield_hp == 0:
                self.end_shield(d, broken=True)
            self.out.battles[-1].shield_absorbed = absorbed
            att.battles_since_class += 1
            d.battles_since_class += 1
            if r.outcome is Outcome.ATTACKER_WON:
                self.kill(att, d)
                if not self.enemies_on(cell, att.user_id):
                    self.place(att, cell)
                    att.path.pop(0)
                att.progress = 0.0
            elif r.outcome is Outcome.DEFENDER_WON:
                self.kill(d, att)
            else:  # both wounded: the attacker stays where it attacked from
                att.path, att.progress = [], 0.0

    def siege(self, att: Unit, cell: str) -> None:
        """Battle against an empty enemy city's garrison. Beating it lets the capture timer start."""
        h = self.w.hexes[cell]
        g = garrison_unit(self.w, cell)
        a_mods, d_mods = battle_modifiers(self.w, att, g, cell, self.t)
        d_mods = [m for m in d_mods if m.value]
        seed = battle_seed(self.secret, self.tick, len(self.out.battles), self.beacon)
        r = resolve_battle(
            Side(unit_id=str(att.id), hp=att.hp, modifiers=a_mods),
            Side(unit_id=f"garrison:{cell}", hp=g.hp, modifiers=d_mods),
            Dice(seed),
            self.t,
        )
        self.out.battles.append(
            BattleRecord(
                hex=cell, attacker_unit=att.id, defender_unit=None, seed=seed, result=r, defender_user=h.owner_id
            )
        )
        att.hp, h.garrison_hp = r.attacker_hp_after, r.defender_hp_after
        att.battles_since_class += 1
        if r.outcome is Outcome.ATTACKER_WON:
            self.place(att, cell)
            att.path.pop(0)
            att.progress = 0.0
        elif r.outcome is Outcome.DEFENDER_WON:
            self.loot(att, h.owner_id)
            att.kills = 0
            self.fall(att)
        else:
            att.path, att.progress = [], 0.0

    def respawn_timeouts(self) -> None:
        spawns = sorted(h.h3 for h in self.w.hexes.values() if h.zone == "spawn" and h.playable)
        if not spawns:
            return
        for u in self._units():
            if u.respawn_deadline is not None and u.respawn_deadline <= self.tick:
                self.respawn(u, min(spawns, key=lambda s, u=u: (h3.grid_distance(s, u.hex), s)))

    def ownership(self) -> None:
        w, t = self.w, self.t
        held: dict[int, int] = defaultdict(int)
        for h in w.hexes.values():
            if h.owner_id is not None:
                held[h.owner_id] += 1
        present: dict[str, int] = {}  # land hex -> the user whose unit holds it (lowest unit id)
        for u in self._units():
            if u.on_map and u.hp > 0 and w.hexes[u.hex].zone == "land":
                present.setdefault(u.hex, u.user_id)
        for cell in sorted(present):
            h, user = w.hexes[cell], present[cell]
            cap = t.hex_cap  # hex_cap_with_capital once user capitals exist (M4)
            if h.owner_id is None:
                if held[user] < cap:
                    self._transfer(h, user, held)
            elif not w.allied(h.owner_id, user) and not (h.garrison_hp or 0):
                if h.flip_user_id != user:
                    h.flip_user_id, h.flip_progress = user, 0
                h.flip_progress += 1
                need = flip_needed(h, self.tick, t)
                if h.flip_progress >= need and held[user] < cap:
                    self._transfer(h, user, held)
        for h in w.hexes.values():  # leaving resets the flip count
            if h.flip_user_id is not None and present.get(h.h3) != h.flip_user_id:
                h.flip_user_id, h.flip_progress = None, 0

    def _transfer(self, h, user: int, held: dict[int, int]) -> None:
        """A claim of open land comes with a full garrison; a captured city's garrison is in ruins."""
        h.garrison_hp = self.t.city_garrison_hp if h.owner_id is None else 0
        if h.owner_id is not None:
            held[h.owner_id] -= 1
        h.owner_id, h.owner_since_tick = user, self.tick
        h.flip_user_id, h.flip_progress = None, 0
        h.is_user_capital = False
        held[user] += 1

    def bonuses(self) -> None:
        t = self.t
        for u in self._units():
            if not u.on_map:
                continue
            if u.id in self.moved:
                u.defence_bonus = t.defence_on_arrival
                if u.id in self.marched:
                    u.attack_bonus = 0.0
            else:
                u.defence_bonus = min(t.defence_max, u.defence_bonus + t.bonus_step)
                u.attack_bonus = min(t.attack_bonus, u.attack_bonus + t.bonus_step)

    def heal(self) -> None:
        w, t = self.w, self.t
        for u in self._units():
            if not u.on_map or u.hp <= 0:
                continue
            h = w.hexes[u.hex]
            rate = t.heal_anywhere
            if h.zone == "land" and h.owner_id is not None and w.allied(h.owner_id, u.user_id):
                rate = t.heal_own_land
            if h.is_user_capital and h.owner_id == u.user_id:
                rate = t.heal_own_capital
            if u.mode == "heal":
                rate *= t.heal_mode_factor
            rate = round(rate * self.spec(u).heal_mult)
            # The strongest allied medic on the hex (not the unit itself) adds its heal.
            rate += max(
                (
                    self.spec(o).heals_allies
                    for o in self._on_hex(u.hex)
                    if o.id != u.id and w.allied(o.user_id, u.user_id)
                ),
                default=0,
            )
            u.hp = min(t.max_hp, u.hp + rate)

    def _on_hex(self, cell: str) -> list[Unit]:
        return [self.w.units[i] for i in sorted(self.occ.get(cell, ())) if self.w.units[i].hp > 0]

    def income(self) -> None:
        w, t = self.w, self.t
        per_tick = t.tick_seconds / 3600
        earned: dict[int, float] = defaultdict(float)
        for cell in sorted(w.hexes):
            h = w.hexes[cell]
            if h.owner_id is not None:
                earned[h.owner_id] += hex_income_per_hour(h, self.tick, t) * per_tick * 1000
        for user_id in sorted(earned):
            amount = round(earned[user_id])
            user = w.users[user_id]
            if t.treasury_cap is not None:
                amount = max(0, min(amount, t.treasury_cap - user.treasury))
            if amount:
                user.treasury += amount
                self.out.ledger.append(_entry(user_id, amount, "income"))

    def fog(self) -> None:
        w, t = self.w, self.t
        requests = self.out.bake_requests
        for cell in sorted(w.hexes):
            h = w.hexes[cell]
            if h.playable:
                continue
            owners = {w.hexes[n].owner_id for n in h3.grid_ring(cell, 1) if n in w.hexes and w.hexes[n].owner_id}
            owned = sum(1 for n in h3.grid_ring(cell, 1) if n in w.hexes and w.hexes[n].owner_id is not None)
            if owned < 2:
                continue
            missing = [c for c in h.chunks if c not in w.ready_chunks]
            if not missing:
                h.playable = True
                continue
            for c in missing:
                for near in lookahead(c, t.lookahead):
                    if near not in w.ready_chunks:
                        requests[near] = max(requests.get(near, 0), len(owners))

    def _units(self) -> list[Unit]:
        return [self.w.units[i] for i in sorted(self.w.units)]


def hex_income_per_hour(h: Hex, tick: int, t: Tunables = DEFAULT) -> float:
    """Coins an hour the hex pays its owner at `tick`: tier income x hold ramp x capital multiplier."""
    held = tick - (h.owner_since_tick if h.owner_since_tick is not None else tick)
    ramp_ticks = t.hold_ramp_hours * 3600 / t.tick_seconds
    ramp = 1 + (t.hold_ramp_max - 1) * min(1.0, held / ramp_ticks)
    return t.income_per_hour[(h.tier or 1) - 1] * ramp * (2 if h.is_user_capital else 1)


def garrison_max(h: Hex, t: Tunables = DEFAULT) -> int:
    return t.garrison_hp if h.is_user_capital else t.city_garrison_hp


def garrison_unit(w: World, cell: str) -> Unit:
    """The garrison as a stand-in unit, so battle maths (and the bots' odds) treat it like a defender."""
    h = w.hexes[cell]
    return Unit(id=-1, user_id=h.owner_id, hex=cell, hp=h.garrison_hp or 0, defence_bonus=0.0)


def _ticks(seconds: float, t: Tunables) -> int:
    return round(seconds / t.tick_seconds)


def _day(tick: int, t: Tunables) -> int:
    return tick // _ticks(86400, t)


def _new_day(u: Unit, tick: int, t: Tunables) -> None:
    if u.shield_used and u.shield_day != _day(tick, t):
        u.shield_used, u.shield_day = 0, _day(tick, t)


SERVICES = ("merc", "guard", "protect")


def income_per_hour(w: World, user: int, tick: int, t: Tunables = DEFAULT) -> float:
    return sum(hex_income_per_hour(h, tick, t) for h in w.hexes.values() if h.owner_id == user)


def price_factor(w: World, buyer: int, t: Tunables = DEFAULT) -> float:
    """How much a buyer pays relative to list: their income against the average, clamped."""
    incomes = [income_per_hour(w, u, w.tick, t) for u in w.users]
    mean = sum(incomes) / len(incomes) if incomes else 0
    if mean <= 0:
        return 1.0
    return min(t.price_factor_max, max(t.price_factor_min, income_per_hour(w, buyer, w.tick, t) / mean))


def service_base(w: World, bot: int, kind: str, t: Tunables = DEFAULT) -> float:
    """A bot service's list price in coins: more for a mercenary or guard with kills on its record."""
    base = getattr(t, f"{kind}_price")
    if kind != "protect":
        unit = next((u for u in w.units.values() if u.user_id == bot), None)
        base *= 1 + t.service_per_kill * (unit.kills if unit else 0)
    return base


def city_base(h: Hex, tick: int, t: Tunables = DEFAULT) -> float:
    """A bot city's list price in coins: hours of its income, at least city_price_min."""
    return max(t.city_price_min, hex_income_per_hour(h, tick, t) * t.city_price_hours)


def quote(w: World, buyer: int, bot: int, kind: str, t: Tunables = DEFAULT) -> int:
    """A bot service's price for `buyer`, in milli-coins."""
    return round(service_base(w, bot, kind, t) * price_factor(w, buyer, t) * 1000)


def city_price(w: World, buyer: int, h: Hex, tick: int, t: Tunables = DEFAULT) -> int:
    """What a bot sells one of its cities to `buyer` for, in milli-coins."""
    return round(city_base(h, tick, t) * price_factor(w, buyer, t) * 1000)


def flip_needed(h: Hex, tick: int, t: Tunables = DEFAULT) -> int:
    """Ticks an enemy must hold an owned hex (garrison beaten) to take it: longer the longer it was held."""
    return min(
        t.x_max, max(t.x_min, round((tick - (tick if h.owner_since_tick is None else h.owner_since_tick)) * t.r_flip))
    )


def _new_hunt_day(u: Unit, tick: int, t: Tunables) -> None:
    if u.hunt_used and u.hunt_day != _day(tick, t):
        u.hunt_used, u.hunt_day = 0, _day(tick, t)


def hunt_left(u: Unit, tick: int, t: Tunables = DEFAULT) -> int:
    used = u.hunt_used if u.hunt_day == _day(tick, t) else 0
    return max(0, _ticks(t.hunt_minutes_daily * 60, t) - used)


def nemesis_of(w: World, hunter: int | None, target: int | None) -> bool:
    """True when `hunter` is a subscriber whose nemesis covers `target` (only subscribers and bots can be one)."""
    h, x = w.users.get(hunter), w.users.get(target)
    if h is None or x is None or hunter == target or not h.subscriber or not (x.subscriber or x.bot):
        return False
    return h.nemesis_user == target or (h.nemesis_clan is not None and x.clan_id == h.nemesis_clan)


def shield_ready_in(u: Unit, tick: int, t: Tunables = DEFAULT) -> int:
    """Ticks until `u` may raise a shield at `tick` (0: now). With the day's budget spent, that's the next day."""
    wait = max(0, (u.shield_ready or 0) - tick)
    if u.shield_day == _day(tick, t) and u.shield_used >= _ticks(t.shield_daily_hours * 3600, t):
        wait = max(wait, (_day(tick, t) + 1) * _ticks(86400, t) - tick)
    return wait


def shield_left(u: Unit, tick: int, t: Tunables = DEFAULT) -> int:
    """Ticks of shield left today (whether or not it's up)."""
    used = u.shield_used if u.shield_day == _day(tick, t) else 0
    return max(0, _ticks(t.shield_daily_hours * 3600, t) - used)


def veteran_bonus(u: Unit, t: Tunables = DEFAULT) -> float:
    return min(t.veteran_max, t.veteran_step * u.kills) if u.veteran else 0.0


def battle_modifiers(
    w: World, att: Unit, d: Unit, cell: str, t: Tunables = DEFAULT, same_tick: bool = False
) -> tuple[list[Modifier], list[Modifier]]:
    """Every modifier on each side of a battle at `cell`. Bots use it too, to judge their odds."""
    specs = t.classes
    ac = specs.get(att.unit_class, specs[DEFAULT_CLASS])
    dc = specs.get(d.unit_class, specs[DEFAULT_CLASS])
    a_mods = [] if same_tick else [Modifier(source="attack_bonus", value=att.attack_bonus)]
    d_mods = [] if same_tick or ac.ignores_defence else [Modifier(source="defence_bonus", value=d.defence_bonus)]
    h = w.hexes[cell]
    if h.is_user_capital and h.owner_id == d.user_id and not ac.ignores_defence:
        d_mods.append(Modifier(source="capital", value=t.def_capital))
    # Class and veteran modifiers are part of the unit, so they apply even in a same-tick entry.
    if ac.attack:
        a_mods.append(Modifier(source=f"class:{att.unit_class}", value=ac.attack))
    if dc.defence:
        d_mods.append(Modifier(source=f"class:{d.unit_class}", value=dc.defence))
    if veteran_bonus(att, t):
        a_mods.append(Modifier(source="veteran", value=veteran_bonus(att, t)))
    if veteran_bonus(d, t):
        d_mods.append(Modifier(source="veteran", value=veteran_bonus(d, t)))
    if nemesis_of(w, att.user_id, d.user_id):
        a_mods.append(Modifier(source="nemesis", value=t.nemesis_bonus))
    if nemesis_of(w, d.user_id, att.user_id):
        d_mods.append(Modifier(source="nemesis", value=t.nemesis_bonus))
    return a_mods, d_mods


def _entry(user_id: int, amount: int, reason: str) -> LedgerEntry:
    return LedgerEntry(user_id=user_id, amount=amount, reason=reason)


def run_tick(
    world: World, orders: list[Order], secret: bytes, t: Tunables = DEFAULT, beacon: bytes | None = None
) -> TickOutput:
    """Apply one tick to a copy of `world`. Same inputs, same output. `beacon` is the tick's drand randomness."""
    k = _Tick(world, secret, t, beacon)
    k.apply_orders(orders)  # 1. orders
    k.contracts()  # 1a. bot contracts end and start
    k.shields()  # 1b. shields: budgets and schedules
    k.hunts()  # 1c. hunt mode picks its nemesis target
    k.pursuits()  # 1d. pursuers re-path to their targets
    k.mode_fees()  # 2. mode fees
    k.move()  # 3. movement
    k.battles()  # 4. battles (garrisons: M4)
    k.respawn_timeouts()  # 5. respawn (capital and clan spawning: M4)
    k.ownership()  # 6. claims and flips (over-cap choices, capital falls: M4)
    k.bonuses()  # 7. attack and defence bonuses
    k.heal()  # 8. healing
    k.income()  # 9. income and treasury cap
    k.fog()  # 10. fog unlock and bake requests
    # 11. AI levelling events: M5
    return k.out
