// Remote access page: access keys per environment, recording settings, and how to connect.
(function () {
  const { api, toast, el, fmt } = OVM;
  const $ = (id) => document.getElementById(id);
  if (!$("env-select")) return;
  const classes = JSON.parse($("object-classes").textContent);
  const S = { env: null, data: null, token: null, example: "poll" };

  function checkboxes(box, selected, onchange) {
    box.replaceChildren(...classes.map((c) => {
      const cb = el("input", { type: "checkbox", value: c.key, checked: selected.includes(c.key) });
      if (onchange) cb.onchange = onchange;
      return el("label", { class: "toggle" }, [cb, el("i", { class: "dot", style: "background:var(--" + c.key + ", var(--other))" }), c.label]);
    }));
  }
  const ticked = (box) => [...box.querySelectorAll("input:checked")].map((i) => i.value);
  const label = (keys) => (keys.length ? keys.map((k) => (classes.find((c) => c.key === k) || { label: k }).label).join(", ") : "All");

  function ago(t) {
    if (!t) return "Never used";
    const s = Date.now() / 1000 - t;
    return s < 60 ? "Used just now" : s < 3600 ? "Used " + Math.round(s / 60) + " min ago" : "Used " + new Date(t * 1000).toLocaleString();
  }

  async function load(env) {
    S.env = env;
    $("env-select").value = env;
    history.replaceState(null, "", "?env=" + encodeURIComponent(env));
    try { localStorage.setItem("ovm.remoteEnv", env); } catch (e) { /* private mode */ }
    S.data = await api("/api/remote/" + encodeURIComponent(env));
    render();
  }

  function render() {
    const d = S.data;
    $("idle").textContent = Math.round(d.idle_seconds);
    $("rec-link").href = "/recordings/?env=" + encodeURIComponent(S.env);
    $("subs").querySelector("tbody").replaceChildren(...(d.subscriptions.length ? d.subscriptions.map((s) => {
      const on = el("input", { type: "checkbox", checked: s.enabled });
      on.onchange = async () => { await api("/api/remote/subscriptions/" + s.id, { method: "PATCH", body: { enabled: on.checked } }); refresh(); };
      return el("tr", {}, [
        el("td", {}, [el("strong", { text: s.name }), el("div", { class: "small muted", text: "Created " + new Date(s.created * 1000).toLocaleDateString() })]),
        el("td", { class: "small", text: label(s.classes) }),
        el("td", {}, el("code", { text: "ovm_…" + s.hint })),
        el("td", { class: "small" }, s.active
          ? el("span", { class: "row", style: "gap:5px;flex-wrap:nowrap" }, [el("i", { class: "live-dot" }), s.streams ? "Connected (" + s.streams + " stream" + (s.streams > 1 ? "s" : "") + ")" : "Polling now"])
          : ago(s.last_used)),
        el("td", {}, on),
        el("td", {}, el("button", { class: "small danger", text: "Revoke", onclick: async () => {
          if (!confirm("Revoke the key for " + s.name + "? Apps using it stop working immediately.")) return;
          await api("/api/remote/subscriptions/" + s.id, { method: "DELETE" });
          refresh();
        } })),
      ]);
    }) : [el("tr", {}, el("td", { colspan: "6", class: "muted small", text: "No keys yet. Create one below." }))]));
    // Recording settings
    $("rec-enabled").checked = d.recording.enabled;
    checkboxes($("rec-classes"), d.recording.classes, saveRecording);
    // Addresses
    const base = $("base-url"), prev = base.value;
    base.replaceChildren(...d.base_urls.map((u) => el("option", { value: u, text: u })));
    if (d.base_urls.includes(prev)) base.value = prev;
    renderExample();
  }

  async function refresh() {
    S.data = await api("/api/remote/" + encodeURIComponent(S.env));
    render();
  }

  async function saveRecording() {
    try {
      await api("/api/remote/" + encodeURIComponent(S.env) + "/recording", { method: "PUT",
        body: { enabled: $("rec-enabled").checked, classes: ticked($("rec-classes")) } });
      toast("Recording settings saved");
    } catch (e) { toast(e.message, "error"); }
  }
  $("rec-enabled").onchange = saveRecording;

  // New key
  checkboxes($("new-classes"), []);
  $("new-sub").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/remote/" + encodeURIComponent(S.env) + "/subscriptions", { method: "POST",
        body: { name: e.target.elements.name.value, classes: ticked($("new-classes")) } });
      S.token = r.token;
      $("token-value").textContent = r.token;
      $("new-token").classList.remove("hidden");
      $("test-output").classList.add("hidden");
      e.target.reset();
      checkboxes($("new-classes"), []);
      await refresh();
    } catch (err) { toast(err.message, "error"); }
  };
  $("copy-token").onclick = async () => {
    try { await navigator.clipboard.writeText(S.token); toast("Key copied"); } catch (e) { toast("Select the key and copy it", "error"); }
  };
  $("test-token").onclick = async () => {
    const out = $("test-output");
    out.classList.remove("hidden");
    out.textContent = "Asking for objects… (the first call starts detection)";
    const url = "/api/v1/environments/" + encodeURIComponent(S.env) + "/objects";
    for (let i = 0; i < 6; i++) {  // the cameras may need a few seconds to start
      const r = await fetch(url, { headers: { Authorization: "Bearer " + S.token } });
      const body = await r.json();
      out.textContent = "HTTP " + r.status + "\n" + JSON.stringify(body, null, 2);
      if (!r.ok || Object.values(body.cameras || {}).some((c) => c.status === "running")) break;
      await new Promise((res) => setTimeout(res, 3000));
    }
    refresh();
  };

  // Examples
  document.querySelectorAll("#ex-tabs button").forEach((b) => (b.onclick = () => {
    S.example = b.dataset.ex;
    document.querySelectorAll("#ex-tabs button").forEach((x) => x.classList.toggle("on", x === b));
    renderExample();
  }));
  $("base-url").onchange = renderExample;

  function renderExample() {
    const base = $("base-url").value || location.origin, env = S.env, key = S.token || "YOUR_KEY";
    const objects = base + "/api/v1/environments/" + env + "/objects", stream = base + "/api/v1/environments/" + env + "/stream";
    const ex = {
      poll: 'curl -H "Authorization: Bearer ' + key + '" \\\n     "' + objects + '"\n\n# only people:\ncurl -H "Authorization: Bearer ' + key + '" \\\n     "' + objects + '?classes=person"',
      python: "import time\nimport requests\n\nURL = \"" + objects + "\"\nHEADERS = {\"Authorization\": \"Bearer " + key + "\"}\n\nwhile True:  # ask at least every " + Math.round(S.data.idle_seconds) + " s to keep detection running\n    data = requests.get(URL, headers=HEADERS, timeout=10).json()\n    for obj in data[\"objects\"]:\n        print(obj[\"id\"], obj[\"label\"], obj[\"position\"], obj[\"heading\"])\n    time.sleep(1)",
      stream: "import json\nimport requests\n\nURL = \"" + stream + "\"\nHEADERS = {\"Authorization\": \"Bearer " + key + "\"}\n\n# One long connection; an event arrives the moment the objects change.\n# Add ?hz=20 to the URL for 20 updates a second, with moving objects predicted ahead.\nwith requests.get(URL, headers=HEADERS, stream=True, timeout=60) as r:\n    for line in r.iter_lines(decode_unicode=True):\n        if line and line.startswith(\"data: \"):\n            data = json.loads(line[6:])\n            print(len(data[\"objects\"]), \"objects\", [o[\"position\"] for o in data[\"objects\"]])",
      js: "// In a browser or Node 18+. EventSource can't send headers, so the key goes in the URL.\nconst source = new EventSource(\"" + stream + "?token=" + key + "\");\nsource.addEventListener(\"objects\", (e) => {\n  const data = JSON.parse(e.data);\n  for (const obj of data.objects) console.log(obj.id, obj.label, obj.position);\n});\n\n// Or poll:\nconst r = await fetch(\"" + objects + "\", { headers: { Authorization: \"Bearer " + key + "\" } });\nconsole.log((await r.json()).objects);",
    };
    $("example").textContent = ex[S.example];
    const outdoor = !S.data || S.data.environment.map.kind !== "plan";
    const tracking = !!(S.data && S.data.environment.tracking);  // without it: ids per answer, no speed or motion heading
    $("sample").textContent = JSON.stringify({
      environment: env, name: S.data ? S.data.environment.name : "", time: "2026-10-23T14:05:12.345+00:00",
      map: outdoor ? { kind: "geo" } : S.data.environment.map, tracking,
      cameras: { tapo_c100: { name: "Tapo C100", status: "running", error: null, located: true } },
      objects: [{
        id: tracking ? 3 : 1, class: "person", label: "Adult", subtype: "adult", confidence: 0.86,
        position: outdoor ? { lat: 35.6858123, lon: -78.8780456 } : { x: 12.4, y: 6.1, units: S.data.environment.map.units },
        heading: tracking ? 92 : null, heading_source: tracking ? "motion" : null, speed_mps: tracking ? 1.3 : null,
        moving: tracking, direction: tracking ? "crossing_right" : "unknown",
        size_m: { length: 0.5, width: 0.5, height: 1.74 }, cameras: ["tapo_c100"], tracked_for_s: tracking ? 8.4 : null,
      }],
    }, null, 2);
  }

  $("env-select").onchange = (e) => { S.token = null; $("new-token").classList.add("hidden"); load(e.target.value); };
  const q = new URLSearchParams(location.search).get("env");
  let remembered = null;
  try { remembered = localStorage.getItem("ovm.remoteEnv"); } catch (e) { /* private mode */ }
  const opts = [...$("env-select").options].map((o) => o.value);
  load(opts.includes(q) ? q : opts.includes(remembered) ? remembered : opts[0]).catch((e) => toast(e.message, "error"));
  setInterval(() => refresh().catch(() => {}), 5000);  // keep "Connected / Used ... ago" current
})();
