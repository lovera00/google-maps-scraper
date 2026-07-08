#!/usr/bin/env python3
"""Calcula features de distancia OSM para celdas H3 usando STRtree batch (Shapely).

Estrategia:
1. Deduplica celdas H3 (~5x menos trabajo)
2. Construye STRtree por capa OSM
3. query_nearest batch para todas las celdas a la vez
4. shortest_line batch para distancias exactas
5. Upsert en batch a PostgreSQL

Uso:
    python scripts/osm_build_features.py
    python scripts/osm_build_features.py --limit 10000
    python scripts/osm_build_features.py --dry-run
"""
import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg
import h3
import numpy as np
from shapely import STRtree, Point, shortest_line
from shapely import wkt

from src.config.loader import load_config

logger = logging.getLogger("osm_features")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS osm_features_r9 (
    h3 TEXT PRIMARY KEY,
    dist_via_principal_m DOUBLE PRECISION,
    dist_parque_m DOUBLE PRECISION,
    dist_agua_m DOUBLE PRECISION,
    dist_hospital_m DOUBLE PRECISION,
    dist_universidad_m DOUBLE PRECISION,
    frente_avenida BOOLEAN,
    cerca_agua BOOLEAN,
    updated_at TIMESTAMPTZ DEFAULT NOW()
)
"""


def _parse_geojson_geom(geojson_str: str):
    """Parse a GeoJSON geometry string to a Shapely geometry via WKT."""
    geom = json.loads(geojson_str)
    geom_type = geom["type"]
    coords = geom["coordinates"]

    if geom_type == "Point":
        return wkt.loads(f"POINT({coords[0]} {coords[1]})")
    elif geom_type == "LineString":
        pts = ", ".join(f"{c[0]} {c[1]}" for c in coords)
        return wkt.loads(f"LINESTRING({pts})")
    elif geom_type == "Polygon":
        rings = []
        for ring in coords:
            pts = ", ".join(f"{c[0]} {c[1]}" for c in ring)
            rings.append(f"({pts})")
        return wkt.loads(f"POLYGON({', '.join(rings)})")
    elif geom_type == "MultiPolygon":
        polys = []
        for poly in coords:
            rings = []
            for ring in poly:
                pts = ", ".join(f"{c[0]} {c[1]}" for c in ring)
                rings.append(f"({pts})")
            polys.append(f"({', '.join(rings)})")
        return wkt.loads(f"MULTIPOLYGON({', '.join(polys)})")
    else:
        return wkt.loads(str(geom))


def _build_strtree(features, filter_tag=None, filter_value=None):
    """Build an STRtree from feature list, optionally filtering by tag."""
    geoms = []
    for f in features:
        if filter_tag and f.get(filter_tag) != filter_value:
            continue
        try:
            geom = _parse_geojson_geom(f["geom"])
            geoms.append(geom)
        except Exception:
            continue

    if not geoms:
        return None, np.array([])

    tree = STRtree(geoms)
    return tree, np.array(geoms)


def _batch_distance_m(tree, layer_geoms, query_points):
    """Compute distances in meters from query points to nearest feature in tree.

    Uses batched STRtree.query_nearest + shortest_line for performance.

    Returns numpy array of distances in meters (NaN if no feature found).
    """
    if tree is None or len(layer_geoms) == 0:
        return np.full(len(query_points), np.nan)

    n = len(query_points)
    raw = tree.query_nearest(query_points)
    raw = np.asarray(raw)

    if raw.size == 0:
        return np.full(n, np.nan)

    # shapely 2.x returns (2, m) array: row 0 = query_idx, row 1 = tree_idx
    # Take the first tree index for each unique query index (break ties)
    if raw.ndim == 2:
        query_idx = raw[0, :].astype(int)
        tree_idx = raw[1, :].astype(int)
    else:
        query_idx = np.arange(n)
        tree_idx = raw.astype(int)

    # Deduplicate: keep first match per query point
    seen = np.full(n, False)
    keep = np.zeros(len(query_idx), dtype=bool)
    for i in range(len(query_idx)):
        qi = query_idx[i]
        if not seen[qi]:
            seen[qi] = True
            keep[i] = True

    q_kept = query_idx[keep]
    t_kept = tree_idx[keep]

    # Build mapping: for each query with a match, store tree index
    nearest_idx = np.full(n, -1)
    nearest_idx[q_kept] = t_kept
    valid = nearest_idx >= 0

    dists = np.full(n, np.nan)
    if not np.any(valid):
        return dists

    # Batch shortest_line only for valid query-geom pairs
    valid_q = np.where(valid)[0]
    valid_geoms = layer_geoms[nearest_idx[valid_q]]
    valid_points = query_points[valid_q]

    lines = shortest_line(valid_points, valid_geoms)

    for j, i in enumerate(valid_q):
        try:
            coords = lines[j].coords
            p1 = coords[0]
            p2 = coords[1]
            dists[i] = _haversine(p1[1], p1[0], p2[1], p2[0])
        except Exception:
            c = valid_geoms[j].centroid
            q = valid_points[j]
            dists[i] = _haversine(q.y, q.x, c.y, c.x)

    return dists


def _haversine(lat1, lng1, lat2, lng2):
    """Distance in meters between two lat/lng points."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class OSMBuildFeatures:
    """Feature builder with batched STRtree computation."""

    def __init__(self, layers: dict):
        self.trees = {}
        self.layer_geoms = {}

        for name in ["vias", "parques", "agua", "equipamiento"]:
            tree, geoms = _build_strtree(layers[name])
            self.trees[name] = tree
            self.layer_geoms[name] = geoms
            logger.info("  %s: %d geometrias", name, len(geoms))

        # Separate trees for hospital and university
        h_tree, h_geoms = _build_strtree(layers["equipamiento"], "amenity", "hospital")
        u_tree, u_geoms = _build_strtree(layers["equipamiento"], "amenity", "university")
        self.trees["hospital"] = h_tree
        self.layer_geoms["hospital"] = h_geoms
        self.trees["university"] = u_tree
        self.layer_geoms["university"] = u_geoms
        logger.info("  hospital: %d, universidad: %d", len(h_geoms), len(u_geoms))

        self.layer_keys = ["vias", "parques", "agua", "hospital", "university"]

    def compute(self, cells: list[tuple[str, float, float]],
                frente_threshold: float, agua_threshold: float):
        """Compute distance features for a batch of (h3, lat, lng) cells."""
        n = len(cells)
        h3_indices = [c[0] for c in cells]
        points = np.array([Point(c[2], c[1]) for c in cells])  # Point(lng, lat)

        dist_arrays = {}
        for layer_key in self.layer_keys:
            tree = self.trees[layer_key]
            geoms = self.layer_geoms[layer_key]
            dist_arrays[layer_key] = _batch_distance_m(tree, geoms, points)

        # Build result rows
        rows = []
        for i in range(n):
            via_d = dist_arrays["vias"][i]
            agua_d = dist_arrays["agua"][i]
            rows.append((
                h3_indices[i],
                float(dist_arrays["vias"][i]) if not np.isnan(dist_arrays["vias"][i]) else None,
                float(dist_arrays["parques"][i]) if not np.isnan(dist_arrays["parques"][i]) else None,
                float(agua_d) if not np.isnan(agua_d) else None,
                float(dist_arrays["hospital"][i]) if not np.isnan(dist_arrays["hospital"][i]) else None,
                float(dist_arrays["university"][i]) if not np.isnan(dist_arrays["university"][i]) else None,
                bool(not np.isnan(via_d) and via_d < frente_threshold),
                bool(not np.isnan(agua_d) and agua_d < agua_threshold),
            ))
        return rows


async def build_features(pbf_path: str, dsn: str, frente_threshold: float,
                         agua_threshold: float, limit: int = 0, batch_size: int = 100000):
    """Main pipeline: load OSM → build STRtrees → batch KNN → upsert PG."""

    # 1. Load OSM layers
    pbf_path_obj = Path(pbf_path)
    cache_path = pbf_path_obj.parent / f"{pbf_path_obj.stem}_layers.json"

    if cache_path.exists():
        logger.info("Cargando capas desde cache: %s", cache_path.name)
        with open(cache_path, "r", encoding="utf-8") as f:
            layers = json.load(f)
    else:
        from src.osm.extract import extract_layers
        logger.info("Extrayendo capas OSM ...")
        layers = extract_layers(pbf_path)

    # 2. Build STRtrees
    logger.info("Construyendo STRtrees ...")
    t0 = time.monotonic()
    builder = OSMBuildFeatures(layers)
    logger.info("STRtrees construidos en %.1fs", time.monotonic() - t0)

    # 3. Collect unique H3 cells from population_cells
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(CREATE_TABLE_SQL)

        logger.info("Recolectando celdas H3 unicas ...")
        t0 = time.monotonic()

        seen_h3 = {}
        offset = 0
        read_batch = 100000

        while True:
            query = f"""
                SELECT ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng
                FROM population_cells
                ORDER BY id
                LIMIT {read_batch} OFFSET {offset}
            """
            rows = await conn.fetch(query)
            if not rows:
                break

            for row in rows:
                lat, lng = row["lat"], row["lng"]
                hidx = h3.latlng_to_cell(lat, lng, 9)
                if hidx not in seen_h3:
                    seen_h3[hidx] = (lat, lng)

            offset += len(rows)
            logger.info("  leidos %d, H3 unicos: %d", offset, len(seen_h3))

            if limit and len(seen_h3) >= limit:
                break

        unique_cells = [(h, lat, lng) for h, (lat, lng) in seen_h3.items()]
        if limit:
            unique_cells = unique_cells[:limit]

        logger.info("Celdas H3 unicas: %d (%.1fs)", len(unique_cells),
                    time.monotonic() - t0)

        # 4. Compute features in batches
        logger.info("Computando features ...")
        t0 = time.monotonic()
        total = len(unique_cells)

        for i in range(0, total, batch_size):
            chunk = unique_cells[i : i + batch_size]
            rows = builder.compute(chunk, frente_threshold, agua_threshold)

            # Upsert to DB
            async with conn.transaction():
                await conn.executemany("""
                    INSERT INTO osm_features_r9
                        (h3, dist_via_principal_m, dist_parque_m, dist_agua_m,
                         dist_hospital_m, dist_universidad_m,
                         frente_avenida, cerca_agua, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                    ON CONFLICT (h3) DO UPDATE SET
                        dist_via_principal_m = EXCLUDED.dist_via_principal_m,
                        dist_parque_m = EXCLUDED.dist_parque_m,
                        dist_agua_m = EXCLUDED.dist_agua_m,
                        dist_hospital_m = EXCLUDED.dist_hospital_m,
                        dist_universidad_m = EXCLUDED.dist_universidad_m,
                        frente_avenida = EXCLUDED.frente_avenida,
                        cerca_agua = EXCLUDED.cerca_agua,
                        updated_at = NOW()
                """, rows)

            processed = min(i + batch_size, total)
            elapsed = time.monotonic() - t0
            rate = processed / elapsed if elapsed > 0 else 0
            logger.info("  %d/%d (%.1f%%) %.0f cel/s",
                        processed, total, processed * 100 / total, rate)

        elapsed = time.monotonic() - t0
        logger.info("Completado: %d celdas en %.1fs (%.0f cel/s)",
                    total, elapsed, total / elapsed if elapsed > 0 else 0)
    finally:
        await conn.close()


async def main():
    parser = argparse.ArgumentParser(description="Construye features OSM por celda H3")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Limitar a N celdas")
    parser.add_argument("--pbf", help="Ruta al PBF")
    parser.add_argument("--batch", type=int, default=100000, help="Batch size")
    args = parser.parse_args()

    setup_logging()
    config = load_config("config.yaml")

    dsn = os.environ.get("PG_DSN", "")
    if not dsn:
        logger.error("PG_DSN no configurado.")
        return

    pbf_dir = Path(config.osm.data_dir)
    if args.pbf:
        pbf_path = args.pbf
    else:
        pbfs = sorted(pbf_dir.glob("paraguay-*.osm.pbf"), reverse=True)
        if not pbfs:
            logger.error("No se encontro PBF.")
            return
        pbf_path = str(pbfs[0])

    if args.dry_run:
        conn = await asyncpg.connect(dsn)
        try:
            cnt = await conn.fetchval("SELECT COUNT(*) FROM population_cells")
        finally:
            await conn.close()
        logger.info("[dry-run] Se procesarian %d celdas", args.limit or cnt)
        return

    await build_features(
        pbf_path, dsn,
        frente_threshold=config.osm.frente_avenida_threshold_m,
        agua_threshold=config.osm.cerca_agua_threshold_m,
        limit=args.limit,
        batch_size=args.batch,
    )


if __name__ == "__main__":
    asyncio.run(main())
