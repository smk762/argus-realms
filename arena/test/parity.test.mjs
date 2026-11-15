// Parity: the browser bundle (Pyodide, from arena/dist) plays the same scripted game as the CPython reference
// (src/ through arena/py/parity_scenario.py) and must produce the same state on every tick. This guards the bundle
// against drifting from the rules: a stale or partial zip, a different h3 or pydantic, Python 3.14 vs the project's.
// Needs `python3 build.py` first (the dist/ it tests) and the project venv for the reference.
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadPyodide } from "pyodide";

const here = dirname(fileURLToPath(import.meta.url));
const arena = resolve(here, ".."), root = resolve(arena, "..");
const dist = join(arena, "dist");
const TICKS = Number(process.env.PARITY_TICKS || 400);
// Every region plays: Cook Strait the full length, the others a shorter game (their worlds differ, the rules don't).
const OTHER_TICKS = Number(process.env.PARITY_OTHER_TICKS || 100);
const REGIONS = JSON.parse(readFileSync(join(arena, "regions.json"), "utf8")).regions.map((r) => r.id);
const python = process.env.PYTHON || [join(root, ".venv/bin/python")].find(existsSync) || "python3";

function reference(region, ticks) {
  const out = execFileSync(python, [join(arena, "py/parity_scenario.py"), "--ticks", String(ticks), "--region", region], {
    env: { ...process.env, PYTHONPATH: join(arena, "py"), PYTHONHASHSEED: "random" }, maxBuffer: 64 << 20,
  });
  return JSON.parse(out);
}

let pyodide;
async function bundle(region, ticks) {
  if (!pyodide) {
    const cfg = JSON.parse(readFileSync(join(dist, "arena.json"), "utf8"));
    pyodide = await loadPyodide({ indexURL: join(dist, "pyodide") + "/" });
    await pyodide.loadPackage(cfg.packages, { messageCallback: () => {} });
    pyodide.unpackArchive(new Uint8Array(readFileSync(join(dist, cfg.python))), "zip", { extractDir: "/home/pyodide/realms" });
  }
  const py = pyodide;
  // The zip's regions: the committed world manifests, as the CPython reference reads them from arena/regions.json.
  return JSON.parse(py.runPython(`
import json, sys
from pathlib import Path
sys.path.insert(0, "/home/pyodide/realms")
import parity_scenario
_root = Path("/home/pyodide/realms/data")
json.dumps(parity_scenario.run(_root / "regions.json", _root, "${region}", ${ticks}), sort_keys=True)
`));
}

for (const region of REGIONS) {
  const ticks = region === REGIONS[0] ? TICKS : OTHER_TICKS;
  test(`${region}: Pyodide bundle matches CPython over ${ticks} ticks`, { timeout: 600_000 }, async () => {
    assert.ok(readdirSync(dist).includes("arena.json"), "arena/dist is missing: run `python3 build.py` first");
    const [want, got] = [reference(region, ticks), await bundle(region, ticks)];
    assert.equal(got.build, want.build, "the bundled Python sources differ from src/ (rebuild arena/dist)");
    assert.equal(got.region, region);
    assert.equal(want.ticks, ticks);
    assert.equal(got.hexes, want.hexes, "the bundle built a different world");
    assert.ok(want.save_resume_matches, "CPython: save + restore changed the game");
    assert.ok(got.save_resume_matches, "Pyodide: save + restore changed the game");
    const firstDiff = want.trace.findIndex((t, i) => JSON.stringify(t) !== JSON.stringify(got.trace[i]));
    assert.equal(firstDiff, -1, `first divergence at trace step ${firstDiff}: ${JSON.stringify(want.trace[firstDiff])} vs ${JSON.stringify(got.trace[firstDiff])}`);
    assert.equal(got.trace.length, want.trace.length);
    assert.deepEqual(got.final, want.final);
    assert.deepEqual(got.report, want.report);
    assert.equal(got.fog, want.fog);
    // The game went somewhere: land was claimed, and over the full game someone fought.
    assert.ok(want.final.players.some((p) => p.hexes > 0));
    if (ticks >= 400) assert.ok(want.final.battles.length > 0);
  });
}
