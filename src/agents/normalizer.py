import logging
import re
from typing import Optional, List

from ..models.business import NormalizedBusiness

logger = logging.getLogger(__name__)

CATEGORY_MAP = {
    "restaurante": "restaurante",
    "restaurant": "restaurante",
    "supermercado": "supermercado",
    "supermarket": "supermercado",
    "farmacia": "farmacia",
    "pharmacy": "farmacia",
    "estacion de servicio": "estacion de servicio",
    "gas station": "estacion de servicio",
    "hotel": "hotel",
    "hospital": "hospital",
    "clinica": "clinica",
    "colegio": "colegio",
    "school": "colegio",
    "universidad": "universidad",
    "university": "universidad",
    "banco": "banco",
    "bank": "banco",
    "mecanico": "mecanico",
    "tienda de ropa": "tienda de ropa",
    "clothing store": "tienda de ropa",
    "ferreteria": "ferreteria",
    "hardware store": "ferreteria",
    "veterinaria": "veterinaria",
    "gimnasio": "gimnasio",
    "gym": "gimnasio",
    "peluqueria": "peluqueria",
    "panaderia": "panaderia",
    "bakery": "panaderia",
    "carniceria": "carniceria",
    "verduleria": "verduleria",
    "libreria": "libreria",
    "bookstore": "libreria",
}


class Normalizer:
    def __init__(self, config):
        self.config = config

    def normalize(self, raw: dict) -> Optional[NormalizedBusiness]:
        name = self._clean_name(raw.get("name", ""))
        if not name:
            return None

        lat, lng = self._extract_coords(raw)
        if lat is None or lng is None:
            return None

        category = self._normalize_category(raw.get("category", "Sin categoria"))

        return NormalizedBusiness(
            name=name,
            lat=lat,
            lng=lng,
            category=category,
            search_category=raw.get("search_category", ""),
            address=raw.get("address"),
            phone=raw.get("phone"),
            website=raw.get("website"),
            rating=raw.get("rating"),
            review_count=raw.get("review_count"),
            source_url=raw.get("source_url"),
            google_place_id=raw.get("google_place_id"),
            raw_name=raw.get("name"),
            metadata=raw.get("metadata"),
        )

    def normalize_batch(self, raw_list: List[dict]) -> List[NormalizedBusiness]:
        result = []
        for raw in raw_list:
            normalized = self.normalize(raw)
            if normalized:
                result.append(normalized)
        return result

    def _clean_name(self, name: str) -> str:
        name = name.strip()
        name = re.sub(r'\s+', ' ', name)
        name = re.sub(r'^["\']|["\']$', '', name)
        return name

    def _extract_coords(self, raw: dict) -> tuple:
        lat = raw.get("lat")
        lng = raw.get("lng")
        if lat is not None and lng is not None:
            try:
                return float(lat), float(lng)
            except (ValueError, TypeError):
                pass

        url = raw.get("source_url", "")
        if url:
            coords = self._extract_coords_from_url(url)
            if coords:
                return coords

        return None, None

    @staticmethod
    def _extract_coords_from_url(url: str) -> Optional[tuple]:
        match = re.search(r'/@(-?\d+\.\d+),(-?\d+\.\d+),\d+z', url)
        if match:
            return float(match.group(1)), float(match.group(2))
        match = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', url)
        if match:
            return float(match.group(1)), float(match.group(2))
        return None

    def _normalize_category(self, category: str) -> str:
        cat_lower = category.lower().strip()
        for key, value in CATEGORY_MAP.items():
            if key in cat_lower:
                return value
        return cat_lower if cat_lower else "Sin categoria"
