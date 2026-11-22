"""The solo arena in one process: what `atlas_realms.api` serves over HTTP, as `Arena.call()`.

The browser build runs this in Pyodide inside a Web Worker (`arena/src/worker.mjs`); the parity tests run it under
CPython too. There is no server: the client's requests become calls here, with the wall clock passed in (`now`, in
seconds) so the lazy catch-up ticking works exactly as on the server. One session per tab, id "local".

Seeding: the session's battle secret is its game seed (32 bytes, random unless the client passes `seed`). Bots and
the news desk already derive their randomness from it, so a seed plus the same orders at the same ticks replays the
game. The shared world's drand beacon is not used here.

Regions: the arena offers several maps (`regions.json`: an id, a title, the world manifest, the land outline and the
spawn points of each). A new game picks one; the session remembers it, and a restored save says which it is.

Saving: `save()` returns the session as a compressed, versioned blob for the client's localStorage, `restore()`
reads one back. A blob from another build of the rules is refused (409) rather than loaded into changed dataclasses.
"""

import base64
import contextlib
import hashlib
import io
import json
import os
import pickle
import sys
import threading
import types
import zlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def _stub_db_driver() -> None:
    """psycopg has no Pyodide build. `atlas_realms.seed` and `atlas_realms.db` import it at module level for the shared
    world's database path, which the solo arena never takes; a stub keeps those imports working and fails loudly if
    anything does try to connect."""
    try:
        import psycopg  # noqa: F401
    except ImportError:

        def no_db(*_a, **_k):
            raise RuntimeError("no database in the browser build")

        pg = types.ModuleType("psycopg")
        pg.Connection = object
        pg.connect = no_db
        pg_types = types.ModuleType("psycopg.types")
        pg_json = types.ModuleType("psycopg.types.json")
        pg_json.Jsonb = no_db
        pg.types, pg_types.json = pg_types, pg_json
        sys.modules.update({"psycopg": pg, "psycopg.types": pg_types, "psycopg.types.json": pg_json})


_stub_db_driver()

from pydantic import ValidationError  # noqa: E402

from atlas_realms.news import STRAIT  # noqa: E402
from atlas_realms.protocol import Control, Looks, NewSolo, OrderIn, check_new, meta  # noqa: E402
from atlas_realms.solo import Session, build_world  # noqa: E402

SID = "local"
# 2: the open strait (all-sea chunks in the block are playable); 3: regions (the session records its map).
# Older saves are refused.
SAVE_VERSION = 3


@dataclass(frozen=True)
class Region:
    id: str
    title: str
    manifest: dict
    outline: Path
    spawns: tuple[tuple[float, float], ...]
    desk: dict | None = None  # the news desk's paper and place names (news.STRAIT's keys); None = Cook Strait's

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title}


def load_regions(index: Path, root: Path) -> list[Region]:
    """The regions listed in `index` (arena/regions.json, or the build's copy in the zip): [{id, title, manifest,
    outline, spawns}], the paths relative to `root`. The first is the default."""
    out = []
    for r in json.loads(index.read_text())["regions"]:
        manifest = json.loads((root / r["manifest"]).read_text())
        spawns = tuple((float(lat), float(lng)) for lat, lng in r["spawns"])
        out.append(Region(r["id"], r["title"], manifest, root / r["outline"], spawns, r.get("news")))
    return out


class _SaveUnpickler(pickle.Unpickler):
    """Unpickles a save without running arbitrary code: a blob may come from anywhere (an edited localStorage entry, a
    save someone shared), so it may only name the game's own classes, never a function or another module's names."""

    def find_class(self, module: str, name: str):
        if (module == "atlas_realms" or module.startswith("atlas_realms.")) and "." not in name:
            obj = super().find_class(module, name)
            if isinstance(obj, type) and obj.__module__ == module:
                return obj
        raise pickle.UnpicklingError(f"a save may not reference {module}.{name}")


def _load_save(raw: bytes):
    return _SaveUnpickler(io.BytesIO(raw)).load()


class HttpError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def rules_build() -> str:
    """A short hash of the game's Python sources: saves are only loaded by the build that wrote them."""
    import atlas_realms

    root = Path(atlas_realms.__file__).parent
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        if p.name == "_version.py":  # written from git at install time, not part of the rules
            continue
        h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes())
    return h.hexdigest()[:16]


class Arena:
    def __init__(self, regions: list[Region]):
        if not regions:
            raise ValueError("no regions")
        self.regions = {r.id: r for r in regions}
        self.default = regions[0].id
        self.session: Session | None = None
        self.build = rules_build()

    # -- the API --------------------------------------------------------------------------------

    def call(self, method: str, url: str, body: dict | None = None, now: float = 0.0) -> dict:
        """One request, routed like atlas_realms.api. Raises HttpError for what the server answers 4xx."""
        parts = urlsplit(url)
        path = [p for p in parts.path.split("/") if p]
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        try:
            return self._route(method.upper(), path, query, body or {}, now)
        except ValidationError as e:
            raise HttpError(422, e.errors(include_url=False)[0]["msg"]) from e

    def _route(self, method: str, path: list[str], query: dict, body: dict, now: float) -> dict:
        if path == ["api", "regions"]:
            return {"default": self.default, "regions": [r.summary() for r in self.regions.values()]}
        if path == ["api", "manifest"]:
            return self._region(query.get("region") or (self.session.region if self.session else None)).manifest
        if path == ["api", "meta"]:
            return meta()
        if path == ["api", "solo"] and method == "POST":
            return self.new(body, now)
        if len(path) != 4 or path[:2] != ["api", "solo"]:
            raise HttpError(404, "not found")
        s = self._get(path[2])
        verb = path[3]
        if verb == "hexes":
            return s.hex_geojson()
        if verb == "fog":
            return s.fog()
        if verb == "state":
            s.catch_up(now)
            return s.state()
        if verb == "orders" and method == "POST":
            o = OrderIn.model_validate(body)
            s.catch_up(now)  # the order joins the next tick due from now, not one already past (restore's shield aside)
            try:
                return {"id": s.submit(o.kind, o.payload)}
            except ValueError as e:
                raise HttpError(422, str(e)) from e
        if verb == "control" and method == "POST":
            c = Control.model_validate(body)
            s.catch_up(now)
            if c.paused is not None:
                s.paused = c.paused
            if c.tick_every is not None:
                s.tick_every = c.tick_every
            return {"paused": s.paused, "tick_every": s.tick_every}
        if verb == "report":
            s.catch_up(now)
            try:
                since = int(query.get("since", 0))
            except ValueError as e:
                raise HttpError(422, "since must be a tick number") from e
            return s.report(since)
        if verb == "looks" and method == "POST":
            lk = Looks.model_validate(body)
            try:
                s.set_look(lk.shield)
            except PermissionError as e:
                raise HttpError(403, str(e)) from e
            except ValueError as e:
                raise HttpError(422, str(e)) from e
            return {"shield": lk.shield}
        raise HttpError(404, "not found")

    def _region(self, rid: str | None) -> Region:
        r = self.regions.get(rid or self.default)
        if r is None:
            raise HttpError(422, f"unknown region {rid!r}")
        return r

    def _get(self, sid: str) -> Session:
        if sid != SID or self.session is None:
            raise HttpError(404, "no such session")
        return self.session

    def new(self, body: dict, now: float) -> dict:
        body = dict(body)
        seed = body.pop("seed", None)
        req = NewSolo.model_validate(body)
        if seed is not None and not isinstance(seed, str):
            raise HttpError(422, "seed must be a hex string")
        try:
            check_new(req)
            secret = bytes.fromhex(seed) if seed else os.urandom(32)
        except ValueError as e:
            raise HttpError(422, str(e)) from e
        region = self._region(req.region)
        world = build_world(region.manifest, region.outline, list(region.spawns))
        s = Session.new(
            world,
            req.name,
            req.unit_class,
            tuple(req.bots),
            req.subscriber,
            tick_every=req.tick_every,
            secret=secret,
            region=region.id,
            desk={**STRAIT, **(region.desk or {})},  # a desk missing a key keeps Cook Strait's
        )
        s._last = now
        self.session = s
        return {"id": SID, "seed": secret.hex(), "region": region.id}

    # -- saving ---------------------------------------------------------------------------------

    def save(self) -> str:
        """The session as text for localStorage: base64(zlib(pickle)), with the save format and rules build."""
        s = self._get(SID)
        fields = {k: v for k, v in vars(s).items() if k != "lock"}
        raw = pickle.dumps({"v": SAVE_VERSION, "build": self.build, "session": fields}, protocol=5)
        return base64.b64encode(zlib.compress(raw, 6)).decode()

    def restore(self, blob: str, now: float, away: str | None = None) -> dict:
        """Load a save. `away` is what the client's "When away" setting did as the tab closed: "pause" keeps the time
        it was closed out of the game, "shield" raises the shield before the missed ticks are caught up."""
        try:
            data = _load_save(zlib.decompress(base64.b64decode(blob)))
        except Exception as e:
            raise HttpError(422, "unreadable save") from e
        if not isinstance(data, dict) or not isinstance(data.get("session"), dict):
            raise HttpError(422, "unreadable save")
        if data.get("v") != SAVE_VERSION or data.get("build") != self.build:
            raise HttpError(409, "saved by another version of the game")
        if data["session"].get("region") not in self.regions:
            raise HttpError(409, "saved on a map this build doesn't have")
        s = Session.__new__(Session)
        vars(s).update(data["session"])
        s.lock = threading.Lock()
        if away == "pause":
            s.paused = True
            s._last = now
        elif away == "shield":
            with contextlib.suppress(ValueError):  # the shield order was refused: load the game anyway, unshielded
                s.submit("shield", {"on": True})
        self.session = s
        return {"id": SID, "seed": s.secret.hex(), "tick": s.world.tick, "region": s.region}

    # -- the worker's entry point ---------------------------------------------------------------

    def handle(self, request: str) -> str:
        """JSON in, JSON out, for the worker: {op, method, url, body, now, blob, away} -> {status, body}."""
        try:
            try:
                r = json.loads(request)
            except ValueError as e:
                raise HttpError(400, "request is not JSON") from e
            if not isinstance(r, dict):
                raise HttpError(400, "request must be a JSON object")
            op = r.get("op", "call")
            if op == "call":
                out = self.call(r["method"], r["url"], r.get("body"), r["now"])
            elif op == "save":
                out = {"blob": self.save(), "tick": self._get(SID).world.tick}
            elif op == "restore":
                out = self.restore(r["blob"], r["now"], r.get("away"))
            else:
                raise HttpError(400, f"unknown op {op}")
            return json.dumps({"status": 200, "body": out})
        except HttpError as e:
            return json.dumps({"status": e.status, "body": {"detail": e.detail}})
        except Exception as e:  # never throw into the worker: every request gets a {status, body} answer
            return json.dumps({"status": 500, "body": {"detail": f"{type(e).__name__}: {e}"}})
