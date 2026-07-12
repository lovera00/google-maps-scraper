"""Serializa TODAS las escrituras a SQLite a través de una cola interna.

Elimina el "database is locked" al tener un solo writer con una conexión
persistente. Los workers/scrapers encolan operaciones; una corrutina interna
las ejecuta secuencialmente.
"""
import asyncio
import logging
from typing import List, Optional

import aiosqlite

from ..models.business import NormalizedBusiness

logger = logging.getLogger(__name__)


class DbWriter:
    def __init__(self, repo):
        self.repo = repo
        self._queue = asyncio.Queue()
        self._db: Optional[aiosqlite.Connection] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        self._db = await aiosqlite.connect(self.repo.db_path, timeout=60.0)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("DbWriter iniciado (escrituras serializadas a SQLite)")

    async def _run(self):
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            try:
                cmd, args = item
                await cmd(self._db, *args)
            except Exception as e:
                logger.error(f"Error en DbWriter: {e}", exc_info=True)
            finally:
                self._queue.task_done()

    async def flush(self):
        await self._queue.join()

    async def stop(self):
        self._running = False
        await self._queue.join()
        await self._queue.put(None)
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning("DbWriter no termino en 30s, forzando cierre")
                self._task.cancel()
        if self._db:
            await self._db.close()
            self._db = None
        logger.info("DbWriter detenido")

    # ── Enqueue methods (fire-and-forget, retornan inmediato) ──

    async def mark_task_in_progress(self, *args):
        await self._queue.put((self.repo._mark_task_in_progress_on_conn, args))

    async def mark_task_completed(self, *args):
        await self._queue.put((self.repo._mark_task_completed_on_conn, args))

    async def mark_task_failed(self, *args):
        await self._queue.put((self.repo._mark_task_failed_on_conn, args))

    async def upsert_task_pending(self, *args):
        await self._queue.put((self.repo._upsert_task_pending_on_conn, args))

    async def insert_batch(self, businesses: List[NormalizedBusiness]):
        await self._queue.put((self.repo._insert_batch_on_conn, (businesses,)))
