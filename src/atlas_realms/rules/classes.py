"""Player classes. Every player can pick any class; subscribers only swap more often.

Each class trades a strength for a weakness, and each strength has a class that answers it.
The numbers are provisional: they are meant to be balanced by simulation, like the combat table.

| Class     | Strength                                        | Weakness                  | Answered by |
|-----------|-------------------------------------------------|---------------------------|-------------|
| barbarian | none: the generalist and the default            | none                      | -           |
| marine    | 4x speed on water                               | 80 km/h on land           | tank        |
| medic     | heals 1.5x, and +10 HP a tick to allies on hex  | -10% attack               | saboteur    |
| saboteur  | ignores the defender's defence and capital bonus| -10% defence              | tank        |
| tank      | +10% attack                                     | 75 km/h on land           | marine      |
"""

from pydantic import BaseModel, ConfigDict


class ClassSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    land_cost: float = 1.0  # multiplies the cost of every non-water hex (1.25 = 80 km/h)
    water_cost: float = 1.0  # multiplies the cost of water hexes (0.25 = 4x speed)
    attack: float = 0.0  # added to the unit's modifiers when it attacks
    defence: float = 0.0  # added to the unit's modifiers when it defends
    heal_mult: float = 1.0
    heals_allies: int = 0  # HP a tick to each allied unit on the same hex
    ignores_defence: bool = False  # as attacker: the defender's defence bonus and capital fortification don't apply


CLASSES: dict[str, ClassSpec] = {
    "barbarian": ClassSpec(),
    "marine": ClassSpec(water_cost=0.25, land_cost=1.25),
    "medic": ClassSpec(heal_mult=1.5, heals_allies=10, attack=-0.10),
    "saboteur": ClassSpec(ignores_defence=True, defence=-0.10),
    "tank": ClassSpec(attack=0.10, land_cost=4 / 3),
}
DEFAULT_CLASS = "barbarian"

# Which class answers each one: the table's last column. Bots that counter-pick read it.
ANSWERED_BY: dict[str, str] = {"marine": "tank", "medic": "saboteur", "saboteur": "tank", "tank": "marine"}
