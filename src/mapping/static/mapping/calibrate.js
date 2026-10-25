// Calibration: match reference points to pixels, then solve the camera pose.
(function () {
  const { api, toast, el, fmt } = OVM;
  const slug = window.OVM_ENV_SLUG;
  const canvas = document.getElementById("calib");
  if (!canvas) return;
  const loupe = document.getElementById("loupe");
  const S = {
    env: null, camId: null, cam: null, overlay: null, img: null, mode: null,
    points: [], active: null, drag: null, show: { grid: true, reproj: true }, residuals: {},
  };

  // ---------------------------------------------------------------- loading
  async function init() {
    const r = await api("/api/environments/" + slug);
    S.env = r.environment;
    const sel = document.getElementById("camera-select");
    sel.replaceChildren(...S.env.cameras.map((c) => el("option", { value: c.id, text: c.name + " (" + c.id + ")" })));
    const want = new URLSearchParams(location.search).get("camera");
    sel.value = want && S.env.cameras.some((c) => c.id === want) ? want : S.env.cameras[0].id;
    sel.onchange = () => selectCamera(sel.value);
    await selectCamera(sel.value);
  }

  async function selectCamera(id) {
    S.camId = id;
    history.replaceState(null, "", "?camera=" + encodeURIComponent(id));
    const r = await api("/api/mapping/" + slug + "/cameras/" + encodeURIComponent(id));
    applyCamera(r);
    S.points = r.camera.calibration.points.map((p) => ({ ...p }));
    S.residuals = residualMap(r.camera.calibration);
    S.active = nextUnplaced();
    document.getElementById("cam-title").textContent = r.camera.name;
    document.getElementById("result").replaceChildren();
    const hasSource = r.camera.source.kind !== "none";
    const hasUpload = await fetch(imageUrl("upload"), { method: "HEAD" }).then((x) => x.ok).catch(() => false);
    document.querySelector('#image-mode [data-mode="live"]').disabled = !hasSource;
    await loadImage(hasUpload ? "upload" : hasSource ? "live" : null);
    renderPoints();
  }

  function applyCamera(r) {
    S.cam = r.camera;
    S.overlay = r.overlay;
    renderCamTable();
    draw();
  }

  function residualMap(cal) {
    const out = {};
    const res = (cal.solved && cal.solved.residuals_px) || [];
    cal.points.forEach((p, i) => { if (res[i] !== undefined) out[p.point_id] = res[i]; });
    return out;
  }

  function imageUrl(mode) {
    if (mode === "live") return "/api/feeds/" + encodeURIComponent(slug + "." + S.camId) + "/snapshot.jpg?t=" + Date.now();
    return "/api/mapping/" + slug + "/cameras/" + encodeURIComponent(S.camId) + "/image?t=" + Date.now();
  }

  async function loadImage(mode) {
    S.mode = mode;
    document.querySelectorAll("#image-mode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
    const empty = document.getElementById("stage-empty");
    if (!mode) {
      S.img = null;
      canvas.classList.add("hidden");
      empty.classList.remove("hidden");
      empty.textContent = "This camera has no source yet. Upload a still image from it to calibrate.";
      return;
    }
    empty.classList.remove("hidden");
    empty.textContent = mode === "live" ? "Grabbing a frame from the camera… (the first one can take a few seconds)" : "Loading image…";
    const img = new Image();
    img.src = imageUrl(mode);
    try { await img.decode(); } catch (e) {
      empty.textContent = mode === "live" ? "Could not get a frame from this camera's source." : "No uploaded image yet.";
      canvas.classList.add("hidden");
      return;
    }
    S.img = img;
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    canvas.classList.remove("hidden");
    empty.classList.add("hidden");
    document.getElementById("image-info").textContent = "Image " + img.naturalWidth + "×" + img.naturalHeight +
      " · camera configured for " + S.cam.resolution[0] + "×" + S.cam.resolution[1] +
      (Math.abs(img.naturalWidth / img.naturalHeight - S.cam.resolution[0] / S.cam.resolution[1]) > 0.01 ? " (different shape: solving will adopt this image's resolution)" : "");
    draw();
  }

  document.querySelectorAll("#image-mode button").forEach((b) => (b.onclick = () => loadImage(b.dataset.mode)));
  document.getElementById("refresh").onclick = () => loadImage(S.mode);
  document.querySelectorAll("#show button").forEach((b) => (b.onclick = () => { b.classList.toggle("on"); S.show[b.dataset.k] = b.classList.contains("on"); draw(); }));
  document.getElementById("upload").onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    try {
      await api("/api/mapping/" + slug + "/cameras/" + encodeURIComponent(S.camId) + "/image", { method: "POST", body: fd });
      await loadImage("upload");
    } catch (err) { toast(err.message, "error"); }
    e.target.value = "";
  };

  // ---------------------------------------------------------------- drawing
  function draw() {
    if (!S.img) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height, s = Math.max(W, H) / 1000;
    ctx.drawImage(S.img, 0, 0, W, H);
    const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#0071e3";
    const solved = S.cam && S.cam.calibration.solved && S.cam.calibration.solved.rmse_px !== undefined;
    if (S.show.grid && S.overlay && solved) {
      ctx.strokeStyle = "rgba(255,255,255,.55)";
      ctx.lineWidth = 1.2 * s;
      for (const line of S.overlay.grid) {
        ctx.beginPath();
        line.forEach(([u, v], i) => (i ? ctx.lineTo(u * W, v * H) : ctx.moveTo(u * W, v * H)));
        ctx.stroke();
      }
    }
    if (S.show.reproj && S.overlay && solved) {
      for (const p of S.overlay.points) {
        if (!p.visible) continue;
        const x = p.u * W, y = p.v * H;
        const placed = S.points.find((q) => q.point_id === p.point_id);
        if (placed) {
          ctx.strokeStyle = "#ffd60a";
          ctx.lineWidth = 2 * s;
          ctx.beginPath(); ctx.moveTo(placed.u * W, placed.v * H); ctx.lineTo(x, y); ctx.stroke();
        }
        ctx.strokeStyle = accent;
        ctx.lineWidth = 2.5 * s;
        ctx.beginPath(); ctx.arc(x, y, 9 * s, 0, Math.PI * 2); ctx.stroke();
        if (!placed) label(ctx, p.point_id, x, y, s, "rgba(0,113,227,.85)");
      }
    }
    for (const p of S.points) {
      const x = p.u * W, y = p.v * H, active = p.point_id === S.active;
      ctx.fillStyle = active ? "#ffd60a" : "#ff2d92";
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 2 * s;
      ctx.beginPath(); ctx.arc(x, y, (active ? 7 : 5.5) * s, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      label(ctx, p.point_id, x, y, s, active ? "rgba(178,140,0,.9)" : "rgba(227,0,123,.9)");
    }
  }

  function label(ctx, text, x, y, s, bg) {
    const fs = Math.max(11, 13 * s);
    ctx.font = "600 " + fs + "px -apple-system, Segoe UI, sans-serif";
    const w = ctx.measureText(text).width + 10 * s;
    ctx.fillStyle = bg;
    ctx.fillRect(x + 9 * s, y - fs - 6 * s, w, fs + 6 * s);
    ctx.fillStyle = "#fff";
    ctx.textBaseline = "top";
    ctx.fillText(text, x + 14 * s, y - fs - 3 * s);
  }

  // ---------------------------------------------------------------- interaction
  function toImage(e) {
    const r = canvas.getBoundingClientRect();
    return [((e.clientX - r.left) / r.width) * canvas.width, ((e.clientY - r.top) / r.height) * canvas.height];
  }
  function nearest(x, y) {
    const s = Math.max(canvas.width, canvas.height) / 1000;
    let best = null, bd = 14 * s * (canvas.width / canvas.getBoundingClientRect().width);
    for (const p of S.points) {
      const d = Math.hypot(p.u * canvas.width - x, p.v * canvas.height - y);
      if (d < bd) { best = p; bd = d; }
    }
    return best;
  }
  canvas.addEventListener("pointerdown", (e) => {
    if (!S.img) return;
    const [x, y] = toImage(e);
    const hit = nearest(x, y);
    if (hit) { S.drag = hit; S.active = hit.point_id; canvas.setPointerCapture(e.pointerId); renderPoints(); draw(); return; }
    if (!S.active) { toast("Select a reference point in the list first"); return; }
    let p = S.points.find((q) => q.point_id === S.active);
    if (!p) { p = { point_id: S.active, u: 0, v: 0 }; S.points.push(p); }
    p.u = x / canvas.width; p.v = y / canvas.height;
    S.active = nextUnplaced() || S.active;
    pointsChanged();
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!S.img) return;
    const [x, y] = toImage(e);
    if (S.drag) { S.drag.u = x / canvas.width; S.drag.v = y / canvas.height; draw(); }
    showLoupe(e, x, y);
  });
  canvas.addEventListener("pointerup", () => { if (S.drag) { S.drag = null; pointsChanged(); } });
  canvas.addEventListener("pointerleave", () => loupe.classList.add("hidden"));

  function showLoupe(e, x, y) {
    const stage = document.getElementById("stage").getBoundingClientRect();
    // 4x magnification of what's on screen: 45 screen px around the cursor, in image pixels.
    const size = 180, src = (size / 4) * (canvas.width / canvas.getBoundingClientRect().width);
    const ctx = loupe.getContext("2d");
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, size, size);
    ctx.drawImage(canvas, x - src / 2, y - src / 2, src, src, 0, 0, size, size);
    ctx.strokeStyle = "#ffd60a"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(size / 2, 0); ctx.lineTo(size / 2, size); ctx.moveTo(0, size / 2); ctx.lineTo(size, size / 2); ctx.stroke();
    let lx = e.clientX - stage.left + 24, ly = e.clientY - stage.top - size - 24;
    if (ly < 0) ly = e.clientY - stage.top + 24;
    if (lx + size > stage.width) lx = e.clientX - stage.left - size - 24;
    loupe.style.left = lx + "px"; loupe.style.top = ly + "px";
    loupe.classList.remove("hidden");
  }

  function nextUnplaced() {
    const placed = new Set(S.points.map((p) => p.point_id));
    const p = S.env.reference_points.find((r) => !placed.has(r.id));
    return p ? p.id : null;
  }

  let saveTimer = null;
  function pointsChanged() {
    renderPoints();
    draw();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try {
        const r = await api("/api/mapping/" + slug + "/cameras/" + encodeURIComponent(S.camId) + "/points",
          { method: "PUT", body: { points: S.points, resolution: [canvas.width, canvas.height] } });
        applyCamera(r);
      } catch (e) { toast(e.message, "error"); }
    }, 400);
  }

  function renderPoints() {
    const tbody = document.querySelector("#points tbody");
    tbody.replaceChildren(...S.env.reference_points.map((rp) => {
      const p = S.points.find((q) => q.point_id === rp.id);
      const res = S.residuals[rp.id];
      return el("tr", { class: "clickable" + (rp.id === S.active ? " sel" : ""), onclick: () => { S.active = rp.id; renderPoints(); draw(); } }, [
        el("td", {}, [el("strong", { text: rp.id }), rp.name !== rp.id ? el("div", { class: "small muted", text: rp.name }) : null]),
        el("td", { class: "small", text: p ? Math.round(p.u * canvas.width) + ", " + Math.round(p.v * canvas.height) : rp.id === S.active ? "Click the image…" : "–" }),
        el("td", { class: "num small", text: res !== undefined ? fmt(res, 1) + " px" : "" }),
        el("td", {}, p ? el("button", { class: "small", text: "Remove", onclick: (ev) => { ev.stopPropagation(); S.points = S.points.filter((q) => q !== p); S.active = rp.id; pointsChanged(); } }) : null),
      ]);
    }));
    document.getElementById("solve").disabled = S.points.length < 4;
    document.getElementById("estimate-fov").disabled = S.points.length < 6;
  }

  function renderCamTable() {
    const c = S.cam;
    const cs = S.env.coordinates;
    const u = cs.kind === "geo" ? "m" : cs.units;
    const pos = cs.kind === "geo" ? fmt(c.position.lat, 6) + ", " + fmt(c.position.lon, 6) : fmt(c.position.x, 2) + ", " + fmt(c.position.y, 2) + " " + u;
    const solved = c.calibration.solved || {};
    const rows = [
      ["Status", solved.rmse_px !== undefined ? "Calibrated (" + fmt(solved.rmse_px, 1) + " px error)" : "Not calibrated"],
      ["Position", pos], ["Mount height", fmt(c.height, 2) + " " + u], ["Heading", fmt(c.heading, 1) + "°"],
      ["Tilt down", fmt(c.tilt, 1) + "°"], ["Roll", fmt(c.roll, 1) + "°"], ["Field of view", fmt(c.hfov, 1) + "°"],
      ["Resolution", c.resolution[0] + "×" + c.resolution[1]],
    ];
    document.querySelector("#cam-table tbody").replaceChildren(...rows.map(([k, v]) => el("tr", {}, [el("td", { class: "muted", text: k }), el("td", { text: v })])));
  }

  document.getElementById("solve").onclick = async () => {
    try {
      const r = await api("/api/mapping/" + slug + "/cameras/" + encodeURIComponent(S.camId) + "/solve", {
        method: "POST", body: { points: S.points, resolution: [canvas.width, canvas.height], estimate_fov: document.getElementById("estimate-fov").checked } });
      S.env = r.environment.environment;
      applyCamera(r);
      S.residuals = residualMap(r.camera.calibration);
      renderPoints();
      const rep = r.report;
      const quality = rep.rmse_per_1000px < 3 ? ["good", "Good fit"] : rep.rmse_per_1000px < 10 ? ["warn", "Rough fit"] : ["bad", "Poor fit: check the points"];
      document.getElementById("result").replaceChildren(...[
        el("span", { class: "pill " + quality[0], text: quality[1] + " · " + fmt(rep.rmse_px, 1) + " px average error" }),
        r.note ? el("p", { class: "small muted", text: r.note }) : null,
        el("p", { class: "small muted", text: "The grid shows the ground as the solved camera sees it; circles show where each reference point should appear. Lines join each click to its prediction." }),
      ].filter(Boolean));
      toast("Camera pose solved and saved");
    } catch (e) { toast(e.message, "error"); }
  };
  document.getElementById("reset").onclick = async () => {
    const r = await api("/api/mapping/" + slug + "/cameras/" + encodeURIComponent(S.camId) + "/reset", { method: "POST" });
    applyCamera(r);
    S.residuals = {};
    renderPoints();
    document.getElementById("result").replaceChildren();
  };

  init().catch((e) => toast(e.message, "error"));
})();
