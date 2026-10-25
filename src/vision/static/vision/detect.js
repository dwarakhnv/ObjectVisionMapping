// Detect page: pick an environment + camera, detect and track objects in its video,
// and show them on a map / 3D view of the environment: at their lat/lon outdoors,
// or at their x / y on the floor plan indoors.
//
// Video: a camera plays live at its own frame rate (MJPEG from /api/feeds/<id>/video) and the
// detections are drawn over it on every screen refresh, moved ahead along their motion so they
// stay on the objects although detection runs slower and a little behind. Files and stills,
// which have no live video, show each analysed frame with its detections instead.
import { BaseMap } from "/static/simulation/basemap.js";
import { GroundView } from "/static/simulation/groundview.js";

const { api, toast, el, fmt } = window.OVM;
const $ = (id) => document.getElementById(id);
const classes = JSON.parse($("object-classes").textContent);
const canvas = $("frame");
const video = $("video");
const AHEAD_MAX_S = 0.6;  // never move boxes further ahead than this

const S = {
  envs: [], slug: null, camId: null, analysis: null,
  stop: null, result: null, img: null, selected: null, trails: new Map(),
  live: false, receivedAt: 0, motion: new Map(),
  hidden: new Set(load("ovm.hidden", [])),
  show: load("ovm.show", { masks: true, boxes: true, labels: true, trails: true }),
  geoMode: load("ovm.geoMode", "2d"), ground: load("ovm.geoGround", "satellite"),
};

function load(key, fallback) {
  try { const v = JSON.parse(localStorage.getItem(key)); return v === null ? fallback : v; } catch (e) { return fallback; }
}
function save(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode */ } }
const visible = (objs) => objs.filter((o) => !S.hidden.has(o.class));
const camSlug = (name) => String(name).trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 64) || "unnamed";

// ================================================================ environment + camera selection
async function loadEnvironments() {
  S.envs = (await api("/api/envs")).environments;
  const sel = $("env-select");
  sel.replaceChildren(...(S.envs.length ? S.envs.map((e) => el("option", { value: e.slug, text: e.name }))
    : [el("option", { value: "", text: "No environments yet" })]));
  const q = new URLSearchParams(location.search);
  const want = q.get("env") || load("ovm.detectEnv", null);
  if (!S.envs.length) { $("feed-empty").textContent = "Create an environment first (Environments page)."; return; }
  await selectEnv(S.envs.some((e) => e.slug === want) ? want : S.envs[0].slug, q.get("camera"));
}

async function selectEnv(slug, camId) {
  S.slug = slug;
  $("env-select").value = slug;
  $("edit-env").href = "/environments/?env=" + encodeURIComponent(slug);
  save("ovm.detectEnv", slug);
  const r = await api("/api/envs/" + slug);
  S.analysis = r.analysis;
  S.envDoc = r.environment;
  const cams = r.environment.cameras.filter((c) => c.source);
  $("cam-select").replaceChildren(...(cams.length ? cams.map((c) => el("option", { value: camSlug(c.name), text: c.name }))
    : [el("option", { value: "", text: "No cameras" })]));
  const wanted = cams.find((c) => camSlug(c.name) === (camId || load("ovm.detectCam", null)));
  await geo.environmentChanged(r.environment.space || { kind: "geo" });
  selectCamera(wanted ? camSlug(wanted.name) : cams.length ? camSlug(cams[0].name) : null);
}

function selectCamera(camId) {
  S.camId = camId;
  $("cam-select").value = camId || "";
  save("ovm.detectCam", camId);
  history.replaceState(null, "", "?env=" + encodeURIComponent(S.slug) + (camId ? "&camera=" + encodeURIComponent(camId) : ""));
  start(camId ? "@" + S.slug + "." + camId : null);
  geo.cameraChanged();
}

$("env-select").onchange = (e) => selectEnv(e.target.value);
$("cam-select").onchange = (e) => selectCamera(e.target.value);

// ================================================================ video + detections
function start(feedId) {
  if (S.stop) S.stop();
  S.stop = null;
  S.result = null;
  S.selected = null;
  S.trails.clear();
  S.motion.clear();
  showVideo(feedId);
  $("feed-empty").classList.toggle("hidden", !!feedId);
  $("feed-empty").textContent = feedId ? "" : "This environment has no camera with a source.";
  canvas.classList.add("hidden");
  renderList();
  geo.objects([]);
  if (!feedId) return;
  setStatus("Starting… (the model loads on first use)");
  S.stop = OVMOverlay.follow(feedId, onFrame, onStatus, () => !S.live);
}

// The camera's live video; files and stills have none (404), so they fall back to analysed frames.
function showVideo(feedId) {
  video.onerror = null;
  video.removeAttribute("src");  // closes the previous camera's stream
  S.live = !!feedId;
  video.classList.toggle("hidden", !S.live);
  canvas.classList.toggle("overlay", S.live);
  if (!feedId) return;
  video.onerror = () => {
    S.live = false;
    video.classList.add("hidden");
    canvas.classList.remove("overlay");
  };
  video.src = "/api/feeds/" + encodeURIComponent(feedId) + "/video";
}

function setStatus(text) {
  $("feed-status").textContent = text || "";
  $("feed-status").classList.toggle("hidden", !text);
}

function onStatus(r) {
  if (r.status === "error") {
    setStatus("");
    canvas.classList.add("hidden");
    video.classList.add("hidden");
    $("feed-empty").classList.remove("hidden");
    $("feed-empty").textContent = r.error || "The feed stopped.";
  } else if (r.status === "starting") setStatus("Starting… (the model loads on first use)");
}

function onFrame(img, r) {
  S.img = img;
  S.result = r;
  S.receivedAt = performance.now();
  // Without tracking, ids only number this result's objects: no motion or trails to follow.
  if (r.tracking) { learnMotion(r); OVMOverlay.updateTrails(S.trails, r); } else { S.motion.clear(); S.trails.clear(); }
  canvas.classList.remove("hidden");
  video.classList.toggle("hidden", !S.live);
  $("feed-empty").classList.add("hidden");
  setStatus(r.fps ? (S.live ? "Live video · " : "") + fmt(r.fps, 1) + " detections/s" : "");
  redraw();
  renderList();
  geo.objects(r.objects);
  $("stats").textContent = r.width + "×" + r.height + " · " + r.detector + (r.runs_on ? " on " + r.runs_on : "") +
    " · " + (r.detect_ms != null ? r.detect_ms : r.inference_ms) + " ms to detect, " + r.inference_ms + " ms per result";
  const cam = r.camera;
  $("camera-note").textContent = !cam ? ""
    : cam.calibrated ? "Locations come from " + cam.name + "'s reference points (accuracy " + fmt(cam.accuracy_m, 2) +
      " m). Distances and sizes use its estimated position."
    : cam.name + " needs at least 4 reference points to place objects. Distances use average object heights.";
}

function redraw() {
  if (!S.result || (!S.live && !S.img)) return;
  const result = S.live ? predicted(S.result) : S.result;
  OVMOverlay.draw(canvas, S.live ? null : S.img, result, { show: S.show, hidden: S.hidden, selected: S.selected, trails: S.trails });
}
// Over live video the overlay is redrawn on every screen refresh, so boxes glide with the objects.
(function tick() { if (S.live && S.result) redraw(); requestAnimationFrame(tick); })();

// Each object's on-screen velocity (pixels per second for each box edge), from its last two results.
function learnMotion(r) {
  const seen = new Set();
  for (const o of r.objects || []) {
    seen.add(o.id);
    const prev = S.motion.get(o.id), t = r.captured_at || r.time;
    let v = [0, 0, 0, 0];
    if (prev && t > prev.t) {
      const dt = t - prev.t;
      v = o.box.map((x, i) => (x - prev.box[i]) / dt);
      if (prev.v) v = v.map((x, i) => 0.5 * x + 0.5 * prev.v[i]);  // smooth out jitter
    }
    S.motion.set(o.id, { box: o.box, t, v: prev ? v : null });
  }
  for (const id of [...S.motion.keys()]) if (!seen.has(id)) S.motion.delete(id);
}

// The result with every box (and outline) moved to where its object should be on the video now:
// the video shows frames as they arrive, the result is inference_ms + time since it arrived old.
function predicted(r) {
  const ahead = Math.min(AHEAD_MAX_S, (performance.now() - S.receivedAt + (r.inference_ms || 0)) / 1000);
  const objects = (r.objects || []).map((o) => {
    const m = S.motion.get(o.id);
    if (!m || !m.v || o.direction === "stationary") return o;  // parked: its box only jitters
    const box = o.box.map((x, i) => x + m.v[i] * ahead);
    const dx = (box[0] - o.box[0] + box[2] - o.box[2]) / 2, dy = (box[1] - o.box[1] + box[3] - o.box[3]) / 2;
    return Object.assign({}, o, { box, polygon: o.polygon && o.polygon.map(([x, y]) => [x + dx, y + dy]) });
  });
  return Object.assign({}, r, { objects });
}

function select(id) {
  S.selected = id;
  redraw();
  renderList();
  geo.objects(S.result ? S.result.objects : []);
}

canvas.addEventListener("click", (e) => {
  if (!S.result) return;
  const rect = canvas.getBoundingClientRect();
  select(OVMOverlay.hit(S.result, ((e.clientX - rect.left) / rect.width) * canvas.width,
    ((e.clientY - rect.top) / rect.height) * canvas.height, S.hidden));
});

// ================================================================ class + overlay toggles
const counters = {};
for (const c of classes) {
  const box = el("input", { type: "checkbox", checked: !S.hidden.has(c.key) });
  counters[c.key] = el("span", { class: "muted", text: "0" });
  const t = el("label", { class: "toggle" + (S.hidden.has(c.key) ? " off" : "") },
    [box, el("i", { class: "dot", style: "background:var(--" + c.key + ", var(--other))" }), c.label, counters[c.key]]);
  box.onchange = () => {
    if (box.checked) S.hidden.delete(c.key); else S.hidden.add(c.key);
    t.classList.toggle("off", !box.checked);
    save("ovm.hidden", [...S.hidden]);
    redraw();
    renderList();
    geo.objects(S.result ? S.result.objects : []);
  };
  $("class-toggles").append(t);
}
document.querySelectorAll("#overlay-toggles button").forEach((b) => {
  b.classList.toggle("on", S.show[b.dataset.k] !== false);
  b.onclick = () => {
    S.show[b.dataset.k] = !b.classList.contains("on");
    b.classList.toggle("on");
    save("ovm.show", S.show);
    redraw();
  };
});

// ================================================================ objects panel
function renderList() {
  const all = S.result ? S.result.objects : [];
  const objs = visible(all);
  const counts = {};
  for (const o of all) counts[o.class] = (counts[o.class] || 0) + 1;
  for (const k in counters) counters[k].textContent = counts[k] || 0;
  $("count-pill").textContent = objs.length + " in view";
  document.querySelector("#objects tbody").replaceChildren(...objs.map((o) => el("tr", {
    class: "clickable" + (o.id === S.selected ? " sel" : ""), onclick: () => select(o.id) }, [
    el("td", { text: "#" + o.id }),
    el("td", {}, el("span", { class: "row", style: "gap:6px;flex-wrap:nowrap" },
      [el("i", { class: "dot", style: "background:var(--" + o.class + ", var(--other))" }), o.label])),
    el("td", { text: OVMOverlay.WORDS[o.direction] || "–" }),
    el("td", { class: "num", text: o.distance_m == null ? "–" : fmt(o.distance_m, 0) + " m" }),
  ])));
  const o = objs.find((x) => x.id === S.selected);
  $("detail").classList.toggle("hidden", !o);
  if (!o) return;
  $("detail-title").textContent = "#" + o.id + " · " + o.label;
  const d = o.dimensions || {};
  const rows = [
    ["Class", o.label + (o.subtype ? " (" + o.class + ")" : "")],
    ["Confidence", fmt(o.score * 100, 0) + " %"],
    S.result.tracking ? ["Tracked for", fmt(o.age_s, 1) + " s"] : null,
    ["Direction", OVMOverlay.WORDS[o.direction] || "–"],
    o.plan !== undefined ? ["Location (x, y)", geo.describe(o.plan)] : ["Location (lat, lon)", o.geo ? fmt(o.geo.lat, 7) + ", " + fmt(o.geo.lon, 7) : "–"],
    ["Distance", o.distance_m == null ? "– (partly out of frame)" : fmt(o.distance_m, 1) + " m"],
    ["Size (L × W × H)", fmt(d.length, 1) + " × " + fmt(d.width, 1) + " × " + fmt(d.height, 2) + " m"],
    ["Size from", o.size_source],
  ];
  if (o.speed_mps != null) rows.push(["Speed", fmt(o.speed_mps, 1) + " m/s (" + fmt(o.speed_mps * 3.6, 0) + " km/h)"]);
  if (o.heading != null) {
    const how = o.heading_source === "motion" ? "from its movement"
      : "from its shape: front/back unknown" + (o.heading_confident ? "" : ", uncertain");
    rows.push(["Heading", fmt(o.heading, 0) + "° from north (" + how + ")"]);
  }
  document.querySelector("#detail-table tbody").replaceChildren(...rows.filter(Boolean).map(([k, v]) =>
    el("tr", {}, [el("td", { class: "muted", text: k }), el("td", { text: v })])));
}

// ================================================================ map / 3D of the environment (bottom)
const geo = (() => {
  let fixed = null, live = null;               // reference points + camera; detected objects
  let home = [];                               // positions to re-centre on
  const base = new BaseMap($("geo-map"), {
    onReady: (m) => { fixed = L.layerGroup().addTo(m); live = L.layerGroup().addTo(m); },
  });
  base.style = S.ground === "none" ? "none" : S.ground;
  const tracks = new Map();                    // object id -> [latLng, ...]
  const view3d = new GroundView($("geo-3d"));
  view3d.ground = S.ground;
  const where = (o) => (base.isPlan ? o.plan : o.geo);

  function cameraInfo() {
    return S.analysis ? S.analysis.cameras.find((c) => c.id === S.camId) : null;
  }

  function showMode() {
    const ready = !!(cameraInfo() && cameraInfo().pose);
    $("geo-empty").classList.toggle("hidden", ready);
    $("geo-map").classList.toggle("hidden", S.geoMode !== "2d");
    $("geo-3d").classList.toggle("hidden", S.geoMode === "2d");
    // Indoors: the ground is the floor plan (no satellite / street map).
    document.querySelectorAll("#geo-ground button").forEach((b) => {
      b.classList.toggle("hidden", base.isPlan && b.dataset.ground === "map");
      if (b.dataset.ground === "satellite") b.textContent = base.isPlan ? "Floor plan" : "Satellite";
    });
    if (S.geoMode === "2d") { if (base.map) base.map.invalidateSize(); } else view3d.fit(S.geoMode === "top");
  }

  async function environmentChanged(space) {
    await base.setSpace(space);
    view3d.setAnalysis(S.analysis);
  }

  function cameraChanged() {
    if (!fixed) return;
    fixed.clearLayers();
    live.clearLayers();
    tracks.clear();
    const info = cameraInfo();
    showMode();
    home = [];
    if (!info || !info.pose || !S.analysis.world) { $("geo-note").textContent = ""; return; }
    // This camera's reference points (from the environment), and its estimated position + view direction.
    const env = S.envDoc && S.envDoc.cameras.find((c) => camSlug(c.name) === S.camId);
    for (const p of env ? env.reference_points : []) {
      home.push(p);
      L.marker(base.toLatLng(p), { icon: L.divIcon({ className: "", html: '<div class="ref-pin"></div>', iconSize: [14, 14], iconAnchor: [7, 7] }),
                                  interactive: false }).addTo(fixed);
    }
    const pose = info.pose;
    home.push(pose);
    L.marker(base.toLatLng(pose), { icon: L.divIcon({ className: "", html: '<div class="cam-pin">📷</div>', iconSize: [18, 18], iconAnchor: [9, 9] }),
                                    interactive: false }).addTo(fixed);
    L.polyline([base.toLatLng(pose), base.toLatLng(base.offset(pose, pose.heading, base.isPlan ? 3 : 16))],
      { color: "#0a84ff", weight: 2, dashArray: "5 4", interactive: false }).addTo(fixed);
    recenter();
    $("geo-note").textContent = " · " + info.name + " · accuracy " + fmt(info.accuracy_m, 2) + " m";
  }

  function recenter() {
    if (S.geoMode === "2d") base.fit(home);
    else view3d.fit(S.geoMode === "top");
  }
  $("geo-recenter").onclick = recenter;

  function objects(list) {
    if (!live) return;
    const shown = visible(list || []).filter(where);
    // 2D map: a marker per object, its id, and a short trail.
    live.clearLayers();
    const seen = new Set();
    for (const o of shown) {
      seen.add(o.id);
      const ll = base.toLatLng(where(o));
      const t = S.result && S.result.tracking ? tracks.get(o.id) || [] : [];
      t.push(ll);
      if (t.length > 40) t.shift();
      tracks.set(o.id, t);
      const color = OVM.classColor(o.class), sel = o.id === S.selected;
      if (t.length > 1) L.polyline(t, { color, weight: 2, opacity: 0.7, interactive: false }).addTo(live);
      L.marker(ll, { icon: objectIcon(o, color, sel) })
        .bindTooltip("#" + o.id + " " + o.label, { permanent: true, direction: "right", className: "obj-label", offset: [10, 0] })
        .on("click", () => select(o.id))
        .addTo(live);
    }
    for (const id of [...tracks.keys()]) if (!seen.has(id)) tracks.delete(id);
    // 3D / top view
    view3d.setObjects(shown);
  }

  // Moving vehicle: arrow toward its heading. Parked vehicle: a bar along its axis (front unknown).
  // Faded when the orientation is uncertain. People and unknown headings: a dot.
  function objectIcon(o, color, selected) {
    const k = selected ? 1.35 : 1;
    let svg, w, h;
    if (o.class === "person" || o.heading == null) {
      w = h = 14 * k;
      svg = '<circle cx="7" cy="7" r="5.5" fill="' + color + '" stroke="#fff" stroke-width="2"/>';
      svg = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 14 14">' + svg + "</svg>";
    } else {
      w = 14 * k; h = 26 * k;
      const shape = o.heading_source === "motion"
        ? '<path d="M7 1 L13 9 L13 25 L1 25 L1 9 Z"'
        : '<rect x="1.5" y="1.5" width="11" height="23" rx="4"';
      svg = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 14 26">' + shape + ' fill="' + color +
        '" stroke="#fff" stroke-width="1.6"/></svg>';
    }
    const style = "transform:rotate(" + (o.heading || 0) + "deg);opacity:" + (o.heading_confident === false ? 0.55 : 1) +
      ";width:" + w + "px;height:" + h + "px";
    return L.divIcon({ className: "", html: '<div style="' + style + '">' + svg + "</div>", iconSize: [w, h], iconAnchor: [w / 2, h / 2] });
  }

  document.querySelectorAll("#geo-mode button").forEach((b) => {
    b.classList.toggle("on", b.dataset.mode === S.geoMode);
    b.onclick = () => {
      S.geoMode = b.dataset.mode;
      save("ovm.geoMode", S.geoMode);
      document.querySelectorAll("#geo-mode button").forEach((x) => x.classList.toggle("on", x === b));
      showMode();
    };
  });
  document.querySelectorAll("#geo-ground button").forEach((b) => {
    b.classList.toggle("on", b.dataset.ground === S.ground);
    b.onclick = () => {
      S.ground = b.dataset.ground;
      save("ovm.geoGround", S.ground);
      document.querySelectorAll("#geo-ground button").forEach((x) => x.classList.toggle("on", x === b));
      base.setStyle(S.ground);
      view3d.setGround(S.ground);
    };
  });
  new ResizeObserver(() => base.map && base.map.invalidateSize()).observe($("geo-map"));
  return { environmentChanged, cameraChanged, objects, describe: (p) => base.describe(p) };
})();

window.addEventListener("pagehide", () => { if (S.stop) S.stop(); });
loadEnvironments().catch((e) => toast(e.message, "error"));
