import json
from pathlib import Path

import h3
import pytest

from atlas_realms.bots import PERSONALITIES, Bot, decide, odds, top_earners
from atlas_realms.rules.combat import Outcome
from atlas_realms.rules.movement import hex_minutes
from atlas_realms.rules.tunables import DEFAULT
from atlas_realms.solo import Session, build_world
from atlas_realms.tick.engine import battle_modifiers, run_tick
from atlas_realms.tick.world import Hex, Unit, User, World

CENTRE = h3.latlng_to_cell(-41.2865, 174.7762, 5)
RING1 = sorted(h3.grid_ring(CENTRE, 1))
ROOT = Path(__file__).parent.parent


def world_with(*units: Unit, spawn: str | None = None) -> World:
    hexes = {c: Hex(h3=c, zone="land", tier=1) for c in h3.grid_disk(CENTRE, 4)}
    if spawn:
        hexes[spawn].zone = "spawn"
    w = World(tick=0, hexes=hexes, units={u.id: u for u in units}, users={})
    for u in units:
        w.users[u.user_id] = User(id=u.user_id)
    return w


def unit(uid: int, at: str, hp: int = 100, **kw) -> Unit:
    return Unit(id=uid, user_id=uid, hex=at, hp=hp, **kw)


# -- veterancy and bounties (engine) ------------------------------------------------------------


def test_veteran_bonus_grows_per_kill_and_caps():
    vet = unit(1, CENTRE, veteran=True, kills=2)
    w = world_with(vet, unit(2, RING1[0]))
    a, _ = battle_modifiers(w, vet, w.units[2], RING1[0])
    assert {m.source: m.value for m in a}["veteran"] == pytest.approx(0.10)
    vet.kills = 50
    a, _ = battle_modifiers(w, vet, w.units[2], RING1[0])
    assert {m.source: m.value for m in a}["veteran"] == DEFAULT.veteran_max
    human = unit(3, CENTRE, kills=5)  # humans never gain the bonus
    assert "veteran" not in {m.source for m in battle_modifiers(w, human, w.units[2], RING1[0])[0]}


def _fight_until(outcome: Outcome, attacker: Unit, defender: Unit, purse: int = 0):
    """Re-run one attack with different secrets until it ends in `outcome`. `purse` is the defender's treasury."""
    for i in range(200):
        a = Unit(**{f: getattr(attacker, f) for f in attacker.__slots__})
        d = Unit(**{f: getattr(defender, f) for f in defender.__slots__})
        a.path, a.progress = [d.hex], hex_minutes()
        w = world_with(a, d, spawn=h3.grid_ring(CENTRE, 3)[0])
        w.users[d.user_id].treasury = purse
        out = run_tick(w, [], i.to_bytes(4, "big"))
        if out.battles and out.battles[0].result.outcome is outcome:
            return out
    raise AssertionError("outcome never came up")


def test_killing_a_veteran_loots_its_purse_capped_by_kills():
    human, bot = unit(1, CENTRE), unit(2, RING1[0], hp=20, veteran=True, kills=3)
    out = _fight_until(Outcome.ATTACKER_WON, human, bot, purse=100_000)
    loot = DEFAULT.bounty_base + 3 * DEFAULT.bounty_per_kill
    assert [(e.user_id, e.amount, e.reason) for e in out.ledger if e.reason in ("loot", "looted")] == [
        (2, -loot, "looted"),
        (1, loot, "loot"),
    ]
    assert out.world.users[2].treasury == 100_000 - loot  # paid out of the bot's purse, not minted
    assert out.world.units[2].kills == 0 and out.world.units[1].kills == 1


def test_loot_is_capped_and_never_more_than_the_purse():
    human = unit(1, CENTRE)
    rich = _fight_until(Outcome.ATTACKER_WON, human, unit(2, RING1[0], hp=20, veteran=True, kills=40), purse=10**6)
    assert [e.amount for e in rich.ledger if e.reason == "loot"] == [DEFAULT.bounty_cap]
    poor = _fight_until(Outcome.ATTACKER_WON, human, unit(2, RING1[0], hp=20, veteran=True, kills=40), purse=1_234)
    assert [e.amount for e in poor.ledger if e.reason == "loot"] == [1_234]
    broke = _fight_until(Outcome.ATTACKER_WON, human, unit(2, RING1[0], hp=20, veteran=True, kills=4), purse=0)
    assert not [e for e in broke.ledger if e.reason == "loot"]


def test_bots_loot_each_other():
    a, b = unit(1, CENTRE, veteran=True), unit(2, RING1[0], hp=20, veteran=True, kills=2)
    out = _fight_until(Outcome.ATTACKER_WON, a, b, purse=50_000)
    assert out.world.units[1].kills == 1
    assert [(e.user_id, e.reason) for e in out.ledger if e.reason in ("loot", "looted")] == [(2, "looted"), (1, "loot")]


def test_a_bot_that_wins_counts_the_kill():
    bot, human = unit(1, CENTRE, veteran=True), unit(2, RING1[0], hp=20)
    out = _fight_until(Outcome.ATTACKER_WON, bot, human)
    assert out.world.units[1].kills == 1
    assert not [e for e in out.ledger if e.reason == "loot"]  # humans carry no loot


# -- strength awareness ---------------------------------------------------------------------------


def test_odds_follow_hp_and_modifiers():
    w = world_with(unit(1, CENTRE), unit(2, RING1[0]))
    a, d = w.units[1], w.units[2]
    even = odds(w, a, d)
    d.hp = 30
    assert odds(w, a, d) > even
    d.hp, d.defence_bonus = 100, 0.20
    assert odds(w, a, d) < even
    before = odds(w, a, d)
    a.veteran, a.kills = True, 6
    assert odds(w, a, d) > before


def test_a_wounded_bot_heals_before_a_hard_fight():
    raider = PERSONALITIES["raider"]
    me = unit(2, CENTRE, hp=60, veteran=True)
    foe = unit(1, RING1[0], defence_bonus=0.20)
    w = world_with(me, foe, spawn=CENTRE)
    orders = decide(w, Bot(user_id=2, personality=raider, home=CENTRE), 1, "s")
    assert ("mode", {"mode": "heal"}) in [(o.kind, o.payload) for o in orders]
    assert not [o for o in orders if o.kind == "move"]


def test_a_healthy_bot_farms_a_softer_target_first():
    jackal = PERSONALITIES["raider"]  # risk 0.45
    me = unit(2, CENTRE, veteran=True)
    tough = unit(1, RING1[0], defence_bonus=0.20, unit_class="tank")
    soft = unit(3, RING1[3], hp=25)
    w = world_with(me, tough, soft, spawn=CENTRE)
    w.users[1], w.users[3] = User(id=1), User(id=3)
    moves = []
    assert odds(w, me, tough) < jackal.risk <= odds(w, me, soft)
    for seed in range(20):  # aggression is a roll (else it expands); when it attacks, it is never the tough one
        moves += [o.payload["to"] for o in decide(w, Bot(2, jackal, CENTRE), 1, str(seed)) if o.kind == "move"]
    assert RING1[3] in moves and RING1[0] not in moves


# -- the reaver -------------------------------------------------------------------------------------


def test_top_earners_is_the_top_tenth_and_at_least_one():
    w = world_with(*(unit(i, CENTRE) for i in range(1, 12)))
    cells = sorted(w.hexes)
    for i, c in enumerate(cells[:11]):
        w.hexes[c].owner_id = i + 1
        w.hexes[c].tier = 5 if i + 1 in (4, 7) else 1
    assert top_earners(w) == [4, 7]  # ceil(10% of 11) = 2
    for c in cells[:11]:
        w.hexes[c].owner_id = None
    w.hexes[cells[0]].owner_id = 5
    assert top_earners(w) == [5]


def test_reaver_raids_a_top_earners_hex():
    reaver = PERSONALITIES["reaver"]
    me = unit(2, CENTRE, veteran=True)
    lord, poor = unit(1, RING1[0]), unit(3, RING1[1])
    w = world_with(me, lord, poor, spawn=CENTRE)
    lord_cities = set(sorted(w.hexes)[10:14])
    for c in lord_cities:
        w.hexes[c].owner_id, w.hexes[c].tier = 1, 5
    w.hexes[sorted(w.hexes)[20]].owner_id = 3
    orders = decide(w, Bot(user_id=2, personality=reaver, home=CENTRE), 1, "s")
    moves = [o.payload["to"] for o in orders if o.kind == "move"]
    assert len(moves) == 1 and moves[0] in lord_cities
    assert ("mode", {"mode": "double_march"}) in [(o.kind, o.payload) for o in orders]


# -- the arena --------------------------------------------------------------------------------------


def test_solo_session_runs_and_bots_play_differently():
    manifest = {"ready_chunks": [[10, x, y] for x in range(1006, 1013) for y in range(638, 645)]}
    w = build_world(manifest, ROOT / "data" / "nz_mainland.geojson", [(-41.2865, 174.7762), (-41.2906, 174.0010)])
    s = Session.new(w, "test", secret=b"bots" * 8)  # seeded: the bots' choices replay exactly
    for _ in range(300):
        s.step()
    st = s.state()
    assert st["tick"] == 300
    bots = [p for p in st["players"] if p["bot"]]
    assert len(bots) == 8 and sum(p["hexes"] for p in bots) > 5
    assert len({p["hexes"] for p in bots}) > 1  # each personality ends up holding a different amount of land
    bot_units = [u for u in st["units"] if u["user_id"] != st["you"]]
    assert len(bot_units) == len(bots) and all(u["veteran"] for u in bot_units)
    assert len({u["class"] for u in bot_units}) > 1  # personalities field different classes
    assert json.dumps(st)  # the state is plain JSON for the client


def test_the_open_strait_is_sailable_and_never_claimed():
    """With the all-sea chunks open (solo.build_world), water is for crossing, as the rules already say: you can route
    into the middle of the strait, and in a long game nobody (bot or not) ever owns a water hex."""
    manifest = json.loads((ROOT / "arena" / "world" / "cook-strait.json").read_text())
    w = build_world(manifest, ROOT / "data" / "nz_mainland.geojson", [(-41.2865, 174.7762), (-41.2906, 174.0010)])
    s = Session.new(w, "test", unit_class="marine", secret=b"strait" * 6)
    mid_strait = h3.latlng_to_cell(-41.6, 174.6, 5)  # in chunk 10/1008/642, never baked
    oid = s.submit("move", {"to": mid_strait})
    s.step()
    assert s.results[oid] == "ok" and s.world.units[1].path
    for _ in range(600):
        s.step()
    assert not any(h.owner_id for h in s.world.hexes.values() if h.zone == "water")


def test_a_hired_mercenary_leaves_its_range_to_hunt_its_mark_and_a_guard_goes_home_to_patrol():
    from atlas_realms.tick.world import Contract

    far = sorted(h3.grid_ring(CENTRE, 4))[0]
    w = world_with(unit(1, CENTRE), unit(2, far, hp=30), unit(3, RING1[0]), spawn=CENTRE)  # a wounded mark: good odds
    w.users[3].bot = True
    w.hexes[RING1[3]].owner_id = 1
    bot = Bot(user_id=3, personality=PERSONALITIES["warden"], home=CENTRE)  # a homebody: roam 2
    w.contracts[1] = Contract(id=1, bot=3, hirer=1, kind="merc", price=1, start=0, minutes=15, target_user=2,
                              status="active", began=0)  # fmt: skip
    moves = [o for o in decide(w, bot, human_id=1, seed="s") if o.kind == "move"]
    assert moves and moves[-1].payload["to"] == far
    w.contracts[1].kind, w.contracts[1].target_user = "guard", None
    moves = [o for o in decide(w, bot, human_id=1, seed="s") if o.kind == "move"]
    assert moves and moves[-1].payload["to"] == RING1[3]  # the hirer's land


def test_the_arena_runs_half_again_as_fast_as_its_speed_setting():
    from atlas_realms.solo import PACE

    s = Session.new(world_with(spawn=CENTRE), "test", roster=("settler",), tick_every=2.0)
    s._last = 0.0
    assert PACE == 1.5 and s.catch_up(now=12.0) == 9  # 12 s at "2 s a tick" is 9 ticks, not 6


def test_a_paused_game_queues_a_bounded_number_of_orders():
    from atlas_realms.solo import MAX_PENDING

    s = Session.new(world_with(spawn=CENTRE), "test", roster=("settler",))
    s.paused = True
    for _ in range(MAX_PENDING):
        s.submit("cancel", {})
    with pytest.raises(ValueError, match="too many orders"):
        s.submit("cancel", {})
    s.step()
    s.submit("cancel", {})  # the queue drains with the tick


def test_order_results_are_kept_to_a_window():
    from atlas_realms.solo import MAX_PENDING, RESULTS

    s = Session.new(world_with(spawn=CENTRE), "test", roster=("settler",))
    for _ in range(3):
        for _ in range(MAX_PENDING):
            s.submit("cancel", {})
        s.step()
    assert len(s.results) == RESULTS
    assert min(s.results) > 2 * MAX_PENDING  # the newest are kept
    assert len(s.state()["results"]) == 10
