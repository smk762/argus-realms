"""Bots: opponents for the solo arena, and later the bot simulator and AI levelling events.

A bot plays by the same rules as a free player: it only issues orders (move, mode, class, respawn)
and the tick engine decides what happens. Each bot has a personality (how it picks targets, how bold
it is, when it falls back) and a class that suits it.

Bots are strength-aware: before attacking they estimate their odds with the engine's own battle
maths (`battle_modifiers` + `resolve_battle`). Below their personality's `risk` threshold they build
up first: heal to full, then farm softer targets for kills (each kill makes a bot stronger, see the
engine's veterancy and bounty rules), and only then take on the hard target.

Three things push back on whoever is winning, so balance problems show up as counter-play rather than
runaway games:
- the reaver raids a random hex of a player in the top 10% of earners, so the lords can't get lazy;
- any player holding more than `dominance_share` of the claimed land becomes every bot's preferred
  target, and bots grow bolder against them (the doc's AI levelling rule, applied to solo play);
- the rival bot always swaps to the class that answers the human's (ANSWERED_BY), under the same
  cooldown and battle rules as a free player.

Decisions are seeded per bot and per tick, so a solo game replays exactly.
"""

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache

import h3

from atlas_realms.rules.classes import ANSWERED_BY
from atlas_realms.rules.combat import Modifier, Outcome, Side, resolve_battle
from atlas_realms.rules.dice import Dice
from atlas_realms.rules.tunables import DEFAULT, Tunables
from atlas_realms.tick.engine import battle_modifiers, garrison_max, garrison_unit, shield_ready_in
from atlas_realms.tick.world import Unit, World

REPLAN_TICKS = 10  # a bot with a route re-thinks it this often, to chase moving targets
ODDS_SAMPLES = 200  # battles simulated per odds estimate (memoised on rounded inputs)
REAVER_RETARGET_TICKS = 120  # the reaver picks a new hex every 2 game hours, or after a kill


@dataclass(frozen=True, slots=True)
class Personality:
    name: str
    unit_class: str
    blurb: str
    aggression: float  # chance per decision to go after an enemy rather than expand
    caution: int  # falls back to its own land and heals below this HP
    roam: int  # how far from its spawn (in hexes) it ranges
    marches: bool = False  # double-marches into attacks
    prey: str = "nearest"  # nearest | weakest | dug_in | coastal | leader | top_earners
    counter_pick: bool = False  # swaps to the class that answers the human's
    risk: float = 0.55  # the lowest estimated chance of winning it will attack at


PERSONALITIES: dict[str, Personality] = {
    p.name: p
    for p in (
        Personality("settler", "barbarian", "Claims open land and holds it", 0.1, 50, 3, risk=0.65),
        Personality(
            "raider", "tank", "Hunts the nearest enemy, double-marching in", 0.8, 30, 8, marches=True, risk=0.45
        ),
        Personality("warden", "medic", "Stays close to home, heals, holds what it has", 0.2, 70, 2, risk=0.7),
        Personality("corsair", "marine", "Crosses the water to raid the coast", 0.6, 40, 10, prey="coastal", risk=0.5),
        Personality("sapper", "saboteur", "Goes after dug-in defenders", 0.6, 40, 6, prey="dug_in", risk=0.5),
        Personality("jackal", "barbarian", "Picks off wounded units", 0.7, 50, 6, prey="weakest", risk=0.6),
        Personality(
            "rival", "barbarian", "Plays whatever class answers yours", 0.6, 40, 8, prey="leader", counter_pick=True
        ),
        Personality(
            "reaver",
            "tank",
            "Raids the top 10% of earners so the lords can't get lazy",
            1.0,
            40,
            99,
            marches=True,
            prey="top_earners",
            risk=0.55,
        ),  # fmt: skip
    )
}


@dataclass(frozen=True, slots=True)
class Bot:
    user_id: int
    personality: Personality
    home: str  # its spawn hex


@dataclass(frozen=True, slots=True)
class BotOrder:
    user_id: int
    kind: str
    payload: dict


def leader(world: World, t: Tunables = DEFAULT) -> int | None:
    """The player over dominance_share of the claimed land, if any."""
    held: dict[int, int] = defaultdict(int)
    for h in world.hexes.values():
        if h.owner_id is not None:
            held[h.owner_id] += 1
    total = sum(held.values())
    if total < 4:
        return None
    top = max(sorted(held), key=lambda u: held[u])
    return top if held[top] / total > t.dominance_share else None


def decide(world: World, bot: Bot, human_id: int, seed: str, t: Tunables = DEFAULT) -> list[BotOrder]:
    """The orders one bot submits before the next tick."""
    p, me = bot.personality, bot.user_id
    unit = next((u for u in world.units.values() if u.user_id == me), None)
    if unit is None:
        return []
    out: list[BotOrder] = []

    def order(kind: str, **payload) -> None:
        out.append(BotOrder(me, kind, payload))

    if not unit.on_map:
        order("respawn", to=bot.home)
        return out
    rng = random.Random(f"{seed}:{world.tick}:{me}")

    if p.counter_pick:
        human = next((u for u in world.units.values() if u.user_id == human_id), None)
        want = ANSWERED_BY.get(human.unit_class, "barbarian") if human else p.unit_class
        cooldown = t.class_swap_hours_free * 3600 / t.tick_seconds
        ready = unit.class_changed_tick is None or world.tick + 1 - unit.class_changed_tick >= cooldown
        if want != unit.unit_class and unit.battles_since_class >= 1 and ready:
            order("class", **{"class": want})

    own = {c for c, h in world.hexes.items() if h.owner_id == me}

    def fall_back() -> None:
        """Go to its own land (or home) and heal there."""
        if unit.hex in own or world.hexes[unit.hex].zone == "spawn":
            if unit.path:
                order("cancel")
            if unit.mode != "heal":
                order("mode", mode="heal")
        else:
            refuge = _nearest(unit.hex, own | {bot.home})
            if refuge and (not unit.path or unit.path[-1] != refuge):
                if unit.mode != "none":
                    order("mode", mode="none")
                order("move", to=refuge)

    def go(dest: str | None, march: bool = False) -> None:
        if march and p.marches and unit.mode != "double_march":
            order("mode", mode="double_march")
        elif not march and unit.mode == "double_march":
            order("mode", mode="none")
        if dest is not None and dest != unit.hex and (not unit.path or unit.path[-1] != dest):
            order("move", to=dest)

    def build_up() -> None:
        """Too weak for the target: heal to full, else farm the softest enemy it can beat, else hold."""
        if unit.hp < t.max_hp:
            fall_back()
            return
        soft = max(_enemies(world, bot), key=lambda o: (odds(world, unit, o, t), -o.id), default=None)
        if soft is not None and odds(world, unit, soft, t) >= p.risk:
            go(soft.hex, march=True)
        else:
            go(_expansion(world, unit, bot, own, rng, t))

    if unit.shield_hp:  # a shielded unit can't attack: heal under it, then drop it to fight again
        if unit.hp >= min(t.max_hp, p.caution + 30):
            order("shield", on=False)
        else:
            fall_back()
            return out
    elif unit.hp < p.caution:
        # Hurt with an enemy two hexes off or closer: raise a shield to get home.
        near = any(h3.grid_distance(o.hex, unit.hex) <= 2 for o in _enemies(world, bot, anywhere=True))
        if near and not shield_ready_in(unit, world.tick + 1, t):
            order("shield")
        fall_back()
        return out
    # Repair the weakest damaged city while there are coins for it (spending the purse it would be looted for).
    fee = t.fee_repair or 0
    damaged = sorted(
        (world.hexes[c].garrison_hp or 0, c)
        for c in own
        if (world.hexes[c].garrison_hp or 0) < garrison_max(world.hexes[c], t)
    )
    if damaged and world.users[me].treasury >= 2 * fee * (garrison_max(world.hexes[damaged[0][1]], t) - damaged[0][0]):
        order("repair", hex=damaged[0][1])
    if unit.mode == "heal" and unit.hp >= min(t.max_hp, p.caution + 30):
        order("mode", mode="none")
    if unit.path and (world.tick + me) % REPLAN_TICKS:
        return out  # keep going; re-think every REPLAN_TICKS

    # On a job it stays itself (heals, repairs, judges its odds), but the job picks its targets, anywhere.
    job = next(
        (c for c in world.contracts.values() if c.bot == me and c.status == "active" and c.kind != "protect"), None
    )
    if job is not None and job.kind == "merc":
        marks = {x.id for x in world.users.values() if job.covers(x)}
        prey = [o for o in _enemies(world, bot, anywhere=True) if o.user_id in marks]
        if prey:
            target = min(prey, key=lambda o: (h3.grid_distance(unit.hex, o.hex), o.id))
            if odds(world, unit, target, t) >= p.risk:
                go(target.hex, march=True)
            else:
                build_up()
            return out
        lands = {c for c, h in world.hexes.items() if h.owner_id in marks}
        if lands:
            go(_nearest(unit.hex, lands), march=True)
            return out
    if job is not None and job.kind == "guard":
        lands = {c for c, h in world.hexes.items() if h.owner_id == job.hirer}
        threats = [
            o for o in _enemies(world, bot, anywhere=True) if any(h3.grid_distance(o.hex, c) <= 1 for c in lands)
        ]
        if threats:
            go(min(threats, key=lambda o: (h3.grid_distance(unit.hex, o.hex), o.id)).hex, march=True)
            return out
        if lands and (not unit.path or unit.hex in lands):
            go(rng.choice(sorted(lands - {unit.hex}) or sorted(lands)))  # patrol
            return out

    if p.prey == "top_earners":
        goal = _reaver_target(world, bot, unit, seed, t)
        if goal is None:
            go(_expansion(world, unit, bot, own, rng, t))
            return out
        guards = [o for o in _enemies(world, bot, anywhere=True) if o.hex == goal]
        guard = max(guards, key=lambda o: (o.hp, -o.id), default=None)
        if guard is None and (world.hexes[goal].garrison_hp or 0) > 0:
            guard = garrison_unit(world, goal)
        if guard is None or odds(world, unit, guard, t) >= p.risk:
            go(goal, march=True)
        else:
            build_up()
        return out

    top = leader(world, t)
    target = _prey(world, unit, bot, top, rng)
    boldness = p.aggression + (0.3 if top is not None and top != me else 0.0)
    if target is not None and rng.random() < boldness:
        if odds(world, unit, target, t) >= p.risk:
            go(target.hex, march=True)
        else:
            build_up()
    else:
        go(_expansion(world, unit, bot, own, rng, t))
    return out


def odds(world: World, att: Unit, d: Unit, t: Tunables = DEFAULT) -> float:
    """Estimated chance that `att` wins outright attacking `d` where it stands, as the engine would fight it."""
    a_mods, d_mods = battle_modifiers(world, att, d, d.hex, t)
    return _odds(
        _round(att.hp, 5),
        round(sum(m.value for m in a_mods), 2),
        _round(d.hp + d.shield_hp, 5),  # a shield is fought through first
        round(sum(m.value for m in d_mods), 2),
    )


def _round(hp: int, step: int) -> int:
    return max(step, step * round(hp / step))


@lru_cache(maxsize=4096)
def _odds(a_hp: int, a_mod: float, d_hp: int, d_mod: float) -> float:
    wins = 0
    for i in range(ODDS_SAMPLES):
        r = resolve_battle(
            Side(unit_id="a", hp=a_hp, modifiers=[Modifier(source="all", value=a_mod)]),
            Side(unit_id="d", hp=d_hp, modifiers=[Modifier(source="all", value=d_mod)]),
            Dice(b"odds" + i.to_bytes(4, "big")),
        )
        wins += r.outcome is Outcome.ATTACKER_WON
    return wins / ODDS_SAMPLES


def earnings(world: World, t: Tunables = DEFAULT) -> dict[int, float]:
    """Coins per hour each player's hexes pay, before the hold ramp."""
    out: dict[int, float] = defaultdict(float)
    for h in world.hexes.values():
        if h.owner_id is not None:
            out[h.owner_id] += t.income_per_hour[(h.tier or 1) - 1] * (2 if h.is_user_capital else 1)
    return dict(out)


def top_earners(world: World, t: Tunables = DEFAULT, share: float = 0.10) -> list[int]:
    """The top `share` of players with any income (at least one), richest first."""
    e = earnings(world, t)
    ranked = sorted(e, key=lambda u: (-e[u], u))
    return ranked[: max(1, math.ceil(share * len(ranked)))] if ranked else []


def _reaver_target(world: World, bot: Bot, unit: Unit, seed: str, t: Tunables) -> str | None:
    """A random hex of a top earner (never its own), stable until the reaver kills or the epoch turns."""
    lords = [u for u in top_earners(world, t) if not world.allied(u, bot.user_id)]
    if not lords:
        ranked = sorted(earnings(world, t).items(), key=lambda kv: (-kv[1], kv[0]))
        lords = [u for u, _ in ranked if not world.allied(u, bot.user_id)][:1]
    cities = sorted(c for c, h in world.hexes.items() if h.owner_id in lords)
    if not cities:
        return None
    pick = random.Random(f"{seed}:{bot.user_id}:reave:{unit.kills}:{world.tick // REAVER_RETARGET_TICKS}")
    return pick.choice(cities)


def _nearest(start: str, cells: set[str]) -> str | None:
    return min(sorted(cells), key=lambda c: h3.grid_distance(start, c), default=None)


def _in_range(bot: Bot, cell: str) -> bool:
    return h3.grid_distance(bot.home, cell) <= bot.personality.roam


def _enemies(world: World, bot: Bot, anywhere: bool = False) -> list[Unit]:
    """Enemy units it could fight: on land (fights happen nowhere else) and, unless `anywhere`, in range."""
    return [
        o
        for o in sorted(world.units.values(), key=lambda o: o.id)
        if o.on_map
        and o.hp > 0
        and not world.allied(o.user_id, bot.user_id)
        and world.hexes[o.hex].zone == "land"
        and (anywhere or _in_range(bot, o.hex))
    ]


def _prey(world: World, unit: Unit, bot: Bot, top: int | None, rng: random.Random) -> Unit | None:
    enemies = _enemies(world, bot)
    if not enemies:
        return None
    if top is not None and top != bot.user_id:
        leaders = [o for o in enemies if o.user_id == top]
        if leaders:
            enemies = leaders
    near = lambda o: (h3.grid_distance(unit.hex, o.hex), o.id)  # noqa: E731
    kind = bot.personality.prey
    if kind == "weakest":
        return min(enemies, key=lambda o: (o.hp, near(o)))
    if kind == "dug_in":
        return max(enemies, key=lambda o: (o.defence_bonus, -near(o)[0], -o.id))
    if kind == "coastal":
        coast = [o for o in enemies if any(world.hexes.get(n) and world.hexes[n].zone == "water"
                                           for n in h3.grid_ring(o.hex, 1))]  # fmt: skip
        return min(coast or enemies, key=near)
    return min(enemies, key=near)


def _expansion(world: World, unit: Unit, bot: Bot, own: set[str], rng: random.Random, t: Tunables) -> str | None:
    """A hex to claim next: open land in range while under the cap, else an enemy hex next to its own."""
    occupied = {o.hex for o in world.units.values() if o.on_map and o.user_id != bot.user_id}
    land = [
        c
        for c, h in sorted(world.hexes.items())
        if h.zone == "land" and h.playable and c not in occupied and _in_range(bot, c)
    ]
    if len(own) < t.hex_cap:
        free = [c for c in land if world.hexes[c].owner_id is None]
    else:
        border = {n for c in own for n in h3.grid_ring(c, 1)}
        free = [
            c
            for c in land
            if c in border
            and world.hexes[c].owner_id not in (None, bot.user_id)
            and not world.allied(world.hexes[c].owner_id, bot.user_id)
            # A garrisoned city is only worth besieging at odds the bot accepts.
            and (
                not world.hexes[c].garrison_hp or odds(world, unit, garrison_unit(world, c), t) >= bot.personality.risk
            )
        ]
    if not free:
        return None
    best = min(h3.grid_distance(unit.hex, c) for c in free)
    return rng.choice([c for c in free if h3.grid_distance(unit.hex, c) == best])
