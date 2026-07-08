#!/usr/bin/env python3
"""Descarga el PBF de Paraguay desde Geofabrik con reintentos.

Uso:
    python scripts/osm_download.py
    python scripts/osm_download.py --dry-run
    python scripts/osm_download.py --url https://... --output data/osm/custom.pbf
"""
import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from src.config.loader import load_config

logger = logging.getLogger("osm_download")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def download(url: str, dest: Path, max_retries: int = 3, backoff: float = 2.0):
    """Download file with retry + backoff."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=600, follow_redirects=True) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    last_pct = -1
                    with open(dest, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total:
                                pct = downloaded * 100 // total
                                if pct != last_pct and pct % 10 == 0:
                                    logger.info("  %d%% (%d/%d MB)", pct,
                                                downloaded // (1024 * 1024),
                                                total // (1024 * 1024))
                                    last_pct = pct
                    logger.info("Descarga completa: %s (%.1f MB)", dest.name,
                                dest.stat().st_size / (1024 * 1024))
                    return
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                delay = backoff ** (attempt + 1)
                logger.warning("Intento %d/%d fallido: %s. Reintentando en %.1fs...",
                               attempt + 1, max_retries, e, delay)
                await asyncio.sleep(delay)
            else:
                logger.error("Fallo tras %d intentos. Ultimo error: %s", max_retries + 1, last_exc)
                raise


async def main():
    parser = argparse.ArgumentParser(description="Descarga OSM PBF de Paraguay")
    parser.add_argument("--dry-run", action="store_true", help="Mostrar URL y destino sin descargar")
    parser.add_argument("--url", help="URL alternativa del PBF")
    parser.add_argument("--output", help="Ruta de destino")
    args = parser.parse_args()

    setup_logging()
    config = load_config("config.yaml")

    url = args.url or config.osm.download_url
    data_dir = Path(args.output).parent if args.output else Path(config.osm.data_dir)
    date_str = datetime.now().strftime("%Y%m%d")
    dest = Path(args.output) if args.output else data_dir / f"paraguay-{date_str}.osm.pbf"

    if args.dry_run:
        logger.info("[dry-run] URL: %s", url)
        logger.info("[dry-run] Destino: %s", dest)
        return

    logger.info("Descargando %s → %s", url, dest)
    await download(url, dest, config.osm.download_retries, config.osm.download_backoff)


if __name__ == "__main__":
    asyncio.run(main())
