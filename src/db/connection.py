import aiosqlite
from pathlib import Path


async def get_connection(db_path: str, wal_mode: bool = True) -> aiosqlite.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path), timeout=30.0)
    conn.row_factory = aiosqlite.Row
    if wal_mode:
        await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=30000")
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn


async def initialize_db(db_path: str, schema_ddl: str) -> None:
    conn = await get_connection(db_path)
    try:
        await conn.executescript(schema_ddl)
        # Migracion: agregar search_category si no existe (BDs existentes)
        try:
            await conn.execute(
                "ALTER TABLE businesses ADD COLUMN search_category TEXT NOT NULL DEFAULT ''"
            )
            await conn.commit()
        except Exception:
            pass  # La columna ya existe
        await conn.commit()
    finally:
        await conn.close()
