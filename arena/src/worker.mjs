// The game server, in a Web Worker: Pyodide runs the same Python rules, engine and bots as atlas_realms.api
// (arena/py/arena_bridge.py routes the client's requests). Nothing leaves the browser.
import { loadPyodide } from "./pyodide/pyodide.mjs";

const queue = [];
let handle = null;

function reply(id, res) { postMessage({ id, res }); }

function serve({ id, req }) {
  try {
    reply(id, handle(JSON.stringify(req)));
  } catch (e) {
    reply(id, JSON.stringify({ status: 500, body: { detail: String(e && e.message || e).split("\n").slice(-2).join(" ") } }));
  }
}

onmessage = (e) => { if (handle) serve(e.data); else queue.push(e.data); };

async function boot() {
  const t0 = performance.now();
  const cfg = await (await fetch("arena.json")).json();
  postMessage({ progress: "Starting Python (WebAssembly)…" });
  const py = await loadPyodide({ indexURL: new URL("pyodide/", location.href).href });
  postMessage({ progress: "Loading h3 and pydantic…" });
  await py.loadPackage(cfg.packages, { messageCallback: () => {} });
  postMessage({ progress: "Loading the rules…" });
  const zip = await (await fetch(cfg.python)).arrayBuffer();
  py.unpackArchive(zip, "zip", { extractDir: "/home/pyodide/realms" });
  py.runPython(`
import json, sys
from pathlib import Path
sys.path.insert(0, "/home/pyodide/realms")
from arena_bridge import Arena
_root = Path("/home/pyodide/realms/data")
arena = Arena(json.loads((_root / "manifest.json").read_text()), _root / "nz_mainland.geojson")
`);
  handle = py.globals.get("arena").handle;
  postMessage({ ready: true, ms: Math.round(performance.now() - t0) });
  while (queue.length) serve(queue.shift());
}

boot().catch((e) => postMessage({ fatal: String(e && e.message || e) }));
