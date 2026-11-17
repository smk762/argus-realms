// Atlas static demo (atlas.dragonhound.dev): MapLibre over a real basemap with the painted tiles draped on top.
(async function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const store = {
    get(k) { try { return localStorage.getItem("realms:" + k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem("realms:" + k, v); } catch { /* private mode */ } },
  };

  // Every off-site link opens in a new tab with no opener or Referer, and says so to screen readers: the page's
  // own links (GitHub, OpenStreetMap) and the ones MapLibre's attribution control rebuilds with innerHTML from the
  // style's sources, which an observer on the map container catches. The Dragonhound sites (dragonhound.dev and
  // *.dragonhound.dev, as in the strip at the top) are one family and open in this tab, so Back works.
  const family = (host) => host === "dragonhound.dev" || host.endsWith(".dragonhound.dev");
  function externalize(root) {
    for (const a of root.querySelectorAll("a[href]")) {
      if (a.dataset.external || !/^https?:$/.test(a.protocol) || a.origin === location.origin || family(a.hostname)) continue;
      a.dataset.external = "1";
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      const cue = document.createElement("span");
      cue.className = "sr-only";
      cue.textContent = " (opens in new tab)";
      a.append(cue);
    }
  }
  externalize(document);
  new MutationObserver(() => externalize($("map"))).observe($("map"), { childList: true, subtree: true });

  // Phones get a compact panel; "More" opens the rest. Wider screens always show everything (the button is hidden).
  const toggle = $("panel-toggle");
  toggle.onclick = () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    toggle.textContent = open ? "Less" : "More";
    $("panel").classList.toggle("open", open);
  };

  // MapLibre's stylesheet was only preloaded (see index.html); attach it now, before any map element exists.
  const cssReady = new Promise((resolve) => {
    const pre = $("maplibre-css"), link = document.createElement("link");
    for (const k of ["href", "integrity", "crossOrigin", "referrerPolicy"]) link[k] = pre[k];
    link.rel = "stylesheet";
    link.onload = link.onerror = resolve;
    pre.after(link); // where the old <link> stood, so the page's own rules below still override MapLibre's
  });

  // tiles/areas.json lists the areas (each its own tile tree and styles); the frame is the island's outline.
  const areas = await (await fetch("tiles/areas.json")).json();
  const byId = Object.fromEntries(areas.map((a) => [a.id, a]));
  const base = new URL(".", location.href).href;
  const fromHash = decodeURIComponent(location.hash.slice(1));
  let area = byId[fromHash] || byId[store.get("area")] || areas[0];
  // The sea mask (scripts/sea_mask.py, one per area): opaque on land and on the coastal sea, fading out by a few
  // kilometres offshore, so the painted chunks' open sea gives way to the basemap's ocean and the stepped block
  // outline is gone. Its JSON lists the tiles that are wholly clear (never fetched) or wholly opaque (drawn as
  // they are); the rest are multiplied by their part of the mask, scaled up by the canvas. Tile files are unchanged.
  // The mask's file name carries its content hash, so it needs no version. The opening area's mask loads now,
  // alongside the basemap.
  const masks = {};
  const seaMask = (a) => {
    if (!a.mask) return null;
    masks[a.id] ||= Promise.all([
      fetch(`${a.mask}.json`).then((r) => r.json()),
      fetch(`${a.mask}.png`).then((r) => r.blob()).then((b) => createImageBitmap(b)),
    ]).then(([m, bitmap]) => {
      // Drawn once onto a canvas: tiles then sample it far faster than they would the decoded bitmap.
      const img = new OffscreenCanvas(bitmap.width, bitmap.height);
      img.getContext("2d").drawImage(bitmap, 0, 0);
      bitmap.close();
      return { ...m, img, clear: new Set(m.clear), full: new Set(m.full) };
    })
      .catch(() => null); // no mask: the painted chunks show whole, as before
    return masks[a.id];
  };
  seaMask(area);
  // Keep the island clear of the panel: beside it on wide screens, below it on phones.
  const padding = () => innerWidth > 700
    ? { top: 40, bottom: 40, left: 340, right: 20 }
    : { top: Math.min($("panel").offsetHeight + 20, innerHeight / 3), bottom: 50, left: 20, right: 20 };

  await cssReady;

  const map = new maplibregl.Map({
    container: "map",
    style: "https://tiles.openfreemap.org/styles/liberty",
    bounds: area.frame,
    fitBoundsOptions: { padding: padding() },
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
  // Compact attribution opens when the basemap's credits arrive and folds into its (i) button on the first drag.
  // On a phone, where open it covers the bottom quarter of the map, it starts folded instead: the first time
  // MapLibre opens it, it is folded again before it is painted (so nothing shifts later). The (i) button opens it,
  // and the credits line at the bottom of the map stays in view.
  if (innerWidth <= 560) {
    const attrib = document.querySelector(".maplibregl-ctrl-attrib");
    const fold = (_, seen) => {
      if (!attrib.classList.contains("maplibregl-compact-show")) return;
      attrib.classList.remove("maplibregl-compact-show");
      seen?.disconnect();
    };
    new MutationObserver(fold).observe(attrib, { attributes: true, attributeFilter: ["class"] });
  }
  await new Promise((r) => map.on("load", r));

  // Painted tiles go under the basemap's labels so the real place names stay readable.
  const firstSymbol = (map.getStyle().layers.find((l) => l.type === "symbol") || {}).id;
  let chosen = store.get("style");
  let peeking = false;
  let layers = [];

  // Tiles are cached at the edge for a day; each area's version (its export time, from areas.json, which is
  // cached for minutes) goes on every tile-tree URL, so a re-export is seen at once without a purge.
  const v = (a) => (a.version ? "?v=" + encodeURIComponent(a.version) : "");
  // Only the tiles listed in each style's index.json exist; answer the rest with an empty tile instead of a 404.
  const have = {};
  const index = async (a, name) => {
    const k = a.id + "/" + name;
    have[k] ||= fetch(`tiles/${k}/index.json${v(a)}`).then((r) => r.json()).then((l) => new Set(l));
    return have[k];
  };
  const EMPTY = new ImageData(256, 256);
  maplibregl.addProtocol("realms", async (params, abort) => {
    const [, id, name, key] = params.url.match(/^realms:\/\/([^/]+)\/([^/]+)\/(\d+\/\d+\/\d+)$/);
    const a = byId[id];
    const [have, mask] = await Promise.all([index(a, name), seaMask(a)]);
    if (!have.has(key) || mask?.clear.has(key)) return { data: await createImageBitmap(EMPTY) };
    const r = await fetch(`${base}tiles/${id}/${name}/${key}.png${v(a)}`, { signal: abort.signal });
    if (!r.ok) throw new Error(`tile ${id}/${name}/${key}: ${r.status}`);
    if (!mask || mask.full.has(key)) return { data: await r.arrayBuffer() };
    const img = await createImageBitmap(await r.blob());
    const [z, x, y] = key.split("/").map(Number), s = 256 * 2 ** (mask.zoom - z);
    const c = new OffscreenCanvas(256, 256), g = c.getContext("2d");
    g.drawImage(img, 0, 0, 256, 256);
    img.close();
    g.globalCompositeOperation = "destination-in";
    g.drawImage(mask.img, x * s - mask.x * 256, y * s - mask.y * 256, s, s, 0, 0, 256, 256);
    return { data: c.transferToImageBitmap() };
  });

  // Place names (argus-atlas labels, from OSM): one GeoJSON per area, drawn above every style. Each chunk labels
  // only its own tile, so a name appears once. Tiers come in relative to the area's chunk zoom; MapLibre's
  // collision pass keeps them from overlapping. While an area has its own names the basemap's are hidden.
  const BASEMAP_PLACES = ["label_other", "label_village", "label_town", "label_city", "label_city_capital"];
  const TIERS = [
    { kinds: ["city"], from: -Infinity, size: 15, font: "Noto Sans Bold" },
    { kinds: ["town"], from: -2, size: 13, font: "Noto Sans Regular" },
    { kinds: ["village"], from: 0, size: 12, font: "Noto Sans Regular" },
    { kinds: ["suburb", "hamlet"], from: 1, size: 11, font: "Noto Sans Italic" },
  ];
  let nameLayers = [];
  function showNames(a) {
    for (const id of nameLayers) map.removeLayer(id);
    if (map.getSource("names")) map.removeSource("names");
    nameLayers = [];
    for (const id of BASEMAP_PLACES) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", a.labels ? "none" : "visible");
    }
    if (!a.labels) return;
    map.addSource("names", { type: "geojson", data: a.labels + v(a) });
    TIERS.forEach((t, i) => {
      const id = `names-${i}`;
      map.addLayer({
        id, type: "symbol", source: "names",
        minzoom: Math.max(0, a.chunk_zoom + t.from),
        filter: ["in", ["get", "kind"], ["literal", t.kinds]],
        layout: {
          "text-field": ["get", "name"], "text-font": [t.font], "text-size": t.size,
          "text-max-width": 8, "text-padding": 4, "symbol-sort-key": ["get", "rank"],
        },
        paint: { "text-color": "#1f1b16", "text-halo-color": "rgba(255,252,246,0.92)", "text-halo-width": 1.6 },
      });
      nameLayers.push(id);
    });
  }

  // An area's zoom range is relative to its own frame: one step out, and in to its native zoom (chunk zoom + 2),
  // where the painted tiles are still 1:1. Raster sources pick tile zoom round(map zoom + 1), hence the - 0.5.
  function showArea(a) {
    for (const id of layers) { map.removeLayer(id); map.removeSource(id); }
    layers = [];
    // Lift the previous area's limits first: cameraForBounds clamps to the current maxZoom.
    map.setMaxBounds(null);
    map.setMinZoom(null);
    map.setMaxZoom(a.maxzoom - 0.5);
    const view = map.cameraForBounds(a.frame, { padding: padding() });
    map.jumpTo(view);
    map.setMinZoom(Math.max(0, view.zoom - 1));
    const [w, s, e, n] = a.bounds, px = (e - w) * 0.5, py = (n - s) * 0.5;
    map.setMaxBounds([[w - px, s - py], [e + px, n + py]]);
    showNames(a);
  }
  // A style's source and layer are added the first time it is chosen, so only the shown style is ever set up.
  function addStyle(a, name) {
    const id = `paint-${a.id}-${name}`;
    if (map.getLayer(id)) return;
    map.addSource(id, {
      type: "raster", tiles: [`realms://${a.id}/${name}/{z}/{x}/{y}`], tileSize: 256,
      minzoom: a.minzoom, maxzoom: a.maxzoom, bounds: a.bounds, attribution: a.attribution,
    });
    map.addLayer({
      id, type: "raster", source: id,
      layout: { visibility: "none" },
      paint: { "raster-opacity": peeking ? 0 : Number($("opacity").value) / 100, "raster-fade-duration": 0 },
    }, firstSymbol);
    layers.push(id);
  }
  function chooseArea(id) {
    area = byId[id];
    store.set("area", id);
    history.replaceState(null, "", "#" + id);
    showArea(area);
    for (const b of $("areas").children) b.setAttribute("aria-pressed", String(b.dataset.area === id));
    $("styles").replaceChildren(...area.styles.map((name) => {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = name; b.dataset.style = name;
      b.title = styleTip(area, name);
      b.onclick = () => choose(name);
      return b;
    }));
    choose(area.styles.includes(chosen) ? chosen : area.styles[0]);
    applyOpacity();
  }
  for (const a of areas) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = a.label; b.dataset.area = a.id;
    b.onclick = () => chooseArea(a.id);
    $("areas").append(b);
  }

  // On phones the styles scroll sideways in one row; fade its right edge while more styles lie beyond it.
  const strip = $("styles");
  const edgeHint = () => strip.classList.toggle("more-right", strip.scrollLeft + strip.clientWidth < strip.scrollWidth - 2);
  strip.addEventListener("scroll", edgeHint, { passive: true });
  addEventListener("resize", edgeHint);

  function applyOpacity() {
    $("opval").textContent = $("opacity").value + "%";
    $("opacity").setAttribute("aria-valuetext", $("opacity").value + "%");
    const v = peeking ? 0 : Number($("opacity").value) / 100;
    for (const id of layers) map.setPaintProperty(id, "raster-opacity", v);
  }
  // "How it was made": the commands and knobs behind the shown style, read by the build from its chunks' job.json
  // (areas.json `bake`), each command linked to its section of the docs page, /docs/ (`docs`, anchors checked by the build).
  const el = (tag, text, attrs = {}) => Object.assign(document.createElement(tag), attrs, text == null ? {} : { textContent: text });
  const several = (v) => (v || []).join(" / ");
  function recipeOf(a, name) {
    const b = (a.bake || {})[name];
    if (!b || !b.recipe?.length) return "";
    const preset = several(b.preset);
    return `Recipe ${several(b.recipe)}` + (preset && preset !== several(b.recipe) ? `, preset ${preset}` : "");
  }
  // The style button's tooltip: the recipe and preset, and the LoRA (or none), that painted it.
  function styleTip(a, name) {
    const b = (a.bake || {})[name];
    if (!b || !b.recipe?.length) return "";
    return `${recipeOf(a, name)}, preset ${several(b.preset) || "none"}, ${b.lora?.length ? `LoRA ${several(b.lora)}` : "no LoRA"}`;
  }
  function step(a, cmd, text, href, extra) {
    const li = el("li");
    const link = el("a", null, { href });
    link.append(el("code", `argus-atlas ${cmd}`));
    li.append(link, ` ${text}`);
    if (extra) li.append(...extra);
    return li;
  }
  function explain(a, name) {
    const b = (a.bake || {})[name], d = a.docs || {};
    const steps = [], params = $("made-params"), build = $("made-build");
    if (!b || !b.chunks) {
      $("made-steps").replaceChildren();
      params.textContent = "This style's tiles carry no bake record.";
      build.replaceChildren();
      return;
    }
    const cmds = new Set(b.commands);
    if (cmds.has("dem")) steps.push(step(a, "dem", "fetched the Copernicus GLO-30 terrain.", d.dem));
    if (cmds.has("osm")) steps.push(step(a, "osm", "fetched the OpenStreetMap layers it was painted from.", d.osm));
    const c = b.chunk || {};
    const one = b.chunks === 1;
    steps.push(step(a, "chunk", `painted ${one ? "one chunk" : `${b.chunks} chunks`} (z${several(c.zoom)} tiles, ` +
      `${several(c.size)} px plus a ${several(c.margin)} px margin)${one ? "" : ", each with its baked neighbours in view,"} and `,
      d.chunk, [el("a", b.aligned ? `the alignment gate passed ${one ? "it" : "every one"}` : "the alignment gate", { href: d.gate }), "."]));
    if (cmds.has("labels")) steps.push(step(a, "labels", "added the place names, drawn on top in a real font.", d.labels));
    steps.push(el("li", "This site's export joined the chunks into one tile tree, with coarser zooms shrunk from them."));
    $("made-steps").replaceChildren(...steps);

    const seeds = b.seeds ? (b.seeds[0] === b.seeds[1] ? `seed ${b.seeds[0]}` : `seeds ${b.seeds[0]}–${b.seeds[1]}, one per chunk`) : "";
    const knobs = [
      recipeOf(a, name),
      several(b.checkpoint) + (b.lora?.length ? ` + ${several(b.lora)}` : ", no LoRA"),
      `denoise ${several(b.denoise)}`, `${several(b.steps)} steps`, `cfg ${several(b.cfg)}`, seeds,
    ].filter(Boolean);
    params.textContent = knobs.join(", ") + ".";

    const built = (b.atlas || []).filter((g) => g.commit);
    build.replaceChildren();
    if (built.length) {
      build.append(`Baked with argus-atlas ${built.map((g) => `${g.version} (${g.commit.slice(0, 7)})`).join(", ")}: `,
        el("a", "read its docs", { href: d.overview }), ".");
    } else {
      build.append("Baked with an argus-atlas build that recorded no version. ", el("a", "The argus-atlas docs", { href: d.overview }), ".");
    }
  }

  function choose(name) {
    chosen = name;
    explain(area, name);
    if (area.docs?.gate) $("fade-docs").href = area.docs.gate;
    store.set("style", name);
    addStyle(area, name);
    for (const id of layers) map.setLayoutProperty(id, "visibility", id === `paint-${area.id}-${name}` ? "visible" : "none");
    for (const b of $("styles").children) {
      b.setAttribute("aria-pressed", String(b.dataset.style === name));
      if (b.dataset.style === name) b.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    edgeHint();
  }
  chooseArea(area.id);
  $("opacity").oninput = applyOpacity;
  const peek = (on) => { peeking = on; applyOpacity(); };
  // Hold space anywhere except on a control that uses space itself (the map canvas counts as anywhere).
  const OWN_SPACE = "input, button, select, textarea, summary, a[href], [contenteditable]";
  addEventListener("keydown", (ev) => {
    if (ev.code === "Space" && !ev.repeat && !ev.target.closest?.(OWN_SPACE)) { ev.preventDefault(); peek(true); }
  });
  addEventListener("keyup", (ev) => { if (ev.code === "Space") peek(false); });
  // The peek button: press and hold with a pointer, or with Space/Enter when it has keyboard focus.
  const btn = $("peek");
  btn.addEventListener("pointerdown", () => peek(true));
  for (const t of ["pointerup", "pointerleave", "pointercancel", "blur"]) btn.addEventListener(t, () => peek(false));
  btn.addEventListener("keydown", (ev) => {
    if ((ev.code === "Space" || ev.key === "Enter") && !ev.repeat) { ev.preventDefault(); peek(true); }
  });
  btn.addEventListener("keyup", (ev) => { if (ev.code === "Space" || ev.key === "Enter") { ev.preventDefault(); peek(false); } });
  addEventListener("hashchange", () => {
    const id = decodeURIComponent(location.hash.slice(1));
    if (byId[id] && id !== area.id) chooseArea(id);
  });
  applyOpacity();
})();
