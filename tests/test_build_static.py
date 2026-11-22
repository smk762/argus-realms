"""scripts/build_static.py: the _headers rules, the CSP's inline-script guard and the checks run before dist/ is wiped."""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_static as bs  # noqa: E402


def _rules(headers: str) -> list[tuple[str, list[str]]]:
    rules: list[tuple[str, list[str]]] = []
    for line in headers.splitlines():
        if line and not line.startswith(" "):
            rules.append((line, []))
        elif line.strip():
            rules[-1][1].append(line.split(":", 1)[0].strip())
    return rules


def _matches(pattern: str, path: str) -> bool:
    """Cloudflare Pages' _headers matching: `*` is a greedy splat over any characters, slashes included."""
    return re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), path) is not None


@pytest.mark.parametrize(
    ("path", "max_age"),
    [
        ("/tiles/areas.json", "max-age=300"),
        ("/tiles/empty.png", "max-age=86400"),
        ("/tiles/cook-strait/antique/12/4031/2560.png", "max-age=86400"),
        ("/tiles/cook-strait/labels.geojson", "max-age=86400"),
    ],
)
def test_one_cache_control_per_path(path: str, max_age: str) -> None:
    setting = [p for p, names in _rules(bs.HEADERS) if _matches(p, path) and "Cache-Control" in names]
    assert len(setting) == 1, setting  # Pages would join two into one comma-separated header
    block = bs.HEADERS.split(setting[0] + "\n", 1)[1]
    assert max_age in block.split("\n/", 1)[0]


def test_an_inline_script_on_any_page_fails_the_build() -> None:
    bs.security_headers("<p>ok</p>", "<p>docs</p>")
    with pytest.raises(SystemExit, match="docs page"):
        bs.security_headers("<p>ok</p>", "<script>x()</script>")


def test_an_area_without_public_styles_or_no_areas_fails_before_anything_is_written(tmp_path: Path) -> None:
    record = {"atlas": {"commit": "a" * 40, "version": "0.3.1"}}
    with pytest.raises(SystemExit, match="no areas"):
        bs.check_sync(tmp_path, record, [])
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "manifest.json").write_text(json.dumps({"styles": [{"name": "antique", "patron_only": True}]}))
    with pytest.raises(SystemExit, match="x: no public styles"):
        bs.check_sync(tmp_path, record, [{"id": "x"}])
