import sys, json, argparse, math, os
import geopandas as gpd
import networkx as nx
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points
from sqlalchemy import create_engine
import pyproj

# ── ARGUMENT PARSER ───────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument('--from',  dest='start', type=int, required=True)
parser.add_argument('--to',    dest='end',   type=int, required=True)
parser.add_argument('--floor', dest='floor', type=str, default='111')
args = parser.parse_args()

START_POI = args.start
END_POI   = args.end
FLOOR     = args.floor

# ── LOAD DATA — tabel dinamis sesuai floor ────────────────
try:
    DB_HOST = os.getenv("DB_HOST")
    DB_PORT = os.getenv("DB_PORT")
    DB_NAME = os.getenv("DB_NAME")
    DB_USER = os.getenv("DB_USER")
    DB_PASS = os.getenv("DB_PASS")
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    engine = create_engine(DATABASE_URL)
    nodes   = gpd.read_postgis(f'SELECT * FROM node_{FLOOR}',    engine, geom_col='geom')
    network = gpd.read_postgis(f'SELECT * FROM network_{FLOOR}', engine, geom_col='geom')
except Exception as e:
    print(json.dumps({'error': f'Gagal load dari PostGIS: {str(e)}'}))
    sys.exit(1)

nodes.columns   = [c.lower() for c in nodes.columns]
network.columns = [c.lower() for c in network.columns]

nodes   = nodes.set_crs('EPSG:4326', allow_override=True).to_crs('EPSG:7855')
network = network.set_crs('EPSG:4326', allow_override=True).to_crs('EPSG:7855')

nodes   = nodes.set_geometry('geom')
network = network.set_geometry('geom')

network = network.explode(index_parts=False).reset_index(drop=True)
network = network.set_geometry('geom')

# ── BUILD GRAPH ───────────────────────────────────────────
def round_coord(x, y, precision=1):
    return (round(x, precision), round(y, precision))

G = nx.Graph()
for _, row in network.iterrows():
    coords      = list(row['geom'].coords)
    start_coord = round_coord(coords[0][0], coords[0][1])
    end_coord   = round_coord(coords[-1][0], coords[-1][1])
    if start_coord not in G:
        G.add_node(start_coord, x=start_coord[0], y=start_coord[1])
    if end_coord not in G:
        G.add_node(end_coord, x=end_coord[0], y=end_coord[1])
    G.add_edge(start_coord, end_coord, weight=row['geom'].length, geometry=row['geom'])

# ── PROJECT POI KE NETWORK ────────────────────────────────
all_lines = list(network['geom'])

def project_poi_to_network(G, poi_id, px, py, all_lines):
    poi_pt = Point(px, py)
    min_dist, nearest_line = float('inf'), None
    for line in all_lines:
        d = poi_pt.distance(line)
        if d < min_dist:
            min_dist, nearest_line = d, line

    proj_pt    = nearest_points(poi_pt, nearest_line)[1]
    proj_coord = round_coord(proj_pt.x, proj_pt.y)

    if proj_coord not in G:
        G.add_node(proj_coord, x=proj_coord[0], y=proj_coord[1])
        line_coords = list(nearest_line.coords)
        start_orig  = round_coord(line_coords[0][0],  line_coords[0][1])
        end_orig    = round_coord(line_coords[-1][0], line_coords[-1][1])
        min_seg_dist, insert_idx = float('inf'), 0
        for i in range(len(line_coords) - 1):
            seg = LineString([line_coords[i], line_coords[i+1]])
            d   = proj_pt.distance(seg)
            if d < min_seg_dist:
                min_seg_dist, insert_idx = d, i
        seg1 = line_coords[:insert_idx+1] + [(proj_pt.x, proj_pt.y)]
        seg2 = [(proj_pt.x, proj_pt.y)] + line_coords[insert_idx+1:]
        if G.has_edge(start_orig, end_orig):
            G.remove_edge(start_orig, end_orig)
        if len(seg1) >= 2:
            G.add_edge(start_orig, proj_coord, weight=LineString(seg1).length, geometry=LineString(seg1))
        if len(seg2) >= 2:
            G.add_edge(proj_coord, end_orig, weight=LineString(seg2).length, geometry=LineString(seg2))

    G.add_node(poi_id, x=px, y=py)
    connector = LineString([(px, py), (proj_coord[0], proj_coord[1])])
    G.add_edge(poi_id, proj_coord, weight=min_dist, geometry=connector)

for _, row in nodes.iterrows():
    project_poi_to_network(G, f"poi_{int(row['id'])}", row['geom'].x, row['geom'].y, all_lines)

# ── ROUTING ───────────────────────────────────────────────
start_node = f"poi_{START_POI}"
end_node   = f"poi_{END_POI}"

try:
    path   = nx.shortest_path(G, start_node, end_node, weight='weight')
    length = nx.shortest_path_length(G, start_node, end_node, weight='weight')
except nx.NetworkXNoPath:
    print(json.dumps({'error': 'Tidak ada rute yang tersedia'})); sys.exit(1)
except nx.NodeNotFound as e:
    print(json.dumps({'error': f'POI tidak ditemukan: {str(e)}'})); sys.exit(1)

# ── INSTRUKSI ARAH ────────────────────────────────────────
def get_direction(p1, p2, p3):
    v1 = (p2[0]-p1[0], p2[1]-p1[1])
    v2 = (p3[0]-p2[0], p3[1]-p2[1])
    cross = v1[0]*v2[1] - v1[1]*v2[0]
    dot   = v1[0]*v2[0] + v1[1]*v2[1]
    mag1, mag2 = math.hypot(*v1), math.hypot(*v2)
    if mag1 == 0 or mag2 == 0: return "Lurus"
    angle = math.degrees(math.acos(max(-1, min(1, dot/(mag1*mag2)))))
    if angle < 30: return "Lurus"
    return "Belok kiri" if cross > 0 else "Belok kanan"

def get_node_coord(G, node): return (G.nodes[node]['x'], G.nodes[node]['y'])

def get_poi_name(node, nodes_gdf):
    if isinstance(node, str) and node.startswith('poi_'):
        poi_id = int(node.replace('poi_', ''))
        match  = nodes_gdf[nodes_gdf['id'] == poi_id]
        if not match.empty: return match.iloc[0]['nama']
    return None

def get_clean_coords(path, G):
    return [(n, get_node_coord(G, n)) for n in path if not (isinstance(n, str) and n.startswith('poi_'))]

def dist_between(path, G, a, b):
    try: ia, ib = path.index(a), path.index(b)
    except ValueError: return 0
    dist = 0
    for j in range(ia, ib):
        e = G.get_edge_data(path[j], path[j+1])
        if e: dist += e['weight']
    return dist

def generate_instructions(path, G, nodes_gdf):
    instructions = []
    start_name = get_poi_name(path[0], nodes_gdf)
    end_name   = get_poi_name(path[-1], nodes_gdf)
    instructions.append({'type': 'start', 'text': f"Mulai dari {start_name}"})

    clean = get_clean_coords(path, G)
    if len(clean) < 2:
        instructions.append({'type': 'arrive', 'text': f"Tiba di {end_name}"}); return instructions

    first_dir = get_direction(clean[0][1], clean[1][1], clean[2][1]) if len(clean) >= 3 else "Lurus"

    belokan = []
    for i in range(1, len(clean) - 1):
        _, pc = clean[i-1]; curr_node, cc = clean[i]; _, nc = clean[i+1]
        d = get_direction(pc, cc, nc)
        if d != "Lurus": belokan.append({'node': curr_node, 'direction': d})

    checkpoints   = [path[0]] + [b['node'] for b in belokan] + [path[-1]]
    segment_dists = [dist_between(path, G, checkpoints[i], checkpoints[i+1]) for i in range(len(checkpoints)-1)]

    if segment_dists:
        if first_dir == "Lurus":
            instructions.append({'type': 'lurus', 'text': f"Jalan lurus ±{segment_dists[0]:.0f}m"})
        else:
            instructions.append({'type': first_dir.lower().replace(' ','_'), 'text': f"{first_dir}, lanjut ±{segment_dists[0]:.0f}m"})

    for i, b in enumerate(belokan):
        dist_after = segment_dists[i+1] if i+1 < len(segment_dists) else 0
        text = b['direction'] + (f", lanjut ±{dist_after:.0f}m" if dist_after > 1 else "")
        instructions.append({'type': b['direction'].lower().replace(' ','_'), 'text': text})

    instructions.append({'type': 'arrive', 'text': f"Tiba di {end_name}"})
    return instructions

instructions = generate_instructions(path, G, nodes)

# ── KONVERSI PATH KE KOORDINAT 4326 ──────────────────────
transformer = pyproj.Transformer.from_crs('EPSG:7855', 'EPSG:4326', always_xy=True)

clean_path = [path[0]]
for node in path[1:]:
    if node != clean_path[-1]: clean_path.append(node)

path_coords, seen_coords = [], set()
for u, v in zip(clean_path[:-1], clean_path[1:]):
    data = G.get_edge_data(u, v)
    if not data or 'geometry' not in data: continue
    coords = list(data['geometry'].coords)
    uc     = (G.nodes[u]['x'], G.nodes[u]['y'])
    if math.hypot(coords[-1][0]-uc[0], coords[-1][1]-uc[1]) < math.hypot(coords[0][0]-uc[0], coords[0][1]-uc[1]):
        coords = coords[::-1]
    for x, y in coords:
        lng, lat = transformer.transform(x, y)
        key = (round(lng,6), round(lat,6))
        if key not in seen_coords:
            seen_coords.add(key)
            path_coords.append([round(lng,8), round(lat,8)])

# ── OUTPUT JSON ───────────────────────────────────────────
output = {
    'start':          START_POI,
    'end':            END_POI,
    'floor':          FLOOR,
    'start_nama':     nodes[nodes['id']==START_POI].iloc[0]['nama'],
    'end_nama':       nodes[nodes['id']==END_POI].iloc[0]['nama'],
    'total_distance': round(length, 1),
    'path_coords':    path_coords,
    'instructions':   instructions
}
print(json.dumps(output, ensure_ascii=False))