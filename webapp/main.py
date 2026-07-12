"""Webapp local para analisis de radio sobre datos scrapeados.

FastAPI + PostgreSQL/PostGIS. Solo lectura.
"""
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg
import h3
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from src.config.loader import _load_dotenv

logger = logging.getLogger("webapp")

# ── Bbox amplio de Paraguay para validacion ──────────────────────
PY_BBOX = {
    "lat_min": -28.0,
    "lat_max": -19.0,
    "lng_min": -63.0,
    "lng_max": -54.0,
}

_pool: Optional[asyncpg.Pool] = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise HTTPException(status_code=503, detail="Pool no inicializado")
    return _pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    # .env del proyecto, independiente del directorio de trabajo
    _load_dotenv(Path(__file__).parent.parent / ".env")
    dsn = os.environ.get("PG_DSN", "")
    if not dsn:
        print("ADVERTENCIA: PG_DSN no configurado. La API no funcionara sin BD.")
    else:
        _pool = await asyncpg.create_pool(
            dsn,
            min_size=2,
            max_size=10,
            command_timeout=10,
        )
        print("Pool PostgreSQL creado")
    yield
    if _pool:
        await _pool.close()
        print("Pool PostgreSQL cerrado")


app = FastAPI(
    title="Analisis de Radio - Paraguay",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/api/stats")
async def stats(
    lat: float = Query(..., ge=-90, le=90, description="Latitud"),
    lng: float = Query(..., ge=-180, le=180, description="Longitud"),
    radius_km: float = Query(..., ge=0.1, le=100, description="Radio en km"),
):
    """Consulta de radio: poblacion, comercios y desglose por rubro."""
    # Validar bbox Paraguay
    if not (PY_BBOX["lat_min"] <= lat <= PY_BBOX["lat_max"]):
        raise HTTPException(
            status_code=400,
            detail=f"Latitud {lat} fuera del rango de Paraguay "
                   f"({PY_BBOX['lat_min']} a {PY_BBOX['lat_max']})",
        )
    if not (PY_BBOX["lng_min"] <= lng <= PY_BBOX["lng_max"]):
        raise HTTPException(
            status_code=400,
            detail=f"Longitud {lng} fuera del rango de Paraguay "
                   f"({PY_BBOX['lng_min']} a {PY_BBOX['lng_max']})",
        )

    pool = get_pool()
    radius_m = radius_km * 1000

    async with pool.acquire() as conn:
        pop_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(population), 0)::REAL AS total_population
            FROM population_cells
            WHERE ST_DWithin(
                geom,
                ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
                $3
            )
            """,
            lng, lat, radius_m,
        )

        biz_total = await conn.fetchrow(
            """
            SELECT COUNT(*) AS total
            FROM businesses
            WHERE is_active = TRUE
              AND geom IS NOT NULL
              AND ST_DWithin(
                  geom,
                  ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
                  $3
              )
            """,
            lng, lat, radius_m,
        )

        by_cat = await conn.fetch(
            """
            SELECT COALESCE(business_category, 'Sin clasificar') AS category,
                   COUNT(*) AS count
            FROM businesses
            WHERE is_active = TRUE
              AND geom IS NOT NULL
              AND ST_DWithin(
                  geom,
                  ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
                  $3
              )
            GROUP BY 1
            ORDER BY count DESC
            """,
            lng, lat, radius_m,
        )

        # "Actividades principales de la zona": replica el filtrado del reporte de
        # la competencia -> radio FIJO de 2000 m (independiente del radius_km del
        # mapa), solo clasificaciones confiables (category_confidence >= 0.5) y
        # top 10 rubros. Nota: hoy el umbral 0.5 no descarta nada, porque la
        # confianza minima de una fila clasificada es 0.50; se deja por fidelidad
        # y para el caso de que se agreguen fuentes de menor confianza.
        by_type = await conn.fetch(
            """
            SELECT business_type AS type,
                   business_category AS category,
                   COUNT(*) AS count
            FROM businesses
            WHERE is_active = TRUE
              AND geom IS NOT NULL
              AND business_type IS NOT NULL
              AND category_confidence >= 0.5
              AND ST_DWithin(
                  geom,
                  ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
                  2000
              )
            GROUP BY 1, 2
            ORDER BY count DESC
            LIMIT 10
            """,
            lng, lat,
        )

        biz_list = await conn.fetch(
            """
            SELECT name, lat, lng,
                   business_type AS type,
                   COALESCE(business_category, 'Sin clasificar') AS category,
                   ST_Distance(
                       geom,
                       ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
                   ) AS distance
            FROM businesses
            WHERE is_active = TRUE
              AND geom IS NOT NULL
              AND ST_DWithin(
                  geom,
                  ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
                  $3
              )
            ORDER BY distance
            LIMIT 20000
            """,
            lng, lat, radius_m,
        )

    population = pop_row["total_population"] if pop_row else 0
    businesses_total = biz_total["total"] if biz_total else 0

    by_category = {row["category"]: row["count"] for row in by_cat}
    top_types = [
        {"type": r["type"], "category": r["category"], "count": r["count"]}
        for r in by_type
    ]

    businesses_per_1000 = 0.0
    if population > 0:
        businesses_per_1000 = round(businesses_total / population * 1000, 2)

    return {
        "center": {"lat": lat, "lng": lng},
        "radius_km": radius_km,
        "population": population,
        "businesses_total": businesses_total,
        "businesses_per_1000": businesses_per_1000,
        "by_category": by_category,
        "top_types": top_types,
        "businesses": [
            {
                "name": r["name"],
                "lat": r["lat"],
                "lng": r["lng"],
                "type": r["type"],
                "category": r["category"],
                "distance": round(r["distance"], 2),
            }
            for r in biz_list
        ],
    }


@app.get("/api/osm")
async def osm_info(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
):
    """Informacion OSM para un punto: via mas cercana, barrio/ciudad, features H3."""
    if not (PY_BBOX["lat_min"] <= lat <= PY_BBOX["lat_max"]):
        raise HTTPException(status_code=400, detail="Latitud fuera de Paraguay")
    if not (PY_BBOX["lng_min"] <= lng <= PY_BBOX["lng_max"]):
        raise HTTPException(status_code=400, detail="Longitud fuera de Paraguay")

    pool = get_pool()
    async with pool.acquire() as conn:
        # ── nearest road ──
        road = await conn.fetchrow(
            """
            SELECT name, highway,
                   ST_Distance(geom::geography,
                       ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography) AS distancia_m
            FROM osm_vias
            ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
            LIMIT 1
            """, lng, lat,
        )
        nearest_road = None
        if road and road["name"]:
            nearest_road = {
                "via": road["name"],
                "tipo": road["highway"],
                "distancia_m": round(road["distancia_m"], 1),
            }
        elif road:
            # Buscar la via con nombre mas cercana en 1 km
            named = await conn.fetchrow(
                """
                SELECT name, highway,
                       ST_Distance(geom::geography,
                           ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography) AS distancia_m
                FROM osm_vias
                WHERE name IS NOT NULL AND name != ''
                  AND ST_DWithin(geom::geography,
                      ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography, 1000)
                ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
                LIMIT 1
                """, lng, lat,
            )
            if named:
                nearest_road = {
                    "via": named["name"],
                    "tipo": named["highway"],
                    "distancia_m": round(named["distancia_m"], 1),
                }
            else:
                nearest_road = {
                    "via": f"Via sin nombre ({road['highway']})",
                    "tipo": road["highway"],
                    "distancia_m": round(road["distancia_m"], 1),
                }

        # ── nearest via principal (motorway/trunk/primary/secondary/tertiary) ──
        via_principal = None
        try:
            principal_row = await conn.fetchrow(
                """
                SELECT name, highway,
                       ST_Distance(geom::geography,
                           ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography) AS distancia_m
                FROM osm_vias
                WHERE highway IN ('motorway', 'trunk', 'primary', 'secondary', 'tertiary')
                ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
                LIMIT 1
                """, lng, lat,
            )
            if principal_row and principal_row["name"]:
                via_principal = {
                    "nombre": principal_row["name"],
                    "tipo": principal_row["highway"],
                    "distancia_m": round(principal_row["distancia_m"], 1),
                }
            elif principal_row:
                # La via principal mas cercana no tiene nombre: buscar la via
                # principal con nombre mas cercana dentro de un radio mayor
                named_principal = await conn.fetchrow(
                    """
                    SELECT name, highway,
                           ST_Distance(geom::geography,
                               ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography) AS distancia_m
                    FROM osm_vias
                    WHERE highway IN ('motorway', 'trunk', 'primary', 'secondary', 'tertiary')
                      AND name IS NOT NULL AND name != ''
                      AND ST_DWithin(geom::geography,
                          ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography, 3000)
                    ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
                    LIMIT 1
                    """, lng, lat,
                )
                if named_principal:
                    via_principal = {
                        "nombre": named_principal["name"],
                        "tipo": named_principal["highway"],
                        "distancia_m": round(named_principal["distancia_m"], 1),
                    }
                else:
                    via_principal = {
                        "nombre": f"Vía principal sin nombre ({principal_row['highway']})",
                        "tipo": principal_row["highway"],
                        "distancia_m": round(principal_row["distancia_m"], 1),
                    }
        except Exception:
            logger.exception("Error buscando via principal para lat=%s lng=%s", lat, lng)

        # ── locate: barrio + ciudad ──
        point = f"ST_SetSRID(ST_MakePoint({lng}, {lat}), 4326)"
        barrio_row = await conn.fetchrow(
            f"""
            SELECT name FROM osm_limites
            WHERE place IN ('suburb', 'neighbourhood')
              AND ST_GeometryType(geom) = 'ST_Polygon'
              AND ST_Contains(geom, {point})
            LIMIT 1
            """
        )
        ciudad_row = await conn.fetchrow(
            f"""
            SELECT name FROM osm_limites
            WHERE (admin_level IN ('8', '9') OR place IN ('city', 'town'))
              AND ST_GeometryType(geom) = 'ST_Polygon'
              AND ST_Contains(geom, {point})
            ORDER BY CASE admin_level WHEN '8' THEN 1 WHEN '9' THEN 2 END,
                     CASE place WHEN 'city' THEN 1 WHEN 'town' THEN 2 END
            LIMIT 1
            """
        )
        barrio = barrio_row["name"] if barrio_row else None
        ciudad = ciudad_row["name"] if ciudad_row else None
        aproximado = False
        barrio_aproximado = False

        if ciudad is None:
            fallback = await conn.fetchrow(
                f"""
                SELECT name FROM osm_limites
                WHERE (admin_level IN ('8', '9') OR place IN ('city', 'town'))
                  AND ST_DWithin(geom::geography, {point}::geography, 2000)
                ORDER BY geom::geography <-> {point}::geography
                LIMIT 1
                """
            )
            if fallback:
                ciudad = fallback["name"]
                aproximado = True

        if barrio is None:
            try:
                barrio_fallback = await conn.fetchrow(
                    f"""
                    SELECT name FROM osm_limites
                    WHERE place IN ('suburb', 'neighbourhood')
                      AND ST_DWithin(geom::geography, {point}::geography, 3000)
                    ORDER BY geom::geography <-> {point}::geography
                    LIMIT 1
                    """
                )
                if barrio_fallback:
                    barrio = barrio_fallback["name"]
                    barrio_aproximado = True
            except Exception:
                logger.exception("Error buscando barrio aproximado para lat=%s lng=%s", lat, lng)

        # ── H3 features ──
        hidx = h3.latlng_to_cell(lat, lng, 9)
        h3_features = await conn.fetchrow(
            """
            SELECT dist_via_principal_m, dist_parque_m, dist_agua_m,
                   dist_hospital_m, dist_universidad_m,
                   frente_avenida, cerca_agua
            FROM osm_features_r9
            WHERE h3 = $1
            """, hidx,
        )

        # Red de seguridad: si la busqueda de via principal con nombre fallo
        # por completo, usar la distancia precalculada (sin nombre) que ya
        # existia antes en osm_features_r9.
        if via_principal is None and h3_features and h3_features["dist_via_principal_m"] is not None:
            via_principal = {
                "nombre": None,
                "tipo": None,
                "distancia_m": round(h3_features["dist_via_principal_m"], 1),
            }

    result = {
        "nearest_road": nearest_road,
        "via_principal": via_principal,
        "locate": {
            "barrio": barrio,
            "barrio_aproximado": barrio_aproximado,
            "ciudad": ciudad,
            "aproximado": aproximado,
        },
    }

    if h3_features:
        result["h3_cell"] = hidx
        result["osm_features"] = {
            "dist_via_principal_m": (round(h3_features["dist_via_principal_m"], 1)
                                     if h3_features["dist_via_principal_m"] is not None else None),
            "dist_parque_m": (round(h3_features["dist_parque_m"], 1)
                             if h3_features["dist_parque_m"] is not None else None),
            "dist_agua_m": (round(h3_features["dist_agua_m"], 1)
                           if h3_features["dist_agua_m"] is not None else None),
            "dist_hospital_m": (round(h3_features["dist_hospital_m"], 1)
                               if h3_features["dist_hospital_m"] is not None else None),
            "dist_universidad_m": (round(h3_features["dist_universidad_m"], 1)
                                  if h3_features["dist_universidad_m"] is not None else None),
            "frente_avenida": h3_features["frente_avenida"],
            "cerca_agua": h3_features["cerca_agua"],
        }

    return result


@app.get("/")
async def root():
    static_dir = Path(__file__).parent / "static"
    return FileResponse(static_dir / "index.html")


# Montar static files (por si hay CSS/JS adicional)
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
