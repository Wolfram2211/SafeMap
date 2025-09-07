# app.py
from flask import Flask, render_template, request, jsonify
import os
import math
import osmnx as ox
import networkx as nx
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point as ShpPoint
from itertools import tee

app = Flask(__name__)

# =========
# CONFIG
# =========

# Default area (keep small for speed; adjust to your city/needs)
north, south = 38.655, 38.635
east,  west  = -90.28, -90.31

# If you have a crimes.csv with columns: lat, lon, severity
CRIMES_CSV = "crimes.csv"

# Betas to materialize as edge attributes
BETAS      = [0.0, 0.3, 1.0]
BETA_TAGS  = {0.0: "b0", 0.3: "b03", 1.0: "b1"}
COLORS     = {0.0: "#ff0000", 0.3: "#1d4ed8", 1.0: "#0fdf00"}  # gray, blue, amber
NAMES      = {0.0: "Shortest distance", 0.3: "Balanced safety", 1.0: "Avoid risk strongly"}

# =========
# GRAPH SETUP
# =========

def build_graph(network_type: str):
    """Build unprojected (G) and projected (Gp) graphs. OSMnx 1.x signatures."""
    center_lat = (north + south) / 2
    center_lon = (east + west) / 2
    # distance is in meters
    G = ox.graph_from_point((center_lat, center_lon), dist=3000, network_type=network_type)
    Gp = ox.project_graph(G)  # projected CRS (meters), node IDs preserved

    # ensure edge length exists (meters)
    for u, v, k, d in Gp.edges(keys=True, data=True):
        if "length" not in d:
            x1, y1 = Gp.nodes[u]["x"], Gp.nodes[u]["y"]
            x2, y2 = Gp.nodes[v]["x"], Gp.nodes[v]["y"]
            d["length"] = float(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)
    return G, Gp

G_walk,  Gp_walk  = build_graph("walk")
G_bike,  Gp_bike  = build_graph("bike")
G_drive, Gp_drive = build_graph("drive")

def pick_graph(mode: str):
    m = (mode or "walk").lower()
    if m == "bike":  return G_bike,  Gp_bike
    if m == "drive": return G_drive, Gp_drive
    return G_walk, Gp_walk

# =========
# CRIME WEIGHTS
# =========

def load_crimes():
    if os.path.exists(CRIMES_CSV):
        df = pd.read_csv(CRIMES_CSV)
        # basic sanity
        need = {"lat", "lon", "severity"}
        if not need.issubset(set(df.columns)):
            raise ValueError(f"{CRIMES_CSV} must contain columns: {need}")
        return df
    # fallback sample (very high severity point)
    return pd.DataFrame([
        {"lat": 38.6521540, "lon": -90.2940248, "severity": 500},
    ])

CRIMES = load_crimes()

def apply_crime_weights(Gp, crimes_df, R=300.0, alpha=150.0):
    """
    Compute node/edge crime_risk for a projected graph Gp.
    NOTE: This version intentionally does NOT normalize risk to [0,1],
    because your beta scaling expects raw sums (consistent with your version).
    If you want normalized risk, normalize risk_raw before assigning.
    """
    # project crimes to graph CRS
    crimes_gdf = gpd.GeoDataFrame(
        crimes_df,
        geometry=gpd.points_from_xy(crimes_df["lon"], crimes_df["lat"]),
        crs="EPSG:4326",
    ).to_crs(Gp.graph["crs"])

    node_ids, node_data = zip(*Gp.nodes(data=True))
    nodes_xy = np.c_[[d["x"] for d in node_data], [d["y"] for d in node_data]]

    if len(crimes_gdf) == 0:
        for n in node_ids:
            Gp.nodes[n]["crime_risk"] = 0.0
    else:
        crime_xy = np.c_[crimes_gdf.geometry.x.values, crimes_gdf.geometry.y.values]
        sev = crimes_gdf["severity"].to_numpy()

        # brute-force radius query (fast enough for neighborhood graphs)
        risk_raw = np.zeros(len(node_ids), dtype=float)
        for i, (x, y) in enumerate(nodes_xy):
            dx = crime_xy[:, 0] - x
            dy = crime_xy[:, 1] - y
            dist = np.hypot(dx, dy)
            mask = dist <= R
            if mask.any():
                # exponential decay
                risk_raw[i] = float((sev[mask] * np.exp(-dist[mask] / alpha)).sum())

        # Assign raw (non-normalized) risk
        for nid, r in zip(node_ids, risk_raw):
            Gp.nodes[nid]["crime_risk"] = float(r)

    # edge risk = max of endpoint risks (use mean if you prefer)
    for u, v, k, d in Gp.edges(keys=True, data=True):
        ru = Gp.nodes[u].get("crime_risk", 0.0)
        rv = Gp.nodes[v].get("crime_risk", 0.0)
        d["crime_risk"] = max(ru, rv)

def materialize_final_weights(Gp, betas=BETAS):
    """
    For each beta, compute and store edge attribute:
      final_w_<tag> = length * (1 + beta * crime_risk)
    where <tag> is b0, b03, b1 for 0, 0.3, 1.0 respectively.
    """
    for _, _, _, d in Gp.edges(keys=True, data=True):
        L = float(d.get("length", 1.0))
        r = float(d.get("crime_risk", 0.0))
        for beta in betas:
            tag = BETA_TAGS[beta]
            d[f"final_w_{tag}"] = L * (1.0 + beta * r)

# Apply once for the three cached graphs
for Gp in (Gp_walk, Gp_bike, Gp_drive):
    apply_crime_weights(Gp, CRIMES, R=300.0, alpha=150.0)
    materialize_final_weights(Gp, betas=BETAS)

# =========
# ROUTING HELPERS
# =========

def pairwise(seq):
    a, b = tee(seq)
    next(b, None)
    return zip(a, b)

def pick_edge_key_by_weight(Gp, u, v, weight_attr: str):
    """Choose the parallel edge key that minimizes the given weight attribute."""
    ed = Gp.get_edge_data(u, v)
    return min(ed.items(), key=lambda kv: float(kv[1].get(weight_attr, 1e18)))[0]

def route_to_geojson_by_weight(G, Gp, route, weight_attr: str):
    """
    Follow exact (u,v,k) chosen under this weight_attr; return GeoJSON + stats.
    """
    coords = []
    total_w = 0.0
    total_L = 0.0
    total_LxR = 0.0

    for u, v in pairwise(route):
        k = pick_edge_key_by_weight(Gp, u, v, weight_attr)
        d = Gp.get_edge_data(u, v, k)

        L = float(d.get("length", 0.0))
        r = float(d.get("crime_risk", 0.0))
        w = float(d.get(weight_attr, 0.0))

        total_L  += L
        total_LxR += L * r
        total_w  += w

        d_geo = G.get_edge_data(u, v, k)
        if d_geo and d_geo.get("geometry") is not None:
            seg = [(lat, lon) for lon, lat in d_geo["geometry"].coords]
        else:
            seg = [(G.nodes[u]["y"], G.nodes[u]["x"]),
                   (G.nodes[v]["y"], G.nodes[v]["x"])]

        if coords and coords[-1] == seg[0]:
            coords.extend(seg[1:])
        else:
            coords.extend(seg)

    mean_risk = (total_LxR / total_L) if total_L > 0 else 0.0
    geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"weight_attr": weight_attr},
            "geometry": {
                "type": "LineString",
                "coordinates": [[lng, lat] for (lat, lng) in coords]
            }
        }]
    }
    stats = {
        "total_weight": total_w,
        "length_m": total_L,
        "risk_length_sum_m": total_LxR,
        "mean_risk": mean_risk,
    }
    return geojson, stats

# -------- Edge-based snapping (reduces endpoint offset) --------

def snap_to_nearest_edge_endpoint(G, Gp, lat: float, lon: float):
    """
    Snap a WGS84 point to the nearest edge in the projected graph, then
    choose the closer endpoint (u or v).
    Returns: node_id, snapped_lat, snapped_lon, offset_m
    """
    # project the query point
    gdf = gpd.GeoDataFrame(geometry=[gpd.points_from_xy([lon], [lat])[0]], crs="EPSG:4326").to_crs(Gp.graph["crs"])
    px, py = float(gdf.geometry.x.iloc[0]), float(gdf.geometry.y.iloc[0])
    p_proj = ShpPoint(px, py)

    best = (None, None, None, float("inf"))  # (u,v,k,dist)
    for u, v, k, d in Gp.edges(keys=True, data=True):
        if "geometry" in d and d["geometry"] is not None:
            dist = d["geometry"].distance(p_proj)
        else:
            # straight segment fallback
            from shapely.geometry import LineString
            seg = LineString([(Gp.nodes[u]["x"], Gp.nodes[u]["y"]),
                              (Gp.nodes[v]["x"], Gp.nodes[v]["y"])])
            dist = seg.distance(p_proj)
        if dist < best[3]:
            best = (u, v, k, dist)

    u, v, k, dist_m = best
    if u is None:
        # fallback to nearest node if nothing found
        nid = ox.distance.nearest_nodes(G, X=lon, Y=lat)
        return nid, G.nodes[nid]["y"], G.nodes[nid]["x"], 0.0

    du = math.hypot(Gp.nodes[u]["x"] - px, Gp.nodes[u]["y"] - py)
    dv = math.hypot(Gp.nodes[v]["x"] - px, Gp.nodes[v]["y"] - py)
    nid = u if du <= dv else v
    return nid, G.nodes[nid]["y"], G.nodes[nid]["x"], float(dist_m)

# =========
# ROUTES
# =========
@app.route("/")
def welcome():
    # New landing page
    return render_template("welcome.html")

@app.route("/map_page")
def map_page():
    return render_template("index.html")

@app.route("/geocode", methods=["GET"])
def geocode():
    import requests
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": q, "format": "jsonv2", "limit": 5, "addressdetails": 1}
    headers = {"User-Agent": "SafeMap/1.0 (contact: you@example.com)"}  # set yours
    r = requests.get(url, params=params, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    out = [{"display_name": it.get("display_name"),
            "lat": float(it["lat"]),
            "lon": float(it["lon"])} for it in data]
    return jsonify(out)

@app.route("/route_multi", methods=["GET"])
def route_multi():
    # Inputs
    try:
        olat = float(request.args["orig_lat"]); olon = float(request.args["orig_lon"])
        dlat = float(request.args["dest_lat"]); dlon = float(request.args["dest_lon"])
    except Exception:
        return jsonify({"error": "Missing or invalid origin/destination"}), 400

    mode = (request.args.get("mode", "walk") or "walk").lower()
    G, Gp = pick_graph(mode)

    # Snap endpoints (edge-based)
    s, s_lat, s_lon, s_off = snap_to_nearest_edge_endpoint(G, Gp, olat, olon)
    t, t_lat, t_lon, t_off = snap_to_nearest_edge_endpoint(G, Gp, dlat, dlon)

    routes_out = []
    for beta in BETAS:
        tag = BETA_TAGS[beta]
        weight_attr = f"final_w_{tag}"

        # Route using MATERIALIZED weights
        path = nx.shortest_path(Gp, s, t, weight=weight_attr)
        geojson, stats = route_to_geojson_by_weight(G, Gp, path, weight_attr)

        desc = (f"{NAMES[beta]} (β={beta}). "
                f"Length {stats['length_m']/1000:.2f} km. "
                f"Mean risk {stats['mean_risk']:.3f}. "
                f"Risk exposure ∑(L·risk) = {stats['risk_length_sum_m']:.0f} m.")

        routes_out.append({
            "beta": beta,
            "name": NAMES[beta],
            "color": COLORS[beta],
            "weight_attr": weight_attr,
            "geojson": geojson,
            "stats": stats,
            "description": desc
        })

    # Deltas vs beta=0 for UI
    base_len  = routes_out[0]["stats"]["length_m"]
    base_risk = routes_out[0]["stats"]["risk_length_sum_m"]
    for r in routes_out:
        rlen  = r["stats"]["length_m"]
        rrisk = r["stats"]["risk_length_sum_m"]
        r["stats"]["detour_m_vs_beta0"]   = rlen - base_len
        r["stats"]["risk_delta_vs_beta0"] = rrisk - base_risk

    return jsonify({
        "mode": mode,
        "snapped_origin": {"lat": s_lat, "lon": s_lon},
        "snapped_destination": {"lat": t_lat, "lon": t_lon},
        "snap_dist_m": {"origin": s_off, "destination": t_off},
        "routes": routes_out
    })

# (Optional) simple route for single β (kept for compatibility)
@app.route("/route", methods=["GET"])
def route_api():
    try:
        olat = float(request.args["orig_lat"]); olon = float(request.args["orig_lon"])
        dlat = float(request.args["dest_lat"]); dlon = float(request.args["dest_lon"])
    except Exception:
        return jsonify({"error": "Missing or invalid origin/destination"}), 400

    mode = (request.args.get("mode", "walk") or "walk").lower()
    beta = float(request.args.get("beta", "0.3"))
    if beta not in BETA_TAGS:
        return jsonify({"error": f"beta must be one of {list(BETA_TAGS.keys())}"}), 400

    G, Gp = pick_graph(mode)
    s, s_lat, s_lon, s_off = snap_to_nearest_edge_endpoint(G, Gp, olat, olon)
    t, t_lat, t_lon, t_off = snap_to_nearest_edge_endpoint(G, Gp, dlat, dlon)

    weight_attr = f"final_w_{BETA_TAGS[beta]}"
    path = nx.shortest_path(Gp, s, t, weight=weight_attr)
    geojson, stats = route_to_geojson_by_weight(G, Gp, path, weight_attr)

    return jsonify({
        "mode": mode,
        "beta": beta,
        "snapped_origin": {"lat": s_lat, "lon": s_lon},
        "snapped_destination": {"lat": t_lat, "lon": t_lon},
        "snap_dist_m": {"origin": s_off, "destination": t_off},
        "geojson": geojson,
        "stats": stats
    })

# --------- Dev helper: list routes -----------
@app.route("/routes", methods=["GET"])
def list_routes():
    return "<pre>" + "\n".join(sorted(str(r) for r in app.url_map.iter_rules())) + "</pre>"

@app.route("/crimes", methods=["GET"])
def crimes_api():
    # Optionally filter by bbox if provided: ?west=&south=&east=&north=
    try:
        west  = float(request.args.get("west"))   if request.args.get("west")  else None
        south = float(request.args.get("south"))  if request.args.get("south") else None
        east  = float(request.args.get("east"))   if request.args.get("east")  else None
        north = float(request.args.get("north"))  if request.args.get("north") else None
    except Exception:
        west = south = east = north = None

    df = CRIMES.copy()
    if all(v is not None for v in (west, south, east, north)):
        df = df[(df["lon"] >= west) & (df["lon"] <= east) &
                (df["lat"] >= south) & (df["lat"] <= north)]

    features = []
    for _, row in df.iterrows():
        features.append({
            "type": "Feature",
            "properties": {
                "severity": float(row.get("severity", 0.0))
            },
            "geometry": {
                "type": "Point",
                "coordinates": [float(row["lon"]), float(row["lat"])]
            }
        })
    return jsonify({"type": "FeatureCollection", "features": features})

@app.route("/route-details")
def route_details_page():
    # Simple template; data comes via query params
    return render_template("route_details.html")

# stub pages you can replace later
@app.route("/panic")
def panic_page():
    return "<h1>PANIC</h1><p>Implement SOS workflow here.</p>"

@app.route("/end")
def end_page():
    return "<h1>Route Ended</h1><p>Summary / feedback goes here.</p>"

# =========
# MAIN
# =========

if __name__ == "__main__":
    # For LAN testing, bind to all interfaces
    app.run(host="0.0.0.0", port=5000, debug=True)
