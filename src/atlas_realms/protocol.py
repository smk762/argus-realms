"""The solo arena's request models and metadata, without FastAPI.

`atlas_realms.api` serves these over HTTP. The browser build (`arena/`) runs the same models in Pyodide,
where FastAPI isn't available, so both validate requests and describe the game the same way.
"""

from pydantic import BaseModel, Field

from atlas_realms.bots import PERSONALITIES
from atlas_realms.rules.classes import CLASSES
from atlas_realms.rules.tunables import DEFAULT
from atlas_realms.solo import DEFAULT_ROSTER, SHIELD_LOOKS

SPAWNS = [(-41.2865, 174.7762), (-41.2906, 174.0010)]  # Wellington side, Picton side


class NewSolo(BaseModel):
    name: str = Field("You", min_length=1, max_length=24)
    unit_class: str = "barbarian"
    bots: list[str] = list(DEFAULT_ROSTER)
    tick_every: float = Field(2.0, ge=0.25, le=60)
    subscriber: bool = False


class Looks(BaseModel):
    shield: str


class OrderIn(BaseModel):
    kind: str
    payload: dict = {}


class Control(BaseModel):
    paused: bool | None = None
    tick_every: float | None = Field(None, ge=0.25, le=60)


def check_new(body: NewSolo) -> None:
    """Raise ValueError for a class or bot the game doesn't have."""
    if body.unit_class not in CLASSES:
        raise ValueError("unknown class")
    if any(b not in PERSONALITIES for b in body.bots):
        raise ValueError("unknown bot personality")


def meta() -> dict:
    return {
        "classes": {k: v.model_dump() for k, v in CLASSES.items()},
        "personalities": {k: {"class": p.unit_class, "blurb": p.blurb} for k, p in PERSONALITIES.items()},
        "shield_looks": SHIELD_LOOKS,
        "shield_daily_hours": DEFAULT.shield_daily_hours,
        "shield_cooldown_minutes": DEFAULT.shield_cooldown_minutes,
    }
