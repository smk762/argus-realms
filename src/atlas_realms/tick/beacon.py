"""Fetch the drand round a tick mixes into its battle seeds. The I/O half of `rules.beacon`.

The runner calls `next_beacon(loaded_at)` after loading the tick's orders, so the round is one that
did not exist when the orders were fixed. It waits for the round (at most `PERIOD` seconds away) and
tries each relay in turn. If none answers within `WAIT` seconds it raises `BeaconUnavailable` and
the runner skips the tick; the orders stay pending for the next one.
"""

import http.client
import json
import time
import urllib.error
import urllib.request

import structlog

from atlas_realms.rules.beacon import CHAIN_HASH, Beacon, matches, round_after, round_time

log = structlog.get_logger()

RELAYS = (
    "https://api.drand.sh",
    "https://api2.drand.sh",
    "https://api3.drand.sh",
    "https://drand.cloudflare.com",
)
WAIT = 10.0  # seconds past the round's publish time before giving up
TIMEOUT = 3.0  # per request
USER_AGENT = "atlas-realms-tick"


class BeaconUnavailable(RuntimeError):
    pass


def fetch(r: int, relay: str) -> Beacon | None:
    """Round `r` from one relay, or None if it isn't out yet or the reply doesn't check out."""
    req = urllib.request.Request(f"{relay}/{CHAIN_HASH}/public/{r}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.load(resp)
    except (OSError, http.client.HTTPException, ValueError) as e:  # URLError and timeouts are OSErrors
        log.debug("beacon_fetch_failed", relay=relay, round=r, error=str(e))
        return None
    try:
        b = Beacon(round=int(body["round"]), signature=bytes.fromhex(body["signature"]))
        ok = b.round == r and matches(b, body["randomness"])
    except (AttributeError, KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        log.warning("beacon_bad_reply", relay=relay, round=r)
        return None
    return b


def next_beacon(loaded_at: float) -> Beacon:
    """The first round published after `loaded_at` (unix seconds)."""
    r = round_after(loaded_at)
    deadline = round_time(r) + WAIT
    while True:
        wait = round_time(r) - time.time()
        if wait > 0:
            time.sleep(wait)
        for relay in RELAYS:
            if b := fetch(r, relay):
                return b
        if time.time() >= deadline:
            raise BeaconUnavailable(f"drand round {r} not available from any relay")
        time.sleep(0.5)
