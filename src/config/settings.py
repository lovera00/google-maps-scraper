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
    # E4: en una corrida nueva, sembrar pre-subdivididas a esta profundidad SOLO
    # las celdas×categoria que ya saturaron el cap en corridas previas (historial
    # en scraping_tasks). Evita re-scrapear los ancestros condenados al overflow.
    # 0 = desactivado. 1 = seguro (coincide con lo que el overflow haria igual).
    overflow_seed_depth: int = 1


class RateLimitConfig(BaseModel):
    request_delay_seconds: float = 3.0  # fallback si no se define el rango
    request_delay_min_seconds: Optional[float] = None
    request_delay_max_seconds: Optional[float] = None
    max_retries: int = 3               # reintentos ante crash de navegador
    retry_backoff_base: float = 2.0
    max_scroll_iterations: int = 50
    # --- Resiliencia ante bloqueo de Google (E2) ---
    # Reintentos por tarea ante fallo transitorio (timeout de navegacion, etc.)
    # antes de marcarla failed. Sobreviven al resume via retry_count del task.
    max_task_retries: int = 3
    # Pausa global escalante ante deteccion de bloqueo: base * 2^(n-1), capada.
    max_consecutive_blocks: int = 5
    block_backoff_base_seconds: float = 300.0    # 5 min
    block_backoff_max_seconds: float = 3600.0    # 1 h


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


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    min_level: str = "warning"
    notify_on_start: bool = True
    notify_on_complete: bool = True


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
    workers: int = 4          # workers de scraping concurrentes (1 browser, N contexts)
    # Tope de adelanto del productor de tareas: mantiene la task_queue por debajo
    # de este tamaño para acotar la RAM (no cargar el shard entero en memoria).
    task_queue_high_water: int = 5000
    # Reciclar el navegador (teardown+setup, con playwright.stop()) cada N tareas
    # para liberar la memoria que Playwright retiene del lado Python. 0 = nunca.
    browser_recycle_interval: int = 500
    # Bloquear descarga de imagenes/media/fonts en modo live (ahorra ancho de
    # banda y tiempo; NO bloquea CSS/JS/XHR de los que depende el feed).
    block_resources: bool = True
    # Guardar el HTML crudo de cada scrape live en data/debug_html/ (diagnostico
    # de selectores). Escribe MBs sync por tarea en el event loop: default off.
    save_debug_html: bool = False
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
    telegram: TelegramConfig = TelegramConfig()
    osm: OsmConfig = OsmConfig()

    def model_post_init(self, _ctx):
        import os
        env_dsn = os.environ.get("PG_DSN", "")
        if env_dsn:
            self.postgres.dsn = env_dsn
            self.postgres.enabled = True
        env_tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if env_tg_token:
            self.telegram.bot_token = env_tg_token
        env_tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        if env_tg_chat:
            self.telegram.chat_id = env_tg_chat
