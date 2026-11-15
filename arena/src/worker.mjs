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

// Download progress is real: arena.json lists every file Pyodide will need with its size, and the worker fetches them
// first, counting bytes. Pyodide's own requests for those files are then answered from memory (serveFromMemory), so
// nothing downloads twice even where the HTTP cache won't keep a 9 MB file. The later steps (start Python, load
// packages, run the game's imports) can't be measured and report as indeterminate.
async function prefetch(files) {
  const total = files.reduce((n, f) => n + f.bytes, 0), bufs = new Map();
  let loaded = 0, last = 0;
  const step = (n) => {
    loaded += n;
    if (performance.now() - last > 100 || loaded >= total) {
      last = performance.now();
      postMessage({ progress: { step: "download", loaded, total } });
    }
  };
  await Promise.all(files.map(async (f) => {
    const r = await fetch(f.url);
    if (!r.ok) throw new Error(`${f.url}: ${r.status}`);
    const reader = r.body.getReader(), parts = [];
    let got = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parts.push(value);
      got += value.length;
      step(value.length);
    }
    step(Math.max(0, f.bytes - got)); // a size that differs from the listing (it shouldn't) still completes the bar
    bufs.set(new URL(f.url, location.href).pathname, await new Blob(parts).arrayBuffer());
  }));
  return bufs;
}

const TYPES = { wasm: "application/wasm", mjs: "text/javascript", json: "application/json" };

function serveFromMemory(bufs) {
  const realFetch = globalThis.fetch;
  globalThis.fetch = (input, init) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    const buf = url.origin === location.origin && bufs.get(url.pathname);
    if (!buf) return realFetch(input, init);
    const type = TYPES[url.pathname.split(".").pop()] || "application/octet-stream";
    return Promise.resolve(new Response(buf, { headers: { "Content-Type": type } }));
  };
  return () => { globalThis.fetch = realFetch; bufs.clear(); };
}

async function boot() {
  const t0 = performance.now();
  const cfg = await (await fetch("arena.json")).json();
  const files = await prefetch(cfg.preload);
  const zip = files.get(new URL(cfg.python, location.href).pathname);
  const release = serveFromMemory(files);
  postMessage({ progress: { step: "python" } });
  const py = await loadPyodide({ indexURL: new URL("pyodide/", location.href).href });
  postMessage({ progress: { step: "packages" } });
  await py.loadPackage(cfg.packages, { messageCallback: () => {} });
  postMessage({ progress: { step: "rules" } });
  py.unpackArchive(zip, "zip", { extractDir: "/home/pyodide/realms" });
  release();
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
