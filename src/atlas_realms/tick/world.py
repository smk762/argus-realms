"""In-memory world state for one tick: plain dataclasses, cheap to copy and compare."""

import copy
from dataclasses import dataclass, field
from typing import Literal

Zone = Literal["spawn", "corridor", "land", "water", "fog"]
Mode = Literal["none", "double_march", "heal"]


@dataclass(slots=True)
class Hex:
    h3: str
    zone: Zone
    terrain: str = "flat"
    tier: int | None = None
    playable: bool = True  # False = still under fog
    chunks: tuple[tuple[int, int, int], ...] = ()  # every chunk the hex's outline touches
    owner_id: int | None = None
    owner_since_tick: int | None = None
    flip_user_id: int | None = None
    flip_progress: int = 0
    is_user_capital: bool = False
    garrison_hp: int | None = None  # the city's own defence while no unit of its owner stands on it
    name: str | None = None  # set by its owner; kept when the city changes hands


@dataclass(slots=True)
class Unit:
    id: int
    user_id: int
    hex: str  # where the unit stands, or where it fell while respawning
    hp: int
    path: list[str] = field(default_factory=list)
    progress: float = 0.0
    mode: Mode = "none"
    attack_bonus: float = 0.15
    defence_bonus: float = 0.05
    arrival_tick: int | None = None
    respawn_deadline: int | None = None  # set while the unit is off the map
    order_seq: int | None = None
    unit_class: str = "barbarian"
    class_changed_tick: int | None = None
    battles_since_class: int = 0
    kills: int = 0  # since the unit last fell
    veteran: bool = False  # bots: kills make it stronger and put a bounty on it
    shield_hp: int = 0  # soaks damage before hp while up
    shield_ready: int | None = None  # the first tick a shield may be raised again
    shield_used: int = 0  # ticks shielded on game day shield_day
    shield_day: int | None = None
    shield_from: int | None = None  # subscribers' schedule: raise at this minute of the game day...
    shield_to: int | None = None  # ...and drop at this one
    pursue: int | None = None  # the user whose unit this one is chasing (revenge)
    hunting: bool = False  # hunt mode: chasing its player's nemesis
    hunt_used: int = 0  # ticks of hunt mode on game day hunt_day
    hunt_day: int | None = None

    @property
    def on_map(self) -> bool:
        return self.respawn_deadline is None


@dataclass(slots=True)
class User:
    id: int
    treasury: int = 0  # milli-coins
    clan_id: int | None = None
    subscriber: bool = False
    bot: bool = False
    nemesis_user: int | None = None  # a subscriber's nemesis: one user...
    nemesis_clan: int | None = None  # ...or one clan
    nemesis_tick: int | None = None  # when it was last set


@dataclass(slots=True)
class Order:
    id: int
    user_id: int
    kind: str  # move, cancel, mode, respawn
    payload: dict
    seq: int  # submit order across all orders this tick


@dataclass(slots=True)
class World:
    tick: int  # the last tick applied
    hexes: dict[str, Hex]
    units: dict[int, Unit]
    users: dict[int, User]
    ready_chunks: set[tuple[int, int, int]] = field(default_factory=set)
    contracts: dict[int, "Contract"] = field(default_factory=dict)

    def allied(self, a: int, b: int) -> bool:
        """Same player, same clan, or a bot and whoever has it under an active contract."""
        if a == b:
            return True
        ca, cb = self.users[a].clan_id, self.users[b].clan_id
        if ca is not None and ca == cb:
            return True
        return any(c.status == "active" and {c.bot, c.hirer} == {a, b} for c in self.contracts.values())

    def snapshot(self) -> "World":
        """An independent copy for a tick to mutate: equal to `copy.deepcopy(self)`, but it only copies what can change
        in place. Every field of Hex, User and Contract is a scalar or a tuple, so a shallow copy of each is a deep one;
        Unit's `path` is the one list. `ready_chunks` holds tuples. On a big map (Iceland, ~1,650 hexes and ~1,500
        chunks) deepcopy was 95% of the tick. `test_snapshot_is_a_deep_copy` fails if a mutable field is added."""
        units = {}
        for k, u in self.units.items():
            units[k] = c = copy.copy(u)
            c.path = list(u.path)
        return World(
            tick=self.tick,
            hexes={k: copy.copy(h) for k, h in self.hexes.items()},
            units=units,
            users={k: copy.copy(u) for k, u in self.users.items()},
            ready_chunks=set(self.ready_chunks),
            contracts={k: copy.copy(c) for k, c in self.contracts.items()},
        )


@dataclass(slots=True)
class Contract:
    """A bot's paid service: mercenary (against a target), guard (the hirer's land) or protection (a truce)."""

    id: int
    bot: int
    hirer: int
    kind: str  # "merc" | "guard" | "protect"
    price: int  # milli-coins, paid when hired
    start: int  # the earliest tick it may start; queued until then and until the bot is free
    minutes: int
    target_user: int | None = None  # merc: who it hunts...
    target_clan: int | None = None  # ...or which clan
    status: str = "queued"  # queued | active | done | broken | cancelled
    began: int | None = None  # the tick it went active

    def covers(self, user: "User") -> bool:
        """True when this mercenary contract is aimed at `user`."""
        return self.target_user == user.id or (self.target_clan is not None and user.clan_id == self.target_clan)
