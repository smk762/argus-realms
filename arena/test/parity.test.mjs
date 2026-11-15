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
const python = process.env.PYTHON || [join(root, ".venv/bin/python")].find(existsSync) || "python3";

function reference() {
  const out = execFileSync(python, [join(arena, "py/parity_scenario.py"), "--ticks", String(TICKS)], {
    env: { ...process.env, PYTHONPATH: join(arena, "py"), PYTHONHASHSEED: "random" }, maxBuffer: 64 << 20,
  });
  return JSON.parse(out);
}

async function bundle() {
  const cfg = JSON.parse(readFileSync(join(dist, "arena.json"), "utf8"));
  const py = await loadPyodide({ indexURL: join(dist, "pyodide") + "/" });
  await py.loadPackage(cfg.packages, { messageCallback: () => {} });
  py.unpackArchive(new Uint8Array(readFileSync(join(dist, cfg.python))), "zip", { extractDir: "/home/pyodide/realms" });
  // The reference world, not dist's manifest (whose styles depend on whether tiles were bundled).
  py.FS.writeFile("/home/pyodide/world.json", readFileSync(join(arena, "world/cook-strait.json")));
  return JSON.parse(py.runPython(`
import json, sys
from pathlib import Path
sys.path.insert(0, "/home/pyodide/realms")
import parity_scenario
json.dumps(parity_scenario.run(json.loads(Path("/home/pyodide/world.json").read_text()),
                               Path("/home/pyodide/realms/data/nz_mainland.geojson"), ${TICKS}), sort_keys=True)
`));
}

test(`Pyodide bundle matches CPython over ${TICKS} ticks`, { timeout: 600_000 }, async () => {
  assert.ok(readdirSync(dist).includes("arena.json"), "arena/dist is missing: run `python3 build.py` first");
  const [want, got] = [reference(), await bundle()];
  assert.equal(got.build, want.build, "the bundled Python sources differ from src/ (rebuild arena/dist)");
  assert.equal(want.ticks, TICKS);
  assert.ok(want.save_resume_matches, "CPython: save + restore changed the game");
  assert.ok(got.save_resume_matches, "Pyodide: save + restore changed the game");
  const firstDiff = want.trace.findIndex((t, i) => JSON.stringify(t) !== JSON.stringify(got.trace[i]));
  assert.equal(firstDiff, -1, `first divergence at trace step ${firstDiff}: ${JSON.stringify(want.trace[firstDiff])} vs ${JSON.stringify(got.trace[firstDiff])}`);
  assert.equal(got.trace.length, want.trace.length);
  assert.deepEqual(got.final, want.final);
  assert.deepEqual(got.report, want.report);
  assert.equal(got.fog, want.fog);
  // The game went somewhere: land was claimed and someone fought.
  assert.ok(want.final.players.some((p) => p.hexes > 0));
  assert.ok(want.final.battles.length > 0);
});
