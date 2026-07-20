#!/usr/bin/env python3
"""Construye public.terreno_features: dataset de entrenamiento para el modelo
que predice precio USD/m2 de un terreno dado su punto en el mapa.

Toma los terrenos LIMPIOS de public.inmuebles y les cruza contexto espacial:
    - OSM (via H3 r9): dist a via principal, parque, agua, hospital, universidad;
      frente_avenida, cerca_agua                       [osm_features_r9]
    - Poblacion en radios de 500m/1km/2km              [population_cells, censo 2020]
    - Comercios en radios de 500m/1km/2km              [businesses, Google Maps]
    - Distancia a Asuncion (centro)
    - lat/lng crudos

Filtros de limpieza (para un modelo espacial):
    - con geo valida (lat/lng no nulos ni 0,0)
    - precio_usd > 0 y superficie_m2 >= 30
    - EXCLUYE office-pins: coordenadas compartidas por >=10 listados (oficina o
      centroide de ciudad, no la ubicacion real del lote)
    - recorta outliers de USD/m2 fuera del [p1, p99]

Requisitos: h3, asyncpg, PostGIS. Crea indices geography one-time.

Uso:  python scripts/build_terreno_features.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import asyncpg
import h3

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASU_LAT, ASU_LNG = -25.2637, -57.5759  # centro de Asuncion
PIN_THRESHOLD = 10                      # coords con >= listados = office-pin

DDL_INDICES = """
CREATE INDEX IF NOT EXISTS ix_popcells_geog ON public.population_cells USING gist ((geom::geography));
CREATE INDEX IF NOT EXISTS ix_biz_geog ON public.businesses USING gist ((geom::geography));
"""

DDL_TABLA = """
DROP TABLE IF EXISTS public.terreno_features;
CREATE TABLE public.terreno_features (
    id               BIGINT PRIMARY KEY,
    fuente           TEXT,
    id_externo       TEXT,
    lat              DOUBLE PRECISION,
    lng              DOUBLE PRECISION,
    geog             geography(Point,4326),
    h3_r9            TEXT,
    departamento_norm TEXT,
    ciudad_norm      TEXT,
    superficie_m2    NUMERIC,
    precio_usd       NUMERIC,
    usd_m2           NUMERIC,          -- TARGET
    -- features OSM (via H3)
    dist_via_principal_m DOUBLE PRECISION,
    dist_parque_m    DOUBLE PRECISION,
    dist_agua_m      DOUBLE PRECISION,
    dist_hospital_m  DOUBLE PRECISION,
    dist_universidad_m DOUBLE PRECISION,
    frente_avenida   BOOLEAN,
    cerca_agua       BOOLEAN,
    -- poblacion
    pop_500m         DOUBLE PRECISION,
    pop_1km          DOUBLE PRECISION,
    pop_2km          DOUBLE PRECISION,
    -- comercios
    biz_500m         INTEGER,
    biz_1km          INTEGER,
    biz_2km          INTEGER,
    -- distancia a Asuncion
    dist_asuncion_km DOUBLE PRECISION
);
"""

SQL_BASE = f"""
INSERT INTO public.terreno_features
   (id, fuente, id_externo, lat, lng, geog, departamento_norm, ciudad_norm,
    superficie_m2, precio_usd, usd_m2)
SELECT i.id, i.fuente, i.id_externo, i.lat, i.lng,
       ST_SetSRID(ST_MakePoint(i.lng, i.lat), 4326)::geography,
       i.departamento_norm, i.ciudad_norm, i.superficie_m2, i.precio_usd,
       round(i.precio_usd / i.superficie_m2, 2)
FROM public.inmuebles i
LEFT JOIN (
    SELECT lat, lng FROM public.inmuebles
    WHERE lat IS NOT NULL AND NOT (lat=0 AND lng=0)
    GROUP BY lat, lng HAVING count(*) >= {PIN_THRESHOLD}
) pins ON pins.lat = i.lat AND pins.lng = i.lng
WHERE i.lat IS NOT NULL AND NOT (i.lat=0 AND i.lng=0)
  AND i.precio_usd > 0 AND i.superficie_m2 >= 30
  AND pins.lat IS NULL;
"""

SQL_OSM = """
UPDATE public.terreno_features t SET
    dist_via_principal_m = o.dist_via_principal_m,
    dist_parque_m        = o.dist_parque_m,
    dist_agua_m          = o.dist_agua_m,
    dist_hospital_m      = o.dist_hospital_m,
    dist_universidad_m   = o.dist_universidad_m,
    frente_avenida       = o.frente_avenida,
    cerca_agua           = o.cerca_agua
FROM public.osm_features_r9 o WHERE o.h3 = t.h3_r9;
"""

SQL_POP = """
WITH agg AS (
  SELECT t.id,
    sum(p.population) FILTER (WHERE ST_DWithin(p.geom::geography, t.geog, 500))  pop_500m,
    sum(p.population) FILTER (WHERE ST_DWithin(p.geom::geography, t.geog, 1000)) pop_1km,
    sum(p.population) pop_2km
  FROM public.terreno_features t
  JOIN public.population_cells p ON ST_DWithin(p.geom::geography, t.geog, 2000)
  GROUP BY t.id)
UPDATE public.terreno_features t SET
    pop_500m = COALESCE(agg.pop_500m, 0),
    pop_1km  = COALESCE(agg.pop_1km, 0),
    pop_2km  = COALESCE(agg.pop_2km, 0)
FROM agg WHERE agg.id = t.id;
"""

SQL_BIZ = """
WITH agg AS (
  SELECT t.id,
    count(*) FILTER (WHERE ST_DWithin(b.geom::geography, t.geog, 500))  biz_500m,
    count(*) FILTER (WHERE ST_DWithin(b.geom::geography, t.geog, 1000)) biz_1km,
    count(*) biz_2km
  FROM public.terreno_features t
  JOIN public.businesses b ON b.is_active AND ST_DWithin(b.geom::geography, t.geog, 2000)
  GROUP BY t.id)
UPDATE public.terreno_features t SET
    biz_500m = COALESCE(agg.biz_500m, 0),
    biz_1km  = COALESCE(agg.biz_1km, 0),
    biz_2km  = COALESCE(agg.biz_2km, 0)
FROM agg WHERE agg.id = t.id;
"""

SQL_ASU = f"""
UPDATE public.terreno_features SET
    dist_asuncion_km = round((ST_Distance(
        geog, ST_SetSRID(ST_MakePoint({ASU_LNG}, {ASU_LAT}), 4326)::geography) / 1000)::numeric, 2),
    biz_500m = COALESCE(biz_500m, 0), biz_1km = COALESCE(biz_1km, 0), biz_2km = COALESCE(biz_2km, 0);
"""

SQL_TRIM = """
DELETE FROM public.terreno_features WHERE usd_m2 < (
    SELECT percentile_cont(0.01) WITHIN GROUP (ORDER BY usd_m2) FROM public.terreno_features)
  OR usd_m2 > (
    SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY usd_m2) FROM public.terreno_features);
"""


def load_env():
    env_path = PROJECT_ROOT / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def connect_retry(dsn, intentos=10):
    for i in range(1, intentos + 1):
        try:
            return await asyncpg.connect(dsn, timeout=20)
        except (OSError, asyncpg.PostgresError) as e:
            if i == intentos:
                raise
            await asyncio.sleep(min(2 ** i, 10))


async def step(conn, nombre, sql):
    t0 = time.monotonic()
    await conn.execute(sql)
    print(f"  {nombre}: {time.monotonic() - t0:.1f}s")


async def main():
    load_env()
    dsn = os.environ.get("PG_DSN", "")
    if not dsn:
        sys.exit("ERROR: PG_DSN no configurado.")
    conn = await connect_retry(dsn)
    try:
        print("Indices geography (one-time, puede tardar en la 1a corrida)...")
        await step(conn, "indices", DDL_INDICES)

        print("Tabla base + set limpio...")
        await conn.execute(DDL_TABLA)
        await step(conn, "insert base", SQL_BASE)
        n = await conn.fetchval("SELECT count(*) FROM public.terreno_features")
        print(f"  terrenos limpios: {n}")

        print("H3 r9 por terreno (Python)...")
        t0 = time.monotonic()
        rows = await conn.fetch("SELECT id, lat, lng FROM public.terreno_features")
        h3vals = [(h3.latlng_to_cell(r["lat"], r["lng"], 9), r["id"]) for r in rows]
        await conn.executemany("UPDATE public.terreno_features SET h3_r9=$1 WHERE id=$2", h3vals)
        print(f"  h3: {time.monotonic() - t0:.1f}s")

        print("Cruces espaciales...")
        await step(conn, "OSM (H3)", SQL_OSM)
        await step(conn, "poblacion (radios)", SQL_POP)
        await step(conn, "comercios (radios)", SQL_BIZ)
        await step(conn, "dist Asuncion", SQL_ASU)

        print("Recorte de outliers de USD/m2 [p1,p99]...")
        antes = await conn.fetchval("SELECT count(*) FROM public.terreno_features")
        await conn.execute(SQL_TRIM)
        final = await conn.fetchval("SELECT count(*) FROM public.terreno_features")
        print(f"  descartados {antes - final}, quedan {final}")

        r = await conn.fetchrow("""
            SELECT round(min(usd_m2),1) min, round(percentile_cont(0.5) WITHIN GROUP (ORDER BY usd_m2)::numeric,1) mediana,
                   round(max(usd_m2),1) max,
                   count(*) FILTER (WHERE dist_via_principal_m IS NULL) sin_osm,
                   round(avg(pop_1km)) pop1km_prom, round(avg(biz_1km),1) biz1km_prom
            FROM public.terreno_features""")
        print(f"\nRESUMEN terreno_features: {final} filas listas para ML")
        print(f"  target USD/m2 -> min {r['min']}  mediana {r['mediana']}  max {r['max']}")
        print(f"  sin match OSM: {r['sin_osm']} | pop_1km prom {r['pop1km_prom']} | biz_1km prom {r['biz1km_prom']}")
    finally:
        await conn.close()
    print("Listo.")


if __name__ == "__main__":
    asyncio.run(main())
