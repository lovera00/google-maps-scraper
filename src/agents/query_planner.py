import json
import logging
from pathlib import Path
from typing import List, Optional

from shapely.geometry import box, shape
from shapely.prepared import PreparedGeometry, prep

from ..models.grid import GridCell, BoundingBox
from ..models.query_task import QueryTask
from ..utils.geo import km_to_degrees_lat, km_to_degrees_lng, bbox_center, haversine_distance

logger = logging.getLogger(__name__)

class QueryPlanner:
    def __init__(self, config):
        grid_cfg = config.grid
        self.bounding_box = BoundingBox(
            lat_min=grid_cfg.bounding_box.lat_min,
            lat_max=grid_cfg.bounding_box.lat_max,
            lng_min=grid_cfg.bounding_box.lng_min,
            lng_max=grid_cfg.bounding_box.lng_max,
        )
        self.initial_grid_size_km = grid_cfg.initial_size_km
        self.max_depth = grid_cfg.max_depth
        self.overflow_seed_depth = getattr(grid_cfg, "overflow_seed_depth", 0)
        self.categories = config.categories
        self.district_locations = self._load_districts()
        self.boundary = self._load_boundary()

    def generate_initial_tasks(self, overflow_keys: set = None) -> List[QueryTask]:
        """Genera una tarea por (celda, categoria).

        (E4) Si `overflow_keys` (conjunto de (grid_cell_json, category) que
        saturaron el cap en corridas previas) no esta vacio y
        overflow_seed_depth > 0, esas celdas se emiten ya subdivididas a
        `overflow_seed_depth` en vez de a depth 0, para no re-scrapear el
        ancestro que se sabe condenado al overflow.
        """
        overflow_keys = overflow_keys or set()
        cells = self._grid_cells()
        tasks = []
        seeded = 0
        for cell in cells:
            cell_json = cell.to_json()
            for category in self.categories:
                if (self.overflow_seed_depth > 0
                        and (cell_json, category) in overflow_keys):
                    for sc in self._expand_to_depth(cell, self.overflow_seed_depth):
                        tasks.append(QueryTask(
                            grid_cell=sc,
                            category=category,
                            depth=self.overflow_seed_depth,
                            priority=self._calculate_priority(sc),
                        ))
                    seeded += 1
                else:
                    tasks.append(QueryTask(
                        grid_cell=cell,
                        category=category,
                        depth=0,
                        priority=self._calculate_priority(cell),
                    ))
        tasks.sort(key=lambda t: t.priority)
        msg = (f"QueryPlanner: {len(tasks)} tareas iniciales generadas "
               f"({len(cells)} celdas x {len(self.categories)} categorias)")
        if seeded:
            msg += (f"; {seeded} celda-categorias sembradas pre-subdivididas "
                    f"a depth {self.overflow_seed_depth} por overflow previo")
        logger.info(msg)
        return tasks

    def _expand_to_depth(self, cell: GridCell, depth: int) -> List[GridCell]:
        """Subdivide una celda `depth` veces (4^depth subceldas), recortando las
        que caen fuera de la frontera de Paraguay."""
        current = [cell]
        for _ in range(depth):
            nxt = []
            for c in current:
                nxt.extend(sc for sc in c.subdivide_quadrant()
                           if self._cell_in_paraguay(sc))
            current = nxt
        return current

    def handle_overflow(self, task: QueryTask) -> List[QueryTask]:
        if task.depth >= self.max_depth:
            logger.warning(f"Profundidad maxima alcanzada para celda {task.grid_cell}")
            return []
        subcells = [sc for sc in task.grid_cell.subdivide_quadrant() if self._cell_in_paraguay(sc)]
        new_tasks = []
        for sc in subcells:
            priority = self._calculate_priority(sc)
            new_tasks.append(QueryTask(
                grid_cell=sc,
                category=task.category,
                depth=task.depth + 1,
                priority=priority,
            ))
        logger.info(f"Overflow: subdividiendo celda en {len(new_tasks)} sub-tareas")
        return new_tasks

    def _load_boundary(self) -> Optional[PreparedGeometry]:
        """Carga el poligono real de Paraguay desde data/paraguay_boundary.geojson.

        Se usa para recortar el grid rectangular a la frontera real del pais,
        evitando generar tareas sobre territorio de Argentina/Brasil/Bolivia.
        Si el archivo no existe, no se recorta (comportamiento anterior).
        """
        geojson_path = Path("data/paraguay_boundary.geojson")
        if not geojson_path.exists():
            logger.warning(
                f"QueryPlanner: {geojson_path} no encontrado, "
                "el grid NO se recortara a la frontera real de Paraguay"
            )
            return None
        feature = json.loads(geojson_path.read_text(encoding="utf-8"))
        polygon = shape(feature["geometry"])
        logger.info(f"QueryPlanner: frontera de Paraguay cargada desde {geojson_path}")
        return prep(polygon)

    def _cell_in_paraguay(self, cell: GridCell) -> bool:
        if self.boundary is None:
            return True
        cell_box = box(cell.lng_min, cell.lat_min, cell.lng_max, cell.lat_max)
        return self.boundary.intersects(cell_box)

    def _load_districts(self) -> list[tuple[float, float]]:
        """Carga los distritos desde paraguay_cities.json. Si no existe, usa config."""
        json_path = Path("data/paraguay_cities.json")
        if json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            points = [(d["lat"], d["lng"]) for d in data]
            logger.info(f"QueryPlanner: {len(points)} distritos cargados desde {json_path}")
            return points
        # Fallback al config viejo (4 ciudades)
        from ..config.loader import load_config
        logger.warning(f"QueryPlanner: {json_path} no encontrado, usando priority_cities del config")
        return []

    def _min_dist_to_district(self, lat: float, lng: float,
                               locations: list = None) -> float:
        """Distancia minima en metros a cualquier punto de la lista dada."""
        pts = locations or self.district_locations
        if not pts:
            return 0.0
        if len(pts[0]) == 3:
            pts = [(dlat, dlng) for _, dlat, dlng in pts]
        return min(haversine_distance(lat, lng, dlat, dlng) for dlat, dlng in pts)

    def _grid_cells(self) -> List[GridCell]:
        bb = self.bounding_box
        cells = []
        skipped = 0
        lat_step = km_to_degrees_lat(self.initial_grid_size_km)
        lat = bb.lat_min
        while lat < bb.lat_max:
            lng_step = km_to_degrees_lng(self.initial_grid_size_km, lat)
            lng = bb.lng_min
            while lng < bb.lng_max:
                cell = GridCell(
                    lat_min=lat,
                    lat_max=min(lat + lat_step, bb.lat_max),
                    lng_min=lng,
                    lng_max=min(lng + lng_step, bb.lng_max),
                )
                if self._cell_in_paraguay(cell):
                    cells.append(cell)
                else:
                    skipped += 1
                lng += lng_step
            lat += lat_step
        if self.boundary is not None:
            logger.info(
                f"QueryPlanner: {skipped} celdas descartadas por caer fuera "
                f"de la frontera real de Paraguay ({len(cells)} celdas utiles)"
            )
        return cells

    def find_cell_for_point(self, lat: float, lng: float) -> GridCell:
        """Devuelve la GridCell exacta (misma grilla que _grid_cells) que contiene un punto.

        Reproduce el mismo stepping fila por fila que usa _grid_cells(), sin
        recorrer todo el grid: calcula directamente el indice de fila/columna.
        """
        bb = self.bounding_box
        if not (bb.lat_min <= lat <= bb.lat_max and bb.lng_min <= lng <= bb.lng_max):
            raise ValueError(
                f"Punto ({lat}, {lng}) fuera del bounding box configurado "
                f"(lat {bb.lat_min}..{bb.lat_max}, lng {bb.lng_min}..{bb.lng_max})"
            )

        lat_step = km_to_degrees_lat(self.initial_grid_size_km)
        row_index = int((lat - bb.lat_min) / lat_step)
        row_lat_min = bb.lat_min + row_index * lat_step
        row_lat_max = min(row_lat_min + lat_step, bb.lat_max)

        lng_step = km_to_degrees_lng(self.initial_grid_size_km, row_lat_min)
        col_index = int((lng - bb.lng_min) / lng_step)
        col_lng_min = bb.lng_min + col_index * lng_step
        col_lng_max = min(col_lng_min + lng_step, bb.lng_max)

        return GridCell(
            lat_min=row_lat_min, lat_max=row_lat_max,
            lng_min=col_lng_min, lng_max=col_lng_max,
        )

    # Ciudades de Gran Asunción para boost de prioridad
    _GRAN_ASUNCION = [
        ("Asuncion", -25.282, -57.635),
        ("San Lorenzo", -25.340, -57.509),
        ("Luque", -25.270, -57.487),
        ("Fernando de la Mora", -25.323, -57.555),
        ("Lambare", -25.330, -57.640),
        ("Limpio", -25.170, -57.490),
        ("Mariano Roque Alonso", -25.210, -57.540),
        ("Nemby", -25.395, -57.537),
        ("San Antonio", -25.382, -57.606),
        ("Villa Elisa", -25.367, -57.624),
        ("Capiata", -25.356, -57.440),
        ("Itaugua", -25.391, -57.326),
        ("Aregua", -25.312, -57.384),
    ]
    _GRAN_ASUNCION_BOOST_KM = 25.0

    def _calculate_priority(self, cell: GridCell) -> float:
        if not self.district_locations:
            return 0.0
        base = self._min_dist_to_district(cell.center_lat, cell.center_lng)
        boost = self._min_dist_to_district(cell.center_lat, cell.center_lng, self._GRAN_ASUNCION)
        # Si esta dentro del radio boost de Gran Asunción, prioridad mucho menor = mas prioritario
        if boost <= self._GRAN_ASUNCION_BOOST_KM * 1000:
            base *= 0.1
        return round(base, 2)
