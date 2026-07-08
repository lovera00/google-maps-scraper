import asyncio
import logging
from typing import List, Optional

from ..db.connection import initialize_db
from ..db.repository import Repository
from ..db.schema import DDL
from ..models.business import NormalizedBusiness

logger = logging.getLogger(__name__)


class Storage:
    def __init__(self, config):
        self.config = config
        self.db_path = config.database.path
        self.batch_size = config.storage.batch_size
        self.repo = Repository(self.db_path)

        # PostgreSQL (opcional, solo se activa en el flush final durante shutdown)
        self._pg_dsn: Optional[str] = None
        self.pg: Optional[object] = None
        if config.postgres.enabled and config.postgres.dsn:
            self._pg_dsn = config.postgres.dsn

    async def initialize(self):
        await initialize_db(self.db_path, DDL)
        logger.info(f"Base de datos SQLite inicializada: {self.db_path}")
        # PostgreSQL solo se conecta en el flush final (shutdown), no durante la ejecucion

    async def close(self):
        """Cierra conexiones PostgreSQL (para graceful shutdown)."""
        if self.pg:
            await self.pg.close()

    async def insert_batch(self, businesses: List[NormalizedBusiness]) -> int:
        # Solo escribe a SQLite durante la ejecucion. PostgreSQL se actualiza en el flush final.
        return await self.repo.insert_batch(businesses)

    async def count(self) -> int:
        return await self.repo.count_businesses()

    async def count_pg(self) -> int:
        """Cuenta registros en PostgreSQL."""
        if self.pg:
            return await self.pg.count_businesses()
        return 0

    async def export_json(self, path: str) -> int:
        return await self.repo.export_json(path)

    async def flush_to_postgres(self) -> int:
        """Fuerza el volcado de todos los datos de SQLite a PostgreSQL.

        Se llama durante el graceful shutdown. Si PostgreSQL no esta inicializado
        (ej: fallo el timeout al inicio), reintenta la conexion con mas tiempo.
        """
        # Intentar reconectar si tenemos DSN configurado pero pg es None
        if self.pg is None and self._pg_dsn:
            try:
                from ..db.postgres_repository import PostgresRepository
                self.pg = PostgresRepository(self._pg_dsn)
                await asyncio.wait_for(self.pg.initialize(), timeout=30.0)
                logger.info("PostgreSQL: reconectado para flush final")
            except Exception as e:
                logger.warning(f"PostgreSQL: no se pudo reconectar para flush ({e})")
                self.pg = None
                return 0

        if not self.pg:
            logger.warning("PostgreSQL no esta configurado, omitiendo flush")
            return 0

        # Leer TODOS los negocios activos de SQLite que tengan categoria.
        # No se suben registros sin categoria. El upsert en PostgreSQL evita
        # duplicados (match por google_place_id, source_url o nombre+proximidad).
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT name, lat, lng, category, search_category,
                          address, phone, website,
                          rating, review_count, source_url, google_place_id,
                          raw_name, metadata
                   FROM businesses
                   WHERE is_active = 1
                     AND category IS NOT NULL
                     AND category != ''
                     AND category != 'Sin categoria'
                     AND search_category IS NOT NULL
                     AND search_category != ''"""
            )
            rows = await cursor.fetchall()

        if not rows:
            logger.info("PostgreSQL flush: 0 registros con categoria en SQLite")
            return 0

        logger.info(f"PostgreSQL flush: sincronizando {len(rows)} registros con categoria")

        businesses = []
        for r in rows:
            import json
            meta = None
            if r["metadata"]:
                try:
                    meta = json.loads(r["metadata"])
                except (json.JSONDecodeError, TypeError):
                    pass

            businesses.append(NormalizedBusiness(
                name=r["name"],
                lat=r["lat"],
                lng=r["lng"],
                category=r["category"],
                search_category=r["search_category"] if "search_category" in r.keys() else "",
                address=r["address"],
                phone=r["phone"],
                website=r["website"],
                rating=r["rating"],
                review_count=r["review_count"],
                source_url=r["source_url"],
                google_place_id=r["google_place_id"],
                raw_name=r["raw_name"],
                metadata=meta,
            ))

        inserted = await self.pg.insert_batch(businesses)
        logger.info(f"PostgreSQL flush completado: {len(businesses)} procesados, "
                     f"{inserted} nuevos insertados")
        return inserted
