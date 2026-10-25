// Draws a frame and its analysed objects onto a canvas. Shared by the Live
// page and the Phase 3 live map's camera tiles.
(function () {
  const ARROWS = { approaching: "↓", receding: "↑", crossing_left: "←", crossing_right: "→", stationary: "•", unknown: "" };
  const WORDS = { approaching: "Approaching", receding: "Moving away", crossing_left: "Crossing ←", crossing_right: "Crossing →", stationary: "Stationary", unknown: "–" };

  function hexToRgba(hex, a) {
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex.trim());
    if (!m) return hex;
    return "rgba(" + parseInt(m[1], 16) + "," + parseInt(m[2], 16) + "," + parseInt(m[3], 16) + "," + a + ")";
  }

  // opts: {show: {masks, boxes, labels, trails}, hidden: Set(class keys), selected: id, trails: Map(id -> [[x,y]...]), compact}
  // img: the frame to draw under the objects, or null for a transparent overlay on top of a live video.
  function draw(canvas, img, result, opts) {
    opts = opts || {};
    const show = opts.show || { masks: true, boxes: true, labels: true, trails: true };
    const hidden = opts.hidden || new Set();
    const w = result.width || img.naturalWidth, h = result.height || img.naturalHeight;
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    const ctx = canvas.getContext("2d");
    if (img) ctx.drawImage(img, 0, 0, w, h);
    else ctx.clearRect(0, 0, w, h);
    const s = Math.max(w, h) / 1000;  // scale strokes/fonts with resolution
    ctx.lineJoin = "round";
    for (const o of result.objects || []) {
      if (hidden.has(o.class)) continue;
      const color = OVM.classColor(o.class);
      const sel = opts.selected === o.id;
      const [x1, y1, x2, y2] = o.box;
      if (show.masks && o.polygon && o.polygon.length > 2) {
        ctx.beginPath();
        o.polygon.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.closePath();
        ctx.fillStyle = hexToRgba(color, sel ? 0.45 : 0.3);
        ctx.fill();
      }
      if (show.boxes) {
        ctx.strokeStyle = color;
        ctx.lineWidth = (sel ? 4 : 2) * s;
        ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
      }
      if (show.trails && opts.trails && opts.trails.has(o.id)) {
        const pts = opts.trails.get(o.id);
        if (pts.length > 1) {
          ctx.beginPath();
          pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
          ctx.strokeStyle = hexToRgba(color, 0.85);
          ctx.lineWidth = 2 * s;
          ctx.stroke();
        }
      }
      if (show.labels) {
        const parts = ["#" + o.id, o.label];
        if (!opts.compact && o.distance_m !== null && o.distance_m !== undefined) parts.push(Math.round(o.distance_m) + " m");
        const arrow = ARROWS[o.direction] || "";
        const text = parts.join(" · ") + (arrow ? " " + arrow : "");
        const fs = Math.max(11, 14 * s);
        ctx.font = "600 " + fs + "px -apple-system, Segoe UI, sans-serif";
        const tw = ctx.measureText(text).width;
        const ty = y1 - fs - 8 * s < 0 ? y2 + 4 * s : y1 - fs - 8 * s;
        ctx.fillStyle = color;
        roundRect(ctx, x1, ty, tw + 12 * s, fs + 8 * s, 5 * s);
        ctx.fill();
        ctx.fillStyle = "#fff";
        ctx.textBaseline = "top";
        ctx.fillText(text, x1 + 6 * s, ty + 4 * s);
      }
    }
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  // Keep a short trail of bottom-centre points per object id.
  function updateTrails(trails, result, max) {
    const seen = new Set();
    for (const o of result.objects || []) {
      seen.add(o.id);
      const p = [(o.box[0] + o.box[2]) / 2, o.box[3]];
      const t = trails.get(o.id) || [];
      t.push(p);
      if (t.length > (max || 40)) t.shift();
      trails.set(o.id, t);
    }
    for (const id of [...trails.keys()]) if (!seen.has(id)) trails.delete(id);
  }

  function hit(result, x, y, hidden) {
    let best = null;
    for (const o of result.objects || []) {
      if (hidden && hidden.has(o.class)) continue;
      const [x1, y1, x2, y2] = o.box;
      if (x >= x1 && x <= x2 && y >= y1 && y <= y2) {
        const area = (x2 - x1) * (y2 - y1);
        if (!best || area < best.area) best = { id: o.id, area };
      }
    }
    return best ? best.id : null;
  }

  // Poll a feed and call onFrame(img, result) for each new analysed frame.
  // wantFrames(): false while the page shows the camera's live video instead (img is then null).
  function follow(feedId, onFrame, onStatus, wantFrames) {
    let stopped = false, after = 0;
    (async function loop() {
      while (!stopped) {
        let r;
        try {
          r = await OVM.api("/api/feeds/" + encodeURIComponent(feedId) + "/latest?after=" + after);
        } catch (e) {
          if (onStatus) onStatus({ status: "error", error: e.message });
          await new Promise((res) => setTimeout(res, 2000));
          continue;
        }
        if (stopped) break;
        if (onStatus) onStatus(r);
        if (r.status === "error" || r.status === "stopped") { await new Promise((res) => setTimeout(res, 2000)); continue; }
        if (r.frame_id > after) {
          after = r.frame_id;
          if (wantFrames && !wantFrames()) { onFrame(null, r); continue; }
          const img = new Image();
          img.src = "/api/feeds/" + encodeURIComponent(feedId) + "/frame/" + r.frame_id + ".jpg";
          try { await img.decode(); } catch (e) { continue; }
          if (!stopped) onFrame(img, r);
        }
      }
    })();
    return () => { stopped = true; };
  }

  window.OVMOverlay = { draw, updateTrails, hit, follow, ARROWS, WORDS };
})();
