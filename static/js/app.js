// Leaflet init
const map = L.map('map', { tap: true });
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>'
}).addTo(map);
map.setView([38.6270, -90.1994], 13);

let originMarker = null;
let destMarker = null;
let routeLayer = null;

async function geocode(q) {
  const res = await fetch(`${GEOCODE_URL}?q=${encodeURIComponent(q)}`);
  if (!res.ok) throw new Error("Geocode failed");
  return await res.json();
}

function setMarker(lat, lon, title, existing) {
  const ll = [lat, lon];
  if (!existing) {
    existing = L.marker(ll, { title }).addTo(map);
  } else {
    existing.setLatLng(ll);
  }
  existing.bindPopup(title);
  return existing;
}

function fitToMarkers() {
  const pts = [];
  if (originMarker) pts.push(originMarker.getLatLng());
  if (destMarker) pts.push(destMarker.getLatLng());
  if (!pts.length) return;
  if (pts.length === 1) map.setView(pts[0], 16);
  else map.fitBounds(L.latLngBounds(pts), { padding: [40, 40] });
}

function getSelectedMode() {
  const sel = document.getElementById('mode-select');
  return sel?.value || 'walk';
}

async function fetchRoute(orig, dest, mode) {
  const qs = new URLSearchParams({
    orig_lat: orig.lat, orig_lon: orig.lng,
    dest_lat: dest.lat, dest_lon: dest.lng,
    mode
  }).toString();
  const res = await fetch(`/route?${qs}`);
  if (!res.ok) throw new Error("Route failed");
  return await res.json(); // { geojson, total_weight, mode }
}

// Form submit: geocode both, set markers, call /route, draw it
const form = document.getElementById('od-form');
const originInput = document.getElementById('origin-input');
const destInput   = document.getElementById('dest-input');

form.addEventListener('submit', async (e) => {
  e.preventDefault();

  const originQ = originInput.value.trim();
  const destQ   = destInput.value.trim();
  const mode    = getSelectedMode();
  if (!originQ || !destQ) return alert("Please enter both origin and destination.");

  try {
    const [origRes, destRes] = await Promise.all([geocode(originQ), geocode(destQ)]);
    if (!origRes.length) return alert("Origin not found.");
    if (!destRes.length)  return alert("Destination not found.");

    const o = origRes[0], d = destRes[0];
    originMarker = setMarker(o.lat, o.lon, "Origin: " + o.display_name, originMarker);
    destMarker   = setMarker(d.lat, d.lon, "Destination: " + d.display_name, destMarker);
    fitToMarkers();

    // draw route
    const data = await fetchRoute({lat: o.lat, lng: o.lon}, {lat: d.lat, lng: d.lon}, mode);
    if (routeLayer) routeLayer.remove();
    routeLayer = L.geoJSON(data.geojson, { style: { color: "#1d4ed8", weight: 5 } }).addTo(map);
    map.fitBounds(routeLayer.getBounds(), { padding: [40, 40] });

    console.log("Mode:", data.mode, "Total weight:", data.total_weight.toFixed(1));
  } catch (err) {
    console.error(err);
    alert(err.message || "Search/route failed.");
  }
});
