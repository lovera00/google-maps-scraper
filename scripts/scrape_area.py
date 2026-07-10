#!/usr/bin/env python3
"""Genera tareas para un AREA (radio alrededor de un punto) con celdas del tamano
que se elija. Sirve para re-scrapear una zona con celdas mas chicas y capturar la
cola larga que el tope de ~120 resultados por busqueda deja afuera con celdas grandes.

Uso:
    # Zona completa (radio 4.6km) con celdas de 2km, todas las categorias:
    python -m scripts.scrape_area --lat -25.402538 --lng -57.577318 --radius-km 4.6 --cell-km 2 --output data/area.jsonl

    # Test rapido: solo las categorias densas donde estaba el gap:
    python -m scripts.scrape_area --lat -25.402538 --lng -57.577318 --radius-km 4.6 --cell-km 2 \
        --categories "despensa,peluqueria,taller,bebidas,heladeria,restaurante,nails,ferreteria" \
        --output data/area_test.jsonl

Despues:
    python run.py --tasks-file data/area.jsonl --db data/paraguay_businesses.db
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.loader import load_config
from src.models.grid import GridCell
from src.models.query_task import QueryTask
from src.utils.geo import km_to_degrees_lat, km_to_degrees_lng, haversine_distance


def main():
    p = argparse.ArgumentParser(description="Generar tareas para un area con celdas configurables")
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lng", type=float, required=True)
    p.add_argument("--radius-km", type=float, default=4.6, help="Radio a cubrir alrededor del punto")
    p.add_argument("--cell-km", type=float, default=2.0, help="Tamano de cada celda (mas chico = mas cobertura)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--output", default="data/area_tasks.jsonl")
    p.add_argument("--categories", default=None,
                   help="Lista separada por comas. Por defecto TODAS las del config.")
    args = p.parse_args()

    config = load_config(args.config)
    cats = ([c.strip() for c in args.categories.split(",")]
            if args.categories else config.categories)

    R = args.radius_km * 1000.0
    lat_step = km_to_degrees_lat(args.cell_km)
    lat_span = km_to_degrees_lat(args.radius_km)

    cells = []
    lat = args.lat - lat_span
    while lat < args.lat + lat_span:
        lng_step = km_to_degrees_lng(args.cell_km, lat)
        lng_span = km_to_degrees_lng(args.radius_km, lat)
        lng = args.lng - lng_span
        while lng < args.lng + lng_span:
            cell = GridCell(lat_min=lat, lat_max=lat + lat_step,
                            lng_min=lng, lng_max=lng + lng_step)
            # incluir si la celda esta (aprox) dentro del circulo
            d = haversine_distance(args.lat, args.lng, cell.center_lat, cell.center_lng)
            if d <= R + args.cell_km * 1000:
                cells.append(cell)
            lng += lng_step
        lat += lat_step

    tasks = [QueryTask(grid_cell=c, category=cat, depth=0, priority=0.0)
             for c in cells for cat in cats]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for t in tasks:
            f.write(json.dumps(t.to_dict(), ensure_ascii=False) + "\n")

    est_min = len(tasks) * 25.3 / 60
    zoom = cells[0].estimate_zoom_level() if cells else "?"
    print(f"Centro:     ({args.lat}, {args.lng})")
    print(f"Radio:      {args.radius_km} km   |   Celda: {args.cell_km} km (zoom {zoom})")
    print(f"Celdas:     {len(cells)}")
    print(f"Categorias: {len(cats)}")
    print(f"Tareas:     {len(tasks)}")
    print(f"Escrito a:  {out}")
    print(f"Estimado (secuencial, ~25.3s/tarea): {est_min:.0f} min = {est_min/60:.1f} h")


if __name__ == "__main__":
    main()
