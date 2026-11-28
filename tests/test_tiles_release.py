"""scripts/tiles_release.py: what goes into the public archive, and what a build refuses."""

import json
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tiles_release as tr  # noqa: E402

AREAS = sorted({Path(r["tiles"]).name for r in tr.regions()})


def _tiles(root: Path) -> Path:
    for area in AREAS:
        for style in ("antique", "secret"):
            f = root / area / style / "8" / "1" / "2.png"
            f.parent.mkdir(parents=True)
            f.write_bytes(f"{area}{style}".encode())
        styles = [{"name": "antique", "patron_only": False}, {"name": "secret", "patron_only": True}]
        (root / area / "labels.geojson").write_text("{}")
        (root / area / "manifest.json").write_text(json.dumps({"styles": styles, "labels_url": "x"}))
    return root


def _packed(tmp_path: Path) -> tuple[Path, dict, Path]:
    tiles, lock = _tiles(tmp_path / "in"), tmp_path / "lock.json"
    record = tr.pack(AREAS, tiles, tmp_path / "out", lock)
    return tmp_path / "out" / record["asset"], record, lock


def test_a_patron_only_style_never_reaches_the_public_archive(tmp_path):
    archive, _, _ = _packed(tmp_path)
    with tarfile.open(archive) as tar:
        assert not [n for n in tar.getnames() if "secret" in n]
        for area in AREAS:
            styles = json.load(tar.extractfile(f"{area}/manifest.json"))["styles"]
            assert [s["name"] for s in styles] == ["antique"]


def test_the_archive_names_no_owner_and_is_reproducible(tmp_path):
    archive, record, _ = _packed(tmp_path)
    again = tr.pack(AREAS, tmp_path / "in", tmp_path / "out2", tmp_path / "lock2.json")
    assert again == record
    with tarfile.open(archive) as tar:
        assert {(m.uname, m.uid, m.mtime) for m in tar.getmembers()} == {("", 0, 0)}


def test_fetch_restores_the_packed_set(tmp_path):
    archive, _, lock = _packed(tmp_path)
    tr.fetch(AREAS, tmp_path / "restored", lock, archive.parent)
    for area in AREAS:
        assert (
            tmp_path / "restored" / area / "antique" / "8" / "1" / "2.png"
        ).read_bytes() == f"{area}antique".encode()


def test_a_changed_archive_is_refused(tmp_path):
    archive, _, lock = _packed(tmp_path)
    data = bytearray(archive.read_bytes())
    data[600] ^= 1
    archive.write_bytes(data)
    with pytest.raises(SystemExit, match="does not match"):
        tr.fetch(AREAS, tmp_path / "restored", lock, archive.parent)
    assert not (tmp_path / "restored").exists()


def test_an_interrupted_download_leaves_nothing_in_the_cache(tmp_path, monkeypatch):
    archive, _, lock = _packed(tmp_path)
    data = archive.read_bytes()
    cache = tmp_path / "cache"

    class Dropped:
        def __init__(self, cut: int | None):
            self.left, self.cut = data, cut

        def read(self, n=-1):
            if self.cut is not None and len(data) - len(self.left) >= self.cut:
                raise ConnectionResetError("dropped")
            n = len(self.left) if n < 0 else min(n, 1024)
            chunk, self.left = self.left[:n], self.left[n:]
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(tr.urllib.request, "urlopen", lambda url, timeout: Dropped(cut=4096))
    with pytest.raises(ConnectionResetError):
        tr.fetch(AREAS, tmp_path / "restored", lock, cache)
    assert list(cache.iterdir()) == []  # no truncated archive for the next run to trust
    monkeypatch.setattr(tr.urllib.request, "urlopen", lambda url, timeout: Dropped(cut=None))
    tr.fetch(AREAS, tmp_path / "restored", lock, cache)
    assert [p.name for p in cache.iterdir()] == [archive.name]


def test_a_missing_area_or_region_is_refused(tmp_path):
    archive, _, lock = _packed(tmp_path)
    with pytest.raises(SystemExit, match="no tiles for: atlantis"):
        tr.fetch([*AREAS, "atlantis"], tmp_path / "restored", lock, archive.parent)
    with pytest.raises(SystemExit, match="arena regions with no tiles"):
        tr.pack(AREAS[:1], tmp_path / "in", tmp_path / "out", tmp_path / "lock.json")


def test_two_sets_are_never_mixed(tmp_path):
    archive, _, lock = _packed(tmp_path)
    (tmp_path / "restored" / AREAS[0]).mkdir(parents=True)
    with pytest.raises(SystemExit, match="already holds"):
        tr.fetch(AREAS, tmp_path / "restored", lock, archive.parent)


def _arena(dist: Path, skip: str = "", placeholder: bool | None = False, extra: str = "") -> Path:
    (dist / "regions").mkdir(parents=True)
    for r in tr.regions():
        styles = json.loads((tr.ROOT / r["manifest"]).read_text())["styles"]
        built = {"styles": styles + ([{"name": extra}] if extra else [])}
        if placeholder is not None:
            built["placeholder"] = placeholder
        (dist / "regions" / f"{r['id']}.json").write_text(json.dumps(built))
        for s in styles:
            f = dist / "tiles" / r["id"] / s["name"] / "8" / "1" / "2.webp"
            f.parent.mkdir(parents=True)
            if s["name"] != skip:
                f.write_bytes(b"x")
    return dist


def test_a_partly_painted_arena_is_refused(tmp_path):
    tr.check_arena(_arena(tmp_path / "full"))
    with pytest.raises(SystemExit, match="antique"):
        tr.check_arena(_arena(tmp_path / "partial", skip="antique"))
    with pytest.raises(SystemExit, match="all tiles"):
        tr.check_arena(_arena(tmp_path / "placeholder", placeholder=True))


@pytest.mark.parametrize("placeholder", [False, None])
def test_a_listed_but_unpainted_style_is_refused_with_or_without_the_placeholder_key(tmp_path, placeholder):
    with pytest.raises(SystemExit, match="listed but not painted: unpainted"):
        tr.check_arena(_arena(tmp_path / "arena", placeholder=placeholder, extra="unpainted"))


def test_a_tile_set_tag_is_never_the_version(tmp_path):
    raw = tomllib.loads((tr.ROOT / "pyproject.toml").read_text())["tool"]["hatch"]["version"]["raw-options"]

    def git(*args: str) -> str:
        who = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
        done = subprocess.run(["git", *who, *args], cwd=tmp_path, check=True, capture_output=True, text=True)
        return done.stdout

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "release")
    git("tag", "v0.1.0")
    git("commit", "-q", "--allow-empty", "-m", "tiles")
    git("tag", "tiles-92459a0f79b0")
    assert git(*raw["git_describe_command"][1:]).startswith("v0.1.0-1-g")
