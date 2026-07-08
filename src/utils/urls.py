import re
from typing import Optional


def extract_google_place_id(source_url: str) -> Optional[str]:
    """Extrae el identificador unico de Google Maps desde una URL de place.

    Busca el parametro data= que contiene el FTID canonico del lugar.
    Ej: /maps/place/.../data=!4m7!3m6!1s0x0:0xabc123 -> data=!4m7!3m6!1s0x0:0xabc123
    """
    if not source_url:
        return None

    match = re.search(r"data=(.+)$", source_url)
    if match:
        return match.group(1)

    # Fallback: extraer la ultima parte significativa del path
    # /maps/place/Some+Name/ChIJ... -> ChIJ...
    parts = source_url.rstrip("/").split("/")
    last = parts[-1]
    if last and "=" not in last and "@" not in last:
        return last

    return None
