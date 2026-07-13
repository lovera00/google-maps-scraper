"""
Extrae terrenos en venta (Paraguay) desde la API interna de RE/MAX.

No es scraping de HTML: el portal remax.com.py alimenta sus listados desde
un indice de Azure Cognitive Search expuesto via un proxy publico. Pedimos
JSON directamente -> datos estructurados, rapido y estable.

Endpoint: POST https://www.remax.com.py/search/listing-search/docs/search
Filtros usados (equivalen a la URL del portal):
  CountryID=114            -> Paraguay
  TransactionTypeUID=261   -> Venta
  MacroPropertyTypeUID=17618 -> Terreno / Land
"""

import csv
import time
import requests

ENDPOINT = "https://www.remax.com.py/search/listing-search/docs/search"

FILTER = (
    "content/CountryID eq 114 "
    "and content/TransactionTypeUID eq 261 "
    "and content/MacroPropertyTypeUID eq 17618 "
    "and content/IsFindable eq true "
    "and content/IsViewable eq true"
)

# Solo los campos que interesan (mas liviano). Quita 'select' del body para traer todo.
SELECT = ",".join(
    "content/" + f for f in [
        "ListingId", "MLSID", "ListingPrice", "ListingCurrency", "PriceTypeUID",
        "TotalArea", "LotSize2", "LotSize",
        "Province", "City", "LocalZone", "TitleAddress",
        "Location", "OrigListingDate", "LastUpdatedOnWeb",
    ]
)

PAGE = 1000  # tope de Azure Search por request
HEADERS = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}


def fetch_page(skip):
    body = {
        "count": True,
        "top": PAGE,
        "skip": skip,
        "search": "*",
        "filter": FILTER,
        "select": SELECT,
        "orderby": "content/OrigListingDate asc",  # campo ordenable -> paginado estable
    }
    r = requests.post(ENDPOINT, json=body, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def main():
    rows = []
    skip = 0
    total = None
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
            loc = c.get("Location") or {}
            coords = (loc.get("coordinates") or [None, None])
            rows.append({
                "ListingId": c.get("ListingId"),
                "MLSID": c.get("MLSID"),
                "Precio": c.get("ListingPrice"),
                "Moneda": c.get("ListingCurrency"),
                "SuperficieM2": c.get("TotalArea") or c.get("LotSize2"),
                "Dimensiones": c.get("LotSize"),
                "Departamento": c.get("Province"),
                "Ciudad": c.get("City"),
                "Zona": c.get("LocalZone"),
                "Direccion": (c.get("TitleAddress") or "").strip(),
                "Lng": coords[0],
                "Lat": coords[1],
            })
        print(f"  {len(rows)}/{total}")
        skip += PAGE
        if skip >= (total or 0):
            break
        time.sleep(0.3)  # cortesia

    out = "remax_terrenos.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Listo: {len(rows)} filas -> {out}")


if __name__ == "__main__":
    main()
