"""HTTP API and static files for the browser client.

    python -m atlas_realms.api --tiles var/tiles --port 8110      # then open http://127.0.0.1:8110

Solo arena only for now: sessions live in memory and are lost on restart. PvP (accounts, the shared
Postgres world) adds its own routes next to these without changing the client's state shape.
"""

import argparse
import json
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from atlas_realms.protocol import SPAWNS, Control, Looks, NewSolo, OrderIn, check_new
from atlas_realms.protocol import meta as solo_meta
from atlas_realms.solo import Session, build_world

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
OUTLINE = ROOT / "data" / "nz_mainland.geojson"
MAX_SESSIONS = 50


def create_app(tiles: Path, web: Path = WEB, outline: Path = OUTLINE) -> FastAPI:
    app = FastAPI(title="Atlas Realms")
    manifest = json.loads((tiles / "manifest.json").read_text())
    sessions: dict[str, Session] = {}

    def get(sid: str) -> Session:
        s = sessions.get(sid)
        if s is None:
            raise HTTPException(404, "no such session")
        return s

    @app.get("/api/manifest")
    def get_manifest() -> dict:
        return manifest

    @app.get("/api/meta")
    def meta() -> dict:
        return solo_meta()

    @app.post("/api/solo")
    def new_solo(body: NewSolo) -> dict:
        try:
            check_new(body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if len(sessions) >= MAX_SESSIONS:
            sessions.pop(next(iter(sessions)))  # oldest first
        world = build_world(manifest, outline, SPAWNS)
        s = Session.new(
            world, body.name, body.unit_class, tuple(body.bots), body.subscriber, tick_every=body.tick_every
        )
        sid = secrets.token_urlsafe(9)
        sessions[sid] = s
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


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser(description="Atlas Realms API and client")
    p.add_argument(
        "--tiles", type=Path, default=ROOT / "var" / "tiles", help="Exported tiles (python -m atlas_realms.tiles)"
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8110)
    a = p.parse_args()
    uvicorn.run(create_app(a.tiles), host=a.host, port=a.port)


if __name__ == "__main__":
    main()
