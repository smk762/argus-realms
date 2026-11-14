"""Battle resolution.

A battle is up to `battle_rolls` opposed rolls. Each side's strength is
d100 x (1 + sum of its modifiers); the higher strength wins the roll and the loser takes
uniform damage_min..damage_max. A tie does nothing. The battle stops when either side hits 0 HP.

The result carries everything the public battle log needs to re-run it: the seed, every
modifier with its source, and each roll pair with strengths and damage.
"""

from enum import StrEnum

from pydantic import BaseModel

from atlas_realms.rules.dice import Dice
from atlas_realms.rules.tunables import DEFAULT, Tunables


class Outcome(StrEnum):
    ATTACKER_WON = "attacker_won"
    DEFENDER_WON = "defender_won"
    BOTH_WOUNDED = "both_wounded"


class Modifier(BaseModel):
    source: str  # e.g. "attack_bonus", "terrain:hills", "flanking"
    value: float


class Side(BaseModel):
    unit_id: str
    hp: int
    modifiers: list[Modifier] = []

    @property
    def multiplier(self) -> float:
        return 1 + sum(m.value for m in self.modifiers)


class Roll(BaseModel):
    attacker_d100: int
    defender_d100: int
    attacker_strength: float
    defender_strength: float
    winner: str | None  # "attacker", "defender", or None on a tie
    damage: int


class BattleResult(BaseModel):
    seed: str
    attacker: Side
    defender: Side
    attacker_hp_after: int
    defender_hp_after: int
    rolls: list[Roll]
    outcome: Outcome


def resolve_battle(attacker: Side, defender: Side, dice: Dice, t: Tunables = DEFAULT) -> BattleResult:
    a_hp, d_hp = attacker.hp, defender.hp
    a_mult, d_mult = attacker.multiplier, defender.multiplier
    rolls: list[Roll] = []
    for _ in range(t.battle_rolls):
        a_roll, d_roll = dice.d100(), dice.d100()
        a_str, d_str = a_roll * a_mult, d_roll * d_mult
        winner, damage = None, 0
        if a_str > d_str:
            winner, damage = "attacker", dice.randint(t.damage_min, t.damage_max)
            d_hp = max(0, d_hp - damage)
        elif d_str > a_str:
            winner, damage = "defender", dice.randint(t.damage_min, t.damage_max)
            a_hp = max(0, a_hp - damage)
        rolls.append(
            Roll(
                attacker_d100=a_roll,
                defender_d100=d_roll,
                attacker_strength=a_str,
                defender_strength=d_str,
                winner=winner,
                damage=damage,
            )
        )
        if a_hp == 0 or d_hp == 0:
            break
    if d_hp == 0:
        outcome = Outcome.ATTACKER_WON
    elif a_hp == 0:
        outcome = Outcome.DEFENDER_WON
    else:
        outcome = Outcome.BOTH_WOUNDED
    return BattleResult(
        seed=dice.seed.hex(),
        attacker=attacker,
        defender=defender,
        attacker_hp_after=a_hp,
        defender_hp_after=d_hp,
        rolls=rolls,
        outcome=outcome,
    )


def simulate(attack: float, defence: float, battles: int, t: Tunables = DEFAULT) -> tuple[float, float]:
    """(decisive share, attacker share of decisive) over `battles` full-HP battles."""
    wins = losses = 0
    for i in range(battles):
        dice = Dice(i.to_bytes(8, "big"))
        a = Side(unit_id="a", hp=t.max_hp, modifiers=[Modifier(source="attack", value=attack)])
        d = Side(unit_id="d", hp=t.max_hp, modifiers=[Modifier(source="defence", value=defence)])
        r = resolve_battle(a, d, dice, t)
        wins += r.outcome is Outcome.ATTACKER_WON
        losses += r.outcome is Outcome.DEFENDER_WON
    decisive = wins + losses
    return decisive / battles, wins / decisive if decisive else 0.0


# The matchups in the design doc's "Simulated rates" table.
DOC_MATCHUPS = {
    "Even, no modifiers": (0.0, 0.0),
    "Attacker +15% vs fresh defender +5%": (0.15, 0.05),
    "Attacker +15% vs dug-in defender +20%": (0.15, 0.20),
    "Marched attacker 0% vs dug-in +20%": (0.0, 0.20),
}


if __name__ == "__main__":
    import sys

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000
    for name, (att, dfn) in DOC_MATCHUPS.items():
        dec, att_share = simulate(att, dfn, n)
        print(f"{name:40s} decisive {dec:6.1%}  attacker wins {att_share:6.1%}")
