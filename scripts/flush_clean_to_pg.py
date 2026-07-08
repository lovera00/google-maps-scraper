#!/usr/bin/env python3
"""Vuelca registros limpios (con categoria) de SQLite a PostgreSQL via bulk INSERT.

Usa INSERT ON CONFLICT para ~100x velocidad vs upserts individuales.

Uso:
    python scripts/flush_clean_to_pg.py
    python scripts/flush_clean_to_pg.py --dry-run
"""
import argparse, asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite, asyncpg
from src.config.loader import load_config


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default="data/paraguay_businesses.db")
    args = parser.parse_args()

    load_config("config.yaml")
    dsn = os.environ.get("PG_DSN", "")
    if not dsn:
        print("ERROR: PG_DSN no esta configurado.")
        return

    # Leer registros limpios de SQLite
    async with aiosqlite.connect(args.db) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT name, lat, lng, category, search_category,
                      address, phone, website, rating, review_count,
                      source_url, google_place_id, raw_name, metadata
               FROM businesses
               WHERE is_active = 1
                 AND category IS NOT NULL
                 AND category != ''
                 AND category != 'Sin categoria'
                 AND search_category IS NOT NULL
                 AND search_category != ''"""
        )
        rows = await cursor.fetchall()

    total = len(rows)
    print(f"Registros limpios: {total}")

    if args.dry_run:
        cats = {}
        for r in rows: cats[r["category"]] = cats.get(r["category"], 0) + 1
        print("Top 10 categorias:")
        for cat, n in sorted(cats.items(), key=lambda x: -x[1])[:10]:
            print(f"  {cat}: {n}")
        return

    # Bulk INSERT a PostgreSQL
    pg = await asyncpg.connect(dsn)
    try:
        # Preparar datos
        values = []
        for r in rows:
            meta = r["metadata"]
            if meta:
                try: meta = json.loads(meta)
                except: meta = {}
            else:
                meta = {}
            values.append((
                r["name"], r["lat"], r["lng"], r["category"],
                r["search_category"] if "search_category" in r.keys() else "",
                r["address"], r["phone"], r["website"],
                r["rating"], r["review_count"],
                r["source_url"], r["google_place_id"],
                r["raw_name"], json.dumps(meta, ensure_ascii=False),
            ))

        # Insertar en batches de 500 para no reventar memoria
        batch_size = 500
        inserted = 0
        for i in range(0, len(values), batch_size):
            batch = values[i:i + batch_size]
            result = await pg.executemany(
                """INSERT INTO businesses
                   (name, lat, lng, category, search_category,
                    address, phone, website, rating, review_count,
                    source_url, google_place_id, raw_name, metadata)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb)
                   ON CONFLICT DO NOTHING""",
                batch,
            )

            # executemany returns None for each statement, count affected differently
            # Let's track approximate progress
            inserted += len(batch)
            pct = min(i + batch_size, len(values)) / len(values) * 100
            print(f"\r  Progreso: {min(i + batch_size, len(values))}/{len(values)} ({pct:.0f}%)",
                  end="", flush=True)

        # Contar total en PG
        count = await pg.fetchval("SELECT COUNT(*) FROM businesses")
        print(f"\n\nCompletado. PostgreSQL tiene {count} registros (de {total} enviados).")

    finally:
        await pg.close()


if __name__ == "__main__":
    asyncio.run(main())
