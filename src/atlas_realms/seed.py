"""Seed the world from an exported tile manifest and a land outline.

    python -m atlas_realms.seed var/tiles/manifest.json data/nz_mainland.geojson \
        --spawn=-41.2865,174.7762 --spawn=-41.2906,174.0010

Every hex touching a ready chunk is created. It is playable once all the chunks its outline touches
are ready; the rest start under fog. A hex is land when its centre is in the outline, else water.
Spawn points (lat,lng) turn the land hex under them into a spawn zone, or the nearest painted land hex
when the one under them is water (a res-5 hex is ~17 km across, so a coastal city often sits on one). Re-running updates zones and
fog but never ownership. Value tiers, terrain and corridors come from the offline scoring pass (later):
every land hex is tier 1, flat, for now.
"""

import argparse
import json
from pathlib import Path

import h3
import psycopg
from argus_atlas.chunks import chunk_bbox
from psycopg.types.json import Jsonb

from atlas_realms.db import connect, migrate
from atlas_realms.rules.chunks import Tile, cell_chunks
from atlas_realms.rules.tunables import DEFAULT, Tunables


def land_cells(outline: Path, res: int) -> set[str]:
    cells: set[str] = set()
    for f in json.loads(outline.read_text())["features"]:
        cells |= set(h3.geo_to_cells(f["geometry"], res))
    return cells


def plan_hexes(
    ready: set[Tile], land: set[str], spawns: list[tuple[float, float]], t: Tunables = DEFAULT
) -> list[dict]:
    res, z = t.h3_resolution, t.chunk_zoom
    cells: set[str] = set()
    for tile in ready:
        b = chunk_bbox(*tile)
        ring = [(b.south, b.west), (b.south, b.east), (b.north, b.east), (b.north, b.west)]
        cells |= set(h3.polygon_to_cells(h3.LatLngPoly(ring), res))
    cells |= {n for c in cells for n in h3.grid_ring(c, 1)}  # hexes whose centre is just outside
    spawn_cells = {_nearest_land(lat, lng, res, land & cells) for lat, lng in spawns}
    rows = []
    for c in sorted(cells):
        chunks = cell_chunks(c, z)
        if not any(ch in ready for ch in chunks):
            continue
        is_land = c in land
        zone = "spawn" if c in spawn_cells and is_land else "land" if is_land else "water"
        rows.append(
            {
                "h3": c,
                "zone": zone,
                "terrain": "flat" if is_land else "water",
                "tier": 1 if is_land else None,
                "playable": all(ch in ready for ch in chunks),
                "chunk": h3_home(c, z),
            }
        )
    missing = spawn_cells - {r["h3"] for r in rows if r["zone"] == "spawn"}
    if missing:
        raise ValueError(f"spawn points not on painted land: {sorted(missing)}")
    return rows


def _nearest_land(lat: float, lng: float, res: int, land: set[str]) -> str:
    here = h3.latlng_to_cell(lat, lng, res)
    if here in land:
        return here
    near = [c for c in h3.grid_disk(here, 2) if c in land]
    if not near:
        return here  # reported as not on painted land below
    return min(near, key=lambda c: (h3.great_circle_distance((lat, lng), h3.cell_to_latlng(c)), c))


def h3_home(cell: str, z: int) -> Tile:
    from atlas_realms.rules.chunks import home_chunk

    return home_chunk(cell, z)


def seed(conn: psycopg.Connection, manifest: dict, land: set[str], spawns: list[tuple[float, float]]) -> int:
    ready = {tuple(c) for c in manifest["ready_chunks"]}
    styles = [s["name"] for s in manifest["styles"]]
    rows = plan_hexes(ready, land, spawns)
    with conn.transaction():
        for s in manifest["styles"]:
            conn.execute(
                "INSERT INTO styles (name, patron_only) VALUES (%s, %s) "
                "ON CONFLICT (name) DO UPDATE SET patron_only = excluded.patron_only",
                (s["name"], s["patron_only"]),
            )
        status = Jsonb({s: "ready" for s in styles})
        for z, x, y in sorted(ready | {r["chunk"] for r in rows}):
            ok = (z, x, y) in ready
            conn.execute(
                "INSERT INTO chunks (z, x, y, styles, ready) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (z, x, y) DO UPDATE SET styles = excluded.styles, ready = excluded.ready",
                (z, x, y, status if ok else Jsonb({}), ok),
            )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO hexes (h3, zone, terrain, tier, playable, chunk_z, chunk_x, chunk_y) "
                "VALUES (%s::h3index, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (h3) DO UPDATE SET "
                "zone = excluded.zone, terrain = excluded.terrain, tier = excluded.tier, playable = excluded.playable "
                "WHERE hexes.owner_id IS NULL",
                [(r["h3"], r["zone"], r["terrain"], r["tier"], r["playable"], *r["chunk"]) for r in rows],
            )
    return len(rows)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("manifest", type=Path)
    p.add_argument("outline", type=Path)
    p.add_argument("--spawn", action="append", default=[], help="lat,lng of a spawn zone (repeatable)")
    a = p.parse_args()
    spawns = [tuple(float(v) for v in s.split(",")) for s in a.spawn]
    manifest = json.loads(a.manifest.read_text())
    with connect() as conn:
        migrate(conn)
        n = seed(conn, manifest, land_cells(a.outline, DEFAULT.h3_resolution), spawns)
        counts = dict(
            conn.execute(
                "SELECT zone::text || CASE WHEN playable THEN '' ELSE ' (fog)' END, count(*) FROM hexes GROUP BY 1 ORDER BY 1"
            ).fetchall()
        )
    print(f"{n} hexes seeded: {counts}")


if __name__ == "__main__":
    main()
