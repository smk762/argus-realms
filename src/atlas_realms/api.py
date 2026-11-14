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
from pydantic import BaseModel, Field

from atlas_realms.bots import PERSONALITIES
from atlas_realms.rules.classes import CLASSES
from atlas_realms.rules.tunables import DEFAULT
from atlas_realms.solo import DEFAULT_ROSTER, SHIELD_LOOKS, Session, build_world

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
OUTLINE = ROOT / "data" / "nz_mainland.geojson"
SPAWNS = [(-41.2865, 174.7762), (-41.2906, 174.0010)]  # Wellington side, Picton side
MAX_SESSIONS = 50


class NewSolo(BaseModel):
    name: str = Field("You", min_length=1, max_length=24)
    unit_class: str = "barbarian"
    bots: list[str] = list(DEFAULT_ROSTER)
    tick_every: float = Field(2.0, ge=0.25, le=60)
    subscriber: bool = False


class Looks(BaseModel):
    shield: str


class OrderIn(BaseModel):
    kind: str
    payload: dict = {}


class Control(BaseModel):
    paused: bool | None = None
    tick_every: float | None = Field(None, ge=0.25, le=60)


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
        return {
            "classes": {k: v.model_dump() for k, v in CLASSES.items()},
            "personalities": {k: {"class": p.unit_class, "blurb": p.blurb} for k, p in PERSONALITIES.items()},
            "shield_looks": SHIELD_LOOKS,
            "shield_daily_hours": DEFAULT.shield_daily_hours,
            "shield_cooldown_minutes": DEFAULT.shield_cooldown_minutes,
        }

    @app.post("/api/solo")
    def new_solo(body: NewSolo) -> dict:
        if body.unit_class not in CLASSES:
            raise HTTPException(422, "unknown class")
        if any(b not in PERSONALITIES for b in body.bots):
            raise HTTPException(422, "unknown bot personality")
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
