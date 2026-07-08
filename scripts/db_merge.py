#!/usr/bin/env python3
"""Mergea multiples DBs SQLite de shards en una DB unificada con re-deduplicacion global.

Uso:
    python -m scripts.db_merge --inputs data/shard_*.db --output data/paraguay_businesses.db
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite

from src.db.connection import initialize_db
from src.db.schema import DDL
from src.models.business import NormalizedBusiness
from src.agents.deduplicator import Deduplicator
from src.config.loader import load_config


async def read_all_businesses(db_path: str) -> list[NormalizedBusiness]:
    businesses = []
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT name, lat, lng, category, address, phone, website, "
            "rating, review_count, source_url, google_place_id, raw_name, metadata "
            "FROM businesses WHERE is_active = 1"
        )
        rows = await cursor.fetchall()
        for row in rows:
            meta = row["metadata"]
            if meta and isinstance(meta, str):
                meta = json.loads(meta)
            businesses.append(NormalizedBusiness(
                name=row["name"],
                lat=row["lat"],
                lng=row["lng"],
                category=row["category"],
                address=row["address"],
                phone=row["phone"],
                website=row["website"],
                rating=row["rating"],
                review_count=row["review_count"],
                source_url=row["source_url"],
                google_place_id=row["google_place_id"],
                raw_name=row["raw_name"],
                metadata=meta,
            ))
    return businesses


async def insert_business(db: aiosqlite.Connection, b: NormalizedBusiness) -> bool:
    match_id = None

    # 1. google_place_id
    if b.google_place_id:
        cursor = await db.execute(
            "SELECT id FROM businesses WHERE google_place_id = ? AND is_active = 1",
            (b.google_place_id,),
        )
        row = await cursor.fetchone()
        if row:
            match_id = row[0]

    # 2. source_url
    if match_id is None and b.source_url:
        cursor = await db.execute(
            "SELECT id FROM businesses WHERE source_url = ? AND is_active = 1",
            (b.source_url,),
        )
        row = await cursor.fetchone()
        if row:
            match_id = row[0]

    # 3. nombre
    if match_id is None:
        cursor = await db.execute(
            "SELECT id FROM businesses WHERE is_active = 1 AND LOWER(name) = LOWER(?) LIMIT 1",
            (b.name,),
        )
        row = await cursor.fetchone()
        if row:
            match_id = row[0]

    if match_id is not None:
        await db.execute(
            """UPDATE businesses
               SET category = ?, address = ?, phone = ?, website = ?,
                   rating = ?, review_count = ?,
                   source_url = COALESCE(?, source_url),
                   google_place_id = COALESCE(?, google_place_id),
                   metadata = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (b.category, b.address, b.phone, b.website,
             b.rating, b.review_count, b.source_url,
             b.google_place_id,
             json.dumps(b.metadata or {}), match_id),
        )
        return False
    else:
        await db.execute(
            """INSERT INTO businesses
               (name, lat, lng, category, address, phone, website,
                rating, review_count, source_url, google_place_id,
                raw_name, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (b.name, b.lat, b.lng, b.category, b.address, b.phone,
             b.website, b.rating, b.review_count, b.source_url,
             b.google_place_id,
             b.raw_name, json.dumps(b.metadata or {})),
        )
        return True


async def merge_databases(input_paths: list[str], output_path: str, config_path: str):
    config = load_config(config_path)
    deduplicator = Deduplicator(config)

    all_businesses: list[NormalizedBusiness] = []

    for i, path in enumerate(input_paths):
        if not Path(path).exists():
            print(f"Advertencia: {path} no existe, saltando...")
            continue
        businesses = await read_all_businesses(path)
        print(f"  {path}: {len(businesses)} registros")
        all_businesses.extend(businesses)

    print(f"\nTotal antes de dedup: {len(all_businesses)}")

    if all_businesses:
        all_businesses = await deduplicator.deduplicate(all_businesses)
        print(f"Total despues de dedup: {len(all_businesses)}")

    await initialize_db(output_path, DDL)

    async with aiosqlite.connect(output_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        inserted = 0
        for b in all_businesses:
            is_new = await insert_business(db, b)
            if is_new:
                inserted += 1
        await db.commit()

    print(f"\n{inserted} nuevos registros insertados en {output_path}")

    # Exportar JSON
    output_json = Path(output_path).parent / "output.json"
    async with aiosqlite.connect(output_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT name, lat, lng, category FROM businesses WHERE is_active = 1"
        )
        rows = await cursor.fetchall()
        data = [{"name": r["name"], "lat": r["lat"], "lng": r["lng"], "category": r["category"]} for r in rows]
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{len(data)} registros exportados a {output_json}")


def main():
    parser = argparse.ArgumentParser(description="Mergear DBs de shards en una DB unificada")
    parser.add_argument("--inputs", nargs="+", required=True, help="Archivos .db a mergear")
    parser.add_argument("--output", default="data/paraguay_businesses.db", help="DB de salida")
    parser.add_argument("--config", default="config.yaml", help="Archivo de configuracion")
    args = parser.parse_args()

    asyncio.run(merge_databases(args.inputs, args.output, args.config))


if __name__ == "__main__":
    main()
