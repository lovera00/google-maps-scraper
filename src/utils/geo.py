from math import radians, sin, cos, sqrt, atan2
from typing import List, Tuple


def haversine_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distancia en metros entre dos puntos geograficos (formula de Haversine)."""
    R = 6_371_000
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c


def km_to_degrees_lat(km: float) -> float:
    """Convierte kilometros a grados de latitud aproximados."""
    return km / 111.32


def km_to_degrees_lng(km: float, lat: float) -> float:
    """Convierte kilometros a grados de longitud aproximados en una latitud dada."""
    return km / (111.32 * cos(radians(lat)))


def point_in_bbox(lat: float, lng: float, lat_min: float, lat_max: float,
                  lng_min: float, lng_max: float) -> bool:
    """Verifica si un punto esta dentro de un bounding box."""
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def bbox_center(lat_min: float, lat_max: float, lng_min: float, lng_max: float) -> Tuple[float, float]:
    """Retorna el centro de un bounding box."""
    return (lat_min + lat_max) / 2, (lng_min + lng_max) / 2
