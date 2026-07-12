import json
import logging
import aiosqlite
from pathlib import Path
from typing import List, Optional

from ..models.business import NormalizedBusiness
from ..utils.geo import haversine_distance

logger = logging.getLogger(__name__)

# Umbral de proximidad del tier-3 del upsert (mismo negocio con igual nombre).
_PROXIMITY_MATCH_METERS = 100.0
# Sobre-aproximacion en grados para el prefiltro bbox del tier-3. A la latitud
# de Paraguay (~-25 deg) 100 m ~= 0.0009 deg tanto en lat como en lng, asi que
# 0.0011 deg cubre el radio con margen; el haversine hace el corte exacto luego.
# Sirve para que SQLite acote candidatos por indice y no compare todos los
# homonimos del pais.
_PROXIMITY_BBOX_DELTA_DEG = 0.0011


class Repository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def _upsert_on_conn(self, db: aiosqlite.Connection,
                              business: NormalizedBusiness) -> bool:
        """Ejecuta un upsert de 3 niveles sobre una conexion ya abierta.

        NO hace commit: el llamador controla la transaccion (una por batch).
        Devuelve True si insertó una fila nueva, False si actualizó una existente.
        """
        match_id = None

        # 1. Buscar por google_place_id (el ID canonico de Google Maps) -> indice unico
        if business.google_place_id:
            cursor = await db.execute(
                "SELECT id FROM businesses WHERE google_place_id = ? AND is_active = 1",
                (business.google_place_id,),
            )
            row = await cursor.fetchone()
            if row:
                match_id = row["id"]

        # 2. Buscar por source_url completa -> indice unico
        if match_id is None and business.source_url:
            cursor = await db.execute(
                "SELECT id FROM businesses WHERE source_url = ? AND is_active = 1",
                (business.source_url,),
            )
            row = await cursor.fetchone()
            if row:
                match_id = row["id"]

        # 3. Sino, buscar por nombre + proximidad.
        #    `name = ? COLLATE NOCASE` usa idx_businesses_name (SEARCH por indice);
        #    `LOWER(name) = LOWER(?)` NO podia usarlo y forzaba un full table scan
        #    por cada insercion nueva. El bbox en lat/lng acota los candidatos antes
        #    del haversine (que hace el corte exacto a <=100 m).
        if match_id is None:
            d = _PROXIMITY_BBOX_DELTA_DEG
            cursor = await db.execute(
                """SELECT id, lat, lng FROM businesses
                   WHERE is_active = 1 AND name = ? COLLATE NOCASE
                     AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?
                   ORDER BY id""",
                (business.name,
                 business.lat - d, business.lat + d,
                 business.lng - d, business.lng + d),
            )
            rows = await cursor.fetchall()
            for row in rows:
                dist = haversine_distance(business.lat, business.lng, row["lat"], row["lng"])
                if dist <= _PROXIMITY_MATCH_METERS:
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
            return True

    async def _insert_batch_on_conn(self, db: aiosqlite.Connection,
                                     businesses: List[NormalizedBusiness]) -> int:
        """Inserta un batch sobre una conexion ya abierta. Hace commit al final."""
        if not businesses:
            return 0
        inserted = 0
        for b in businesses:
            try:
                if await self._upsert_on_conn(db, b):
                    inserted += 1
            except Exception as e:
                logger.warning(f"Upsert fallido para '{getattr(b, 'name', '?')}': {e}")
        await db.commit()
        return inserted

    async def upsert_business(self, business: NormalizedBusiness) -> bool:
        """Upsert de un solo negocio en su propia transaccion (uso puntual)."""
        async with aiosqlite.connect(self.db_path, timeout=30.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            is_new = await self._upsert_on_conn(db, business)
            await db.commit()
            return is_new

    async def insert_batch(self, businesses: List[NormalizedBusiness]) -> int:
        """Upsert de un batch entero en UNA sola conexion y UN solo commit."""
        async with aiosqlite.connect(self.db_path, timeout=30.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            return await self._insert_batch_on_conn(db, businesses)

    async def exists_similar(self, name: str, lat: float, lng: float,
                             threshold_m: float = 50.0) -> bool:
        async with aiosqlite.connect(self.db_path, timeout=30.0) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT 1 FROM businesses WHERE is_active = 1 AND LOWER(name) = LOWER(?) LIMIT 1""",
                (name,),
            )
            if await cursor.fetchone():
                return True
            return False

    async def count_businesses(self) -> int:
        async with aiosqlite.connect(self.db_path, timeout=30.0) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM businesses WHERE is_active = 1")
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def export_json(self, output_path: str) -> int:
        async with aiosqlite.connect(self.db_path, timeout=30.0) as db:
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

    async def _upsert_task_pending_on_conn(self, db: aiosqlite.Connection,
                                           grid_cell_json: str, category: str, depth: int) -> None:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            """INSERT OR IGNORE INTO scraping_tasks
               (grid_cell_json, category, depth, status)
               VALUES (?, ?, ?, 'pending')""",
            (grid_cell_json, category, depth),
        )
        await db.commit()

    async def _mark_task_in_progress_on_conn(self, db: aiosqlite.Connection,
                                             grid_cell_json: str, category: str, depth: int) -> None:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            """INSERT INTO scraping_tasks (grid_cell_json, category, depth, status, started_at)
               VALUES (?, ?, ?, 'in_progress', datetime('now'))
               ON CONFLICT(grid_cell_json, category, depth)
               DO UPDATE SET status='in_progress', started_at=datetime('now')""",
            (grid_cell_json, category, depth),
        )
        await db.commit()

    async def _mark_task_completed_on_conn(self, db: aiosqlite.Connection,
                                           grid_cell_json: str, category: str, depth: int,
                                           results_count: int) -> None:
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

    async def _mark_task_failed_on_conn(self, db: aiosqlite.Connection,
                                        grid_cell_json: str, category: str, depth: int,
                                        error_message: str) -> None:
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

    async def upsert_task_pending(self, grid_cell_json: str, category: str, depth: int) -> None:
        async with aiosqlite.connect(self.db_path, timeout=30.0) as db:
            await self._upsert_task_pending_on_conn(db, grid_cell_json, category, depth)

    async def mark_task_in_progress(self, grid_cell_json: str, category: str, depth: int) -> None:
        async with aiosqlite.connect(self.db_path, timeout=30.0) as db:
            await self._mark_task_in_progress_on_conn(db, grid_cell_json, category, depth)

    async def mark_task_completed(self, grid_cell_json: str, category: str, depth: int,
                                  results_count: int) -> None:
        async with aiosqlite.connect(self.db_path, timeout=30.0) as db:
            await self._mark_task_completed_on_conn(db, grid_cell_json, category, depth, results_count)

    async def mark_task_failed(self, grid_cell_json: str, category: str, depth: int,
                               error_message: str) -> None:
        async with aiosqlite.connect(self.db_path, timeout=30.0) as db:
            await self._mark_task_failed_on_conn(db, grid_cell_json, category, depth, error_message)

    async def get_completed_task_keys(self) -> set:
        """Devuelve el conjunto de (grid_cell_json, category, depth) completados."""
        async with aiosqlite.connect(self.db_path, timeout=30.0) as db:
            cursor = await db.execute(
                "SELECT grid_cell_json, category, depth FROM scraping_tasks WHERE status='completed'"
            )
            rows = await cursor.fetchall()
            return {(r[0], r[1], r[2]) for r in rows}

    async def get_overflowed_task_keys(self, cap: int = 120, depth: int = 0) -> set:
        """(E4) Celdas×categoria que saturaron el cap en corridas previas.

        Devuelve el conjunto de (grid_cell_json, category) de tareas a la
        profundidad `depth` (por defecto las celdas iniciales) cuyo scrape
        alcanzo `cap` resultados. Se usa para sembrarlas pre-subdivididas y no
        re-scrapear el ancestro condenado al overflow.
        """
        async with aiosqlite.connect(self.db_path, timeout=30.0) as db:
            cursor = await db.execute(
                """SELECT grid_cell_json, category FROM scraping_tasks
                   WHERE depth = ? AND results_count >= ?""",
                (depth, cap),
            )
            rows = await cursor.fetchall()
            return {(r[0], r[1]) for r in rows}

    async def get_pending_or_in_progress_tasks(self) -> list[dict]:
        """Devuelve tareas interrumpidas (pending o in_progress) para re-encolar."""
        async with aiosqlite.connect(self.db_path, timeout=30.0) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT grid_cell_json, category, depth
                   FROM scraping_tasks
                   WHERE status IN ('pending', 'in_progress')"""
            )
            rows = await cursor.fetchall()
            return [{"grid_cell_json": r["grid_cell_json"], "category": r["category"],
                     "depth": r["depth"]} for r in rows]
