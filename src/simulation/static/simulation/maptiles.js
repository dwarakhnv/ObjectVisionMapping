// Map tile helpers shared by the Leaflet map and the 3D view's ground picture.

export const PROVIDERS = {
  satellite: {
    label: "Satellite",
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics",
    maxNativeZoom: 19,
  },
  map: {
    label: "Map",
    url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution: "&copy; OpenStreetMap contributors",
    maxNativeZoom: 19,
  },
};

const R = 6371008.8;

// Same flat-earth conversion as simulation/environment.py (geo_to_local / local_to_geo).
export function geoToLocal(lat, lon, origin) {
  return [((lon - origin.lon) * Math.PI / 180) * R * Math.cos(origin.lat * Math.PI / 180),
          ((lat - origin.lat) * Math.PI / 180) * R];
}

const lonToX = (lon, z) => ((lon + 180) / 360) * 2 ** z;
const latToY = (lat, z) => {
  const r = (lat * Math.PI) / 180;
  return ((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * 2 ** z;
};
const xToLon = (x, z) => (x / 2 ** z) * 360 - 180;
const yToLat = (y, z) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / 2 ** z))) * 180) / Math.PI;

// Fetch the tiles covering `halfSize` metres around `origin` into one canvas.
// Returns {canvas, bounds: {east0, north0, east1, north1}} in metres from origin.
export async function groundPicture(provider, origin, halfSize) {
  const p = PROVIDERS[provider];
  const cosLat = Math.cos((origin.lat * Math.PI) / 180);
  // Highest zoom where the area still fits in about 2048 pixels.
  const z = Math.max(1, Math.min(p.maxNativeZoom, Math.floor(Math.log2((156543.034 * cosLat * 2048) / (2 * halfSize)))));
  const dLat = (halfSize / R) * (180 / Math.PI), dLon = dLat / cosLat;
  const x0 = Math.floor(lonToX(origin.lon - dLon, z)), x1 = Math.floor(lonToX(origin.lon + dLon, z));
  const y0 = Math.floor(latToY(origin.lat + dLat, z)), y1 = Math.floor(latToY(origin.lat - dLat, z));
  const canvas = document.createElement("canvas");
  canvas.width = (x1 - x0 + 1) * 256;
  canvas.height = (y1 - y0 + 1) * 256;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#888";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const jobs = [];
  for (let x = x0; x <= x1; x++) {
    for (let y = y0; y <= y1; y++) {
      const img = new Image();
      img.crossOrigin = "anonymous";  // needed to use the picture in WebGL
      img.src = p.url.replace("{z}", z).replace("{x}", x).replace("{y}", y);
      jobs.push(img.decode().then(() => ctx.drawImage(img, (x - x0) * 256, (y - y0) * 256)).catch(() => {}));
    }
  }
  await Promise.all(jobs);
  const [east0, north1] = geoToLocal(yToLat(y0, z), xToLon(x0, z), origin);
  const [east1, north0] = geoToLocal(yToLat(y1 + 1, z), xToLon(x1 + 1, z), origin);
  return { canvas, bounds: { east0, north0, east1, north1 } };
}

// Scrolling the page over a map shouldn't zoom it: wheel-zoom only after the map is clicked.
export function scrollZoomOnClick(map) {
  map.scrollWheelZoom.disable();
  map.on("click", () => map.scrollWheelZoom.enable());
  map.on("mouseout", () => map.scrollWheelZoom.disable());
}
