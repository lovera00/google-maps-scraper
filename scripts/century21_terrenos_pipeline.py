#!/usr/bin/env python3
"""Pipeline de terrenos Century21 Paraguay -> Postgres (modelo multi-portal).

Espejo de remax_terrenos_pipeline.py. Escribe en:
    public.century21   (cruda, forma del portal + JSON original en `raw`)
    public.inmuebles   (unificada normalizada, fuente='century21')

Fuente Century21: NO tiene API JSON. Cada pagina HTML (SSR) embebe un array
JSON en la variable JS `propiedades: [...]` con ~92 campos por propiedad. Se
extrae con un parser de corchetes balanceados (sin parsear HTML).

    Listado : https://century21.com.py/busqueda/tipo_terreno
    Pagina  : /busqueda/tipo_terreno/pagina_N   (10 por pagina)

OJO - TOPE DE PAGINACION: el sitio corta en la pagina 100 (~1000 de 2714). Para
bajar TODO se segmenta por el facet `metros-de-terreno_<rango>` (6 tramos que
suman exacto el total, cada uno <1000 = paginable completo) y se unen los
resultados deduplicando por id.

Usa asyncpg (driver del proyecto).

Uso:
    python scripts/century21_terrenos_pipeline.py
    python scripts/century21_terrenos_pipeline.py --solo-normalizar
"""
import argparse
import asyncio
import json
import os
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import asyncpg
import requests

FUENTE = "century21"
BASE = "https://century21.com.py"
LISTADO = "/busqueda/tipo_terreno"
# Segmentos por m2 de terreno: suman exacto el total y cada uno < 1000 (esquiva
# el tope de pagina 100). Son los buckets reales del facet del sitio.
SEGMENTOS = ["metros-de-terreno_0-59", "metros-de-terreno_100-149",
             "metros-de-terreno_150-299", "metros-de-terreno_300-499",
             "metros-de-terreno_500-999", "metros-de-terreno_1000-mas"]
HEADERS = {"User-Agent": "Mozilla/5.0"}
MAX_PAG = 100  # tope duro del sitio; freno de seguridad por segmento
PROJECT_ROOT = Path(__file__).resolve().parent.parent

PORTAL_COLS = ["id_externo", "operacion", "subtipo", "precio", "moneda",
               "precio_secundario", "moneda_secundaria", "m2t", "m2c", "unidad",
               "departamento", "ciudad", "zona", "direccion", "lat", "lng",
               "url", "encabezado", "status", "dias_modificacion", "raw"]

DDL_PORTAL = """
CREATE TABLE IF NOT EXISTS public.century21 (
    id_externo        TEXT PRIMARY KEY,   -- id de la propiedad en C21
    operacion         TEXT,
    subtipo           TEXT,
    precio            NUMERIC,
    moneda            TEXT,
    precio_secundario NUMERIC,
    moneda_secundaria TEXT,
    m2t               NUMERIC,            -- superficie de terreno (en `unidad`)
    m2c               NUMERIC,            -- superficie construida
    unidad            TEXT,               -- 'm2' o 'ha' (hectareas)
    departamento      TEXT,
    ciudad            TEXT,
    zona              TEXT,
    direccion         TEXT,
    lat               DOUBLE PRECISION,
    lng               DOUBLE PRECISION,
    url               TEXT,
    encabezado        TEXT,
    status            TEXT,
    dias_modificacion INTEGER,
    raw               JSONB,
    scraped_at        TIMESTAMPTZ DEFAULT now()
);
-- por si la tabla ya existia sin la columna unidad (corridas previas)
ALTER TABLE public.century21 ADD COLUMN IF NOT EXISTS unidad TEXT;
"""

# Modelo unificado: la funcion public.norm() y la tabla public.inmuebles ya las
# crea el pipeline de RE/MAX. Las volvemos a declarar IF NOT EXISTS por si este
# script corre primero.
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
    fuente           TEXT NOT NULL,
    id_externo       TEXT NOT NULL,
    operacion        TEXT,
    tipo_propiedad   TEXT,
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

SQL_NORMALIZAR = """
INSERT INTO public.inmuebles AS t (
    fuente, id_externo, operacion, tipo_propiedad, precio, moneda,
    superficie_m2, dimensiones, departamento, departamento_norm,
    ciudad, ciudad_norm, zona, direccion, lat, lng, url, publicado, actualizado)
SELECT
    'century21', c.id_externo,
    CASE WHEN c.operacion='renta' THEN 'alquiler' ELSE c.operacion END,
    'terreno', c.precio, c.moneda,
    c.m2t * CASE WHEN c.unidad='ha' THEN 10000 ELSE 1 END, NULL,
    c.departamento, public.norm(c.departamento),
    c.ciudad, public.norm(c.ciudad),
    c.zona, c.direccion, c.lat, c.lng, c.url, NULL, NULL
FROM public.century21 c
ON CONFLICT (fuente, id_externo) DO UPDATE SET
    operacion=EXCLUDED.operacion, tipo_propiedad=EXCLUDED.tipo_propiedad,
    precio=EXCLUDED.precio, moneda=EXCLUDED.moneda,
    superficie_m2=EXCLUDED.superficie_m2,
    departamento=EXCLUDED.departamento, departamento_norm=EXCLUDED.departamento_norm,
    ciudad=EXCLUDED.ciudad, ciudad_norm=EXCLUDED.ciudad_norm,
    zona=EXCLUDED.zona, direccion=EXCLUDED.direccion,
    lat=EXCLUDED.lat, lng=EXCLUDED.lng, url=EXCLUDED.url,
    ultimo_visto=now();
"""

INSERT_PORTAL = (
    "INSERT INTO public.century21 (" + ",".join(PORTAL_COLS) + ") VALUES (" +
    ",".join(f"${i}" for i in range(1, len(PORTAL_COLS) + 1)) + ") "
    "ON CONFLICT (id_externo) DO UPDATE SET " +
    ", ".join(f"{c}=EXCLUDED.{c}" for c in PORTAL_COLS if c != "id_externo") +
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
    if x is None or x == "":
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None


def flt(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def extraer_propiedades(html):
    """Extrae el array JS `propiedades: [...]` con parser de corchetes balanceados."""
    i = html.find("propiedades:")
    if i < 0:
        return []
    try:
        start = html.index("[", i)
    except ValueError:
        return []
    depth = 0
    instr = False
    esc = False
    for j in range(start, len(html)):
        ch = html[j]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        else:
            if ch == '"':
                instr = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return json.loads(html[start:j + 1])
    return []


def direccion(p):
    partes = [p.get(k) for k in ("calle", "colonia", "ciudad", "estado")]
    partes = [str(x).strip() for x in partes if x]
    return ", ".join(dict.fromkeys(partes)) or None


def to_row(p):
    return (
        str(p["id"]),
        p.get("tipoOperacion"),
        p.get("subTipoPropiedad"),
        dec(p.get("precio")),
        p.get("moneda"),
        dec(p.get("precioSecundario")),
        p.get("monedaSecundaria"),
        dec(p.get("m2T")),
        dec(p.get("m2C")),
        p.get("unidadDeMedida"),
        p.get("estado"),
        p.get("municipio"),
        p.get("colonia"),
        direccion(p),
        flt(p.get("lat")),
        flt(p.get("lon")),
        BASE + p["urlCorrecta"] if p.get("urlCorrecta") else None,
        p.get("encabezado"),
        p.get("status"),
        p.get("diasModificacion") if isinstance(p.get("diasModificacion"), int) else None,
        p,  # dict -> jsonb via type codec
    )


def fetch_all():
    """Baja todos los terrenos segmentando por m2 y deduplicando por id.

    El sitio clampa las paginas fuera de rango a la ultima (repite contenido),
    asi que cortamos cuando una pagina repite los ids de la anterior o no aporta
    ids nuevos. Un no-200 tambien corta el segmento (sin abortar todo)."""
    vistos = {}
    for seg in SEGMENTOS:
        antes = len(vistos)
        prev_ids = None
        for pg in range(1, MAX_PAG + 1):
            suf = f"/{seg}" + ("" if pg == 1 else f"/pagina_{pg}")
            r = requests.get(BASE + LISTADO + suf, headers=HEADERS, timeout=60)
            if r.status_code != 200:
                print(f"    {seg} pagina {pg}: HTTP {r.status_code}, corto segmento")
                break
            props = extraer_propiedades(r.text)
            ids = [str(p["id"]) for p in props]
            if not ids or ids == prev_ids:  # vacio o clamp a la ultima pagina
                break
            nuevos = sum(1 for p in props if str(p["id"]) not in vistos)
            for p in props:
                vistos.setdefault(str(p["id"]), p)
            prev_ids = ids
            if nuevos == 0:  # nada nuevo aunque cambie el orden -> fin
                break
            time.sleep(0.3)
        print(f"  segmento {seg}: +{len(vistos) - antes} nuevos (total {len(vistos)})")
    return [to_row(p) for p in vistos.values()]


def get_dsn():
    dsn = os.environ.get("PG_DSN", "")
    if not dsn:
        sys.exit("ERROR: PG_DSN no configurado (.env).")
    return dsn


async def connect_retry(dsn, intentos=10):
    """Conecta reintentando ante fallos transitorios de red/DNS (Railway remoto)."""
    for i in range(1, intentos + 1):
        try:
            return await asyncpg.connect(dsn, timeout=20)
        except (OSError, asyncpg.PostgresError) as e:
            if i == intentos:
                raise
            espera = min(2 ** i, 10)
            print(f"Conexion fallo ({type(e).__name__}: {e}). "
                  f"Reintento {i}/{intentos - 1} en {espera}s...")
            await asyncio.sleep(espera)


async def guardar_portal(conn, rows):
    await conn.execute(DDL_PORTAL)
    for i in range(0, len(rows), 500):
        await conn.executemany(INSERT_PORTAL, rows[i:i + 500])
    n = await conn.fetchval("SELECT COUNT(*) FROM public.century21")
    print(f"public.century21: {len(rows)} filas upsert (tabla tiene {n})")


async def normalizar(conn):
    await conn.execute(DDL_UNIFICADO)
    await conn.execute(SQL_NORMALIZAR)
    n_fuente = await conn.fetchval(
        "SELECT COUNT(*) FROM public.inmuebles WHERE fuente=$1", FUENTE)
    row = await conn.fetchrow(
        "SELECT COUNT(*) AS t, COUNT(DISTINCT fuente) AS p FROM public.inmuebles")
    print(f"public.inmuebles: {n_fuente} de '{FUENTE}' "
          f"(unificada tiene {row['t']} de {row['p']} portal/es)")


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
    ap = argparse.ArgumentParser(description="Century21 terrenos -> Postgres (multi-portal)")
    ap.add_argument("--solo-normalizar", action="store_true",
                    help="no baja del sitio; re-normaliza public.century21 -> public.inmuebles")
    args = ap.parse_args()
    load_env()
    return asyncio.run(run(args.solo_normalizar))


if __name__ == "__main__":
    sys.exit(main())
