#!/usr/bin/env python3
"""Trae proxies de la API publica de geonode y los escribe a data/proxies.txt
en el formato que entiende el ProxyManager (una linea `scheme://host:port`).

Los proxies gratuitos MUEREN rapido y son compartidos: corre este script cada
vez que quieras refrescar la lista, y filtra por google=true (geonode marca los
que pasan su check contra Google) para no cargar basura que Maps ya bloquea.

Uso:
    python scripts/fetch_proxies.py                      # google=true, http/https
    python scripts/fetch_proxies.py --no-google-only     # toda la lista (mas, peor)
    python scripts/fetch_proxies.py --min-uptime 90 --limit 500
    python scripts/fetch_proxies.py --protocols http,https,socks5
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://proxylist.geonode.com/api/proxy-list"


def fetch_page(page: int, limit: int, protocols: str, google_only: bool) -> dict:
    params = {
        "page": page,
        "limit": limit,
        "sort_by": "responseTime",
        "sort_type": "asc",
        "protocols": protocols,
    }
    if google_only:
        params["google"] = "true"
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def scheme_for(protocols: list[str]) -> str:
    # Playwright: para un proxy HTTP el server va como http:// (tuneliza https
    # via CONNECT). socks5 solo si el proxy lo habla.
    if "socks5" in protocols:
        return "socks5"
    if "socks4" in protocols:
        return "socks4"
    return "http"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/proxies.txt")
    ap.add_argument("--limit", type=int, default=500, help="proxies por pagina")
    ap.add_argument("--pages", type=int, default=1, help="cuantas paginas traer")
    ap.add_argument("--protocols", default="http,https",
                    help="lista separada por coma (http,https,socks5,socks4)")
    ap.add_argument("--min-uptime", type=float, default=0.0,
                    help="descartar proxies con upTime menor a este %%")
    ap.add_argument("--no-google-only", dest="google_only", action="store_false",
                    help="NO filtrar por google=true (trae mas, pero peores)")
    ap.set_defaults(google_only=True)
    args = ap.parse_args()

    seen = set()
    lines = []
    total = None
    for page in range(1, args.pages + 1):
        try:
            data = fetch_page(page, args.limit, args.protocols, args.google_only)
        except Exception as e:
            print(f"Error al traer pagina {page}: {e}", file=sys.stderr)
            break
        total = data.get("total", total)
        rows = data.get("data", [])
        if not rows:
            break
        for p in rows:
            ip, port = p.get("ip"), p.get("port")
            if not ip or not port:
                continue
            if p.get("upTime", 0) < args.min_uptime:
                continue
            scheme = scheme_for(p.get("protocols", []))
            entry = f"{scheme}://{ip}:{port}"
            if entry not in seen:
                seen.add(entry)
                lines.append(entry)
        if len(rows) < args.limit:
            break  # ultima pagina

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Proxies generados por scripts/fetch_proxies.py (geonode)\n"
        f"# google_only={args.google_only} protocols={args.protocols} "
        f"min_uptime={args.min_uptime}\n"
        f"# total reportado por la API: {total}\n"
    )
    out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"Escritos {len(lines)} proxies en {out} "
          f"(google_only={args.google_only}, total API={total})")
    if len(lines) < 10:
        print("AVISO: muy pocos proxies. Los gratuitos que funcionan con Google "
              "son escasos; considera proxies pagos para throughput real.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
