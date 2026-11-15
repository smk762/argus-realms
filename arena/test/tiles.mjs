// Painted tiles load from a server that, like Cloudflare Pages on *.pages.dev, ignores Range headers
// (python -m http.server). Builds with --tiles only; local, like the other Playwright checks:
//
//   python3 build.py --tiles ../var/tiles && node test/tiles.mjs [screenshot dir]
//
// Counts successful tile image responses at three zooms and fails on any failed tile, any request carrying a Range
// header, or any console error. CHROME=<path> uses an installed Chromium.
import { chromium } from "playwright";
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const dist = join(dirname(fileURLToPath(import.meta.url)), "..", "dist");
const shots = process.argv[2];
const port = 8140 + Math.floor(Math.random() * 50);
const manifest = JSON.parse(readFileSync(join(dist, "manifest.json"), "utf8"));
if (manifest.placeholder) { console.log("placeholder build: no painted tiles to check"); process.exit(0); }

const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", dist], { stdio: "ignore" });
const problems = [];
const tiles = new Map(); // url -> status
let ranged = 0;
try {
  await new Promise((ok) => setTimeout(ok, 800));
  const browser = await chromium.launch({ executablePath: process.env.CHROME || undefined });
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 800 } })).newPage();
  page.on("console", (m) => { if (m.type() === "error") problems.push(`console: ${m.text()}`); });
  page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));
  page.on("request", (r) => { if (r.headers().range) ranged++; });
  page.on("response", (r) => {
    if (/\/tiles\/[a-z]+\/\d+\/\d+\/\d+\.webp$/.test(r.url())) {
      tiles.set(r.url(), r.status());
      if (r.headers()["content-type"] !== "image/webp") problems.push(`${r.url()}: ${r.headers()["content-type"]}`);
    }
  });
  await page.goto(`http://127.0.0.1:${port}/`);
  await page.waitForSelector("dialog#start[open]", { timeout: 120_000 });
  await page.click("#start-form button[type=submit]");
  await page.waitForFunction(() => typeof lastState !== "undefined" && lastState);
  for (const [zoom, centre] of [[8, null], [10, [174.6, -41.3]], [12, [174.78, -41.29]], [12, [174.6, -41.6]]]) {
    await page.evaluate(([z, c]) => map.jumpTo(c ? { zoom: z, center: c } : { zoom: z }), [zoom, centre]);
    await page.waitForTimeout(2500);
    if (shots) await page.screenshot({ path: join(shots, `arena-tiles-xyz-z${zoom}${centre ? "-" + centre.join("_") : ""}.png`) });
  }
  await browser.close();
} finally {
  server.kill();
}
const ok = [...tiles.values()].filter((s) => s === 200).length;
const failed = [...tiles].filter(([, s]) => s !== 200);
const zooms = [...new Set([...tiles.keys()].map((u) => u.split("/").at(-3)))].sort((a, b) => a - b);
console.log(`tiles: ${ok} loaded (zooms ${zooms.join(", ")}), ${failed.length} failed; requests with a Range header: ${ranged}`);
if (ok < 20) problems.push(`only ${ok} tiles loaded`);
if (failed.length) problems.push(`failed tiles: ${failed.slice(0, 5).map(([u, s]) => `${s} ${u}`).join(", ")}`);
if (ranged) problems.push(`${ranged} requests asked for a byte range`);
if (problems.length) { console.log(problems.join("\n")); process.exit(1); }
console.log("ok: painted tiles load from a server without range support");
