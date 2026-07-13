#!/usr/bin/env python3
"""Pipeline de terrenos RE/MAX -> Postgres, con modelo multi-portal normalizado.

Arquitectura (todo en el schema `public` de Postgres):

    1. Tabla POR PORTAL (cruda, forma propia de cada portal + JSON original):
           public.remax          <- este script
           public.century21      <- futuro script analogo
           ...

    2. Tabla UNIFICADA normalizada (columnas homogeneas + columna `fuente`):
           public.inmuebles      <- combina todos los portales

    3. NORMALIZACION por portal: mapea la tabla cruda del portal -> la unificada
       (UPSERT por (fuente, id_externo)). Para RE/MAX es casi 1:1; cada portal
       nuevo agrega su propia funcion de normalizacion.

Usa asyncpg (mismo driver que el resto del proyecto; ver requirements.txt y
src/db/postgres_repository.py).

Fuente RE/MAX: indice Azure Cognitive Search expuesto por proxy publico
(no es scraping de HTML). Sin API key ni cookies.
    POST https://www.remax.com.py/search/listing-search/docs/search
    CountryID=114 (Paraguay) | TransactionTypeUID=261 (Venta) | MacroPropertyTypeUID=17618 (Terreno)

Uso:
    python scripts/remax_terrenos_pipeline.py
    python scripts/remax_terrenos_pipeline.py --solo-normalizar   # re-normaliza sin re-bajar
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import asyncpg
import requests

# ── Config de la fuente RE/MAX ────────────────────────────────────────────
FUENTE = "remax"
ENDPOINT = "https://www.remax.com.py/search/listing-search/docs/search"
FILTER = (
    "content/CountryID eq 114 "
    "and content/TransactionTypeUID eq 261 "
    "and content/MacroPropertyTypeUID eq 17618 "
    "and content/IsFindable eq true "
    "and content/IsViewable eq true"
)
PAGE = 1000
HEADERS = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
SITE = "https://www.remax.com.py/"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Columnas de la tabla POR PORTAL (public.remax)
PORTAL_COLS = ["mls_id", "listing_id", "precio", "moneda", "superficie_m2",
               "dimensiones", "departamento", "ciudad", "zona", "direccion",
               "lat", "lng", "url", "publicado", "actualizado", "raw"]

DDL_PORTAL = """
CREATE TABLE IF NOT EXISTS public.remax (
    mls_id        TEXT PRIMARY KEY,
    listing_id    INTEGER,
    precio        NUMERIC,
    moneda        TEXT,
    superficie_m2 NUMERIC,
    dimensiones   TEXT,
    departamento  TEXT,
    ciudad        TEXT,
    zona          TEXT,
    direccion     TEXT,
    lat           DOUBLE PRECISION,
    lng           DOUBLE PRECISION,
    url           TEXT,
    publicado     DATE,
    actualizado   TIMESTAMPTZ,
    raw           JSONB,
    scraped_at    TIMESTAMPTZ DEFAULT now()
);
"""

# Modelo UNIFICADO: funcion de normalizacion de texto + tabla de hechos.
# norm(): minusculas, sin tildes, sin espacios extra. Sin depender de la
# extension unaccent (translate cubre el espanol de Paraguay).
DDL_UNIFICADO = r"""
CREATE OR REPLACE FUNCTION public.norm(t text) RETURNS text AS $$
  SELECT nullif(trim(regexp_replace(
    lower(translate(coalesce(t,''),
      'ÁÀÄÂÃÉÈËÊÍÌÏÎÓÒÖÔÕÚÙÜÛÑáàäâãéèëêíìïîóòöôõúùüûñ',
      'AAAAAEEEEIIIIOOOOOUUUUNaaaaaeeeeiiiiooooouuuun')),
    '\s+', ' ', 'g')), '')
$$ LANGUAGE sql IMMUTABLE;

CREATE TABLE IF NOT EXISTS public.inmuebles (
    id               BIGSERIAL PRIMARY KEY,
    fuente           TEXT NOT NULL,          -- 'remax', 'century21', ...
    id_externo       TEXT NOT NULL,          -- id unico dentro del portal (MLSID)
    operacion        TEXT,                   -- 'venta' / 'alquiler'
    tipo_propiedad   TEXT,                   -- 'terreno' / 'casa' / ...
    precio           NUMERIC,
    moneda           TEXT,
    superficie_m2    NUMERIC,
    dimensiones      TEXT,
    departamento     TEXT,
    departamento_norm TEXT,
    ciudad           TEXT,
    ciudad_norm      TEXT,
    zona             TEXT,
    direccion        TEXT,
    lat              DOUBLE PRECISION,
    lng              DOUBLE PRECISION,
    url              TEXT,
    publicado        DATE,
    actualizado      TIMESTAMPTZ,
    primer_visto     TIMESTAMPTZ DEFAULT now(),
    ultimo_visto     TIMESTAMPTZ DEFAULT now(),
    UNIQUE (fuente, id_externo)
);
CREATE INDEX IF NOT EXISTS ix_inmuebles_fuente ON public.inmuebles (fuente);
CREATE INDEX IF NOT EXISTS ix_inmuebles_ciudad ON public.inmuebles (ciudad_norm);
CREATE INDEX IF NOT EXISTS ix_inmuebles_precio ON public.inmuebles (moneda, precio);
"""

# Normalizacion RE/MAX: public.remax -> public.inmuebles (set-based, en SQL).
# Cada portal nuevo replica este INSERT..SELECT ajustando el mapeo de columnas.
SQL_NORMALIZAR_REMAX = """
INSERT INTO public.inmuebles AS t (
    fuente, id_externo, operacion, tipo_propiedad, precio, moneda,
    superficie_m2, dimensiones, departamento, departamento_norm,
    ciudad, ciudad_norm, zona, direccion, lat, lng, url, publicado, actualizado)
SELECT
    'remax', r.mls_id, 'venta', 'terreno', r.precio, r.moneda,
    r.superficie_m2, r.dimensiones,
    r.departamento, public.norm(r.departamento),
    r.ciudad, public.norm(r.ciudad),
    r.zona, r.direccion, r.lat, r.lng, r.url, r.publicado, r.actualizado
FROM public.remax r
ON CONFLICT (fuente, id_externo) DO UPDATE SET
    operacion=EXCLUDED.operacion, tipo_propiedad=EXCLUDED.tipo_propiedad,
    precio=EXCLUDED.precio, moneda=EXCLUDED.moneda,
    superficie_m2=EXCLUDED.superficie_m2, dimensiones=EXCLUDED.dimensiones,
    departamento=EXCLUDED.departamento, departamento_norm=EXCLUDED.departamento_norm,
    ciudad=EXCLUDED.ciudad, ciudad_norm=EXCLUDED.ciudad_norm,
    zona=EXCLUDED.zona, direccion=EXCLUDED.direccion,
    lat=EXCLUDED.lat, lng=EXCLUDED.lng, url=EXCLUDED.url,
    publicado=EXCLUDED.publicado, actualizado=EXCLUDED.actualizado,
    ultimo_visto=now();
"""

# INSERT del portal con placeholders $1..$N. Los tipos van nativos de Python
# (Decimal/date/datetime/dict) porque asyncpg no castea desde texto como psycopg2.
INSERT_PORTAL = (
    "INSERT INTO public.remax (" + ",".join(PORTAL_COLS) + ") VALUES (" +
    ",".join(f"${i}" for i in range(1, len(PORTAL_COLS) + 1)) + ") "
    "ON CONFLICT (mls_id) DO UPDATE SET " +
    ", ".join(f"{c}=EXCLUDED.{c}" for c in PORTAL_COLS if c != "mls_id") +
    ", scraped_at=now()"
)


def load_env():
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def dec(x):
    """A Decimal para columnas NUMERIC (asyncpg no acepta int/float ahi)."""
    return Decimal(str(x)) if x is not None else None


def date_epoch(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc).date() if epoch else None


def ts_epoch(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc) if epoch else None


def short_url(c):
    for sl in (c.get("ShortLinks") or []):
        if sl.get("LanguageCode") == "es-PY":
            return SITE + sl["ShortLink"]
    return None


def fetch_page(skip):
    body = {"count": True, "top": PAGE, "skip": skip, "search": "*",
            "filter": FILTER, "orderby": "content/OrigListingDate asc"}
    r = requests.post(ENDPOINT, json=body, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_all():
    """Baja todos los terrenos y los devuelve como tuplas para public.remax."""
    rows, seen = [], set()
    skip, total = 0, None
    while True:
        data = fetch_page(skip)
        if total is None:
            total = data.get("@odata.count")
            print(f"Total terrenos en venta: {total}")
        batch = data.get("value", [])
        if not batch:
            break
        for v in batch:
            c = v["content"]
            mls = c.get("MLSID")
            if not mls or mls in seen:
                continue
            seen.add(mls)
            loc = c.get("Location") or {}
            lng, lat = (loc.get("coordinates") or [None, None])[:2]
            rows.append((
                mls, c.get("ListingId"), dec(c.get("ListingPrice")), c.get("ListingCurrency"),
                dec(c.get("TotalArea") or c.get("LotSize2")), c.get("LotSize"),
                c.get("Province"), c.get("City"), c.get("LocalZone"),
                (c.get("TitleAddress") or "").strip() or None, lat, lng, short_url(c),
                date_epoch(c.get("OrigListingDate")), ts_epoch(c.get("LastUpdatedOnWeb")),
                c,  # dict -> jsonb via type codec
            ))
        print(f"  bajados {len(rows)}/{total}")
        skip += PAGE
        if skip >= (total or 0):
            break
        time.sleep(0.3)
    return rows


def get_dsn():
    dsn = os.environ.get("PG_DSN", "")
    if not dsn:
        sys.exit("ERROR: PG_DSN no configurado (.env).")
    return dsn


async def guardar_portal(conn, rows):
    """UPSERT de la tabla cruda del portal (public.remax), en lotes."""
    await conn.execute(DDL_PORTAL)
    batch = 500
    for i in range(0, len(rows), batch):
        await conn.executemany(INSERT_PORTAL, rows[i:i + batch])
    n = await conn.fetchval("SELECT COUNT(*) FROM public.remax")
    print(f"public.remax: {len(rows)} filas upsert (tabla tiene {n})")


async def normalizar(conn):
    """Mapea public.remax -> public.inmuebles (unificada, normalizada)."""
    await conn.execute(DDL_UNIFICADO)
    await conn.execute(SQL_NORMALIZAR_REMAX)
    n_fuente = await conn.fetchval(
        "SELECT COUNT(*) FROM public.inmuebles WHERE fuente=$1", FUENTE)
    row = await conn.fetchrow(
        "SELECT COUNT(*) AS t, COUNT(DISTINCT fuente) AS p FROM public.inmuebles")
    print(f"public.inmuebles: {n_fuente} de '{FUENTE}' "
          f"(unificada tiene {row['t']} de {row['p']} portal/es)")


async def connect_retry(dsn, intentos=10):
    """Conecta reintentando ante fallos transitorios de red/DNS (Railway remoto).

    El proxy de Railway a veces no resuelve por DNS unos segundos (getaddrinfo
    failed / WSA 11001). Con backoff tope de 10s aguantamos ~1 min de corte.
    """
    for i in range(1, intentos + 1):
        try:
            return await asyncpg.connect(dsn, timeout=20)
        except (OSError, asyncpg.PostgresError) as e:
            if i == intentos:
                raise
            espera = min(2 ** i, 10)  # 2,4,8,10,10,... (tope 10s)
            print(f"Conexion fallo ({type(e).__name__}: {e}). "
                  f"Reintento {i}/{intentos - 1} en {espera}s...")
            await asyncio.sleep(espera)


async def run(solo_normalizar):
    conn = await connect_retry(get_dsn())
    await conn.set_type_codec(
        "jsonb", encoder=lambda v: json.dumps(v, ensure_ascii=False),
        decoder=json.loads, schema="pg_catalog",
    )
    try:
        if not solo_normalizar:
            rows = fetch_all()
            if not rows:
                print("No se obtuvieron listados. Abortando.")
                return 1
            await guardar_portal(conn, rows)
        await normalizar(conn)
    finally:
        await conn.close()
    print("Pipeline completo.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="RE/MAX terrenos -> Postgres (modelo multi-portal)")
    ap.add_argument("--solo-normalizar", action="store_true",
                    help="no baja de la API; re-normaliza public.remax -> public.inmuebles")
    args = ap.parse_args()
    load_env()
    return asyncio.run(run(args.solo_normalizar))


if __name__ == "__main__":
    sys.exit(main())
