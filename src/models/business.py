from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class NormalizedBusiness:
    name: str
    lat: float
    lng: float
    category: str
    search_category: str = ""  # la categoria con la que se busco (ej. "restaurantes")
    address: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    source_url: Optional[str] = None
    google_place_id: Optional[str] = None
    raw_name: Optional[str] = None
    metadata: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lat": self.lat,
            "lng": self.lng,
            "category": self.category,
            "search_category": self.search_category,
        }
