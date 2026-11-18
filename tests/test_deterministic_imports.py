"""The deterministic code (the rules and the tick engine) imports no chain, network or database client.

Ticks must be a pure function of DB state plus the tick seed, so they can be replayed, and game logic never
reads the chain: an indexer copies chain state into the database, and the tick reads that copy (argus-mint
hard rule 6, smk762/argus-mint#53). The check runs in a fresh interpreter, so nothing another test imported
can hide a violation, and it discovers every module under ``rules/``, so a new one is covered at once.
"""

from __future__ import annotations

import json
import os
import pkgutil
import subprocess
import sys
from pathlib import Path

import atlas_realms.rules

SRC = Path(__file__).resolve().parents[1] / "src"
DETERMINISTIC = sorted(
    [f"atlas_realms.rules.{m.name}" for m in pkgutil.iter_modules(atlas_realms.rules.__path__)]
    + ["atlas_realms.tick.engine", "atlas_realms.tick.world"]
)
FORBIDDEN = (
    # chain clients, argus-mint's included (its pure core is fine; these read the chain)
    "web3", "eth_account", "argus_mint.chain", "argus_mint.ledger", "argus_mint.indexer",
    # network clients
    "httpx", "requests", "urllib3", "aiohttp", "websockets", "urllib.request", "http.client",
    # the database and the impure parts of the tick
    "psycopg", "psycopg2", "asyncpg", "sqlalchemy",
    "atlas_realms.db", "atlas_realms.tick.store", "atlas_realms.tick.runner", "atlas_realms.tick.beacon",
)  # fmt: skip


def test_the_rules_and_the_tick_engine_import_no_chain_network_or_database_client():
    assert "atlas_realms.rules.combat" in DETERMINISTIC  # the discovery found the rules
    code = (
        "import importlib, json, sys\n"
        f"for m in {DETERMINISTIC!r}: importlib.import_module(m)\n"
        f"print(json.dumps(sorted(m for m in {list(FORBIDDEN)!r} if m in sys.modules)))"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}  # this tree's code, whatever is installed
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env).stdout
    assert json.loads(out) == [], "deterministic code pulled in a chain, network or database client"
