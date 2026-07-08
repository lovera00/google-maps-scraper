from pydantic import BaseModel, Field
from typing import List, Optional


class BoundingBox(BaseModel):
    lat_min: float
    lat_max: float
    lng_min: float
    lng_max: float


class GridConfig(BaseModel):
    initial_size_km: float = 5.0
    max_depth: int = 5
    bounding_box: BoundingBox


class RateLimitConfig(BaseModel):
    request_delay_seconds: float = 3.0  # fallback si no se define el rango
    request_delay_min_seconds: Optional[float] = None
    request_delay_max_seconds: Optional[float] = None
    max_retries: int = 3
    retry_backoff_base: float = 2.0
    max_scroll_iterations: int = 50


class DedupConfig(BaseModel):
    proximity_threshold_meters: float = 50.0
    batch_size: int = 100
    use_spatial_hash: bool = True


class StorageConfig(BaseModel):
    batch_size: int = 50
    update_existing: bool = True


class DatabaseConfig(BaseModel):
    path: str = "data/paraguay_businesses.db"
    wal_mode: bool = True


class PostgresConfig(BaseModel):
    dsn: str = ""
    enabled: bool = False


class ProxyConfig(BaseModel):
    enabled: bool = False
    source: str = "file"
    file_path: str = "data/proxies.txt"
    rotation_strategy: str = "round_robin"
    max_failures: int = 3
    cooldown_minutes: int = 10
    health_check_url: str = "https://www.google.com"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/scraper.log"
    max_file_size_mb: int = 50
    backup_count: int = 5
    format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class MockConfig(BaseModel):
    html_directory: str = "mocks/google_maps"
    simulate_delay: bool = True
    mock_delay_seconds: float = 0.3


class PriorityCity(BaseModel):
    name: str
    lat: float
    lng: float


class OsmConfig(BaseModel):
    data_dir: str = "data/osm"
    download_url: str = "https://download.geofabrik.de/south-america/paraguay-latest.osm.pbf"
    download_retries: int = 3
    download_backoff: float = 2.0
    frente_avenida_threshold_m: float = 120.0
    cerca_agua_threshold_m: float = 400.0
    h3_resolution: int = 9


class Settings(BaseModel):
    test_mode: bool = False
    headless: bool = True
    workers: int = 2
    database: DatabaseConfig = DatabaseConfig()
    postgres: PostgresConfig = PostgresConfig()
    grid: GridConfig
    categories: List[str]
    rate_limit: RateLimitConfig = RateLimitConfig()
    dedup: DedupConfig = DedupConfig()
    storage: StorageConfig = StorageConfig()
    proxies: ProxyConfig = ProxyConfig()
    logging: LoggingConfig = LoggingConfig()
    mock: MockConfig = MockConfig()
    priority_cities: List[PriorityCity] = []
    osm: OsmConfig = OsmConfig()

    def model_post_init(self, _ctx):
        import os
        env_dsn = os.environ.get("PG_DSN", "")
        if env_dsn:
            self.postgres.dsn = env_dsn
            self.postgres.enabled = True
