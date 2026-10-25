// Draws a floor plan (see simulation/plans.py) onto a canvas. One drawing routine is shared by
// the plan editor, the Environments / Detect maps and the 3D view's ground.
// Plan coordinates: units, x right, y down, origin top-left.

export const UNITS = { m: 1, cm: 0.01, mm: 0.001, ft: 0.3048, in: 0.0254, yd: 0.9144 };
export const UNIT_NAMES = { m: "metres", cm: "centimetres", mm: "millimetres", ft: "feet", in: "inches", yd: "yards" };

const COLORS = {
  paper: "#ffffff", wall: "#2c2c2e", room: ["#e3f2fd", "#e8f5e9", "#fff3e0", "#f3e5f5", "#e0f7fa", "#fce4ec"],
  roomText: "#3a3a3c", box: "#d1d1d6", boxEdge: "#8e8e93", window: "#5ac8fa", door: "#636366", label: "#1c1c1e",
};

const images = new Map();
export function planImageUrl(file) { return "/api/plans/images/" + encodeURIComponent(file); }
export function loadImage(url) {
  if (!images.has(url)) {
    images.set(url, new Promise((resolve) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => resolve(null);
      img.src = url;
    }));
  }
  return images.get(url);
}

// t = {scale: pixels per unit, ox, oy: canvas position of the plan's (0, 0)}
export function toCanvas(t, x, y) { return [t.ox + x * t.scale, t.oy + y * t.scale]; }

// Draw everything. bg = the loaded background image (or null). opts.selected = index of a shape to highlight.
export function drawPlan(ctx, plan, t, bg, opts) {
  opts = opts || {};
  const S = t.scale;
  const P = (p) => toCanvas(t, p[0], p[1]);
  ctx.save();
  // Paper
  ctx.fillStyle = COLORS.paper;
  ctx.fillRect(t.ox, t.oy, plan.width * S, plan.height * S);
  // Background image
  if (bg && plan.background) {
    const b = plan.background;
    ctx.globalAlpha = b.opacity ?? 1;
    ctx.drawImage(bg, t.ox + b.x * S, t.oy + b.y * S, b.width * S, b.height * S);
    ctx.globalAlpha = 1;
  }
  const shapes = plan.shapes || [];
  const wallWidth = (sh) => Math.max(2, (sh.thickness || 0) * S);
  const defaultWall = Math.max(2, Math.max(...shapes.filter((s) => s.type === "wall").map((s) => s.thickness || 0), 0.15 / UNITS[plan.units]) * S);
  // Rooms (under everything)
  let roomN = 0;
  for (const sh of shapes) {
    if (sh.type !== "room") continue;
    path(ctx, sh.points.map(P), true);
    ctx.fillStyle = COLORS.room[roomN++ % COLORS.room.length];
    ctx.fill();
  }
  // Boxes (furniture, shelving)
  for (const sh of shapes) {
    if (sh.type !== "box") continue;
    const [a, b] = sh.points.map(P);
    ctx.fillStyle = COLORS.box;
    ctx.strokeStyle = COLORS.boxEdge;
    ctx.lineWidth = 1.5;
    ctx.fillRect(Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.abs(b[0] - a[0]), Math.abs(b[1] - a[1]));
    ctx.strokeRect(Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.abs(b[0] - a[0]), Math.abs(b[1] - a[1]));
  }
  // Walls
  ctx.strokeStyle = COLORS.wall;
  ctx.lineJoin = "miter";
  ctx.lineCap = "square";
  for (const sh of shapes) {
    if (sh.type !== "wall") continue;
    ctx.lineWidth = wallWidth(sh);
    path(ctx, sh.points.map(P), false);
    ctx.stroke();
  }
  // Openings: cut the wall, then draw the window glass or the door swing
  for (const sh of shapes) {
    if (sh.type !== "window" && sh.type !== "door") continue;
    const [a, b] = sh.points.map(P);
    ctx.lineCap = "butt";
    ctx.strokeStyle = COLORS.paper;
    ctx.lineWidth = defaultWall + 2;
    line(ctx, a, b);
    if (sh.type === "window") {
      ctx.strokeStyle = COLORS.window;
      ctx.lineWidth = Math.max(1.5, defaultWall * 0.35);
      line(ctx, a, b);
    } else {
      const r = Math.hypot(b[0] - a[0], b[1] - a[1]);
      const ang = Math.atan2(b[1] - a[1], b[0] - a[0]);
      const leaf = [a[0] + r * Math.cos(ang - Math.PI / 2), a[1] + r * Math.sin(ang - Math.PI / 2)];
      ctx.strokeStyle = COLORS.door;
      ctx.lineWidth = 1.5;
      line(ctx, a, leaf);
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.arc(a[0], a[1], r, ang - Math.PI / 2, ang);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }
  // Names and labels
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (const sh of shapes) {
    let text = null, at = null, size = 13;
    if ((sh.type === "room" || sh.type === "box") && sh.name) { text = sh.name; at = centroid(sh.points.map(P)); size = sh.type === "room" ? 14 : 11; }
    if (sh.type === "label" && sh.text) { text = sh.text; at = P(sh.points[0]); size = 14; }
    if (!text) continue;
    ctx.font = "600 " + size + "px -apple-system, Segoe UI, sans-serif";
    ctx.fillStyle = sh.type === "label" ? COLORS.label : COLORS.roomText;
    ctx.fillText(text, at[0], at[1]);
  }
  // Selection highlight
  if (opts.selected != null && shapes[opts.selected]) {
    const sh = shapes[opts.selected];
    const pts = sh.points.map(P);
    ctx.strokeStyle = "#0a84ff";
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    if (sh.type === "box") {
      const [a, b] = pts;
      ctx.strokeRect(Math.min(a[0], b[0]) - 3, Math.min(a[1], b[1]) - 3, Math.abs(b[0] - a[0]) + 6, Math.abs(b[1] - a[1]) + 6);
    } else if (pts.length > 1) {
      path(ctx, pts, sh.type === "room");
      ctx.stroke();
    }
    ctx.setLineDash([]);
    for (const p of pts) {  // vertex handles
      ctx.fillStyle = "#fff";
      ctx.strokeStyle = "#0a84ff";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(p[0], p[1], 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
  }
  // Outline of the plan area
  ctx.strokeStyle = "rgba(0,0,0,.25)";
  ctx.lineWidth = 1;
  ctx.strokeRect(t.ox, t.oy, plan.width * S, plan.height * S);
  ctx.restore();
}

// The whole plan as a picture, at most maxPx pixels on its long side (for maps and the 3D ground).
export async function renderPlan(plan, maxPx) {
  maxPx = maxPx || 3000;
  const scale = maxPx / Math.max(plan.width, plan.height);
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(plan.width * scale));
  canvas.height = Math.max(1, Math.round(plan.height * scale));
  const bg = plan.background ? await loadImage(planImageUrl(plan.background.file)) : null;
  drawPlan(canvas.getContext("2d"), plan, { scale, ox: 0, oy: 0 }, bg);
  return { canvas, scale };
}

export async function fetchPlan(slug) {
  const r = await window.OVM.api("/api/plans/" + encodeURIComponent(slug));
  return r.plan;
}

function path(ctx, pts, close) {
  ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
  if (close) ctx.closePath();
}
function line(ctx, a, b) { ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); }
function centroid(pts) {
  const n = pts.length;
  return [pts.reduce((s, p) => s + p[0], 0) / n, pts.reduce((s, p) => s + p[1], 0) / n];
}
