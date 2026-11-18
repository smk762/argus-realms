import http.client

import pytest

from atlas_realms.rules import beacon as rb
from atlas_realms.rules.beacon import Beacon, matches, round_after, round_at, round_time
from atlas_realms.tick import beacon as tb

# A real quicknet round, as api.drand.sh served it.
ROUND = 33769379
SIGNATURE = bytes.fromhex(
    "b46f67e956db2779d85f02754ec57193760c79e3826e12d6680653de9491e48f90d84dd494f762e95f02a48a2a7bbfcd"
)
RANDOMNESS = "9bd369906c3627e9d643ecfd0d7967a8e78e14fd557aa2e6665c89978e2e8e8c"
PUBLISHED = 1794111501


def test_randomness_is_sha256_of_signature():
    b = Beacon(ROUND, SIGNATURE)
    assert b.randomness.hex() == RANDOMNESS
    assert matches(b, RANDOMNESS.upper())
    assert not matches(Beacon(ROUND, SIGNATURE[:-1] + b"\x00"), RANDOMNESS)


def test_round_arithmetic():
    assert round_time(1) == rb.GENESIS
    assert round_time(ROUND) == PUBLISHED
    assert round_at(PUBLISHED) == ROUND
    assert round_at(PUBLISHED + 2.9) == ROUND
    assert round_at(rb.GENESIS - 1) == 0
    # A tick that loaded its orders exactly as a round came out uses the next one.
    assert round_after(PUBLISHED) == ROUND + 1
    assert round_after(PUBLISHED - 0.1) == ROUND


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


def test_next_beacon_waits_for_the_round_and_falls_back_across_relays(monkeypatch):
    clock = _Clock(PUBLISHED - 1)
    monkeypatch.setattr(tb, "time", clock)
    asked = []

    def fetch(r, relay):
        asked.append((r, relay, clock.now))
        return Beacon(r, SIGNATURE) if relay == tb.RELAYS[1] else None

    monkeypatch.setattr(tb, "fetch", fetch)
    b = tb.next_beacon(PUBLISHED - 1)
    assert b.round == ROUND
    assert asked[0] == (ROUND, tb.RELAYS[0], PUBLISHED)  # slept until the round was out
    assert [a[1] for a in asked] == list(tb.RELAYS[:2])


def test_next_beacon_gives_up_after_the_wait(monkeypatch):
    monkeypatch.setattr(tb, "time", _Clock(PUBLISHED))
    monkeypatch.setattr(tb, "fetch", lambda r, relay: None)
    with pytest.raises(tb.BeaconUnavailable):
        tb.next_beacon(PUBLISHED)


def test_fetch_rejects_a_reply_that_does_not_check_out(monkeypatch):
    import io
    import json

    def reply(body):
        def urlopen(req, timeout):
            return io.BytesIO(json.dumps(body).encode())

        return urlopen

    good = {"round": ROUND, "signature": SIGNATURE.hex(), "randomness": RANDOMNESS}
    monkeypatch.setattr(tb.urllib.request, "urlopen", reply(good))
    assert tb.fetch(ROUND, "https://relay") == Beacon(ROUND, SIGNATURE)
    monkeypatch.setattr(tb.urllib.request, "urlopen", reply({**good, "randomness": "00" * 32}))
    assert tb.fetch(ROUND, "https://relay") is None
    monkeypatch.setattr(tb.urllib.request, "urlopen", reply({**good, "round": ROUND + 1}))
    assert tb.fetch(ROUND, "https://relay") is None


@pytest.mark.parametrize(
    "body",
    [
        [],
        None,
        {"round": ROUND},
        {"round": ROUND, "signature": "not hex", "randomness": RANDOMNESS},
        {"round": "x", "signature": SIGNATURE.hex(), "randomness": RANDOMNESS},
        {"round": ROUND, "signature": SIGNATURE.hex(), "randomness": 7},
    ],
)
def test_fetch_returns_none_for_a_malformed_reply(monkeypatch, body):
    import io
    import json

    monkeypatch.setattr(tb.urllib.request, "urlopen", lambda req, timeout: io.BytesIO(json.dumps(body).encode()))
    assert tb.fetch(ROUND, "https://relay") is None


@pytest.mark.parametrize(
    "error", [http.client.IncompleteRead(b""), ConnectionResetError(), UnicodeDecodeError("utf-8", b"", 0, 1, "x")]
)
def test_fetch_returns_none_when_reading_the_body_fails(monkeypatch, error):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            raise error

    monkeypatch.setattr(tb.urllib.request, "urlopen", lambda req, timeout: Resp())
    assert tb.fetch(ROUND, "https://relay") is None
