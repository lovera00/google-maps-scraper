#!/usr/bin/env python3
"""Genera todas las tareas de scraping y las escribe a un archivo JSONL.

Uso:
    python -m scripts.task_generator --config config.yaml --output data/tasks.jsonl
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.loader import load_config
from src.agents.query_planner import QueryPlanner
from src.agents.data_collector import DataCollector


def _load_overflow_keys(db_path: str, cap: int) -> set:
    """(E4) Lee del DB de una corrida previa las celdas×categoria (depth 0) que
    saturaron el cap, para sembrarlas pre-subdivididas en esta generacion."""
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            "SELECT grid_cell_json, category FROM scraping_tasks "
            "WHERE depth = 0 AND results_count >= ?",
            (cap,),
        )
        return {(r[0], r[1]) for r in cur.fetchall()}
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description="Generar tareas de scraping a JSONL")
    parser.add_argument("--config", default="config.yaml", help="Ruta al archivo YAML de config")
    parser.add_argument("--output", default="data/tasks.jsonl", help="Archivo JSONL de salida")
    parser.add_argument("--overflow-db",
                        help="(E4) DB SQLite de una corrida previa: las celdas que "
                             "saturaron el cap se generan ya pre-subdivididas "
                             "(requiere grid.overflow_seed_depth > 0 en el config)")
    args = parser.parse_args()

    config = load_config(args.config)
    planner = QueryPlanner(config)

    overflow_keys = None
    if args.overflow_db:
        if not Path(args.overflow_db).exists():
            print(f"Error: --overflow-db no encontrado: {args.overflow_db}")
            sys.exit(1)
        overflow_keys = _load_overflow_keys(args.overflow_db, DataCollector.RESULTS_CAP)
        print(f"E4: {len(overflow_keys)} celda-categorias con overflow previo "
              f"-> sembradas a depth {planner.overflow_seed_depth}")

    tasks = planner.generate_initial_tasks(overflow_keys)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        for task in tasks:
            f.write(json.dumps(task.to_dict(), ensure_ascii=False) + "\n")

    print(f"{len(tasks)} tareas escritas a {output_path}")


if __name__ == "__main__":
    main()
