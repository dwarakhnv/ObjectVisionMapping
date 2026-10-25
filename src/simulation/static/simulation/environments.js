// Environments page: pair video pixels with map locations, save, and see the estimated cameras in 3D.
//
// Flow: click the video  -> a pending pixel (yellow)
//       click the map    -> a reference point for the selected camera, saved at once:
//                           {pixel, lat, lon} outdoors, {pixel, x, y} on a floor plan.
// Map markers can be dragged; video points can be dragged too. Esc cancels a pending click.
import { BaseMap } from "/static/simulation/basemap.js";
import { GroundView } from "/static/simulation/groundview.js";

const { api, toast, el, fmt } = window.OVM;
const $ = (id) => document.getElementById(id);

const S = {
  slug: null, env: null, analysis: null,   // the saved environment and what the server computed from it
  camId: null,                             // selected camera
  pending: null,                           // [x, y] video pixel waiting for its map click
  frozen: false, natural: null,            // video frame size [w, h]
  sources: [],                             // cameras in config/secrets.json
};

// ================================================================ environments: load / save
async function loadList(select) {
  const r = await api("/api/envs");
  const sel = $("env-select");
  sel.replaceChildren(...r.environments.map((e) => el("option", { value: e.slug, text: e.name })));
  const want = select || new URLSearchParams(location.search).get("env") || remembered("ovm.env");
  const has = r.environments.some((e) => e.slug === want);
  $("no-env").classList.toggle("hidden", r.environments.length > 0);
  $("env-body").classList.toggle("hidden", r.environments.length === 0);
  if (r.environments.length) await openEnv(has ? want : r.environments[0].slug);
}

async function openEnv(slug) {
  const r = await api("/api/envs/" + slug);
  S.slug = r.slug;
  S.env = r.environment;
  S.analysis = r.analysis;
  await base.setSpace(S.env.space || { kind: "geo" });
  $("env-select").value = S.slug;
  $("env-tracking").checked = !!S.env.tracking;
  remember("ovm.env", S.slug);
  history.replaceState(null, "", "?env=" + encodeURIComponent(S.slug));
  const cams = S.env.cameras;
  if (!cams.some((c) => idOf(c) === S.camId)) S.camId = cams.length ? idOf(cams[0]) : null;
  setState("Saved", "good");
  renderAll(true);
}

let saveTimer = null;
function changed() {
  setState("Saving…", "");
  renderAll(false);
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, 300);
}

async function save() {
  try {
    const r = await api("/api/envs/" + S.slug, { method: "PUT", body: S.env });
    S.analysis = r.analysis;
    if (r.slug !== S.slug) { S.slug = r.slug; await loadList(S.slug); return; }
    setState("Saved", "good");
    renderAll(false);
  } catch (e) {
    setState("Not saved", "bad");
    toast(e.message, "error");
  }
}

function setState(text, kind) {
  const p = $("save-state");
  p.textContent = text;
  p.className = "pill " + (kind || "");
}

$("env-select").onchange = (e) => openEnv(e.target.value);
// New environment: outdoors (map) or indoors (a floor plan).
const newEnv = async () => {
  const plans = (await api("/api/plans")).plans;
  const f = $("new-form").elements;
  f.plan.replaceChildren(...(plans.length ? plans.map((p) => el("option", { value: p.slug, text: p.name + " (" + p.units + ")" }))
    : [el("option", { value: "", text: "No floor plans yet" })]));
  $("new-card").classList.remove("hidden");
  f.name.focus();
  syncNewKind();
};
const syncNewKind = () => {
  const indoor = $("new-form").elements.kind.value === "plan";
  $("new-plan-row").classList.toggle("hidden", !indoor);
};
$("new-form").addEventListener("change", syncNewKind);
$("new-cancel").onclick = () => $("new-card").classList.add("hidden");
$("new-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const space = f.kind.value === "plan" ? { kind: "plan", plan: f.plan.value } : { kind: "geo" };
  if (space.kind === "plan" && !space.plan) { toast("Create a floor plan first (Floor plans page)", "error"); return; }
  try {
    const r = await api("/api/envs", { method: "POST", body: { name: f.name.value, space, cameras: [] } });
    $("new-card").classList.add("hidden");
    e.target.reset();
    await loadList(r.slug);
  } catch (err) { toast(err.message, "error"); }
};
$("env-new").onclick = newEnv;
$("env-new-2").onclick = newEnv;
$("env-tracking").onchange = (e) => { S.env.tracking = e.target.checked; changed(); };
$("env-rename").onclick = () => {
  const name = prompt("New name:", S.env.name);
  if (name && name !== S.env.name) { S.env.name = name; changed(); }
};
$("env-delete").onclick = async () => {
  if (!confirm("Delete the environment '" + S.env.name + "' and its reference points?")) return;
  await api("/api/envs/" + S.slug, { method: "DELETE" });
  S.slug = null;
  await loadList();
};

// ================================================================ cameras
const slug = (s) => String(s).trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 64) || "unnamed";
const idOf = (c) => slug(c.name);
const camera = () => (S.env ? S.env.cameras.find((c) => idOf(c) === S.camId) : null);
const camInfo = (id) => (S.analysis ? S.analysis.cameras.find((c) => c.id === id) : null);

async function loadSources() {
  try { S.sources = (await api("/api/cameras")).cameras; } catch (e) { S.sources = []; }
}

function renderCameraTabs() {
  const tabs = $("camera-tabs");
  tabs.replaceChildren(...S.env.cameras.map((c) => el("button", {
    class: idOf(c) === S.camId ? "on" : "", text: c.name,
    onclick: () => { S.camId = idOf(c); cancelPending(); renderAll(true); },
  })));
  tabs.classList.toggle("hidden", !S.env.cameras.length);
  const link = $("detect-link");
  link.classList.toggle("hidden", !S.camId);
  if (S.camId) link.href = "/detect/?env=" + encodeURIComponent(S.slug) + "&camera=" + encodeURIComponent(S.camId);
  // Cameras not used yet, grouped: network (secrets.json) and plugged into this computer.
  const used = new Set(S.env.cameras.map((c) => c.source));
  const free = S.sources.filter((s) => !used.has(s.id));
  const group = (label, list) => (list.length ? el("optgroup", { label }, list.map((s) =>
    el("option", { value: s.id, text: s.name + (s.kind === "network" ? "" : " (" + s.kind + ")") }))) : null);
  $("camera-add").replaceChildren(el("option", { value: "", text: free.length ? "+ Add camera…" : "No more cameras found" }),
    ...[group("Network cameras", free.filter((s) => s.kind === "network")),
        group("On this computer", free.filter((s) => s.kind !== "network"))].filter(Boolean));
}

$("camera-add").onchange = (e) => {
  const src = S.sources.find((s) => s.id === e.target.value);
  e.target.value = "";
  if (!src) return;
  let name = src.name, n = 2;
  while (S.env.cameras.some((c) => idOf(c) === slug(name))) name = src.name + " " + n++;
  S.env.cameras.push({ name, source: src.id, resolution: [1920, 1080], reference_points: [] });
  S.camId = slug(name);
  changed();
  renderAll(true);
};

// ================================================================ video (left)
const video = $("video"), overlay = $("video-overlay");

function showVideo(reload) {
  const cam = camera();
  $("video-empty").classList.toggle("hidden", !!(cam && cam.source));
  if (!cam || !cam.source) { video.removeAttribute("src"); return; }
  if (!reload) return;
  video.onload = () => {
    S.natural = [video.naturalWidth, video.naturalHeight];
    $("video-size").textContent = S.natural[0] + " × " + S.natural[1];
    drawOverlay();
  };
  video.removeAttribute("src");  // close the previous stream first
  const base = "/api/cameras/" + encodeURIComponent(cam.source);
  video.src = S.frozen ? base + "/snapshot.jpg?t=" + Date.now() : base + "/stream.mjpg?t=" + Date.now();
}

$("freeze").onclick = () => {
  S.frozen = !S.frozen;
  $("freeze").textContent = S.frozen ? "Live" : "Freeze frame";
  $("freeze").classList.toggle("primary", S.frozen);
  showVideo(true);
};
window.addEventListener("pagehide", () => video.removeAttribute("src"));

// Where the picture sits inside the <img> box (object-fit: contain leaves bars).
function videoBox() {
  const r = overlay.getBoundingClientRect();
  if (!S.natural) return null;
  const scale = Math.min(r.width / S.natural[0], r.height / S.natural[1]);
  const w = S.natural[0] * scale, h = S.natural[1] * scale;
  return { left: (r.width - w) / 2, top: (r.height - h) / 2, scale, rect: r };
}

// Camera pixel (in the camera's saved resolution) <-> overlay position.
function toScreen(px, cam, box) {
  const sx = S.natural[0] / cam.resolution[0], sy = S.natural[1] / cam.resolution[1];
  return [box.left + px[0] * sx * box.scale, box.top + px[1] * sy * box.scale];
}
function fromEvent(e, cam, box) {
  const x = (e.clientX - box.rect.left - box.left) / box.scale, y = (e.clientY - box.rect.top - box.top) / box.scale;
  if (x < 0 || y < 0 || x > S.natural[0] || y > S.natural[1]) return null;
  return [x * cam.resolution[0] / S.natural[0], y * cam.resolution[1] / S.natural[1]];
}

function drawOverlay() {
  const r = overlay.getBoundingClientRect();
  overlay.width = r.width * devicePixelRatio;
  overlay.height = r.height * devicePixelRatio;
  const ctx = overlay.getContext("2d");
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  const cam = camera(), box = videoBox();
  if (!cam || !box) return;
  const dot = (p, color, label) => {
    const [x, y] = toScreen(p, cam, box);
    ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2);
    ctx.fillStyle = color; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = "#fff"; ctx.stroke();
    if (label) {
      ctx.font = "600 12px -apple-system, Segoe UI, sans-serif";
      ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,.7)"; ctx.strokeText(label, x + 10, y - 8);
      ctx.fillStyle = "#fff"; ctx.fillText(label, x + 10, y - 8);
    }
  };
  cam.reference_points.forEach((p, i) => dot(p.pixel, "#ff2d92", p.name || "P" + (i + 1)));
  if (S.pending) dot(S.pending, "#ffd60a", "?");
}
new ResizeObserver(drawOverlay).observe(overlay);
new ResizeObserver(() => base.map && base.map.invalidateSize()).observe($("map"));

// Click = new pending pixel; drag an existing point = move it.
let drag = null;
overlay.addEventListener("pointerdown", (e) => {
  const cam = camera(), box = videoBox();
  if (!cam || !box) return;
  const hit = cam.reference_points.findIndex((p) => {
    const [x, y] = toScreen(p.pixel, cam, box);
    return Math.hypot(x - (e.clientX - box.rect.left), y - (e.clientY - box.rect.top)) < 10;
  });
  if (hit >= 0) { drag = { index: hit, moved: false }; overlay.setPointerCapture(e.pointerId); return; }
  const px = fromEvent(e, cam, box);
  if (!px) return;
  if (!cam.reference_points.length && S.natural) cam.resolution = S.natural.slice();  // first point: use the feed's size
  S.pending = fromEvent(e, cam, box);
  setHint(true);
  drawOverlay();
});
overlay.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const cam = camera(), px = fromEvent(e, cam, videoBox());
  if (!px) return;
  cam.reference_points[drag.index].pixel = px;
  drag.moved = true;
  drawOverlay();
});
overlay.addEventListener("pointerup", () => { if (drag && drag.moved) changed(); drag = null; });

function setHint(waiting) {
  const hint = $("hint");
  const where = base.isPlan ? "floor plan" : "map";
  hint.textContent = waiting ? "Now click the same spot on the " + where + " (Esc to cancel)."
    : "Click a spot on the ground in the video, then click the same spot on the " + where + ".";
  hint.classList.toggle("active", waiting);
  $("map").classList.toggle("waiting", waiting);
}
function cancelPending() { S.pending = null; setHint(false); drawOverlay(); }
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && S.pending) cancelPending(); });

// ================================================================ map (right): outdoor map or floor plan
let markers = null;
const base = new BaseMap($("map"), {
  onReady: (m) => { markers = L.layerGroup().addTo(m); },
  onClick: (pos) => {
    const cam = camera();
    if (!S.pending || !cam) return;
    const n = cam.reference_points.length + 1;
    cam.reference_points.push(Object.assign({ name: "P" + n, pixel: S.pending }, pos));
    cancelPending();
    changed();
  },
});
base.style = remembered("ovm.mapStyle") || "satellite";
document.querySelectorAll("#map-style button").forEach((b) => {
  b.classList.toggle("on", b.dataset.style === base.style);
  b.onclick = () => {
    base.setStyle(b.dataset.style);
    remember("ovm.mapStyle", b.dataset.style);
    document.querySelectorAll("#map-style button").forEach((x) => x.classList.toggle("on", x === b));
  };
});
$("goto").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || base.isPlan) return;
  const m = e.target.value.match(/(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)/);
  if (m) base.map.setView([+m[1], +m[2]], 19); else toast("Type a latitude and longitude, e.g. 35.6858, -78.8781");
});

const pin = (text, cls) => L.divIcon({ className: "", html: '<div class="pin ' + (cls || "") + '">' + text + "</div>", iconSize: [24, 24], iconAnchor: [12, 12] });

function drawMap(fit) {
  if (!markers) return;
  markers.clearLayers();
  $("map-style").classList.toggle("hidden", base.isPlan);
  $("goto").classList.toggle("hidden", base.isPlan);
  $("map-title").textContent = base.isPlan ? "Floor plan · " + base.plan.name : "Map";
  const cam = camera();
  if (!cam) return;
  cam.reference_points.forEach((p, i) => {
    const m = L.marker(base.toLatLng(p), { icon: pin(i + 1), draggable: true, title: p.name || "P" + (i + 1) }).addTo(markers);
    m.on("dragend", () => { Object.assign(p, base.fromLatLng(m.getLatLng())); changed(); });
  });
  const info = camInfo(S.camId);
  if (info && info.pose) {  // estimated camera position and view direction
    const pose = info.pose;
    L.marker(base.toLatLng(pose), { icon: pin("📷", "cam"), interactive: false }).addTo(markers);
    L.polyline([base.toLatLng(pose), base.toLatLng(base.offset(pose, pose.heading, base.isPlan ? 3 : 13))],
      { color: "#0a84ff", weight: 3, dashArray: "6 4" }).addTo(markers);
  }
  if (fit) base.fit(cam.reference_points.concat(info && info.pose ? [info.pose] : []));
}

// ================================================================ lists (below)
function renderPoints() {
  const cam = camera(), info = camInfo(S.camId);
  const tbody = document.querySelector("#points tbody");
  if (!cam) { tbody.replaceChildren(); $("points-note").textContent = ""; return; }
  const errs = (info && info.errors_m) || [];
  $("pos-a").textContent = base.isPlan ? "x (" + base.space.units + ")" : "Latitude";
  $("pos-b").textContent = base.isPlan ? "y (" + base.space.units + ")" : "Longitude";
  const n = cam.reference_points.length;
  $("points-note").textContent = " · " + cam.name + " · " + n + " point" + (n === 1 ? "" : "s") +
    (n < 4 ? " (need at least 4)" : n === 4 ? " (5+ lets you check accuracy)" : "");
  tbody.replaceChildren(...cam.reference_points.map((p, i) => {
    const name = el("input", { value: p.name || "" });
    name.onchange = () => { p.name = name.value; changed(); };
    const e = errs[i];
    return el("tr", {}, [
      el("td", {}, name),
      el("td", { class: "num", text: Math.round(p.pixel[0]) + ", " + Math.round(p.pixel[1]) }),
      el("td", { class: "num", text: base.isPlan ? fmt(p.x, 2) : fmt(p.lat, 7) }),
      el("td", { class: "num", text: base.isPlan ? fmt(p.y, 2) : fmt(p.lon, 7) }),
      el("td", { class: "num", text: e === undefined ? "–" : fmt(e, 2) + " m", style: e > 1 ? "color:var(--danger)" : "" }),
      el("td", {}, el("button", { class: "small danger", text: "Remove", onclick: () => { cam.reference_points.splice(i, 1); changed(); } })),
    ]);
  }));
}

function renderCameras() {
  const tbody = document.querySelector("#cameras tbody");
  tbody.replaceChildren(...S.env.cameras.map((c) => {
    const info = camInfo(idOf(c)) || {}, pose = info.pose;
    const src = S.sources.find((s) => s.id === c.source);
    return el("tr", { class: idOf(c) === S.camId ? "sel" : "" }, [
      el("td", {}, [el("strong", { text: c.name }), el("div", { class: "small muted", text: src ? src.url : (c.source || "no source") })]),
      el("td", { class: "num", text: String(c.reference_points.length) }),
      el("td", { class: "num", text: info.accuracy_m === undefined ? "–" : fmt(info.accuracy_m, 2) + " m" }),
      el("td", { class: "num", text: pose ? fmt(pose.height_m, 1) + " m" : "–" }),
      el("td", { class: "num", text: pose ? fmt(pose.heading, 0) + "°" : "–" }),
      el("td", { class: "num", text: pose ? fmt(pose.tilt, 0) + "°" : "–" }),
      el("td", { class: "num", text: pose ? fmt(pose.hfov, 0) + "°" : "–" }),
      el("td", { class: "small " + (info.error ? "muted" : ""), text: info.error || "Ready" }),
      el("td", {}, el("button", { class: "small danger", text: "Remove", onclick: () => {
        if (!confirm("Remove " + c.name + " and its reference points from this environment?")) return;
        S.env.cameras = S.env.cameras.filter((x) => x !== c);
        S.camId = S.env.cameras.length ? idOf(S.env.cameras[0]) : null;
        changed();
        renderAll(true);
      } })),
    ]);
  }));
}

// ================================================================ 3D view (bottom)
const view3d = new GroundView($("view3d"));
view3d.ground = remembered("ovm.ground3d") || "satellite";
document.querySelectorAll("#view3d-mode button").forEach((b) => (b.onclick = () => {
  document.querySelectorAll("#view3d-mode button").forEach((x) => x.classList.toggle("on", x === b));
  view3d.fit(b.dataset.mode === "top");
}));
document.querySelectorAll("#view3d-ground button").forEach((b) => {
  b.classList.toggle("on", b.dataset.ground === view3d.ground);
  b.onclick = () => {
    remember("ovm.ground3d", b.dataset.ground);
    document.querySelectorAll("#view3d-ground button").forEach((x) => x.classList.toggle("on", x === b));
    view3d.setGround(b.dataset.ground);
  };
});

async function render3d() {
  const ready = await view3d.setAnalysis(S.analysis);
  $("view3d-empty").classList.toggle("hidden", !!ready);
}

// ================================================================ glue
function renderAll(reloadVideo) {
  if (!S.env) return;
  if (base.map) base.map.invalidateSize();  // the map may have been hidden (size 0) when Leaflet started
  // Indoors the 3D ground is the floor plan; outdoors satellite or street map.
  document.querySelectorAll("#view3d-ground button").forEach((b) => {
    b.classList.toggle("hidden", base.isPlan && b.dataset.ground === "map");
    if (b.dataset.ground === "satellite") b.textContent = base.isPlan ? "Floor plan" : "Satellite";
  });
  renderCameraTabs();
  showVideo(reloadVideo);
  drawOverlay();
  drawMap(reloadVideo);
  renderPoints();
  renderCameras();
  render3d();
}

function remembered(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function remember(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } }

loadSources().then(() => loadList()).catch((e) => toast(e.message, "error"));
