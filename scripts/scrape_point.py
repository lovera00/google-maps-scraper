#!/usr/bin/env python3
"""Genera tareas para UNA sola cuadricula de 5km (la que contiene un punto dado),
en vez de para todo Paraguay. Util para probar el enfoque de "un punto -> features
del entorno" sin correr el pipeline completo.

Uso:
    python -m scripts.scrape_point --lat -25.336 --lng -57.403
    python -m scripts.scrape_point --lat -25.336 --lng -57.403 --categories restaurante,farmacia,banco
    python -m scripts.scrape_point --lat -25.336 --lng -57.403 --output data/mi_punto.jsonl

Despues de generar el archivo, ejecutar con el pipeline normal:
    python run.py --tasks-file data/point_tasks.jsonl --db data/point_test.db
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.loader import load_config
from src.agents.query_planner import QueryPlanner
from src.models.query_task import QueryTask


def main():
    parser = argparse.ArgumentParser(description="Generar tareas para una sola cuadricula de 5km")
    parser.add_argument("--lat", type=float, required=True, help="Latitud del punto")
    parser.add_argument("--lng", type=float, required=True, help="Longitud del punto")
    parser.add_argument("--config", default="config.yaml", help="Ruta al config.yaml")
    parser.add_argument("--output", default="data/point_tasks.jsonl", help="Archivo JSONL de salida")
    parser.add_argument("--categories", default=None,
                        help="Lista separada por comas (ej: restaurante,farmacia). "
                             "Por defecto usa TODAS las categorias del config.")
    args = parser.parse_args()

    config = load_config(args.config)
    planner = QueryPlanner(config)

    cell = planner.find_cell_for_point(args.lat, args.lng)

    categories = (
        [c.strip() for c in args.categories.split(",")]
        if args.categories else config.categories
    )

    tasks = [QueryTask(grid_cell=cell, category=cat, depth=0, priority=0.0) for cat in categories]

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for task in tasks:
            f.write(json.dumps(task.to_dict(), ensure_ascii=False) + "\n")

    est_seconds = len(tasks) * 25.3  # tasa medida en el run actual
    print(f"Punto:      ({args.lat}, {args.lng})")
    print(f"Celda:      lat [{cell.lat_min:.6f}, {cell.lat_max:.6f}]  lng [{cell.lng_min:.6f}, {cell.lng_max:.6f}]")
    print(f"Centro:     ({cell.center_lat:.6f}, {cell.center_lng:.6f})")
    print(f"Area:       {cell.area_km2():.2f} km2")
    print(f"Categorias: {len(categories)}")
    print(f"Tareas escritas a {output_path}: {len(tasks)}")
    print(f"Tiempo estimado (secuencial, ~25.3s/tarea): {est_seconds/60:.1f} min")


if __name__ == "__main__":
    main()
