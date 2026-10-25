// Cameras page: list detected cameras, stream the chosen one, show its metadata.
(function () {
  const { api, toast, el, fmt } = OVM;
  const img = document.getElementById("live");
  const state = { cameras: [], selected: null, metaTimer: null };
  const KIND = { usb: "USB", "built-in": "Built-in", virtual: "Virtual", network: "Network", device: "Device" };

  function remembered() { try { return localStorage.getItem("ovm.camera"); } catch (e) { return null; } }
  function remember(id) { try { localStorage.setItem("ovm.camera", id); } catch (e) { /* private mode */ } }

  async function loadCameras(rescan) {
    const r = await api("/api/cameras" + (rescan ? "?rescan=1" : ""));
    state.cameras = r.cameras;
    renderList();
    const want = new URLSearchParams(location.search).get("camera") || remembered();
    if (!state.selected && want && r.cameras.some((c) => c.id === want)) select(want);
  }

  function renderList() {
    const box = document.getElementById("camera-list");
    if (!state.cameras.length) {
      box.replaceChildren(el("p", { class: "small muted", text: "No cameras found. Plug one in and press Rescan, or add a network camera." }));
      return;
    }
    box.replaceChildren(...state.cameras.map((c) => el("button", { class: "cam-item" + (c.id === state.selected ? " sel" : ""), onclick: () => select(c.id) }, [
      el("div", { style: "font-weight:600", text: c.name }),
      el("div", { class: "sub" }, [
        el("span", { class: "pill", text: KIND[c.kind] || c.kind }),
        c.vendor_id ? el("span", { text: c.vendor_id + ":" + c.product_id }) : null,
        c.streaming ? el("span", { class: "row", style: "gap:4px" }, [el("i", { class: "live-dot" }), "Live"]) : null,
      ]),
    ])));
  }

  function select(id) {
    state.selected = id;
    remember(id);
    history.replaceState(null, "", "?camera=" + encodeURIComponent(id));
    const cam = state.cameras.find((c) => c.id === id);
    renderList();
    document.getElementById("feed-empty").classList.remove("hidden");
    document.getElementById("feed-empty").textContent = "Connecting to " + (cam ? cam.name : "camera") + "…";
    img.classList.add("hidden");
    img.onload = () => { img.classList.remove("hidden"); document.getElementById("feed-empty").classList.add("hidden"); };
    img.onerror = () => { loadMeta(true); };
    img.removeAttribute("src");  // close the previous stream before opening the next
    // A new URL each time so the browser opens a fresh stream.
    img.src = "/api/cameras/" + encodeURIComponent(id) + "/stream.mjpg?t=" + Date.now();
    document.getElementById("snapshot").href = "/api/cameras/" + encodeURIComponent(id) + "/snapshot.jpg";
    document.getElementById("controls").style.visibility = "visible";
    document.getElementById("meta-title").textContent = cam ? cam.name : "Details";
    document.getElementById("meta").replaceChildren(el("p", { class: "small muted", text: "Reading camera details…" }));
    clearInterval(state.metaTimer);
    setTimeout(() => loadMeta(), 1200);
    state.metaTimer = setInterval(() => loadMeta(), 3000);
  }

  async function loadMeta(afterError) {
    const id = state.selected;
    if (!id) return;
    let m;
    try { m = await api("/api/cameras/" + encodeURIComponent(id)); } catch (e) { return; }
    if (id !== state.selected) return;
    if (m.error) {
      const empty = document.getElementById("feed-empty");
      empty.classList.remove("hidden");
      empty.textContent = m.error;
      img.classList.add("hidden");
    } else if (afterError || !m.streaming) {
      // The stream dropped (server restart, camera hiccup): reconnect.
      if (!state.reconnecting) {
        state.reconnecting = true;
        setTimeout(() => { state.reconnecting = false; if (state.selected === id) select(id); }, 1500);
      }
      return;
    }
    renderMeta(m);
  }

  function renderMeta(m) {
    const i = m.info, f = m.format || {};
    const status = document.getElementById("feed-status");
    if (f.width) {
      status.textContent = f.width + "×" + f.height + (f.fps_measured ? " · " + fmt(f.fps_measured, 1) + " fps" : "");
      status.classList.remove("hidden");
    }
    const sections = [
      ["Camera", [
        ["Name", i.name], ["Type", KIND[i.kind] || i.kind],
        i.vendor_id ? ["USB vendor : product", i.vendor_id + " : " + i.product_id] : null,
        i.kind === "network" ? ["URL", i.url] : ["Device index", String(i.index)],
        ["Driver", f.backend || i.backend],
        i.path ? ["Device path", i.path] : null,
        ["ID", i.id],
      ]],
      ["Picture", f.width ? [
        ["Resolution", f.width + " × " + f.height + " (" + f.aspect + ")"],
        ["Frame rate", f.fps_measured ? fmt(f.fps_measured, 1) + " fps measured" + (f.fps_reported > 0 ? ", " + fmt(f.fps_reported, 0) + " reported" : "") : (f.fps_reported > 0 ? fmt(f.fps_reported, 0) + " fps reported" : "–")],
        ["Format", f.fourcc || "–"],
        ["Viewers", String(m.viewers)],
      ] : [["Status", m.error || "Not available"]]],
      ["Controls", Object.entries(m.controls || {}).map(([k, v]) => [k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()), String(v)])],
    ];
    const box = document.getElementById("meta");
    box.replaceChildren(...sections.filter(([, rows]) => rows.length).flatMap(([title, rows]) => [
      el("h3", { text: title }),
      el("table", { class: "list" }, el("tbody", {}, rows.filter(Boolean).map(([k, v]) => el("tr", {}, [el("td", { text: k }), el("td", { text: v })])))),
    ]));
    // Resolution picker
    const sel = document.getElementById("resolution");
    const options = (m.resolutions || []).slice();
    const current = f.width ? f.width + "x" + f.height : "";
    if (current && !options.includes(current)) options.push(current);
    const sig = options.join(",") + "|" + current;
    if (sel.dataset.sig !== sig) {
      sel.dataset.sig = sig;
      sel.replaceChildren(...options.map((r) => el("option", { value: r, text: r.replace("x", " × ") })));
      sel.value = current;
      sel.disabled = options.length < 2;
    }
  }

  document.getElementById("resolution").onchange = async (e) => {
    const [width, height] = e.target.value.split("x").map(Number);
    try {
      const r = await api("/api/cameras/" + encodeURIComponent(state.selected) + "/resolution", { method: "POST", body: { width, height } });
      toast("Camera is now sending " + r.width + " × " + r.height);
      loadMeta();
    } catch (err) { toast(err.message, "error"); }
  };

  document.getElementById("rescan").onclick = () => loadCameras(true).then(() => toast("Found " + state.cameras.length + " camera" + (state.cameras.length === 1 ? "" : "s")));

  // Close the stream when leaving the page, so the camera can stop when nobody watches.
  window.addEventListener("pagehide", () => { img.onerror = null; img.removeAttribute("src"); clearInterval(state.metaTimer); });
  window.addEventListener("pageshow", (e) => { if (e.persisted && state.selected) select(state.selected); });

  // Refresh the list now and then so plugged / unplugged cameras show up.
  setInterval(() => loadCameras(false).catch(() => {}), 10000);
  loadCameras(true).catch((e) => toast(e.message, "error"));
})();
