import osmnx as ox
import networkx as nx
import folium
import pandas as pd
import random
import numpy as np
from sklearn.neighbors import BallTree
from shapely.geometry import Point
import geopandas as gpd
from shapely.geometry import Point
from itertools import tee
def nearby_node_weights(Gp, lat, lon, radius_m=200):
    """
    Show node crime_risk for nodes within radius (meters) of a lat/lon point.
    """
    # project the point into the same CRS as Gp
    point_gdf = gpd.GeoDataFrame(
        geometry=[Point(lon, lat)], crs="EPSG:4326"
    ).to_crs(Gp.graph["crs"])
    px, py = point_gdf.geometry.x.iloc[0], point_gdf.geometry.y.iloc[0]

    rows = []
    for nid, d in Gp.nodes(data=True):
        dx = d["x"] - px
        dy = d["y"] - py
        dist = (dx**2 + dy**2) ** 0.5
        if dist <= radius_m:
            rows.append((nid, dist, d.get("crime_risk", 0.0)))

    df = pd.DataFrame(rows, columns=["node", "dist_m", "crime_risk"]).sort_values("dist_m")
    return df
def pairwise(seq):
    a, b = tee(seq)
    next(b, None)
    return zip(a, b)
def add_route_with_edge_geometry(G, Gp, route, m, color="blue", weight=6, opacity=0.9):
    from itertools import tee
    def pairwise(seq):
        a, b = tee(seq); next(b, None); return zip(a, b)

    for u, v in pairwise(route):
        # choose the key the router would use = minimal final_w among parallel edges
        k_best = min(Gp.get_edge_data(u, v).items(), key=lambda kv: kv[1].get("final_w", float("inf")))[0]

        d_geo = G.get_edge_data(u, v, k_best)
        if d_geo and d_geo.get("geometry") is not None:
            coords = [(lat, lon) for lon, lat in d_geo["geometry"].coords]
        else:
            coords = [(G.nodes[u]["y"], G.nodes[u]["x"]),
                      (G.nodes[v]["y"], G.nodes[v]["x"])]

        folium.PolyLine(coords, color=color, weight=weight, opacity=opacity).add_to(m)
def add_risky_edges_with_tooltips(G, Gp, m, min_risk=0.0, both_endpoints=False):
    drawn = 0
    for u, v, k, d in Gp.edges(keys=True, data=True):
        ru = Gp.nodes[u].get("crime_risk", 0.0)
        rv = Gp.nodes[v].get("crime_risk", 0.0)
        er = float(d.get("crime_risk", 0.0))  # normalized risk
        cond = (ru > min_risk and rv > min_risk) if both_endpoints else (ru > min_risk or rv > min_risk or er > min_risk)
        if not cond:
            continue

        w = float(d.get("final_w", 0.0))
        L = float(d.get("length", 0.0))

        # geometry from G (WGS84)
        dg = G.get_edge_data(u, v, k)
        if dg and dg.get("geometry") is not None:
            coords = [(lat, lon) for lon, lat in dg["geometry"].coords]
        else:
            coords = [(G.nodes[u]["y"], G.nodes[u]["x"]),
                      (G.nodes[v]["y"], G.nodes[v]["x"])]

        tip = f"risk={er:.3f}<br>final_w={w:.1f}<br>length_m={L:.1f}"
        folium.PolyLine(coords, color="red", weight=4, opacity=0.8,
                        tooltip=folium.Tooltip(tip, sticky=True)).add_to(m)
        drawn += 1
    print(f"drawn {drawn} risky edges")
lat_min, lat_max = 38.641, 38.652
lon_min, lon_max = -90.321, -90.273

# Generate random crimes
n = 1
crimes = pd.DataFrame({
    "lat": [random.uniform(lat_min, lat_max) for _ in range(n)],
    "lon": [random.uniform(lon_min, lon_max) for _ in range(n)],
    "severity": [random.randint(1, 5) for _ in range(n)]
})
new_row = {"lat":38.6526264, "lon":-90.2939061, "severity":150}
crimes = pd.concat([crimes, pd.DataFrame([new_row])], ignore_index=True)
north, south = 38.652, 38.641   # latitudes
east, west   = -90.273, -90.321  # longitudes

G = ox.graph_from_point(((north+south)/2, (east+west)/2), dist=1000, network_type="walk")
Gp = ox.project_graph(G)
crs = Gp.graph["crs"]
crimes_gdf = gpd.GeoDataFrame(
    crimes,
    geometry=gpd.points_from_xy(crimes["lon"], crimes["lat"]),
    crs="EPSG:4326"
).to_crs(Gp.graph["crs"])

# 2b) Build a BallTree in meters for fast neighbor queries
crime_xy = np.vstack([crimes_gdf.geometry.x.values, crimes_gdf.geometry.y.values]).T
if len(crime_xy) == 0:
    raise ValueError("No crime points provided.")
tree = BallTree(crime_xy, metric="euclidean")

# 2c) Collect node coordinates (in meters, from projected graph)
node_ids, node_data = zip(*Gp.nodes(data=True))
nodes_xy = np.vstack([ [d["x"], d["y"]] for d in node_data ])

# 2d) Parameters for risk
R = 300.0   # meters: only consider crimes within 1 km
alpha = 30.0  # meters: decay length-scale

# Query crimes within R for each node
# radius_neighbors returns (indices, distances) for neighbors within R
indices, dists = tree.query_radius(nodes_xy, r=R, return_distance=True, sort_results=False)

# 2e) Compute risk per node
risk_per_node = np.zeros(len(node_ids), dtype=float)
sev = crimes_gdf["severity"].to_numpy()

for i, (idxs, ds) in enumerate(zip(indices, dists)):
    if len(idxs) == 0:
        continue
    # exponential decay
    weights = np.exp(-ds / alpha)
    risk_per_node[i] = np.sum(sev[idxs] * weights)

# Attach node risk to graph
for nid, r in zip(node_ids, risk_per_node):
    Gp.nodes[nid]["crime_risk"] = float(r)

# Compute edge risk = mean(node_u, node_v) risk
for u, v, k, data in Gp.edges(keys=True, data=True):
    ru = Gp.nodes[u].get("crime_risk", 0.0)
    rv = Gp.nodes[v].get("crime_risk", 0.0)
    data["crime_risk"] = max(ru, rv)

# Custom weight: distance * (1 + beta * risk)
beta = 0.1  # tune this to control how strongly crime risk influences routing

for u, v, k, d in Gp.edges(keys=True, data=True):
    L = float(d.get("length", 1.0))
    r = float(d.get("crime_risk", 0.0))
    d["final_w"] = L * (1.0 + beta * r)

orig_node = ox.distance.nearest_nodes(G, X=-90.28567, Y=38.64848)
dest_node = ox.distance.nearest_nodes(G, X=-90.30146,   Y=38.65571)

route = nx.shortest_path(Gp, orig_node, dest_node, weight="final_w")

route_latlon = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in route]

m = folium.Map(location=[G.nodes[route[0]]['y'], G.nodes[route[0]]['x']], zoom_start=13, tiles="CartoDB positron")

# crime points as small circle markers (careful: many points can be heavy)
for y, x, s in zip(crimes["lat"], crimes["lon"], crimes["severity"]):
    folium.CircleMarker([y, x], radius=3, fill=True, opacity=0.7,
                        tooltip=f"severity: {s}").add_to(m)

# start/end markers
folium.Marker(route_latlon[0], tooltip="Origin").add_to(m)
folium.Marker(route_latlon[-1], tooltip="Destination").add_to(m)
hot_lat, hot_lon = 38.6526264, -90.2939061
df_nearby = nearby_node_weights(Gp, hot_lat, hot_lon, radius_m=200)
print(df_nearby)

edge_risks = [d.get("crime_risk", 0.0) for _, _, _, d in Gp.edges(keys=True, data=True)]
max_er = max(edge_risks) if edge_risks else 1.0
add_route_with_edge_geometry(G, Gp, route, m, color="blue", weight=6)
add_risky_edges_with_tooltips(G, Gp, m, min_risk=0.0, both_endpoints=False)

for nid in Gp.nodes:
    r = Gp.nodes[nid].get("crime_risk", 0.0)
    if r > 0:
        lat = G.nodes[nid]['y']  # WGS84 latitude
        lon = G.nodes[nid]['x']  # WGS84 longitude
        folium.CircleMarker(
            [lat, lon],
            radius=3,   # make higher risk a bit bigger
            fill=True,
            fill_opacity=0.7,
            color=None,
            fill_color="red",
            tooltip=f"risk={r:.3f}"
        ).add_to(m)
m.save("crime_aware_route.html")