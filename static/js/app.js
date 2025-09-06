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
map.setView([38.6480784, -90.3089436], 15);

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
  renderRouteChoicesTemplate(data.routes, data.mode);
}
// Speeds for ETA (m/s); tweak to your liking
const MODE_SPEEDS = { walk: 1.3, bike: 4.5, drive: 11.0 };

function fmtMinutes(seconds) {
  const m = Math.round(seconds / 60);
  return `${m}m`;
}
function fmtMiles(meters) {
  return `${(meters / 1609.344).toFixed(1)} mi`;
}

// Build a friendly name by beta if backend didn't supply one
function nameForBeta(beta) {
  if (beta === 0) return "Fastest Route";
  if (beta >= 1) return "Safest Route";
  return "Balanced Route";
}

// Turn mean risk into a 0–100 “safety” score relative to the set
function safetyScores(routes) {
  const maxRisk = Math.max(1e-9, ...routes.map(r => r.stats.mean_risk || 0));
  return routes.map(r => {
    const rel = (r.stats.mean_risk || 0) / maxRisk;         // 0..1 (1 = worst)
    const score = Math.round((1 - Math.min(1, rel)) * 100); // 0..100 (100=best)
    return Math.max(0, Math.min(100, score));
  });
}
function safetyBadge(score) {
  if (score >= 80) return { text: "Safe",     klass: "badge badge-safe" };
  if (score >= 60) return { text: "Moderate", klass: "badge badge-moderate" };
  return { text: "Caution", klass: "badge badge-caution" };
}
function featureChips(beta) {
  if (beta === 0)   return ["Main thoroughfares", "Sidewalks available", "Direct route prioritizes speed"];
  if (beta >= 1.0)  return ["Excellent safety features", "Sidewalks available", "Longer distance"];
  return ["Mixed safety features", "Sidewalks available", "Moderate complexity"];
}

// === NEW: render like the template ===
function renderRouteChoicesTemplate(routes, mode = "walk") {
  const box = document.getElementById("route-choices");
  if (!box) return;
  box.innerHTML = "";

  const speeds = MODE_SPEEDS[mode] || MODE_SPEEDS.walk;
  const scores = safetyScores(routes);

  routes.forEach((r, idx) => {
    const length_m = r.stats.length_m || 0;
    // if you have per-edge speeds, you can compute ETA better; here we estimate from length + mode speed
    const etaSec = length_m / speeds;
    const safety = scores[idx];
    const badge  = safetyBadge(safety);
    const title  = r.name || nameForBeta(r.beta);
    const chips  = featureChips(r.beta);
    const miles  = fmtMiles(length_m);

    // Wrapper (clickable)
    const card = document.createElement("a");
    // TODO: change this URL later to your details page
    card.href = `/route-details?beta=${encodeURIComponent(r.beta)}&mode=${encodeURIComponent(mode)}`;
    card.className =
      "relative block rounded-3xl border border-slate-200 shadow-sm " +
      "hover:shadow-md transition overflow-hidden px-4 py-4";

    // Accent rail on the left using the route color
    const rail = document.createElement("div");
    rail.className = "route-rail";
    rail.style.background = r.color || "#1d4ed8";
    card.appendChild(rail);

    // Top row
    const top = document.createElement("div");
    top.className = "flex items-start justify-between";
    top.innerHTML = `
      <div class="flex items-center gap-2">
        <div class="text-xl font-extrabold text-slate-900">${title}</div>
        <div class="text-xs text-slate-500 font-semibold">Optimal</div>
      </div>
      <span class="${badge.klass}">${badge.text}</span>
    `;
    card.appendChild(top);

    // Subrow: time + distance + safety score
    const meta = document.createElement("div");
    meta.className = "mt-2 grid grid-cols-2 sm:grid-cols-3 gap-3 text-sm";
    meta.innerHTML = `
      <div class="flex items-center gap-2 text-slate-700">
        <span class="material-symbols-outlined text-slate-500">schedule</span>
        <span class="font-semibold">${fmtMinutes(etaSec)}</span>
        <span class="text-slate-500">· ${miles}</span>
      </div>
      <div class="flex items-center gap-2 text-slate-700">
        <span class="material-symbols-outlined text-slate-500">shield</span>
        <span class="font-semibold">${safety}/100</span>
        <span class="text-slate-500">Safety</span>
      </div>
      <div class="hidden sm:flex items-center justify-end text-slate-400 text-xs">
        <span>Confidence</span>&nbsp;<span>90%</span>
      </div>
    `;
    card.appendChild(meta);

    // Progress bar
    const progWrap = document.createElement("div");
    progWrap.className = "mt-3";
    progWrap.innerHTML = `
      <div class="text-xs text-slate-500 mb-1">Safety Score</div>
      <div class="progress-track">
        <div class="progress-fill" style="width:${safety}%;"></div>
      </div>
    `;
    card.appendChild(progWrap);

    // Feature chips row
    const chipsRow = document.createElement("div");
    chipsRow.className = "mt-3 flex flex-wrap gap-x-6 gap-y-2 text-sm";
    chips.forEach((c, i) => {
      const span = document.createElement("span");
      // make the third chip orange like the mock
      span.className = i === 2 ? "text-orange-600 font-semibold" : "text-slate-700";
      span.textContent = c;
      chipsRow.appendChild(span);
    });
    card.appendChild(chipsRow);

    // Subtitle
    const subtitle = document.createElement("div");
    subtitle.className = "mt-2 text-sm text-slate-400";
    subtitle.textContent =
      r.beta === 0 ? "Direct route optimized for walk"
    : r.beta >= 1 ? "Maximum safety route for walk"
                  : "Good compromise between speed and safety for walk";
    card.appendChild(subtitle);

    // Chevron
    const chev = document.createElement("div");
    chev.className = "absolute right-3 top-3 text-slate-400";
    chev.innerHTML = `<span class="material-symbols-outlined">chevron_right</span>`;
    card.appendChild(chev);

    box.appendChild(card);
  });
}
// ---- Crime dots layer ----
let crimeLayer = null;

function colorBySeverity(sev) {
  // blue->orange scale; tweak as desired
  if (sev >= 100) return "#d97706"; // amber-600 for very severe
  if (sev >= 50)  return "#f59e0b"; // amber-500
  if (sev >= 10)  return "#60a5fa"; // blue-400
  return "#93c5fd";                 // blue-300 (low)
}

function radiusBySeverity(sev) {
  // Keep small to avoid clutter; clamp between 2 and 8
  const r = 2 + Math.log10(Math.max(1, sev+1)) * 3;
  return Math.max(2, Math.min(8, r));
}

async function fetchCrimesInView() {
  const b = map.getBounds();
  const qs = new URLSearchParams({
    west:  b.getWest(), south: b.getSouth(),
    east:  b.getEast(), north: b.getNorth()
  }).toString();
  const res = await fetch(`/crimes?${qs}`);
  if (!res.ok) throw new Error("Failed to load crimes");
  return await res.json();
}

async function showCrimeDots() {
  const data = await fetchCrimesInView();
  // Create layer once
  if (!crimeLayer) {
    crimeLayer = L.layerGroup().addTo(map);
  } else {
    crimeLayer.clearLayers();
  }

  // Render as circle markers (Canvas-backed for speed)
  const pts = [];
  data.features.forEach(f => {
    const [lng, lat] = f.geometry.coordinates;
    const sev = f.properties?.severity ?? 0;
    const marker = L.circleMarker([lat, lng], {
      radius: radiusBySeverity(sev),
      color: colorBySeverity(sev),
      fillColor: colorBySeverity(sev),
      fillOpacity: 0.7,
      weight: 0.5,
      opacity: 0.9
    }).bindTooltip(`Severity: ${sev}`, { direction: 'top', offset: [0, -6] });
    pts.push(marker);
  });
  if (pts.length) L.featureGroup(pts).addTo(crimeLayer);
}

function hideCrimeDots() {
  if (crimeLayer) {
    crimeLayer.remove();
    crimeLayer = null;
  }
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
    document.getElementById("route-sheet").style.display = "block";
    document.dispatchEvent(new Event("routes:updated"));
  } catch (err) {
    console.error(err);
    alert(err.message || "Search/route failed.");
  }
});

