import osmnx as ox
import networkx as nx
import folium
import pandas as pd
import numpy as np
from sklearn.neighbors import BallTree
from shapely.geometry import Point
import geopandas as gpd

G = ox.graph_from_place("St. Louis, Missouri, USA", network_type="drive")

# 2) add edge lengths (meters) and speeds (km/h), then compute travel time (seconds)
G = ox.add_edge_speeds(G)         # uses OSM tags to estimate speeds
G = ox.add_edge_travel_times(G)   # adds 'travel_time' attribute

# 3) define your custom weight (example: 1*travel_time + 5*unsealed_penalty)
def my_weight(u, v, data):
    t = data.get("travel_time", data.get("length", 1.0))
    surface = data.get("surface", "")
    return t + (5.0 if surface in {"gravel", "dirt", "unpaved"} else 0.0)

# 4) shortest path with custom weight
orig = ox.distance.nearest_nodes(G, -90.1987, 38.6270)  # lon, lat for St. Louis
dest = ox.distance.nearest_nodes(G, -90.25,   38.65)
route = nx.shortest_path(G, orig, dest, weight=my_weight)
#fig, ax = ox.plot_graph_route(G, route, route_linewidth=4, node_size=0)
route_coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in route]

# initialize map centered on first point
m = folium.Map(location=route_coords[0], zoom_start=14, tiles="CartoDB positron")

# add route line
folium.PolyLine(route_coords, weight=6, color="blue").add_to(m)

# add start/end markers
folium.Marker(route_coords[0], tooltip="Origin").add_to(m)
folium.Marker(route_coords[-1], tooltip="Destination").add_to(m)

m.save("route.html")