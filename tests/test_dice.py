import hashlib
import hmac
from collections import Counter

import pytest

from atlas_realms.rules.dice import Dice, battle_seed, secret_commitment


def test_same_seed_same_rolls():
    seed = battle_seed(b"day-secret", tick_id=1440, battle_id=7)
    a, b = Dice(seed), Dice(seed)
    assert [a.d100() for _ in range(50)] == [b.d100() for _ in range(50)]


def test_seed_depends_on_every_input():
    base = battle_seed(b"s", 1, 1)
    assert base != battle_seed(b"t", 1, 1)
    assert base != battle_seed(b"s", 2, 1)
    assert base != battle_seed(b"s", 1, 2)


def test_beacon_is_mixed_in_and_old_seeds_stay_valid():
    assert battle_seed(b"s", 1, 1) == hmac.new(b"s", b"1:1", hashlib.sha256).digest()
    beacon = bytes(range(32))
    assert battle_seed(b"s", 1, 1, beacon) == hmac.new(b"s", f"1:1:{beacon.hex()}".encode(), hashlib.sha256).digest()
    assert battle_seed(b"s", 1, 1, beacon) != battle_seed(b"s", 1, 1, bytes(32))


def test_commitment_is_sha256_hex():
    assert secret_commitment(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_range_and_rough_uniformity():
    dice = Dice(b"uniform")
    counts = Counter(dice.randint(16, 22) for _ in range(70_000))
    assert set(counts) == set(range(16, 23))
    assert all(9_400 < n < 10_600 for n in counts.values())


def test_empty_range_rejected():
    with pytest.raises(ValueError):
        Dice(b"x").randint(5, 4)


def test_the_stream_is_the_documented_one():
    """Players re-run battles from logs with their own code, so the stream is a public contract: block i is
    SHA256(seed || i as 8-byte big-endian), read as 4-byte big-endian words, rejection-sampled into [lo, hi].
    Changing it invalidates every published log."""

    def words(seed: bytes):
        i = 0
        while True:
            block = hashlib.sha256(seed + i.to_bytes(8, "big")).digest()
            yield from (int.from_bytes(block[j : j + 4], "big") for j in range(0, 32, 4))
            i += 1

    def reference(seed: bytes, ranges: list[tuple[int, int]]) -> list[int]:
        stream, out = words(seed), []
        for lo, hi in ranges:
            n = hi - lo + 1
            limit = 2**32 - 2**32 % n
            w = next(stream)
            while w >= limit:
                w = next(stream)
            out.append(lo + w % n)
        return out

    seed = battle_seed(b"day-secret", tick_id=1440, battle_id=7)
    ranges = [(1, 100), (16, 22), (0, 2**31 + 1), (5, 5), (1, 6)] * 40  # 2**31 + 2 values rejects about half the words
    dice = Dice(seed)
    assert [dice.randint(lo, hi) for lo, hi in ranges] == reference(seed, ranges)
    assert Dice(b"").d100() == 1 + int.from_bytes(hashlib.sha256(bytes(8)).digest()[:4], "big") % 100
