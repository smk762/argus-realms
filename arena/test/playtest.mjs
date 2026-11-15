// A scripted play-test of the built arena in Chromium (local only; CI runs the parity test instead):
// start a game, move, let the bots act, check that territory, coins and battles change, reload and check the save.
//
//   python3 build.py [--tiles ../var/tiles] && node scripts/serve.mjs dist &
//   node test/playtest.mjs [http://127.0.0.1:8120/] [screenshot dir]
//
// CHROME=<path> uses an installed Chromium instead of Playwright's own.
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

const url = process.argv[2] || "http://127.0.0.1:8120/";
const shots = process.argv[3] || "review";
const prefix = process.env.SHOT_PREFIX || "arena";
mkdirSync(shots, { recursive: true });
const shot = (page, name) => page.screenshot({ path: join(shots, `${prefix}-${name}.png`) });
const problems = [];
const check = (ok, what) => { console.log(`${ok ? "ok  " : "FAIL"} ${what}`); if (!ok) problems.push(what); };

const browser = await chromium.launch({ executablePath: process.env.CHROME || undefined });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: "reduce" });
const page = await context.newPage();
page.on("response", (r) => { if (r.status() >= 400) problems.push(`${r.status()} ${r.url()}`); });
page.on("console", (m) => { if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) problems.push(`console: ${m.text()}`); });
page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));

// The loading card, caught mid-load on a throttled connection: light and dark at desktop width, and a 390 px phone.
for (const [name, opts] of [
  ["loading-light", { colorScheme: "light", viewport: { width: 1440, height: 900 } }],
  ["loading-dark", { colorScheme: "dark", viewport: { width: 1440, height: 900 } }],
  ["loading-390", { colorScheme: "dark", viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true }],
]) {
  const ctx = await browser.newContext(opts), p = await ctx.newPage();
  const cdp = await ctx.newCDPSession(p);
  await cdp.send("Network.enable");
  await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 40, downloadThroughput: 1.5e6, uploadThroughput: 5e5 });
  await p.goto(url);
  await p.waitForFunction(() => {
    const bar = document.querySelector("#loader-bar"), q = document.querySelector(".quote.on blockquote");
    return q && q.textContent && Number(bar.getAttribute("aria-valuenow")) > 20;
  }, null, { timeout: 120_000 });
  await p.waitForTimeout(1500); // let the first quote finish fading in
  await shot(p, name);
  const status = await p.textContent("#loading");
  check(/Downloading/.test(status), `${name}: loading card with progress ("${status.trim()}") and a quote`);
  await ctx.close();
}

const t0 = Date.now();
await page.goto(url);
await page.waitForSelector("dialog#start[open]", { timeout: 120_000 });
check(await page.isHidden("#loader"), "loading card hidden when the game is ready");
console.log(`rules engine ready in ${((Date.now() - t0) / 1000).toFixed(1)} s (cold load from localhost)`);
await page.fill("input[name=name]", "Tester");
await page.selectOption("#start-class", "barbarian");
await shot(page, "1-start");
await page.click("#start-form button[type=submit]");
await page.waitForFunction(() => typeof lastState !== "undefined" && lastState && lastState.tick >= 0);
await page.selectOption("#speed", "0.25"); // 4x
await page.dispatchEvent("#speed", "change");

const state = () => page.evaluate(() => lastState);
const first = await state();
const me0 = first.units.find((u) => u.user_id === first.you);
check(first.players.length === 9, `9 players (you + 8 bots): ${first.players.map((p) => p.name).join(", ")}`);

// Move by clicking the map: the nearest open land hex, 2+ hexes away.
const target = await page.evaluate((from) => {
  const land = lastState.hexes.filter((h) => h.zone === "land" && h.playable && h.owner === null);
  const f = map.project(centreOf(from));
  const scored = land.map((h) => { const { x, y } = map.project(centreOf(h.h3)); return { h3: h.h3, x, y, d: Math.hypot(x - f.x, y - f.y) }; });
  return scored.filter((s) => s.d > 60).sort((a, b) => a.d - b.d)[0];
}, me0.hex);
const box = await page.locator("#map canvas").boundingBox();
await page.mouse.click(box.x + target.x, box.y + target.y);
await page.waitForTimeout(1500);
let st = await state();
let me = st.units.find((u) => u.user_id === st.you);
check(me.path && me.path.length > 0 || me.hex !== me0.hex, `clicked hex ${target.h3}: unit is on its way (path ${me.path ? me.path.length : 0})`);
await shot(page, "2-moving");

// Let the game run: bots expand and fight; you claim land as you arrive.
const owned = (s) => s.hexes.filter((h) => h.owner !== null).length;
for (let i = 0; i < 6; i++) {
  await page.waitForTimeout(10_000);
  st = await state();
  me = st.units.find((u) => u.user_id === st.you);
  if (me.on_map && !me.path) { // keep claiming: send it to the nearest open land
    await page.evaluate(() => {
      const u = myUnit(), here = centreOf(u.hex);
      const open = lastState.hexes.filter((h) => h.zone === "land" && h.playable && h.owner === null);
      const d = (h) => { const c = centreOf(h.h3); return Math.hypot(c[0] - here[0], c[1] - here[1]); };
      const next = open.sort((a, b) => d(a) - d(b))[0];
      if (next) order("move", { to: next.h3 });
    });
  }
  console.log(`  tick ${st.tick}: ${owned(st)} hexes owned, ${st.battles.length} recent battles, you hold ${st.players[0].hexes}`);
}
check(st.tick > first.tick + 100, `ticks advanced: ${first.tick} -> ${st.tick}`);
check(owned(st) > owned(first), `territory changed: ${owned(first)} -> ${owned(st)} hexes owned`);
check(st.players.some((p) => p.treasury !== first.players.find((q) => q.id === p.id).treasury), "coins changed (income, fees, loot)");
check(st.units.some((u) => u.user_id !== st.you && u.hex !== first.units.find((v) => v.id === u.id).hex), "bots moved");
await shot(page, "3-playing");

// Battles and bounties: the battle log, and the paper's stories (bounties are paid to whoever kills a veteran bot).
await page.click('.tabs button[data-tab="battles"]');
await page.waitForTimeout(1200);
await shot(page, "4-battles");
const report = await page.evaluate(() => api(`/api/solo/local/report?since=0`));
check(st.battles.length > 0 || report.stories.length > 0, `battles fought (${st.battles.length} recent); paper: "${report.headline}"`);
console.log(`  stories: ${report.stories.map((s) => s.headline).join(" | ")}`);
const bounty = st.units.filter((u) => u.kills > 0).map((u) => `${st.players.find((p) => p.id === u.user_id).name} ${u.kills} kill(s)`);
console.log(`  kills (veteran bonus and bounty grow with these): ${bounty.join(", ") || "none yet"}`);
await page.click("#news");
await page.waitForSelector("dialog#report[open]");
await shot(page, "5-news");
await page.click("dialog#report button");

// Saved in localStorage: a reload restores the game where it was.
await page.waitForTimeout(3500);
const before = (await state()).tick;
await page.reload();
await page.waitForFunction(() => typeof lastState !== "undefined" && lastState && lastState.tick > 0, null, { timeout: 120_000 });
const after = await state();
check(after.tick >= before && after.players[0].name === "Tester", `reload restored the save (tick ${before} -> ${after.tick})`);
await shot(page, "6-restored");

check(await page.isHidden("#loader"), "loading card hidden once the game is ready");
const csp = problems.filter((p) => /Content Security Policy|Refused/.test(p));
check(csp.length === 0, "no CSP violations");
await browser.close();
if (problems.length) { console.log(problems.join("\n")); process.exit(1); }
