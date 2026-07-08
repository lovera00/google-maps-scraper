#!/usr/bin/env python3
"""Extrae capas de OSM PBF y las carga a PostGIS con staging swap.

Guarda las capas extraidas a JSON en data/osm/ para evitar re-extraer
si la carga falla. Para forzar re-extraccion, borrar los archivos JSON.

Uso:
    python scripts/osm_load.py
    python scripts/osm_load.py --dry-run
    python scripts/osm_load.py --pbf data/osm/paraguay-20260707.osm.pbf
    python scripts/osm_load.py --no-cache   # Forzar re-extraccion
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.loader import load_config
from src.osm.extract import extract_layers
from src.osm.load import load_layers

logger = logging.getLogger("osm_load")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _cache_path(pbf_path: Path, data_dir: Path) -> Path:
    stem = pbf_path.stem  # e.g. paraguay-20260707
    return data_dir / f"{stem}_layers.json"


def _load_cache(cache_path: Path) -> dict | None:
    if not cache_path.exists():
        return None
    logger.info("Cargando cache: %s (%.1f MB)", cache_path.name,
                cache_path.stat().st_size / (1024 * 1024))
    with open(cache_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_cache(cache_path: Path, layers: dict):
    logger.info("Guardando cache: %s", cache_path.name)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(layers, f, ensure_ascii=False)


async def main():
    parser = argparse.ArgumentParser(description="Carga capas OSM a PostGIS")
    parser.add_argument("--dry-run", action="store_true", help="Extraer sin cargar a la DB")
    parser.add_argument("--pbf", help="Ruta al archivo .osm.pbf")
    parser.add_argument("--no-cache", action="store_true", help="Forzar re-extraccion ignorando cache")
    args = parser.parse_args()

    setup_logging()
    config = load_config("config.yaml")

    dsn = os.environ.get("PG_DSN", "")
    if not args.dry_run and not dsn:
        logger.error("PG_DSN no configurado. Usa --dry-run para probar sin DB.")
        return

    pbf_dir = Path(config.osm.data_dir)
    if args.pbf:
        pbf_path = Path(args.pbf)
    else:
        pbfs = sorted(pbf_dir.glob("paraguay-*.osm.pbf"), reverse=True)
        if not pbfs:
            logger.error("No se encontro PBF en %s. Ejecuta osm_download.py primero.", pbf_dir)
            return
        pbf_path = pbfs[0]

    logger.info("PBF: %s (%.1f MB)", pbf_path, pbf_path.stat().st_size / (1024 * 1024))

    t0 = time.monotonic()

    # 1. Extract (with disk cache)
    cache_path = _cache_path(pbf_path, pbf_dir)
    layers = None if args.no_cache else _load_cache(cache_path)

    if layers is None:
        layers = extract_layers(str(pbf_path))
        _save_cache(cache_path, layers)

    total = sum(len(v) for v in layers.values())
    logger.info("Total features: %d (%.1fs)", total, time.monotonic() - t0)

    # 2. Load to PostGIS
    if args.dry_run:
        logger.info("[dry-run] Se cargarian %d features en 5 tablas osm_*", total)
    else:
        t1 = time.monotonic()
        await load_layers(layers, dsn)
        logger.info("Carga completa en %.1fs", time.monotonic() - t1)

    logger.info("Tiempo total: %.1fs", time.monotonic() - t0)


if __name__ == "__main__":
    asyncio.run(main())
