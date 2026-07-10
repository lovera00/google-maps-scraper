#!/usr/bin/env python3
"""Vuelca registros limpios (con categoria) de SQLite a PostgreSQL via bulk INSERT.

Antes de insertar, CURA las categorias: extrae la categoria real de Google del
texto crudo scrapeado (formato "nombre 4.3 categoria" o "nombre No hay opiniones
categoria") y la clasifica contra la taxonomia de PG (commerce_types /
category_mappings) en cascada:

    1. google_categoria   (0.90)  cola extraida del texto crudo
    2. categoria_original (0.85)  el campo category ya venia limpio
    3. search_category    (0.60)  termino de busqueda del scraping
    4. nombre             (0.50)  keyword del mapeo dentro del nombre

Las filas entran a PG con business_type / business_category / category_source /
category_confidence ya poblados. Usa INSERT ON CONFLICT DO NOTHING, asi que
nunca pisa registros existentes.

Uso:
    python scripts/flush_clean_to_pg.py
    python scripts/flush_clean_to_pg.py --dry-run
"""
import argparse, asyncio, json, os, re, sys, unicodedata
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite, asyncpg
from src.config.loader import load_config

# Mismos patrones que la cascada SQL en PG (ver CLAUDE.md, seccion Categorizacion)
RE_RATING_TAIL = re.compile(r"\d\.\d\s+(.*)$")
RE_NO_REVIEWS_TAIL = re.compile(r"no hay opiniones\s+(.*)$", re.IGNORECASE)
RE_STATUS_SUFFIX = re.compile(
    r"\s+(cerrado|abierto|cierra pronto|abre pronto)"
    r"(\s+(temporalmente|permanentemente|24 horas))?$"
)


def norm_txt(s: str) -> str:
    """Equivalente Python de norm_txt() en PG: lower + unaccent + trim."""
    s = unicodedata.normalize("NFD", (s or "").lower().strip())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def extract_google_category(raw: str) -> str | None:
    """Extrae la cola con la categoria real de Google del texto crudo."""
    if not raw:
        return None
    m = RE_RATING_TAIL.search(raw)
    if not m:
        m = RE_NO_REVIEWS_TAIL.search(raw)
    return m.group(1).strip() if m else None


class Curator:
    """Clasifica una fila contra category_mappings replicando la cascada SQL."""

    def __init__(self, mappings: dict[str, tuple[str | None, str]]):
        self.mappings = mappings
        # Regex de keywords para el paso por nombre: claves >= 5 chars,
        # con limites de palabra, la mas larga gana.
        keys = sorted((k for k in mappings if len(k) >= 5), key=len, reverse=True)
        self.name_re = (
            re.compile(r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b")
            if keys else None
        )

    def _lookup(self, text: str | None) -> tuple[str | None, str] | None:
        if not text:
            return None
        key = RE_STATUS_SUFFIX.sub("", norm_txt(text))
        return self.mappings.get(key)

    def curate(self, category: str, search_category: str, name: str):
        """Devuelve (google_category_raw, type, category, source, confidence)."""
        google_raw = extract_google_category(category)

        hit = self._lookup(google_raw)
        if hit:
            return google_raw, hit[0], hit[1], "google_categoria", 0.90

        hit = self._lookup(category)
        if hit:
            return google_raw, hit[0], hit[1], "categoria_original", 0.85

        hit = self._lookup(search_category)
        if hit:
            return google_raw, hit[0], hit[1], "search_category", 0.60

        if self.name_re:
            m = max(self.name_re.findall(norm_txt(name)), key=len, default=None)
            if m:
                t, c = self.mappings[m]
                return google_raw, t, c, "nombre", 0.50

        return google_raw, None, None, None, None


async def load_mappings(pg: asyncpg.Connection) -> dict[str, tuple[str | None, str]]:
    """Carga category_mappings desde PG. Vacio si la tabla no existe."""
    try:
        rows = await pg.fetch("SELECT raw_norm, type, category FROM category_mappings")
        return {r["raw_norm"]: (r["type"], r["category"]) for r in rows}
    except asyncpg.UndefinedTableError:
        print("ADVERTENCIA: category_mappings no existe en PG. "
              "Se insertara sin curar categorias.")
        return {}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default="data/paraguay_businesses.db")
    args = parser.parse_args()

    load_config("config.yaml")
    dsn = os.environ.get("PG_DSN", "")
    if not dsn:
        print("ERROR: PG_DSN no esta configurado.")
        return

    # Leer registros limpios de SQLite
    async with aiosqlite.connect(args.db) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT name, lat, lng, category, search_category,
                      address, phone, website, rating, review_count,
                      source_url, google_place_id, raw_name, metadata
               FROM businesses
               WHERE is_active = 1
                 AND category IS NOT NULL
                 AND category != ''
                 AND category != 'Sin categoria'
                 AND search_category IS NOT NULL
                 AND search_category != ''"""
        )
        rows = await cursor.fetchall()

    total = len(rows)
    print(f"Registros limpios: {total}")
    if not total:
        return

    pg = await asyncpg.connect(dsn)
    try:
        # ── Paso 1: curar categorias ──────────────────────────────────
        curator = Curator(await load_mappings(pg))
        by_source: dict[str, int] = {}
        curated = []
        for r in rows:
            google_raw, btype, bcat, source, conf = curator.curate(
                r["category"], r["search_category"], r["name"]
            )
            by_source[source or "sin clasificar"] = by_source.get(source or "sin clasificar", 0) + 1
            curated.append((r, google_raw, btype, bcat, source, conf))

        print("Curacion de categorias:")
        for source, n in sorted(by_source.items(), key=lambda x: -x[1]):
            print(f"  {source}: {n} ({n / total * 100:.1f}%)")

        if args.dry_run:
            cats = {}
            for _, _, _, bcat, _, _ in curated:
                key = bcat or "(sin clasificar)"
                cats[key] = cats.get(key, 0) + 1
            print("Distribucion por business_category:")
            for cat, n in sorted(cats.items(), key=lambda x: -x[1]):
                print(f"  {cat}: {n}")
            return

        # ── Paso 2: bulk INSERT a PostgreSQL ──────────────────────────
        values = []
        for r, google_raw, btype, bcat, source, conf in curated:
            meta = r["metadata"]
            if meta:
                try: meta = json.loads(meta)
                except: meta = {}
            else:
                meta = {}
            values.append((
                r["name"], r["lat"], r["lng"], r["category"],
                r["search_category"] if "search_category" in r.keys() else "",
                r["address"], r["phone"], r["website"],
                r["rating"], r["review_count"],
                r["source_url"], r["google_place_id"],
                r["raw_name"], json.dumps(meta, ensure_ascii=False),
                google_raw, btype, bcat, source, conf,
            ))

        # Insertar en batches de 500 para no reventar memoria
        batch_size = 500
        for i in range(0, len(values), batch_size):
            batch = values[i:i + batch_size]
            await pg.executemany(
                """INSERT INTO businesses
                   (name, lat, lng, category, search_category,
                    address, phone, website, rating, review_count,
                    source_url, google_place_id, raw_name, metadata,
                    google_category_raw, business_type, business_category,
                    category_source, category_confidence)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                           $11, $12, $13, $14::jsonb, $15, $16, $17, $18, $19)
                   ON CONFLICT DO NOTHING""",
                batch,
            )
            pct = min(i + batch_size, len(values)) / len(values) * 100
            print(f"\r  Progreso: {min(i + batch_size, len(values))}/{len(values)} ({pct:.0f}%)",
                  end="", flush=True)

        # Contar total en PG
        count = await pg.fetchval("SELECT COUNT(*) FROM businesses")
        print(f"\n\nCompletado. PostgreSQL tiene {count} registros (de {total} enviados).")

    finally:
        await pg.close()


if __name__ == "__main__":
    asyncio.run(main())
