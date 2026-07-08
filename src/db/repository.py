import json
import aiosqlite
from pathlib import Path
from typing import List, Optional

from ..models.business import NormalizedBusiness
from ..utils.geo import haversine_distance


class Repository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def upsert_business(self, business: NormalizedBusiness) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")

            match_id = None

            # 1. Buscar por google_place_id (el ID canonico de Google Maps)
            if business.google_place_id:
                cursor = await db.execute(
                    "SELECT id FROM businesses WHERE google_place_id = ? AND is_active = 1",
                    (business.google_place_id,),
                )
                row = await cursor.fetchone()
                if row:
                    match_id = row["id"]

            # 2. Buscar por source_url completa
            if match_id is None and business.source_url:
                cursor = await db.execute(
                    "SELECT id FROM businesses WHERE source_url = ? AND is_active = 1",
                    (business.source_url,),
                )
                row = await cursor.fetchone()
                if row:
                    match_id = row["id"]

            # 3. Sino, buscar por nombre + proximidad
            if match_id is None:
                cursor = await db.execute(
                    """SELECT id, lat, lng FROM businesses
                       WHERE is_active = 1 AND LOWER(name) = LOWER(?)
                       ORDER BY id""",
                    (business.name,),
                )
                rows = await cursor.fetchall()
                for row in rows:
                    dist = haversine_distance(business.lat, business.lng, row["lat"], row["lng"])
                    if dist <= 100:
                        match_id = row["id"]
                        break

            if match_id is not None:
                await db.execute(
                    """UPDATE businesses
                       SET category = ?, search_category = ?, address = ?, phone = ?, website = ?,
                           rating = ?, review_count = ?,
                           source_url = COALESCE(?, source_url),
                           google_place_id = COALESCE(?, google_place_id),
                           metadata = ?, updated_at = datetime('now')
                       WHERE id = ?""",
                    (
                        business.category, business.search_category,
                        business.address, business.phone,
                        business.website, business.rating, business.review_count,
                        business.source_url, business.google_place_id,
                        json.dumps(business.metadata or {}),
                        match_id,
                    ),
                )
                await db.commit()
                return False
            else:
                await db.execute(
                    """INSERT INTO businesses
                       (name, lat, lng, category, search_category, address, phone, website,
                        rating, review_count, source_url, google_place_id,
                        raw_name, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        business.name, business.lat, business.lng,
                        business.category, business.search_category,
                        business.address, business.phone,
                        business.website, business.rating, business.review_count,
                        business.source_url, business.google_place_id,
                        business.raw_name,
                        json.dumps(business.metadata or {}),
                    ),
                )
                await db.commit()
                return True

    async def insert_batch(self, businesses: List[NormalizedBusiness]) -> int:
        inserted = 0
        for b in businesses:
            is_new = await self.upsert_business(b)
            if is_new:
                inserted += 1
        return inserted

    async def exists_similar(self, name: str, lat: float, lng: float,
                             threshold_m: float = 50.0) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT 1 FROM businesses WHERE is_active = 1 AND LOWER(name) = LOWER(?) LIMIT 1""",
                (name,),
            )
            if await cursor.fetchone():
                return True
            return False

    async def count_businesses(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM businesses WHERE is_active = 1")
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def export_json(self, output_path: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT name, lat, lng, category FROM businesses WHERE is_active = 1"
            )
            rows = await cursor.fetchall()
            data = [{"name": r["name"], "lat": r["lat"], "lng": r["lng"], "category": r["category"]} for r in rows]
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return len(data)

    # ── scraping_tasks CRUD ──────────────────────────────────────

    async def upsert_task_pending(self, grid_cell_json: str, category: str, depth: int) -> None:
        """Registra una tarea como pendiente (idempotente, para overflow)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                """INSERT OR IGNORE INTO scraping_tasks
                   (grid_cell_json, category, depth, status)
                   VALUES (?, ?, ?, 'pending')""",
                (grid_cell_json, category, depth),
            )
            await db.commit()

    async def mark_task_in_progress(self, grid_cell_json: str, category: str, depth: int) -> None:
        """Marca una tarea como en progreso (al hacer dequeue)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                """INSERT INTO scraping_tasks (grid_cell_json, category, depth, status, started_at)
                   VALUES (?, ?, ?, 'in_progress', datetime('now'))
                   ON CONFLICT(grid_cell_json, category, depth)
                   DO UPDATE SET status='in_progress', started_at=datetime('now')""",
                (grid_cell_json, category, depth),
            )
            await db.commit()

    async def mark_task_completed(self, grid_cell_json: str, category: str, depth: int,
                                  results_count: int) -> None:
        """Marca una tarea como completada."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                """INSERT INTO scraping_tasks
                   (grid_cell_json, category, depth, status, results_count, completed_at)
                   VALUES (?, ?, ?, 'completed', ?, datetime('now'))
                   ON CONFLICT(grid_cell_json, category, depth)
                   DO UPDATE SET status='completed', results_count=excluded.results_count,
                                 completed_at=datetime('now')""",
                (grid_cell_json, category, depth, results_count),
            )
            await db.commit()

    async def mark_task_failed(self, grid_cell_json: str, category: str, depth: int,
                               error_message: str) -> None:
        """Marca una tarea como fallida e incrementa retry_count."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                """INSERT INTO scraping_tasks
                   (grid_cell_json, category, depth, status, error_message, retry_count)
                   VALUES (?, ?, ?, 'failed', ?, 1)
                   ON CONFLICT(grid_cell_json, category, depth)
                   DO UPDATE SET status='failed',
                                 error_message=excluded.error_message,
                                 retry_count=retry_count + 1""",
                (grid_cell_json, category, depth, error_message),
            )
            await db.commit()

    async def get_completed_task_keys(self) -> set:
        """Devuelve el conjunto de (grid_cell_json, category, depth) completados."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT grid_cell_json, category, depth FROM scraping_tasks WHERE status='completed'"
            )
            rows = await cursor.fetchall()
            return {(r[0], r[1], r[2]) for r in rows}

    async def get_pending_or_in_progress_tasks(self) -> list[dict]:
        """Devuelve tareas interrumpidas (pending o in_progress) para re-encolar."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT grid_cell_json, category, depth
                   FROM scraping_tasks
                   WHERE status IN ('pending', 'in_progress')"""
            )
            rows = await cursor.fetchall()
            return [{"grid_cell_json": r["grid_cell_json"], "category": r["category"],
                     "depth": r["depth"]} for r in rows]
