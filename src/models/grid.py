from dataclasses import dataclass
from typing import List
from math import cos, radians


@dataclass(slots=True)
class GridCell:
    lat_min: float
    lat_max: float
    lng_min: float
    lng_max: float

    @property
    def center_lat(self) -> float:
        return (self.lat_min + self.lat_max) / 2

    @property
    def center_lng(self) -> float:
        return (self.lng_min + self.lng_max) / 2

    @property
    def lat_span(self) -> float:
        return self.lat_max - self.lat_min

    @property
    def lng_span(self) -> float:
        return self.lng_max - self.lng_min

    def subdivide_quadrant(self) -> List["GridCell"]:
        mid_lat = (self.lat_min + self.lat_max) / 2
        mid_lng = (self.lng_min + self.lng_max) / 2
        return [
            GridCell(self.lat_min, mid_lat, self.lng_min, mid_lng),
            GridCell(self.lat_min, mid_lat, mid_lng, self.lng_max),
            GridCell(mid_lat, self.lat_max, self.lng_min, mid_lng),
            GridCell(mid_lat, self.lat_max, mid_lng, self.lng_max),
        ]

    def area_km2(self) -> float:
        avg_lat = radians(abs(self.center_lat))
        km_per_deg_lat = 111.32
        km_per_deg_lng = 111.32 * cos(avg_lat)
        width_km = self.lat_span * km_per_deg_lat
        height_km = self.lng_span * km_per_deg_lng
        return abs(width_km * height_km)

    def estimate_zoom_level(self) -> int:
        area = self.area_km2()
        if area > 500:
            return 10
        elif area > 100:
            return 12
        elif area > 25:
            return 14
        elif area > 5:
            return 15
        elif area > 1:
            return 16
        else:
            return 17

    def __hash__(self):
        return hash((self.lat_min, self.lat_max, self.lng_min, self.lng_max))

    def to_dict(self) -> dict:
        return {
            "lat_min": self.lat_min,
            "lat_max": self.lat_max,
            "lng_min": self.lng_min,
            "lng_max": self.lng_max,
        }

    def to_json(self) -> str:
        import json
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "GridCell":
        return cls(
            lat_min=d["lat_min"],
            lat_max=d["lat_max"],
            lng_min=d["lng_min"],
            lng_max=d["lng_max"],
        )


@dataclass
class BoundingBox:
    lat_min: float
    lat_max: float
    lng_min: float
    lng_max: float
