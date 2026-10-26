// Recording region: a polygon on the environment's map. Only movement inside it is recorded.
//
// Draw: click "Draw region", click the corners on the map, then "Finish" (or click the first corner).
// Edit: drag a corner. Esc cancels drawing. "Clear" records movement anywhere again.
// remote.js announces the environment with an "ovm:remote-env" event.
import { BaseMap } from "/static/simulation/basemap.js";

const { api, toast } = window.OVM;
const $ = (id) => document.getElementById(id);
const S = { env: null, saved: [], points: [], drawing: false, layer: null };

const base = new BaseMap($("region-map"), {
  onReady: (m) => { S.layer = L.layerGroup().addTo(m); },
  onClick: (p) => {
    if (!S.drawing) return;
    S.points.push(p);
    render();
  },
});

async function openEnv({ env, data }) {
  S.env = env;
  S.saved = data.recording.region || [];
  S.points = S.saved.slice();
  S.drawing = false;
  await base.setSpace(data.environment.map.kind === "plan" ? data.environment.map : { kind: "geo" });
  S.refs = data.cameras.flatMap((c) => c.points || []);
  render();
  base.fit(S.points.length ? S.points : S.refs);
}
document.addEventListener("ovm:remote-env", (e) => openEnv(e.detail));
if (window.OVM_REMOTE_ENV) openEnv(window.OVM_REMOTE_ENV);

function vertex(p, i) {
  const first = S.drawing && i === 0 && S.points.length >= 3;
  const m = L.marker(base.toLatLng(p), {
    draggable: !S.drawing,
    icon: L.divIcon({ className: "region-vertex" + (first ? " close" : ""), iconSize: [14, 14] }),
    title: first ? "Click to finish" : S.drawing ? "" : "Drag to move",
  });
  m.on("click", () => { if (first) finish(); });
  m.on("dragend", () => { S.points[i] = base.fromLatLng(m.getLatLng()); save(); });
  return m;
}

function render() {
  if (!S.layer) return;
  S.layer.clearLayers();
  for (const p of S.refs || []) {
    L.circleMarker(base.toLatLng(p), { radius: 3, color: "#e3007b", weight: 1, fillOpacity: 0.8, interactive: false }).addTo(S.layer);
  }
  const ll = S.points.map((p) => base.toLatLng(p));
  if (ll.length >= 3 && !S.drawing) {
    L.polygon(ll, { color: "#30d158", weight: 2, fillOpacity: 0.18, interactive: false }).addTo(S.layer);
  } else if (ll.length) {
    L.polyline(ll, { color: "#ffd60a", weight: 2, dashArray: "6 4", interactive: false }).addTo(S.layer);
  }
  S.points.forEach((p, i) => vertex(p, i).addTo(S.layer));
  const n = S.saved.length;
  $("region-state").textContent = S.drawing ? "Drawing: " + S.points.length + " corner" + (S.points.length === 1 ? "" : "s")
    : n ? "Region set (" + n + " corners)" : "Everywhere";
  $("region-state").className = "pill" + (n && !S.drawing ? " good" : "");
  $("region-draw").classList.toggle("hidden", S.drawing);
  $("region-done").classList.toggle("hidden", !S.drawing);
  $("region-done").disabled = S.points.length < 3;
  $("region-cancel").classList.toggle("hidden", !S.drawing);
  $("region-clear").disabled = S.drawing || !n;
  $("region-map").classList.toggle("drawing", S.drawing);
  if (base.map) base.map.doubleClickZoom[S.drawing ? "disable" : "enable"]();
}

async function save() {
  try {
    const r = await api("/api/remote/" + encodeURIComponent(S.env) + "/recording", { method: "PUT", body: { region: S.points } });
    S.saved = r.recording.region;
    S.points = S.saved.slice();
    toast(S.saved.length ? "Recording region saved" : "Recording region cleared: movement anywhere is recorded");
  } catch (e) {
    toast(e.message, "error");
    S.points = S.saved.slice();
  }
  render();
}

function finish() {
  if (S.points.length < 3) return;
  S.drawing = false;
  save();
}

$("region-draw").onclick = () => { S.drawing = true; S.points = []; render(); };
$("region-done").onclick = finish;
$("region-cancel").onclick = () => { S.drawing = false; S.points = S.saved.slice(); render(); };
$("region-clear").onclick = () => {
  if (!confirm("Clear the recording region? Movement anywhere the cameras see will be recorded again.")) return;
  S.points = [];
  save();
};
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && S.drawing) $("region-cancel").click(); });
