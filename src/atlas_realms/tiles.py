"""Export baked chunks as static tiles for the client.

    python -m atlas_realms.tiles var/maps var/tiles

Reads `<maps>/<style>/<job>/fantasy.pmtiles` (one archive per chunk, as argus-atlas writes them) and
writes `<out>/<style>/<z>/<x>/<y>.png` plus `<out>/manifest.json`. A new chunk is only new files,
so the output can sit behind a CDN as is. argus-atlas cuts neighbouring archives without overlap;
a tile that two archives disagree on is an error. Place names (`labels.geojson`, written by
`argus-atlas labels --job` for one style's chunks) are merged into `<out>/labels.geojson`.

Below the chunks' own zoom, overview tiles down to `overview_min` are built by shrinking each 2 x 2
block of the zoom above into one tile, so the painted map still shows when zoomed out. (A general
mosaic feature: a candidate for argus-atlas once more than this game needs it.)
"""

import argparse
import io
import json
import sys
import time
from pathlib import Path

from PIL import Image
from pmtiles.reader import MmapSource, Reader, all_tiles

TILE_PX = 256

Tile = tuple[int, int, int]


def _chunk_of(job_dir: Path) -> Tile | None:
    try:
        c = json.loads((job_dir / "job.json").read_text()).get("chunk")
    except (OSError, ValueError):
        return None
    return (c["z"], c["x"], c["y"]) if c else None


def export_style(style_dir: Path, out: Path) -> tuple[set[Tile], dict]:
    """Write one style's tiles. Returns the chunks exported and the zoom range and bounds seen."""
    chunks: set[Tile] = set()
    zooms: set[int] = set()
    bounds = [180.0, 90.0, -180.0, -90.0]
    for archive in sorted(style_dir.glob("*/fantasy.pmtiles")):
        chunk = _chunk_of(archive.parent)
        if chunk is None:
            continue
        with archive.open("rb") as fh:
            source = MmapSource(fh)
            h = Reader(source).header()
            for (z, x, y), data in all_tiles(source):
                dst = out / style_dir.name / str(z) / str(x) / f"{y}.png"
                if dst.exists() and dst.read_bytes() != data:
                    raise ValueError(
                        f"{style_dir.name} {z}/{x}/{y}: {archive.parent.name} disagrees with an earlier chunk"
                    )
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data)
                zooms.add(z)
        bounds = [
            min(bounds[0], h["min_lon_e7"] / 1e7),
            min(bounds[1], h["min_lat_e7"] / 1e7),
            max(bounds[2], h["max_lon_e7"] / 1e7),
            max(bounds[3], h["max_lat_e7"] / 1e7),
        ]
        chunks.add(chunk)
    return chunks, {"minzoom": min(zooms, default=0), "maxzoom": max(zooms, default=0), "bounds": bounds}


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
        "styles": [{"name": s, "patron_only": s in patron_only, "chunks": len(per_style[s])} for s in styles],
        "ready_chunks": sorted(ready),
        "minzoom": min((m["minzoom"] for m in info), default=0),
        "chunk_zoom": min((m.get("chunk_zoom", m["minzoom"]) for m in info), default=0),
        "maxzoom": max((m["maxzoom"] for m in info), default=0),
        "bounds": [
            min((m["bounds"][0] for m in info), default=0),
            min((m["bounds"][1] for m in info), default=0),
            max((m["bounds"][2] for m in info), default=0),
            max((m["bounds"][3] for m in info), default=0),
        ],
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
