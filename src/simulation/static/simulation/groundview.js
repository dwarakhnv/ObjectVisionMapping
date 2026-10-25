// 3D / top view of an environment with a picture on the ground: map tiles outdoors, the floor plan indoors.
// Used by the Environments page (cameras + reference points) and the Detect page (+ live objects).
import { EnvScene } from "ovm/scene3d.js";
import { geoToLocal, groundPicture } from "/static/simulation/maptiles.js";
import { fetchPlan, renderPlan } from "/static/simulation/planrender.js";

export class GroundView {
  constructor(el) {
    this.scene = new EnvScene(el, {});
    this.analysis = null;
    this.ground = "satellite";   // "none" | "satellite" | "map" (outdoors); anything but "none" shows the plan indoors
    this._groundKey = null;
  }

  get isPlan() { return !!(this.analysis && this.analysis.space && this.analysis.space.kind === "plan"); }

  // analysis = what /api/envs/<slug> returns: {world, origin, space, plan, cameras}
  async setAnalysis(analysis) {
    this.analysis = analysis;
    const ready = analysis && analysis.world && analysis.world.cameras.length;
    if (!ready) { this.scene.setGroundTexture(null); this._groundKey = null; return false; }
    this.scene.setWorld(analysis.world, !!this.scene.world);
    await this._updateGround();
    return true;
  }

  async setGround(style) {
    this.ground = style;
    this._groundKey = null;
    await this._updateGround();
  }

  // Re-make the ground picture only when the place, size or style changes.
  async _updateGround() {
    const a = this.analysis;
    if (!a || !a.world) return;
    const key = this.isPlan
      ? [this.ground === "none" ? "none" : "plan", a.space.plan].join("|")
      : [this.ground, a.origin.lat.toFixed(5), a.origin.lon.toFixed(5), Math.round(a.world.extent_m)].join("|");
    if (key === this._groundKey) return;
    this._groundKey = key;
    if (this.ground === "none") { this.scene.setGroundTexture(null); return; }
    let pic;
    if (this.isPlan) {
      const plan = await fetchPlan(a.space.plan), m = a.space.meters_per_unit;
      const { canvas } = await renderPlan(plan, 3000);
      pic = { canvas, bounds: { east0: 0, east1: plan.width * m, north0: -plan.height * m, north1: 0 } };
    } else {
      pic = await groundPicture(this.ground, a.origin, a.world.extent_m * 1.2);
    }
    if (key === this._groundKey) this.scene.setGroundTexture(pic.canvas, pic.bounds);
  }

  // Metres in the 3D view for a map position ({lat, lon} outdoors, {x, y} on a plan).
  toWorld(p) {
    if (this.isPlan) {
      const m = this.analysis.space.meters_per_unit;
      return [p.x * m, -p.y * m];
    }
    return geoToLocal(p.lat, p.lon, this.analysis.origin);
  }

  // objects: Detect results [{id, class, label, geo: {lat, lon} | plan: {x, y}, heading, dimensions}]
  setObjects(objects) {
    if (!this.analysis || !this.analysis.world) return;
    const where = (o) => (this.isPlan ? o.plan : o.geo);
    this.scene.setObjects(objects.filter(where).map((o) => {
      const [e, n] = this.toWorld(where(o));
      return { key: o.id, cls: o.class, label: "#" + o.id + " " + o.label, world: [e, n, 0], heading: o.heading, dims: o.dimensions };
    }));
  }

  fit(top) { this.scene.resetFov(); this.scene.fit(top); }
}
