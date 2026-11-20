from atlas_realms.news import digest

NAMES = {1: "You", 2: "Raider", 3: "Jackal"}


def battle(tick, att, dfn, outcome, **kw):
    e = {
        "tick": tick, "kind": "battle", "hex": "h", "attacker": att, "defender": dfn, "garrison": False,
        "outcome": outcome, "rolls": 6, "shield": 0, "attacker_hp": 100, "defender_hp": 100,
        "attacker_left": 40, "defender_left": 0,
    }  # fmt: skip
    return e | kw


def test_a_quiet_window_says_so():
    r = digest([], NAMES, 1, 0, 100)
    assert r["stories"] == [] and r["stats"]["battles"] == 0 and "quiet" in (r["headline"] + r["lead"]).lower()


def test_your_own_fights_and_losses_lead_the_report():
    events = [
        battle(10, 2, 3, "attacker_won"),
        battle(20, 2, 1, "defender_won", attacker_hp=100, defender_hp=60),  # you, the underdog, won
        {"tick": 30, "kind": "capture", "hex": "c", "from": 1, "to": 3},
        {"tick": 31, "kind": "loot", "from": 2, "to": 1, "amount": 10.0},
        battle(500, 3, 2, "attacker_won"),  # outside the window
    ]
    r = digest(events, NAMES, 1, 0, 100, {"c": ("Port Nicholson", 2)})
    heads = [s["headline"] for s in r["stories"]]
    assert heads[0] in ("Upset: You turn the tables on Raider", "Jackal seizes Port Nicholson")
    assert "Jackal seizes Port Nicholson" in heads and "You collect on Raider" in heads
    assert r["stats"]["battles"] == 2 and r["stats"]["your_losses"] == 1
    assert any("Port Nicholson" in s["text"] for s in r["stories"])
    assert digest(events, NAMES, 1, 0, 100) == digest(events, NAMES, 1, 0, 100)  # same window, same words


def test_a_busy_night_rounds_up_the_repeats():
    sieges = [battle(10 + i, 2 + i % 2, 9, "attacker_won", garrison=True, hex=f"h{i}") for i in range(6)]
    names = NAMES | {9: "Warden"}
    r = digest(sieges, names, 1, 0, 100, {f"h{i}": (f"Town {i}", 1) for i in range(6)})
    assert len(r["stories"]) == 2  # the biggest one, then one round-up of the other five
    assert r["stories"][1]["headline"] in ("Walls fall across the Strait", "Siege season")
    assert r["stories"][1]["text"].count(";") == 4  # four briefs, then "and 1 more"
    r = digest(sieges, names, 1, 0, 100, {f"h{i}": (None, 1) for i in range(6)})
    assert "Warden's tier-1 city" in r["stories"][0]["text"]
    assert "(×" in r["stories"][1]["text"]  # the same brief twice is said once


def test_your_shield_reads_in_the_second_person():
    r = digest([battle(10, 2, 1, "attacker_won", shield=40)], NAMES, 1, 0, 100)
    assert "Your shield soaked 40" in r["stories"][0]["text"] and "You's" not in r["stories"][0]["text"]
    r = digest([battle(10, 1, 2, "attacker_won", shield=40)], NAMES, 1, 0, 100)
    assert "Raider's shield soaked 40" in r["stories"][0]["text"]


def test_an_ownerless_garrison_has_no_kill_streak():
    sieges = [battle(10 + i, 2, None, "defender_won", garrison=True, hex=f"h{i}") for i in range(3)]
    r = digest(sieges, NAMES, 1, 0, 100)
    assert not any("tear" in s["headline"] for s in r["stories"])
    assert None not in r["stats"].get("kills", {})
