// static/js/app.js

// --- Leaflet initialization ---
const map = L.map('map', { tap: true });
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>'
}).addTo(map);

// Default center (St. Louis for example)
map.setView([38.6270, -90.1994], 13);

let originMarker = null;
let destMarker = null;
let routeLayer = null;

// --- Helpers ---

// Try to parse input like "38.64,-90.29" into {lat, lon}
function tryParseLatLon(text) {
  if (!text) return null;
  const m = text.trim().match(/^([+-]?\d+(\.\d+)?)[,\s]+([+-]?\d+(\.\d+)?)$/);
  if (!m) return null;
  const lat = parseFloat(m[1]), lon = parseFloat(m[3]);
  if (Number.isNaN(lat) || Number.isNaN(lon)) return null;
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
  return { lat, lon };
}

// Call Flask /geocode
async function geocode(q) {
  const res = await fetch(`${GEOCODE_URL}?q=${encodeURIComponent(q)}`);
  if (!res.ok) throw new Error("Geocode failed");
  return await res.json(); // [{display_name, lat, lon}, ...]
}

// Add/update a marker
function setMarker(lat, lon, title, existing) {
  const ll = [lat, lon];
  if (!existing) existing = L.marker(ll, { title }).addTo(map);
  else existing.setLatLng(ll);
  existing.bindPopup(title);
  return existing;
}

// Fit map to current markers
function fitToMarkers() {
  const pts = [];
  if (originMarker) pts.push(originMarker.getLatLng());
  if (destMarker) pts.push(destMarker.getLatLng());
  if (!pts.length) return;
  if (pts.length === 1) map.setView(pts[0], 16);
  else map.fitBounds(L.latLngBounds(pts), { padding: [40, 40] });
}

// Call Flask /route
async function fetchRoute(orig, dest, mode) {
  const qs = new URLSearchParams({
    orig_lat: orig.lat, orig_lon: orig.lon,
    dest_lat: dest.lat, dest_lon: dest.lon,
    mode
  }).toString();
  const res = await fetch(`/route?${qs}`);
  if (!res.ok) throw new Error("Route failed");
  return await res.json(); // { geojson, total_weight, mode, snapped_origin, snapped_destination, snap_dist_m }
}

// --- Form handling ---
const form        = document.getElementById('od-form');
const originInput = document.getElementById('origin-input');
const destInput   = document.getElementById('dest-input');

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const originQ = originInput.value.trim();
  const destQ   = destInput.value.trim();
  const modeSel = document.getElementById('mode-select');
  const mode    = modeSel ? modeSel.value : 'walk';

  if (!originQ || !destQ) return alert("Please enter both origin and destination.");

  try {
    // 1) Try lat/lon first
    let o = tryParseLatLon(originQ);
    let d = tryParseLatLon(destQ);

    // 2) Geocode fallback
    if (!o) {
      const r = await geocode(originQ);
      if (!r.length) return alert("Origin not found.");
      o = { lat: r[0].lat, lon: r[0].lon, display: r[0].display_name };
    } else {
      o.display = `${o.lat.toFixed(6)}, ${o.lon.toFixed(6)} (raw)`;
    }

    if (!d) {
      const r = await geocode(destQ);
      if (!r.length) return alert("Destination not found.");
      d = { lat: r[0].lat, lon: r[0].lon, display: r[0].display_name };
    } else {
      d.display = `${d.lat.toFixed(6)}, ${d.lon.toFixed(6)} (raw)`;
    }

    // 3) Place markers at RAW inputs
    originMarker = setMarker(o.lat, o.lon, "Origin: " + o.display, originMarker);
    destMarker   = setMarker(d.lat, d.lon, "Destination: " + d.display, destMarker);
    fitToMarkers();

    // 4) Ask server for route (snapped to network)
    const data = await fetchRoute({ lat: o.lat, lon: o.lon }, { lat: d.lat, lon: d.lon }, mode);

    // 5) Draw route polyline
    if (routeLayer) routeLayer.remove();
    routeLayer = L.geoJSON(data.geojson, { style: { color: "#1d4ed8", weight: 5 } }).addTo(map);
    map.fitBounds(routeLayer.getBounds(), { padding: [40, 40] });

    // 6) Optional dotted connectors from raw → snapped if offset is big
    const DOT = 15; // meters
    const connStyle = { color: "#6b7280", weight: 2, dashArray: "4,6" };
    if (data.snap_dist_m?.origin > DOT) {
      L.polyline([[o.lat, o.lon], [data.snapped_origin.lat, data.snapped_origin.lon]], connStyle).addTo(map);
    }
    if (data.snap_dist_m?.destination > DOT) {
      L.polyline([[d.lat, d.lon], [data.snapped_destination.lat, data.snapped_destination.lon]], connStyle).addTo(map);
    }

    console.log("Mode:", data.mode, "Total weight:", data.total_weight);
  } catch (err) {
    console.error(err);
    alert(err.message || "Search/route failed.");
  }
});
