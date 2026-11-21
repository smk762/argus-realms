// Atlas Realms client: MapLibre over the painted tiles, polling one solo session.
"use strict";

const COLORS = ["#e4572e", "#4c9be8", "#f2c14e", "#8bc34a", "#b66dd8", "#46c2b4", "#f08a5d", "#c0c0c0"];
const POLL_MS = 1000;
const IDLE_MS = 5 * 60 * 1000; // auto-pause after this long without input
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* private mode */ } },
};
const $ = (id) => document.getElementById(id);

let manifest, meta, map, sid, hexes, lastState, fogCount = -1, seenResults = new Set();
// The city being renamed (its row is left alone while you type) and names sent but not yet ticked in.
let renaming = null;
let popup = null, popupUser = null; // the open player card on the map
const sentNames = new Map();

async function api(path, body, keepalive = false) {
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), keepalive,
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

function colorOf(userId) { return COLORS[(userId - 1) % COLORS.length]; }

function clock(tick) {
  const minutes = tick; // one tick is one game minute
  const day = Math.floor(minutes / 1440) + 1;
  const hh = String(Math.floor((minutes % 1440) / 60)).padStart(2, "0");
  const mm = String(minutes % 60).padStart(2, "0");
  return `Day ${day}, ${hh}:${mm}`;
}

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { el.hidden = true; }, 3500);
}

// -- map ---------------------------------------------------------------------------------------

function initMap() {
  const [w, s, e, n] = manifest.bounds;
  const padX = (e - w) * 0.6, padY = (n - s) * 0.6;
  map = new maplibregl.Map({
    container: "map",
    style: "https://tiles.openfreemap.org/styles/positron",
    bounds: [[w, s], [e, n]],
    maxBounds: [[w - padX, s - padY], [e + padX, n + padY]], // no panning off into the fog
    minZoom: 6,
    maxZoom: manifest.maxzoom, // the painted map's native detail
    attributionControl: false, // credits are in the footer (renderAttribution)
  });
  map.on("styleimagemissing", (e) => {
    if (e.id.startsWith("unit:")) map.addImage(e.id, unitIcon(e.id), { pixelRatio: 2 });
    else if (e.id.startsWith("hp:")) map.addImage(e.id, hpIcon(e.id), { pixelRatio: 2 });
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
  return new Promise((resolve) => map.on("load", resolve));
}

function addLayers() {
  const chosen = store.get("style") || manifest.styles[0].name;
  for (const s of manifest.styles) {
    map.addSource(`paint-${s.name}`, {
      type: "raster",
      // Built by hand: URL() would percent-encode the {z}/{x}/{y} placeholders MapLibre fills in.
      tiles: [`${location.origin}/${manifest.tile_url.replace("{style}", s.name)}`],
      tileSize: 256,
      minzoom: manifest.minzoom,
      maxzoom: manifest.maxzoom,
      bounds: manifest.bounds,
    });
    map.addLayer({
      id: `paint-${s.name}`, type: "raster", source: `paint-${s.name}`,
      layout: { visibility: s.name === chosen ? "visible" : "none" },
    });
  }
  // Place names from argus-atlas (the painted map has none). Under the fog, like the land they name.
  if (manifest.labels_url) {
    map.addSource("labels", { type: "geojson", data: `${location.origin}/${manifest.labels_url}` });
    map.addLayer({
      id: "labels", type: "symbol", source: "labels",
      // OSM ranks: 0 city, 1 town, 2 village; towns from zoom 8 and villages from 11, as argus-atlas cuts them.
      filter: ["<=", ["get", "rank"], ["step", ["zoom"], 0, 8, 1, 11, 9]],
      layout: {
        "text-field": ["get", "name"], "text-font": ["Noto Sans Regular"],
        "text-size": ["max", 11, ["-", 15, ["*", 2, ["get", "rank"]]]], "symbol-sort-key": ["get", "rank"],
      },
      paint: { "text-color": "#2b2116", "text-halo-color": "#f3ead7", "text-halo-width": 1.5 },
    });
  }
  map.addSource("fog", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "fog", type: "fill", source: "fog", paint: { "fill-color": "#0d0f13", "fill-opacity": 0.88 } });
  map.addSource("hexes", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addSource("route", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  // Other units cluster when zoomed out so stacks don't spam icons; yours is its own source and never clusters.
  map.addSource("units", {
    type: "geojson", data: { type: "FeatureCollection", features: [] },
    cluster: true, clusterRadius: 36, clusterMaxZoom: 10,
  });
  map.addSource("you", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  // Hexes under the fog are drawn by the fog layer; only playable ones get fills and borders.
  map.addLayer({
    id: "hex-fill", type: "fill", source: "hexes", filter: ["get", "playable"],
    paint: {
      "fill-color": ["case", ["has", "color"], ["get", "color"], "#000000"],
      "fill-opacity": ["case", ["has", "color"], 0.3, 0.0],
    },
  });
  map.addLayer({
    id: "hex-line", type: "line", source: "hexes", filter: ["get", "playable"],
    paint: {
      "line-color": ["case", ["has", "color"], ["get", "color"], "#1d1d1d"],
      "line-width": ["case", ["has", "color"], 2.5, 0.8],
      "line-opacity": 0.75,
    },
  });
  // MapLibre can't vary line-dasharray by feature, so spawn zones get their own dashed layer.
  map.addLayer({
    id: "spawn-line", type: "line", source: "hexes", filter: ["==", ["get", "zone"], "spawn"],
    paint: { "line-color": "#ffffff", "line-width": 2.5, "line-dasharray": [2, 1] },
  });
  map.addLayer({
    id: "route", type: "line", source: "route",
    paint: { "line-color": "#ffffff", "line-width": 2, "line-dasharray": [2, 2] },
  });
  map.addLayer({
    id: "unit-clusters", type: "circle", source: "units", filter: ["has", "point_count"],
    paint: {
      "circle-color": "#1b1e24",
      "circle-radius": ["step", ["get", "point_count"], 13, 4, 16, 8, 19],
      "circle-stroke-color": "#e8e4da", "circle-stroke-width": 2,
    },
  });
  map.addLayer({
    id: "unit-cluster-count", type: "symbol", source: "units", filter: ["has", "point_count"],
    layout: { "text-field": ["get", "point_count_abbreviated"], "text-size": 13, "text-font": ["Noto Sans Regular"] },
    paint: { "text-color": "#ffffff" },
  });
  const unitLayout = {
    "icon-image": ["get", "icon"], "icon-allow-overlap": true, "icon-ignore-placement": true,
    // Names only once zoomed in far enough to read them.
    "text-field": ["step", ["zoom"], "", 9, ["get", "label"]],
    "text-size": 12, "text-offset": [0, 2.1], "text-anchor": "top",
    "text-font": ["Noto Sans Regular"], "text-allow-overlap": true,
  };
  const unitPaint = { "text-color": "#ffffff", "text-halo-color": "#000000", "text-halo-width": 1.5 };
  // Health bars sit just under each sprite, as their own layer because a symbol carries one icon.
  const hpLayout = {
    "icon-image": ["get", "hpIcon"], "icon-offset": [0, 13], "icon-anchor": "top",
    "icon-allow-overlap": true, "icon-ignore-placement": true,
  };
  map.addLayer({ id: "unit-hp", type: "symbol", source: "units", filter: ["!", ["has", "point_count"]], layout: hpLayout });
  map.addLayer({ id: "you-hp", type: "symbol", source: "you", layout: { ...hpLayout, "icon-offset": [0, 17] } });
  map.addLayer({ id: "units", type: "symbol", source: "units", filter: ["!", ["has", "point_count"]], layout: unitLayout, paint: unitPaint });
  map.addLayer({ id: "you", type: "symbol", source: "you", layout: unitLayout, paint: unitPaint });
  // The card closes on a click anywhere but a sprite (MapLibre's own closeOnClick would also fire on those).
  map.on("click", (e) => {
    if (popup && !map.queryRenderedFeatures(e.point, { layers: ["units", "you"] }).length) popup.remove();
  });
  for (const layer of ["units", "you"]) {
    map.on("click", layer, (e) => showPlayer(e.features[0].properties.user_id));
    map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "help"; });
    map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; });
  }
  map.on("click", "unit-clusters", async (e) => {
    const f = e.features[0];
    const zoom = await map.getSource("units").getClusterExpansionZoom(f.properties.cluster_id);
    map.easeTo({ center: f.geometry.coordinates, zoom: Math.min(zoom + 0.5, manifest.maxzoom) });
  });
  map.on("mouseenter", "unit-clusters", () => { map.getCanvas().style.cursor = "zoom-in"; });
  map.on("mouseleave", "unit-clusters", () => { map.getCanvas().style.cursor = ""; });
  map.on("click", "hex-fill", (e) => {
    // A click on a cluster badge zooms in, and one on a sprite opens its card (above), instead of moving there.
    if (map.queryRenderedFeatures(e.point, { layers: ["unit-clusters", "units", "you"] }).length) return;
    const f = e.features[0];
    if (!f) return;
    // Plain ground: just go. Anything with someone on it, or a city: ask what to do.
    if (hexMenu(f.properties.h3, e.point, false) === "move") order("move", { to: f.properties.h3 });
  });
  map.on("contextmenu", "hex-fill", (e) => {
    e.preventDefault();
    if (e.features[0]) hexMenu(e.features[0].properties.h3, e.point, true);
  });
  map.on("movestart", closeMenu);
  map.on("mouseenter", "hex-fill", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "hex-fill", () => { map.getCanvas().style.cursor = ""; });
}

function setStyle(name) {
  for (const s of manifest.styles) {
    map.setLayoutProperty(`paint-${s.name}`, "visibility", s.name === name ? "visible" : "none");
  }
  store.set("style", name);
}

function centreOf(h3) {
  const f = hexes.get(h3);
  return f ? [f.properties.lng, f.properties.lat] : null;
}

function panTo(h3) {
  const at = centreOf(h3);
  if (at) map.flyTo({ center: at, zoom: Math.min(Math.max(map.getZoom(), 10), manifest.maxzoom), speed: 1.6 });
}

function draw(st) {
  const byHex = new Map(st.hexes.map((h) => [h.h3, h]));
  const features = [];
  for (const [id, f] of hexes) {
    const h = byHex.get(id);
    const props = { ...f.properties, playable: h.playable, owner: h.owner };
    if (h.owner) props.color = colorOf(h.owner);
    features.push({ ...f, properties: props });
  }
  map.getSource("hexes").setData({ type: "FeatureCollection", features });

  // Units stacked on one hex are fanned out around its centre.
  const stacks = new Map();
  const names = new Map(st.players.map((p) => [p.id, p.name]));
  const looks = new Map(st.players.map((p) => [p.id, p.shield_look]));
  const points = [];
  for (const u of st.units.filter((u) => u.on_map)) {
    const i = stacks.get(u.hex) || 0;
    stacks.set(u.hex, i + 1);
    const [lng, lat] = centreOf(u.hex);
    const a = i * 2.1, r = i ? 0.045 : 0;
    points.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: [lng + r * Math.cos(a) * 1.3, lat + r * Math.sin(a)] },
      properties: {
        // A shielded unit sits in a bubble, in its player's shield look.
        icon: `unit:${u.class}:${colorOf(u.user_id)}:${u.user_id === st.you ? "you" : "bot"}:${u.shield ? looks.get(u.user_id) || meta.shield_looks.plain : ""}`,
        you: u.user_id === st.you,
        user_id: u.user_id,
        hpIcon: ((h) => `hp:${Math.round(h.pct / 5) * 5}:${u.user_id === st.you ? 34 : 28}:${h.colour}`)(health(u)),
        label: `${names.get(u.user_id)}${u.kills ? ` ★${u.kills}` : ""}`,
      },
    });
  }
  map.getSource("units").setData({ type: "FeatureCollection", features: points.filter((p) => !p.properties.you) });
  map.getSource("you").setData({ type: "FeatureCollection", features: points.filter((p) => p.properties.you) });

  const me = st.units.find((u) => u.user_id === st.you);
  const route = me && me.on_map && me.path.length
    ? [{ type: "Feature", geometry: { type: "LineString", coordinates: [me.hex, ...me.path].map(centreOf) } }]
    : [];
  map.getSource("route").setData({ type: "FeatureCollection", features: route });
}

// -- unit sprites ------------------------------------------------------------------------------
// A token tinted in the player's colour with a white emblem for the unit's class. Yours is a shield
// with a gold rim; bots are round. Drawn on a canvas on first use, so there are no sprite files.

const EMBLEMS = {
  barbarian(c) { // axe
    c.lineWidth = 3; c.beginPath(); c.moveTo(-6, 9); c.lineTo(5, -8); c.stroke();
    c.beginPath(); c.moveTo(1, -10); c.quadraticCurveTo(12, -9, 9, 2); c.lineTo(3, -4); c.closePath(); c.fill();
  },
  marine(c) { // anchor
    c.lineWidth = 2.5; c.beginPath(); c.moveTo(0, -6); c.lineTo(0, 9); c.moveTo(-5, -3); c.lineTo(5, -3); c.stroke();
    c.beginPath(); c.arc(0, -8.5, 2.5, 0, 2 * Math.PI); c.stroke();
    c.beginPath(); c.arc(0, 2, 8, 0.15 * Math.PI, 0.85 * Math.PI); c.stroke();
  },
  medic(c) { // cross
    c.fillRect(-2.5, -9, 5, 18); c.fillRect(-9, -2.5, 18, 5);
  },
  saboteur(c) { // dagger
    c.beginPath(); c.moveTo(0, -11); c.lineTo(3, 2); c.lineTo(-3, 2); c.closePath(); c.fill();
    c.fillRect(-6, 2, 12, 2.5); c.fillRect(-1.5, 4.5, 3, 6);
  },
  tank(c) { // hull, turret, barrel
    c.fillRect(-9, 1, 18, 6); c.fillRect(-5, -4, 9, 6); c.fillRect(3, -2.5, 8, 2.5);
  },
};

function unitIcon(id) {
  const cv = unitCanvas(id);
  return cv.getContext("2d").getImageData(0, 0, cv.width, cv.height);
}

// The same sprite as an <img> source for the sidebar, drawn once per class and colour.
const iconUrls = new Map();
function unitIconUrl(id) {
  if (!iconUrls.has(id)) iconUrls.set(id, unitCanvas(id).toDataURL());
  return iconUrls.get(id);
}

function unitCanvas(id) {
  const [, cls, color, who, halo] = id.split(":");
  const you = who === "you", token = you ? 40 : 32, r = token / 2 - 2, size = halo ? token + 12 : token;
  const cv = document.createElement("canvas");
  cv.width = cv.height = size * 2;
  const c = cv.getContext("2d");
  c.scale(2, 2);
  c.translate(size / 2, size / 2);
  if (halo) {
    c.beginPath(); c.arc(0, 0, size / 2 - 1.5, 0, 2 * Math.PI);
    c.fillStyle = `${halo}40`; c.fill();
    c.lineWidth = 2.5; c.strokeStyle = halo; c.stroke();
  }
  c.beginPath();
  if (you) { // shield
    c.moveTo(-r, -r * 0.85); c.lineTo(r, -r * 0.85); c.lineTo(r, 0);
    c.quadraticCurveTo(r * 0.9, r * 0.8, 0, r); c.quadraticCurveTo(-r * 0.9, r * 0.8, -r, 0); c.closePath();
  } else {
    c.arc(0, 0, r, 0, 2 * Math.PI);
  }
  c.fillStyle = color; c.fill();
  c.lineWidth = you ? 3 : 2; c.strokeStyle = you ? "#e0a84f" : "#14161a"; c.stroke();
  c.fillStyle = c.strokeStyle = "#ffffff"; c.lineCap = "round";
  if (you) c.scale(1.15, 1.15);
  (EMBLEMS[cls] || EMBLEMS.barbarian)(c);
  return cv;
}

// -- health bars -------------------------------------------------------------------------------
// One colour scale everywhere: blue while shielded (the bar shows what's left of the shield), then
// green, orange and red by HP.

const HP_COLOURS = { shield: "#4aa3ff", good: "#6cbf6c", hurt: "#f08a24", bad: "#d9534f" };

function health(u) {
  if (u.shield > 0) {
    return { pct: (100 * u.shield) / u.shield_max, colour: HP_COLOURS.shield, text: `shield ${u.shield} · ${u.hp} HP` };
  }
  const colour = u.hp > 50 ? HP_COLOURS.good : u.hp > 25 ? HP_COLOURS.hurt : HP_COLOURS.bad;
  return { pct: u.hp, colour, text: `${u.hp} HP` };
}

// A small bar image under a sprite, one per 5%: "hp:<pct>:<width>:<colour>".
function hpIcon(id) {
  const [, pct, w, colour] = id.split(":");
  const cv = document.createElement("canvas");
  cv.width = w * 2; cv.height = 10;
  const c = cv.getContext("2d");
  c.scale(2, 2);
  c.fillStyle = "#14161a"; c.fillRect(0, 0, w, 5);
  c.fillStyle = colour; c.fillRect(1, 1, ((w - 2) * pct) / 100, 3);
  return c.getImageData(0, 0, cv.width, cv.height);
}

function hpBar(u, text) {
  const { pct, colour, text: label } = health(u);
  const bar = document.createElement("div");
  bar.className = "hp";
  bar.innerHTML = "<div></div><span></span>";
  bar.children[0].style.width = `${pct}%`;
  bar.children[0].style.background = colour;
  bar.children[1].textContent = text ?? label;
  return bar;
}

function duration(minutes) { return minutes >= 60 ? `${Math.ceil(minutes / 60)} h` : `${minutes} min`; }

// What killing a bot pays: its bounty, from its purse (bounty_base + bounty_per_kill per kill, capped).
function lootOf(p, u) { return Math.min(p.treasury, 5 + 5 * u.kills, 50); }

// -- player card -------------------------------------------------------------------------------

function showPlayer(userId) {
  popupUser = userId;
  if (!popup) {
    popup = new maplibregl.Popup({ closeButton: true, closeOnClick: false, maxWidth: "260px", offset: 22, className: "card" });
    popup.on("close", () => { popupUser = null; popup = null; });
  }
  if (!updatePlayer(lastState) && popup) popup.remove(); // updatePlayer may have closed it already
}

// Refreshes the open card from the latest state; false when there is nothing to show.
function updatePlayer(st) {
  if (!popup || popupUser == null || !st) return false;
  const p = st.players.find((q) => q.id === popupUser);
  const u = st.units.find((q) => q.user_id === popupUser);
  if (!p || !u || !u.on_map) { popup.remove(); return false; }
  const you = p.id === st.you;
  const el = document.createElement("div");
  el.innerHTML = `<div class="row"><strong></strong><span class="tag"></span></div><div class="small blurb"></div>
    <div class="facts small"></div>`;
  const [head, blurb, facts] = el.children;
  head.children[0].textContent = you ? `${p.name} (you)` : p.name;
  head.children[0].style.color = colorOf(p.id);
  head.children[1].textContent = u.class;
  blurb.textContent = p.personality || "";
  blurb.hidden = !p.personality;
  el.insertBefore(hpBar(u), facts);
  const bonus = u.veteran ? Math.min(0.05 * u.kills, 0.3) : 0;
  const lines = [
    u.shield ? `shielded: ${u.shield}/${u.shield_max}, ${duration(u.shield_left)} left today` : null,
    `${p.hexes} hex · ${p.treasury.toFixed(1)} coins`,
    `defence +${Math.round(u.defence_bonus * 100)}% · attack +${Math.round(u.attack_bonus * 100)}%`,
    u.kills ? `${u.kills} kill${u.kills === 1 ? "" : "s"}${bonus ? ` · veteran +${Math.round(bonus * 100)}%` : ""}` : null,
    p.bot ? `bounty ${lootOf(p, u).toFixed(0)} coins` : null,
    u.mode !== "none" ? `mode: ${u.mode.replace("_", " ")}` : u.path.length ? "on the move" : null,
  ];
  facts.replaceChildren(...lines.filter(Boolean).map((t) => Object.assign(document.createElement("div"), { textContent: t })));
  if (p.bot) {
    const h = document.createElement("button");
    h.className = "attack";
    h.textContent = `Hire ${p.name}…`;
    h.onclick = () => { popup.remove(); openHire(p.id); };
    el.append(h);
  }
  if (!you) {
    const go = document.createElement("button");
    go.className = "attack";
    go.textContent = "Attack";
    const me = st.units.find((q) => q.user_id === st.you);
    go.disabled = me.shield > 0;
    go.title = me.shield > 0 ? "You can't attack while your shield is up" : "Follow them and attack (only on land)";
    go.onclick = () => { order("pursue", { user: u.user_id }); popup.remove(); };
    el.append(go);
  }
  popup.setLngLat(centreOf(u.hex)).setDOMContent(el);
  if (!popup.isOpen()) popup.addTo(map);
  return true;
}

// -- panel -------------------------------------------------------------------------------------

function renderPanel(st) {
  const names = new Map(st.players.map((p) => [p.id, p.name]));
  $("clock").textContent = clock(st.tick);
  const me = st.units.find((u) => u.user_id === st.you);
  const you = st.players.find((p) => p.id === st.you);
  $("me-name").textContent = you.name;
  $("me-class").textContent = me.class;
  const hp = health(me);
  $("me-hp").textContent = hp.text;
  $("me-hp-bar").style.width = `${hp.pct}%`;
  $("me-hp-bar").style.background = hp.colour;
  // Shield: the same rule for everyone, toggled within a daily budget.
  const sh = $("shield"), on = me.shield > 0;
  sh.classList.toggle("on", on);
  sh.textContent = on ? `Shield ${duration(me.shield_left)}` : me.shield_ready_in ? `Shield in ${duration(me.shield_ready_in)}` : "Shield";
  sh.disabled = !on && (me.shield_ready_in > 0 || !me.on_map);
  sh.title = on
    ? `Shielded: ${me.shield} of ${me.shield_max} left, ${duration(me.shield_left)} of today's shield to go. `
      + `You can't attack while it's up. Click to drop it.`
    : `Raise a shield of ${me.shield_max} HP (${duration(me.shield_left)} left today). You can't attack while it's up.`;
  sh.onclick = () => order("shield", { on: !on });
  $("me-status").textContent = me.on_map
    ? `defence +${Math.round(me.defence_bonus * 100)}% · attack +${Math.round(me.attack_bonus * 100)}%`
    : `respawning in ${me.respawn_in} min`;
  if (me.pursue) $("me-status").textContent += ` · ${me.hunting ? "hunt mode" : "hunting"}: ${st.players.find((p) => p.id === me.pursue).name}`;
  else if (me.hunting) $("me-status").textContent += " · hunt mode";
  // On someone else's city: say how the capture is going, or why it can't happen.
  const cap = st.capture, status = $("me-status");
  if (cap) {
    const whose = `${names.get(cap.owner)}'s city`;
    status.textContent += you.hexes >= st.hex_cap ? ` · at your ${st.hex_cap}-hex cap: can't take ${whose}`
      : cap.garrison ? ` · ${whose}: garrison ${cap.garrison} still standing`
      : ` · capturing ${whose}: ${cap.progress}/${cap.need} min`;
  }
  $("me-coins").textContent = `${you.treasury.toFixed(1)} coins · ${you.hexes} hex`;
  for (const b of document.querySelectorAll("#modes [data-mode]")) b.classList.toggle("on", b.dataset.mode === me.mode);
  $("swap-go").title = me.battles_since_class < 1 ? "Fight one battle before you can swap" : "";
  $("pause").textContent = st.paused ? "Resume" : "Pause";
  $("speed").value = String(st.tick_every);

  const units = new Map(st.units.map((u) => [u.user_id, u]));
  $("players").replaceChildren(...[...st.players].sort((a, b) => b.hexes - a.hexes).map((p) => {
    const u = units.get(p.id);
    const li = document.createElement("li");
    li.className = u.on_map ? "" : "dead";
    // Their unit, or while it respawns, one of their hexes.
    const where = u.on_map ? u.hex : (st.hexes.find((h) => h.owner === p.id) || {}).h3;
    if (where) { li.classList.add("go"); li.title = "Show on map"; li.onclick = () => { panTo(where); if (u.on_map) showPlayer(p.id); }; }
    li.innerHTML = `<img class="cls" alt=""><span></span><span class="small"></span><span class="who"></span>`;
    li.append(hpBar(u, u.on_map ? null : `respawning in ${u.respawn_in} min`));
    const icon = li.children[0];
    icon.src = unitIconUrl(`unit:${u.class}:${colorOf(p.id)}:bot`);
    icon.alt = u.class;
    icon.title = `${u.class[0].toUpperCase()}${u.class.slice(1)}: ${describe(meta.classes[u.class])}`;
    li.children[1].textContent = p.id === st.you ? `${p.name} (you)` : p.name;
    li.children[2].textContent = `${p.hexes} hex${u.kills ? ` · ★${u.kills}` : ""}${p.bot ? ` · loot ${lootOf(p, u).toFixed(0)}` : ""}`
      + (p.targeted_by.length ? ` · ◎${p.targeted_by.length}` : "");
    if (p.targeted_by.length) li.children[2].title = `Nemesis of ${p.targeted_by.map((h) => names.get(h)).join(", ")}`;
    li.children[3].textContent = p.personality || (u.shield ? "shielded" : "");
    return li;
  }));

  renderBattles(st, names);
  renderNemesis(st, names);
  renderContracts(st, names);

  renderCities(st, names);

  for (const [id, r] of Object.entries(st.results)) {
    if (seenResults.has(id)) continue;
    seenResults.add(id);
    if (r !== "ok") toast(`Order refused: ${r}`);
  }
}

// -- map context menu ------------------------------------------------------------------------------
// What a click on a hex can mean: move there, attack (follow) one of the units on it, besiege a city,
// or look after your own city. Returns "move" when the hex is plain ground and the menu wasn't forced.

function closeMenu() { $("ctx").hidden = true; }

function hexMenu(h3, point, force) {
  const st = lastState;
  if (!st) return null;
  const me = myUnit(), names = new Map(st.players.map((p) => [p.id, p.name]));
  const hex = st.hexes.find((h) => h.h3 === h3);
  const here = st.units.filter((u) => u.on_map && u.hex === h3 && u.user_id !== st.you);
  const city = st.cities.find((c) => c.h3 === h3);
  const enemyCity = hex.owner && hex.owner !== st.you;
  if (!force && !here.length && !city && !enemyCity) return "move";

  const items = [];
  const add = (label, run, why = null, info = null) => items.push({ label, run, why, info });
  const shielded = me.shield > 0 ? "You can't attack while your shield is up" : null;
  if (me.on_map && me.hex !== h3) add("Move here", () => order("move", { to: h3 }),
    here.length && shielded ? shielded : null);
  for (const u of [...here].sort((a, b) => b.hp - a.hp)) {
    const who = names.get(u.user_id);
    add(`Attack ${who} (${u.hp} HP${u.shield ? `, shield ${u.shield}` : ""})`, () => order("pursue", { user: u.user_id }),
      shielded || (!me.on_map ? "You're respawning" : null), () => showPlayer(u.user_id));
  }
  const owner = st.players.find((p) => p.id === hex.owner);
  if (owner && owner.bot && st.city_prices[h3] != null) {
    add(`Buy this city from ${owner.name} (${st.city_prices[h3].toFixed(1)} coins)`, () => order("buy_city", { hex: h3 }),
      you().treasury < st.city_prices[h3] ? "Not enough coins" : you().hexes >= st.hex_cap ? `At your ${st.hex_cap}-hex cap` : null);
  }
  for (const u of here) {
    if (st.players.find((p) => p.id === u.user_id).bot) add(`Hire ${names.get(u.user_id)}…`, () => openHire(u.user_id));
  }
  if (enemyCity && hex.garrison && !here.length) {
    add(`Besiege ${names.get(hex.owner)}'s city (garrison ${hex.garrison})`, () => order("move", { to: h3 }), shielded);
  }
  if (city) {
    if (city.repair_cost > 0) add(`Repair (${city.repair_cost.toFixed(1)} coins)`, () => order("repair", { hex: h3 }));
    add("Rename…", () => {
      showTab("cities");
      const row = document.querySelector(`#cities li[data-h3="${h3}"] .rename`);
      if (row) row.click();
    });
    add("Set as capital", () => {}, city.capital ? "Already your capital" : "Capitals come with M4 (user capitals)");
  }
  if (!items.length) return null;

  const menu = $("ctx");
  menu.replaceChildren(...items.map((it) => {
    const row = document.createElement("div");
    const b = document.createElement("button");
    b.textContent = it.label;
    b.disabled = !!it.why;
    if (it.why) b.title = it.why;
    b.onclick = () => { closeMenu(); it.run(); };
    row.append(b);
    if (it.info) {
      const i = document.createElement("button");
      i.className = "info";
      i.textContent = "i";
      i.title = "About this player";
      i.onclick = () => { closeMenu(); it.info(); };
      row.append(i);
    }
    return row;
  }));
  const box = map.getContainer().getBoundingClientRect();
  menu.hidden = false;
  menu.style.left = `${Math.min(point.x, box.width - menu.offsetWidth - 8)}px`;
  menu.style.top = `${Math.min(point.y, box.height - menu.offsetHeight - 8)}px`;
  return "menu";
}

// -- nemesis -------------------------------------------------------------------------------------
// A subscriber's nemesis is public: the table lists every target and who is hunting it.

function nemesisName(n, names) { return n.kind === "clan" ? `Clan ${n.id}` : names.get(n.id); }

function renderNemesis(st, names) {
  const you = st.players.find((p) => p.id === st.you), me = myUnit(), patron = you.subscriber;
  const box = $("my-nemesis");
  box.classList.toggle("locked", !patron);
  const pick = $("nemesis-pick");
  if (document.activeElement !== pick) {
    const options = [new Option("— none —", "")];
    for (const p of st.players) {
      if (p.id !== st.you && (p.bot || p.subscriber)) options.push(new Option(p.name, `user:${p.id}`));
    }
    const clans = new Set(st.players.map((p) => p.clan).filter((c) => c != null && c !== you.clan));
    for (const c of clans) options.push(new Option(`Clan ${c}`, `clan:${c}`));
    pick.replaceChildren(...options);
    pick.value = you.nemesis ? `${you.nemesis.kind}:${you.nemesis.id}` : "";
  }
  const wait = you.nemesis_ready_in;
  pick.disabled = !patron;
  $("nemesis-set").disabled = !patron;
  $("nemesis-set").title = !patron ? "Patron perk" : wait ? `You can change it again in ${duration(wait)}` : "";
  const hunt = $("hunt");
  hunt.disabled = !patron || !you.nemesis || (!me.hunting && (!me.hunt_left || me.shield > 0 || !me.on_map));
  hunt.classList.toggle("on", me.hunting);
  hunt.textContent = me.hunting ? `Hunting · ${duration(me.hunt_left)}` : "Hunt";
  hunt.title = !patron ? "Patron perk"
    : me.shield > 0 ? "You can't attack while your shield is up"
    : `Chase your nemesis for up to 1 h a game day (${duration(me.hunt_left)} left today)`;
  $("nemesis-note").textContent = you.nemesis
    ? `Hunting ${nemesisName(you.nemesis, names)}${wait ? ` · change in ${duration(wait)}` : ""}` : "No nemesis set.";

  const rows = st.nemesis_table.map((r) => {
    const tr = document.createElement("tr");
    tr.innerHTML = "<td></td><td></td><td></td>";
    tr.children[0].textContent = nemesisName(r, names) + (r.kind === "user" && r.id === st.you ? " (you)" : "");
    tr.children[1].textContent = r.hunters.map((h) => (h === st.you ? "you" : names.get(h))).join(", ");
    tr.children[2].textContent = r.hunters.length;
    return tr;
  });
  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = '<td colspan="3" class="small">Nobody has a nemesis yet.</td>';
    rows.push(tr);
  }
  $("nemesis-table").tBodies[0].replaceChildren(...rows);
}

function wireNemesis() {
  $("nemesis-set").onclick = () => {
    const [kind, id] = $("nemesis-pick").value.split(":");
    order("nemesis", kind ? { [kind]: Number(id) } : {});
  };
  $("hunt").onclick = () => order("hunt", { on: !(myUnit() || {}).hunting });
}

// -- bot services ---------------------------------------------------------------------------------

const SERVICE = {
  merc: { name: "Mercenary", time: "15 min", what: "hunts your nemesis or a target you pick" },
  guard: { name: "Guard", time: "1 h", what: "patrols your land and attacks what comes near it" },
  protect: { name: "Protection", time: "24 h", what: "won't fight you or take your land" },
};

function openHire(bot) {
  const st = lastState, names = new Map(st.players.map((p) => [p.id, p.name]));
  const me = you(), info = st.bots_for_hire[bot];
  $("hire-title").textContent = `Hire ${names.get(bot)}`;
  const hunting = st.contracts.find((c) => c.bot === bot && c.status === "active" && c.kind === "merc" && c.target === st.you);
  $("hire-note").textContent = hunting
    ? `${names.get(bot)} is hunting you for ${names.get(hunting.hirer)}. Offer ${(2 * hunting.price).toFixed(1)} coins and it switches sides.`
    : info.free_in ? `Busy for ${duration(info.free_in)}: a mercenary or guard job would queue. Protection starts now.`
    : "Free now.";
  $("hire-factor").textContent = st.price_factor;
  const target = $("hire-target");
  const others = st.players.filter((p) => p.id !== st.you && p.id !== bot);
  target.replaceChildren(
    ...(me.nemesis ? [new Option(`Your nemesis (${nemesisName(me.nemesis, names)})`, "")] : []),
    ...others.map((p) => new Option(p.name, String(p.id))),
  );
  if (hunting) target.value = String(hunting.hirer);
  const when = () => {
    if ($("hire-when").value !== "at") return {};
    const day = 1440, now = st.tick, wanted = minutesOf($("hire-at").value);
    let at = now - (now % day) + wanted;
    if (at <= now) at += day;
    return { start: at };
  };
  $("hire-options").replaceChildren(...Object.entries(SERVICE).map(([kind, s]) => {
    // A buy-off offers whole coins, so it is checked (and shown) at the rounded-up amount it will send.
    const price = hunting && kind === "merc" ? Math.ceil(2 * hunting.price) : info.quotes[kind];
    const row = document.createElement("div");
    row.className = "hire-row";
    row.innerHTML = "<div><strong></strong> <span class='small'></span><div class='small'></div></div><button type='button'></button>";
    row.querySelector("strong").textContent = s.name;
    row.querySelector("span").textContent = s.time;
    row.querySelector(".small:not(span)").textContent = `${names.get(bot)} ${s.what}.`;
    const b = row.querySelector("button");
    b.textContent = hunting && kind === "merc" ? `Buy off · ${price.toFixed(1)}` : `${price.toFixed(1)} coins`;
    b.disabled = me.treasury < price;
    if (b.disabled) b.title = "Not enough coins";
    b.onclick = () => {
      const payload = { bot, kind, ...when() };
      if (kind === "merc" && target.value) payload.target = Number(target.value);
      if (hunting && kind === "merc") payload.offer = price;
      order("hire", payload);
      $("hire").close();
    };
    return row;
  }));
  $("hire").showModal();
}

function renderContracts(st, names) {
  const mine = st.contracts.filter((c) => c.hirer === st.you || c.target === st.you);
  if (!mine.length) {
    const li = document.createElement("li");
    li.textContent = "No contracts. Click a bot to hire it.";
    $("contracts").replaceChildren(li);
    return;
  }
  $("contracts").replaceChildren(...mine.map((c) => {
    const li = document.createElement("li");
    const s = SERVICE[c.kind];
    const against = c.target != null ? ` vs ${c.target === st.you ? "you" : names.get(c.target)}` : "";
    const when = c.status === "active" ? `${duration(c.ends_in)} left`
      : c.status === "queued" ? (c.starts_in ? `starts in ${duration(c.starts_in)}` : "queued") : c.status;
    li.textContent = c.hirer === st.you
      ? `${names.get(c.bot)}: ${s.name.toLowerCase()}${against} · ${when}`
      : `${names.get(c.bot)} hired by ${names.get(c.hirer)} to hunt you · ${when}`;
    li.classList.toggle("against", c.hirer !== st.you);
    if (c.hirer === st.you && c.status === "queued") {
      const x = document.createElement("button");
      x.className = "revenge";
      x.textContent = "Cancel";
      x.title = "Cancel for a full refund";
      x.onclick = () => order("cancel_contract", { id: c.id });
      li.append(x);
    } else if (c.hirer !== st.you && c.status === "active") {
      const x = document.createElement("button");
      x.className = "revenge";
      x.textContent = "Buy off";
      x.onclick = () => openHire(c.bot);
      li.append(x);
    }
    return li;
  }));
}

// -- battles ----------------------------------------------------------------------------------------
// "Recent" is everyone's last battles; "Mine" (a patron perk) is every battle you were in, with its replay.
// A battle where you were attacked has a Revenge button: your unit follows the attacker until it catches it.

let battleView = "recent", openReplay = null;

function battleLine(b, names) {
  const won = b.outcome === "attacker_won" ? names.get(b.attacker) : b.outcome === "defender_won" ? names.get(b.defender) : null;
  const defender = b.garrison ? `${names.get(b.defender)}'s garrison` : names.get(b.defender);
  const winner = b.outcome === "defender_won" && b.garrison ? "the garrison" : won;
  return `${clock(b.tick).split(", ")[1]} ${names.get(b.attacker)} → ${defender}: `
    + (winner ? `${winner} won` : "both wounded") + ` (${b.rolls} rolls${b.shield ? `, shield took ${b.shield}` : ""})`;
}

function revengeButton(st, b, names) {
  const me = myUnit(), them = st.units.find((u) => u.user_id === b.attacker);
  if (b.defender !== st.you || b.attacker === st.you || !them) return null;
  const btn = document.createElement("button");
  btn.className = "revenge";
  const chasing = me.pursue === b.attacker;
  btn.textContent = chasing ? "Hunting" : "Revenge";
  btn.classList.toggle("on", chasing);
  btn.disabled = chasing || !them.on_map || !me.on_map || me.shield > 0;
  btn.title = me.shield > 0 ? "You can't attack while your shield is up"
    : !them.on_map ? `${names.get(b.attacker)} is respawning` : `Follow ${names.get(b.attacker)} and attack`;
  btn.onclick = (e) => { e.stopPropagation(); order("pursue", { user: b.attacker }); };
  return btn;
}

function replayBox(b, names) {
  const r = b.replay, box = document.createElement("div");
  box.className = "replay";
  const mods = (m) => Object.entries(m).map(([k, v]) => `${k.replace(/_/g, " ")} ${v >= 0 ? "+" : ""}${Math.round(v * 100)}%`).join(", ") || "none";
  const head = document.createElement("div");
  head.textContent = `${names.get(b.attacker)} ${r.attacker_hp} HP (${mods(r.attacker_mods)}) vs `
    + `${b.garrison ? "garrison" : names.get(b.defender)} ${r.defender_hp} HP (${mods(r.defender_mods)})`;
  const ol = document.createElement("ol");
  ol.append(...r.rolls.map(([ad, dd, as, ds, w, dmg]) => {
    const li = document.createElement("li");
    li.textContent = `${ad} → ${as} vs ${dd} → ${ds}: ` + (w ? `${w === "attacker" ? "defender" : "attacker"} −${dmg}` : "tie");
    return li;
  }));
  box.append(head, ol);
  return box;
}

function renderBattles(st, names) {
  const patron = st.history !== null;
  for (const b of document.querySelectorAll("#battle-views button")) {
    b.classList.toggle("on", b.dataset.view === battleView);
    if (b.dataset.view === "mine") {
      b.disabled = !patron;
      b.title = patron ? "Every battle you were in, with replays" : "Patron perk: your full battle history with replays";
    }
  }
  const mine = battleView === "mine" && patron;
  const list = mine ? st.history : st.battles;
  if (!list.length) {
    const li = document.createElement("li");
    li.textContent = "No battles yet.";
    $("battles").replaceChildren(li);
    return;
  }
  $("battles").replaceChildren(...list.map((b) => {
    const li = document.createElement("li");
    const key = `${b.tick}:${b.attacker}:${b.hex}`;
    const line = document.createElement("span");
    line.textContent = battleLine(b, names);
    li.append(line);
    const rv = revengeButton(st, b, names);
    if (rv) li.append(rv);
    if (mine) {
      li.classList.add("go");
      li.onclick = () => { openReplay = openReplay === key ? null : key; renderBattles(lastState, names); };
      if (openReplay === key) li.append(replayBox(b, names));
    }
    return li;
  }));
}

function renderCities(st, names) {
  const total = st.cities.reduce((a, c) => a + c.income_per_hour, 0);
  const cost = st.cities.reduce((a, c) => a + c.repair_cost, 0);
  const summary = $("cities-summary");
  summary.replaceChildren(`${st.cities.length} cities · ${total.toFixed(1)} coins/h`);
  if (cost > 0) {
    const all = document.createElement("button");
    all.textContent = `Repair all (${cost.toFixed(1)})`;
    all.onclick = () => { for (const c of st.cities) if (c.repair_cost > 0) order("repair", { hex: c.h3 }); };
    summary.append(all);
  }
  if (renaming && st.cities.some((c) => c.h3 === renaming)) return;
  renaming = null;
  if (!st.cities.length) {
    const li = document.createElement("li");
    li.className = "small";
    li.textContent = "No cities yet. Move onto open land to claim it.";
    $("cities").replaceChildren(li);
    return;
  }
  // Unnamed cities are numbered in the order you took them, so the numbers don't shuffle as income changes.
  const number = new Map([...st.cities].sort((a, b) => a.since - b.since || (a.h3 < b.h3 ? -1 : 1)).map((c, i) => [c.h3, i + 1]));
  $("cities").replaceChildren(...st.cities.map((c) => {
    if (sentNames.get(c.h3) === (c.name || "")) sentNames.delete(c.h3);
    const name = sentNames.has(c.h3) ? sentNames.get(c.h3) : c.name;
    const li = document.createElement("li");
    li.innerHTML = `<div class="row"><span class="city-title"><strong class="city-name"></strong><button class="rename" title="Rename">✎</button></span><span class="small"></span></div>
      <div class="hp"><div></div><span></span></div>
      <div class="row small"><span></span><button hidden></button></div><div class="siege" hidden></div>`;
    const [head, bar, foot, siege] = li.children;
    const [title] = head.children;
    title.children[0].textContent = `${name || `${c.capital ? "Capital" : "City"} ${number.get(c.h3)}`} ${"★".repeat(c.tier)}`;
    title.children[1].onclick = (e) => { e.stopPropagation(); renameCity(title, c.h3, name || ""); };
    head.children[1].textContent = `${c.income_per_hour.toFixed(2)} coins/h`;
    bar.children[0].style.width = `${(100 * c.garrison) / c.garrison_max}%`;
    bar.children[0].style.background = c.garrison > c.garrison_max / 2 ? "var(--good)" : c.garrison ? "var(--accent)" : "var(--bad)";
    bar.children[1].textContent = `garrison ${c.garrison}/${c.garrison_max}`;
    foot.children[0].textContent = `held ${c.held_hours} h`;
    const fix = foot.children[1];
    if (c.repair_cost > 0) {
      fix.hidden = false;
      fix.textContent = `Repair (${c.repair_cost.toFixed(1)})`;
      fix.onclick = (e) => { e.stopPropagation(); order("repair", { hex: c.h3 }); };
    }
    if (c.besieged_by) {
      siege.hidden = false;
      siege.textContent = `Under siege by ${names.get(c.besieged_by)}`
        + (c.capture_progress ? ` · capture ${c.capture_progress} min in` : "");
    }
    li.onclick = () => panTo(c.h3);
    li.dataset.h3 = c.h3;
    return li;
  }));
}

function renameCity(title, h3, current) {
  renaming = h3;
  const input = document.createElement("input");
  input.className = "city-input";
  input.maxLength = 24;
  input.value = current;
  input.placeholder = "City name";
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const name = input.value.trim().replace(/\s+/g, " ");
    if (save && name !== current) { sentNames.set(h3, name); order("rename", { hex: h3, name }); }
    renaming = null;
    if (lastState) renderCities(lastState, new Map(lastState.players.map((p) => [p.id, p.name])));
  };
  input.onclick = (e) => e.stopPropagation();
  input.onkeydown = (e) => { if (e.key === "Enter") finish(true); else if (e.key === "Escape") finish(false); };
  input.onblur = () => finish(true);
  title.replaceChildren(input);
  input.focus();
  input.select();
}

async function order(kind, payload = {}) {
  if (!sid) return;
  try {
    await api(`/api/solo/${sid}/orders`, { kind, payload });
  } catch (e) {
    toast(e.message);
  }
}

async function poll() {
  try {
    lastState = await api(`/api/solo/${sid}/state`);
    if (lastState.playable_hexes !== fogCount) {
      // Counted only once the fog is drawn: a failed fetch is retried on the next poll.
      const playable = lastState.playable_hexes;
      map.getSource("fog").setData(await api(`/api/solo/${sid}/fog`));
      fogCount = playable;
    }
    draw(lastState);
    renderPanel(lastState);
    updatePlayer(lastState);
  } catch (e) {
    if (String(e.message).startsWith("404")) { store.del("solo"); location.reload(); return; }
    toast(`Connection: ${e.message}`);
  }
  setTimeout(poll, POLL_MS);
}

// -- start -------------------------------------------------------------------------------------

function describe(spec) {
  const bits = [];
  if (spec.water_cost !== 1) bits.push(`water ×${(1 / spec.water_cost).toFixed(0)} speed`);
  if (spec.land_cost !== 1) bits.push(`land ${Math.round(100 / spec.land_cost)} km/h`);
  if (spec.attack) bits.push(`attack ${spec.attack > 0 ? "+" : ""}${Math.round(spec.attack * 100)}%`);
  if (spec.defence) bits.push(`defence ${spec.defence > 0 ? "+" : ""}${Math.round(spec.defence * 100)}%`);
  if (spec.heal_mult !== 1) bits.push(`heals ×${spec.heal_mult}`);
  if (spec.heals_allies) bits.push(`+${spec.heals_allies} HP to allies on its hex`);
  if (spec.ignores_defence) bits.push("ignores defence bonuses");
  return bits.join(", ") || "the generalist: no strengths, no weaknesses";
}

function startDialog() {
  const sel = $("start-class");
  sel.replaceChildren(...Object.keys(meta.classes).map((c) => new Option(c, c)));
  const blurb = () => { $("class-blurb").textContent = describe(meta.classes[sel.value]); };
  sel.onchange = blurb;
  blurb();
  $("start-bots").append(...Object.entries(meta.personalities).map(([k, p]) => {
    const l = document.createElement("label");
    l.title = `${p.blurb} (${p.class})`;
    l.innerHTML = `<input type="checkbox" name="bots" checked> `;
    l.firstChild.value = k;
    l.append(`${k} (${p.class})`);
    return l;
  }));
  $("start-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { name: f.get("name"), unit_class: f.get("unit_class"), bots: f.getAll("bots"), subscriber: f.get("subscriber") === "on" };
    const r = await api("/api/solo", body);
    store.set("solo", r.id);
    $("start").close();
    await begin(r.id);
  };
  $("start").showModal();
}

async function begin(id) {
  sid = id;
  const geo = await api(`/api/solo/${sid}/hexes`);
  hexes = new Map(geo.features.map((f) => [f.properties.h3, f]));
  $("panel").hidden = false;
  renderAttribution();
  toast("Click a hex to move there. Click one with units or a city, or right-click, for more.");
  poll();
}

function renderAttribution() {
  // Shown in the footer rather than over the map. The basemap's credits are required by its licence.
  const el = $("attribution");
  el.textContent = `${manifest.attribution} · `;
  for (const [i, [text, href]] of [
    ["OpenFreeMap", "https://openfreemap.org"],
    ["© OpenMapTiles", "https://www.openmaptiles.org/"],
    ["Data from OpenStreetMap", "https://www.openstreetmap.org/copyright"],
  ].entries()) {
    const a = document.createElement("a");
    a.href = href; a.target = "_blank"; a.rel = "noopener"; a.textContent = text;
    el.append(...(i ? [" ", a] : [a]));
  }
}

function showTab(name) {
  for (const b of document.querySelectorAll(".tabs button")) b.classList.toggle("on", b.dataset.tab === name);
  for (const p of document.querySelectorAll(".pane")) p.hidden = p.id !== `pane-${name}`;
  store.set("tab", name);
}

function wirePanel() {
  for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => showTab(b.dataset.tab);
  for (const b of document.querySelectorAll("#battle-views button")) {
    b.onclick = () => { battleView = b.dataset.view; if (lastState) renderPanel(lastState); };
  }
  showTab(store.get("tab") || "players");
  for (const b of document.querySelectorAll("#modes [data-mode]")) b.onclick = () => order("mode", { mode: b.dataset.mode });
  $("stop").onclick = () => order("cancel");
  $("swap").replaceChildren(...Object.keys(meta.classes).map((c) => new Option(c, c)));
  $("swap-go").onclick = () => order("class", { class: $("swap").value });
  const style = $("style");
  style.replaceChildren(...manifest.styles.map((s) => new Option(s.name, s.name)));
  style.value = store.get("style") || manifest.styles[0].name;
  style.onchange = () => setStyle(style.value);
  $("speed").onchange = () => api(`/api/solo/${sid}/control`, { tick_every: Number($("speed").value) });
  $("pause").onclick = () => { autoPaused = false; setPaused(!(lastState && lastState.paused)); };
  $("quit").onclick = () => { store.del("solo"); location.reload(); };
  wireAway();
  addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
  addEventListener("pointerdown", (e) => { if (!$("ctx").contains(e.target)) closeMenu(); }, true);
  wireShieldOptions();
  wireNemesis();
  $("news").onclick = () => lastState && showReport(Math.max(0, lastState.tick - 360), "The last six hours");
}

// -- when away ----------------------------------------------------------------------------------
// "When away" pauses the game or raises your shield when the tab is hidden or closed, or after IDLE_MS
// without input, and undoes it when you're back. It only undoes what it did itself: a pause or a shield
// you chose stays. The shield comes out of the same daily budget as one you raise by hand.

let autoPaused = false, autoShielded = false, lastInput = Date.now();

function setPaused(paused, keepalive = false) {
  return sid ? api(`/api/solo/${sid}/control`, { paused }, keepalive).catch(() => {}) : Promise.resolve();
}

function you() { return lastState.players.find((p) => p.id === lastState.you); }

function myUnit() { return lastState && lastState.units.find((u) => u.user_id === lastState.you); }

// The game time you left at, so the paper can report what you missed (any "When away" setting).
const REPORT_AFTER = 30; // game minutes away before there's a paper waiting
function markAway() {
  if (lastState && sid && store.get("away_tick") == null) store.set("away_tick", `${sid}:${lastState.tick}`);
}

function awayNow() {
  markAway();
  const mode = $("away").value, me = myUnit();
  if (!me || autoPaused || autoShielded) return;
  // Hiding a tab that is about to close lands here before pagehide, so the flag is stored now (pagehide then has
  // nothing left to do) and the request is keepalive, to outlive the page.
  if (mode === "pause" && !lastState.paused) {
    autoPaused = true;
    store.set("autopaused", sid);
    setPaused(true, true);
  } else if (mode === "shield" && me.on_map && !me.shield && !me.shield_ready_in) {
    autoShielded = true;
    store.set("autoshielded", sid);
    api(`/api/solo/${sid}/orders`, { kind: "shield", payload: { on: true } }, true).catch((e) => toast(e.message));
  }
}

function backNow() {
  lastInput = Date.now();
  const left = store.get("away_tick");
  if (left != null && lastState) {
    store.del("away_tick");
    const [at, tick] = left.split(":");
    if (at === sid && lastState.tick - Number(tick) >= REPORT_AFTER) showReport(Number(tick));
  }
  // Before the game has loaded there is nothing to resume or unshield yet: keep the flags for the first input after.
  if (!sid || !lastState) return;
  if (autoPaused) {
    autoPaused = false;
    store.del("autopaused");
    setPaused(false);
    toast("Resumed: the game was paused while you were away");
  }
  if (autoShielded) {
    autoShielded = false;
    store.del("autoshielded");
    if ((myUnit() || {}).shield) order("shield", { on: false });
    toast("Shield dropped: it was up while you were away");
  }
}

function wireAway() {
  const away = $("away");
  if (store.get("autopause") === "1") store.set("away", "pause"); // the old checkbox
  away.value = store.get("away") || "play";
  // Set by a beacon as the last page closed: the first input here undoes it.
  autoPaused = store.get("autopaused") === store.get("solo") && store.get("solo") != null;
  autoShielded = store.get("autoshielded") === store.get("solo") && store.get("solo") != null;
  away.onchange = () => { store.set("away", away.value); backNow(); };
  for (const ev of ["pointerdown", "pointermove", "keydown", "wheel"]) {
    addEventListener(ev, () => { if (document.visibilityState === "visible") backNow(); }, { passive: true });
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") awayNow(); });
  // A closing tab can't wait for a fetch; a beacon still gets through.
  addEventListener("pagehide", () => {
    markAway();
    const me = myUnit(), beacon = (path, body) =>
      navigator.sendBeacon(`/api/solo/${sid}/${path}`, new Blob([JSON.stringify(body)], { type: "application/json" }));
    if (!sid || !me || autoPaused || autoShielded) return;
    if (away.value === "pause" && !lastState.paused) {
      beacon("control", { paused: true });
      store.set("autopaused", sid);
    } else if (away.value === "shield" && me.on_map && !me.shield && !me.shield_ready_in) {
      beacon("orders", { kind: "shield", payload: { on: true } });
      store.set("autoshielded", sid);
    }
  });
  setInterval(() => { if (Date.now() - lastInput > IDLE_MS) awayNow(); }, 10000);
}

// -- the paper -----------------------------------------------------------------------------------

async function showReport(since, title = "While you were away") {
  let r;
  try { r = await api(`/api/solo/${sid}/report?since=${since}`); } catch (e) { toast(e.message); return; }
  $("report-kicker").textContent = `${r.paper} · ${title} · ${clock(r.since)} – ${clock(r.until).split(", ")[1]}`;
  $("report-headline").textContent = r.headline;
  $("report-lead").textContent = r.lead;
  $("report-stories").replaceChildren(...r.stories.map((st) => {
    const a = document.createElement("article");
    a.innerHTML = "<h3></h3><p></p>";
    a.children[0].textContent = st.headline;
    a.children[1].textContent = st.text;
    return a;
  }));
  $("report").showModal();
}

// -- shield options ------------------------------------------------------------------------------
// The schedule and the shield's look are patron perks; free players see them, greyed out.

function minutesOf(hhmm) { const [h, m] = hhmm.split(":").map(Number); return h * 60 + m; }
function hhmm(minutes) { return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`; }

function openShieldOptions() {
  const st = lastState, me = myUnit(), you = st.players.find((p) => p.id === st.you);
  const patron = you.subscriber, d = $("shield-dialog");
  $("shield-rules").textContent = `Everyone: up to ${meta.shield_daily_hours} h of shield a game day, toggled as you like, `
    + `${meta.shield_cooldown_minutes} min between. A breached shield needs 12 h to mend. You can't attack while it's up.`;
  for (const el of d.querySelectorAll(".patron")) {
    el.classList.toggle("locked", !patron);
    for (const c of el.querySelectorAll("input, button")) c.disabled = !patron;
  }
  const [from, to] = me.shield_schedule || [22 * 60, 6 * 60];
  $("shield-from").value = hhmm(from);
  $("shield-to").value = hhmm(to);
  $("shield-schedule-now").textContent = me.shield_schedule
    ? `Scheduled daily ${hhmm(from)}–${hhmm(to)}.` : "No schedule.";
  $("shield-looks").replaceChildren(...Object.entries(meta.shield_looks).map(([name, colour]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "swatch-btn" + (you.shield_look === colour ? " on" : "");
    b.style.setProperty("--c", colour);
    b.title = name;
    b.disabled = !patron && name !== "plain";
    b.onclick = async () => {
      try { await api(`/api/solo/${sid}/looks`, { shield: name }); } catch (e) { toast(e.message); }
      d.close();
    };
    return b;
  }));
  d.showModal();
}

function wireShieldOptions() {
  $("shield-opts").onclick = openShieldOptions;
  $("shield-save").onclick = () => {
    order("shield_schedule", { from: minutesOf($("shield-from").value), to: minutesOf($("shield-to").value) });
    $("shield-dialog").close();
  };
  $("shield-clear").onclick = () => { order("shield_schedule", { from: null, to: null }); $("shield-dialog").close(); };
}

async function main() {
  [manifest, meta] = await Promise.all([api("/api/manifest"), api("/api/meta")]);
  await initMap();
  addLayers();
  wirePanel();
  const saved = store.get("solo");
  if (saved) {
    try { await begin(saved); return; } catch { store.del("solo"); }
  }
  startDialog();
}

main().catch((e) => toast(e.message));
