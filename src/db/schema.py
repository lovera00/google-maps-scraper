DDL = """
CREATE TABLE IF NOT EXISTS businesses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    lat         REAL    NOT NULL,
    lng         REAL    NOT NULL,
    category        TEXT    NOT NULL DEFAULT 'Sin categoria',
    search_category TEXT    NOT NULL DEFAULT '',    -- categoria buscada (ej. "restaurantes")
    address     TEXT,
    phone       TEXT,
    website     TEXT,
    rating      REAL,
    review_count INTEGER,
    source_url       TEXT,
    google_place_id  TEXT,
    raw_name    TEXT,
    metadata    TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    is_active   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_businesses_name ON businesses(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_businesses_coords ON businesses(lat, lng);
CREATE INDEX IF NOT EXISTS idx_businesses_category ON businesses(category);
CREATE UNIQUE INDEX IF NOT EXISTS idx_businesses_source_url
    ON businesses(source_url) WHERE source_url IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_businesses_google_place_id
    ON businesses(google_place_id) WHERE google_place_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS scraping_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_cell_json  TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    depth           INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'pending',
    results_count   INTEGER DEFAULT 0,
    error_message   TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    retry_count     INTEGER DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_scraping_tasks_status ON scraping_tasks(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_scraping_tasks_unique
    ON scraping_tasks(grid_cell_json, category, depth);
"""
