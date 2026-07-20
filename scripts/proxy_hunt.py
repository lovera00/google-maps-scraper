#!/usr/bin/env python3
"""Caza proxies free de varias fuentes, los prueba TODOS contra Google en
paralelo y escribe solo los que funcionan a data/proxies.txt.

Fuentes:
  - ProxyScrape (API v4, text)        -> http/socks4/socks5, miles
  - Geonode (API json)                -> filtra google=true
  - ProxyDB (proxydb.net)             -> best-effort (suele estar tras Cloudflare)
  - Spys.one                          -> best-effort (puertos ofuscados con JS)

Test: curl --proxy <p> https://www.google.com/generate_204 (204/200 = OK),
concurrente. Requiere curl en PATH (viene con Windows 10+).

Uso:
    python scripts/proxy_hunt.py                    # todas las fuentes, test, escribe
    python scripts/proxy_hunt.py --no-test          # solo junta, no prueba
    python scripts/proxy_hunt.py --concurrency 300 --timeout 8
    python scripts/proxy_hunt.py --max-test 4000    # cap de proxies a probar
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _get(url: str, timeout=30, data=None, headers=None) -> bytes:
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ── Fuentes ────────────────────────────────────────────────────────────────

def src_proxyscrape() -> list[str]:
    out = []
    for proto in ("http", "socks4", "socks5"):
        try:
            url = (f"https://api.proxyscrape.com/v4/free-proxy-list/get?"
                   f"request=display_proxies&protocol={proto}&"
                   f"proxy_format=protocolipport&format=text")
            txt = _get(url, timeout=30).decode("utf-8", "ignore")
            got = [l.strip() for l in txt.splitlines() if "://" in l]
            out += got
            print(f"  proxyscrape[{proto}]: {len(got)}")
        except Exception as e:
            print(f"  proxyscrape[{proto}]: ERROR {e}", file=sys.stderr)
    return out


def src_geonode(google_only=True) -> list[str]:
    out = []
    try:
        params = {"page": 1, "limit": 500, "sort_by": "responseTime",
                  "sort_type": "asc", "protocols": "http,https,socks5"}
        if google_only:
            params["google"] = "true"
        data = json.loads(_get(f"https://proxylist.geonode.com/api/proxy-list?"
                               f"{urllib.parse.urlencode(params)}").decode())
        for p in data.get("data", []):
            ip, port = p.get("ip"), p.get("port")
            if not ip or not port:
                continue
            prot = p.get("protocols", ["http"])
            scheme = "socks5" if "socks5" in prot else "http"
            out.append(f"{scheme}://{ip}:{port}")
        print(f"  geonode(google_only={google_only}): {len(out)}")
    except Exception as e:
        print(f"  geonode: ERROR {e}", file=sys.stderr)
    return out


def src_proxydb() -> list[str]:
    out = []
    try:
        html = _get("https://proxydb.net/", timeout=30).decode("utf-8", "ignore")
        for ip, port in re.findall(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})", html):
            out.append(f"http://{ip}:{port}")
        print(f"  proxydb: {len(out)}")
    except Exception as e:
        print(f"  proxydb: ERROR {e} (suele estar tras Cloudflare)", file=sys.stderr)
    return out


def src_spys() -> list[str]:
    # Spys.one ofusca los puertos con JS (var*var). Best-effort: rara vez sirve.
    out = []
    try:
        html = _get("https://spys.one/en/free-proxy-list/", timeout=30).decode("utf-8", "ignore")
        # solo captura ip:port en claro si los hubiera (normalmente no)
        for ip, port in re.findall(r"(\d{1,3}(?:\.\d{1,3}){3})</font>[^0-9]{0,40}(\d{2,5})", html):
            out.append(f"http://{ip}:{port}")
        print(f"  spys.one: {len(out)} (puertos suelen estar ofuscados con JS)")
    except Exception as e:
        print(f"  spys.one: ERROR {e}", file=sys.stderr)
    return out


# ── Test de conectividad ─────────────────────────────────────────────────────

def test_proxy(proxy: str, timeout: int) -> bool:
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "NUL" if sys.platform == "win32" else "/dev/null",
             "-w", "%{http_code}", "--proxy", proxy, "--max-time", str(timeout),
             "https://www.google.com/generate_204"],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        return r.stdout.strip() in ("204", "200")
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/proxies.txt")
    ap.add_argument("--concurrency", type=int, default=200)
    ap.add_argument("--timeout", type=int, default=8)
    ap.add_argument("--max-test", type=int, default=6000)
    ap.add_argument("--no-test", dest="test", action="store_false")
    ap.set_defaults(test=True)
    args = ap.parse_args()

    print("== Juntando proxies ==")
    raw = []
    raw += src_proxyscrape()
    raw += src_geonode(google_only=True)
    raw += src_geonode(google_only=False)
    raw += src_proxydb()
    raw += src_spys()

    # dedupe preservando orden
    seen, pool = set(), []
    for p in raw:
        if p not in seen:
            seen.add(p)
            pool.append(p)
    print(f"Total unicos: {len(pool)}")

    if not args.test:
        Path(args.out).write_text("\n".join(pool) + "\n", encoding="utf-8")
        print(f"Escritos {len(pool)} (sin probar) en {args.out}")
        return

    pool = pool[: args.max_test]
    print(f"== Probando {len(pool)} proxies contra Google "
          f"(concurrency={args.concurrency}, timeout={args.timeout}s) ==")
    working = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(test_proxy, p, args.timeout): p for p in pool}
        for fut in as_completed(futs):
            done += 1
            if fut.result():
                working.append(futs[fut])
            if done % 250 == 0:
                print(f"  {done}/{len(pool)} probados, {len(working)} OK")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (f"# proxy_hunt.py | probados={len(pool)} funcionan={len(working)}\n"
              f"# test: curl generate_204, timeout={args.timeout}s\n")
    out.write_text(header + "\n".join(working) + "\n", encoding="utf-8")
    print(f"\n== RESULTADO: {len(working)}/{len(pool)} proxies funcionan con Google ==")
    print(f"Escritos en {out}")
    if working:
        print("Ejemplos:", ", ".join(working[:5]))


if __name__ == "__main__":
    main()
