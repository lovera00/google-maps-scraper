#!/usr/bin/env python3
"""Genera todas las tareas de scraping y las escribe a un archivo JSONL.

Uso:
    python -m scripts.task_generator --config config.yaml --output data/tasks.jsonl
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.loader import load_config
from src.agents.query_planner import QueryPlanner


def main():
    parser = argparse.ArgumentParser(description="Generar tareas de scraping a JSONL")
    parser.add_argument("--config", default="config.yaml", help="Ruta al archivo YAML de config")
    parser.add_argument("--output", default="data/tasks.jsonl", help="Archivo JSONL de salida")
    args = parser.parse_args()

    config = load_config(args.config)
    planner = QueryPlanner(config)
    tasks = planner.generate_initial_tasks()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        for task in tasks:
            f.write(json.dumps(task.to_dict(), ensure_ascii=False) + "\n")

    print(f"{len(tasks)} tareas escritas a {output_path}")


if __name__ == "__main__":
    main()
