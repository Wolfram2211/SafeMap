// static/js/app.js

// --- Leaflet initialization ---
// app.js
const map = L.map('map', {
  zoomControl: true,
  scrollWheelZoom: true,
  touchZoom: true,
  dragging: true,
  inertia: true,

  // iOS Safari quirk: disabling Leaflet’s "tap" handler often fixes dead touches
  tap: false
});

// if something disabled them earlier, re-enable explicitly:
map.dragging.enable();
map.touchZoom.enable();
map.scrollWheelZoom.enable();
map.boxZoom.enable();
map.keyboard.enable();

// Optional: move zoom buttons
map.zoomControl.setPosition('bottomright');

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

async function fetchRoutesMulti(orig, dest, mode) {
  const qs = new URLSearchParams({
    orig_lat: orig.lat, orig_lon: orig.lon,
    dest_lat: dest.lat, dest_lon: dest.lon,
    mode
  }).toString();
  const res = await fetch(`/route_multi?${qs}`);
  if (!res.ok) throw new Error("Route computation failed");
  return await res.json(); // {mode, snapped_origin, snapped_destination, routes:[...] }
}

let routeLayers = []; // store 3 Leaflet layers

function clearRoutes() {
  routeLayers.forEach(l => l && l.remove());
  routeLayers = [];
}

function drawMultiRoutes(data) {
  clearRoutes();
  const group = [];

  data.routes.forEach((r, idx) => {
    const layer = L.geoJSON(r.geojson, {
      style: { color: r.color, weight: 5, opacity: 0.9 }
    }).addTo(map);
    routeLayers.push(layer);
    group.push(layer);
  });

  // Fit to everything (snapped markers optional)
  const fg = L.featureGroup(group);
  if (group.length) map.fitBounds(fg.getBounds(), { padding: [40, 40] });

  // Render the choice cards
  renderRouteChoices(data.routes);
}

function renderRouteChoices(routes) {
  const box = document.getElementById('route-choices');
  if (!box) return;
  box.innerHTML = ""; // reset

  routes.forEach((r) => {
    const detour = r.stats.detour_m_vs_beta0;
    const detourTxt = (Math.abs(detour) < 1) ? "same distance as β=0"
                    : (detour > 0 ? `+${detour.toFixed(0)} m` : `${detour.toFixed(0)} m`);

    const riskDelta = r.stats.risk_delta_vs_beta0;
    const riskTxt = (Math.abs(riskDelta) < 1) ? "same risk as β=0"
                  : (riskDelta < 0 ? `−${Math.abs(riskDelta).toFixed(0)} m·risk` : `+${riskDelta.toFixed(0)} m·risk`);

    const card = document.createElement('div');
    card.className = 'route-card';
    card.innerHTML = `
      <div class="route-dot" style="background:${r.color}"></div>
      <div>
        <div class="route-title">${r.name} (β=${r.beta})</div>
        <div class="route-desc">
          ${ (r.stats.length_m/1000).toFixed(2) } km · mean risk ${ r.stats.mean_risk.toFixed(3) }
          · ${detourTxt} · ${riskTxt}
        </div>
      </div>
    `;
    // (No click behavior yet, as requested)
    box.appendChild(card);
  });
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
    const data = await fetchRoutesMulti({ lat: o.lat, lon: o.lon }, { lat: d.lat, lon: d.lon }, mode);
    drawMultiRoutes(data);

  } catch (err) {
    console.error(err);
    alert(err.message || "Search/route failed.");
  }
});
