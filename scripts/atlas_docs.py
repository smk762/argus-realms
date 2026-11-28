"""The argus-atlas docs on Atlas: import them at one atlas commit, check them, render them into /docs/ (docs/index.html).

    python scripts/atlas_docs.py import --atlas ../argus-atlas [--ref origin/dev]   # online: updates docs/atlas/
    python scripts/atlas_docs.py check                                              # offline: what CI runs

**Import** reads each doc in :data:`DOCS` from an argus-atlas checkout *at a commit* (``git show``, so the working
tree doesn't matter), runs the opsec gate (private addresses, home paths, emails, and the private names in
``OPSEC_DENYLIST``), copies the images they use, and writes ``docs/atlas/sync.json``: the atlas commit and version, and
the SHA256 of every file. The version is what hatch-vcs gives a build of that commit (setuptools-scm in a clean
worktree), the same string a bake records in ``job.json``. ``docs/your-first-bake.md`` (once ``docs/first-bake.md``)
is never imported: it is a GPU-host runbook, not demo documentation.

**Render** turns the imported markdown into one page at build time, so no markdown parser ships to the browser:
every heading gets an id (``<doc>-<slug>``) for deep links, links between imported docs become in-page anchors, other
argus-atlas links are pinned to the imported commit, off-site links open in a new tab with ``rel="noopener
noreferrer"`` and an sr-only cue, and images are served from this site (``img-src`` allows no other origin).

**The sync rule** (argus-studio#169): docs, tiles and package versions agree at argus-atlas ``dev`` HEAD. The static
build refuses tiles whose bakes record another atlas commit than ``sync.json`` (:func:`check_bakes`), and a scheduled
workflow (``atlas-drift.yml``) flags when ``dev`` moves on.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from argus_cortex.hashing import sha256_hex

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "docs" / "atlas"
RECORD = VENDOR / "sync.json"
REPO = "smk762/argus-atlas"
GITHUB = f"https://github.com/{REPO}"
PYPI = "https://pypi.org/project/argus-atlas/"
SETUPTOOLS_SCM = "setuptools-scm==8.3.1"
PAGE_DIR = "docs"  # the page is served at /docs/ (docs/index.html in the build)
IMG_DIR = "img"  # the docs' images, beside it at /docs/img/


@dataclass(frozen=True)
class Doc:
    id: str  # section id on the docs page, and the prefix of its heading ids
    path: str  # in argus-atlas
    title: str


DOCS = (
    Doc("overview", "README.md", "Overview"),
    Doc("chunks-and-mosaic", "docs/chunks-and-mosaic.md", "Chunks, mosaics and place names"),
    Doc("alignment-gate", "docs/alignment-gate.md", "The alignment gate"),
    Doc("cli", "docs/cli.md", "CLI reference"),
)
NEVER_IMPORT = {"docs/your-first-bake.md", "docs/first-bake.md"}

# The opsec gate Studio and Lightbox run on what they import, and argus-atlas's own tests/test_rules.py runs on its
# docs: private and CGNAT IPv4, home and mount paths, emails, plus the private names (the suite's GPU hosts and the
# like) in OPSEC_DENYLIST. The names are never committed, not even here: CI fills OPSEC_DENYLIST from the Actions secret
# of the same name, and a local run reads it from the environment or the untracked .opsec-denylist file (gitignored).
# Comma- or newline-separated, matched case-insensitively as whole words. With neither set, the name check is skipped
# (said once, on stderr) and the other checks still run.
OPSEC = {
    "private IPv4": re.compile(
        r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01])|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d{1,3}\.\d{1,3}\b"
    ),
    "home or mount path": re.compile(r"(?:/home/|/Users/|/root/|/mnt/|/media/|[A-Z]:\\Users\\)[\w.-]+"),
    "email": re.compile(r"\b[\w.%+-]+@(?!example\.(?:com|org)\b)[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b"),
}
DENYLIST_FILE = ROOT / ".opsec-denylist"
_denylist_notice_given = False


class DocsError(Exception):
    """The docs can't be imported, don't match their record, or disagree with the bakes."""


def denylist_terms() -> list[str]:
    """The private names: ``OPSEC_DENYLIST`` plus the untracked :data:`DENYLIST_FILE`, comma- or newline-separated."""
    raw = os.environ.get("OPSEC_DENYLIST", "")
    if DENYLIST_FILE.is_file():
        raw += "\n" + DENYLIST_FILE.read_text()
    return [t.strip() for t in re.split(r"[,\n]", raw) if t.strip()]


def denylist_pattern(terms: list[str]) -> re.Pattern[str] | None:
    """Every term as a whole word (not inside a longer one), case-insensitively; None when there are none."""
    if not terms:
        return None
    alternatives = "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


def opsec_findings(text: str) -> list[str]:
    global _denylist_notice_given
    found = [f"{label}: {m.group(0)!r}" for label, rx in OPSEC.items() for m in rx.finditer(text)]
    denied = denylist_pattern(denylist_terms())
    if denied:
        hits = {m.group(0).lower() for m in denied.finditer(text)}
        found += ["an OPSEC_DENYLIST term"] * len(hits)  # one per term found, never echoing it
    elif not _denylist_notice_given:
        _denylist_notice_given = True
        print(
            "opsec gate: OPSEC_DENYLIST is not configured (env, Actions secret or .opsec-denylist); "
            "machine names are not checked",
            file=sys.stderr,
        )
    return found


# --- import ---------------------------------------------------------------------------------------------------


def _git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    out = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return out.stdout if binary else out.stdout.decode().strip()


def atlas_version(repo: Path, commit: str) -> str:
    """The version hatch-vcs gives a build of *commit*: setuptools-scm in a clean, detached worktree."""
    with tempfile.TemporaryDirectory(prefix="atlas-docs-") as tmp:
        tree = Path(tmp) / "atlas"
        _git(repo, "worktree", "add", "--detach", "--quiet", str(tree), commit)
        try:
            cmd = ["uvx", "--quiet", "--from", SETUPTOOLS_SCM, "python", "-m", "setuptools_scm", "--root", str(tree)]
            return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()
        finally:
            _git(repo, "worktree", "remove", "--force", str(tree))


IMAGE_REF = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


def repo_path(href: str, doc_path: str) -> str | None:
    """The argus-atlas path a link or image points at (a GitHub blob/tree/raw URL of the repo, or a path relative to
    *doc_path*), or ``None`` when it points anywhere else."""
    url = href.split("#", 1)[0].split("?", 1)[0]
    m = re.match(rf"^(?:https://raw\.githubusercontent\.com/{REPO}/[^/]+/|{GITHUB}/(?:blob|tree|raw)/[^/]+/)(.+)$", url)
    if m:
        return m.group(1)
    if not url or SCHEME.match(url) or url.startswith("/"):
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(doc_path), url))
    return None if path == ".." or path.startswith("../") else path


def local_image(src: str, doc_path: str) -> str | None:
    """The ``docs/img/<name>`` an image shows, or ``None`` for an image on another origin (not served here)."""
    path = repo_path(src, doc_path)
    return path if path and re.fullmatch(r"docs/img/[\w.-]+", path) else None


def import_docs(repo: Path, ref: str) -> dict:
    """Copy :data:`DOCS` and their images from *repo* at *ref* into :data:`VENDOR` and write the record."""
    commit = str(_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}"))
    files: dict[str, dict] = {}
    texts: dict[Doc, bytes] = {}
    for doc in DOCS:
        if doc.path in NEVER_IMPORT:
            raise DocsError(f"{doc.path} is never imported")
        data = bytes(_git(repo, "show", f"{commit}:{doc.path}", binary=True))
        found = opsec_findings(data.decode("utf-8"))
        if found:
            raise DocsError(f"{doc.path} at {commit[:7]} fails the opsec gate: {found}")
        texts[doc] = data
    images: dict[str, bytes] = {}
    for doc, data in texts.items():
        for src in IMAGE_REF.findall(data.decode("utf-8")):
            path = local_image(src, doc.path)
            if path and path not in images:
                images[path] = bytes(_git(repo, "show", f"{commit}:{path}", binary=True))
    version = atlas_version(repo, commit)
    # Written into a sibling and swapped in whole, record included: a failure part-way leaves the old import as it was.
    new = VENDOR.with_name(f".{VENDOR.name}.new")
    shutil.rmtree(new, ignore_errors=True)
    try:
        (new / "img").mkdir(parents=True)
        for doc, data in texts.items():
            (new / f"{doc.id}.md").write_bytes(data)
            files[doc.path] = {"as": f"{doc.id}.md", "sha256": sha256_hex(data)}
        for path, data in images.items():
            name = PurePosixPath(path).name
            (new / "img" / name).write_bytes(data)
            files[path] = {"as": f"img/{name}", "sha256": sha256_hex(data)}
        record = {
            "atlas": {"repo": REPO, "commit": commit, "version": version, "ref": ref},
            "imported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "files": files,
        }
        (new / RECORD.relative_to(VENDOR)).write_text(json.dumps(record, indent=1) + "\n")
        old = VENDOR.with_name(f".{VENDOR.name}.old")
        shutil.rmtree(old, ignore_errors=True)
        if VENDOR.exists():
            VENDOR.rename(old)
        new.rename(VENDOR)
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(new, ignore_errors=True)
    return record


# --- check ----------------------------------------------------------------------------------------------------


def load_record(vendor: Path = VENDOR) -> dict:
    """The sync record, with every file it names present, unchanged, opsec-clean and allowed."""
    record_path = vendor / "sync.json"
    if not record_path.is_file():
        raise DocsError(f"{record_path} not found: import the argus-atlas docs (scripts/atlas_docs.py import)")
    record = json.loads(record_path.read_text())
    commit = record["atlas"]["commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise DocsError(f"sync.json: {commit!r} is not a full commit sha")
    as_doc = {d.path: f"{d.id}.md" for d in DOCS}
    for upstream, entry in record["files"].items():
        if upstream in NEVER_IMPORT:
            raise DocsError(f"sync.json imports {upstream}, which is never imported")
        # Only the allowlisted docs and the images they may show, each under the name import gives it.
        if upstream in as_doc:
            expected = as_doc[upstream]
        elif re.fullmatch(r"docs/img/[\w.-]+", upstream):
            expected = f"img/{PurePosixPath(upstream).name}"
        else:
            raise DocsError(f"sync.json imports {upstream}, which is not in DOCS")
        if entry["as"] != expected:
            raise DocsError(f"sync.json vendors {upstream} as {entry['as']!r}, not {expected!r}")
        path = vendor / entry["as"]
        if not path.is_file() or sha256_hex(path.read_bytes()) != entry["sha256"]:
            raise DocsError(f"docs/atlas/{entry['as']} differs from its record: re-import instead of editing it")
        if path.suffix == ".md" and (found := opsec_findings(path.read_text())):
            raise DocsError(f"docs/atlas/{entry['as']} fails the opsec gate: {found}")
    missing = [d.path for d in DOCS if d.path not in record["files"]]
    if missing:
        raise DocsError(f"sync.json lacks {', '.join(missing)}: re-import")
    extra = sorted(p.name for p in vendor.glob("*.md") if p.name not in {e["as"] for e in record["files"].values()})
    if extra:
        raise DocsError(f"docs/atlas holds files its record doesn't name: {', '.join(extra)}")
    return record


def public_version(version: str | None) -> str | None:
    """A version without its local part (``+g<sha>.d<date>``): a dirty build of the same commit still matches."""
    return version.split("+", 1)[0] if version else None


def check_bakes(record: dict, builds: dict[str, list[dict]]) -> None:
    """Refuse tiles whose bakes record another argus-atlas build than the docs: *builds* maps an area/style name to
    the ``{"version", "commit"}`` builds its chunks' ``job.json`` record (from the tile export's manifest)."""
    want = record["atlas"]
    problems = []
    for name, seen in sorted(builds.items()):
        for b in seen:
            if not b.get("commit"):
                problems.append(f"{name}: baked by an argus-atlas build that recorded no commit (before 0.4)")
            elif b["commit"] != want["commit"]:
                problems.append(f"{name}: baked at argus-atlas {b['commit'][:7]} ({b.get('version')})")
            elif public_version(b.get("version")) != public_version(want["version"]):
                problems.append(f"{name}: argus-atlas version {b.get('version')} at the docs' commit")
    if problems:
        raise DocsError(
            f"the tiles and the argus-atlas docs disagree: the docs are argus-atlas {want['version']} at "
            f"{want['commit'][:7]}, but\n  " + "\n  ".join(problems) + "\nRe-bake at that commit, or import the docs "
            "at the bakes' commit (scripts/atlas_docs.py import --ref <sha>), so the two land together."
        )


# --- render ---------------------------------------------------------------------------------------------------

LEAD_HTML = re.compile(r"\A\s*<h1>.*?</h1>\s*", re.S)  # README's logo lockup (raw HTML, external images)
BADGES = re.compile(r"^\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)\s*$\n?", re.M)  # shields.io badges: another origin
NEW_TAB = '<span class="sr-only"> (opens in new tab)</span>'


def _family(host: str) -> bool:
    return host == "dragonhound.dev" or host.endswith(".dragonhound.dev")


def slug(text: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    return re.sub(r"\s", "-", re.sub(r"[^\w\- ]", "", text.strip().lower()))


@dataclass
class Rendered:
    html: str
    anchors: set[str]
    toc: list[tuple[str, str, list[tuple[str, str]]]]  # (doc id, title, [(anchor, heading)])


def render(record: dict, vendor: Path = VENDOR) -> Rendered:
    """Every imported doc as one ``<section>`` each, with the rewritten links, heading ids and a table of contents."""
    from markdown_it import MarkdownIt
    from markdown_it.token import Token

    commit = record["atlas"]["commit"]
    by_path = {d.path: d for d in DOCS}
    md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
    anchors: set[str] = set()
    toc = []
    parts = []

    # Headings first, over every doc, so each id is unique on the page: the docs' own ids are reserved, a heading
    # that would land on one (doc `a`, "B C" vs doc `a-b`, "C") gets a suffix, and links resolve to what was assigned.
    taken = {d.id for d in DOCS}
    assigned: dict[tuple[str, str], str] = {}  # (doc id, GitHub's in-doc anchor) -> the id on this page
    parsed: list[tuple[Doc, list, list[tuple[str, str]], bool]] = []
    for doc in DOCS:
        text = (vendor / record["files"][doc.path]["as"]).read_text()
        tokens = md.parse(BADGES.sub("", LEAD_HTML.sub("", text)))
        heads: list[tuple[str, str]] = []
        seen: dict[str, int] = {}
        first = True
        titled = False  # the doc's first heading is an H1, which then carries the section's id
        for i, tok in enumerate(tokens):
            if tok.type != "heading_open":
                continue
            title = tokens[i + 1].content
            level = int(tok.tag[1])
            if level == 1 and first:
                anchor = doc.id  # the doc's own title names its section
                titled = True
            else:
                base = slug(title)
                n = seen.get(base, 0)
                seen[base] = n + 1
                local = base if n == 0 else f"{base}-{n}"
                anchor, k = f"{doc.id}-{local}", 0
                while anchor in taken:
                    k += 1
                    anchor = f"{doc.id}-{local}-x{k}"
                assigned.setdefault((doc.id, local), anchor)
                taken.add(anchor)
                if level == 2:
                    heads.append((anchor, re.sub(r"`", "", title)))
            first = False
            tok.attrSet("id", anchor)
            anchors.add(anchor)
            tok.tag = tokens[i + 2].tag = f"h{min(level + 1, 6)}"  # the page's h1 is its own title
        parsed.append((doc, tokens, heads, titled))

    def to(doc: Doc, frag: str) -> str:
        return "#" + (assigned.get((doc.id, slug(frag)), f"{doc.id}-{slug(frag)}") if frag else doc.id)

    def resolve(href: str, doc: Doc) -> tuple[str, bool]:
        """(new href, opens off-site)."""
        url, _, frag = href.partition("#")
        if not url:
            return to(doc, frag), False
        path = repo_path(href, doc.path)
        if path in by_path:  # another imported doc: an anchor on this page
            return to(by_path[path], frag), False
        if path:  # anything else in argus-atlas, pinned to the imported commit (GitHub redirects blob to tree)
            return f"{GITHUB}/blob/{commit}/{path}" + (f"#{frag}" if frag else ""), True
        host = re.match(r"^https?://([^/:]+)", url)
        return href, bool(host) and not _family(host.group(1))

    for doc, tokens, heads, titled in parsed:
        for tok in tokens:
            if tok.type != "inline" or not tok.children:
                continue
            kids: list = []
            for child in tok.children:
                if child.type == "image":
                    path = local_image(child.attrGet("src") or "", doc.path)
                    if not path:
                        continue  # another origin: img-src would block it
                    child.attrSet("src", f"/{PAGE_DIR}/{IMG_DIR}/{PurePosixPath(path).name}")
                    child.attrSet("loading", "lazy")
                elif child.type == "link_open":
                    href, external = resolve(child.attrGet("href") or "", doc)
                    child.attrSet("href", href)
                    if external:
                        child.attrSet("target", "_blank")
                        child.attrSet("rel", "noopener noreferrer")
                        child.meta["external"] = True
                elif child.type == "link_close":
                    opener = next(c for c in reversed(kids) if c.type == "link_open")
                    if opener.meta.get("external"):
                        cue = Token("html_inline", "", 0)
                        cue.content = NEW_TAB
                        kids.append(cue)
                kids.append(child)
            tok.children = kids
        body = md.renderer.render(tokens, md.options, {})
        heading = "" if titled else f'<h2 id="{doc.id}">{html.escape(doc.title)}</h2>\n'
        anchors.add(doc.id)
        parts.append(f'<section class="doc" aria-labelledby="{doc.id}">\n{heading}{body}</section>')
        toc.append((doc.id, doc.title, heads))
    return Rendered("\n".join(parts), anchors, toc)


def pypi_url(version: str) -> str:
    """The release's page when *version* is one PyPI has (a final, alpha, beta or rc), else the project page."""
    return f"{PYPI}{version}/" if re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", version) else PYPI


def external(href: str, text: str) -> str:
    return f'<a href="{html.escape(href)}" target="_blank" rel="noopener noreferrer">{text}{NEW_TAB}</a>'


def page(record: dict, rendered: Rendered, strip: str) -> str:
    """The docs page (/docs/): the shared strip, a header naming the atlas build, the contents and the docs. No inline script or
    style of its own (docs.css); the build inlines the vendored stylesheets, as on index.html."""
    a = record["atlas"]
    version, short = html.escape(a["version"]), a["commit"][:7]
    nav = "\n".join(
        f'<li><a href="#{doc_id}">{html.escape(title)}</a>'
        + (
            "<ul>" + "".join(f'<li><a href="#{h}">{html.escape(t)}</a></li>' for h, t in heads) + "</ul>"
            if heads
            else ""
        )
        + "</li>"
        for doc_id, title, heads in rendered.toc
    )
    return f"""<!doctype html>
<html lang="en" data-dh-site="atlas">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>argus-atlas docs · Atlas</title>
<meta name="description" content="How Atlas's maps were made: the argus-atlas docs at the build that baked them.">
<link rel="canonical" href="https://atlas.dragonhound.dev/docs/">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-icon.png">
<meta name="color-scheme" content="light dark">
<script src="/vendor/dh/dh-theme.js"></script>
<link rel="stylesheet" href="vendor/dh/dh-tokens.css">
<link rel="stylesheet" href="vendor/dh/dh-strip.css">
<link rel="stylesheet" href="vendor/dh/dh-badge.css">
<link rel="stylesheet" href="/docs/docs.css">
</head>
<body>
<a class="skip" href="#docs">Skip to the docs</a>
{strip}
<header class="docs-head">
  <p class="back"><a href="/">← Back to the map</a></p>
  <h1>argus-atlas docs</h1>
  <p class="build">The docs of the argus-atlas build that baked these maps: version <strong>{version}</strong>,
    commit <code>{short}</code>.</p>
  <p class="links">{external(pypi_url(a["version"]), "argus-atlas on PyPI")} ·
    {external(f"{GITHUB}/tree/{a['commit']}", f"Source at {short} on GitHub")}</p>
</header>
<div class="docs-body">
<nav class="toc" aria-label="Contents"><ul>
{nav}
</ul></nav>
<main id="docs" tabindex="-1">
{rendered.html}
</main>
</div>
</body>
</html>
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="Copy the docs from an argus-atlas checkout at a commit (online)")
    imp.add_argument("--atlas", type=Path, required=True, help="An argus-atlas git checkout")
    imp.add_argument("--ref", default="origin/dev", help="The commit to import at (default: origin/dev, fetched)")
    sub.add_parser("check", help="Check the imported docs against their record and render them (offline)")
    a = p.parse_args()
    try:
        if a.cmd == "import":
            if a.ref.startswith("origin/"):
                _git(a.atlas, "fetch", "--quiet", "origin")
            r = import_docs(a.atlas, a.ref)
            print(
                f"imported argus-atlas {r['atlas']['version']} at {r['atlas']['commit'][:7]}: {len(r['files'])} files"
            )
        else:
            record = load_record()
            rendered = render(record)
            print(
                f"docs/atlas: argus-atlas {record['atlas']['version']} at {record['atlas']['commit'][:7]}, "
                f"{len(rendered.anchors)} anchors"
            )
    except (DocsError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.decode() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else ""
        sys.exit(f"{exc}\n{detail}".strip())


if __name__ == "__main__":
    main()
