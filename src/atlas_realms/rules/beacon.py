"""The public randomness beacon mixed into battle seeds: drand quicknet (League of Entropy).

The daily secret alone lets the server know every roll in advance. Each tick therefore also mixes in
the first drand round published after the tick has loaded its orders, which nobody (the server
included) can know beforehand. A battle's rolls need both: the secret (revealed the next day) and
the round (public a few seconds after the tick).

Pure: round arithmetic and the randomness check. `atlas_realms.tick.beacon` does the fetching. The
signature is stored, so anyone can verify a round against `PUBLIC_KEY` with any drand client.
"""

import hashlib
import math
from dataclasses import dataclass

CHAIN_HASH = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
PUBLIC_KEY = (
    "83cf0f2896adee7eb8b5f01fcad3912212c437e0073e911fb90022d3e760183c8c4b450b6a0a6c3ac6a5776a2d1064510d1fec758c921cc22b0e1"
    "7e63aaf4bcb5ed66304de9cf809bd274ca73bab4af5a6e9c76a4bc09e76eae8991ef5ece45a"
)
SCHEME = "bls-unchained-g1-rfc9380"
GENESIS = 1692803367  # unix seconds of round 1
PERIOD = 3  # seconds between rounds


@dataclass(frozen=True, slots=True)
class Beacon:
    round: int
    signature: bytes

    @property
    def randomness(self) -> bytes:
        """drand's randomness is sha256 of the round's signature."""
        return hashlib.sha256(self.signature).digest()


def round_time(r: int) -> int:
    """Unix seconds at which round `r` is published."""
    return GENESIS + (r - 1) * PERIOD


def round_at(ts: float) -> int:
    """The latest round published at or before `ts` (0 before genesis)."""
    return 0 if ts < GENESIS else math.floor((ts - GENESIS) / PERIOD) + 1


def round_after(ts: float) -> int:
    """The first round published strictly after `ts`: the one a tick that loaded its orders at `ts` uses."""
    return round_at(ts) + 1


def matches(b: Beacon, randomness_hex: str) -> bool:
    """A relay's reply is consistent: its randomness is the hash of its signature."""
    return b.randomness.hex() == randomness_hex.lower()
