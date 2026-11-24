"""Export baked chunks as static tiles for the client.

    python -m atlas_realms.tiles var/maps/areas/cook-strait var/tiles-areas/cook-strait   # or `make tiles`: every area

Reads `<maps>/<style>/<job>/fantasy.pmtiles` (one archive per chunk, as argus-atlas writes them) and
writes `<out>/<style>/<z>/<x>/<y>.png` plus `<out>/manifest.json`. A new chunk is only new files,
so the output can sit behind a CDN as is; a tile already exported is rewritten when a re-bake changed
it, and one the bake no longer has is removed. argus-atlas cuts neighbouring archives without overlap; a tile that two archives in this run
disagree on is an error. Place names (`labels.geojson`, written by
`argus-atlas labels --job` for one style's chunks) are merged into `<out>/labels.geojson`.

Each style's manifest entry also says how it was painted (`bake`: recipe, preset, checkpoint, denoise, seeds, the
argus-atlas commands that made it and the argus-atlas builds that ran them), read from the chunks' `job.json`, so the
demo's "How it was made" never hard-codes what a bake did. `atlas` lists every argus-atlas build across the styles;
a bake from before argus-atlas 0.4 recorded none and shows as `{"version": null, "commit": null}`.

Below the chunks' own zoom, overview tiles down to `overview_min` are built by shrinking each 2 x 2
block of the zoom above into one tile, so the painted map still shows when zoomed out. (A general
mosaic feature: a candidate for argus-atlas once more than this game needs it.)
"""

import argparse
import io
import json
import math
import sys
import time
from pathlib import Path

from argus_atlas.addressing import chunk_bbox
from PIL import Image
from pmtiles.reader import MmapSource, Reader, all_tiles

TILE_PX = 256

Tile = tuple[int, int, int]


def _sidecar(job_dir: Path) -> dict | None:
    try:
        job = json.loads((job_dir / "job.json").read_text())
    except (OSError, ValueError):
        return None
    return job if isinstance(job, dict) else None


def _chunk_of(job: dict | None) -> Tile | None:
    c = (job or {}).get("chunk")
    return (c["z"], c["x"], c["y"]) if c else None


def _distinct(values) -> list:
    return sorted({v for v in values if v is not None}, key=str)


def bake_summary(jobs: list[dict]) -> dict:
    """How one style's chunks were painted, from their `job.json`: every distinct value, so a style baked in two
    sittings with different knobs says so instead of showing one of them."""
    baked = [(j, j["bake"]) for j in jobs if j.get("bake")]
    bakes = [b for _, b in baked]
    params = [b.get("params") or {} for b in bakes]
    seeds = [p["seed"] for p in params if isinstance(p.get("seed"), int)]
    builds = {
        (g.get("version"), g.get("commit"))
        for g in ((b.get("generator") or j.get("generator") or {}) for j, b in baked)
    }
    keys = {src.get("key") for j in jobs for src in (j.get("sources") or [])}
    commands = [c for c, used in (("dem", "copernicus-glo30" in keys), ("osm", "openstreetmap" in keys)) if used]
    commands += ["chunk"] + (["labels"] if any(j.get("labels") for j in jobs) else [])
    chunk = [j["chunk"] for j in jobs if j.get("chunk")]
    return {
        "recipe": _distinct(p.get("recipe") for p in params),
        "preset": _distinct(p.get("preset") for p in params),
        "checkpoint": _distinct((b.get("checkpoint") or {}).get("name") for b in bakes),
        "lora": _distinct((b.get("lora") or {}).get("name") for b in bakes),
        "denoise": _distinct(p.get("denoise") for p in params),
        "steps": _distinct(p.get("steps") for p in params),
        "cfg": _distinct(p.get("cfg") for p in params),
        "seeds": [min(seeds), max(seeds)] if seeds else None,
        "chunks": len(bakes),
        "chunk": {
            "zoom": _distinct(c.get("z") for c in chunk),
            "size": _distinct(c.get("size") for c in chunk),
            "margin": _distinct(c.get("margin") for c in chunk),
        },
        "commands": commands,
        "aligned": all(b.get("aligned") for b in bakes) if bakes else None,
        "atlas": [{"version": v, "commit": c} for v, c in sorted(builds, key=str)],
    }


def _prune(style_out: Path, keep: set[Tile]) -> None:
    """Remove the z/x/y tiles an earlier export left that aren't in `keep`, old overviews included (they are rebuilt)."""
    for p in style_out.glob("*/*/*.png"):
        parts = (p.parent.parent.name, p.parent.name, p.stem)
        if not all(v.isdecimal() for v in parts) or tuple(map(int, parts)) in keep:
            continue
        p.unlink()
        for d in (p.parent, p.parent.parent):
            if not any(d.iterdir()):
                d.rmdir()


def export_style(style_dir: Path, out: Path) -> tuple[set[Tile], dict]:
    """Write one style's tiles. Returns the chunks exported and the zoom range, bounds and bake summary seen."""
    chunks: set[Tile] = set()
    zooms: set[int] = set()
    bounds = [180.0, 90.0, -180.0, -90.0]
    jobs: list[dict] = []
    # Tiles this run has already written. A file left by an earlier export is replaced; a second
    # archive in this run that paints the same tile differently is the seam error.
    written: set[tuple[int, int, int]] = set()
    for archive in sorted(style_dir.glob("*/fantasy.pmtiles")):
        job = _sidecar(archive.parent)
        chunk = _chunk_of(job)
        if chunk is None:
            continue
        jobs.append(job)
        with archive.open("rb") as fh:
            source = MmapSource(fh)
            h = Reader(source).header()
            for (z, x, y), data in all_tiles(source):
                dst = out / style_dir.name / str(z) / str(x) / f"{y}.png"
                if (z, x, y) in written and dst.read_bytes() != data:
                    raise ValueError(
                        f"{style_dir.name} {z}/{x}/{y}: {archive.parent.name} disagrees with an earlier chunk"
                    )
                if not dst.exists() or dst.read_bytes() != data:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(data)
                written.add((z, x, y))
                zooms.add(z)
        bounds = [
            min(bounds[0], h["min_lon_e7"] / 1e7),
            min(bounds[1], h["min_lat_e7"] / 1e7),
            max(bounds[2], h["max_lon_e7"] / 1e7),
            max(bounds[3], h["max_lat_e7"] / 1e7),
        ]
        chunks.add(chunk)
    if chunks:  # a style dir with no bake leaves the earlier export alone
        _prune(out / style_dir.name, written)
    meta = {"minzoom": min(zooms, default=0), "maxzoom": max(zooms, default=0), "bounds": bounds}
    return chunks, {**meta, "bake": bake_summary(jobs)}


def export_labels(maps: Path, out: Path) -> int:
    """Merge every chunk's place names into `out/labels.geojson`. Returns the names written (0: no file).

    argus-atlas labels each chunk's own tile, so chunks never repeat a name; a name labelled on
    several styles' copies of a chunk is kept once.
    """
    seen, features = set(), []
    for path in sorted(maps.glob("*/*/labels.geojson")):
        for f in json.loads(path.read_text())["features"]:
            key = (f["properties"]["name"], tuple(f["geometry"]["coordinates"]))
            if key not in seen:
                seen.add(key)
                features.append(f)
    if features:
        out.mkdir(parents=True, exist_ok=True)
        (out / "labels.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    else:
        (out / "labels.geojson").unlink(missing_ok=True)  # an earlier export's names would no longer match the tiles
    return len(features)


def build_overviews(style_out: Path, from_zoom: int, overview_min: int) -> int:
    """Shrink zoom z+1 into zoom z for z = from_zoom-1 .. overview_min. Returns tiles written."""
    written = 0
    for z in range(from_zoom - 1, overview_min - 1, -1):
        children = sorted(style_out.glob(f"{z + 1}/*/*.png"))
        parents = {(int(c.parent.name) // 2, int(c.stem) // 2) for c in children}
        for px, py in sorted(parents):
            canvas = Image.new("RGBA", (2 * TILE_PX, 2 * TILE_PX), (0, 0, 0, 0))
            for dx in (0, 1):
                for dy in (0, 1):
                    child = style_out / str(z + 1) / str(2 * px + dx) / f"{2 * py + dy}.png"
                    if child.exists():
                        with Image.open(child) as im:
                            canvas.paste(im.convert("RGBA"), (dx * TILE_PX, dy * TILE_PX))
            dst = style_out / str(z) / str(px) / f"{py}.png"
            dst.parent.mkdir(parents=True, exist_ok=True)
            buf = io.BytesIO()
            canvas.resize((TILE_PX, TILE_PX), Image.LANCZOS).save(buf, "PNG", optimize=True)
            dst.write_bytes(buf.getvalue())
            written += 1
    return written


def chunks_bounds(chunks) -> list[float]:
    """[west, south, east, north] of the chunks' own tile edges, rounded inward to 1e-7 degrees. The PMTiles headers
    store bounds in 1e-7 steps rounded outward, and a raster source given an east edge of -11.2499999 asks for a whole
    tile column past the painting (404s at Iceland's edge)."""
    boxes = [chunk_bbox(*c) for c in chunks]
    w, s = min(b.west for b in boxes), min(b.south for b in boxes)
    e, n = max(b.east for b in boxes), max(b.north for b in boxes)
    return [math.ceil(w * 1e7) / 1e7, math.ceil(s * 1e7) / 1e7, math.floor(e * 1e7) / 1e7, math.floor(n * 1e7) / 1e7]


def export(maps: Path, out: Path, patron_only: set[str] = frozenset(), overview_min: int = 5) -> dict:
    """Export every style under `maps` and write the manifest. A chunk is ready when every style has it."""
    styles, per_style, info = [], {}, []
    for style_dir in sorted(p for p in maps.iterdir() if p.is_dir()):
        chunks, meta = export_style(style_dir, out)
        if chunks and overview_min < meta["minzoom"]:
            build_overviews(out / style_dir.name, meta["minzoom"], overview_min)
            meta["chunk_zoom"], meta["minzoom"] = meta["minzoom"], overview_min
        if chunks:
            styles.append(style_dir.name)
            per_style[style_dir.name] = chunks
            info.append(meta)
    ready = set.intersection(*per_style.values()) if per_style else set()
    labels = export_labels(maps, out)
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "styles": [
            {"name": s, "patron_only": s in patron_only, "chunks": len(per_style[s]), "bake": m["bake"]}
            for s, m in zip(styles, info, strict=True)
        ],
        # Every argus-atlas build behind these tiles; the static build checks it against the imported docs.
        "atlas": [
            json.loads(b) for b in sorted({json.dumps(b, sort_keys=True) for m in info for b in m["bake"]["atlas"]})
        ],
        "ready_chunks": sorted(ready),
        "minzoom": min((m["minzoom"] for m in info), default=0),
        "chunk_zoom": min((m.get("chunk_zoom", m["minzoom"]) for m in info), default=0),
        "maxzoom": max((m["maxzoom"] for m in info), default=0),
        "bounds": chunks_bounds(set().union(*per_style.values())) if per_style else [0, 0, 0, 0],
        "tile_url": "tiles/{style}/{z}/{x}/{y}.png",
        "labels_url": "tiles/labels.geojson" if labels else None,
        "attribution": "Map painted by argus-atlas from Copernicus GLO-30 and © OpenStreetMap contributors",
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("maps", type=Path, help="Root holding <style>/<job>/fantasy.pmtiles")
    p.add_argument("out", type=Path, help="Where to write <style>/<z>/<x>/<y>.png and manifest.json")
    p.add_argument("--patron-only", default="", help="Comma-separated styles only subscribers see")
    p.add_argument("--overview-min", type=int, default=5, help="Lowest zoom of the shrunk overview tiles")
    a = p.parse_args()
    m = export(a.maps, a.out, {s for s in a.patron_only.split(",") if s}, a.overview_min)
    n = len(m["ready_chunks"])
    print(
        f"{len(m['styles'])} styles, {n} chunks ready in all of them, zoom {m['minzoom']}-{m['maxzoom']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
