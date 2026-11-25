"""The painted tiles as a GitHub release asset, so CI can build both sites without the bakes.

    python scripts/tiles_release.py pack --areas "iceland reunion cook-strait"   # var/tiles-areas -> dist/<asset>.tar + lock
    python scripts/tiles_release.py fetch --areas "iceland reunion cook-strait"  # lock -> var/tiles-areas
    python scripts/tiles_release.py check-arena                                  # arena/dist is painted, every region

`data/tiles.lock.json` names the asset and its sha256, so a commit builds one known set. Every set has its own tag, so a
published asset is never replaced. The release is public: a patron-only style is left out of the archive.
"""

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import urllib.request
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TILES = ROOT / "var" / "tiles-areas"
LOCK = ROOT / "data" / "tiles.lock.json"
RELEASES = "https://github.com/smk762/argus-realms/releases/download"


def _sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def regions() -> list[dict]:
    return json.loads((ROOT / "arena" / "regions.json").read_text())["regions"]


def require_regions(areas: list[str]) -> None:
    """Every arena region paints from one of the packed areas, or the arena would ship a placeholder."""
    missing = [r["id"] for r in regions() if Path(r["tiles"]).name not in areas]
    if missing:
        sys.exit(f"arena regions with no tiles in this set: {', '.join(missing)}")


def pack(areas: list[str], tiles: Path, out: Path, lock: Path) -> dict:
    require_regions(areas)
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "tiles.tar.part"
    with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as tar:

        def add(name: str, data: bytes) -> None:  # no owner, no mtime: the same tiles make the same archive
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, BytesIO(data))

        for area in sorted(areas):
            src = tiles / area
            if not (src / "manifest.json").exists():
                sys.exit(f"{src}/manifest.json not found: export the area's tiles first (make tiles)")
            manifest = json.loads((src / "manifest.json").read_text())
            manifest["styles"] = [s for s in manifest["styles"] if not s.get("patron_only")]
            if not manifest["styles"]:
                sys.exit(f"{area}: no public styles to pack")
            add(f"{area}/manifest.json", json.dumps(manifest, indent=1).encode())
            if manifest.get("labels_url"):
                add(f"{area}/labels.geojson", (src / "labels.geojson").read_bytes())
            for style in manifest["styles"]:
                pngs = sorted((src / style["name"]).rglob("*.png"))
                if not pngs:
                    sys.exit(f"{area}/{style['name']}: no tiles")
                for p in pngs:
                    add(p.relative_to(tiles).as_posix(), p.read_bytes())
    sha = _sha256(tmp)
    asset = f"tiles-{sha[:12]}.tar"
    tmp.replace(out / asset)
    record = {
        "tag": f"tiles-{sha[:12]}",
        "asset": asset,
        "sha256": sha,
        "bytes": (out / asset).stat().st_size,
        "areas": sorted(areas),
    }
    lock.write_text(json.dumps(record, indent=1) + "\n")
    return record


def unpack(archive: Path, record: dict, tiles: Path) -> None:
    if archive.stat().st_size != record["bytes"] or _sha256(archive) != record["sha256"]:
        sys.exit(f"{archive.name} does not match {LOCK.name}")
    tiles.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        tar.extractall(tiles, filter="data")


def fetch(areas: list[str], tiles: Path, lock: Path, cache: Path) -> None:
    record = json.loads(lock.read_text())
    missing = sorted(set(areas) - set(record["areas"]))
    if missing:
        sys.exit(f"{lock.name} has no tiles for: {', '.join(missing)}")
    require_regions(record["areas"])
    there = [a for a in record["areas"] if (tiles / a).exists()]
    if there:  # never mix two sets
        sys.exit(f"{tiles} already holds {', '.join(there)}: move it aside first")
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / record["asset"]
    if not archive.exists():
        url = f"{RELEASES}/{record['tag']}/{record['asset']}"
        with urllib.request.urlopen(url, timeout=60) as res, archive.open("wb") as f:
            shutil.copyfileobj(res, f)
    unpack(archive, record, tiles)


def check_arena(dist: Path) -> None:
    """The arena build falls back to a placeholder basemap where tiles are missing; a deploy must not."""
    problems = []
    for r in regions():
        built = json.loads((dist / "regions" / f"{r['id']}.json").read_text())
        want = {s["name"] for s in json.loads((ROOT / r["manifest"]).read_text())["styles"] if not s.get("patron_only")}
        have = {s["name"] for s in built["styles"]}
        painted = {s for s in have if any((dist / "tiles" / r["id"] / s).rglob("*.webp"))}
        if built.get("placeholder") or want - painted:
            problems.append(f"{r['id']}: missing {', '.join(sorted(want - painted)) or 'all tiles'}")
        if built.get("placeholder") is False and have - painted:
            problems.append(f"{r['id']}: listed but not painted: {', '.join(sorted(have - painted))}")
    if problems:
        sys.exit("arena is only partly painted:\n" + "\n".join(problems))
    print(f"arena: {len(regions())} regions, every style painted")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["pack", "fetch", "check-arena"])
    p.add_argument("--areas", default="", help="Space-separated area ids (the Makefile's AREAS)")
    p.add_argument("--out", type=Path, default=ROOT / "dist", help="Where pack writes the archive")
    p.add_argument("--cache", type=Path, default=ROOT / "build" / "tiles", help="Where fetch keeps the download")
    a = p.parse_args()
    areas = a.areas.split()
    if a.command == "check-arena":
        return check_arena(ROOT / "arena" / "dist")
    if not areas:
        sys.exit("--areas is empty")
    if a.command == "fetch":
        return fetch(areas, TILES, LOCK, a.cache)
    r = pack(areas, TILES, a.out, LOCK)
    print(f"{a.out / r['asset']} ({r['bytes'] / 1e6:.0f} MB), {LOCK.relative_to(ROOT)} written")


if __name__ == "__main__":
    main()
