#!/usr/bin/env python3
"""Sistema Multi-Agente de Scraping de Google Maps para Paraguay.

Uso:
    python run.py                  # Modo normal (requiere Playwright)
    python run.py --test-mode      # Modo test con mocks HTML locales
    python run.py --config mi_config.yaml
"""
import asyncio
import argparse
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.orchestrator import Orchestrator


def main():
    parser = argparse.ArgumentParser(
        description="Scraper multi-agente de Google Maps para Paraguay"
    )
    parser.add_argument("--test-mode", action="store_true",
                        help="Usar mocks HTML locales en lugar de Google Maps real")
    parser.add_argument("--config", default="config.yaml",
                        help="Ruta al archivo de configuracion YAML")
    parser.add_argument("--tasks-file",
                        help="Archivo JSONL con tareas pre-generadas (modo distribuido)")
    parser.add_argument("--db",
                        help="Path personalizado para la base de datos SQLite")
    parser.add_argument("--no-resume", action="store_true",
                        help="Ignorar progreso previo y empezar desde cero")
    args = parser.parse_args()

    orchestrator = Orchestrator(config_path=args.config)

    if args.test_mode:
        orchestrator.config.test_mode = True

    try:
        asyncio.run(orchestrator.run(
            tasks_file=args.tasks_file,
            db_path=args.db,
            resume=not args.no_resume,
        ))
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario. El shutdown graceful ya proceso los datos.", file=sys.stderr)
        sys.exit(0)
    except Exception as e:
        print(f"Error fatal: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
