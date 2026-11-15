// Phone layout check (local only, like playtest.mjs): portrait 390 and 320 px and a landscape phone, plus desktop.
// Starts a game, taps the map to move, opens and closes the panel sheet, the game menu, "⋯" and the credits, and checks
// the title bar is never covered, there is no sideways scroll, and every visible control is at least 44 px.
//
//   node test/mobile.mjs [http://127.0.0.1:8120/] [screenshot dir]
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

const url = process.argv[2] || "http://127.0.0.1:8120/";
const shots = process.argv[3] || "review";
mkdirSync(shots, { recursive: true });
const problems = [];
const check = (ok, what) => { console.log(`${ok ? "ok  " : "FAIL"} ${what}`); if (!ok) problems.push(what); };

const browser = await chromium.launch({ executablePath: process.env.CHROME || undefined });
const PHONE = { deviceScaleFactor: 2, isMobile: true, hasTouch: true };
for (const [name, opts] of [
  ["390", { viewport: { width: 390, height: 844 }, ...PHONE }],
  ["320", { viewport: { width: 320, height: 568 }, ...PHONE }],
  ["landscape", { viewport: { width: 844, height: 390 }, ...PHONE }],
  ["desktop", { viewport: { width: 1440, height: 900 } }],
]) {
  const ctx = await browser.newContext({ ...opts, colorScheme: "dark", reducedMotion: "reduce" });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => problems.push(`${name}: ${e.message}`));
  const shot = (state) => page.screenshot({ path: join(shots, `arena-mobile-${name}-${state}.png`) });
  await page.goto(url);
  await page.waitForSelector("dialog#start[open]", { timeout: 120_000 });
  await page.click("#start-form button[type=submit]");
  await page.waitForFunction(() => typeof lastState !== "undefined" && lastState);
  await page.waitForTimeout(1500);
  const phone = name !== "desktop";
  const hint = await page.textContent("#toast");
  check(phone ? hint.startsWith("Tap a hex") && hint.includes("long-press") : hint.startsWith("Click a hex") && hint.includes("right-click"),
    `${name}: ${phone ? "touch" : "mouse"} wording in the hint ("${hint.slice(0, 40)}…")`);

  const layout = () => page.evaluate(() => {
    const vis = (el) => el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
    const t = document.getElementById("title").getBoundingClientRect();
    const covered = [[t.left + 8, t.top + t.height / 2], [t.right - 8, t.top + t.height / 2], [(t.left + t.right) / 2, t.top + 6]]
      .some(([x, y]) => !document.getElementById("title").contains(document.elementFromPoint(x, y)));
    const small = [...document.querySelectorAll("#title button, #title select, #bar button, #bar select, #sidebar .tabs button")]
      .filter(vis).map((el) => [el.id || el.textContent.trim() || el.tagName, el.getBoundingClientRect()])
      .filter(([, r]) => r.height < 44 || r.width < 44).map(([n, r]) => `${n} ${Math.round(r.width)}x${Math.round(r.height)}`);
    const map = document.getElementById("map").getBoundingClientRect();
    const bar = document.getElementById("bar").getBoundingClientRect();
    return {
      scrollX: document.scrollingElement.scrollWidth - innerWidth, covered, small,
      sheet: vis(document.getElementById("sidebar")), attribution: vis(document.getElementById("attribution")),
      mapShare: Math.round(100 * (bar.top - t.bottom) / map.height),
    };
  });

  let l = await layout();
  if (!phone) {
    check(l.sheet && l.attribution && !(await page.isVisible("#sheet-toggle")), "desktop: side panel and credits as before, no phone toggles");
    await shot("game");
    await ctx.close();
    continue;
  }
  check(l.scrollX <= 0, `${name}: no sideways scroll (${l.scrollX})`);
  check(!l.covered, `${name}: title bar not covered`);
  check(!l.sheet && !l.attribution, `${name}: panel and credits closed by default`);
  check(l.small.length === 0, `${name}: controls at least 44 px${l.small.length ? ": " + l.small.join(", ") : ""}`);
  console.log(`  ${name}: map clear between title and bar: ${l.mapShare}% of the map's height`);
  await shot("game");

  // Tap a hex to move there.
  const target = await page.evaluate(() => {
    const me = myUnit(), here = map.project(centreOf(me.hex));
    const busy = new Set(lastState.units.filter((u) => u.on_map).map((u) => u.hex));
    const open = lastState.hexes.filter((h) => h.zone === "land" && h.playable && h.owner === null && !busy.has(h.h3))
      .map((h) => ({ h3: h.h3, p: map.project(centreOf(h.h3)) }))
      .filter(({ p }) => p.x > 20 && p.x < innerWidth - 20 && p.y > 140 && p.y < innerHeight - 160);
    open.sort((a, b) => Math.hypot(a.p.x - here.x, a.p.y - here.y) - Math.hypot(b.p.x - here.x, b.p.y - here.y));
    const t = open[1] || open[0];
    const box = document.getElementById("map").getBoundingClientRect();
    return t && { h3: t.h3, x: box.left + t.p.x, y: box.top + t.p.y };
  });
  if (target) {
    await page.touchscreen.tap(target.x, target.y);
    await page.waitForTimeout(1500);
    const moving = await page.evaluate((to) => {
      const u = myUnit();
      return u.hex === to || !!(u.path && u.path.length) || !!document.querySelector("#ctx:not([hidden])");
    }, target.h3);
    check(moving, `${name}: tap on a hex moves (or offers a menu)`);
    await page.evaluate(() => closeMenu());

    // Long-press a hex other than the one you're on (that one has nothing to offer): the menu a right-click opens.
    const other = await page.evaluate(() => {
      const me = myUnit(), box = document.getElementById("map").getBoundingClientRect();
      const ok = lastState.hexes.filter((h) => h.zone === "land" && h.playable && h.h3 !== me.hex)
        .map((h) => ({ h3: h.h3, p: map.project(centreOf(h.h3)) }))
        .filter(({ p }) => p.x > 30 && p.x < innerWidth - 30 && p.y > 150 && p.y < innerHeight - 170);
      const t = ok[Math.floor(ok.length / 2)];
      return t && { x: box.left + t.p.x, y: box.top + t.p.y };
    });
    const cdp = await ctx.newCDPSession(page);
    const pt = [{ x: other.x, y: other.y }];
    await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: pt });
    await page.waitForTimeout(800);
    await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await page.waitForTimeout(300);
    const menu = await page.evaluate(() => {
      const el = document.querySelector("#ctx");
      return el.hidden ? null : [...el.querySelectorAll("button")].map((b) => b.textContent.trim()).filter(Boolean);
    });
    check(!!(menu && menu.length), `${name}: long-press opens the hex menu (${menu ? menu.slice(0, 3).join(", ") : "nothing"})`);
    await shot("longpress");
    await page.evaluate(() => closeMenu());
  }

  await page.tap("#sheet-toggle");
  await page.waitForTimeout(300);
  l = await layout();
  check(l.sheet && !l.covered, `${name}: panel opens as a sheet, title bar still clear`);
  await page.tap('#sidebar .tabs button[data-tab="battles"]');
  await shot("sheet");
  await page.tap("#sheet-close");
  check(!(await layout()).sheet, `${name}: sheet closes`);

  await page.tap("#options-toggle");
  await page.waitForTimeout(200);
  check(await page.isVisible("#speed"), `${name}: game menu shows the options`);
  await shot("menu");
  await page.tap("#options-toggle");

  await page.tap("#more-toggle");
  await page.waitForTimeout(200);
  check(await page.isVisible("#swap-go") && await page.isVisible("#shield-opts"), `${name}: "⋯" shows class swap and shield options`);
  await page.tap("#attr-toggle");
  await page.waitForTimeout(200);
  l = await layout();
  check(l.attribution, `${name}: ⓘ shows the credits`);
  check(l.scrollX <= 0 && !l.covered, `${name}: still no sideways scroll, title clear`);
  await shot("more");
  await page.tap("#stop");
  await ctx.close();
}
await browser.close();
if (problems.length) { console.log(problems.join("\n")); process.exit(1); }
