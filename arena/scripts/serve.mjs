// Local dev server for arena/dist: static files with the headers from dist/_headers (so the CSP is exercised as on
// Cloudflare Pages). Loopback only.   node scripts/serve.mjs dist [port]
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { extname, join, normalize, resolve } from "node:path";

const root = resolve(process.argv[2] || "dist");
const port = Number(process.argv[3] || 8120);
const TYPES = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
  ".json": "application/json", ".wasm": "application/wasm", ".zip": "application/zip", ".whl": "application/zip",
  ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".geojson": "application/geo+json",
};

// _headers: a path pattern line, then indented "Name: value" lines. "*" matches anything.
function parseHeaders(text) {
  const rules = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    if (!/^\s/.test(line)) rules.push({ re: new RegExp(`^${line.trim().replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*")}$`), h: [] });
    else { const i = line.indexOf(":"); rules.at(-1).h.push([line.slice(0, i).trim(), line.slice(i + 1).trim()]); }
  }
  return rules;
}

const rules = parseHeaders(await readFile(join(root, "_headers"), "utf8").catch(() => ""));

createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  let path = normalize(decodeURIComponent(url.pathname));
  if (path.includes("..")) { res.writeHead(400).end(); return; }
  let file = join(root, path);
  try { if ((await stat(file)).isDirectory()) file = join(file, "index.html"); } catch { /* 404 below */ }
  try {
    const body = await readFile(file);
    const headers = { "Content-Type": TYPES[extname(file)] || "application/octet-stream" };
    for (const r of rules) if (r.re.test(url.pathname)) for (const [k, v] of r.h) headers[k] = v;
    res.writeHead(200, headers).end(body);
  } catch {
    res.writeHead(404, { "Content-Type": "text/plain" }).end("not found");
  }
}).listen(port, "127.0.0.1", () => console.log(`arena: http://127.0.0.1:${port}/`));
