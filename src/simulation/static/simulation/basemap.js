// The map an environment is drawn on: an outdoor map (satellite / street tiles, lat/lon)
// or a floor plan (x / y in plan units). Both pages use positions as plain objects:
//   outdoor {lat, lon}      floor plan {x, y}
// and BaseMap converts them to and from Leaflet. A plan uses Leaflet's flat CRS with
// lat = -y and lng = x, so "up" on the plan is up on screen (and north in 3D).
import { PROVIDERS, scrollZoomOnClick } from "/static/simulation/maptiles.js";
import { fetchPlan, renderPlan } from "/static/simulation/planrender.js";

export class BaseMap {
  constructor(el, options) {
    this.el = el;
    this.options = options || {};
    this.map = null;
    this.space = null;         // {kind: "geo"} or {kind: "plan", plan, units, meters_per_unit}
    this.plan = null;          // the plan JSON (floor plans)
    this.style = "satellite";  // outdoor tiles: "satellite" | "map"
    this.tiles = {};
  }

  get isPlan() { return !!this.space && this.space.kind === "plan"; }

  // Rebuild the Leaflet map when the kind of map (or the plan) changes.
  async setSpace(space) {
    const key = (s) => (s ? s.kind + ":" + (s.plan || "") : "");
    if (this.map && key(space) === key(this.space)) return false;
    if (this.map) { this.map.remove(); this.map = null; }
    this.space = space;
    this.plan = null;
    if (space.kind === "plan") {
      this.plan = await fetchPlan(space.plan);
      this.map = L.map(this.el, { crs: L.CRS.Simple, minZoom: -6, maxZoom: 8, zoomSnap: 0.25, attributionControl: false });
      const { canvas } = await renderPlan(this.plan, 3000);
      const bounds = [[-this.plan.height, 0], [0, this.plan.width]];
      L.imageOverlay(canvas.toDataURL("image/png"), bounds).addTo(this.map);
      this.map.fitBounds(bounds, { padding: [20, 20] });
    } else {
      this.map = L.map(this.el, { maxZoom: 22 }).setView([0, 0], 2);
      for (const [k, p] of Object.entries(PROVIDERS)) {
        this.tiles[k] = L.tileLayer(p.url, { attribution: p.attribution, maxNativeZoom: p.maxNativeZoom, maxZoom: 22 });
      }
      this.setStyle(this.style);
    }
    scrollZoomOnClick(this.map);
    if (this.options.onClick) this.map.on("click", (e) => this.options.onClick(this.fromLatLng(e.latlng), e));
    if (this.options.onReady) this.options.onReady(this.map);
    return true;
  }

  // Outdoor tiles: "satellite", "map" or "none". (A floor plan always shows the plan.)
  setStyle(style) {
    this.style = style;
    if (this.isPlan || !this.map) return;
    Object.values(this.tiles).forEach((t) => this.map.removeLayer(t));
    if (this.tiles[style]) this.tiles[style].addTo(this.map);
  }

  toLatLng(p) { return this.isPlan ? L.latLng(-p.y, p.x) : L.latLng(p.lat, p.lon); }
  fromLatLng(ll) { return this.isPlan ? { x: ll.lng, y: -ll.lat } : { lat: ll.lat, lon: ll.lng }; }
  has(p) { return !!p && (this.isPlan ? p.x != null : p.lat != null); }

  // A point `distance` metres from p in compass direction `heading` (for camera view lines).
  offset(p, heading, distance) {
    const h = (heading * Math.PI) / 180;
    if (this.isPlan) {
      const d = distance / this.space.meters_per_unit;
      return { x: p.x + d * Math.sin(h), y: p.y - d * Math.cos(h) };
    }
    const dLat = (distance * Math.cos(h)) / 111195;
    return { lat: p.lat + dLat, lon: p.lon + (distance * Math.sin(h)) / (111195 * Math.cos((p.lat * Math.PI) / 180)) };
  }

  fit(points) {
    const ll = points.filter((p) => this.has(p)).map((p) => this.toLatLng(p));
    if (ll.length) this.map.fitBounds(L.latLngBounds(ll), { padding: [40, 40], maxZoom: this.isPlan ? 6 : 20 });
    else if (this.isPlan) this.map.fitBounds([[-this.plan.height, 0], [0, this.plan.width]], { padding: [20, 20] });
  }

  // "12.3, 4.5 ft" or "35.6858060, -78.8780686"
  describe(p, digits) {
    if (!this.has(p)) return "–";
    const f = (v, d) => Number(v).toFixed(d);
    return this.isPlan ? f(p.x, digits ?? 2) + ", " + f(p.y, digits ?? 2) + " " + this.space.units : f(p.lat, 7) + ", " + f(p.lon, 7);
  }
}
