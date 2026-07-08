import osmium
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

HIGHWAY_VALUES = frozenset({'motorway', 'trunk', 'primary', 'secondary', 'tertiary'})
AMENITY_VALUES = frozenset({'hospital', 'university', 'bus_station', 'police', 'marketplace'})
LEISURE_VALUES = frozenset({'park', 'garden', 'pitch', 'playground'})
NATURAL_VALUES = frozenset({'water'})
WATERWAY_VALUES = frozenset({'river', 'riverbank'})
ADMIN_LEVELS = frozenset({'8', '9'})
PLACE_VALUES = frozenset({'suburb', 'neighbourhood', 'city', 'town'})


class OSMExtractor(osmium.SimpleHandler):
    """Single-pass OSM extractor that reads .osm.pbf or .osm XML with locations=True.

    Produces 5 lists of dicts with GeoJSON geometry strings, ready for PostGIS insert.
    """

    def __init__(self):
        super().__init__()
        self._factory = osmium.geom.GeoJSONFactory()
        self._way_geoms = {}  # way_id -> [(lon, lat), ...]

        self.vias = []
        self.parques = []
        self.agua = []
        self.equipamiento = []
        self.limites = []

    # ── node ──────────────────────────────────────────────────────────────

    def node(self, n):
        tags = {tag.k: tag.v for tag in n.tags}
        amenity = tags.get("amenity", "")
        place = tags.get("place", "")

        if amenity in AMENITY_VALUES:
            self.equipamiento.append({
                "name": tags.get("name", ""),
                "amenity": amenity,
                "geom": _point_geojson(n.location.lon, n.location.lat),
            })

        if place in PLACE_VALUES:
            self.limites.append({
                "name": tags.get("name", ""),
                "admin_level": tags.get("admin_level", ""),
                "place": place,
                "geom": _point_geojson(n.location.lon, n.location.lat),
            })

    # ── way ───────────────────────────────────────────────────────────────

    def way(self, w):
        tags = {tag.k: tag.v for tag in w.tags}
        if not tags:
            return

        highway = tags.get("highway", "")
        amenity = tags.get("amenity", "")
        leisure = tags.get("leisure", "")
        landuse = tags.get("landuse", "")
        natural = tags.get("natural", "")
        waterway = tags.get("waterway", "")
        place = tags.get("place", "")
        admin_level = tags.get("admin_level", "")

        # --- VIAS (highway) ---
        if highway in HIGHWAY_VALUES:
            geojson = self._safe_linestring(w)
            if geojson:
                self.vias.append({
                    "name": tags.get("name", ""),
                    "highway": highway,
                    "geom": geojson,
                })
            return  # a way is only one layer

        coords, geojson_ls = self._safe_coords_and_linestring(w)
        if not coords:
            return

        is_closed = _ring_is_closed(coords)

        # --- EQUIPAMIENTO (way amenity → centroid) ---
        if amenity in AMENITY_VALUES:
            cx, cy = _centroid(coords)
            self.equipamiento.append({
                "name": tags.get("name", ""),
                "amenity": amenity,
                "geom": _point_geojson(cx, cy),
            })

        # --- PARQUES (leisure / landuse=recreation_ground → polygon) ---
        if leisure in LEISURE_VALUES or landuse == "recreation_ground":
            poly = self._to_polygon(geojson_ls, is_closed, coords)
            if poly:
                self.parques.append({
                    "name": tags.get("name", ""),
                    "leisure": leisure or "recreation_ground",
                    "geom": poly,
                })

        # --- AGUA (natural=water / waterway → polygon or linestring) ---
        if natural in NATURAL_VALUES or waterway in WATERWAY_VALUES:
            if is_closed and geojson_ls:
                self.agua.append({
                    "name": tags.get("name", ""),
                    "natural": natural,
                    "waterway": waterway,
                    "geom": _linestring_to_polygon(geojson_ls),
                })
            elif geojson_ls:
                self.agua.append({
                    "name": tags.get("name", ""),
                    "natural": natural,
                    "waterway": waterway,
                    "geom": geojson_ls,
                })

        # --- LIMITES (place as closed way → polygon) ---
        if place in PLACE_VALUES and is_closed:
            poly = self._to_polygon(geojson_ls, is_closed, coords)
            if poly:
                self.limites.append({
                    "name": tags.get("name", ""),
                    "admin_level": admin_level,
                    "place": place,
                    "geom": poly,
                })

        # Store for later relation assembly
        if coords and (admin_level in ADMIN_LEVELS or place in PLACE_VALUES
                       or leisure in LEISURE_VALUES or natural in NATURAL_VALUES
                       or waterway in WATERWAY_VALUES):
            self._way_geoms[w.id] = coords

    # ── relation ──────────────────────────────────────────────────────────

    def relation(self, r):
        tags = {tag.k: tag.v for tag in r.tags}
        admin_level = tags.get("admin_level", "")
        leisure = tags.get("leisure", "")
        natural = tags.get("natural", "")
        waterway = tags.get("waterway", "")
        place = tags.get("place", "")

        outer_coords = []
        for m in r.members:
            if m.type == "w" and m.role == "outer":
                if m.ref in self._way_geoms:
                    outer_coords.extend(self._way_geoms[m.ref])

        if not outer_coords:
            return

        if not _ring_is_closed(outer_coords):
            outer_coords.append(outer_coords[:1][0])

        geojson = json.dumps({
            "type": "Polygon",
            "coordinates": [[[c[0], c[1]] for c in outer_coords]],
        })

        # --- LIMITES (admin boundaries & places) ---
        if admin_level in ADMIN_LEVELS or place in PLACE_VALUES:
            self.limites.append({
                "name": tags.get("name", ""),
                "admin_level": admin_level,
                "place": place,
                "geom": geojson,
            })

        # --- PARQUES ---
        if leisure in LEISURE_VALUES:
            self.parques.append({
                "name": tags.get("name", ""),
                "leisure": leisure,
                "geom": geojson,
            })

        # --- AGUA ---
        if natural in NATURAL_VALUES or waterway in WATERWAY_VALUES:
            self.agua.append({
                "name": tags.get("name", ""),
                "natural": natural,
                "waterway": waterway,
                "geom": geojson,
            })

    # ── helpers ───────────────────────────────────────────────────────────

    def _safe_linestring(self, w):
        try:
            return self._factory.create_linestring(w)
        except (osmium.InvalidLocationError, RuntimeError):
            return None

    def _safe_coords_and_linestring(self, w):
        coords = []
        try:
            for n in w.nodes:
                coords.append((n.location.lon, n.location.lat))
        except osmium.InvalidLocationError:
            pass

        if not coords:
            return None, None

        geojson = self._safe_linestring(w)
        return coords, geojson

    @staticmethod
    def _to_polygon(geojson_ls, is_closed, coords):
        if geojson_ls and is_closed:
            return _linestring_to_polygon(geojson_ls)
        if coords and len(coords) >= 3:
            closed = list(coords)
            if not _ring_is_closed(closed):
                closed.append(closed[0])
            return json.dumps({
                "type": "Polygon",
                "coordinates": [[[c[0], c[1]] for c in closed]],
            })
        return None


# ── pure geometry helpers ──────────────────────────────────────────────────

def _point_geojson(lon, lat):
    return json.dumps({"type": "Point", "coordinates": [lon, lat]})


def _ring_is_closed(coords):
    if len(coords) < 3:
        return False
    return (abs(coords[0][0] - coords[-1][0]) < 1e-9
            and abs(coords[0][1] - coords[-1][1]) < 1e-9)


def _centroid(coords):
    if not coords:
        return (0, 0)
    n = len(coords)
    return (sum(c[0] for c in coords) / n, sum(c[1] for c in coords) / n)


def _linestring_to_polygon(geojson_str):
    geom = json.loads(geojson_str)
    return json.dumps({
        "type": "Polygon",
        "coordinates": [geom["coordinates"]],
    })


# ── public entry point ────────────────────────────────────────────────────

def extract_layers(pbf_path: str):
    """Extract 5 layers from an OSM PBF or XML file.

    Uses disk-backed sparse memory-mapped array for node locations
    to avoid OOM on large PBF files.

    Returns
    -------
    dict[str, list[dict]]
        Keys: vias, parques, agua, equipamiento, limites.
        Each value is a list of dicts with keys: name, <tag>, geom (GeoJSON str).
    """
    logger.info("Extrayendo capas de %s ...", pbf_path)
    extractor = OSMExtractor()
    extractor.apply_file(str(pbf_path), locations=True)

    layers = {
        "vias": extractor.vias,
        "parques": extractor.parques,
        "agua": extractor.agua,
        "equipamiento": extractor.equipamiento,
        "limites": extractor.limites,
    }

    for name, items in layers.items():
        logger.info("  %s: %d features", name, len(items))

    return layers
