from dataclasses import dataclass, field
from .grid import GridCell


@dataclass(slots=True)
class QueryTask:
    grid_cell: GridCell
    category: str
    depth: int = 0
    retry_count: int = 0
    priority: float = 0.0

    @property
    def center_lat(self) -> float:
        return self.grid_cell.center_lat

    @property
    def center_lng(self) -> float:
        return self.grid_cell.center_lng

    def to_maps_url(self) -> str:
        import urllib.parse
        center_lat = self.grid_cell.center_lat
        center_lng = self.grid_cell.center_lng
        zoom = self.grid_cell.estimate_zoom_level()
        query = urllib.parse.quote(self.category)
        return (
            f"https://www.google.com/maps/search/{query}/"
            f"@{center_lat:.6f},{center_lng:.6f},{zoom}z"
        )

    def to_dict(self) -> dict:
        return {
            "grid_cell": self.grid_cell.to_dict(),
            "category": self.category,
            "depth": self.depth,
            "retry_count": self.retry_count,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QueryTask":
        return cls(
            grid_cell=GridCell.from_dict(d["grid_cell"]),
            category=d["category"],
            depth=d.get("depth", 0),
            retry_count=d.get("retry_count", 0),
            priority=d.get("priority", 0.0),
        )
