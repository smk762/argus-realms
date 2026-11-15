// Cloudflare Pages doesn't answer HTTP range requests on *.pages.dev (and a custom domain only does so for file types
// the zone caches), so the arena must work with every file fetched whole. This fails if runtime code reaches for
// ranges (a PMTiles archive, a Range header, a custom tile protocol), or if a built dist breaks the Pages limits.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const arena = join(dirname(fileURLToPath(import.meta.url)), "..");
const RANGED = [/pmtiles/i, /\bRange\b/, /bytes=\d/, /Accept-Ranges/i, /addProtocol\(/];

function files(dir) {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? files(p) : [p];
  });
}

test("runtime code needs no range requests", () => {
  for (const f of files(join(arena, "src")).filter((p) => /\.(m?js|html|css|json)$/.test(p))) {
    const text = readFileSync(f, "utf8");
    for (const re of RANGED) assert.ok(!re.test(text), `${relative(arena, f)} matches ${re}`);
  }
  const pkg = JSON.parse(readFileSync(join(arena, "package.json"), "utf8"));
  assert.ok(!("pmtiles" in { ...pkg.dependencies, ...pkg.devDependencies }), "package.json still pulls in pmtiles");
});

test("a built dist fits Pages and serves tiles as plain files", { skip: !existsSync(join(arena, "dist", "manifest.json")) }, () => {
  const all = files(join(arena, "dist"));
  assert.ok(all.length < 20_000, `${all.length} files: Pages allows fewer than 20,000`);
  for (const f of all) {
    assert.ok(statSync(f).size <= 25 * 2 ** 20, `${relative(arena, f)} is over Pages' 25 MiB per file`);
    assert.ok(!f.endsWith(".pmtiles"), `${relative(arena, f)} would need range requests`);
  }
  const manifest = JSON.parse(readFileSync(join(arena, "dist", "manifest.json"), "utf8"));
  assert.match(manifest.tile_url, /^tiles\/\{style\}\/\{z\}\/\{x\}\/\{y\}\.webp$/);
  assert.ok(!("pmtiles_url" in manifest));
});
