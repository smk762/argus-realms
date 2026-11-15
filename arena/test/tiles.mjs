// Painted tiles load from a server that, like Cloudflare Pages on *.pages.dev, ignores Range headers
// (python -m http.server). Builds with --tiles only; local, like the other Playwright checks:
//
//   python3 build.py --tiles && node test/tiles.mjs [screenshot dir]
//
// Starts a game on every painted region (picked in the start dialog) and counts successful tile image responses at
// three zooms of each; fails on any failed tile, any request carrying a Range
// header, or any console error. CHROME=<path> uses an installed Chromium.
import { chromium } from "playwright";
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const dist = join(dirname(fileURLToPath(import.meta.url)), "..", "dist");
const shots = process.argv[2];
const port = 8140 + Math.floor(Math.random() * 50);
const painted = JSON.parse(readFileSync(join(dist, "regions.json"), "utf8")).regions.filter((r) => !r.placeholder);
if (!painted.length) { console.log("placeholder build: no painted tiles to check"); process.exit(0); }

const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", dist], { stdio: "ignore" });
const problems = [];
const tiles = new Map(); // url -> status
const perRegion = new Map(); // region -> tiles loaded
let ranged = 0;
try {
  await new Promise((ok) => setTimeout(ok, 800));
  const browser = await chromium.launch({ executablePath: process.env.CHROME || undefined });
  for (const region of painted) {
    const m = JSON.parse(readFileSync(join(dist, region.manifest), "utf8"));
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 } }); // no save from the last one
    const page = await context.newPage();
    page.on("console", (msg) => { if (msg.type() === "error") problems.push(`${region.id} console: ${msg.text()}`); });
    page.on("pageerror", (e) => problems.push(`${region.id} page error: ${e.message}`));
    page.on("request", (r) => { if (r.headers().range) ranged++; });
    page.on("response", (r) => {
      const hit = r.url().match(/\/tiles\/([a-z-]+)\/[a-z]+\/\d+\/\d+\/\d+\.webp$/);
      if (!hit) return;
      tiles.set(r.url(), r.status());
      if (r.status() === 200) perRegion.set(hit[1], (perRegion.get(hit[1]) || 0) + 1);
      if (hit[1] !== region.id) problems.push(`${region.id}: loaded another region's tile ${r.url()}`);
      if (r.headers()["content-type"] !== "image/webp") problems.push(`${r.url()}: ${r.headers()["content-type"]}`);
    });
    await page.goto(`http://127.0.0.1:${port}/`);
    await page.waitForSelector("dialog#start[open]", { timeout: 120_000 });
    await page.selectOption("#start-region", region.id);
    await page.waitForFunction((id) => manifest.region.id === id && map.loaded(), region.id);
    await page.click("#start-form button[type=submit]");
    await page.waitForFunction(() => typeof lastState !== "undefined" && lastState);
    const [w, s, e, n] = m.bounds, mid = [(w + e) / 2, (s + n) / 2];
    const [lat, lng] = region.spawns[0];
    for (const [zoom, centre] of [[m.chunk_zoom - 2, mid], [m.chunk_zoom, mid], [m.maxzoom, [lng, lat]]]) {
      await page.evaluate(([z, c]) => map.jumpTo({ zoom: z, center: c }), [zoom, centre]);
      await page.waitForTimeout(2500);
      if (shots) await page.screenshot({ path: join(shots, `arena-tiles-${region.id}-z${zoom}.png`) });
    }
    await context.close();
  }
  await browser.close();
} finally {
  server.kill();
}
const ok = [...tiles.values()].filter((s) => s === 200).length;
const failed = [...tiles].filter(([, s]) => s !== 200);
console.log(`tiles: ${ok} loaded (${[...perRegion].map(([r, k]) => `${r} ${k}`).join(", ")}), ${failed.length} failed; requests with a Range header: ${ranged}`);
for (const r of painted) if ((perRegion.get(r.id) || 0) < 20) problems.push(`${r.id}: only ${perRegion.get(r.id) || 0} tiles loaded`);
if (failed.length) problems.push(`failed tiles: ${failed.slice(0, 5).map(([u, s]) => `${s} ${u}`).join(", ")}`);
if (ranged) problems.push(`${ranged} requests asked for a byte range`);
if (problems.length) { console.log(problems.join("\n")); process.exit(1); }
console.log("ok: painted tiles load from a server without range support");
