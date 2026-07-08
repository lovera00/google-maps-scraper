#!/usr/bin/env python3
"""Carga datos de poblacion desde CSV a PostgreSQL (population_cells).

Inserta directo con executemany, ~36 chunks de 100k filas.

Uso:
    python scripts/load_population.py --csv pry_general_2020_csv/pry_general_2020.csv
    python scripts/load_population.py --csv ... --replace   # Truncar y recargar
"""
import argparse
import asyncio
import csv
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.loader import _load_dotenv

CHUNK_SIZE = 100_000

INSERT_SQL = """
    INSERT INTO population_cells (population, geom)
    VALUES ($1, ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography)
"""


async def load_csv(dsn: str, csv_path: str, replace: bool = False) -> None:
    import asyncpg

    csv_abs = str(Path(csv_path).resolve())
    if not os.path.exists(csv_abs):
        print(f"ERROR: CSV no encontrado: {csv_abs}", file=sys.stderr)
        sys.exit(1)

    start = time.monotonic()
    conn = await asyncpg.connect(dsn)

    try:
        if replace:
            await conn.execute("TRUNCATE TABLE population_cells RESTART IDENTITY")
            print("Tabla population_cells truncada")

        total_read = 0
        total_skipped = 0
        total_inserted = 0
        chunk = []

        with open(csv_abs, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pop = float(row["pry_general_2020"])
                if pop <= 0:
                    total_skipped += 1
                    continue
                # (population, lng, lat) — ST_MakePoint(x=lng, y=lat)
                chunk.append((pop, float(row["longitude"]), float(row["latitude"])))
                total_read += 1

                if len(chunk) >= CHUNK_SIZE:
                    await conn.executemany(INSERT_SQL, chunk)
                    total_inserted += len(chunk)
                    chunk = []

            if chunk:
                await conn.executemany(INSERT_SQL, chunk)
                total_inserted += len(chunk)

        elapsed = time.monotonic() - start
        print(f"\nCarga completada en {elapsed:.1f}s")
        print(f"  Leidas del CSV:    {total_read:,}")
        if total_skipped > 0:
            print(f"  Saltadas (<= 0):   {total_skipped:,}")
        print(f"  Insertadas:        {total_inserted:,}")

        total = await conn.fetchval("SELECT COUNT(*) FROM population_cells")
        print(f"  Total en tabla:    {total:,}")

    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Cargar CSV de poblacion a PostgreSQL")
    parser.add_argument("--csv", required=True, help="Ruta al archivo CSV")
    parser.add_argument("--replace", action="store_true",
                        help="Truncar tabla antes de cargar")
    parser.add_argument("--dsn", help="PostgreSQL DSN (por defecto usa PG_DSN del entorno)")
    args = parser.parse_args()

    _load_dotenv()
    dsn = args.dsn or os.environ.get("PG_DSN", "")
    if not dsn:
        print("ERROR: PG_DSN no configurado. Usa --dsn o setea PG_DSN en .env", file=sys.stderr)
        sys.exit(1)

    asyncio.run(load_csv(dsn, args.csv, args.replace))


if __name__ == "__main__":
    main()
