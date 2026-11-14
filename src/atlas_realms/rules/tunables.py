"""Every number the design doc marks as tunable, with its starting value.

One frozen model so a tick can record exactly which values it ran with, and playtests
can override a field without touching rule code. Values the doc leaves TBD are None.
"""

from pydantic import BaseModel, ConfigDict, Field

from atlas_realms.rules.classes import CLASSES, ClassSpec


class Tunables(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tick_seconds: int = 60
    # Start coarse; refine 5 -> 6 -> 7 as the map fills (rules/split.py). ~268 km2, 17.6 km across in NZ.
    h3_resolution: int = 5

    # Combat: up to battle_rolls opposed d100 rolls, the loser of each takes damage_min..damage_max.
    battle_rolls: int = 10
    damage_min: int = 16
    damage_max: int = 22
    max_hp: int = 100
    respawn_hp: int = 50

    # Bonuses as fractions (0.15 = +15%).
    attack_bonus: float = 0.15
    defence_on_arrival: float = 0.05
    bonus_step: float = 0.05
    defence_max: float = 0.20
    def_capital: float = 0.25

    # Movement: walking speed on neutral or enemy land; a hex costs walk_kmh's crossing time x its multiplier
    # (movement.hex_minutes). At res 5 that is ~10 min per hex. Double march halves every cost.
    walk_kmh: float = 100
    bot_speed: float = 0.5  # bots move at this fraction of normal speed, except on a mercenary or guard job.0
    cost_land: float = 1.0
    cost_own_land: float = 0.5
    cost_water: float = 2.0

    # Ownership. Caps grow by split_cap_growth (rounded up) each time the grid is split to a finer resolution.
    hex_cap: int = 3
    cap_share: float = 1.68  # solo: the cap is claimable hexes / players x cap_share
    start_coins: float = 10  # what every player starts a game with
    hex_cap_with_capital: int = 12
    split_cap_growth: float = 1.5
    r_flip: float = 1 / 60  # flip ticks per tick the prior owner held the hex (1 per hour held)
    x_min: int = 2
    x_max: int = 60
    garrison_hp: int = 100  # a user capital's garrison
    city_garrison_hp: int = 50  # every other city's; a claim starts full, a captured city starts at 0

    # Shields soak damage before HP, with shield_hp_mult x max_hp of their own; a shielded unit can't attack.
    # The same for every player: toggled on and off within shield_daily_hours a game day, shield_cooldown_minutes
    # between. A breached shield is broken: shield_breach_cooldown_hours before the next.
    shield_hp_mult: int = 5
    shield_daily_hours: float = 12
    shield_cooldown_minutes: float = 15
    shield_breach_cooldown_hours: float = 12

    # Nemesis (subscribers): one target (a subscriber, a bot or a clan's subscribed and bot members), changed at most
    # once per nemesis_change_hours. Fights between hunter and nemesis give the hunter nemesis_bonus attack and defence;
    # hunt mode chases the nemesis for up to hunt_minutes_daily a game day.
    nemesis_bonus: float = 0.05
    nemesis_change_hours: float = 24
    hunt_minutes_daily: float = 60

    # Bot services and sales, in coins. Every price is scaled by the buyer's income against the average
    # (price_factor_min..price_factor_max), so the leaders pay more and the underdogs less.
    merc_minutes: int = 15
    guard_minutes: int = 60
    protect_minutes: int = 24 * 60
    merc_price: float = 10
    guard_price: float = 15
    protect_price: float = 20
    service_per_kill: float = 0.10  # a mercenary or guard costs this much more per kill on its record
    city_price_hours: float = 24  # a bot sells a city for this many hours of its income...
    city_price_min: float = 5  # ...and never for less
    price_factor_min: float = 0.5
    price_factor_max: float = 3.0
    outbid_factor: float = 2.0  # a mercenary's target can buy it off with this multiple of its contract

    # Healing, HP per tick. The best rate that applies is used; heal mode multiplies it.
    heal_anywhere: int = 10
    heal_own_land: int = 20
    heal_own_capital: int = 40
    heal_mode_factor: int = 2

    # Income: coins per hour by value tier 1-5, ramping from 1x to hold_ramp_max over hold_ramp_hours of holding.
    # Money is stored as integer milli-coins (1 coin = 1000) and rounded per player per tick.
    income_per_hour: tuple[int, int, int, int, int] = (1, 2, 4, 7, 12)
    hold_ramp_max: float = 1.5
    hold_ramp_hours: int = 7 * 24

    # Economy. None = not decided yet. Fees are in milli-coins.
    l_walk: int = 0
    l_march: int | None = None
    fee_capital: int | None = None
    fee_repair: int | None = 50  # milli-coins per garrison HP (provisional: a full city repair is 2.5 coins)
    treasury_cap: int | None = None

    # Fog of war: chunks are XYZ tiles baked by argus-atlas at chunk_size px plus chunk_margin px of seam margin.
    chunk_zoom: int = 10
    chunk_size: int = 1024
    chunk_margin: int = 128
    lookahead: int = 1  # rings of chunks queued around one a player is about to reach

    # Classes (rules/classes.py). A swap needs the cooldown since the last one and a battle since then.
    classes: dict[str, ClassSpec] = Field(default_factory=lambda: dict(CLASSES))
    class_swap_hours_free: int = 7 * 24
    class_swap_hours_subscriber: int = 7

    # Bots: +veteran_step attack and defence per kill (to veteran_max). Killing one loots its purse (milli-coins):
    # bounty_base + bounty_per_kill x kills, capped at bounty_cap and at what it holds.
    veteran_step: float = 0.05
    veteran_max: float = 0.30
    bounty_base: int = 5_000
    bounty_per_kill: int = 5_000
    bounty_cap: int = 50_000

    respawn_timeout: int = 5
    choice_timeout: int = 10
    invite_ttl_hours: int = 48
    min_account_age_days: int = 3
    dominance_share: float = 0.25
    warning_ticks: int = 60


DEFAULT = Tunables()
