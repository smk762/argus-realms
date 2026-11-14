"""Verifiable dice.

A battle's seed is HMAC-SHA256(daily secret, "<tick id>:<battle id>:<beacon hex>"), where the beacon
is the drand round's randomness for that tick (`rules.beacon`). The server publishes sha256(secret)
ahead of the day and reveals the secret the day after, so anyone holding a battle log can recompute
the seed and every roll. The beacon means the server can't know the rolls ahead either. Without a
beacon (the solo arena, and logs from before it) the message is "<tick id>:<battle id>".

The stream is deliberately simple so a third party can reimplement it in a few lines:
block i = SHA256(seed || i as 8-byte big-endian), read as 4-byte big-endian words.
Integers in [lo, hi] use rejection sampling over those words, so there is no modulo bias.
"""

import hashlib
import hmac

_WORD = 4
_SPAN = 1 << 32


def battle_seed(daily_secret: bytes, tick_id: int, battle_id: int, beacon: bytes | None = None) -> bytes:
    msg = f"{tick_id}:{battle_id}" if beacon is None else f"{tick_id}:{battle_id}:{beacon.hex()}"
    return hmac.new(daily_secret, msg.encode(), hashlib.sha256).digest()


def secret_commitment(daily_secret: bytes) -> str:
    """The hash published before the day starts."""
    return hashlib.sha256(daily_secret).hexdigest()


class Dice:
    def __init__(self, seed: bytes) -> None:
        self.seed = seed
        self._block = 0
        self._buf = b""

    def _word(self) -> int:
        if not self._buf:
            self._buf = hashlib.sha256(self.seed + self._block.to_bytes(8, "big")).digest()
            self._block += 1
        word, self._buf = self._buf[:_WORD], self._buf[_WORD:]
        return int.from_bytes(word, "big")

    def randint(self, lo: int, hi: int) -> int:
        """Uniform integer in [lo, hi], inclusive."""
        n = hi - lo + 1
        if n <= 0:
            raise ValueError(f"empty range [{lo}, {hi}]")
        limit = _SPAN - _SPAN % n
        while True:
            w = self._word()
            if w < limit:
                return lo + w % n

    def d100(self) -> int:
        return self.randint(1, 100)
