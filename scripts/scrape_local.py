#!/usr/bin/env python3
"""Corre el pipeline escribiendo SOLO al SQLite (SIN dual-write a Railway).

Identico a run.py, pero fuerza PG_DSN vacio ANTES de cargar la config, asi el
dual-write automatico a PostgreSQL queda desactivado. Pensado para el flujo:

    scrape -> SQLite (paraguay_businesses.db) -> flush_clean_to_pg.py -> Railway (clasificado)

Uso:
    python -m scripts.scrape_local --tasks-file data/asuncion_tasks.jsonl \
        --db data/paraguay_businesses.db --config config.asuncion.yaml

    # Reanudar tras un corte: mismo comando (saltea tareas ya completadas).
    # Reempezar de cero: agregar --no-resume
"""
import os
# Desactiva el dual-write a Railway. _load_dotenv usa os.environ.setdefault,
# asi que al estar PG_DSN ya presente (vacio) NO lo pisa con el de .env, y
# settings.py no auto-activa postgres cuando el DSN es vacio.
os.environ["PG_DSN"] = ""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.orchestrator import Orchestrator


def main():
    p = argparse.ArgumentParser(description="Pipeline solo-SQLite (sin Railway)")
    p.add_argument("--tasks-file", required=True, help="JSONL de tareas pre-generadas")
    p.add_argument("--db", default="data/paraguay_businesses.db",
                   help="SQLite destino (por defecto el master)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--no-resume", action="store_true",
                   help="Ignorar progreso previo y empezar de cero")
    args = p.parse_args()

    orchestrator = Orchestrator(config_path=args.config)
    try:
        asyncio.run(orchestrator.run(
            tasks_file=args.tasks_file,
            db_path=args.db,
            resume=not args.no_resume,
        ))
    except KeyboardInterrupt:
        print("\nInterrumpido. El shutdown graceful ya proceso los datos en SQLite.", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
