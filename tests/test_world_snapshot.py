import copy
import dataclasses
import types
import typing

from atlas_realms.tick.world import Contract, Hex, Unit, User, World

# The fields World.snapshot() copies itself; every other field must hold immutable values only.
COPIED = {(Unit, "path")}
IMMUTABLE = (int, float, str, bool, type(None), tuple)


def immutable(tp) -> bool:
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin is typing.Literal:
        return True
    if origin in (typing.Union, types.UnionType):
        return all(immutable(a) for a in args)
    if origin is tuple:
        return all(a is Ellipsis or immutable(a) for a in args)
    return tp in IMMUTABLE


def test_snapshot_only_shares_immutable_fields():
    for cls in (Hex, Unit, User, Contract):
        hints = typing.get_type_hints(cls)
        for f in dataclasses.fields(cls):
            if (cls, f.name) not in COPIED:
                assert immutable(hints[f.name]), (
                    f"{cls.__name__}.{f.name}: copy it in World.snapshot() or keep it immutable"
                )


def world() -> World:
    hexes = {"a": Hex(h3="a", zone="land", chunks=((10, 1, 2),), owner_id=2), "b": Hex(h3="b", zone="water")}
    units = {1: Unit(id=1, user_id=1, hex="a", hp=100, path=["b", "a"])}
    users = {1: User(id=1, treasury=5), 2: User(id=2, bot=True)}
    contracts = {1: Contract(id=1, bot=2, hirer=1, kind="merc", price=1, start=0, minutes=60, target_user=3)}
    return World(tick=4, hexes=hexes, units=units, users=users, ready_chunks={(10, 1, 2)}, contracts=contracts)


def test_snapshot_is_a_deep_copy():
    w = world()
    s = w.snapshot()
    assert s == copy.deepcopy(w)
    s.hexes["a"].owner_id = 9
    s.units[1].path.append("c")
    s.units[1].hp = 1
    s.users[1].treasury = 0
    s.contracts[1].status = "active"
    s.ready_chunks.add((10, 0, 0))
    assert w == world()
