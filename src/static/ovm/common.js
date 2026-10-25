// Shared helpers: JSON fetch with CSRF, toasts, formatting, element builder.
(function () {
  function csrf() {
    const el = document.querySelector("input[name=csrfmiddlewaretoken]");
    return el ? el.value : "";
  }
  async function api(url, options) {
    options = options || {};
    const init = { method: options.method || "GET", headers: { "X-CSRFToken": csrf() } };
    if (options.body instanceof FormData) init.body = options.body;
    else if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    const res = await fetch(url, init);
    let data = null;
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) throw new Error((data && data.error) || res.status + " " + res.statusText);
    return data;
  }
  function toast(message, kind) {
    let box = document.querySelector(".toasts");
    if (!box) { box = document.createElement("div"); box.className = "toasts"; document.body.appendChild(box); }
    const t = document.createElement("div");
    t.className = "toast " + (kind || "");
    t.textContent = message;
    box.appendChild(t);
    setTimeout(() => t.classList.add("gone"), 3200);
    setTimeout(() => t.remove(), 3800);
  }
  function fmt(n, d) {
    return n === null || n === undefined || isNaN(n) ? "–" : Number(n).toFixed(d === undefined ? 1 : d);
  }
  function el(tag, attrs, children) {
    const e = document.createElement(tag);
    for (const k in attrs || {}) {
      const v = attrs[k];
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (k === "value") e.value = v;
      else if (k === "checked") e.checked = !!v;
      else e.setAttribute(k, v === true ? "" : v);
    }
    for (const c of [].concat(children === undefined ? [] : children)) if (c !== null && c !== undefined) e.append(c);
    return e;
  }
  // Colours per object class, read from CSS so light/dark mode both work.
  function classColor(key) {
    const v = getComputedStyle(document.documentElement).getPropertyValue("--" + key).trim();
    return v || "#8e8e93";
  }
  window.OVM = { api, toast, fmt, el, csrf, classColor };
})();
