#!/usr/bin/env python3
"""Genera tareas para una CIUDAD completa, recortando el grid al poligono
administrativo real de OSM (tabla osm_limites), en vez de a un circulo o bbox.

Asi no se gastan tareas fuera del limite real (rio, municipios vecinos).

Uso:
    # Asuncion completa, celdas de 1km, todas las categorias:
    python -m scripts.scrape_city --city "Asuncion" --cell-km 1 --output data/asuncion_tasks.jsonl

    # Otra ciudad / otro nivel administrativo:
    python -m scripts.scrape_city --city "Luque" --admin-level 8 --cell-km 2

Despues:
    python run.py --tasks-file data/asuncion_tasks.jsonl --db data/asuncion.db --no-resume
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Prints seguros con acentos aunque la consola sea cp1252 (Windows).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.config.loader import load_config
from src.models.grid import GridCell
from src.models.query_task import QueryTask
from src.utils.geo import km_to_degrees_lat, km_to_degrees_lng


async def fetch_polygon(dsn: str, city: str, admin_level):
    """Devuelve la fila del limite (poligono) mas grande que matchea el nombre."""
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchrow(
            """
            SELECT id, name, admin_level,
                   ST_AsGeoJSON(geom) AS gj,
                   ST_YMin(geom) AS lat_min, ST_YMax(geom) AS lat_max,
                   ST_XMin(geom) AS lng_min, ST_XMax(geom) AS lng_max,
                   ST_Area(geom::geography) / 1e6 AS area_km2
            FROM osm_limites
            WHERE unaccent(lower(name)) = unaccent(lower($1))
              AND GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON')
              AND ($2::text IS NULL OR admin_level = $2)
            ORDER BY ST_Area(geom) DESC
            LIMIT 1
            """,
            city, admin_level,
        )
    finally:
        await conn.close()


def main():
    p = argparse.ArgumentParser(description="Generar tareas para una ciudad recortada al poligono OSM")
    p.add_argument("--city", required=True, help="Nombre tal como figura en osm_limites (ej: Asuncion)")
    p.add_argument("--admin-level", default="8",
                   help="admin_level del limite (por defecto 8 = municipio/ciudad). "
                        "Usar 'any' para no filtrar por nivel.")
    p.add_argument("--cell-km", type=float, default=1.0, help="Tamano de cada celda base en km")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--output", default="data/city_tasks.jsonl")
    p.add_argument("--categories", default=None,
                   help="Lista separada por comas. Por defecto TODAS las del config.")
    args = p.parse_args()

    config = load_config(args.config)  # carga .env -> PG_DSN
    dsn = os.environ.get("PG_DSN", "")
    if not dsn:
        print("ERROR: PG_DSN no configurado (.env)")
        sys.exit(1)

    admin_level = None if args.admin_level.lower() in ("", "any", "none") else args.admin_level
    row = asyncio.run(fetch_polygon(dsn, args.city, admin_level))
    if not row:
        print(f"ERROR: no se encontro poligono para '{args.city}' (admin_level={admin_level}).")
        sys.exit(1)

    from shapely.geometry import shape, box
    from shapely.prepared import prep
    poly = prep(shape(json.loads(row["gj"])))

    cats = ([c.strip() for c in args.categories.split(",")]
            if args.categories else config.categories)

    lat_min, lat_max = row["lat_min"], row["lat_max"]
    lng_min, lng_max = row["lng_min"], row["lng_max"]
    lat_step = km_to_degrees_lat(args.cell_km)

    cells = []
    bbox_cells = 0
    lat = lat_min
    while lat < lat_max:
        lng_step = km_to_degrees_lng(args.cell_km, lat)
        lng = lng_min
        while lng < lng_max:
            cell = GridCell(lat_min=lat, lat_max=min(lat + lat_step, lat_max),
                            lng_min=lng, lng_max=min(lng + lng_step, lng_max))
            bbox_cells += 1
            # Recorte al poligono real: solo celdas que lo intersectan.
            if poly.intersects(box(cell.lng_min, cell.lat_min, cell.lng_max, cell.lat_max)):
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

    est_seq_h = len(tasks) * 25.3 / 3600
    workers = max(getattr(config, "workers", 1), 1)
    zoom = cells[0].estimate_zoom_level() if cells else "?"
    print(f"Ciudad:     {row['name']} (id={row['id']}, admin_level={row['admin_level']}, {row['area_km2']:.1f} km2)")
    print(f"bbox:       lat[{lat_min:.4f}, {lat_max:.4f}]  lng[{lng_min:.4f}, {lng_max:.4f}]")
    print(f"Celda:      {args.cell_km} km (zoom base {zoom})")
    print(f"Celdas:     {len(cells)} dentro del poligono (de {bbox_cells} del bbox)")
    print(f"Categorias: {len(cats)}")
    print(f"Tareas:     {len(tasks)}")
    print(f"Escrito a:  {out}")
    print(f"Estimado base (sin overflow): ~{est_seq_h:.1f} h secuencial / "
          f"~{est_seq_h / workers:.1f} h con {workers} workers")


if __name__ == "__main__":
    main()
