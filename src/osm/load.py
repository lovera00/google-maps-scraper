import asyncio
import asyncpg
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LAYER_SCHEMAS = {
    "vias": {
        "geom_type": "GEOMETRY(LINESTRING, 4326)",
        "columns": [("name", "TEXT"), ("highway", "TEXT")],
    },
    "parques": {
        "geom_type": "GEOMETRY(POLYGON, 4326)",
        "columns": [("name", "TEXT"), ("leisure", "TEXT")],
    },
    "agua": {
        "geom_type": "GEOMETRY(GEOMETRY, 4326)",
        "columns": [("name", "TEXT"), ("natural", "TEXT"), ("waterway", "TEXT")],
    },
    "equipamiento": {
        "geom_type": "GEOMETRY(POINT, 4326)",
        "columns": [("name", "TEXT"), ("amenity", "TEXT")],
    },
    "limites": {
        "geom_type": "GEOMETRY(GEOMETRY, 4326)",
        "columns": [("name", "TEXT"), ("admin_level", "TEXT"), ("place", "TEXT")],
    },
}


async def load_layers(layers: dict[str, list[dict]], dsn: str, dry_run: bool = False):
    """Load extracted OSM layers into PostGIS using staging-table swap.

    Parameters
    ----------
    layers : dict
        Output of extract_layers(): {layer_name: [feature_dict, ...]}
    dsn : str
        PostgreSQL connection string.
    dry_run : bool
        If True, print what would happen without modifying the DB.
    """
    conn = await asyncpg.connect(dsn)
    try:
        for layer_name, features in layers.items():
            await _load_layer(conn, layer_name, features, dry_run)
    finally:
        await conn.close()


async def _load_layer(conn, layer_name: str, features: list[dict], dry_run: bool):
    schema = LAYER_SCHEMAS[layer_name]
    table = f"osm_{layer_name}"
    staging = f"{table}_staging"

    if dry_run:
        logger.info("[dry-run] Cargar %d features en %s", len(features), table)
        return

    start = datetime.now(timezone.utc)

    # 1. Drop staging if exists
    await conn.execute(f"DROP TABLE IF EXISTS {staging}")

    # 2. Create staging table
    col_defs = ", ".join(f'"{col}" {dtype}' for col, dtype in schema["columns"])
    await conn.execute(f"""
        CREATE TABLE {staging} (
            id SERIAL PRIMARY KEY,
            {col_defs},
            geom {schema["geom_type"]}
        )
    """)

    # 3. Batch insert
    batch_size = 1000
    total = len(features)

    col_names = [c[0] for c in schema["columns"]]
    placeholders = ", ".join(
        [f"${i + 1}" for i in range(len(col_names))]
        + [f"ST_SetSRID(ST_GeomFromGeoJSON(${len(col_names) + 1}), 4326)"]
    )

    quoted_cols = ", ".join(f'"{c}"' for c in col_names)
    sql = f"INSERT INTO {staging} ({quoted_cols}, geom) VALUES ({placeholders})"

    for i in range(0, total, batch_size):
        batch = features[i : i + batch_size]
        rows = []
        for f in batch:
            row = tuple(f.get(c, "") for c in col_names) + (f["geom"],)
            rows.append(row)

        async with conn.transaction():
            await conn.executemany(sql, rows)

        if (i + batch_size) % 10000 == 0 or i + batch_size >= total:
            logger.info("  %s: %d/%d", layer_name, min(i + batch_size, total), total)

    # 4. Create GIST index on staging
    await conn.execute(f"CREATE INDEX ON {staging} USING GIST (geom)")

    # 5. Transactional swap
    async with conn.transaction():
        await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        await conn.execute(f"ALTER TABLE {staging} RENAME TO {table}")
        await conn.execute(f"ALTER INDEX {staging}_geom_idx RENAME TO {table}_geom_idx")

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    logger.info("  %s: %d features cargados en %.1fs", table, total, elapsed)

    # 6. Update osm_meta
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS osm_meta (
            extract_date DATE,
            loaded_at TIMESTAMPTZ,
            layer TEXT,
            row_count INTEGER,
            PRIMARY KEY (extract_date, layer)
        )
    """)
    today = start.date()
    await conn.execute(
        "INSERT INTO osm_meta (extract_date, loaded_at, layer, row_count) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (extract_date, layer) DO UPDATE SET "
        "loaded_at = $2, row_count = $4",
        today, start, table, total,
    )
