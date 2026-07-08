import logging
from typing import List

from ..models.business import NormalizedBusiness
from ..utils.geo import haversine_distance

logger = logging.getLogger(__name__)


class Deduplicator:
    def __init__(self, config):
        self.proximity_threshold = config.dedup.proximity_threshold_meters
        self.use_spatial_hash = config.dedup.use_spatial_hash

    async def deduplicate(
        self,
        businesses: List[NormalizedBusiness],
    ) -> List[NormalizedBusiness]:
        if len(businesses) <= 1:
            return businesses

        businesses = self._dedup_by_exact_name(businesses)
        businesses = self._dedup_by_proximity(businesses)
        return businesses

    def _dedup_by_exact_name(
        self, businesses: List[NormalizedBusiness]
    ) -> List[NormalizedBusiness]:
        groups: dict = {}
        for b in businesses:
            key = b.name.lower().strip()
            if key not in groups:
                groups[key] = []
            groups[key].append(b)

        result = []
        for group in groups.values():
            if len(group) == 1:
                result.append(group[0])
            else:
                best = max(group, key=lambda x: len(x.name))
                result.append(best)
        return result

    def _dedup_by_proximity(
        self, businesses: List[NormalizedBusiness]
    ) -> List[NormalizedBusiness]:
        if len(businesses) <= 1:
            return businesses

        if self.use_spatial_hash:
            return self._spatial_hash_dedup(businesses)
        return self._pairwise_dedup(businesses)

    def _spatial_hash_dedup(
        self, businesses: List[NormalizedBusiness]
    ) -> List[NormalizedBusiness]:
        cell_size = 0.001
        grid: dict = {}
        index_map: dict = {}

        for i, b in enumerate(businesses):
            cell = (int(b.lat / cell_size), int(b.lng / cell_size))
            if cell not in grid:
                grid[cell] = []
            grid[cell].append(i)
            index_map[i] = cell

        seen: set = set()
        result = []

        for i, b in enumerate(businesses):
            if i in seen:
                continue

            cluster = [b]
            seen.add(i)
            cell = index_map[i]

            for dlat in (-1, 0, 1):
                for dlng in (-1, 0, 1):
                    neighbor = (cell[0] + dlat, cell[1] + dlng)
                    for j in grid.get(neighbor, []):
                        if j not in seen:
                            dist = haversine_distance(b.lat, b.lng, businesses[j].lat, businesses[j].lng)
                            if dist < self.proximity_threshold:
                                cluster.append(businesses[j])
                                seen.add(j)

            best = max(cluster, key=lambda x: len(x.name))
            result.append(best)

        return result

    def _pairwise_dedup(
        self, businesses: List[NormalizedBusiness]
    ) -> List[NormalizedBusiness]:
        seen: set = set()
        result = []
        for i, b in enumerate(businesses):
            if i in seen:
                continue
            cluster = [b]
            seen.add(i)
            for j in range(i + 1, len(businesses)):
                if j not in seen:
                    dist = haversine_distance(b.lat, b.lng, businesses[j].lat, businesses[j].lng)
                    if dist < self.proximity_threshold:
                        cluster.append(businesses[j])
                        seen.add(j)
            best = max(cluster, key=lambda x: len(x.name))
            result.append(best)
        return result
