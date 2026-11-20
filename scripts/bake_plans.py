"""Regenerate every bake plan (bakes/<name>.sh) from bakes/plans.json, or check the committed ones are current.

    python scripts/bake_plans.py           # write bakes/<name>.sh   (make bake-plans)
    python scripts/bake_plans.py --check   # exit 1 if a committed plan differs (tests/test_bakeplan.py runs it)

Each plan bakes with the argus-atlas image at the commit in docs/atlas/sync.json, so after a docs import
(`make atlas-docs`) the plans must be regenerated too: tiles and docs come from one build.
"""

import argparse
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from atlas_realms.bakeplan import main as bakeplan

ROOT = Path(__file__).resolve().parent.parent
PLANS = ROOT / "bakes" / "plans.json"


def argv(name: str, spec: dict) -> list[str]:
    args = ["--name", name, "--zoom", str(spec["zoom"]), "--styles", spec["styles"], "--layers", spec["layers"], "--area"]
    if spec.get("tiles"):
        args += ["--tiles", ",".join(spec["tiles"])]
    else:
        args += [f"--center={spec['center']}", "--rings", str(spec["rings"])]
    if spec.get("land"):
        args += ["--land", str(ROOT / spec["land"])]
    return args


def render(name: str, spec: dict) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        bakeplan(argv(name, spec))
    return buf.getvalue()


def plans() -> dict[str, dict]:
    return {k: v for k, v in json.loads(PLANS.read_text()).items() if not k.startswith("_")}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="Only check the committed plans are current")
    a = p.parse_args()
    stale = []
    for name, spec in plans().items():
        path, text = ROOT / "bakes" / f"{name}.sh", render(name, spec)
        if a.check:
            if not path.is_file() or path.read_text() != text:
                stale.append(path.name)
        else:
            path.write_text(text)
    if stale:
        sys.exit(f"stale bake plans: {', '.join(stale)}; run `make bake-plans` (after a docs import, the commit moved)")
    print("bake plans current" if a.check else f"wrote {len(plans())} bake plans")


if __name__ == "__main__":
    main()
