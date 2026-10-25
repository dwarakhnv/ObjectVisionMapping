// 3D view of an environment in world metres (x east, y north, z up).
// Draws the ground, reference points, cameras (body, mast, view frustum and
// ground footprint) and, optionally, tracked objects as oriented boxes.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
const rad = (d) => (d * Math.PI) / 180;

// Same convention as environments/geometry.py: world -> camera axes.
export function cameraAxes(heading, tilt, roll) {
  const h = rad(heading), t = rad(tilt), r = rad(roll || 0);
  const forward = new THREE.Vector3(Math.sin(h) * Math.cos(t), Math.cos(h) * Math.cos(t), -Math.sin(t));
  const right0 = new THREE.Vector3(Math.cos(h), -Math.sin(h), 0);
  const down0 = new THREE.Vector3().crossVectors(forward, right0);
  const right = right0.clone().multiplyScalar(Math.cos(r)).addScaledVector(down0, Math.sin(r));
  const down = right0.clone().multiplyScalar(-Math.sin(r)).addScaledVector(down0, Math.cos(r));
  return { forward, right, down };
}

export class EnvScene {
  constructor(container, options) {
    this.container = container;
    this.options = options || {};
    this.world = null;
    this.selected = null;
    this.layers = { refpoints: true, cameras: true, footprints: true, objects: true, labels: true };
    this.objectMeshes = new Map();

    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    container.appendChild(this.renderer.domElement);
    this.labelBox = document.createElement("div");
    this.labelBox.className = "labels";
    container.appendChild(this.labelBox);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(50, 1, 0.05, 1e6);
    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.screenSpacePanning = false;
    this.controls.maxPolarAngle = Math.PI * 0.495;

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x888899, 2.2));
    const sun = new THREE.DirectionalLight(0xffffff, 1.2);
    sun.position.set(0.4, -0.6, 1);
    this.scene.add(sun);

    this.static = new THREE.Group();
    this.objects = new THREE.Group();
    this.scene.add(this.static, this.objects);
    this.raycaster = new THREE.Raycaster();
    this.pickables = [];
    this.labels = [];

    this._bindPointer();
    this._resize = () => this.resize();
    window.addEventListener("resize", this._resize);
    new ResizeObserver(this._resize).observe(container);
    this.resize();
    this.renderer.setAnimationLoop(() => this._frame());
  }

  resize() {
    const w = this.container.clientWidth || 1, h = this.container.clientHeight || 1;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  // ---------------------------------------------------------------- static content
  setWorld(world, keepView) {
    const first = !this.world;
    this.world = world;
    this._clear(this.static);
    this.pickables = [];
    this.labels = this.labels.filter((l) => l.dynamic);
    this.labelBox.querySelectorAll(".static").forEach((n) => n.remove());

    const g = world.ground || 0;
    const ext = Math.max(world.extent_m || 100, 1);
    const centre = this._centre();
    this.scale = ext;

    // Ground and grid
    const size = ext * 2.2;
    const groundMat = new THREE.MeshBasicMaterial({ color: css("--surface-2", "#fbfbfd"), side: THREE.DoubleSide });
    const ground = new THREE.Mesh(new THREE.PlaneGeometry(size * 3, size * 3), groundMat);
    ground.position.set(centre.x, centre.y, g - 0.002 * ext);
    ground.userData.ground = true;
    this.static.add(ground);
    this.groundMesh = ground;
    const step = niceStep(size / 16);
    const div = Math.round(size / step);
    const grid = new THREE.GridHelper(div * step, div, css("--line", "#d2d2d7"), css("--line-soft", "#e8e8ed"));
    grid.rotation.x = Math.PI / 2;
    grid.position.set(Math.round(centre.x / step) * step, Math.round(centre.y / step) * step, g);
    this.static.add(grid);
    this.gridStep = step;

    // North arrow at the origin
    const arrow = new THREE.ArrowHelper(new THREE.Vector3(0, 1, 0), new THREE.Vector3(0, 0, g + 0.01 * ext), ext * 0.12,
      css("--danger", "#d70015"), ext * 0.03, ext * 0.02);
    this.static.add(arrow);
    this._label("N", new THREE.Vector3(0, ext * 0.14, g), "static", null, { color: css("--danger", "#d70015") });

    // Reference points
    const rpColor = css("--refpoint", "#e3007b");
    const r = clamp(ext * 0.008, 0.08, 50);
    for (const p of world.reference_points || []) {
      const v = new THREE.Vector3(...p.world);
      const sel = this._isSel("point", p.id);
      const m = new THREE.Mesh(new THREE.SphereGeometry(sel ? r * 1.7 : r, 20, 14),
        new THREE.MeshStandardMaterial({ color: rpColor, emissive: sel ? rpColor : 0x000000, emissiveIntensity: 0.4 }));
      m.position.copy(v);
      m.userData = { kind: "point", id: p.id };
      m.visible = this.layers.refpoints;
      m.userData.layer = "refpoints";
      this.static.add(m);
      this.pickables.push(m);
      if (Math.abs(v.z - g) > 1e-3) this._line([v, new THREE.Vector3(v.x, v.y, g)], rpColor, 0.5, "refpoints");
      this._label(p.name || p.id, v, "static", "refpoints");
    }

    // Cameras
    for (const c of world.cameras || []) this._drawCamera(c, ext);

    if (first && !keepView) this.fit();
  }

  _drawCamera(c, ext) {
    const g = this.world.ground || 0;
    const sel = this._isSel("camera", c.id);
    const colour = sel ? css("--accent", "#0071e3") : css("--camera", "#1d1d1f");
    const pos = new THREE.Vector3(...c.world);
    const { forward, right, down } = cameraAxes(c.heading, c.tilt, c.roll);
    const len = clamp(ext * 0.1, 1.5, 80);
    const tx = Math.tan(rad(c.hfov) / 2), ty = Math.tan(rad(c.vfov) / 2);
    const corner = (sx, sy) => pos.clone().addScaledVector(forward, len).addScaledVector(right, sx * tx * len).addScaledVector(down, sy * ty * len);
    const cs = [corner(-1, -1), corner(1, -1), corner(1, 1), corner(-1, 1)];

    // Body: a small box aligned with the camera axes
    const s = clamp(ext * 0.012, 0.15, 12);
    const body = new THREE.Mesh(new THREE.BoxGeometry(s * 1.4, s, s * 0.9),
      new THREE.MeshStandardMaterial({ color: colour }));
    const basis = new THREE.Matrix4().makeBasis(right, forward, down.clone().negate());
    body.quaternion.setFromRotationMatrix(basis);
    body.position.copy(pos);
    body.userData = { kind: "camera", id: c.id, layer: "cameras" };
    this.static.add(body);
    this.pickables.push(body);

    // Mast
    this._line([pos, new THREE.Vector3(pos.x, pos.y, g)], colour, 0.8, "cameras");
    // Frustum
    const pts = [];
    for (const k of cs) pts.push(pos, k);
    for (let i = 0; i < 4; i++) pts.push(cs[i], cs[(i + 1) % 4]);
    this._segments(pts, colour, sel ? 1 : 0.65, "cameras");
    // "Up" tick on the top edge so roll is visible
    const topMid = cs[0].clone().add(cs[1]).multiplyScalar(0.5);
    this._segments([topMid, topMid.clone().addScaledVector(down, -ty * len * 0.25)], colour, 1, "cameras");

    // Ground footprint (what the camera sees on the ground)
    const foot = this._footprint(c, pos, forward, right, down, tx, ty, ext * 1.6, g);
    if (foot.length > 2) {
      const shape = new THREE.Shape(foot.map((p) => new THREE.Vector2(p.x, p.y)));
      const mesh = new THREE.Mesh(new THREE.ShapeGeometry(shape),
        new THREE.MeshBasicMaterial({ color: css("--accent", "#0071e3"), transparent: true, opacity: sel ? 0.16 : 0.08,
          side: THREE.DoubleSide, depthWrite: false }));
      mesh.position.z = g + 0.001 * ext;
      mesh.userData.layer = "footprints";
      mesh.visible = this.layers.footprints;
      this.static.add(mesh);
      const outline = foot.concat([foot[0]]).map((p) => new THREE.Vector3(p.x, p.y, g + 0.0015 * ext));
      this._line(outline, css("--accent", "#0071e3"), sel ? 0.7 : 0.3, "footprints");
    }
    this._label(c.name || c.id, pos, "static", "cameras", { strong: true });
  }

  _footprint(c, pos, forward, right, down, tx, ty, range, g) {
    const out = [];
    const N = 24;
    const border = [];
    for (let i = 0; i < N; i++) border.push([-1 + (2 * i) / N, -1]);
    for (let i = 0; i < N; i++) border.push([1, -1 + (2 * i) / N]);
    for (let i = 0; i < N; i++) border.push([1 - (2 * i) / N, 1]);
    for (let i = 0; i < N; i++) border.push([-1, 1 - (2 * i) / N]);
    for (const [sx, sy] of border) {
      const d = forward.clone().addScaledVector(right, sx * tx).addScaledVector(down, sy * ty).normalize();
      let p;
      if (d.z < -1e-6) {
        const t = (g - pos.z) / d.z;
        if (t > 0 && t < range) p = pos.clone().addScaledVector(d, t);
      }
      if (!p) {
        const flat = new THREE.Vector2(d.x, d.y);
        if (flat.length() < 1e-6) continue;
        flat.normalize().multiplyScalar(range);
        p = new THREE.Vector3(pos.x + flat.x, pos.y + flat.y, g);
      }
      out.push(p);
    }
    // Only show the part in front of the camera: if the camera looks up, there is no ground.
    return out;
  }

  // ---------------------------------------------------------------- dynamic objects
  // objects: [{key, cls, label, world:[x,y,z], heading, dims:{length,width,height}, color, stale}]
  setObjects(objects) {
    const seen = new Set();
    for (const o of objects) {
      seen.add(o.key);
      let entry = this.objectMeshes.get(o.key);
      const color = o.color || css("--" + o.cls, "#8e8e93");
      const d = o.dims || { length: 1, width: 1, height: 1 };
      if (!entry) {
        const trail = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.6 }));
        this.objects.add(trail);
        entry = { mesh: null, trail, history: [], label: this._label(o.label, new THREE.Vector3(), "dynamic", "objects", { dynamic: true }) };
        this.objectMeshes.set(o.key, entry);
      }
      // (Re)build the model when it's new or its size changed noticeably.
      const key = o.cls + ":" + [d.length, d.width, d.height].map((v) => Math.round(v * 10)).join(",");
      if (entry.modelKey !== key) {
        if (entry.mesh) { this.objects.remove(entry.mesh); disposeTree(entry.mesh); }
        entry.mesh = objectModel(o.cls, d, color);
        entry.modelKey = key;
        this.objects.add(entry.mesh);
      }
      entry.mesh.position.set(o.world[0], o.world[1], o.world[2] || 0);
      // Unknown heading: keep the last one (or face north) rather than spinning.
      if (o.heading !== null && o.heading !== undefined) entry.mesh.rotation.set(0, 0, -rad(o.heading));
      entry.mesh.userData.body.color.set(color);
      entry.mesh.userData.body.opacity = o.stale ? 0.35 : 1;
      entry.mesh.userData.body.transparent = !!o.stale;
      entry.mesh.visible = this.layers.objects;
      entry.history.push(new THREE.Vector3(o.world[0], o.world[1], (o.world[2] || 0) + 0.05));
      if (entry.history.length > 60) entry.history.shift();
      entry.trail.geometry.dispose();  // the trail grows, so it needs a new buffer
      entry.trail.geometry = new THREE.BufferGeometry().setFromPoints(entry.history);
      entry.trail.visible = this.layers.objects;
      entry.label.pos.set(o.world[0], o.world[1], (o.world[2] || 0) + d.height);
      entry.label.el.textContent = o.label;
    }
    for (const [key, entry] of this.objectMeshes) {
      if (seen.has(key)) continue;
      this.objects.remove(entry.mesh, entry.trail);
      disposeTree(entry.mesh);
      entry.trail.geometry.dispose();
      entry.label.el.remove();
      this.labels = this.labels.filter((l) => l !== entry.label);
      this.objectMeshes.delete(key);
    }
  }

  // A picture (e.g. map tiles on a canvas) laid on the ground; bounds in metres {east0, north0, east1, north1}.
  setGroundTexture(canvas, bounds) {
    if (this.groundTex) {
      this.scene.remove(this.groundTex);
      this.groundTex.geometry.dispose();
      this.groundTex.material.map.dispose();
      this.groundTex.material.dispose();
      this.groundTex = null;
    }
    if (!canvas) return;
    const tex = new THREE.CanvasTexture(canvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = this.renderer.capabilities.getMaxAnisotropy();
    const w = bounds.east1 - bounds.east0, h = bounds.north1 - bounds.north0;
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ map: tex }));
    const g = (this.world && this.world.ground) || 0;
    // Between the plain ground (below) and the grid (on the ground).
    mesh.position.set((bounds.east0 + bounds.east1) / 2, (bounds.north0 + bounds.north1) / 2, g - 0.001 * (this.scale || 100));
    this.scene.add(mesh);
    this.groundTex = mesh;
  }

  // ---------------------------------------------------------------- view
  setSelection(kind, id) {
    this.selected = kind ? { kind, id } : null;
    if (this.world) this.setWorld(this.world, true);
  }

  setLayer(name, on) {
    this.layers[name] = on;
    this.static.traverse((o) => { if (o.userData && o.userData.layer === name) o.visible = on; });
    if (name === "objects") this.objectMeshes.forEach((e) => { e.mesh.visible = on; e.trail.visible = on; });
    this.labels.forEach((l) => { if (l.layer === name) l.hidden = !on; });
  }

  fit(top) {
    const c = this._centre();
    const ext = this.scale || 100;
    const g = (this.world && this.world.ground) || 0;
    this.controls.target.set(c.x, c.y, g);
    if (top) this.camera.position.set(c.x, c.y - ext * 0.001, g + ext * 1.5);
    else this.camera.position.set(c.x - ext * 0.5, c.y - ext * 0.8, g + ext * 0.6);
    this.camera.near = ext / 2000;
    this.camera.far = ext * 200;
    this.camera.updateProjectionMatrix();
    this.controls.update();
  }

  // Look through one of the environment's cameras.
  viewFrom(cameraId) {
    const c = (this.world.cameras || []).find((x) => x.id === cameraId);
    if (!c) return;
    const { forward } = cameraAxes(c.heading, c.tilt, c.roll);
    const pos = new THREE.Vector3(...c.world);
    this.camera.position.copy(pos);
    this.camera.fov = c.vfov;
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(pos.clone().addScaledVector(forward, Math.max(this.scale * 0.2, 2)));
    this.controls.update();
  }

  resetFov() { this.camera.fov = 50; this.camera.updateProjectionMatrix(); }

  _centre() {
    const pts = [];
    if (this.world) {
      for (const p of this.world.reference_points || []) pts.push(p.world);
      for (const c of this.world.cameras || []) pts.push(c.ground);
    }
    if (!pts.length) return new THREE.Vector3(0, 0, 0);
    const v = new THREE.Vector3();
    for (const p of pts) v.add(new THREE.Vector3(p[0], p[1], 0));
    return v.multiplyScalar(1 / pts.length);
  }

  // ---------------------------------------------------------------- helpers
  _isSel(kind, id) { return this.selected && this.selected.kind === kind && this.selected.id === id; }

  _line(points, color, opacity, layer) {
    const l = new THREE.Line(new THREE.BufferGeometry().setFromPoints(points),
      new THREE.LineBasicMaterial({ color, transparent: opacity < 1, opacity }));
    l.userData.layer = layer;
    if (layer && this.layers[layer] === false) l.visible = false;
    this.static.add(l);
    return l;
  }

  _segments(points, color, opacity, layer) {
    const l = new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(points),
      new THREE.LineBasicMaterial({ color, transparent: opacity < 1, opacity }));
    l.userData.layer = layer;
    if (layer && this.layers[layer] === false) l.visible = false;
    this.static.add(l);
    return l;
  }

  _label(text, pos, cls, layer, opts) {
    opts = opts || {};
    const el = document.createElement("div");
    el.className = "label3d " + cls;
    el.textContent = text;
    if (opts.color) el.style.color = opts.color;
    if (opts.strong) el.style.fontWeight = "600";
    this.labelBox.appendChild(el);
    const entry = { el, pos: pos.clone(), layer, dynamic: !!opts.dynamic, hidden: layer ? this.layers[layer] === false : false };
    this.labels.push(entry);
    return entry;
  }

  _clear(group) {
    for (const child of [...group.children]) {
      group.remove(child);
      child.traverse((o) => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
    }
  }

  _bindPointer() {
    const dom = this.renderer.domElement;
    let down = null;
    dom.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; });
    dom.addEventListener("pointerup", (e) => {
      if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
      const rect = dom.getBoundingClientRect();
      const ndc = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
      this.raycaster.setFromCamera(ndc, this.camera);
      const hits = this.raycaster.intersectObjects(this.pickables.filter((p) => p.visible), false);
      if (hits.length && this.options.onPick) { this.options.onPick(hits[0].object.userData.kind, hits[0].object.userData.id, e); return; }
      if (this.options.onGroundClick && this.world) {
        const plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), -(this.world.ground || 0));
        const p = new THREE.Vector3();
        if (this.raycaster.ray.intersectPlane(plane, p)) this.options.onGroundClick([p.x, p.y, p.z], e);
      }
    });
  }

  _frame() {
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    if (!this.layers.labels) { this.labels.forEach((l) => (l.el.style.display = "none")); return; }
    const w = this.container.clientWidth, h = this.container.clientHeight;
    const v = new THREE.Vector3();
    for (const l of this.labels) {
      v.copy(l.pos).project(this.camera);
      const show = !l.hidden && v.z < 1 && v.z > -1 && Math.abs(v.x) < 1.05 && Math.abs(v.y) < 1.05;
      l.el.style.display = show ? "" : "none";
      if (show) { l.el.style.left = ((v.x + 1) / 2) * w + "px"; l.el.style.top = ((1 - v.y) / 2) * h + "px"; }
    }
  }
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function disposeTree(obj) {
  obj.traverse((o) => { if (o.geometry) o.geometry.dispose(); });
}

// ---------------------------------------------------------------- object models
// Simple low-poly models in metres: x = width (right), y = length (+y is the FRONT), z = up, standing on z = 0.
const DARK = new THREE.MeshStandardMaterial({ color: 0x1c1c1e, roughness: 0.6 });
const GLASS = new THREE.MeshStandardMaterial({ color: 0x24303a, roughness: 0.2, metalness: 0.3 });
const LIGHT = new THREE.MeshStandardMaterial({ color: 0xfff4c2, emissive: 0xfff4c2, emissiveIntensity: 0.6 });
const TAIL = new THREE.MeshStandardMaterial({ color: 0xff3b30, emissive: 0xff3b30, emissiveIntensity: 0.4 });
const CARGO = new THREE.MeshStandardMaterial({ color: 0xe5e5ea, roughness: 0.8 });

function box(group, mat, w, l, h, x, y, z) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, l, h), mat);
  m.position.set(x, y, z + h / 2);  // z is the bottom of the box
  group.add(m);
  return m;
}

function wheels(group, W, L, r, ys) {
  const t = Math.min(0.3, 0.16 * W);
  for (const y of ys) {
    for (const side of [-1, 1]) {
      const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, t, 16), DARK);
      m.rotation.z = Math.PI / 2;  // axle across the vehicle
      m.position.set(side * (W / 2 - t / 2 + 0.02), y, r);
      group.add(m);
    }
  }
}

function lights(group, W, L, z, h) {
  for (const side of [-1, 1]) {
    box(group, LIGHT, 0.2 * W, 0.05, h, side * 0.32 * W, L / 2, z);
    box(group, TAIL, 0.18 * W, 0.05, h, side * 0.34 * W, -L / 2, z);
  }
}

function objectModel(cls, d, color) {
  const g = new THREE.Group();
  const body = new THREE.MeshStandardMaterial({ color, roughness: 0.5, metalness: 0.15 });
  g.userData.body = body;
  const L = d.length, W = d.width, H = d.height;
  if (cls === "car") {
    const r = Math.min(0.34, 0.22 * H), base = 0.55 * r;
    box(g, body, W, L, 0.5 * H - base, 0, 0, base);                       // lower body
    box(g, GLASS, 0.84 * W, 0.48 * L, 0.36 * H, 0, -0.06 * L, 0.5 * H);   // cabin (windows)
    box(g, body, 0.8 * W, 0.4 * L, 0.06 * H, 0, -0.06 * L, 0.86 * H);     // roof
    wheels(g, W, L, r, [0.32 * L, -0.32 * L]);
    lights(g, W, L, 0.3 * H, 0.08 * H);
  } else if (cls === "bus") {
    const r = 0.5, base = 0.3;
    box(g, body, W, L, H - base, 0, 0, base);
    box(g, GLASS, W * 1.01, L * 0.9, 0.3 * H, 0, -0.02 * L, 0.5 * H);     // side window band
    box(g, GLASS, W * 0.9, 0.04, 0.4 * H, 0, L / 2 + 0.01, 0.42 * H);     // windscreen
    wheels(g, W, L, r, [0.3 * L, -0.3 * L]);
    lights(g, W, L, 0.18 * H, 0.06 * H);
  } else if (cls === "truck") {
    const r = Math.min(0.5, 0.15 * H), base = 0.6 * r, cab = Math.min(2.4, 0.3 * L);
    box(g, body, W, cab, 0.72 * H - base, 0, L / 2 - cab / 2, base);                // cab
    box(g, GLASS, W * 0.9, 0.04, 0.22 * H, 0, L / 2 + 0.01, 0.42 * H);             // windscreen
    box(g, CARGO, W, L - cab - 0.15, H - base, 0, -cab / 2 - 0.075, base);         // cargo box / bed
    wheels(g, W, L, r, [L / 2 - cab / 2, -0.15 * L, -0.38 * L]);
    lights(g, W, L, 0.25 * H, 0.06 * H);
  } else if (cls === "person") {
    const leg = 0.47 * H, torso = 0.33 * H, head = 0.065 * H;
    const legs = new THREE.Mesh(new THREE.CylinderGeometry(0.22 * W, 0.18 * W, leg, 12), DARK);
    legs.rotation.x = Math.PI / 2;
    legs.position.set(0, 0, leg / 2);
    const chest = new THREE.Mesh(new THREE.CylinderGeometry(0.34 * W, 0.26 * W, torso, 12), body);
    chest.rotation.x = Math.PI / 2;
    chest.position.set(0, 0, leg + torso / 2);
    const ball = new THREE.Mesh(new THREE.SphereGeometry(head, 14, 10), body);
    ball.position.set(0, 0.02, H - head);
    const nose = new THREE.Mesh(new THREE.SphereGeometry(head * 0.35, 8, 6), DARK);  // shows which way they face
    nose.position.set(0, head * 0.95, H - head);
    g.add(legs, chest, ball, nose);
  } else {
    box(g, body, W, L, H, 0, 0, 0);
  }
  return g;
}
function niceStep(x) {
  const p = Math.pow(10, Math.floor(Math.log10(x)));
  const m = x / p;
  return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * p;
}
