# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Multi-agent Google Maps scraping system for Paraguay. Scrapes business data (name, lat, lng, category) from Google Maps web interface — **no Google Places API used**. Playwright simulates a real browser; HTML mocks enable testing without hitting Google.

## Commands

```bash
# Set PostgreSQL DSN before running (optional, for dual-write persistence)
export PG_DSN="postgresql://user:pass@host:port/db"

python run.py                          # Full pipeline (requires Playwright)
python run.py --test-mode              # Pipeline using local mock HTML files (no browser)
python run.py --config config.yaml     # Custom config path (copy from config.example.yaml)
python run.py --tasks-file data/tasks.jsonl  # Distributed: consume pre-generated tasks
python run.py --db data/custom.db      # Custom SQLite path
python run.py --no-resume              # Ignore prior progress, start fresh

pytest tests/ -v                       # All tests
pytest tests/test_deduplicator.py -v   # Single test file

python -m mocks.generator --all        # Capture real HTML from Google Maps as mocks
python -m mocks.generator --url "..."  # Capture a single URL

# Distributed scraping workflow
python -m scripts.task_generator --config config.yaml --output data/tasks.jsonl
python -m scripts.task_sharder --input data/tasks.jsonl --shards 5 --output-dir data/shards/
python -m scripts.db_merge --inputs data/shard_*.db --output data/paraguay_businesses.db
python -m scripts.fetch_paraguay_districts  # Update cities.json from Wikipedia+Nominatim

# Category extraction validation
python scripts/validate_categories.py --mock          # Test with local mock HTML
python scripts/validate_categories.py --all            # All debug_html/ files
python scripts/validate_categories.py --all --verbose  # Show per-result strategy

# Maintenance scripts
python scripts/repair_categories.py --limit 50         # Re-scrape 50 cells to fill missing categories
python scripts/repair_categories.py --dry-run          # Preview without modifying DB
python scripts/flush_clean_to_pg.py                    # Bulk INSERT categorized rows from SQLite to PG
python scripts/flush_clean_to_pg.py --dry-run          # Preview category distribution only
```

## Architecture

5 agents connected by `asyncio.Queue` pipelines. Each agent is a coroutine consuming from an input queue and producing to an output queue, run concurrently via `asyncio.gather()` in `src/orchestrator.py`.

```
QueryPlanner -> task_queue -> DataCollector -> raw_queue -> Normalizer
  -> normalized_queue -> Deduplicator -> final_queue -> Storage -> SQLite
```

**Feedback loop**: If DataCollector returns >=120 results for a grid cell (Google Maps limit), the cell is subdivided into 4 quadrants and new tasks are re-injected into `task_queue`. Max subdivision depth is `grid.max_depth` (default 5).

### Agent 1 — QueryPlanner (`src/agents/query_planner.py`)
Divides Paraguay bounding box into a grid (default 5km cells), then generates `QueryTask = GridCell + category` for every cell × category. Tasks are sorted by proximity to priority cities (Asuncion, CDE, etc.) so urban areas are scraped first. `handle_overflow()` subdivides cells that hit the 120-result cap.

### Agent 2 — DataCollector (`src/agents/data_collector.py`)
Dual-mode: `test_mode=True` loads local HTML from `mocks/google_maps/`; test_mode=False launches Playwright Chromium. In live mode: dismisses consent popups, scrolls the results feed, extracts raw dicts via BeautifulSoup. Coordinates are extracted from two URL patterns: `@lat,lng,z` and `!3dlat!4dlng` (fallback). Category is read from elements with `W4Efsd` class. Google Place ID is extracted from the `data=` URL parameter. Browser crash recovery: if the page/context is closed mid-scrape, the collector tears down and re-launches the browser then retries.

Mock HTML files use Google Maps DOM structure: `[role="feed"]` containers, `<a href="/maps/place/...@lat,lng,z">` cards with `aria-label` for names and `W4Efsd` spans for categories. A `<div class="overflow-simulated">120</div>` element triggers the overflow feedback loop in test mode.

### Agent 3 — Normalizer (`src/agents/normalizer.py`)
Cleans names (strips whitespace, normalizes spaces), validates lat/lng, maps Spanish/English category labels to normalized rubros via `CATEGORY_MAP` dict. Falls back to extracting coordinates from `source_url` if not provided directly. Discards entries with empty names or missing coordinates.

### Agent 4 — Deduplicator (`src/agents/deduplicator.py`)
Two-pass dedup within each batch:
1. Exact name match (case-insensitive) — keeps the entry with the longest name
2. Spatial proximity <50m using Haversine distance with spatial hash grid (0.001° cells ≈ 111m) for O(n) performance

### Agent 5 — Storage (`src/agents/storage.py`)
SQLite via aiosqlite (WAL mode) as primary store. Optional PostgreSQL dual-write via asyncpg — if `postgres.enabled` is true and the DSN is reachable, every insert batch is also written to PG. During graceful shutdown (SIGINT/SIGTERM), all remaining queue items are drained and a full SQLite→PostgreSQL bulk flush runs.

**Upsert matching** (both SQLite and PG repos use the same 3-tier strategy):
1. Match by `google_place_id` (Google's canonical FTID from the `data=` URL param)
2. Match by `source_url` (full Maps URL)
3. Match by `LOWER(name)` + proximity (<100m Haversine)

On match: update fields (category, rating, etc.) and fill in missing `source_url`/`google_place_id` via COALESCE. No match: insert new row. Exports results to JSON at `data/output.json` on pipeline completion.

## Key design decisions

- **SQLite + PostgreSQL**: SQLite is the primary store (WAL mode) used during the entire run. PostgreSQL (`postgres.enabled: true` + `PG_DSN` env var) is only used at shutdown: all SQLite data is bulk-flushed to PG via `flush_to_postgres()` with a 30s timeout and reconnection logic. PostgreSQL is never touched during normal execution to avoid unnecessary resource consumption.
- **asyncio.Queue over Celery/RabbitMQ**: no external infra. In-process queues are fast and simple.
- **Playwright over Selenium**: native async API, better stealth via `playwright-stealth`.
- **Pydantic settings**: `config.yaml` is validated at load time by `src/config/settings.py`. The `Settings` model is the single source of truth for all configuration.
- **Mock mode**: `DataCollector` checks `self.test_mode` and dispatches to `_scrape_mock()` vs `_scrape_live()`. Mock routing maps categories to HTML files in `_load_mock_html()`.
- **Rate limiting**: `RateLimiter` class enforces `request_delay_seconds` between live requests. `RetryHandler` with exponential backoff for transient failures.
- **Proxy support**: Optional proxy rotation via `src/proxy/manager.py` — file-based proxy list, round-robin rotation, health checks, cooldown on failure. Disabled by default (`proxies.enabled: false`).
- **Pause/resume**: The `scraping_tasks` audit table tracks every task as pending→in_progress→completed/failed. On restart with resume (default), completed tasks are skipped and interrupted tasks (pending/in_progress) are re-queued. Use `--no-resume` to start fresh.

## Distributed scraping

For large-scale runs (>100K tasks), the pipeline supports distributed execution across multiple machines:

1. **Generate** all tasks: `python -m scripts.task_generator` writes every grid-cell×category combination to a JSONL file
2. **Shard** tasks: `python -m scripts.task_sharder` splits the JSONL into N balanced chunks — one per worker machine
3. **Run** each shard independently: `python run.py --tasks-file data/shards/shard_001.jsonl --db data/shard_001.db` — each worker gets its own SQLite DB
4. **Merge** results: `python -m scripts.db_merge` reads all shard DBs, runs global re-deduplication (name + spatial), and writes the unified DB

Tasks in JSONL are serialized as `QueryTask.to_dict()` with `grid_cell`, `category`, `depth`, `retry_count`, and `priority` fields.

## Database schema

Table `businesses`: `id, name, lat, lng, category, search_category, address, phone, website, rating, review_count, source_url, google_place_id, raw_name, metadata (JSON), created_at, updated_at, is_active`. Indexes on `name` (NOCASE), `(lat, lng)`, `category`, `source_url` (UNIQUE WHERE NOT NULL), `google_place_id` (UNIQUE WHERE NOT NULL).

Table `scraping_tasks`: audit log with grid cell, category, depth, status, results count, timestamps. Supports pause/resume.

## Important patterns

- All tests live under `tests/` and use the shared `config` fixture from `conftest.py` which forces `test_mode=True`.
- Storage tests use `tmp_path` for isolated databases.
- `ModelName` classes in `src/models/` are plain Python dataclasses, not Pydantic.
- Config classes in `src/config/settings.py` ARE Pydantic BaseModels with strict validation.
- The orchestrator sends `None` as a sentinel value through each queue to signal end-of-stream to downstream agents.
- Grid cell subdivision is quadrant-based (4 equal subcells per division), not adaptive.
- `GridCell.to_json()` uses `sort_keys=True` for deterministic serialization — used as task keys in the DB.
- **Always use `encoding="utf-8"`** when calling `open()` or `read_text()`/`write_text()` in Python. On Windows the default is cp1252, which corrupts Spanish characters (accented letters, ñ).
- `.env` is auto-loaded by `src/config/loader.py` at startup — no need for `python-dotenv`. `PG_DSN` is read from the environment after `.env` is loaded.
- `config.yaml` is always read with `encoding="utf-8"` by the config loader — the `categories` list contains accented Spanish words that will break if opened without explicit UTF-8.

## OSM module

Independent ETL module that downloads Paraguay OSM data, loads 5 layers into PostGIS, and computes distance features per H3 cell for radius analysis (used by the webapp).

### Commands

```bash
python scripts/osm_download.py                    # Download paraguay-latest.osm.pbf from Geofabrik
python scripts/osm_download.py --dry-run          # Show URL and destination without downloading

python scripts/osm_load.py                        # Extract 5 layers + load to PostGIS (staging swap)
python scripts/osm_load.py --dry-run              # Extract without DB load
python scripts/osm_load.py --pbf path/to/file.pbf # Use specific PBF file

python scripts/osm_build_features.py              # Compute distance features for all 3.6M H3 cells
python scripts/osm_build_features.py --limit 1000 # Test with first 1000 cells
python scripts/osm_build_features.py --dry-run    # Show cell count without processing

python scripts/osm_query.py --lat -25.3 --lng -57.6           # nearest_road + locate_point
python scripts/osm_query.py --lat -25.3 --lng -57.6 --road    # Only nearest road
python scripts/osm_query.py --lat -25.3 --lng -57.6 --locate  # Only locate point
```

### Full pipeline

```bash
python scripts/osm_download.py && python scripts/osm_load.py && python scripts/osm_build_features.py
```

### Layers and tables

| Layer | Table | Geometry | Key tags |
|---|---|---|---|
| vias | osm_vias | LineString | name, highway |
| parques | osm_parques | Polygon | name, leisure |
| agua | osm_agua | Geometry | name, natural, waterway |
| equipamiento | osm_equipamiento | Point | name, amenity |
| limites | osm_limites | Geometry | name, admin_level, place |
| features | osm_features_r9 | — | h3 (PK), 5 distances, 2 boolean flags |

### Architecture

- `src/osm/extract.py` — pyosmium handler reads PBF in a single pass with `locations=True`, builds GeoJSON geometries for 5 layers using `osmium.geom.GeoJSONFactory`
- `src/osm/load.py` — asyncpg batch INSERT via `ST_GeomFromGeoJSON`, staging-table swap for idempotent reloads, `osm_meta` tracking
- `src/osm/queries.py` — `nearest_road()` and `locate_point()` point-in-polygon/fallback queries
- H3 indices computed in Python via `h3.latlng_to_cell()` from `population_cells` centroids (no h3-pg extension)
- Config: `osm:` section in config.yaml validated by `OsmConfig` in settings.py