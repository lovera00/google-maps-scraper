#!/usr/bin/env python3
"""Repara registros sin categoria re-ejecutando las busquedas ya hechas.

Lee las tareas completadas de scraping_tasks, re-ejecuta cada busqueda
con el parser actual (que extrae categorias al 100%), y actualiza los
registros matcheando por google_place_id o source_url.

Como cada busqueda devuelve ~120 resultados, es ~500x mas eficiente que
visitar paginas individuales.

Uso:
    python scripts/repair_categories.py --limit 50     # Solo 50 celdas
    python scripts/repair_categories.py --dry-run      # Ver sin modificar
    python scripts/repair_categories.py                # Reparar todo
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite

from src.config.loader import load_config
from src.models.query_task import QueryTask
from src.models.grid import GridCell


async def get_completed_tasks(db_path: str, limit: int = 0) -> list:
    """Obtiene tareas completadas de scraping_tasks."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        query = """SELECT DISTINCT grid_cell_json, category
                   FROM scraping_tasks
                   WHERE status = 'completed'
                   ORDER BY grid_cell_json, category"""
        if limit:
            query += f" LIMIT {limit}"
        cursor = await db.execute(query)
        tasks = []
        for r in await cursor.fetchall():
            tasks.append({
                "grid_cell_json": r["grid_cell_json"],
                "category": r["category"],
            })
        return tasks


async def count_missing_categories(db_path: str) -> int:
    """Cuenta registros sin categoria."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """SELECT COUNT(*) FROM businesses
               WHERE is_active = 1
                 AND (category = '' OR category = 'Sin categoria')"""
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def update_categories(db_path: str, updates: list[tuple[str, str]], dry: bool = False):
    """Actualiza categorias en la BD. updates = [(source_url, category), ...]."""
    if not updates:
        return 0

    if dry:
        for url, cat in updates[:5]:
            print(f"  [DRY] UPDATE source_url={url[:60]}... SET category='{cat}'")
        if len(updates) > 5:
            print(f"  [DRY] ... y {len(updates) - 5} mas")
        return len(updates)

    updated = 0
    async with aiosqlite.connect(db_path) as db:
        for source_url, category in updates:
            cursor = await db.execute(
                """UPDATE businesses
                   SET category = ?, updated_at = datetime('now')
                   WHERE source_url = ? AND is_active = 1
                     AND (category = '' OR category = 'Sin categoria')""",
                (category, source_url),
            )
            updated += cursor.rowcount
        await db.commit()
    return updated


async def repair(config, db_path: str, limit: int = 0, dry_run: bool = False):
    """Proceso principal: re-ejecuta busquedas completadas y actualiza categorias."""
    from src.agents.data_collector import DataCollector

    missing_before = await count_missing_categories(db_path)
    print(f"Registros sin categoria ANTES: {missing_before}")

    tasks_data = await get_completed_tasks(db_path, limit)
    if not tasks_data:
        print("No hay tareas completadas en scraping_tasks. Corre el pipeline primero.")
        return

    print(f"Tareas a re-ejecutar: {len(tasks_data)}")

    collector = DataCollector(config)
    await collector.setup()

    total_found = 0
    total_updated = 0

    try:
        for i, td in enumerate(tasks_data):
            # Reconstruir GridCell desde JSON
            import json
            grid = GridCell.from_dict(json.loads(td["grid_cell_json"]))
            task = QueryTask(grid_cell=grid, category=td["category"], depth=0)

            results = await collector.scrape(task)
            total_found += len(results)

            if not results:
                continue

            # Construir mapeo source_url -> category de los resultados
            url_to_cat = {}
            for r in results:
                url = r.get("source_url", "")
                cat = r.get("category", "")
                if url and cat:
                    url_to_cat[url] = cat

            if url_to_cat:
                updates = [(url, cat) for url, cat in url_to_cat.items()]
                updated = await update_categories(db_path, updates, dry=dry_run)
                total_updated += updated

            # Progreso cada 10 tareas
            if (i + 1) % 10 == 0 or i == len(tasks_data) - 1:
                pct = (i + 1) / len(tasks_data) * 100
                print(f"\r  Tareas: {i + 1}/{len(tasks_data)} ({pct:.0f}%) | "
                      f"Encontrados: {total_found} | Actualizados: {total_updated}",
                      end="", flush=True)

    finally:
        await collector.teardown()

    missing_after = await count_missing_categories(db_path)
    print(f"\n\nResultado:")
    print(f"  Registros sin categoria ANTES:  {missing_before}")
    print(f"  Registros sin categoria AHORA:  {missing_after}")
    print(f"  Reparados:                      {missing_before - missing_after}")
    print(f"  Resultados de busqueda totales: {total_found}")


def main():
    parser = argparse.ArgumentParser(description="Reparar categorias faltantes")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max tareas a re-ejecutar (0=todas)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo mostrar, no modificar BD")
    parser.add_argument("--db", default="data/paraguay_businesses.db")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    config.headless = True
    config.test_mode = False

    asyncio.run(repair(config, args.db, args.limit, args.dry_run))


if __name__ == "__main__":
    main()
