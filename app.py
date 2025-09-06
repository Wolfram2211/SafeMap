# app.py
from flask import Flask, render_template, request, jsonify
import os
import osmnx as ox
import networkx as nx
import numpy as np
import pandas as pd
from sklearn.neighbors import BallTree
import geopandas as gpd

app = Flask(__name__)

# ----- CONFIG: pick a small area to keep routing fast -----
# St. Louis bbox example (tweak these)
NORTH, SOUTH = 38.655, 38.635
EAST,  WEST  = -90.28, -90.31

# ----- Build graphs once at startup (OSMnx 1.x signatures) -----
def build_graph(network_type: str):
    G = ox.graph_from_bbox(north=NORTH, south=SOUTH, east=EAST, west=WEST, network_type=network_type)
    Gp = ox.project_graph(G)  # projected (meters), keeps same node IDs
    # Ensure 'length' exists on edges
    for u, v, k, d in Gp.edges(keys=True, data=True):
        if "length" not in d:
            x1,y1 = Gp.nodes[u]["x"], Gp.nodes[u]["y"]
            x2,y2 = Gp.nodes[v]["x"], Gp.nodes[v]["y"]
            d["length"] = float(((x2-x1)**2 + (y2-y1)**2) ** 0.5)
    return G, Gp

G_walk,  Gp_walk  = build_graph("walk")
G_bike,  Gp_bike  = build_graph("bike")
G_drive, Gp_drive = build_graph("drive")

# ----- Crime weights (run at startup; re-run when data changes) -----
def load_crimes():
    # Replace with your data source; demo CSV if present, else small sample
    if os.path.exists("crimes.csv"):
        df = pd.read_csv("crimes.csv")  # expects columns: lat, lon, severity
        return df
    return pd.DataFrame([
        {"lat": 38.6468, "lon": -90.2962, "severity": 5},
        {"lat": 38.6450, "lon": -90.3005, "severity": 3},
        {"lat": 38.6422, "lon": -90.2880, "severity": 4},
    ])

CRIMES = load_crimes()

def apply_crime_weights(Gp, crimes_df, R=300.0, alpha=150.0, beta=0.3):
    """Adds crime_risk ∈ [0,1] to nodes/edges and materializes final_w per edge."""
    # project crimes to graph CRS
    crimes_gdf = gpd.GeoDataFrame(
        crimes_df, geometry=gpd.points_from_xy(crimes_df["lon"], crimes_df["lat"]), crs="EPSG:4326"
    ).to_crs(Gp.graph["crs"])

    node_ids, node_data = zip(*Gp.nodes(data=True))
    nodes_xy = np.c_[[d["x"] for d in node_data], [d["y"] for d in node_data]]

    # If no crimes, zero risk
    if len(crimes_gdf) == 0:
        for n in node_ids: Gp.nodes[n]["crime_risk"] = 0.0
    else:
        crime_xy = np.c_[crimes_gdf.geometry.x.values, crimes_gdf.geometry.y.values]
        sev = crimes_gdf["severity"].to_numpy()
        tree = BallTree(crime_xy, metric="euclidean")
        idxs, dists = tree.query_radius(nodes_xy, r=R, return_distance=True)

        risk_raw = np.zeros(len(node_ids), dtype=float)
        for i, (I, D) in enumerate(zip(idxs, dists)):
            if len(I):
                risk_raw[i] = float((sev[I] * np.exp(-D/alpha)).sum())

        # normalize node risk to [0,1]
        if risk_raw.max() > 0:
            risk_node = (risk_raw - risk_raw.min()) / (risk_raw.max() - risk_raw.min())
        else:
            risk_node = np.zeros_like(risk_raw)

        for nid, r in zip(node_ids, risk_node):
            Gp.nodes[nid]["crime_risk"] = float(r)

    # edge risk = max of endpoint risks (or mean if you prefer)
    for u, v, k, d in Gp.edges(keys=True, data=True):
        ru = Gp.nodes[u].get("crime_risk", 0.0)
        rv = Gp.nodes[v].get("crime_risk", 0.0)
        d["crime_risk"] = max(ru, rv)

    # materialize final weight: distance * (1 + beta * risk)
    for _, _, _, d in Gp.edges(keys=True, data=True):
        L = float(d.get("length", 1.0))
        r = float(d.get("crime_risk", 0.0))
        d["final_w"] = L * (1.0 + beta * r)

# apply once to all three graphs (tune R/alpha/beta to taste)
for Gp in (Gp_walk, Gp_bike, Gp_drive):
    apply_crime_weights(Gp, CRIMES, R=300.0, alpha=150.0, beta=0.3)

# ---------- Utilities ----------
def route_to_geojson(G, Gp, route):
    """Build a GeoJSON LineString following each edge's geometry (correct key)."""
    from itertools import tee
    def pairwise(seq): a, b = tee(seq); next(b, None); return zip(a, b)
    coords = []
    for u, v in pairwise(route):
        # pick the (u,v,k) the router would choose: minimal final_w
        k_best = min(Gp.get_edge_data(u, v).items(), key=lambda kv: kv[1].get("final_w", 1e18))[0]
        d_geo = G.get_edge_data(u, v, k_best)
        if d_geo and d_geo.get("geometry") is not None:
            seg = [(lat, lon) for lon, lat in d_geo["geometry"].coords]
        else:
            seg = [(G.nodes[u]["y"], G.nodes[u]["x"]), (G.nodes[v]["y"], G.nodes[v]["x"])]
        if coords and coords[-1] == seg[0]: coords.extend(seg[1:])
        else: coords.extend(seg)
    # GeoJSON expects [lon,lat]
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {},
            "geometry": {"type": "LineString", "coordinates": [[y_x[1], y_x[0]] for y_x in coords]}
        }]
    }

def pick_graph(mode: str):
    mode = (mode or "walk").lower()
    if mode == "bike":  return G_bike,  Gp_bike
    if mode == "drive": return G_drive, Gp_drive
    return G_walk, Gp_walk

# ---------- Routes ----------
@app.get("/")
def index():
    return render_template("index.html")

@app.get("/geocode")
def geocode():
    # Minimal proxy to Nominatim (remember to set a UA in production)
    import requests
    q = request.args.get("q", "").strip()
    if not q: return jsonify([])
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": q, "format": "jsonv2", "limit": 5, "addressdetails": 1}
    headers = {"User-Agent": "SafeMap/1.0 (contact: you@example.com)"}
    r = requests.get(url, params=params, headers=headers, timeout=8)
    r.raise_for_status()
    data = r.json()
    return jsonify([
        {"display_name": it.get("display_name"), "lat": float(it["lat"]), "lon": float(it["lon"])}
        for it in data
    ])

@app.route("/route", methods=["GET"])
def route_api():
    try:
        olat = float(request.args["orig_lat"]); olon = float(request.args["orig_lon"])
        dlat = float(request.args["dest_lat"]); dlon = float(request.args["dest_lon"])
    except Exception:
        return jsonify({"error": "Missing or invalid origin/destination"}), 400

    mode = request.args.get("mode", "walk")
    G, Gp = pick_graph(mode)

    s = ox.distance.nearest_nodes(G, X=olon, Y=olat)
    t = ox.distance.nearest_nodes(G, X=dlon, Y=dlat)

    path = nx.shortest_path(Gp, s, t, weight="final_w")
    total_w = nx.path_weight(Gp, path, weight="final_w")
    return jsonify({"geojson": route_to_geojson(G, Gp, path), "total_weight": total_w, "mode": mode})


if __name__ == "__main__":
    app.run(debug=True)
