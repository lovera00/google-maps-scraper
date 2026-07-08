#!/usr/bin/env python3
"""Consultas puntuales sobre capas OSM cargadas en PostGIS.

Uso:
    python scripts/osm_query.py --lat -25.282 --lng -57.635    # nearest_road + locate_point
    python scripts/osm_query.py --lat -25.3 --lng -57.6 --road # Solo nearest_road
    python scripts/osm_query.py --lat -25.3 --lng -57.6 --locate  # Solo locate_point
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.loader import load_config
from src.osm.queries import nearest_road, locate_point

logger = logging.getLogger("osm_query")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def main():
    parser = argparse.ArgumentParser(description="Consulta OSM puntual")
    parser.add_argument("--lat", type=float, required=True, help="Latitud")
    parser.add_argument("--lng", type=float, required=True, help="Longitud")
    parser.add_argument("--road", action="store_true", help="Solo nearest_road")
    parser.add_argument("--locate", action="store_true", help="Solo locate_point")
    args = parser.parse_args()

    setup_logging()
    load_config("config.yaml")

    dsn = os.environ.get("PG_DSN", "")
    if not dsn:
        logger.error("PG_DSN no configurado.")
        return

    lat, lng = args.lat, args.lng
    logger.info("Consultando lat=%.6f lng=%.6f ...", lat, lng)

    do_road = args.road or not args.locate
    do_locate = args.locate or not args.road

    result = {"lat": lat, "lng": lng}

    if do_road:
        result["nearest_road"] = await nearest_road(lat, lng, dsn)

    if do_locate:
        result["locate"] = await locate_point(lat, lng, dsn)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
