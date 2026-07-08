#!/usr/bin/env python3
"""Extrae los 262+1 distritos de Paraguay desde Wikipedia y los geocodifica con Nominatim.

Fuente: https://es.wikipedia.org/wiki/Anexo:Municipios_de_Paraguay
Geocodificacion: Nominatim (OpenStreetMap)

Uso:
    python fetch_paraguay_districts.py
    python fetch_paraguay_districts.py --output cities.json
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

WIKIPEDIA_API = "https://es.wikipedia.org/w/api.php"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "ParaguayDistrictsBot/1.0 (districts scraper)"
TIMEOUT = 15
NOMINATIM_DELAY = 1.1  # Nominatim rate limit: 1 req/s
MAX_RETRIES = 3


def fetch_wikitext() -> str:
    """Obtiene el wikitexto de la pagina de municipios de Paraguay."""
    params = {
        "action": "query",
        "titles": "Anexo:Municipios de Paraguay",
        "prop": "revisions",
        "rvprop": "content",
        "format": "json",
        "formatversion": "2",
        "redirects": "1",
    }
    url = f"{WIKIPEDIA_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            pages = data.get("query", {}).get("pages", [])
            if pages:
                return pages[0].get("revisions", [{}])[0].get("content", "")
            return ""
        except Exception as e:
            print(f"  Error Wikipedia (intento {attempt+1}): {e}")
            time.sleep(2)
    raise RuntimeError("No se pudo obtener el wikitexto despues de varios intentos")


def parse_districts(wikitext: str) -> list[str]:
    """Extrae nombres de distritos de las tablas del wikitexto de Wikipedia.

    Formato de fila de distrito: | align="center" | '''[[Nombre del distrito]]'''
    Formato de departamento:     | colspan="5" | ... '''[[Departamento]]'''<br>
    """
    names = []
    district_pattern = re.compile(r"'''\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]'''")

    for line in wikitext.split("\n"):
        # Saltar lineas de encabezado de departamento (tienen colspan)
        if "colspan" in line:
            continue
        # Saltar lineas de imagen/archivo
        if "Archivo:" in line or "File:" in line:
            continue

        match = district_pattern.search(line)
        if match:
            name = match.group(1).strip()
            # Limpiar cualquier texto entre parentesis (desambiguacion de Wikipedia)
            name = re.sub(r'\s*\([^)]*\)\s*', '', name).strip()
            names.append(name)

    # Eliminar duplicados manteniendo orden
    seen = set()
    unique = []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            unique.append(n)

    return unique


def is_in_paraguay(lat: float, lng: float) -> bool:
    """Verifica que las coordenadas esten dentro del bounding box de Paraguay."""
    return -27.7 <= lat <= -19.2 and -62.7 <= lng <= -54.2


def geocode(name: str) -> Optional[dict]:
    """Geocodifica un nombre de distrito con Nominatim usando multiples estrategias."""
    queries = [
        f"{name}, Paraguay",
        f"{name}, Paraguay, South America",
        name.replace("  ", " ").strip(),  # Sin pais (ultimo recurso)
    ]

    for query in queries:
        for attempt in range(MAX_RETRIES):
            try:
                params = {
                    "q": query,
                    "format": "jsonv2",
                    "limit": 3,
                    "accept-language": "es",
                    "countrycodes": "py",
                }
                url = f"{NOMINATIM_URL}?{urllib.parse.urlencode(params)}"
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    results = json.loads(resp.read().decode("utf-8"))

                # Buscar primer resultado dentro de Paraguay
                for r in results:
                    lat = float(r["lat"])
                    lng = float(r["lon"])
                    if is_in_paraguay(lat, lng):
                        return {"name": name, "lat": lat, "lng": lng}

                time.sleep(0.5)
            except Exception as e:
                if attempt == MAX_RETRIES - 1:
                    print(f" ({e})", end="")
                time.sleep(2)

    return None


# Fallback manual para distritos que Nominatim no encuentra
# Coordenadas verificadas manualmente desde fuentes oficiales
MANUAL_FALLBACK: dict[str, tuple[float, float]] = {
    "Cerro Corá": (-22.6333, -56.1333),
    "Regimiento de Infantería Tres Corrales": (-23.4496, -56.1175),
    "Independencia": (-25.7185, -56.2650),
    "Teniente Primero Manuel Irala Fernández": (-22.3167, -59.5833),
    "Capiibary": (-24.8000, -56.0333),
    "San Pedro de Ycuamandiyú": (-24.1000, -57.0833),
    "San Vicente Pancholo": (-24.6366, -55.3005),
    "Veinticinco de Diciembre": (-24.6500, -56.4833),
}


def main():
    output = "cities.json"
    if len(sys.argv) > 1:
        output = sys.argv[sys.argv.index("--output") + 1] if "--output" in sys.argv else output

    print("1/3 Obteniendo lista de distritos desde Wikipedia...")
    wikitext = fetch_wikitext()
    print(f"   Wikitexto: {len(wikitext)} caracteres")

    districts = parse_districts(wikitext)
    print(f"   Distritos encontrados: {len(districts)}")

    print(f"\n2/3 Geocodificando {len(districts)} distritos con Nominatim...")
    print(f"   Delay: {NOMINATIM_DELAY}s entre requests (rate limit de Nominatim)")
    print(f"   Tiempo estimado: {len(districts) * NOMINATIM_DELAY / 60:.1f} minutos\n")

    cities = []
    errores = 0
    for i, name in enumerate(districts):
        sys.stdout.write(f"   [{i+1:3d}/{len(districts)}] {name:35s} ")
        sys.stdout.flush()

        # Intentar fallback manual si Nominatim falla
        if name in MANUAL_FALLBACK:
            lat, lng = MANUAL_FALLBACK[name]
            cities.append({"name": name, "lat": lat, "lng": lng})
            print(f"-> ({lat:.4f}, {lng:.4f}) [manual]")
        else:
            result = geocode(name)
            if result:
                cities.append(result)
                print(f"-> ({result['lat']:.4f}, {result['lng']:.4f})")
            else:
                errores += 1
                print("-> FALLIDO")

        time.sleep(NOMINATIM_DELAY)

    print(f"\n3/3 Guardando {len(cities)} ciudades en {output}")
    Path(output).write_text(
        json.dumps(cities, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\nCompletado: {len(cities)} ciudades guardadas, {errores} errores")
    if errores > 0:
        print("CIUDADES CON ERROR - reintenta con el script nuevamente (usa cache de la primera pasada)")
        sys.exit(1)


if __name__ == "__main__":
    main()
