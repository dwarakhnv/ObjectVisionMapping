// Live map: the environment in 3D with every object its calibrated cameras
// see, fused across cameras, plus a small live tile per camera.
import { EnvScene } from "ovm/scene3d.js";

const { api, toast, el, fmt } = window.OVM;
const slug = window.OVM_ENV_SLUG;
const classes = JSON.parse(document.getElementById("object-classes").textContent);
const hidden = new Set();
const scene = new EnvScene(document.getElementById("viewer"), {});
let env = null, objects = [], cameraStatus = {};

// ---------------------------------------------------------------- controls
const toggles = document.getElementById("class-toggles");
for (const c of classes) {
  const box = el("input", { type: "checkbox", checked: true });
  const t = el("label", { class: "toggle" }, [box, el("i", { class: "dot", style: "background:var(--" + c.key + ", var(--other))" }), c.label]);
  box.onchange = () => { if (box.checked) hidden.delete(c.key); else hidden.add(c.key); t.classList.toggle("off", !box.checked); render(); };
  toggles.append(t);
}
document.getElementById("legend").replaceChildren(
  ...classes.map((c) => el("span", {}, [el("i", { class: "dot", style: "background:var(--" + c.key + ", var(--other))" }), c.label])),
  el("span", {}, [el("i", { class: "dot", style: "background:var(--refpoint)" }), "Reference point"]),
  el("span", {}, [el("i", { class: "dot", style: "background:var(--camera)" }), "Camera"]));
document.querySelectorAll("#view-mode button").forEach((b) => (b.onclick = () => {
  document.querySelectorAll("#view-mode button").forEach((x) => x.classList.toggle("on", x === b));
  scene.resetFov();
  scene.fit(b.dataset.mode === "top");
}));
document.querySelectorAll("#layers button").forEach((b) => (b.onclick = () => { b.classList.toggle("on"); scene.setLayer(b.dataset.layer, b.classList.contains("on")); }));

// ---------------------------------------------------------------- camera tiles
function buildTiles() {
  const box = document.getElementById("tiles");
  box.replaceChildren();
  for (const cam of env.environment.cameras) {
    const canvas = el("canvas", { class: "hidden", style: "display:block;width:100%" });
    const empty = el("div", { class: "empty", style: "padding:40px 12px", text: cam.source.kind === "none" ? "No source paired" : "Starting…" });
    const status = el("span", { class: "pill", text: cam.calibration.solved && cam.calibration.solved.rmse_px !== undefined ? "Calibrated" : "Not calibrated" });
    if (!(cam.calibration.solved && cam.calibration.solved.rmse_px !== undefined)) status.classList.add("warn");
    const tile = el("div", { class: "card", style: "padding:10px" }, [
      el("div", { class: "row", style: "margin-bottom:8px" }, [el("strong", { text: cam.name }), el("div", { class: "spacer" }), status,
        el("a", { class: "btn small", href: "/detect/?feed=" + encodeURIComponent(slug + "." + cam.id), text: "Open" }),
        el("button", { class: "small", text: "Look through", onclick: () => scene.viewFrom(cam.id) })]),
      el("div", { class: "feed" }, [empty, canvas]),
    ]);
    box.append(tile);
    if (cam.source.kind === "none") continue;
    const trails = new Map();
    OVMOverlay.follow(slug + "." + cam.id, (img, r) => {
      OVMOverlay.updateTrails(trails, r, 20);
      canvas.classList.remove("hidden");
      empty.classList.add("hidden");
      OVMOverlay.draw(canvas, img, r, { hidden, trails, compact: true, show: { masks: true, boxes: true, labels: true, trails: true } });
    }, (r) => {
      if (r.status === "error") { canvas.classList.add("hidden"); empty.classList.remove("hidden"); empty.textContent = r.error || "Stopped"; }
    });
  }
}

// ---------------------------------------------------------------- world objects
async function poll() {
  for (;;) {
    try {
      const r = await api("/api/mapping/" + slug + "/objects");
      objects = r.objects;
      cameraStatus = r.cameras;
      render();
    } catch (e) { /* server restarting: keep trying */ }
    await new Promise((res) => setTimeout(res, 150));
  }
}

function render() {
  const shown = objects.filter((o) => !hidden.has(o.class));
  scene.setObjects(shown.map((o) => ({
    key: o.id, cls: o.class, world: o.world, heading: o.heading, dims: o.dimensions,
    label: "#" + o.id + " " + o.label + (o.seen_by.length > 1 ? " · " + o.seen_by.length + " cams" : ""),
  })));
  document.getElementById("count-pill").textContent = shown.length + " placed";
  const cs = env.environment.coordinates;
  document.querySelector("#objects tbody").replaceChildren(...shown.map((o) => el("tr", {}, [
    el("td", { text: "#" + o.id }),
    el("td", {}, [el("span", { class: "row", style: "gap:6px;flex-wrap:nowrap" }, [el("i", { class: "dot", style: "background:var(--" + o.class + ", var(--other))" }), o.label])]),
    el("td", { class: "small", text: o.position ? (cs.kind === "geo" ? fmt(o.position.lat, 6) + ", " + fmt(o.position.lon, 6) : fmt(o.position.x, 1) + ", " + fmt(o.position.y, 1) + " " + cs.units) : "–" }),
    el("td", { class: "num small", text: o.speed_mps === null || o.speed_mps === undefined ? "–" : fmt(o.speed_mps * 3.6, 0) + " km/h" }),
  ])));
  const cams = Object.values(cameraStatus);
  const calibrated = cams.filter((c) => c.calibrated && c.source !== "none").length;
  document.getElementById("note").textContent = calibrated
    ? "Objects from " + calibrated + " calibrated camera" + (calibrated === 1 ? "" : "s") + ". Objects seen by several cameras are merged."
    : "No calibrated camera with a source yet: calibrate one to place objects here.";
}

async function init() {
  env = await api("/api/environments/" + slug);
  scene.setLayer("refpoints", false);  // objects matter more here; the Points button brings them back
  scene.setWorld(env.world);
  buildTiles();
  poll();
}
init().catch((e) => toast(e.message, "error"));
