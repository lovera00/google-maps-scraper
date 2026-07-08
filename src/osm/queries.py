import asyncpg
import logging

logger = logging.getLogger(__name__)


async def nearest_road(lat: float, lng: float, dsn: str) -> dict:
    """Find the nearest named road to a point.

    Returns the closest road with a non-empty name. If the closest road
    has no name, looks for the closest named road within 1 km.
    """
    conn = await asyncpg.connect(dsn)
    try:
        point = f"ST_SetSRID(ST_MakePoint({lng}, {lat}), 4326)"

        # First: absolute nearest road (with or without name)
        row = await conn.fetchrow(f"""
            SELECT name, highway,
                   ST_Distance(geom::geography, {point}::geography) AS distancia_m
            FROM osm_vias
            ORDER BY geom::geography <-> {point}::geography
            LIMIT 1
        """)

        if row is None:
            return {"error": "no hay vias cargadas en osm_vias"}

        if row["name"]:
            return {
                "via": row["name"],
                "tipo": row["highway"],
                "distancia_m": round(row["distancia_m"], 1),
            }

        # Closest road has no name — search for nearest named road within 1 km
        row_named = await conn.fetchrow(f"""
            SELECT name, highway,
                   ST_Distance(geom::geography, {point}::geography) AS distancia_m
            FROM osm_vias
            WHERE name IS NOT NULL AND name != ''
              AND ST_DWithin(geom::geography, {point}::geography, 1000)
            ORDER BY geom::geography <-> {point}::geography
            LIMIT 1
        """)

        if row_named:
            return {
                "via": row_named["name"],
                "tipo": row_named["highway"],
                "distancia_m": round(row_named["distancia_m"], 1),
            }

        # Fallback: return the unnamed road
        return {
            "via": f"Via sin nombre ({row['highway']})",
            "tipo": row["highway"],
            "distancia_m": round(row["distancia_m"], 1),
        }
    finally:
        await conn.close()


async def locate_point(lat: float, lng: float, dsn: str) -> dict:
    """Locate a point within OSM boundaries.

    Returns neighbourhood/suburb and city by point-in-polygon lookup
    against osm_limites. Falls back to nearest place point within 2 km.
    """
    conn = await asyncpg.connect(dsn)
    try:
        point = f"ST_SetSRID(ST_MakePoint({lng}, {lat}), 4326)"

        # Barrio: place IN (suburb, neighbourhood) as polygon
        barrio_row = await conn.fetchrow(f"""
            SELECT name, place
            FROM osm_limites
            WHERE place IN ('suburb', 'neighbourhood')
              AND ST_GeometryType(geom) = 'ST_Polygon'
              AND ST_Contains(geom, {point})
            LIMIT 1
        """)

        # Ciudad: admin_level IN (8,9) or place IN (city, town) as polygon
        ciudad_row = await conn.fetchrow(f"""
            SELECT name, admin_level, place,
                   ST_GeometryType(geom) as geom_type
            FROM osm_limites
            WHERE (
                admin_level IN ('8', '9')
                OR place IN ('city', 'town')
            )
              AND ST_GeometryType(geom) = 'ST_Polygon'
              AND ST_Contains(geom, {point})
            ORDER BY
                CASE admin_level WHEN '8' THEN 1 WHEN '9' THEN 2 END,
                CASE place WHEN 'city' THEN 1 WHEN 'town' THEN 2 END
            LIMIT 1
        """)

        barrio = barrio_row["name"] if barrio_row else None
        ciudad = ciudad_row["name"] if ciudad_row else None
        aproximado = False

        # Fallback: nearest place point within 2 km
        if ciudad is None:
            fallback = await conn.fetchrow(f"""
                SELECT name, place, admin_level,
                       ST_Distance(geom::geography, {point}::geography) AS distancia_m
                FROM osm_limites
                WHERE (admin_level IN ('8', '9') OR place IN ('city', 'town'))
                  AND ST_DWithin(geom::geography, {point}::geography, 2000)
                ORDER BY geom::geography <-> {point}::geography
                LIMIT 1
            """)
            if fallback:
                ciudad = fallback["name"]
                aproximado = True

        return {
            "barrio": barrio,
            "ciudad": ciudad,
            "aproximado": aproximado,
        }
    finally:
        await conn.close()
