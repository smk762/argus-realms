"""HTTP API and static files for the browser client.

    python -m atlas_realms.api --tiles var/tiles-areas/cook-strait --port 8110      # then open http://127.0.0.1:8110
    python -m atlas_realms.api --world cook-strait,iceland --host 0.0.0.0           # the shared world's API

Solo mode: sessions live in memory and are lost on restart, so it runs as one process. World mode (`--world`, what
Kubernetes runs, `deploy/k8s/`) keeps nothing in memory: every request reads the regions' databases, so it scales
out. For now it reports each region's tick; PvP (accounts, orders) adds its routes there without changing the
client's state shape.
"""

import argparse
import json
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from atlas_realms import __version__
from atlas_realms.db import connect
from atlas_realms.protocol import SPAWNS, Control, Looks, NewSolo, OrderIn, check_new
from atlas_realms.protocol import meta as solo_meta
from atlas_realms.regions import is_ready, region_dsn
from atlas_realms.solo import Session, build_world

log = structlog.get_logger()

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
OUTLINE = ROOT / "data" / "nz_mainland.geojson"
MAX_SESSIONS = 50
IDLE_SECONDS = 30 * 60  # a session untouched this long may be evicted to make room for a new one
MAX_PER_CLIENT = 3  # games one client address may hold: its oldest makes room for a new one, so no one fills the arena
MAX_BUILDS = 2  # world builds running at once (each is CPU-heavy)
BUILD_WAIT = 30.0  # seconds a new game waits for a build slot before a 503
READY_POLL = 3.0  # seconds between looks at a region's database until it is migrated and seeded


def create_app(tiles: Path, web: Path = WEB, outline: Path = OUTLINE) -> FastAPI:
    app = FastAPI(title="Atlas Realms")
    manifest = json.loads((tiles / "manifest.json").read_text())
    sessions: dict[str, Session] = {}
    seen: dict[str, float] = {}  # sid -> last request (monotonic)
    owner: dict[str, str] = {}  # sid -> the client address that created it
    lock = threading.Lock()  # the sync routes run in a threadpool
    builds = threading.BoundedSemaphore(MAX_BUILDS)

    def get(sid: str) -> Session:
        with lock:
            s = sessions.get(sid)
            if s is None:
                raise HTTPException(404, "no such session")
            seen[sid] = time.monotonic()
        return s

    def drop(sid: str) -> None:
        sessions.pop(sid, None)
        seen.pop(sid, None)
        owner.pop(sid, None)

    def make_room(client: str) -> None:
        """Called under `lock`. A client at its quota gives up its own least recently used game. Then, if the store is
        full, evict the longest-idle session, but never one still in use."""
        mine = [sid for sid, who in owner.items() if who == client]
        if len(mine) >= MAX_PER_CLIENT:
            drop(min(mine, key=seen.__getitem__))
        if len(sessions) < MAX_SESSIONS:
            return
        oldest = min(seen, key=seen.__getitem__)
        if time.monotonic() - seen[oldest] < IDLE_SECONDS:
            raise HTTPException(503, "the arena is full: try again later")
        drop(oldest)

    @app.get("/api/manifest")
    def get_manifest() -> dict:
        return manifest

    @app.get("/api/meta")
    def meta() -> dict:
        return solo_meta()

    @app.post("/api/solo")
    def new_solo(body: NewSolo, request: Request) -> dict:
        try:
            check_new(body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if body.region not in (None, "cook-strait"):  # the server's solo mode has only Cook Strait
            raise HTTPException(422, f"unknown region {body.region!r}")
        client = request.client.host if request.client else "?"  # behind a proxy, configure its forwarded headers
        with lock:
            make_room(client)  # refuse before the expensive world build
        if not builds.acquire(timeout=BUILD_WAIT):
            raise HTTPException(503, "the arena is busy: try again shortly")
        try:
            world = build_world(manifest, outline, SPAWNS)
        finally:
            builds.release()
        s = Session.new(
            world, body.name, body.unit_class, tuple(body.bots), body.subscriber, tick_every=body.tick_every
        )
        sid = secrets.token_urlsafe(9)
        with lock:
            make_room(client)  # again: another request may have filled the store meanwhile
            sessions[sid], seen[sid], owner[sid] = s, time.monotonic(), client
        return {"id": sid}

    @app.get("/api/solo/{sid}/hexes")
    def hexes(sid: str) -> dict:
        return get(sid).hex_geojson()

    @app.get("/api/solo/{sid}/fog")
    def fog(sid: str) -> dict:
        s = get(sid)
        with s.lock:
            return s.fog()

    @app.get("/api/solo/{sid}/state")
    def state(sid: str) -> dict:
        s = get(sid)
        with s.lock:
            s.catch_up()
            return s.state()

    @app.post("/api/solo/{sid}/orders")
    def order(sid: str, body: OrderIn) -> dict:
        s = get(sid)
        with s.lock:
            s.catch_up()  # the order joins the next tick due from now, not one already past
            try:
                return {"id": s.submit(body.kind, body.payload)}
            except ValueError as e:
                raise HTTPException(422, str(e)) from e

    @app.post("/api/solo/{sid}/control")
    def control(sid: str, body: Control) -> dict:
        s = get(sid)
        with s.lock:
            s.catch_up()
            if body.paused is not None:
                s.paused = body.paused
            if body.tick_every is not None:
                s.tick_every = body.tick_every
            return {"paused": s.paused, "tick_every": s.tick_every}

    @app.get("/api/solo/{sid}/report")
    def report(sid: str, since: int) -> dict:
        s = get(sid)
        with s.lock:
            s.catch_up()
            return s.report(since)

    @app.post("/api/solo/{sid}/looks")
    def looks(sid: str, body: Looks) -> dict:
        s = get(sid)
        with s.lock:
            try:
                s.set_look(body.shield)
            except PermissionError as e:
                raise HTTPException(403, str(e)) from e
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            return {"shield": body.shield}

    app.mount("/tiles", StaticFiles(directory=tiles), name="tiles")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(web / "index.html")

    app.mount("/", StaticFiles(directory=web), name="web")
    return app


def region_status(region: str) -> dict:
    with connect(region_dsn(region)) as c:
        tick, at = c.execute("SELECT coalesce(max(id), 0), max(finished_at) FROM ticks").fetchone()
        players = c.execute("SELECT count(*) FROM users WHERE NOT bot AND deleted_at IS NULL").fetchone()[0]
    return {"id": region, "tick": tick, "last_tick_at": at.isoformat() if at else None, "players": players}


def region_ready(region: str) -> bool:
    with connect(region_dsn(region)) as c:
        return is_ready(c)


def create_world_app(regions: list[str]) -> FastAPI:
    """The shared world's API: one database per region (`atlas_realms.regions`), no state of its own.

    `/health` answers `starting` (503) until every region's database has been found migrated and seeded once, so
    Kubernetes routes no traffic to it before the migrate job has run. After that it is 200: `ok`, or `degraded` when
    a region stops answering (`?checks=0` skips that look, for the liveness probe), so a database restart doesn't
    restart the API. The body follows argus_cortex.service.ServiceHealth's fields.
    """
    ready: set[str] = set()
    stop = threading.Event()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def watch() -> None:
        while not stop.is_set() and ready != set(regions):
            for r in set(regions) - ready:
                try:
                    if region_ready(r):
                        ready.add(r)
                        log.info("region_ready", region=r)
                except Exception as e:  # noqa: BLE001 - not reachable yet is the normal state at startup
                    log.info("region_waiting", region=r, reason=type(e).__name__)
            stop.wait(READY_POLL)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        t = threading.Thread(target=watch, name="region-ready", daemon=True)
        t.start()
        yield
        stop.set()

    app = FastAPI(title="Atlas Realms world", lifespan=lifespan)

    def check(region: str) -> dict:
        t0 = time.perf_counter()
        try:
            ok, detail = region_ready(region), None
        except Exception as e:  # noqa: BLE001 - a failing dependency is a result, not an error in this route
            ok, detail = False, type(e).__name__  # the type only: a message can carry a host or a DSN
        return {"ok": ok, "detail": detail, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    # nosemgrep: handrolled-health argus-atlas 0.5 pins argus-cortex<0.15, which has no argus_cortex.service; switch to install_health once argus-atlas 0.6 (cortex 0.16) is released
    @app.get("/health")
    def health(checks: str = "1") -> JSONResponse:
        body = {
            "service_version": "1.1",
            "service": "atlas-realms",
            "version": __version__,
            "started_at": started_at,
            "regions": list(regions),
        }
        liveness = checks.lower() in ("0", "false", "no", "off")
        if ready != set(regions):  # not ready yet, but alive: only readiness waits for the regions
            starting = {**body, "status": "starting", "checks": {}}
            return JSONResponse(starting, status_code=200 if liveness else 503, headers={"Cache-Control": "no-store"})
        results = {} if liveness else {r: check(r) for r in regions}
        status = "degraded" if any(not c["ok"] for c in results.values()) else "ok"
        return JSONResponse({**body, "status": status, "checks": results}, headers={"Cache-Control": "no-store"})

    def known(region: str) -> str:
        if region not in regions:
            raise HTTPException(404, "no such region")
        return region

    @app.get("/api/meta")
    def meta() -> dict:
        return solo_meta()

    @app.get("/api/world")
    def world() -> dict:
        return {"regions": [region_status(r) for r in regions]}

    @app.get("/api/world/{region}")
    def one_region(region: str) -> dict:
        return region_status(known(region))

    return app


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser(description="Atlas Realms API and client")
    p.add_argument(
        "--tiles",
        type=Path,
        default=ROOT / "var" / "tiles-areas" / "cook-strait",
        help="Exported tiles (python -m atlas_realms.tiles)",
    )
    p.add_argument("--world", help="Serve the shared world's regions (comma-separated ids) instead of the solo arena")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8110)
    a = p.parse_args()
    app = create_world_app([r for r in a.world.split(",") if r]) if a.world else create_app(a.tiles)
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
