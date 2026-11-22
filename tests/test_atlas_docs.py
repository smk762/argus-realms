"""Atlas's argus-atlas docs (argus-atlas#34): import at one commit, check against the record, render, and refuse tiles
baked at another commit."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import atlas_docs as ad  # noqa: E402
import build_static as bs  # noqa: E402

from atlas_realms.tiles import bake_summary  # noqa: E402

COMMIT = "a" * 40

README = """<h1>
  <picture><img alt="argus-atlas" src="https://raw.githubusercontent.com/smk762/argus-cortex/dev/logo.svg"></picture>
</h1>

[![PyPI](https://img.shields.io/pypi/v/argus-atlas.svg)](https://pypi.org/project/argus-atlas/)

Bake one map.

![Hero](https://raw.githubusercontent.com/smk762/argus-atlas/main/docs/img/hero.jpg)
![Elsewhere](https://example.org/x.png)

See [the gate](https://github.com/smk762/argus-atlas/blob/main/docs/alignment-gate.md#when-it-fails),
[chunks](docs/chunks-and-mosaic.md), [the first bake](docs/your-first-bake.md), [the tenant](gpu-tenant.yaml),
[Realms](https://realms.dragonhound.dev) and [OSM](https://www.openstreetmap.org/copyright). <script>x()</script>

## Install

## Install
"""
DOCS = {
    "README.md": README,
    "docs/chunks-and-mosaic.md": "# Chunks\n\n## One chunk\n\n![seam](img/seam.png)\n\n## Place names\n\n[up](#one-chunk)\n",
    "docs/alignment-gate.md": "# The alignment gate\n\n## When it fails\n",
    "docs/cli.md": "# CLI\n\n## `argus-atlas dem`\n\n## `argus-atlas osm`\n",
    "docs/your-first-bake.md": "# First bake on a host\n",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def atlas(tmp_path: Path) -> Path:
    """A tiny argus-atlas repository: the four docs, two images, and the runbook that is never imported."""
    repo = tmp_path / "argus-atlas"
    for path, text in DOCS.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    (repo / "docs/img").mkdir()
    (repo / "docs/img/hero.jpg").write_bytes(b"jpeg")
    (repo / "docs/img/seam.png").write_bytes(b"png")
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "docs")
    return repo


@pytest.fixture
def vendor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    out = tmp_path / "vendor"
    monkeypatch.setattr(ad, "VENDOR", out)
    monkeypatch.setattr(ad, "RECORD", out / "sync.json")
    monkeypatch.setattr(ad, "atlas_version", lambda repo, commit: "0.3.1.dev41+gabc1234")  # offline
    monkeypatch.delenv("OPSEC_DENYLIST", raising=False)
    return out


# --- import and record ----------------------------------------------------------------------------------------


def test_import_copies_the_docs_and_their_images_at_one_commit(atlas: Path, vendor: Path) -> None:
    record = ad.import_docs(atlas, "HEAD")
    commit = _git(atlas, "rev-parse", "HEAD")
    assert record["atlas"] == {"repo": ad.REPO, "commit": commit, "version": "0.3.1.dev41+gabc1234", "ref": "HEAD"}
    assert set(record["files"]) == {d.path for d in ad.DOCS} | {"docs/img/hero.jpg", "docs/img/seam.png"}
    assert "docs/your-first-bake.md" not in record["files"] and not (vendor / "your-first-bake.md").exists()
    assert (vendor / "overview.md").read_text() == README  # copied unchanged: the record hashes upstream bytes
    assert ad.load_record(vendor) == record

    (atlas / "README.md").write_text("changed in the working tree, not committed")
    assert ad.import_docs(atlas, commit)["files"]["README.md"] == record["files"]["README.md"]  # git show, not the tree


def test_import_refuses_a_doc_that_fails_the_opsec_gate(atlas: Path, vendor: Path, monkeypatch) -> None:
    (atlas / "docs/cli.md").write_text("# CLI\n\nComfyUI on http://192.168.200.20:8188\n")
    _git(atlas, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "leak")
    with pytest.raises(
        ad.DocsError, match=r"docs/cli.md at \w{7} fails the opsec gate: .*private IPv4: '192.168.200.20'"
    ):
        ad.import_docs(atlas, "HEAD")
    assert not vendor.exists()  # nothing written

    _git(atlas, "-c", "user.name=t", "-c", "user.email=t@example.com", "revert", "--no-edit", "HEAD")
    monkeypatch.setenv("OPSEC_DENYLIST", "Bake one map")
    with pytest.raises(ad.DocsError, match="an OPSEC_DENYLIST term"):
        ad.import_docs(atlas, "HEAD")


def test_the_record_catches_an_edited_extra_or_forbidden_file(atlas: Path, vendor: Path) -> None:
    record = ad.import_docs(atlas, "HEAD")
    (vendor / "cli.md").write_text("edited by hand")
    with pytest.raises(ad.DocsError, match="cli.md differs from its record"):
        ad.load_record(vendor)
    ad.import_docs(atlas, "HEAD")
    (vendor / "your-first-bake.md").write_text("# no")
    with pytest.raises(ad.DocsError, match="doesn't name: your-first-bake.md"):
        ad.load_record(vendor)
    (vendor / "your-first-bake.md").unlink()
    record["files"]["docs/your-first-bake.md"] = {"as": "cli.md", "sha256": record["files"]["docs/cli.md"]["sha256"]}
    (vendor / "sync.json").write_text(json.dumps(record))
    with pytest.raises(ad.DocsError, match="never imported"):
        ad.load_record(vendor)


# --- render ---------------------------------------------------------------------------------------------------


def test_render_rewrites_links_images_and_headings(atlas: Path, vendor: Path) -> None:
    record = ad.import_docs(atlas, "HEAD")
    commit = record["atlas"]["commit"]
    r = ad.render(record, vendor)
    html = r.html
    assert "picture" not in html and "shields.io" not in html and "example.org" not in html
    assert "<script>" not in html and "&lt;script&gt;" in html  # raw HTML is never passed through
    assert 'src="/docs/img/hero.jpg"' in html and 'src="/docs/img/seam.png"' in html
    assert 'href="#alignment-gate-when-it-fails"' in html and 'href="#chunks-and-mosaic"' in html
    assert 'href="#chunks-and-mosaic-one-chunk"' in html  # an in-doc anchor gets its doc's prefix
    first_bake = f'href="https://github.com/smk762/argus-atlas/blob/{commit}/docs/your-first-bake.md"'
    assert first_bake in html and f"blob/{commit}/gpu-tenant.yaml" in html  # pinned, never "main"
    assert '<a href="https://realms.dragonhound.dev">Realms</a>' in html  # the family opens in this tab
    osm = '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OSM'
    assert osm + ad.NEW_TAB + "</a>" in html
    assert 'id="overview-install"' in html and 'id="overview-install-1"' in html  # unique ids
    assert '<h3 id="chunks-and-mosaic-one-chunk">' in html  # levels shift under the page's own h1
    assert {"overview", "cli", "cli-argus-atlas-dem", "alignment-gate"} <= r.anchors
    linked = set(re.findall(r'href="#([^"]+)"', html))
    assert linked <= r.anchors, linked - r.anchors


def test_the_page_names_the_build_and_links_it(atlas: Path, vendor: Path) -> None:
    record = ad.import_docs(atlas, "HEAD")
    commit = record["atlas"]["commit"]
    out = ad.page(record, ad.render(record, vendor), "<nav>strip</nav>")
    assert "0.3.1.dev41+gabc1234" in out and f"<code>{commit[:7]}</code>" in out
    assert (
        f'href="https://github.com/smk762/argus-atlas/tree/{commit}" target="_blank" rel="noopener noreferrer"' in out
    )
    assert f'href="{ad.PYPI}"' in out  # a dev build: the project page
    assert ad.pypi_url("0.4.0") == f"{ad.PYPI}0.4.0/" and ad.pypi_url("0.4.0rc1") == f"{ad.PYPI}0.4.0rc1/"
    assert not bs.INLINE_SCRIPT.search(out) and not bs.INLINE_STYLE.search(out)
    # Served at /docs/ (smk762/argus-realms#10): root-absolute assets, so /docs without the slash renders the same.
    assert '<link rel="canonical" href="https://atlas.dragonhound.dev/docs/">' in out
    assert 'href="/docs/docs.css"' in out and 'src="/vendor/dh/dh-theme.js"' in out and '<a href="/">' in out


def test_the_committed_docs_render_with_every_panel_link() -> None:
    record = ad.load_record()
    rendered = ad.render(record)
    links = bs.docs_links(rendered.anchors)
    assert set(links) == set(bs.DOC_LINKS)
    assert all(href.startswith("docs/#") for href in links.values())  # from index.html at /, to /docs/
    assert (Path(bs.ROOT) / "site" / "docs" / "docs.css").is_file()
    for doc in ad.DOCS:
        assert doc.path in record["files"]
    assert not set(record["files"]) & ad.NEVER_IMPORT


def test_a_missing_panel_anchor_fails_the_build() -> None:
    with pytest.raises(SystemExit, match="#cli-argus-atlas-osm"):
        bs.docs_links({a for a in bs.DOC_LINKS.values() if a != "cli-argus-atlas-osm"})


# --- the sync rule --------------------------------------------------------------------------------------------


def test_tiles_from_another_build_are_refused() -> None:
    record = {"atlas": {"commit": COMMIT, "version": "0.3.1.dev41+gaaaaaaa"}}
    same = {"version": "0.3.1.dev41+gaaaaaaaaa.d20261117", "commit": COMMIT}  # a dirty build of the same commit
    ad.check_bakes(record, {"iceland/antique": [same]})
    with pytest.raises(ad.DocsError, match=r"iceland/tolkien: baked at argus-atlas bbbbbbb"):
        ad.check_bakes(
            record, {"iceland/antique": [same], "iceland/tolkien": [{"version": "0.3.0", "commit": "b" * 40}]}
        )
    with pytest.raises(ad.DocsError, match=r"recorded no commit \(before 0.4\)"):
        ad.check_bakes(record, {"reunion/antique": [{}]})
    with pytest.raises(ad.DocsError, match="version 0.3.2 at the docs' commit"):
        ad.check_bakes(record, {"reunion/antique": [{"version": "0.3.2", "commit": COMMIT}]})


def test_the_export_summarises_each_styles_bakes() -> None:
    def job(seed: int, gen: dict | None, labels: bool = False) -> dict:
        return {
            "chunk": {"z": 7, "x": 57, "y": seed, "size": 1024, "margin": 128},
            "sources": [{"key": "copernicus-glo30"}, {"key": "openstreetmap"}],
            "labels": {"count": 1} if labels else None,
            "bake": {
                "params": {
                    "recipe": "antique",
                    "preset": "antique",
                    "denoise": 0.6,
                    "steps": 30,
                    "cfg": 6.0,
                    "seed": seed,
                },
                "checkpoint": {"name": "juggernautXL.safetensors"},
                "lora": None,
                "aligned": True,
                "generator": gen,
            },
        }

    gen = {"version": "0.3.1.dev41+gaaaaaaa", "commit": COMMIT}
    s = bake_summary([job(31, gen, labels=True), job(32, gen)])
    assert (s["recipe"], s["preset"], s["denoise"], s["steps"], s["cfg"]) == (
        ["antique"],
        ["antique"],
        [0.6],
        [30],
        [6.0],
    )
    assert s["seeds"] == [31, 32] and s["chunks"] == 2 and s["aligned"] is True
    assert s["chunk"] == {"zoom": [7], "size": [1024], "margin": [128]}
    assert s["commands"] == ["dem", "osm", "chunk", "labels"]
    assert s["atlas"] == [gen] and s["lora"] == []
    old = bake_summary([{**job(31, None), "sources": None}])  # a bake from before argus-atlas 0.4
    assert old["atlas"] == [{"version": None, "commit": None}] and old["commands"] == ["chunk"]


def test_the_record_refuses_a_doc_outside_the_allowlist_or_vendored_elsewhere(atlas: Path, vendor: Path) -> None:
    record = ad.import_docs(atlas, "HEAD")
    (vendor / "secret.md").write_text("# Not vetted\n")
    unvetted = {
        **record,
        "files": {**record["files"], "docs/secret.md": {"as": "secret.md", "sha256": ad.sha256(b"# Not vetted\n")}},
    }
    (vendor / "sync.json").write_text(json.dumps(unvetted))
    with pytest.raises(ad.DocsError, match="not in DOCS"):
        ad.load_record(vendor)
    (vendor / "secret.md").unlink()
    outside = vendor.parent / "outside.md"
    outside.write_text("x")
    moved = json.loads(json.dumps(record))
    moved["files"]["docs/cli.md"] = {"as": "../outside.md", "sha256": ad.sha256(b"x")}
    (vendor / "sync.json").write_text(json.dumps(moved))
    with pytest.raises(ad.DocsError, match="vendors docs/cli.md as '../outside.md'"):
        ad.load_record(vendor)


def test_a_setext_title_is_not_doubled(atlas: Path, vendor: Path) -> None:
    (atlas / "docs/cli.md").write_text("CLI\n===\n\n## `argus-atlas dem`\n")
    _git(atlas, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "setext")
    html = ad.render(ad.import_docs(atlas, "HEAD"), vendor).html
    assert html.count('id="cli"') == 1


def test_realms_own_docs_and_bake_plans_pass_the_opsec_gate() -> None:
    """The bake plans run on the GPU host but read its layout from the environment: no host paths, host names or LAN
    addresses are committed, in them or in this repo's own docs. The imported docs/atlas/ are checked against their
    record above. The one address allowed is the GitHub noreply commit identity docs/deploy.md asks for."""
    root = Path(__file__).resolve().parent.parent
    tracked = subprocess.run(["git", "-C", str(root), "ls-files"], check=True, capture_output=True, text=True).stdout
    paths = [
        p
        for p in tracked.split()
        if p.startswith("bakes/") or (p.endswith(".md") and not p.startswith(("docs/atlas/", "site/vendor/")))
    ]
    assert "bakes/cook-strait.sh" in paths and "README.md" in paths
    found = {
        p: hits
        for p in paths
        if (hits := [f for f in ad.opsec_findings((root / p).read_text()) if "users.noreply.github.com" not in f])
    }
    assert found == {}
