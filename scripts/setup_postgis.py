#!/usr/bin/env python3
"""Configura PostGIS en PostgreSQL para el analisis geoespacial de negocios.

Idempotente: se puede ejecutar multiples veces sin errores.

Acciones:
1. CREATE EXTENSION IF NOT EXISTS postgis
2. Agrega columna geography(Point, 4326) a businesses (si no existe)
3. Crea indice GIST sobre la columna geography (si no existe)
4. Crea tabla population_cells con indice GIST (si no existe)
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.loader import _load_dotenv

GEOM_COLUMN_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'businesses' AND column_name = 'geom'
    ) THEN
        ALTER TABLE businesses
        ADD COLUMN geom geography(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography) STORED;
        RAISE NOTICE 'Columna geom agregada a businesses';
    ELSE
        RAISE NOTICE 'Columna geom ya existe en businesses';
    END IF;
END $$;
"""

GIST_INDEX_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'businesses' AND indexname = 'idx_businesses_geom'
    ) THEN
        CREATE INDEX idx_businesses_geom ON businesses USING GIST (geom);
        RAISE NOTICE 'Indice GIST creado en businesses.geom';
    ELSE
        RAISE NOTICE 'Indice GIST ya existe en businesses.geom';
    END IF;
END $$;
"""

POPULATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS population_cells (
    id          BIGSERIAL PRIMARY KEY,
    population  REAL NOT NULL,
    geom        geography(Point, 4326) NOT NULL
);
"""

POPULATION_GIST_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'population_cells' AND indexname = 'idx_population_cells_geom'
    ) THEN
        CREATE INDEX idx_population_cells_geom ON population_cells USING GIST (geom);
        RAISE NOTICE 'Indice GIST creado en population_cells.geom';
    ELSE
        RAISE NOTICE 'Indice GIST ya existe en population_cells.geom';
    END IF;
END $$;
"""


async def setup(dsn: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        # 1. PostGIS extension
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            print("Extension PostGIS habilitada")
        except Exception as e:
            print(f"ERROR: No se pudo habilitar PostGIS: {e}", file=sys.stderr)
            print("Instalalo con: CREATE EXTENSION postgis; como superusuario", file=sys.stderr)
            raise SystemExit(1) from e

        # 2. Columna geography en businesses
        await conn.execute(GEOM_COLUMN_SQL)
        print("Columna businesses.geom verificada")

        # 3. Indice GIST en businesses.geom
        await conn.execute(GIST_INDEX_SQL)
        print("Indice businesses.geom verificado")

        # 4. Tabla population_cells
        await conn.execute(POPULATION_TABLE_SQL)
        print("Tabla population_cells verificada")

        # 5. Indice GIST en population_cells.geom
        await conn.execute(POPULATION_GIST_SQL)
        print("Indice population_cells.geom verificado")

        print("\nSetup completado exitosamente.")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Configurar PostGIS para el proyecto")
    parser.add_argument("--dsn", help="PostgreSQL DSN (por defecto usa PG_DSN del entorno)")
    args = parser.parse_args()

    _load_dotenv()
    dsn = args.dsn or os.environ.get("PG_DSN", "")
    if not dsn:
        print("ERROR: PG_DSN no configurado. Usa --dsn o setea PG_DSN en .env", file=sys.stderr)
        sys.exit(1)

    asyncio.run(setup(dsn))


if __name__ == "__main__":
    main()
