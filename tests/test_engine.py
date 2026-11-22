import copy

import h3
import pytest

from atlas_realms.rules.combat import Outcome
from atlas_realms.rules.movement import hex_minutes
from atlas_realms.rules.tunables import DEFAULT, Tunables
from atlas_realms.tick.engine import battle_modifiers, nemesis_of, run_tick
from atlas_realms.tick.world import Hex, Order, Unit, User, World

SECRET = b"test-day-secret"
CENTRE = h3.latlng_to_cell(-41.2865, 174.7762, DEFAULT.h3_resolution)
TICKS_PER_HEX = int(hex_minutes()) + 1  # 10.24 min -> the 11th tick


def make_world(spawn: str | None = CENTRE, radius: int = 3) -> World:
    hexes = {c: Hex(h3=c, zone="land", tier=1) for c in h3.grid_disk(CENTRE, radius)}
    if spawn:
        hexes[spawn].zone = "spawn"
    return World(tick=0, hexes=hexes, units={}, users={})


def add_player(w: World, uid: int, at: str, hp: int = 100, clan: int | None = None) -> Unit:
    w.users[uid] = User(id=uid, clan_id=clan)
    u = Unit(id=uid, user_id=uid, hex=at, hp=hp)
    w.units[uid] = u
    return u


def step(w: World, orders: list[Order] | None = None, t: Tunables = DEFAULT, n: int = 1):
    out = None
    for _ in range(n):
        out = run_tick(w, orders or [], SECRET, t)
        w, orders = out.world, []
    return out


def move(uid: int, to: str, seq: int = 1) -> Order:
    return Order(id=seq, user_id=uid, kind="move", payload={"to": to}, seq=seq)


RING1 = sorted(h3.grid_ring(CENTRE, 1))


def test_tick_is_pure_and_replayable():
    w = make_world()
    add_player(w, 1, CENTRE)
    before = copy.deepcopy(w)
    a = run_tick(w, [move(1, RING1[0])], SECRET)
    b = run_tick(w, [move(1, RING1[0])], SECRET)
    assert w == before  # the input world is never touched
    assert a == b
    assert a.world.tick == 1


def test_unit_crosses_one_hex_in_eleven_ticks_and_claims_it():
    w = make_world()
    add_player(w, 1, CENTRE)
    out = step(w, [move(1, RING1[0])], n=TICKS_PER_HEX - 1)
    assert out.world.units[1].hex == CENTRE
    out = step(out.world)
    u = out.world.units[1]
    assert u.hex == RING1[0] and u.path == []
    assert out.world.hexes[RING1[0]].owner_id == 1
    assert out.order_results == {}  # orders were consumed on the first tick


def test_bad_orders_are_reported():
    w = make_world()
    add_player(w, 1, CENTRE)
    far = h3.grid_ring(CENTRE, 6)[0]  # off the map
    out = step(w, [move(1, far, seq=1), Order(id=2, user_id=9, kind="move", payload={"to": CENTRE}, seq=2)])
    assert out.order_results == {1: "unreachable", 2: "no unit"}


def test_spawn_zone_is_never_claimed_and_cap_limits_claims():
    w = make_world()
    u = add_player(w, 1, CENTRE)
    out = step(w)
    assert out.world.hexes[CENTRE].owner_id is None
    for c in RING1[:3]:
        out.world.hexes[c].owner_id = 1
        out.world.hexes[c].owner_since_tick = 0
    out.world.units[u.id].hex = RING1[3]
    out = step(out.world)
    assert out.world.hexes[RING1[3]].owner_id is None  # at cap (3): left unclaimed


def test_enemy_hex_flips_after_x_min_ticks():
    w = make_world()
    add_player(w, 1, RING1[0])
    add_player(w, 2, RING1[3])
    w.hexes[RING1[0]].owner_id, w.hexes[RING1[0]].owner_since_tick = 2, 0
    out = step(w)
    assert out.world.hexes[RING1[0]].owner_id == 2 and out.world.hexes[RING1[0]].flip_progress == 1
    out = step(out.world)
    assert out.world.hexes[RING1[0]].owner_id == 1  # held 1 tick -> x = x_min = 2


def test_attack_fights_logs_and_resolves():
    w = make_world(spawn=h3.grid_ring(CENTRE, 3)[0])
    a = add_player(w, 1, CENTRE)
    add_player(w, 2, RING1[0])
    a.path, a.progress = [RING1[0]], hex_minutes()  # one tick from contact
    out = step(w)
    assert len(out.battles) == 1
    b = out.battles[0]
    assert (b.attacker_unit, b.defender_unit, b.hex) == (1, 2, RING1[0])
    mods = {m.source: m.value for m in b.result.attacker.modifiers + b.result.defender.modifiers}
    assert mods == {"attack_bonus": 0.15, "defence_bonus": 0.05}
    att, dfn = out.world.units[1], out.world.units[2]
    if b.result.outcome is Outcome.ATTACKER_WON:
        assert att.hex == RING1[0] and not dfn.on_map
    elif b.result.outcome is Outcome.DEFENDER_WON:
        assert not att.on_map and dfn.hex == RING1[0]
    else:
        assert att.hex == CENTRE and att.path == []


def test_same_seed_same_battle():
    def battle():
        w = make_world(spawn=None)
        a = add_player(w, 1, CENTRE)
        add_player(w, 2, RING1[0])
        a.path, a.progress = [RING1[0]], hex_minutes()
        return step(w).battles[0]

    assert battle() == battle()


def test_loser_respawns_at_nearest_spawn_after_timeout():
    spawn = h3.grid_ring(CENTRE, 2)[0]
    w = make_world(spawn=spawn)
    u = add_player(w, 1, CENTRE, hp=0)
    u.respawn_deadline = 3
    out = step(w, n=2)
    assert not out.world.units[1].on_map
    out = step(out.world)
    u = out.world.units[1]
    assert u.on_map and u.hex == spawn and u.hp == DEFAULT.respawn_hp + DEFAULT.heal_anywhere


def test_respawn_order_picks_a_spawn_zone():
    w = make_world()
    u = add_player(w, 1, RING1[0], hp=0)
    u.respawn_deadline = 99
    bad = Order(id=1, user_id=1, kind="respawn", payload={"to": RING1[1]}, seq=1)
    assert step(w, [bad]).order_results[1] == "not a spawn zone"
    good = Order(id=2, user_id=1, kind="respawn", payload={"to": CENTRE}, seq=2)
    out = step(w, [good])
    assert out.order_results[2] == "ok" and out.world.units[1].hex == CENTRE


def test_defence_builds_while_still_and_march_drops_attack():
    w = make_world()
    add_player(w, 1, CENTRE)
    out = step(w, n=5)
    assert out.world.units[1].defence_bonus == pytest.approx(DEFAULT.defence_max)
    marching = Order(id=1, user_id=1, kind="mode", payload={"mode": "double_march"}, seq=1)
    out = step(out.world, [marching, move(1, RING1[0], seq=2)], n=6)  # 2 min a tick: arrives on the 6th
    u = out.world.units[1]
    assert u.hex == RING1[0] and u.attack_bonus == 0.0 and u.defence_bonus == DEFAULT.defence_on_arrival
    out = step(out.world, [Order(id=3, user_id=1, kind="mode", payload={"mode": "none"}, seq=3)])
    assert out.world.units[1].attack_bonus == pytest.approx(DEFAULT.bonus_step)


def test_healing_rates():
    w = make_world()
    add_player(w, 1, CENTRE, hp=50)  # spawn zone: anywhere rate
    add_player(w, 2, RING1[0], hp=50)
    w.hexes[RING1[0]].owner_id, w.hexes[RING1[0]].owner_since_tick = 2, 0
    w.units[2].mode = "heal"
    out = step(w)
    assert out.world.units[1].hp == 60
    assert out.world.units[2].hp == 90  # own land 20, heal mode x2


def test_income_per_tick_in_milli_coins():
    w = make_world()
    add_player(w, 1, CENTRE)
    for c, tier in zip(RING1[:2], (1, 5), strict=True):
        w.hexes[c].owner_id, w.hexes[c].owner_since_tick, w.hexes[c].tier = 1, 1, tier
    out = step(w)
    # (1 + 12) coins/hour over 60 ticks, held 0 ticks so no ramp yet.
    assert out.world.users[1].treasury == round(13 * 1000 / 60)
    assert out.ledger[-1].reason == "income"


def test_income_ramps_to_one_and_a_half_over_a_week():
    w = make_world()
    add_player(w, 1, CENTRE)
    week = 7 * 24 * 60
    w.tick = week
    w.hexes[RING1[0]].owner_id, w.hexes[RING1[0]].owner_since_tick = 1, 0
    assert step(w).world.users[1].treasury == round(1.5 * 1000 / 60)


def test_treasury_cap():
    w = make_world()
    add_player(w, 1, CENTRE)
    w.users[1].treasury = 990
    w.hexes[RING1[0]].owner_id, w.hexes[RING1[0]].owner_since_tick = 1, 1
    assert step(w, t=Tunables(treasury_cap=1000)).world.users[1].treasury == 1000


def test_march_fee_switches_mode_off_when_broke():
    w = make_world()
    add_player(w, 1, CENTRE)
    w.users[1].treasury = 5
    w.units[1].mode = "double_march"
    out = step(w, t=Tunables(l_march=5))
    assert out.world.units[1].mode == "double_march" and out.world.users[1].treasury == 0
    out = step(out.world, t=Tunables(l_march=5))
    assert out.world.units[1].mode == "none"


def test_fog_unlocks_when_chunks_ready_else_requests_bakes():
    w = make_world()
    fogged = RING1[0]
    w.hexes[fogged].playable = False
    w.hexes[fogged].chunks = ((10, 1009, 641),)
    a, b = sorted(n for n in h3.grid_ring(fogged, 1) if n != CENTRE and n in w.hexes)[:2]
    add_player(w, 1, CENTRE)
    add_player(w, 2, a)
    w.hexes[a].owner_id, w.hexes[b].owner_id = 2, 1
    out = step(w)
    assert not out.world.hexes[fogged].playable
    assert out.bake_requests[(10, 1009, 641)] == 2  # two players border it
    assert len(out.bake_requests) == 9  # the chunk and its lookahead ring
    out.world.ready_chunks.add((10, 1009, 641))
    out = step(out.world)
    assert out.world.hexes[fogged].playable and not out.bake_requests


def test_clanmates_share_hexes_and_never_fight():
    w = make_world(spawn=None)
    a = add_player(w, 1, CENTRE, clan=7)
    add_player(w, 2, RING1[0], clan=7)
    a.path, a.progress = [RING1[0]], hex_minutes()
    out = step(w)
    assert out.battles == [] and out.world.units[1].hex == RING1[0]


# -- classes ---------------------------------------------------------------------------------


def class_order(uid: int, name: str, seq: int = 50) -> Order:
    return Order(id=seq, user_id=uid, kind="class", payload={"class": name}, seq=seq)


def test_class_swap_needs_a_battle_then_waits_out_the_cooldown():
    w = make_world()
    u = add_player(w, 1, CENTRE)
    assert step(w, [class_order(1, "marine")]).order_results[50] == "needs a battle first"
    u.battles_since_class = 1
    out = step(w, [class_order(1, "marine")])
    assert out.order_results[50] == "ok" and out.world.units[1].unit_class == "marine"
    out.world.units[1].battles_since_class = 1
    out = step(out.world, [class_order(1, "tank", seq=51)])
    assert out.order_results[51] == "cooldown"  # free players: 7 days


def test_subscribers_swap_after_seven_hours():
    w = make_world()
    u = add_player(w, 1, CENTRE)
    w.users[1].subscriber = True
    u.unit_class, u.class_changed_tick, u.battles_since_class = "marine", 0, 1
    w.tick = 7 * 60 - 2  # the next tick is 1 short of 7 hours
    assert step(w, [class_order(1, "tank")]).order_results[50] == "cooldown"
    w.tick += 1
    assert step(w, [class_order(1, "tank")]).order_results[50] == "ok"
    w.users[1].subscriber = False
    assert step(w, [class_order(1, "tank")]).order_results[50] == "cooldown"


def test_unknown_or_same_class():
    w = make_world()
    add_player(w, 1, CENTRE).battles_since_class = 1
    out = step(w, [class_order(1, "wizard"), class_order(1, "barbarian", seq=51)])
    assert out.order_results == {50: "unknown class", 51: "already that class"}


def test_battles_count_toward_swapping():
    w = make_world(spawn=None)
    a = add_player(w, 1, CENTRE)
    add_player(w, 2, RING1[0])
    a.path, a.progress = [RING1[0]], hex_minutes()
    out = step(w)
    assert out.world.units[1].battles_since_class == 1 and out.world.units[2].battles_since_class == 1


def test_marine_is_four_times_faster_on_water_and_slower_on_land():
    w = make_world(spawn=None)
    for c in RING1:
        w.hexes[c].zone = "water"
    u = add_player(w, 1, CENTRE)
    u.unit_class = "marine"
    u.path = [RING1[0]]
    # Water costs 2x for everyone; a marine's 0.25 makes it 0.5x: 5.1 minutes, so the 6th tick.
    out = step(w, n=5)
    assert out.world.units[1].hex == CENTRE
    assert step(out.world).world.units[1].hex == RING1[0]
    w2 = make_world(spawn=None)
    m = add_player(w2, 1, CENTRE)
    m.unit_class, m.path = "marine", [RING1[0]]
    assert step(w2, n=12).world.units[1].hex == CENTRE  # land at 80 km/h: 12.8 minutes
    assert step(w2, n=13).world.units[1].hex == RING1[0]


def test_saboteur_ignores_defence_and_tank_adds_attack():
    w = make_world(spawn=None)
    a = add_player(w, 1, CENTRE)
    d = add_player(w, 2, RING1[0])
    a.unit_class, d.unit_class = "saboteur", "tank"
    a.path, a.progress = [RING1[0]], hex_minutes() * 4 / 3
    b = step(w).battles[0].result
    assert {m.source for m in b.attacker.modifiers} == {"attack_bonus"}
    assert b.defender.modifiers == []  # no defence bonus, and a tank's class bonus is attack only
    w = make_world(spawn=None)
    a = add_player(w, 1, CENTRE)
    d = add_player(w, 2, RING1[0])
    a.unit_class, d.unit_class = "tank", "saboteur"
    a.path, a.progress = [RING1[0]], hex_minutes() * 4 / 3
    b = step(w).battles[0].result
    assert {m.source: m.value for m in b.attacker.modifiers} == {"attack_bonus": 0.15, "class:tank": 0.10}
    assert {m.source: m.value for m in b.defender.modifiers} == {"defence_bonus": 0.05, "class:saboteur": -0.10}


def test_medic_heals_more_and_heals_allies_on_its_hex():
    w = make_world()
    medic = add_player(w, 1, CENTRE, hp=50, clan=3)
    add_player(w, 2, CENTRE, hp=50, clan=3)
    add_player(w, 3, CENTRE, hp=50)  # not allied
    medic.unit_class = "medic"
    out = step(w)
    assert out.world.units[1].hp == 65  # 10 x 1.5
    assert out.world.units[2].hp == 70  # 10 + the medic's 10
    assert out.world.units[3].hp == 60


# -- cities and garrisons -----------------------------------------------------------------------


def repair(uid: int, cell: str, hp: int | None = None, seq: int = 70) -> Order:
    payload = {"hex": cell} if hp is None else {"hex": cell, "hp": hp}
    return Order(id=seq, user_id=uid, kind="repair", payload=payload, seq=seq)


def test_claiming_open_land_comes_with_a_full_garrison():
    w = make_world()
    add_player(w, 1, CENTRE)
    out = step(w, [move(1, RING1[0])], n=TICKS_PER_HEX)
    assert out.world.hexes[RING1[0]].owner_id == 1
    assert out.world.hexes[RING1[0]].garrison_hp == DEFAULT.city_garrison_hp


def test_entering_an_empty_enemy_city_fights_its_garrison_first():
    w = make_world(spawn=None)
    a = add_player(w, 1, CENTRE)
    add_player(w, 2, h3.grid_ring(CENTRE, 3)[0])  # the owner is away
    city = w.hexes[RING1[0]]
    city.owner_id, city.owner_since_tick, city.garrison_hp = 2, 0, 50
    a.path, a.progress = [RING1[0]], hex_minutes()
    out = step(w)
    assert len(out.battles) == 1
    b = out.battles[0]
    assert b.defender_unit is None and b.defender_user == 2 and b.result.defender.unit_id == f"garrison:{RING1[0]}"
    assert b.result.defender.hp == 50 and b.result.defender.modifiers == []
    g = out.world.hexes[RING1[0]].garrison_hp
    if b.result.outcome is Outcome.ATTACKER_WON:
        assert g == 0 and out.world.units[1].hex == RING1[0]
    else:
        assert g == b.result.defender_hp_after and out.world.units[1].hex != RING1[0]


def test_capture_waits_for_the_garrison_and_leaves_it_in_ruins():
    w = make_world()
    add_player(w, 1, RING1[0])  # already standing in the enemy city
    add_player(w, 2, h3.grid_ring(CENTRE, 3)[0])
    city = w.hexes[RING1[0]]
    city.owner_id, city.owner_since_tick, city.garrison_hp = 2, 0, 10
    out = step(w, n=3)
    assert out.world.hexes[RING1[0]].owner_id == 2 and out.world.hexes[RING1[0]].flip_progress == 0
    out.world.hexes[RING1[0]].garrison_hp = 0
    out = step(out.world, n=2)
    assert out.world.hexes[RING1[0]].owner_id == 1
    assert out.world.hexes[RING1[0]].garrison_hp == 0  # captured: the new owner has to repair it


def test_repair_costs_per_hp_and_stops_at_what_you_can_afford():
    w = make_world()
    add_player(w, 1, CENTRE)
    city = w.hexes[RING1[0]]
    city.owner_id, city.owner_since_tick, city.garrison_hp = 1, 0, 20
    w.users[1].treasury = 10 * DEFAULT.fee_repair + 7
    out = step(w, [repair(1, RING1[0])])
    assert out.order_results[70] == "repaired 10 of 30 HP"
    assert out.world.hexes[RING1[0]].garrison_hp == 30
    assert [(e.amount, e.reason) for e in out.ledger if e.reason == "repair"] == [(-10 * DEFAULT.fee_repair, "repair")]
    out.world.users[1].treasury = 10**6
    out = step(out.world, [repair(1, RING1[0], hp=5, seq=71), repair(1, RING1[0], seq=72)])
    assert out.order_results == {71: "ok", 72: "ok"} and out.world.hexes[RING1[0]].garrison_hp == 50
    out = step(out.world, [repair(1, RING1[0], seq=73), repair(1, RING1[1], seq=74)])
    assert out.order_results == {73: "already full", 74: "not your city"}


def test_owners_rename_their_cities():
    w = make_world()
    add_player(w, 1, CENTRE)
    w.hexes[RING1[0]].owner_id, w.hexes[RING1[0]].owner_since_tick = 1, 0

    def rename(cell, name, seq):
        return Order(id=seq, user_id=1, kind="rename", payload={"hex": cell, "name": name}, seq=seq)

    out = step(
        w, [rename(RING1[0], "  Port   Nicholson ", 80), rename(RING1[1], "Mine", 81), rename(RING1[0], "x" * 25, 82)]
    )
    assert out.order_results == {80: "ok", 81: "not your city", 82: "name longer than 24 characters"}
    assert out.world.hexes[RING1[0]].name == "Port Nicholson"
    out = step(out.world, [rename(RING1[0], " ", 83)])
    assert out.world.hexes[RING1[0]].name is None


def test_a_garrison_that_kills_a_bot_loots_it_for_the_city_owner():
    for i in range(200):
        w = make_world(spawn=h3.grid_ring(CENTRE, 3)[0])
        bot = add_player(w, 1, CENTRE, hp=20)
        bot.veteran, bot.kills = True, 2
        w.users[1].treasury = 100_000
        add_player(w, 2, h3.grid_ring(CENTRE, 3)[1])
        city = w.hexes[RING1[0]]
        city.owner_id, city.owner_since_tick, city.garrison_hp = 2, 0, 50
        bot.path, bot.progress = [RING1[0]], hex_minutes()
        out = run_tick(w, [], i.to_bytes(4, "big"))
        if out.battles[0].result.outcome is Outcome.DEFENDER_WON:
            loot = DEFAULT.bounty_base + 2 * DEFAULT.bounty_per_kill
            assert [(e.user_id, e.amount) for e in out.ledger if e.reason == "loot"] == [(2, loot)]
            return
    raise AssertionError("the garrison never won")


# -- shields -----------------------------------------------------------------------------------

HOUR = 3600 // DEFAULT.tick_seconds
DAY = 24 * HOUR


def shield(uid: int, seq: int = 90, on: bool = True) -> Order:
    return Order(id=seq, user_id=uid, kind="shield", payload={"on": on}, seq=seq)


def shielded(u: Unit, hp: int = 500) -> Unit:
    u.shield_hp, u.shield_day = hp, 0
    return u


def test_everyone_toggles_with_fifteen_minutes_between_and_twelve_hours_a_day():
    w = make_world()
    add_player(w, 1, CENTRE)
    out = step(w, [shield(1)])  # tick 1
    assert out.order_results[90] == "ok" and out.world.units[1].shield_hp == 5 * DEFAULT.max_hp
    assert step(out.world, [shield(1, 91)]).order_results == {91: "already shielded"}
    out = step(out.world, n=59)
    out = step(out.world, [shield(1, 92, on=False)])  # tick 61: 60 ticks used
    assert out.world.units[1].shield_ready == 61 + 15
    assert step(out.world, [shield(1, 93)]).order_results == {93: "cooldown"}
    out = step(out.world, n=14)
    out = step(out.world, [shield(1, 94)])  # tick 76: up again
    assert out.order_results == {94: "ok"} and out.world.units[1].shield_used == 61
    out = step(out.world, n=12 * HOUR - 61 - 1)
    assert out.world.units[1].shield_hp > 0
    out = step(out.world)  # the day's 12 hours run out
    u = out.world.units[1]
    assert u.shield_hp == 0 and u.shield_used == 12 * HOUR
    assert step(out.world, [shield(1, 95)]).order_results == {95: "cooldown"}
    out = step(out.world, n=DAY - 1 - out.world.tick)  # the next order lands on the next game day
    assert step(out.world, [shield(1, 96)]).order_results == {96: "ok"}


def test_only_subscribers_schedule_their_shield_and_it_runs_on_the_same_budget():
    w = make_world()
    add_player(w, 1, CENTRE)
    sched = Order(id=90, user_id=1, kind="shield_schedule", payload={"from": 10, "to": 70}, seq=90)
    assert step(w, [sched]).order_results == {90: "patron perk"}
    w.users[1].subscriber = True
    out = step(w, [sched], n=10)  # tick 10: the schedule raises it
    assert out.world.units[1].shield_hp == 500
    out = step(out.world, n=60)  # tick 70: and drops it
    u = out.world.units[1]
    assert u.shield_hp == 0 and u.shield_used == 60 and u.shield_ready == 70 + 15


def test_shield_soaks_damage_before_hp():
    w = make_world(spawn=h3.grid_ring(CENTRE, 3)[0])
    a = add_player(w, 1, CENTRE)
    shielded(add_player(w, 2, RING1[0]))
    a.path, a.progress = [RING1[0]], hex_minutes()
    out = step(w)
    b = out.battles[0]
    assert b.result.defender.hp == 600 and b.shield_absorbed == 600 - b.result.defender_hp_after
    dfn = out.world.units[2]
    assert dfn.hp == 100 and dfn.shield_hp == 500 - b.shield_absorbed > 0


def test_a_shielded_unit_cannot_attack():
    w = make_world(spawn=h3.grid_ring(CENTRE, 3)[0])
    a = shielded(add_player(w, 1, CENTRE))
    add_player(w, 2, RING1[0])
    pursue = Order(id=2, user_id=1, kind="pursue", payload={"user": 2}, seq=2)
    assert step(w, [move(1, RING1[0]), pursue]).order_results == {
        1: "can't attack while shielded",
        2: "can't attack while shielded",
    }
    a.path, a.progress = [RING1[0]], hex_minutes()  # walked into contact anyway: it stops short
    out = step(w)
    assert not out.battles and out.world.units[1].hex == CENTRE and out.world.units[1].path == []


def test_a_breached_shield_breaks_for_twelve_hours():
    w = make_world(spawn=h3.grid_ring(CENTRE, 3)[0])
    a = add_player(w, 1, CENTRE)
    shielded(add_player(w, 2, RING1[0], hp=60), hp=5)
    a.path, a.progress = [RING1[0]], hex_minutes()
    for i in range(50):  # find a seed where the attacker lands more than the shield's 5
        out = run_tick(w, [], SECRET + bytes([i]))
        b = out.battles[0]
        if b.result.defender_hp_after < 60:
            break
    dfn = out.world.units[2]
    assert b.shield_absorbed == 5 and dfn.shield_hp == 0 and dfn.shield_ready == 1 + 12 * HOUR
    assert dfn.hp == b.result.defender_hp_after


# -- revenge -----------------------------------------------------------------------------------


def test_pursuit_follows_a_moving_target_until_it_catches_it():
    w = make_world(spawn=None, radius=4)
    add_player(w, 1, CENTRE)
    far = sorted(h3.grid_ring(CENTRE, 3))[0]
    add_player(w, 2, far)
    out = step(w, [Order(id=5, user_id=1, kind="pursue", payload={"user": 2}, seq=5)])
    assert out.order_results == {5: "ok"} and out.world.units[1].path[-1] == far
    w = out.world
    w.units[2].hex = sorted(h3.grid_ring(CENTRE, 2))[0]  # the target moves: the next tick re-paths
    out = step(w)
    assert out.world.units[1].path[-1] == w.units[2].hex and out.world.units[1].pursue == 2
    out = step(out.world, n=8 * TICKS_PER_HEX)
    assert out.world.units[1].pursue is None  # caught: one of them fell
    assert any(not u.on_map for u in out.world.units.values())


def test_a_move_order_ends_a_pursuit():
    w = make_world(spawn=None)
    add_player(w, 1, CENTRE)
    add_player(w, 2, sorted(h3.grid_ring(CENTRE, 3))[0])
    out = step(w, [Order(id=5, user_id=1, kind="pursue", payload={"user": 2}, seq=5)])
    out = step(out.world, [move(1, RING1[0], seq=6)])
    assert out.world.units[1].pursue is None and out.world.units[1].path == [RING1[0]]
    assert step(w, [Order(id=7, user_id=1, kind="pursue", payload={"user": 1}, seq=7)]).order_results == {
        7: "no such enemy"
    }


# -- nemesis -----------------------------------------------------------------------------------


def nemesis(uid: int, seq: int = 100, **target) -> Order:
    return Order(id=seq, user_id=uid, kind="nemesis", payload=target, seq=seq)


def test_only_subscribers_pick_a_nemesis_and_only_subscribers_or_bots_can_be_one():
    w = make_world(spawn=None)
    add_player(w, 1, CENTRE)
    add_player(w, 2, RING1[0])  # a free player
    add_player(w, 3, RING1[1])
    w.users[3].bot = True
    assert step(w, [nemesis(1, user=3)]).order_results == {100: "patron perk"}
    w.users[1].subscriber = True
    out = step(w, [nemesis(1, 100, user=2), nemesis(1, 101, user=1), nemesis(1, 102, user=3)])
    assert out.order_results == {100: "not a valid nemesis", 101: "not a valid nemesis", 102: "ok"}
    assert out.world.users[1].nemesis_user == 3
    assert step(out.world, [nemesis(1, 103, user=3)]).order_results == {103: "cooldown"}  # once a game day
    out = step(out.world, [nemesis(1, 104)])  # clearing is always allowed
    assert out.order_results == {104: "ok"} and out.world.users[1].nemesis_user is None


def test_the_hunter_gets_the_nemesis_bonus_whichever_side_attacks():
    w = make_world(spawn=None)
    a, b = add_player(w, 1, CENTRE), add_player(w, 2, RING1[0])
    w.users[1].subscriber, w.users[2].bot, w.users[1].nemesis_user = True, True, 2
    att, dfn = battle_modifiers(w, a, b, b.hex)
    assert ("nemesis", 0.05) in [(m.source, m.value) for m in att] and "nemesis" not in [m.source for m in dfn]
    att, dfn = battle_modifiers(w, b, a, a.hex)
    assert "nemesis" not in [m.source for m in att] and ("nemesis", 0.05) in [(m.source, m.value) for m in dfn]
    w.users[2].bot = False  # a lapsed target: free players never face the bonus
    assert "nemesis" not in [m.source for m in battle_modifiers(w, a, b, b.hex)[0]]


def test_a_clan_nemesis_covers_its_subscribed_and_bot_members_only():
    w = make_world(spawn=None)
    add_player(w, 1, CENTRE)
    add_player(w, 2, RING1[0], clan=7)
    add_player(w, 3, RING1[1], clan=7)
    w.users[1].subscriber, w.users[2].subscriber = True, True
    out = step(w, [nemesis(1, clan=7)])
    assert out.order_results == {100: "ok"}
    assert nemesis_of(out.world, 1, 2) and not nemesis_of(out.world, 1, 3)


def test_hunt_mode_chases_the_nemesis_for_an_hour_a_day():
    w = make_world(spawn=None, radius=4)
    add_player(w, 1, CENTRE)
    add_player(w, 2, sorted(h3.grid_ring(CENTRE, 3))[0])
    w.users[1].subscriber, w.users[2].bot, w.users[1].nemesis_user = True, True, 2
    hunt = Order(id=110, user_id=1, kind="hunt", payload={"on": True}, seq=110)
    out = step(w, [hunt])
    u = out.world.units[1]
    assert out.order_results == {110: "ok"} and u.hunting and u.pursue == 2 and u.path[-1] == w.units[2].hex
    w = out.world
    w.units[2].respawn_deadline = 10**6  # the nemesis leaves the map: hunt its land instead
    w.units[2].hp = 0
    w.hexes[RING1[0]].owner_id = 2
    out = step(w)
    u = out.world.units[1]
    assert u.pursue is None and u.path and u.path[-1] == RING1[0]
    out = step(out.world, n=58)
    assert out.world.units[1].hunting
    out = step(out.world)  # the hour is up
    assert not out.world.units[1].hunting and out.world.units[1].hunt_used == 60


def test_pursuing_a_unit_on_your_own_hex_waits_for_it_to_move():
    w = make_world(spawn=None)
    add_player(w, 1, CENTRE)
    add_player(w, 2, CENTRE)
    out = step(w, [Order(id=5, user_id=1, kind="pursue", payload={"user": 2}, seq=5)], n=3)
    u = out.world.units[1]
    assert u.pursue == 2 and u.path == [] and u.hex == CENTRE


def test_no_repairs_while_an_enemy_stands_in_the_city_so_the_capture_goes_through():
    w = make_world(spawn=None)
    add_player(w, 1, RING1[0])
    add_player(w, 2, sorted(h3.grid_ring(CENTRE, 3))[0])
    city = w.hexes[RING1[0]]
    city.owner_id, city.owner_since_tick, city.garrison_hp = 2, 0, 0  # garrison beaten, attacker inside
    w.users[2].treasury = 10**6
    out = step(w, [repair(2, RING1[0])])
    assert out.order_results == {70: "under siege"} and out.world.hexes[RING1[0]].garrison_hp == 0
    out = step(out.world, n=DEFAULT.x_min)
    assert out.world.hexes[RING1[0]].owner_id == 1


def test_a_pursuit_whose_target_falls_stops_instead_of_walking_on():
    w = make_world(spawn=None, radius=4)
    add_player(w, 1, CENTRE)
    add_player(w, 2, sorted(h3.grid_ring(CENTRE, 3))[0])
    out = step(w, [Order(id=5, user_id=1, kind="pursue", payload={"user": 2}, seq=5)])
    w = out.world
    w.units[2].respawn_deadline, w.units[2].hp = 10**6, 0  # the target falls (elsewhere)
    u = step(w).world.units[1]
    assert u.pursue is None and u.path == []


# -- bot services ------------------------------------------------------------------------------


def hire(uid: int, bot: int, kind: str, seq: int = 120, **extra) -> Order:
    return Order(id=seq, user_id=uid, kind="hire", payload={"bot": bot, "kind": kind, **extra}, seq=seq)


def services_world():
    w = make_world(spawn=None, radius=4)
    add_player(w, 1, CENTRE)
    add_player(w, 2, sorted(h3.grid_ring(CENTRE, 3))[0])  # the bot
    add_player(w, 3, sorted(h3.grid_ring(CENTRE, 3))[5])
    w.users[2].bot = True
    w.units[2].veteran = True
    for uid in (1, 3):
        w.users[uid].treasury = 10**6
    return w


def test_a_guard_job_starts_now_and_the_next_one_queues_behind_it():
    w = services_world()
    out = step(w, [hire(1, 2, "guard"), hire(3, 2, "merc", 121, target=1)])
    assert out.order_results == {120: "ok", 121: "ok"}
    guard, merc = out.world.contracts[1], out.world.contracts[2]
    assert (guard.status, merc.status) == ("active", "queued")
    assert guard.price == 15_000  # list price: nobody earns anything yet
    assert [(e.user_id, e.amount) for e in out.ledger if e.reason.startswith("hire")] == [
        (1, -15_000),
        (3, -merc.price),
    ]
    assert out.world.allied(1, 2) and not out.world.allied(3, 2)
    out = step(out.world, n=DEFAULT.guard_minutes)
    assert (out.world.contracts[1].status, out.world.contracts[2].status) == ("done", "active")


def test_mercenaries_need_a_target_and_respect_the_bots_protection():
    w = services_world()
    out = step(w, [hire(1, 2, "merc"), hire(3, 2, "protect", 121)])
    assert out.order_results == {120: "pick a target", 121: "ok"}
    out = step(out.world, [hire(1, 2, "merc", 122, target=3)])
    assert out.order_results == {122: "target is under this bot's protection"}
    w.users[1].subscriber, w.users[1].nemesis_user, w.users[3].subscriber = True, 3, True
    out = step(w, [hire(1, 2, "merc")])  # aimed at the nemesis by default
    assert out.order_results == {120: "ok"} and out.world.contracts[1].target_user == 3


def test_the_target_can_buy_a_mercenary_off_with_double_its_price():
    w = services_world()
    out = step(w, [hire(1, 2, "merc", target=3)])
    price = out.world.contracts[1].price
    out = step(out.world, n=4)
    paid = out.world.users[1].treasury
    out = step(out.world, [hire(3, 2, "merc", 121, offer=2 * price // 1000 - 1)])  # not quite double: queued
    assert out.order_results == {121: "ok"} and out.world.contracts[2].status == "queued"
    out = step(out.world, [hire(3, 2, "merc", 122, offer=2 * price // 1000)])
    assert out.order_results == {122: "ok: contract broken"}
    old, new = out.world.contracts[1], out.world.contracts[3]
    assert old.status == "broken" and new.status == "active" and new.target_user == 1  # it turns on its old hirer
    refund = out.world.users[1].treasury - paid
    assert 0 < refund < price  # paid back for the minutes it didn't serve


def test_a_bot_on_the_job_splits_its_loot_with_the_hirer():
    w = services_world()
    w.units[3].hex = RING1[0]
    w.users[3].bot = True
    w.units[3].veteran, w.units[3].kills = True, 2
    w.users[3].treasury = 15_000
    w.units[2].hex = CENTRE
    out = step(w, [hire(1, 2, "guard")])
    w = out.world
    w.units[2].path, w.units[2].progress = [RING1[0]], hex_minutes()
    w.units[2].hp, w.units[3].hp = 100, 1
    out = step(w)
    assert out.battles and out.battles[0].result.outcome is Outcome.ATTACKER_WON
    shares = {e.reason: e.amount for e in out.ledger}
    assert shares["loot"] == shares["loot_share"] == 7_500


def test_buying_a_bots_city_pays_its_purse_and_moves_the_city():
    w = services_world()
    city = w.hexes[RING1[2]]
    city.owner_id, city.owner_since_tick, city.garrison_hp = 2, 0, 30
    out = step(w, [Order(id=130, user_id=1, kind="buy_city", payload={"hex": RING1[2]}, seq=130)])
    assert out.order_results == {130: "ok"}
    h = out.world.hexes[RING1[2]]
    assert h.owner_id == 1 and h.garrison_hp == 30
    (buyer, paid), (seller, got) = [(e.user_id, e.amount) for e in out.ledger if "city" in e.reason]
    assert (buyer, seller) == (1, 2) and -paid == got >= 5_000  # at least the minimum, to the bot's purse
    assert step(
        out.world, [Order(id=131, user_id=1, kind="buy_city", payload={"hex": RING1[2]}, seq=131)]
    ).order_results == {131: "not a bot's city"}


def test_the_leader_pays_more():
    from atlas_realms.tick.engine import quote

    w = services_world()
    for c in sorted(h3.grid_ring(CENTRE, 2))[:4]:
        w.hexes[c].owner_id, w.hexes[c].owner_since_tick = 1, 0
    w.hexes[sorted(h3.grid_ring(CENTRE, 2))[5]].owner_id = 3
    assert quote(w, 1, 2, "guard") > quote(w, 3, 2, "guard")
    assert quote(w, 1, 2, "guard") <= 3 * 15_000


def test_bots_move_at_half_speed_unless_on_a_job():
    import math

    from atlas_realms.tick.world import Contract

    def ticks_to_cross(hired: bool) -> int:
        w = make_world(spawn=None)
        add_player(w, 1, CENTRE)
        add_player(w, 2, sorted(h3.grid_ring(CENTRE, 3))[0])
        w.users[1].bot = True
        if hired:
            w.contracts[1] = Contract(id=1, bot=1, hirer=2, kind="guard", price=0, start=0, minutes=999,
                                      status="active", began=0)  # fmt: skip
        out = step(w, [move(1, RING1[0])])
        n = 1
        while out.world.units[1].hex == CENTRE:
            out, n = step(out.world), n + 1
        return n

    assert ticks_to_cross(hired=True) == TICKS_PER_HEX
    assert ticks_to_cross(hired=False) == math.ceil(hex_minutes() / DEFAULT.bot_speed)


# -- malformed orders never abort the tick ------------------------------------------------------


def test_a_shielded_move_to_an_unknown_hex_is_unreachable():
    w = make_world()
    u = add_player(w, 1, CENTRE)
    u.shield_hp = 500
    out = step(w, [Order(id=1, user_id=1, kind="move", payload={"to": "garbage"}, seq=1), move(1, "None", seq=2)])
    assert out.order_results == {1: "unreachable", 2: "unreachable"}


def test_a_non_numeric_repair_hp_is_refused():
    w = make_world()
    add_player(w, 1, CENTRE)
    city = w.hexes[RING1[0]]
    city.owner_id, city.owner_since_tick, city.garrison_hp = 1, 0, 20
    w.users[1].treasury = 10**6
    out = step(w, [repair(1, RING1[0], hp="abc", seq=70), repair(1, RING1[0], hp=[1], seq=71)])
    assert out.order_results == {70: "bad hp", 71: "bad hp"}
    assert out.world.hexes[RING1[0]].garrison_hp == 20


def test_a_non_numeric_hire_offer_is_refused():
    w = services_world()
    out = step(w, [hire(1, 2, "guard", offer="abc"), hire(3, 2, "guard", 121, offer={"x": 1})])
    assert out.order_results == {120: "bad offer", 121: "bad offer"}
    assert out.world.contracts == {}


def test_unhashable_class_and_clan_payloads_are_refused():
    w = make_world(spawn=None)
    add_player(w, 1, CENTRE, clan=1)
    add_player(w, 2, RING1[0], clan=2)
    w.users[1].subscriber = True
    out = step(
        w,
        [
            Order(id=1, user_id=1, kind="class", payload={"class": ["scout"]}, seq=1),
            Order(id=2, user_id=1, kind="nemesis", payload={"clan": [2]}, seq=2),
            Order(id=3, user_id=1, kind="nemesis", payload={"clan": {"a": 1}}, seq=3),
        ],
    )
    assert out.order_results == {1: "unknown class", 2: "not a valid nemesis", 3: "not a valid nemesis"}


def test_a_hex_held_since_tick_zero_counts_its_whole_holding():
    from atlas_realms.tick.engine import flip_needed

    h = Hex(h3=CENTRE, zone="land", tier=1, owner_id=1, owner_since_tick=0)
    assert flip_needed(h, 60 * 30) == 30  # held 30 hours -> 30 flip ticks, not x_min


def test_double_march_on_own_land_halves_the_cost_once():
    import math

    w = make_world(spawn=None)
    u = add_player(w, 1, CENTRE)
    line = [CENTRE]
    while len(line) < 4:
        line.append(sorted(n for n in h3.grid_ring(line[-1], 1) if h3.grid_distance(CENTRE, n) == len(line))[0])
    for cell in line:
        w.hexes[cell].owner_id, w.hexes[cell].owner_since_tick = 1, 0
    u.path, u.mode = line[1:], "double_march"
    per_tick = 2 * DEFAULT.tick_seconds / 60  # marching earns twice the walking progress...
    need = 3 * hex_minutes() * DEFAULT.cost_own_land  # ...against unmarched own-land costs
    out = step(w, n=math.ceil(need / per_tick) - 1)
    assert out.world.units[1].path  # one tick short
    assert step(out.world).world.units[1].hex == line[-1]


def test_a_contract_runs_its_minutes_whatever_the_tick_length():
    t = DEFAULT.model_copy(update={"tick_seconds": 30})  # two ticks a minute
    out = step(services_world(), [hire(1, 2, "guard")], t=t)
    out = step(out.world, t=t, n=DEFAULT.guard_minutes)  # half the job's minutes
    assert out.world.contracts[1].status == "active"
    out = step(out.world, t=t, n=DEFAULT.guard_minutes)
    assert out.world.contracts[1].status == "done"
