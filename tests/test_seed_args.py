"""`python -m atlas_realms.seed`'s arguments: spawn points south of the equator, in either form."""

import pytest

from atlas_realms.seed import parse_args


def test_a_southern_spawn_parses_with_or_without_the_equals_sign():
    a = parse_args(
        ["m.json", "o.geojson", "--spawn", "-33.8,151.2", "--spawn=-41.2865,174.7762", "--spawn", "64.1,-21.9"]
    )
    assert a.spawn == [(-33.8, 151.2), (-41.2865, 174.7762), (64.1, -21.9)]


@pytest.mark.parametrize("bad", ["-33.8", "1,2,3", "x,y", "95,0"])
def test_a_spawn_must_be_one_lat_lng_point(bad, capsys):
    with pytest.raises(SystemExit):
        parse_args(["m.json", "o.geojson", f"--spawn={bad}"])
    assert "--spawn" in capsys.readouterr().err
