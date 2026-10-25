// Floor plan editor: draw walls, rooms, doors, windows, boxes and labels in real units,
// optionally over an uploaded image whose scale you set from a known distance.
import { UNITS, UNIT_NAMES, drawPlan, loadImage, planImageUrl } from "/static/simulation/planrender.js";

const { api, toast, el, fmt } = window.OVM;
const $ = (id) => document.getElementById(id);
const canvas = $("canvas"), ctx = canvas.getContext("2d");

const S = {
  slug: null, plan: null, bg: null, dirty: false, usedBy: [],
  tool: "select", draft: [], selected: null, drag: null,
  view: { scale: 20, ox: 40, oy: 40 },          // pixels per unit, canvas position of (0, 0)
  snap: true, grid: 1, thickness: 0.15, undo: [], mouse: null,
};

const HINTS = {
  select: "Click a shape to select it, drag to move it or its corners. Drag empty space to pan; scroll to zoom. Delete removes.",
  wall: "Click to start a wall, click again for each corner. Double-click or Enter to finish, Esc to cancel. Shift keeps it straight.",
  room: "Click each corner of the room. Double-click or Enter to close it.",
  door: "Click the hinge side of the opening, then the other side.",
  window: "Click one end of the window, then the other.",
  box: "Click one corner, then the opposite corner (furniture, shelving, counters, pillars).",
  label: "Click where the text should go.",
  scale: "Click two points on the image whose real distance you know.",
};

// ================================================================ loading and saving
async function loadList(select) {
  const r = await api("/api/plans");
  const sel = $("plan-select");
  sel.replaceChildren(...r.plans.map((p) => el("option", { value: p.slug, text: p.name })));
  $("no-plan").classList.toggle("hidden", r.plans.length > 0);
  $("editor").classList.toggle("hidden", r.plans.length === 0);
  if (!r.plans.length) return;
  const want = select || new URLSearchParams(location.search).get("plan");
  await open(r.plans.some((p) => p.slug === want) ? want : r.plans[0].slug);
}

async function open(slug) {
  if (S.dirty && !confirm("Discard unsaved changes to " + S.plan.name + "?")) { $("plan-select").value = S.slug; return; }
  const r = await api("/api/plans/" + encodeURIComponent(slug));
  S.slug = r.slug; S.plan = r.plan; S.usedBy = r.used_by;
  S.selected = null; S.draft = []; S.undo = []; S.dirty = false;
  S.bg = S.plan.background ? await loadImage(planImageUrl(S.plan.background.file)) : null;
  const u = UNITS[S.plan.units];
  S.grid = niceGrid(S.plan.units);
  S.thickness = +(0.15 / u).toFixed(3);
  $("plan-select").value = slug;
  history.replaceState(null, "", "?plan=" + encodeURIComponent(slug));
  setSaved(true);
  renderSide();
  fit();
}

async function save() {
  try {
    const r = await api("/api/plans/" + encodeURIComponent(S.slug), { method: "PUT", body: S.plan });
    S.plan = r.plan; S.usedBy = r.used_by;
    setSaved(true);
    toast("Saved");
    await loadList(S.slug);
  } catch (e) { toast(e.message, "error"); }
}

function setSaved(saved) {
  S.dirty = !saved;
  $("save-state").textContent = saved ? "Saved" : "Unsaved changes";
  $("save-state").className = "pill " + (saved ? "good" : "warn");
}
function changed() { setSaved(false); renderSide(); draw(); }
function snapshot() {
  S.undo.push(JSON.stringify(S.plan));
  if (S.undo.length > 100) S.undo.shift();
}
function undo() {
  if (!S.undo.length) return;
  S.plan = JSON.parse(S.undo.pop());
  S.selected = null;
  changed();
}

$("save").onclick = save;
$("undo").onclick = undo;
$("plan-select").onchange = (e) => open(e.target.value);
window.addEventListener("beforeunload", (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ""; } });

// New plan (optionally from an image)
const showNew = () => { $("new-card").classList.remove("hidden"); $("new-form").elements.name.focus(); };
$("plan-new").onclick = showNew;
$("plan-new-2").onclick = showNew;
$("new-cancel").onclick = () => $("new-card").classList.add("hidden");
$("new-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const body = { name: f.name.value, units: f.units.value, width: +f.width.value, height: +f.height.value, shapes: [] };
  try {
    if (f.image.files[0]) {
      const fd = new FormData();
      fd.append("file", f.image.files[0]);
      const up = await api("/api/plans/images", { method: "POST", body: fd });
      const img = await loadImage(planImageUrl(up.file));
      if (!img) throw new Error("That image could not be read");
      body.height = +(body.width * img.naturalHeight / img.naturalWidth).toFixed(3);  // keep the image's shape
      body.background = { file: up.file, x: 0, y: 0, width: body.width, height: body.height, opacity: 1 };
    }
    const r = await api("/api/plans", { method: "POST", body });
    $("new-card").classList.add("hidden");
    e.target.reset();
    S.dirty = false;
    await loadList(r.slug);
    if (body.background) selectTool("scale");
  } catch (err) { toast(err.message, "error"); }
};
$("plan-delete").onclick = async () => {
  if (!confirm("Delete the floor plan '" + S.plan.name + "'?")) return;
  try {
    await api("/api/plans/" + encodeURIComponent(S.slug), { method: "DELETE" });
    S.dirty = false;
    await loadList();
  } catch (e) { toast(e.message, "error"); }
};

// ================================================================ side panel
function renderSide() {
  const p = S.plan;
  $("p-name").value = p.name;
  $("p-units").value = p.units;
  $("p-width").value = +p.width.toFixed(3);
  $("p-height").value = +p.height.toFixed(3);
  document.querySelectorAll(".u").forEach((n) => (n.textContent = p.units));
  $("grid").value = S.grid;
  $("thickness").value = S.thickness;
  $("used-by").textContent = S.usedBy.length ? S.usedBy.join(", ") : "Not used by an environment yet";

  const bg = p.background;
  const bgBox = $("bg-panel");
  const upload = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/gif" });
  upload.onchange = async () => {
    const file = upload.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const up = await api("/api/plans/images", { method: "POST", body: fd });
      const img = await loadImage(planImageUrl(up.file));
      snapshot();
      const w = p.width, h = +(w * img.naturalHeight / img.naturalWidth).toFixed(3);
      p.background = { file: up.file, x: 0, y: 0, width: w, height: h, opacity: 1 };
      p.height = Math.max(p.height, h);
      S.bg = img;
      changed();
      selectTool("scale");
    } catch (e) { toast(e.message, "error"); }
  };
  if (!bg) {
    bgBox.replaceChildren(el("p", { class: "small muted", text: "Trace over a floor plan, drawing or photo. Upload it, then use Set scale." }), upload);
    return;
  }
  const opacity = el("input", { type: "range", min: "0.1", max: "1", step: "0.05", value: bg.opacity ?? 1 });
  opacity.oninput = () => { bg.opacity = +opacity.value; setSaved(false); draw(); };
  bgBox.replaceChildren(
    el("p", { class: "small muted", text: "Image is " + fmt(bg.width, 2) + " × " + fmt(bg.height, 2) + " " + p.units + "." }),
    el("label", { class: "field" }, [el("span", { text: "Opacity" }), opacity]),
    el("div", { class: "row", style: "margin-top:8px" }, [
      el("button", { class: "small", text: "Set scale", onclick: () => selectTool("scale") }),
      el("button", { class: "small", text: "Fit plan to image", onclick: () => {
        snapshot(); p.width = bg.x + bg.width; p.height = bg.y + bg.height; changed(); fit();
      } }),
      el("button", { class: "small danger", text: "Remove", onclick: () => { snapshot(); delete p.background; S.bg = null; changed(); } }),
    ]),
    el("p", { class: "small muted", style: "margin-top:6px" }, ["Replace: ", upload]));
  renderSelected();
}

function renderSelected() {
  const box = $("sel-panel");
  const sh = S.plan.shapes[S.selected];
  if (!sh) { box.replaceChildren(el("p", { class: "small muted", text: "Click a shape with Select to edit it. Delete removes it." })); return; }
  const rows = [el("p", { class: "small", style: "margin:0 0 6px" }, [el("strong", { text: sh.type[0].toUpperCase() + sh.type.slice(1) }),
    " · " + describe(sh)])];
  const field = (label, key, type) => {
    const input = el("input", { type: type || "text", value: sh[key] ?? "", step: "any" });
    input.onfocus = snapshot;
    input.oninput = () => { sh[key] = type === "number" ? +input.value : input.value; setSaved(false); draw(); };
    return el("label", { class: "field" }, [el("span", { text: label }), input]);
  };
  if (sh.type === "wall") rows.push(field("Thickness (" + S.plan.units + ")", "thickness", "number"));
  if (sh.type === "room" || sh.type === "box") rows.push(field("Name", "name"));
  if (sh.type === "label") rows.push(field("Text", "text"));
  rows.push(el("button", { class: "small danger", style: "margin-top:8px", text: "Delete", onclick: deleteSelected }));
  box.replaceChildren(...rows);
}

function describe(sh) {
  const u = S.plan.units, d = (a, b) => Math.hypot(b[0] - a[0], b[1] - a[1]);
  if (sh.type === "wall") { let L = 0; for (let i = 1; i < sh.points.length; i++) L += d(sh.points[i - 1], sh.points[i]); return fmt(L, 2) + " " + u + " long"; }
  if (sh.type === "room") return fmt(area(sh.points), 2) + " " + u + "²";
  if (sh.type === "door" || sh.type === "window") return fmt(d(...sh.points), 2) + " " + u + " wide";
  if (sh.type === "box") return fmt(Math.abs(sh.points[1][0] - sh.points[0][0]), 2) + " × " + fmt(Math.abs(sh.points[1][1] - sh.points[0][1]), 2) + " " + u;
  return "";
}

function area(pts) {
  let a = 0;
  for (let i = 0; i < pts.length; i++) { const [x1, y1] = pts[i], [x2, y2] = pts[(i + 1) % pts.length]; a += x1 * y2 - x2 * y1; }
  return Math.abs(a) / 2;
}

$("p-name").oninput = (e) => { S.plan.name = e.target.value; setSaved(false); };
for (const k of ["width", "height"]) {
  $("p-" + k).onchange = (e) => { const v = +e.target.value; if (v > 0) { snapshot(); S.plan[k] = v; changed(); } };
}
$("p-units").onchange = (e) => {
  // Convert everything so the drawing keeps its real size.
  const from = S.plan.units, to = e.target.value, k = UNITS[from] / UNITS[to];
  snapshot();
  const p = S.plan;
  p.units = to; p.width *= k; p.height *= k;
  for (const sh of p.shapes) { sh.points = sh.points.map(([x, y]) => [x * k, y * k]); if (sh.thickness != null) sh.thickness *= k; }
  if (p.background) for (const key of ["x", "y", "width", "height"]) p.background[key] *= k;
  S.grid = niceGrid(to); S.thickness = +(S.thickness * k).toFixed(3);
  S.view.scale /= k;
  changed();
  toast("Converted to " + UNIT_NAMES[to]);
};
$("snap").onchange = (e) => (S.snap = e.target.checked);
$("grid").onchange = (e) => { if (+e.target.value > 0) { S.grid = +e.target.value; draw(); } };
$("thickness").onchange = (e) => { if (+e.target.value >= 0) S.thickness = +e.target.value; };

function niceGrid(units) { return { m: 0.5, cm: 50, mm: 500, ft: 1, in: 12, yd: 0.5 }[units] || 1; }

// ================================================================ tools
function selectTool(tool) {
  S.tool = tool;
  S.draft = [];
  document.querySelectorAll("#tools button").forEach((b) => b.classList.toggle("on", b.dataset.tool === tool));
  $("hint").textContent = tool === "scale" && !S.plan.background ? "Upload a background image first (right panel)." : HINTS[tool];
  canvas.style.cursor = tool === "select" ? "default" : "crosshair";
  draw();
}
document.querySelectorAll("#tools button").forEach((b) => (b.onclick = () => selectTool(b.dataset.tool)));

function deleteSelected() {
  if (S.selected == null) return;
  snapshot();
  S.plan.shapes.splice(S.selected, 1);
  S.selected = null;
  changed();
}

function finishDraft() {
  const t = S.tool, d = S.draft;
  if ((t === "wall" && d.length >= 2) || (t === "room" && d.length >= 3)) {
    snapshot();
    const sh = { type: t, points: d.map((p) => p.slice()) };
    if (t === "wall") sh.thickness = S.thickness;
    if (t === "room") sh.name = prompt("Room name (optional):", "") || "";
    S.plan.shapes.push(sh);
    S.selected = S.plan.shapes.length - 1;
    changed();
  }
  S.draft = [];
  draw();
}

// ================================================================ coordinates, snapping
function toPlan(e) {
  const r = canvas.getBoundingClientRect();
  return [(e.clientX - r.left - S.view.ox) / S.view.scale, (e.clientY - r.top - S.view.oy) / S.view.scale];
}
function snapPoint(p, e) {
  // Prefer an existing corner within 10 px, then the grid; Shift keeps the segment straight.
  const tol = 10 / S.view.scale;
  for (const sh of S.plan.shapes) for (const q of sh.points) if (Math.hypot(q[0] - p[0], q[1] - p[1]) < tol) return q.slice();
  let out = p.slice();
  if (S.snap) out = out.map((v) => Math.round(v / S.grid) * S.grid);
  const prev = S.draft[S.draft.length - 1];
  if (e && e.shiftKey && prev) {
    if (Math.abs(out[0] - prev[0]) > Math.abs(out[1] - prev[1])) out[1] = prev[1]; else out[0] = prev[0];
  }
  return out;
}

// Which shape is under the pointer (topmost first)?
function hitTest(p) {
  const tol = 8 / S.view.scale;
  for (let i = S.plan.shapes.length - 1; i >= 0; i--) {
    const sh = S.plan.shapes[i], pts = sh.points;
    if (sh.type === "label" && Math.hypot(pts[0][0] - p[0], pts[0][1] - p[1]) < tol * 3) return i;
    if (sh.type === "box") {
      const [a, b] = pts;
      if (p[0] >= Math.min(a[0], b[0]) && p[0] <= Math.max(a[0], b[0]) && p[1] >= Math.min(a[1], b[1]) && p[1] <= Math.max(a[1], b[1])) return i;
    }
    for (let k = 1; k < pts.length; k++) if (segDist(p, pts[k - 1], pts[k]) < tol + (sh.thickness || 0) / 2) return i;
  }
  for (let i = S.plan.shapes.length - 1; i >= 0; i--) {  // inside a room
    if (S.plan.shapes[i].type === "room" && inside(p, S.plan.shapes[i].points)) return i;
  }
  return null;
}
function segDist(p, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1], L = dx * dx + dy * dy;
  const t = L ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L)) : 0;
  return Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy);
}
function inside(p, poly) {
  let c = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > p[1]) !== (yj > p[1]) && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi) c = !c;
  }
  return c;
}

// ================================================================ pointer and keys
canvas.tabIndex = 0;  // so keys (Enter, Esc, Delete) go to the drawing after a click, not to a form field
canvas.addEventListener("pointerdown", (e) => {
  canvas.focus({ preventScroll: true });
  canvas.setPointerCapture(e.pointerId);
  const raw = toPlan(e);
  if (e.button === 1 || e.button === 2 || (S.tool === "select" && e.altKey)) { S.drag = { kind: "pan", x: e.clientX, y: e.clientY, ox: S.view.ox, oy: S.view.oy }; return; }
  if (S.tool === "select") {
    // A corner of the selected shape?
    const sel = S.plan.shapes[S.selected];
    if (sel) {
      const k = sel.points.findIndex((q) => Math.hypot(q[0] - raw[0], q[1] - raw[1]) < 8 / S.view.scale);
      if (k >= 0) { snapshot(); S.drag = { kind: "vertex", k }; return; }
    }
    S.selected = hitTest(raw);
    renderSelected();
    if (S.selected != null) { snapshot(); S.drag = { kind: "move", last: raw, moved: false }; }
    else S.drag = { kind: "pan", x: e.clientX, y: e.clientY, ox: S.view.ox, oy: S.view.oy };
    draw();
    return;
  }
  const p = snapPoint(raw, e);
  if (S.tool === "label") {
    const text = prompt("Label text:", "");
    if (text) { snapshot(); S.plan.shapes.push({ type: "label", points: [p], text }); changed(); }
    return;
  }
  if (S.tool === "scale") {
    if (!S.plan.background) return;
    S.draft.push(raw);
    if (S.draft.length === 2) applyScale();
    draw();
    return;
  }
  S.draft.push(p);
  if (["door", "window", "box"].includes(S.tool) && S.draft.length === 2) {
    snapshot();
    S.plan.shapes.push({ type: S.tool, points: S.draft.map((q) => q.slice()), ...(S.tool === "box" ? { name: "" } : {}) });
    S.draft = [];
    S.selected = S.plan.shapes.length - 1;
    changed();
  }
  draw();
});

canvas.addEventListener("pointermove", (e) => {
  const raw = toPlan(e);
  S.mouse = S.tool === "select" || S.tool === "scale" ? raw : snapPoint(raw, e);
  $("coords").textContent = fmt(raw[0], 2) + ", " + fmt(raw[1], 2) + " " + S.plan.units;
  const d = S.drag;
  if (d && d.kind === "pan") { S.view.ox = d.ox + e.clientX - d.x; S.view.oy = d.oy + e.clientY - d.y; }
  if (d && d.kind === "move") {
    const sh = S.plan.shapes[S.selected];
    let dx = raw[0] - d.last[0], dy = raw[1] - d.last[1];
    if (S.snap) { dx = Math.round(dx / S.grid) * S.grid; dy = Math.round(dy / S.grid) * S.grid; }
    if (dx || dy) { sh.points = sh.points.map(([x, y]) => [x + dx, y + dy]); d.last = [d.last[0] + dx, d.last[1] + dy]; d.moved = true; setSaved(false); }
  }
  if (d && d.kind === "vertex") { S.plan.shapes[S.selected].points[d.k] = snapPoint(raw); setSaved(false); }
  draw();
});

canvas.addEventListener("pointerup", () => {
  if (S.drag && S.drag.kind === "move" && !S.drag.moved) S.undo.pop();  // a click, not a move
  if (S.drag && S.drag.kind !== "pan") renderSelected();
  S.drag = null;
});
canvas.addEventListener("dblclick", () => { if (S.tool === "wall" || S.tool === "room") { S.draft.pop(); finishDraft(); } });
canvas.addEventListener("contextmenu", (e) => e.preventDefault());
canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = canvas.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
  const k = Math.exp(-e.deltaY * 0.0015);
  S.view.ox = mx - (mx - S.view.ox) * k;
  S.view.oy = my - (my - S.view.oy) * k;
  S.view.scale *= k;
  draw();
}, { passive: false });

document.addEventListener("keydown", (e) => {
  if (!S.plan || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); return; }
  if (e.key === "Delete" || e.key === "Backspace") { deleteSelected(); return; }
  if (e.key === "Enter") { e.preventDefault(); finishDraft(); return; }
  if (e.key === "Escape") { if (S.draft.length) { S.draft = []; draw(); } else { S.selected = null; renderSelected(); draw(); } return; }
  const keys = { v: "select", w: "wall", r: "room", d: "door", n: "window", b: "box", t: "label", s: "scale" };
  if (keys[e.key.toLowerCase()]) selectTool(keys[e.key.toLowerCase()]);
});

function applyScale() {
  const [a, b] = S.draft;
  S.draft = [];
  const measured = Math.hypot(b[0] - a[0], b[1] - a[1]);
  const real = parseFloat(prompt("How far apart are those two points in reality (" + UNIT_NAMES[S.plan.units] + ")?", ""));
  if (!(real > 0) || !(measured > 0)) return;
  // Resize only the image (about its top-left corner); shapes are already in real units.
  // The plan grows if the image no longer fits. Set the scale first, then trace.
  const k = real / measured, p = S.plan, bg = p.background;
  snapshot();
  bg.width *= k;
  bg.height *= k;
  p.width = Math.max(p.width, bg.x + bg.width);
  p.height = Math.max(p.height, bg.y + bg.height);
  changed();
  fit();
  toast("Scale set: the image is now " + fmt(bg.width, 2) + " × " + fmt(bg.height, 2) + " " + p.units);
  selectTool("select");
}

// ================================================================ drawing
function fit() {
  const r = canvas.getBoundingClientRect();
  const pad = 30, p = S.plan;
  S.view.scale = Math.min((r.width - 2 * pad) / p.width, (r.height - 2 * pad) / p.height);
  S.view.ox = (r.width - p.width * S.view.scale) / 2;
  S.view.oy = (r.height - p.height * S.view.scale) / 2;
  draw();
}
$("fit").onclick = fit;

function draw() {
  if (!S.plan) return;
  const r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(r.width * dpr) || canvas.height !== Math.round(r.height * dpr)) {
    canvas.width = Math.round(r.width * dpr);
    canvas.height = Math.round(r.height * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, r.width, r.height);
  drawPlan(ctx, S.plan, S.view, S.bg, { selected: S.selected });
  drawGrid(r);
  drawDraft();
}

function drawGrid(r) {
  const g = S.grid * S.view.scale;
  if (g < 6) return;  // too dense to be useful
  ctx.save();
  ctx.strokeStyle = "rgba(0, 0, 0, .06)";
  ctx.lineWidth = 1;
  const p = S.plan;
  ctx.beginPath();
  for (let x = 0; x <= p.width + 1e-9; x += S.grid) { const X = S.view.ox + x * S.view.scale; ctx.moveTo(X, S.view.oy); ctx.lineTo(X, S.view.oy + p.height * S.view.scale); }
  for (let y = 0; y <= p.height + 1e-9; y += S.grid) { const Y = S.view.oy + y * S.view.scale; ctx.moveTo(S.view.ox, Y); ctx.lineTo(S.view.ox + p.width * S.view.scale, Y); }
  ctx.stroke();
  ctx.restore();
}

function drawDraft() {
  const pts = S.draft.slice();
  if (!pts.length || !S.mouse) return;
  if (S.tool !== "label") pts.push(S.mouse);
  const P = (q) => [S.view.ox + q[0] * S.view.scale, S.view.oy + q[1] * S.view.scale];
  ctx.save();
  ctx.strokeStyle = "#0a84ff";
  ctx.lineWidth = S.tool === "wall" ? Math.max(2, S.thickness * S.view.scale) : 2;
  ctx.globalAlpha = 0.7;
  ctx.beginPath();
  if (S.tool === "box") {
    const [a, b] = pts.map(P);
    ctx.rect(Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.abs(b[0] - a[0]), Math.abs(b[1] - a[1]));
  } else {
    pts.map(P).forEach((q, i) => (i ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1])));
    if (S.tool === "room" && pts.length > 2) ctx.closePath();
  }
  ctx.stroke();
  // Length of the segment being drawn
  const a = pts[pts.length - 2], b = pts[pts.length - 1];
  if (a && b) {
    const [x, y] = P([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]);
    ctx.globalAlpha = 1;
    ctx.font = "600 12px -apple-system, Segoe UI, sans-serif";
    ctx.fillStyle = "#0a84ff";
    ctx.fillText(fmt(Math.hypot(b[0] - a[0], b[1] - a[1]), 2) + " " + S.plan.units, x + 8, y - 8);
  }
  ctx.restore();
}

new ResizeObserver(draw).observe(canvas);
selectTool("select");
loadList().catch((e) => toast(e.message, "error"));
