// Saved recordings: list clips for an environment, play them (with the detections drawn on top), delete them.
(function () {
  const { api, toast, el, fmt } = OVM;
  const $ = (id) => document.getElementById(id);
  if (!$("env-select")) return;
  const classes = JSON.parse($("object-classes").textContent);
  const labelOf = (k) => (classes.find((c) => c.key === k) || { label: k }).label;
  const video = $("video"), canvas = $("boxes");
  const S = { env: null, list: [], current: null, meta: null };

  const base = () => "/api/recordings/" + encodeURIComponent(S.env) + "/";

  async function load(env) {
    S.env = env;
    $("env-select").value = env;
    $("settings-link").href = "/remote/?env=" + encodeURIComponent(env);
    history.replaceState(null, "", "?env=" + encodeURIComponent(env));
    const r = await api(base().slice(0, -1));
    S.list = r.recordings;
    const mb = r.total_bytes / 1e6;
    $("summary").textContent = r.settings.enabled
      ? "Video saved around movement while detection runs. " + S.list.length + " clip" + (S.list.length === 1 ? "" : "s") + ", " + fmt(mb, mb < 10 ? 1 : 0) + " MB."
      : "Recording is off for this environment (turn it on under Recording settings).";
    renderList();
    if (!S.list.some((x) => x.id === (S.current && S.current.id))) show(S.list[0] || null);
  }

  function seen(rec) {
    const parts = Object.entries(rec.seen || {}).map(([k, n]) => n + " " + labelOf(k).toLowerCase() + (n === 1 ? "" : "s"));
    return parts.length ? parts.join(", ") : "nothing tracked";
  }

  function renderList() {
    $("count").textContent = S.list.length;
    const box = $("list");
    if (!S.list.length) { box.replaceChildren(el("p", { class: "small muted", text: "No recordings yet. They appear here when something moves while detection runs." })); return; }
    const items = [];
    let day = null;
    for (const rec of S.list) {
      const d = new Date(rec.start);
      const dayText = d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
      if (dayText !== day) { day = dayText; items.push(el("div", { class: "rec-day", text: dayText })); }
      items.push(el("div", { class: "rec-item" + (S.current && S.current.id === rec.id ? " sel" : ""), onclick: () => show(rec) }, [
        rec.thumbnail ? el("img", { src: base() + rec.id + ".jpg", alt: "", loading: "lazy" }) : el("img", { alt: "" }),
        el("div", { class: "small" }, [
          el("strong", { text: d.toLocaleTimeString() }),
          el("div", { class: "muted", text: rec.camera.replace(/_/g, " ") + " · " + fmt(rec.duration_s, 0) + " s" + (rec.continued ? " (continued)" : "") }),
          el("div", { class: "muted", text: seen(rec) }),
        ]),
      ]));
    }
    box.replaceChildren(...items);
  }

  async function show(rec) {
    S.current = rec;
    S.meta = null;
    renderList();
    const has = !!rec;
    $("player-empty").classList.toggle("hidden", has);
    video.classList.toggle("hidden", !has);
    canvas.classList.toggle("hidden", !has);
    $("delete").classList.toggle("hidden", !has);
    $("download").classList.toggle("hidden", !has);
    if (!has) { video.removeAttribute("src"); $("rec-title").textContent = ""; $("rec-info").textContent = ""; return; }
    video.src = base() + rec.id + ".webm";
    $("download").href = base() + rec.id + ".webm";
    $("rec-title").textContent = new Date(rec.start).toLocaleString();
    $("rec-info").textContent = " · " + rec.camera.replace(/_/g, " ") + " · " + fmt(rec.duration_s, 0) + " s · " + seen(rec);
    S.meta = await api(base() + rec.id);
    drawBoxes();
  }

  // Detections stored with the clip, drawn at the matching moment of the video.
  function drawBoxes() {
    const r = canvas.getBoundingClientRect(), dpr = devicePixelRatio || 1;
    canvas.width = r.width * dpr;
    canvas.height = r.height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, r.width, r.height);
    const m = S.meta;
    if (!m || !$("show-boxes").checked || !m.timeline || !m.timeline.length || !m.size) return;
    const t = video.currentTime;
    let snap = null;
    for (const s of m.timeline) { if (s.t <= t + 0.25) snap = s; else break; }
    if (!snap || t - snap.t > 1.5) return;
    const [w, h] = m.size, k = Math.min(r.width / w, r.height / h), ox = (r.width - w * k) / 2, oy = (r.height - h * k) / 2;
    ctx.font = "600 12px -apple-system, Segoe UI, sans-serif";
    for (const o of snap.objects) {
      const color = OVM.classColor(o.class);
      const [x1, y1, x2, y2] = o.box;
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.strokeRect(ox + x1 * k, oy + y1 * k, (x2 - x1) * k, (y2 - y1) * k);
      const text = "#" + o.id + " " + o.label;
      ctx.fillStyle = color;
      ctx.fillRect(ox + x1 * k, oy + y1 * k - 18, ctx.measureText(text).width + 10, 18);
      ctx.fillStyle = "#fff";
      ctx.fillText(text, ox + x1 * k + 5, oy + y1 * k - 5);
    }
  }
  video.addEventListener("timeupdate", drawBoxes);
  video.addEventListener("seeked", drawBoxes);
  $("show-boxes").onchange = drawBoxes;
  new ResizeObserver(drawBoxes).observe(canvas);
  (function tick() { if (!video.paused) drawBoxes(); requestAnimationFrame(tick); })();  // smoother than timeupdate alone

  $("delete").onclick = async () => {
    const rec = S.current;
    if (!rec || !confirm("Delete the recording from " + new Date(rec.start).toLocaleString() + "? This can't be undone.")) return;
    try {
      video.removeAttribute("src");
      video.load();
      await api(base() + rec.id, { method: "DELETE" });
      toast("Recording deleted");
      S.current = null;
      await load(S.env);
    } catch (e) { toast(e.message, "error"); }
  };

  $("env-select").onchange = (e) => { S.current = null; load(e.target.value); };
  const q = new URLSearchParams(location.search).get("env");
  const opts = [...$("env-select").options].map((o) => o.value);
  load(opts.includes(q) ? q : opts[0]).catch((e) => toast(e.message, "error"));
  setInterval(() => { if (video.paused) load(S.env).catch(() => {}); }, 15000);  // new clips appear by themselves
})();
