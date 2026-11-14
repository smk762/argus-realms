import pytest

from atlas_realms.rules.combat import DOC_MATCHUPS, Modifier, Outcome, Side, resolve_battle, simulate
from atlas_realms.rules.dice import Dice


def _side(uid: str, hp: int = 100, *mods: float) -> Side:
    return Side(unit_id=uid, hp=hp, modifiers=[Modifier(source=f"m{i}", value=v) for i, v in enumerate(mods)])


def test_battle_is_replayable_from_its_seed():
    a, d = _side("a", 100, 0.15), _side("d", 100, 0.05)
    first = resolve_battle(a, d, Dice(b"seed"))
    again = resolve_battle(a, d, Dice(bytes.fromhex(first.seed)))
    assert first == again


def test_log_is_consistent():
    r = resolve_battle(_side("a"), _side("d"), Dice(b"log"))
    assert 1 <= len(r.rolls) <= 10
    a_taken = sum(x.damage for x in r.rolls if x.winner == "defender")
    d_taken = sum(x.damage for x in r.rolls if x.winner == "attacker")
    assert r.attacker_hp_after == max(0, 100 - a_taken)
    assert r.defender_hp_after == max(0, 100 - d_taken)
    for x in r.rolls:
        assert (x.winner is None) == (x.damage == 0)
        if x.winner:
            assert 16 <= x.damage <= 22


def test_outcome_matches_hp():
    for i in range(200):
        r = resolve_battle(_side("a"), _side("d"), Dice(i.to_bytes(4, "big")))
        if r.outcome is Outcome.ATTACKER_WON:
            assert r.defender_hp_after == 0 < r.attacker_hp_after
        elif r.outcome is Outcome.DEFENDER_WON:
            assert r.attacker_hp_after == 0 < r.defender_hp_after
        else:
            assert len(r.rolls) == 10 and r.attacker_hp_after > 0 and r.defender_hp_after > 0


def test_wounded_unit_goes_down_fast():
    # 16 HP dies to any single lost roll.
    r = resolve_battle(_side("a", 16), _side("d"), Dice(b"wounded"))
    assert r.outcome is not Outcome.BOTH_WOUNDED


# The design doc's "Simulated rates" table: (decisive, attacker share of decisive).
DOC_RATES = {
    "Even, no modifiers": (0.80, 0.50),
    "Attacker +15% vs fresh defender +5%": (0.83, 0.63),
    "Attacker +15% vs dug-in defender +20%": (0.83, 0.44),
    "Marched attacker 0% vs dug-in +20%": (0.85, 0.26),
}


@pytest.mark.slow
@pytest.mark.parametrize("name", list(DOC_RATES))
def test_matches_design_doc_rates(name):
    decisive, attacker_share = simulate(*DOC_MATCHUPS[name], battles=10_000)
    want_dec, want_att = DOC_RATES[name]
    assert decisive == pytest.approx(want_dec, abs=0.02)
    assert attacker_share == pytest.approx(want_att, abs=0.025)
