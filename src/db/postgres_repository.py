"""Repositorio PostgreSQL para persistencia final de negocios.

Usa asyncpg para operaciones async y JSONB para metadata.
Crea las tablas automaticamente si no existen.
"""
import json
import logging
from typing import List, Optional

from ..models.business import NormalizedBusiness
from ..utils.geo import haversine_distance

logger = logging.getLogger(__name__)

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS businesses (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    lat             DOUBLE PRECISION NOT NULL,
    lng             DOUBLE PRECISION NOT NULL,
    category        TEXT NOT NULL DEFAULT 'Sin categoria',
    search_category TEXT NOT NULL DEFAULT '',
    address         TEXT,
    phone           TEXT,
    website         TEXT,
    rating          DOUBLE PRECISION,
    review_count    INTEGER,
    source_url      TEXT,
    google_place_id TEXT,
    raw_name        TEXT,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_pg_businesses_name
    ON businesses (LOWER(name));
CREATE INDEX IF NOT EXISTS idx_pg_businesses_coords
    ON businesses (lat, lng);
CREATE INDEX IF NOT EXISTS idx_pg_businesses_category
    ON businesses (category);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pg_businesses_source_url
    ON businesses (source_url) WHERE source_url IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_pg_businesses_google_place_id
    ON businesses (google_place_id) WHERE google_place_id IS NOT NULL;
"""


class PostgresRepository:
    """Repositorio PostgreSQL con logica de upsert identica al SQLite repo."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool = None  # asyncpg.Pool

    async def initialize(self) -> None:
        """Crea el pool de conexiones y las tablas si no existen."""
        import asyncpg
        self._pool = await asyncpg.create_pool(
            self.dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(CREATE_TABLES_SQL)
        logger.info("PostgreSQL: pool creado y tablas inicializadas")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            logger.info("PostgreSQL: pool cerrado")

    async def upsert_business(self, business: NormalizedBusiness) -> bool:
        """Inserta o actualiza un negocio. Retorna True si fue insert nuevo."""
        async with self._pool.acquire() as conn:
            match_id = None

            # 1. Buscar por google_place_id
            if business.google_place_id:
                row = await conn.fetchrow(
                    "SELECT id FROM businesses WHERE google_place_id = $1 AND is_active = TRUE",
                    business.google_place_id,
                )
                if row:
                    match_id = row["id"]

            # 2. Buscar por source_url
            if match_id is None and business.source_url:
                row = await conn.fetchrow(
                    "SELECT id FROM businesses WHERE source_url = $1 AND is_active = TRUE",
                    business.source_url,
                )
                if row:
                    match_id = row["id"]

            # 3. Buscar por nombre + proximidad (carga todos los candidatos)
            if match_id is None:
                rows = await conn.fetch(
                    """SELECT id, lat, lng FROM businesses
                       WHERE is_active = TRUE AND LOWER(name) = LOWER($1)
                       ORDER BY id""",
                    business.name,
                )
                for row in rows:
                    dist = haversine_distance(
                        business.lat, business.lng, row["lat"], row["lng"]
                    )
                    if dist <= 100:
                        match_id = row["id"]
                        break

            metadata_json = json.dumps(business.metadata or {}, ensure_ascii=False)

            if match_id is not None:
                await conn.execute(
                    """UPDATE businesses
                       SET category = $1, search_category = $2, address = $3, phone = $4, website = $5,
                           rating = $6, review_count = $7,
                           source_url = COALESCE($8, source_url),
                           google_place_id = COALESCE($9, google_place_id),
                           metadata = $10::jsonb, updated_at = NOW()
                       WHERE id = $11""",
                    business.category,
                    business.search_category,
                    business.address,
                    business.phone,
                    business.website,
                    business.rating,
                    business.review_count,
                    business.source_url,
                    business.google_place_id,
                    metadata_json,
                    match_id,
                )
                return False
            else:
                await conn.execute(
                    """INSERT INTO businesses
                       (name, lat, lng, category, search_category, address, phone, website,
                        rating, review_count, source_url, google_place_id,
                        raw_name, metadata)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb)""",
                    business.name,
                    business.lat,
                    business.lng,
                    business.category,
                    business.search_category,
                    business.address,
                    business.phone,
                    business.website,
                    business.rating,
                    business.review_count,
                    business.source_url,
                    business.google_place_id,
                    business.raw_name,
                    metadata_json,
                )
                return True

    async def insert_batch(self, businesses: List[NormalizedBusiness]) -> int:
        inserted = 0
        total = len(businesses)
        for i, b in enumerate(businesses, start=1):
            is_new = await self.upsert_business(b)
            if is_new:
                inserted += 1
            if total > 200 and i % 200 == 0:
                logger.info(f"PostgreSQL flush: {i}/{total} procesados ({inserted} nuevos)")
        return inserted

    async def count_businesses(self) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) FROM businesses WHERE is_active = TRUE"
            )
            return row[0] if row else 0
