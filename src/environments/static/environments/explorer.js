// Environment explorer: view the environment in 3D and edit its reference
// points, cameras and settings. Edits are previewed live and saved on "Save".
import { EnvScene } from "ovm/scene3d.js";

const { api, toast, el, fmt } = window.OVM;
const slug = window.OVM_ENV_SLUG;
const state = { env: null, world: null, tab: "points", sel: { points: null, cameras: null }, dirty: false, pick: null };

const scene = new EnvScene(document.getElementById("viewer"), {
  onPick: (kind, id) => {
    const tab = kind === "camera" ? "cameras" : "points";
    state.sel[tab] = id;
    showTab(tab);
  },
  onGroundClick: (world) => { if (state.pick) state.pick(world); },
});

// ---------------------------------------------------------------- load / save / preview
async function load() {
  const r = await api("/api/environments/" + slug);
  state.env = r.environment;
  state.world = r.world;
  scene.setWorld(r.world);
  updateGridNote();
  render();
}

let previewTimer = null;
function changed() {
  state.dirty = true;
  document.getElementById("save-btn").disabled = false;
  const pill = document.getElementById("dirty-pill");
  pill.textContent = "Unsaved changes";
  pill.className = "pill warn";
  clearTimeout(previewTimer);
  previewTimer = setTimeout(preview, 250);
}

async function preview() {
  try {
    const r = await api("/api/environments/preview", { method: "POST", body: state.env });
    state.world = r.world;
    scene.setWorld(r.world, true);
    setError(null);
  } catch (e) { setError(e.message); }
}

function setError(msg) {
  const pill = document.getElementById("dirty-pill");
  if (msg) { pill.textContent = msg; pill.className = "pill bad"; }
  else if (state.dirty) { pill.textContent = "Unsaved changes"; pill.className = "pill warn"; }
}

document.getElementById("save-btn").onclick = async () => {
  try {
    const r = await api("/api/environments/" + slug, { method: "PUT", body: state.env });
    state.env = r.environment;
    state.world = r.world;
    state.dirty = false;
    scene.setWorld(r.world, true);
    document.getElementById("save-btn").disabled = true;
    const pill = document.getElementById("dirty-pill");
    pill.textContent = "Saved";
    pill.className = "pill good";
    document.getElementById("env-title").textContent = state.env.name;
    render();
    toast("Saved to data/environments/" + slug + "/environment.json");
  } catch (e) { toast(e.message, "error"); }
};
window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });

// ---------------------------------------------------------------- view controls
document.querySelectorAll("#view-mode button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("#view-mode button").forEach((x) => x.classList.toggle("on", x === b));
  scene.resetFov();
  scene.fit(b.dataset.mode === "top");
});
document.querySelectorAll("#layers button").forEach((b) => b.onclick = () => {
  b.classList.toggle("on");
  scene.setLayer(b.dataset.layer, b.classList.contains("on"));
});
document.querySelectorAll("#tabs button").forEach((b) => b.onclick = () => showTab(b.dataset.tab));

function showTab(tab) {
  state.tab = tab;
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x.dataset.tab === tab));
  for (const t of ["points", "cameras", "settings"]) document.getElementById("tab-" + t).classList.toggle("hidden", t !== tab);
  const kind = tab === "cameras" ? "camera" : tab === "points" ? "point" : null;
  scene.setSelection(kind, kind ? state.sel[tab] : null);
  render();
}

function updateGridNote() {
  const cs = state.env.coordinates;
  const unit = cs.kind === "geo" ? "m" : cs.units;
  const step = scene.gridStep / (cs.kind === "geo" ? 1 : cs.meters_per_unit);
  document.getElementById("grid-note").textContent = "Grid: " + fmt(step, step < 1 ? 2 : 0) + " " + unit;
}

// ---------------------------------------------------------------- picking on the map
function startPick(message, fn) {
  const hint = document.getElementById("pick-hint");
  hint.textContent = message + " (Esc to cancel)";
  hint.classList.remove("hidden");
  state.pick = async (world) => {
    state.pick = null;
    hint.classList.add("hidden");
    await fn(world);
  };
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.pick) { state.pick = null; document.getElementById("pick-hint").classList.add("hidden"); }
});
async function worldToPosition(world) {
  const r = await api("/api/environments/convert", { method: "POST", body: { coordinates: state.env.coordinates, world } });
  return r.position;
}

// ---------------------------------------------------------------- forms
const isGeo = () => state.env.coordinates.kind === "geo";
const unit = () => (isGeo() ? "m" : state.env.coordinates.units);

function numField(label, obj, key, opts) {
  opts = opts || {};
  const input = el("input", { type: "number", step: opts.step || "any", value: obj[key] ?? "" });
  input.oninput = () => {
    obj[key] = input.value === "" ? null : Number(input.value);
    if (opts.onchange) opts.onchange();
    changed();
  };
  return el("label", { class: "field" + (opts.wide ? " wide" : "") }, [el("span", { text: label }), input]);
}
function textField(label, obj, key, opts) {
  opts = opts || {};
  const input = el("input", { value: obj[key] ?? "", placeholder: opts.placeholder || "" });
  input.oninput = () => { obj[key] = input.value; changed(); if (opts.onchange) opts.onchange(); };
  return el("label", { class: "field" + (opts.wide === false ? "" : " wide") }, [el("span", { text: label }), input]);
}
function positionFields(pos, what) {
  if (isGeo()) {
    return [numField("Latitude", pos, "lat", { step: "0.000001" }), numField("Longitude", pos, "lon", { step: "0.000001" }),
            numField((what === "camera" ? "Ground alt" : "Altitude") + " (m)", pos, "alt")];
  }
  const u = unit();
  return [numField("East x (" + u + ")", pos, "x"), numField("North y (" + u + ")", pos, "y"),
          numField((what === "camera" ? "Ground z" : "Height z") + " (" + u + ")", pos, "z")];
}
function newId(prefix, list) {
  let i = list.length + 1;
  while (list.some((x) => x.id === prefix + i)) i++;
  return prefix + i;
}
function defaultPosition() {
  const cs = state.env.coordinates;
  if (cs.kind === "geo") return { lat: cs.origin.lat, lon: cs.origin.lon, alt: cs.ground };
  return { x: 0, y: 0, z: cs.ground };
}

function render() {
  if (!state.env) return;
  if (state.tab === "points") renderPoints();
  if (state.tab === "cameras") renderCameras();
  if (state.tab === "settings") renderSettings();
}

function itemTable(items, selId, cols, onSelect) {
  const tbody = el("tbody");
  for (const it of items) {
    const tr = el("tr", { class: "clickable" + (it.id === selId ? " sel" : ""), onclick: () => onSelect(it.id) },
      cols.map((c) => el("td", { class: c.num ? "num" : "", text: c.get(it) })));
    tbody.append(tr);
  }
  return el("table", { class: "list" }, [el("thead", {}, el("tr", {}, cols.map((c) => el("th", { class: c.num ? "num" : "", text: c.label })))), tbody]);
}

function posText(p) {
  return isGeo() ? fmt(p.lat, 6) + ", " + fmt(p.lon, 6) : fmt(p.x, 2) + ", " + fmt(p.y, 2) + ", " + fmt(p.z, 2);
}

function renderPoints() {
  const box = document.getElementById("tab-points");
  box.replaceChildren();
  const pts = state.env.reference_points;
  const add = () => {
    const id = newId("P", pts);
    pts.push({ id, name: id, position: defaultPosition(), description: "" });
    state.sel.points = id;
    changed();
    showTab("points");
    placePoint(pts[pts.length - 1]);
  };
  box.append(el("p", { class: "small muted", text: "Known, measurable spots (corners, pole bases, lane-line ends) that cameras can see. They are matched to pixels to calibrate cameras." }));
  box.append(el("div", { class: "row" }, [el("button", { class: "small primary", text: "Add point", onclick: add }),
    el("span", { class: "small muted", text: pts.length + " point" + (pts.length === 1 ? "" : "s") })]));
  if (pts.length) {
    box.append(itemTable(pts, state.sel.points, [
      { label: "Id", get: (p) => p.id }, { label: "Name", get: (p) => p.name },
      { label: isGeo() ? "Lat, lon" : "x, y, z (" + unit() + ")", get: (p) => posText(p.position) }],
    (id) => { state.sel.points = id; showTab("points"); }));
  }
  const p = pts.find((x) => x.id === state.sel.points);
  if (!p) return;
  box.append(el("h3", { text: "Point " + p.id }));
  const fields = el("div", { class: "fields" }, [
    textField("Id", p, "id", { wide: false, onchange: () => { renameCalibrationRefs(state.sel.points, p.id); state.sel.points = p.id; } }),
    textField("Name", p, "name", { wide: false }),
    ...positionFields(p.position, "point"),
    textField("Description", p, "description"),
  ]);
  box.append(fields);
  box.append(el("div", { class: "row", style: "margin-top:10px" }, [
    el("button", { class: "small", text: "Place on map", onclick: () => placePoint(p) }),
    el("div", { class: "spacer" }),
    el("button", { class: "small danger", text: "Delete", onclick: () => {
      state.env.reference_points = pts.filter((x) => x !== p);
      for (const c of state.env.cameras) c.calibration.points = c.calibration.points.filter((cp) => cp.point_id !== p.id);
      state.sel.points = null; changed(); showTab("points");
    } }),
  ]));
}

function renameCalibrationRefs(oldId, newIdValue) {
  for (const c of state.env.cameras) for (const cp of c.calibration.points) if (cp.point_id === oldId) cp.point_id = newIdValue;
}

function placePoint(p) {
  startPick("Click the ground to place " + p.id, async (world) => {
    const pos = await worldToPosition(world);
    const keepZ = isGeo() ? p.position.alt : p.position.z;
    Object.assign(p.position, pos);
    if (isGeo()) p.position.alt = keepZ; else p.position.z = keepZ;
    changed();
    render();
  });
}

function renderCameras() {
  const box = document.getElementById("tab-cameras");
  box.replaceChildren();
  const cams = state.env.cameras;
  const add = () => {
    const id = newId("cam", cams);
    cams.push({ id, name: "Camera " + (cams.length + 1), position: defaultPosition(), height: isGeo() ? 6 : 6 / state.env.coordinates.meters_per_unit,
                heading: 0, tilt: 15, roll: 0, hfov: 70, resolution: [1920, 1080],
                source: { kind: "none", uri: "", secret: "", loop: true }, calibration: { resolution: [], points: [], solved: {} }, description: "" });
    state.sel.cameras = id;
    changed();
    showTab("cameras");
    startPick("Click the ground where the camera's mast stands", async (world) => {
      const c = cams[cams.length - 1];
      const keep = isGeo() ? c.position.alt : c.position.z;
      Object.assign(c.position, await worldToPosition(world));
      if (isGeo()) c.position.alt = keep; else c.position.z = keep;
      changed();
      render();
    });
  };
  box.append(el("p", { class: "small muted", text: "Fixed cameras: where they stand, how high, which way they point (heading, clockwise from north), how far they tilt down, and their field of view. Pair one with a real camera under Source." }));
  box.append(el("div", { class: "row" }, [el("button", { class: "small primary", text: "Add camera", onclick: add }),
    el("span", { class: "small muted", text: cams.length + " camera" + (cams.length === 1 ? "" : "s") })]));
  if (cams.length) {
    box.append(itemTable(cams, state.sel.cameras, [
      { label: "Id", get: (c) => c.id }, { label: "Name", get: (c) => c.name },
      { label: "Heading", num: true, get: (c) => fmt(c.heading, 0) + "°" },
      { label: "Status", get: (c) => (c.calibration.solved && c.calibration.solved.rmse_px !== undefined ? "Calibrated" : c.source.kind === "none" ? "No source" : "Not calibrated") }],
    (id) => { state.sel.cameras = id; showTab("cameras"); }));
  }
  const c = cams.find((x) => x.id === state.sel.cameras);
  if (!c) return;
  const res = { w: c.resolution[0], h: c.resolution[1] };
  const resChanged = () => { c.resolution = [Math.round(res.w || 0), Math.round(res.h || 0)]; };
  box.append(el("h3", { text: "Camera " + c.id }));
  box.append(el("div", { class: "fields" }, [
    textField("Id", c, "id", { wide: false, onchange: () => (state.sel.cameras = c.id) }),
    textField("Name", c, "name", { wide: false }),
    ...positionFields(c.position, "camera"),
    numField("Mount height (" + unit() + ")", c, "height"),
    numField("Heading (° from N)", c, "heading", { step: "1" }),
    numField("Tilt down (°)", c, "tilt", { step: "0.5" }),
    numField("Roll (°)", c, "roll", { step: "0.5" }),
    numField("Field of view (°)", c, "hfov", { step: "0.5" }),
    numField("Width (px)", res, "w", { step: "1", onchange: resChanged }),
    numField("Height (px)", res, "h", { step: "1", onchange: resChanged }),
  ]));
  box.append(el("div", { class: "row", style: "margin-top:10px" }, [
    el("button", { class: "small", text: "Move on map", onclick: () => startPick("Click the ground where " + c.id + " stands", async (w) => {
      const keep = isGeo() ? c.position.alt : c.position.z;
      Object.assign(c.position, await worldToPosition(w));
      if (isGeo()) c.position.alt = keep; else c.position.z = keep;
      changed(); render();
    }) }),
    el("button", { class: "small", text: "Aim at map point", onclick: () => startPick("Click the ground point at the centre of " + c.id + "'s view", (w) => { aimCamera(c, w); }) }),
    el("button", { class: "small", text: "Look through", onclick: () => scene.viewFrom(c.id) }),
  ]));

  // Source (pairing with a real camera)
  box.append(el("h3", { text: "Source" }));
  const kindSel = el("select", {}, ["none", "file", "url", "device", "image"].map((k) => el("option", { value: k, text: {
    none: "Not paired yet", file: "Video file", url: "Stream URL (RTSP / HTTP)", device: "Local device (webcam)", image: "Still image" }[k] })));
  kindSel.value = c.source.kind;
  kindSel.onchange = () => { c.source.kind = kindSel.value; changed(); render(); };
  const srcFields = [el("label", { class: "field wide" }, [el("span", { text: "Kind" }), kindSel])];
  if (c.source.kind !== "none") {
    const hint = { file: "File name in data/sources (or an absolute path)", url: "rtsp://{username}:{password}@10.0.0.5/stream1", device: "0", image: "File name in data/sources" }[c.source.kind];
    srcFields.push(textField(c.source.kind === "device" ? "Device index" : "Location", c.source, "uri", { placeholder: hint }));
    if (c.source.kind === "url") srcFields.push(textField("Secret name (config/secrets.json → cameras)", c.source, "secret", { placeholder: "front_gate" }));
  }
  box.append(el("div", { class: "fields" }, srcFields));
  if (c.source.kind === "url") box.append(el("p", { class: "small muted", text: "Credentials stay in config/secrets.json. {username} and {password} in the URL are filled from the named entry when the stream opens." }));
  loadSourceList(box, c);

  box.append(el("h3", { text: "Calibration" }));
  const solved = c.calibration.solved || {};
  box.append(el("p", { class: "small muted" }, solved.rmse_px !== undefined
    ? ["Solved from " + solved.points + " points, error " + fmt(solved.rmse_px, 1) + " px. ", el("a", { href: "/mapping/" + slug + "/?camera=" + encodeURIComponent(c.id), text: "Edit calibration" })]
    : [c.calibration.points.length + " point(s) matched. ", el("a", { href: "/mapping/" + slug + "/?camera=" + encodeURIComponent(c.id), text: "Calibrate this camera" })]));

  box.append(el("div", { class: "row", style: "margin-top:12px" }, [el("div", { class: "spacer" }),
    el("button", { class: "small danger", text: "Delete camera", onclick: () => {
      state.env.cameras = cams.filter((x) => x !== c); state.sel.cameras = null; changed(); showTab("cameras");
    } })]));
}

async function loadSourceList(box, c) {
  if (!["file", "image"].includes(c.source.kind)) return;
  try {
    const r = await api("/api/sources");
    const list = r.sources.filter((s) => (c.source.kind === "image" ? s.kind === "image" : s.kind === "video"));
    if (!list.length) return;
    const row = el("div", { class: "row small", style: "margin-top:6px" }, [el("span", { class: "muted", text: "In data/sources:" })]);
    for (const s of list) row.append(el("button", { class: "small", text: s.name, onclick: () => { c.source.uri = s.name; changed(); render(); } }));
    box.querySelector(".fields:last-of-type").after(row);
  } catch (e) { /* the vision app may not be ready */ }
}

function aimCamera(c, target) {
  const cam = state.world.cameras.find((x) => x.id === c.id);
  if (!cam) return;
  const dx = target[0] - cam.world[0], dy = target[1] - cam.world[1], dz = target[2] - cam.world[2];
  c.heading = Math.round(((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360 * 10) / 10;
  c.tilt = Math.round(((Math.atan2(-dz, Math.hypot(dx, dy)) * 180) / Math.PI) * 10) / 10;
  changed();
  render();
}

function renderSettings() {
  const box = document.getElementById("tab-settings");
  box.replaceChildren();
  const env = state.env, cs = env.coordinates;
  box.append(el("div", { class: "fields" }, [
    textField("Name", env, "name"),
    textField("Description", env, "description"),
    numField("Size to show (" + unit() + ")", env, "extent", { onchange: () => setTimeout(updateGridNote, 400) }),
  ]));
  box.append(el("h3", { text: "Coordinates" }));
  if (cs.kind === "geo") {
    box.append(el("p", { class: "small muted", text: "Geo-spatial: positions are latitude / longitude (WGS84) with altitude in metres. The origin anchors the local east/north/up frame the cameras are solved in." }));
    box.append(el("div", { class: "fields" }, [
      numField("Origin latitude", cs.origin, "lat", { step: "0.000001" }), numField("Origin longitude", cs.origin, "lon", { step: "0.000001" }),
      numField("Origin altitude (m)", cs.origin, "alt"), numField("Ground altitude (m)", cs, "ground"),
    ]));
  } else {
    box.append(el("p", { class: "small muted", text: "Custom units: x points east (right on the plan), y north (up on the plan), z up. Set how many metres one unit is so object sizes come out right." }));
    box.append(el("div", { class: "fields" }, [
      textField("Unit name", cs, "units", { wide: false }), numField("Metres per unit", cs, "meters_per_unit"),
      numField("Ground level (" + cs.units + ")", cs, "ground"),
    ]));
  }
  box.append(el("h3", { text: "Storage" }));
  box.append(el("p", { class: "small muted" }, ["Saved as ", el("code", { text: "data/environments/" + slug + "/environment.json" })]));
  box.append(el("div", { class: "row" }, [
    el("a", { class: "btn small", href: "/api/environments/" + slug, target: "_blank", text: "View JSON" }),
    el("div", { class: "spacer" }),
    el("button", { class: "small danger", text: "Delete environment", onclick: async () => {
      if (!confirm("Delete " + env.name + " and its folder? This cannot be undone.")) return;
      await api("/api/environments/" + slug, { method: "DELETE" });
      state.dirty = false;
      location.href = "/legacy/environments/";
    } }),
  ]));
}

load().catch((e) => toast(e.message, "error"));
