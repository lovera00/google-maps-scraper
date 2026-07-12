"""Orquestador central del pipeline multi-agente.

Coordina los 5 agentes usando asyncio.Queue como buffers entre etapas.
Corte inmediato (sin drenar colas ni flush) ante Ctrl+C/SIGTERM (ver
_abort_immediately). Ante bloqueo de Google NO se corta de una: se dispara
una pausa global con backoff escalante y se reintenta la tarea; solo se aborta
si el bloqueo persiste tras `max_consecutive_blocks` pausas.
"""
import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path

from .config.loader import load_config
from .config.settings import Settings
from .utils.logging_config import setup_logging
from .notifiers.telegram import TelegramNotifier
from .agents.query_planner import QueryPlanner
from .agents.data_collector import (
    DataCollector,
    GoogleMapsBlockedError,
    ScrapeTransientError,
)
from .agents.normalizer import Normalizer
from .agents.deduplicator import Deduplicator
from .agents.storage import Storage
from .db.writer import DbWriter
from .models.query_task import QueryTask
from .models.grid import GridCell

logger = logging.getLogger(__name__)


class ScrapingStats:
    def __init__(self):
        self.total_scraped = 0
        self.total_normalized = 0
        self.total_discarded = 0
        self.total_duplicates = 0
        self.total_stored = 0
        self.overflow_events = 0
        self.start_time = time.monotonic()

    def report(self):
        elapsed = time.monotonic() - self.start_time
        logger.info("=" * 55)
        logger.info("  ESTADISTICAS FINALES DEL PIPELINE")
        logger.info("=" * 55)
        logger.info(f"  Scrapeados:      {self.total_scraped:>8}")
        logger.info(f"  Normalizados:    {self.total_normalized:>8}")
        logger.info(f"  Descartados:     {self.total_discarded:>8}")
        logger.info(f"  Duplicados:      {self.total_duplicates:>8}")
        logger.info(f"  Almacenados:     {self.total_stored:>8}")
        logger.info(f"  Overflows:       {self.overflow_events:>8}")
        logger.info(f"  Tiempo total:    {elapsed:>7.1f}s")
        logger.info("=" * 55)


class Orchestrator:
    def __init__(self, config_path: str = "config.yaml"):
        self.config = load_config(config_path)
        setup_logging(self.config)
        logger.info("Pipeline multi-agente inicializado")

        # Colas entre agentes
        self.task_queue: asyncio.Queue = asyncio.Queue(maxsize=0)
        self.raw_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.normalized_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.final_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

        # Agentes
        self.planner = QueryPlanner(self.config)
        self.collector = DataCollector(self.config)
        self.normalizer = Normalizer(self.config)
        self.deduplicator = Deduplicator(self.config)
        self.storage = Storage(self.config)

        # Writer serializado a SQLite (elimina "database is locked" con 50 workers)
        self.db_writer = DbWriter(self.storage.repo)

        # Reintentos por tarea ante fallo transitorio (timeout de navegacion)
        self.max_task_retries = getattr(
            self.config.rate_limit, "max_task_retries", 3)
        # Pausa pre-retry ante corte de red local (ver RateLimitConfig)
        self.network_blip_pause = getattr(
            self.config.rate_limit, "network_blip_pause_seconds", 8.0)

        # Paralelismo del collector (E1): N workers concurrentes sobre task_queue.
        self.num_workers = max(1, getattr(self.config, "workers", 1))
        # Tareas actualmente en proceso (dequeued, sin terminar). Un worker solo
        # termina cuando la cola esta vacia Y nadie esta procesando: asi no corta
        # mientras otro worker esta por inyectar sub-tareas de overflow.
        self._active_tasks = 0
        # Streaming de tareas: el productor (_feed_tasks) encola de a poco para
        # acotar RAM; los workers no deben terminar hasta que el productor termine.
        self.task_queue_high_water = getattr(self.config, "task_queue_high_water", 5000)
        self._producer_done = False

        # Notificador Telegram
        tg = self.config.telegram
        self.telegram = TelegramNotifier(
            bot_token=tg.bot_token,
            chat_id=tg.chat_id,
            min_level=tg.min_level,
            notify_on_start=tg.notify_on_start,
            notify_on_complete=tg.notify_on_complete,
        )
        self.collector.telegram = self.telegram if tg.enabled else None

        # Estadisticas
        self.stats = ScrapingStats()

    def _iter_tasks_from_file(self, path: str):
        """Generador LAZY: una QueryTask por linea, sin cargar el archivo entero
        en memoria. Lo consume el productor _feed_tasks con backpressure."""
        task_path = Path(path)
        if not task_path.exists():
            raise FileNotFoundError(f"Archivo de tareas no encontrado: {path}")
        with open(task_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield QueryTask.from_dict(json.loads(line))

    async def _feed_tasks(self, task_source, completed: set):
        """Productor: encola tareas desde `task_source` (iterable lazy) con
        backpressure por high-water para acotar la RAM, salteando las ya
        completadas (resume). Marca _producer_done al terminar."""
        fed = 0
        skipped = 0
        hw = self.task_queue_high_water
        check_completed = bool(completed)
        try:
            for task in task_source:
                if check_completed:
                    key = (task.grid_cell.to_json(), task.category, task.depth)
                    if key in completed:
                        skipped += 1
                        continue
                # Backpressure: si la cola ya tiene suficiente adelanto, esperar
                # a que los workers drenen antes de seguir encolando.
                while self.task_queue.qsize() >= hw:
                    await asyncio.sleep(0.2)
                await self.task_queue.put(task)
                fed += 1
                if fed % 2000 == 0:
                    await asyncio.sleep(0)  # ceder el loop a los workers
                if fed % 100000 == 0:
                    logger.info(f"Feed: {fed} tareas encoladas "
                                f"(cola actual={self.task_queue.qsize()})")
        finally:
            self._producer_done = True
        logger.info(f"Feed completo: {fed} tareas encoladas, "
                    f"{skipped} salteadas (ya completadas)")

    def _handle_shutdown_signal(self):
        """Callback sincrono para Ctrl+C / SIGTERM. Corta el proceso ya mismo,
        sin drenar colas ni hacer flush."""
        self._abort_immediately("interrupcion del usuario (Ctrl+C / SIGTERM)")

    async def _async_abort(self, reason: str):
        """Version async que notifica antes de abortar (usar desde workers)."""
        if self.telegram.enabled:
            try:
                await asyncio.wait_for(self.telegram.notify_abort(reason), timeout=5.0)
            except Exception:
                pass
        self._abort_immediately(reason)

    def _abort_immediately(self, reason: str):
        """Corte duro e inmediato: nada de drenar colas, nada de flush.
        Mata el proceso ahi mismo. Uso: Google Maps bloqueandonos, no tiene
        sentido seguir ni esperar un shutdown ordenado."""
        logger.error(f"ABORTANDO: {reason}")
        os._exit(1)

    async def run(self, tasks_file: str = None, db_path: str = None, resume: bool = True):
        logger.info("=== Iniciando pipeline de scraping ===")

        if db_path:
            self.storage.db_path = db_path
            self.storage.repo.db_path = db_path

        await self.storage.initialize()
        await self.db_writer.start()

        # Ctrl+C / SIGTERM: corte inmediato (ver _handle_shutdown_signal)
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, self._handle_shutdown_signal)
            loop.add_signal_handler(signal.SIGTERM, self._handle_shutdown_signal)
        except (NotImplementedError, RuntimeError):
            # Windows no soporta add_signal_handler
            signal.signal(signal.SIGINT, lambda s, f: self._handle_shutdown_signal())
            signal.signal(signal.SIGTERM, lambda s, f: self._handle_shutdown_signal())

        # Preparar la fuente de tareas de forma LAZY (streaming): un productor
        # (_feed_tasks) las encola con backpressure en vez de cargar el shard
        # entero en RAM. Resume = saltear las ya completadas (aplica tambien al
        # modo archivo, que antes re-scrapeaba todo al reiniciar).
        completed = set()
        if resume:
            completed = await self.storage.repo.get_completed_task_keys()
            if completed:
                logger.info(f"Reanudando: {len(completed)} tareas ya completadas se saltearan")

        if tasks_file:
            task_source = self._iter_tasks_from_file(tasks_file)
        else:
            # (E4) Sembrar pre-subdivididas las celdas que ya saturaron el cap
            # en corridas previas (historial en scraping_tasks), para no
            # re-scrapear el ancestro condenado al overflow.
            overflow_keys = None
            if getattr(self.planner, "overflow_seed_depth", 0) > 0:
                overflow_keys = await self.storage.repo.get_overflowed_task_keys(
                    self.collector.RESULTS_CAP
                )
                if overflow_keys:
                    logger.info(
                        f"E4: {len(overflow_keys)} celda-categorias con overflow "
                        f"previo se sembraran pre-subdivididas"
                    )
            # Lista ordenada por prioridad; se itera de forma lazy (se preserva
            # el orden urbano->rural).
            task_source = iter(self.planner.generate_initial_tasks(overflow_keys))

            if resume:
                interrupted = await self.storage.repo.get_pending_or_in_progress_tasks()
                if interrupted:
                    for row in interrupted:
                        cell = GridCell.from_dict(json.loads(row["grid_cell_json"]))
                        task = QueryTask(
                            grid_cell=cell,
                            category=row["category"],
                            depth=row["depth"],
                        )
                        task.priority = self.planner._calculate_priority(cell)
                        self.task_queue.put_nowait(task)
                    logger.info(f"{len(interrupted)} tareas interrumpidas re-encoladas")

        await self.collector.setup()

        if self.telegram.enabled:
            config_summary = (
                f"{self.num_workers} workers | "
                f"{len(self.config.categories)} categorias | "
                f"grid {self.config.grid.initial_size_km}km"
            )
            asyncio.create_task(self.telegram.notify_pipeline_start(config_summary))

        logger.info(f"Lanzando {self.num_workers} workers de scraping concurrentes")
        try:
            await asyncio.gather(
                self._feed_tasks(task_source, completed),
                self._run_all_collectors(),
                self._run_normalizer(),
                self._run_deduplicator(),
                self._run_storage(),
            )
        except asyncio.CancelledError:
            logger.warning("Pipeline cancelado, drenando colas...")
        except Exception as e:
            logger.error(f"Error fatal en el pipeline: {e}", exc_info=True)
            if self.telegram.enabled:
                try:
                    await asyncio.wait_for(
                        self.telegram.notify_fatal_error(str(e)[:300]), timeout=8.0
                    )
                except Exception:
                    pass
        finally:
            await self.collector.teardown()
            await self._drain_and_flush()
            if self.telegram.enabled:
                elapsed = time.monotonic() - self.stats.start_time
                stats_summary = (
                    f"{self.stats.total_scraped} scrapeados | "
                    f"{self.stats.total_stored} almacenados | "
                    f"{self.stats.overflow_events} overflows | "
                    f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"
                )
                await self.telegram.notify_pipeline_end(stats_summary)
            if self.telegram.enabled:
                await self.telegram.close()

        count = await self.storage.count()
        logger.info(f"=== Pipeline completado. {count} negocios en BD. ===")
        self.stats.report()

    async def _drain_and_flush(self):
        """Drena lo que quede en la cola final y persiste en PostgreSQL."""
        logger.info("Drenando cola final para flush a PostgreSQL...")

        # Drenar la cola final (items ya deduplicados pendientes de storage)
        remaining = []
        while not self.final_queue.empty():
            try:
                item = self.final_queue.get_nowait()
                if item is not None:
                    remaining.append(item)
                self.final_queue.task_done()
            except asyncio.QueueEmpty:
                break

        if remaining:
            await self.db_writer.insert_batch(remaining)
            self.stats.total_stored += len(remaining)
            logger.info(f"Drenados {len(remaining)} items pendientes de la cola final "
                        f"({len(remaining)} nuevos en SQLite)")

        # Esperar a que la cola de escrituras se vacíe antes de seguir
        await self.db_writer.flush()

        # Flush completo de SQLite a PostgreSQL (solo si hay DSN configurado)
        if self.storage._pg_dsn:
            try:
                pg_count = await self.storage.flush_to_postgres()
                logger.info(f"PostgreSQL tiene {pg_count} registros insertados/actualizados")
            except Exception as e:
                logger.error(f"Error en flush a PostgreSQL: {e}", exc_info=True)

        await self.db_writer.stop()
        await self.storage.close()

        # Exportar JSON
        json_path = "data/output.json"
        try:
            count = await self.storage.export_json(json_path)
            logger.info(f"Datos exportados a {json_path}: {count} registros")
        except Exception as e:
            logger.error(f"Error exportando JSON: {e}", exc_info=True)

    async def _run_all_collectors(self):
        """Lanza N workers de scraping y emite UN solo sentinela al terminar todos."""
        await asyncio.gather(
            *[self._collector_worker(i) for i in range(self.num_workers)]
        )
        # Recien cuando TODOS los workers terminaron, cerrar el stream aguas abajo.
        await self.raw_queue.put(None)
        logger.info("Todos los collectors finalizaron")

    async def _collector_worker(self, worker_id: int):
        while True:
            try:
                task = await asyncio.wait_for(self.task_queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                # Terminar solo si el productor ya termino de encolar, la cola
                # esta vacia, y nadie esta procesando (un scrape en curso podria
                # estar por inyectar sub-tareas de overflow). Sin la condicion de
                # _producer_done, un worker podria cortar durante un bache del
                # productor al arranque, cuando la cola aun no se lleno.
                if (self._producer_done
                        and self.task_queue.empty()
                        and self._active_tasks == 0):
                    break
                continue

            self._active_tasks += 1
            task_key = (task.grid_cell.to_json(), task.category, task.depth)

            try:
                await self.db_writer.mark_task_in_progress(*task_key)

                raw_businesses = await self.collector.scrape(task, worker_id)
                result_count = len(raw_businesses)

                if result_count >= self.collector.RESULTS_CAP:
                    new_tasks = self.planner.handle_overflow(task)
                    for nt in new_tasks:
                        nt_key = (nt.grid_cell.to_json(), nt.category, nt.depth)
                        await self.db_writer.upsert_task_pending(*nt_key)
                        await self.task_queue.put(nt)
                    self.stats.overflow_events += 1
                    logger.info(f"Overflow: {result_count} resultados en celda, "
                                f"{len(new_tasks)} sub-tareas creadas (depth={task.depth})")
                    if not new_tasks and self.telegram.enabled:
                        asyncio.create_task(self.telegram.notify_overflow_max_depth(task))

                for rb in raw_businesses:
                    await self.raw_queue.put(rb)
                self.stats.total_scraped += result_count

                await self.db_writer.mark_task_completed(*task_key, result_count)

                if result_count > 0:
                    logger.debug(f"Scraped: {result_count} resultados de {task.category}")

            except GoogleMapsBlockedError as e:
                logger.error(f"Google Maps bloqueado: {e}")
                # Notificar bloqueo antes de la pausa
                if self.telegram.enabled:
                    delay = min(
                        self.config.rate_limit.block_backoff_base_seconds *
                        (2 ** (self.collector._consecutive_blocks)),
                        self.config.rate_limit.block_backoff_max_seconds
                    )
                    asyncio.create_task(self.telegram.notify_block(
                        consecutive=self.collector._consecutive_blocks + 1,
                        max_consecutive=self.collector.max_consecutive_blocks,
                        delay_seconds=delay,
                        task=task,
                    ))
                # Pausa global con backoff escalante en vez de abortar de una.
                should_retry = await self.collector.handle_block()
                if not should_retry:
                    await self.db_writer.mark_task_failed(*task_key, str(e)[:500])
                    await self._async_abort(
                        f"Google Maps sigue bloqueando tras "
                        f"{self.collector.max_consecutive_blocks} pausas con backoff ({e})"
                    )
                # Re-encolar la MISMA tarea para reintentarla tras la pausa.
                await self.task_queue.put(task)
                logger.info("Reintentando la tarea tras la pausa por bloqueo")
            except ScrapeTransientError as e:
                if task.retry_count < self.max_task_retries:
                    task.retry_count += 1
                    if DataCollector._is_network_blip(str(e)):
                        # Corte de red local: esperar antes de re-encolar para
                        # no quemar los reintentos con la red todavia caida.
                        pause = self.network_blip_pause * task.retry_count
                        logger.warning(
                            f"Corte de red local detectado; esperando "
                            f"{pause:.0f}s antes de re-encolar (retry "
                            f"{task.retry_count}/{self.max_task_retries}): {e}"
                        )
                        await asyncio.sleep(pause)
                    await self.task_queue.put(task)
                    logger.warning(
                        f"Fallo transitorio (retry {task.retry_count}/"
                        f"{self.max_task_retries}), re-encolando: {e}"
                    )
                    if self.telegram.enabled:
                        asyncio.create_task(self.telegram.notify_transient_retry(
                            task, task.retry_count, self.max_task_retries, str(e)
                        ))
                else:
                    await self.db_writer.mark_task_failed(*task_key, str(e)[:500])
                    logger.error(
                        f"Fallo transitorio tras {self.max_task_retries} reintentos, "
                        f"marcando failed: {task.category} @ "
                        f"{task.center_lat:.4f},{task.center_lng:.4f}"
                    )
                    if self.telegram.enabled:
                        asyncio.create_task(self.telegram.notify_task_failed(
                            task, str(e), task.retry_count, self.max_task_retries
                        ))
            except Exception as e:
                logger.error(f"Error en collector: {e}")
                await self.db_writer.mark_task_failed(*task_key, str(e)[:500])
                if self.telegram.enabled:
                    asyncio.create_task(self.telegram.notify_task_failed(
                        task, str(e)[:200], 0, 0
                    ))
            finally:
                self._active_tasks -= 1
                self.task_queue.task_done()

        logger.info(f"[w{worker_id}] collector finalizado")

    async def _run_normalizer(self):
        batch = []
        batch_size = 50
        while True:
            item = await self.raw_queue.get()
            if item is None:
                if batch:
                    for b in batch:
                        await self.normalized_queue.put(b)
                await self.normalized_queue.put(None)
                break

            normalized = self.normalizer.normalize(item)
            if normalized:
                batch.append(normalized)
                self.stats.total_normalized += 1
            else:
                self.stats.total_discarded += 1

            if len(batch) >= batch_size:
                for b in batch:
                    await self.normalized_queue.put(b)
                batch = []

            self.raw_queue.task_done()
        logger.info("Normalizer finalizado")

    async def _run_deduplicator(self):
        batch = []
        batch_size = self.config.dedup.batch_size

        while True:
            item = await self.normalized_queue.get()
            if item is None:
                if batch:
                    deduped = await self.deduplicator.deduplicate(batch)
                    duplicates = len(batch) - len(deduped)
                    self.stats.total_duplicates += duplicates
                    for b in deduped:
                        await self.final_queue.put(b)
                await self.final_queue.put(None)
                break

            batch.append(item)
            if len(batch) >= batch_size:
                deduped = await self.deduplicator.deduplicate(batch)
                duplicates = len(batch) - len(deduped)
                self.stats.total_duplicates += duplicates
                for b in deduped:
                    await self.final_queue.put(b)
                batch = []

            self.normalized_queue.task_done()
        logger.info("Deduplicator finalizado")

    async def _run_storage(self):
        batch = []
        batch_size = self.config.storage.batch_size

        while True:
            item = await self.final_queue.get()
            if item is None:
                if batch:
                    await self.db_writer.insert_batch(batch)
                    self.stats.total_stored += len(batch)
                break

            batch.append(item)
            if len(batch) >= batch_size:
                await self.db_writer.insert_batch(batch)
                self.stats.total_stored += len(batch)
                batch = []

            self.final_queue.task_done()

        logger.info(f"Storage finalizado. {self.stats.total_stored} nuevos almacenados")
