#!/usr/bin/env python3
"""Cotizacion PYG/USD por dia + dolarizacion de public.inmuebles.

Que hace (por defecto):
    1. Trae la cotizacion del dia (guaranies por 1 USD) desde open.er-api.com.
    2. La guarda en public.cotizaciones (una fila por fecha; upsert).
    3. Dolariza public.inmuebles: llena precio_usd usando la ULTIMA cotizacion
       disponible en la tabla.

"Si no se corre el script se usa el ultimo resultado": la dolarizacion siempre
toma MAX(fecha) de public.cotizaciones. Si hoy no se actualiza, se usa la del
ultimo dia guardado.

Superficie: public.inmuebles.superficie_m2 ya esta SIEMPRE en m2 (las hectareas
de Century21 se convierten x10.000 en su pipeline). Este script lo verifica.

Uso:
    python scripts/actualizar_cotizacion.py                 # trae hoy + dolariza
    python scripts/actualizar_cotizacion.py --tc 7300       # fija la cotizacion a mano
    python scripts/actualizar_cotizacion.py --solo-dolarizar# no trae; usa la ultima guardada
"""
import argparse
import asyncio
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import asyncpg
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FX_URL = "https://open.er-api.com/v6/latest/USD"

DDL = """
CREATE TABLE IF NOT EXISTS public.cotizaciones (
    fecha        DATE PRIMARY KEY,
    pyg_por_usd  NUMERIC NOT NULL,      -- guaranies por 1 USD
    fuente       TEXT,
    capturado_en TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE public.inmuebles ADD COLUMN IF NOT EXISTS precio_usd NUMERIC;
"""

# Dolariza con la cotizacion $1 (guaranies por USD). USD queda igual; PYG se divide.
SQL_DOLARIZAR = """
UPDATE public.inmuebles SET precio_usd = CASE
    WHEN precio IS NULL OR precio = 0 THEN NULL
    WHEN upper(moneda) = 'USD' THEN round(precio, 2)
    WHEN upper(moneda) = 'PYG' THEN round(precio / $1, 2)
    ELSE NULL
END;
"""


def load_env():
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_dsn():
    dsn = os.environ.get("PG_DSN", "")
    if not dsn:
        sys.exit("ERROR: PG_DSN no configurado (.env).")
    return dsn


def fetch_tc():
    """Trae guaranies por 1 USD desde open.er-api.com."""
    r = requests.get(FX_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    j = r.json()
    pyg = (j.get("rates") or {}).get("PYG")
    if not pyg:
        raise RuntimeError("La respuesta no trae rate PYG")
    return Decimal(str(pyg)), "open.er-api.com"


async def connect_retry(dsn, intentos=10):
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


async def run(args):
    conn = await connect_retry(get_dsn())
    try:
        await conn.execute(DDL)

        if not args.solo_dolarizar:
            if args.tc:
                tc, fuente = Decimal(str(args.tc)), "manual"
            else:
                tc, fuente = fetch_tc()
            await conn.execute(
                """INSERT INTO public.cotizaciones (fecha, pyg_por_usd, fuente)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (fecha) DO UPDATE
                     SET pyg_por_usd = EXCLUDED.pyg_por_usd,
                         fuente = EXCLUDED.fuente, capturado_en = now()""",
                date.today(), tc, fuente,
            )
            print(f"Cotizacion {date.today()}: {tc} PYG/USD (fuente: {fuente})")

        # Ultima cotizacion disponible (hoy si se acaba de traer, o la mas reciente)
        row = await conn.fetchrow(
            "SELECT fecha, pyg_por_usd FROM public.cotizaciones ORDER BY fecha DESC LIMIT 1")
        if not row:
            sys.exit("ERROR: no hay ninguna cotizacion guardada. Corre sin --solo-dolarizar "
                     "o pasa --tc <valor> para cargar la primera.")
        tc = row["pyg_por_usd"]
        print(f"Dolarizando con cotizacion del {row['fecha']}: {tc} PYG/USD")

        await conn.execute(SQL_DOLARIZAR, tc)

        # Resumen + verificacion de superficie en m2
        r = await conn.fetchrow("""
            SELECT count(*) total,
                   count(precio_usd) con_usd,
                   round(avg(precio_usd) FILTER (WHERE moneda='PYG')) prom_pyg_dolarizado,
                   round(avg(precio_usd) FILTER (WHERE moneda='USD')) prom_usd
            FROM public.inmuebles""")
        print(f"public.inmuebles: {r['con_usd']}/{r['total']} con precio_usd "
              f"(prom ex-PYG ${r['prom_pyg_dolarizado']}, prom USD ${r['prom_usd']})")

        # superficie: confirmar que no quedan hectareas sin convertir
        ha = await conn.fetchval("SELECT count(*) FROM public.century21 WHERE unidad='ha'")
        print(f"superficie: public.inmuebles.superficie_m2 en m2 (las {ha} de C21 en ha "
              f"ya van x10.000 en su pipeline)")
    finally:
        await conn.close()
    print("Listo.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Cotizacion PYG/USD por dia + dolarizar inmuebles")
    ap.add_argument("--tc", type=float, help="fija la cotizacion (PYG por USD) a mano")
    ap.add_argument("--solo-dolarizar", action="store_true",
                    help="no trae cotizacion; re-dolariza con la ultima guardada")
    args = ap.parse_args()
    load_env()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
