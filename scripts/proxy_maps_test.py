#!/usr/bin/env python3
"""Segunda etapa: prueba los proxies de data/proxies.txt contra una busqueda
REAL de Google Maps (no solo generate_204). Detecta bloqueo/captcha/consent y
deja en el archivo solo los que devuelven contenido de Maps utilizable.

Uso: python scripts/proxy_maps_test.py [--concurrency 150] [--timeout 12]
"""
import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

MAPS_URL = "https://www.google.com/maps/search/farmacia/@-25.2820,-57.6350,14z?hl=es"
BLOCK_SIGNALS = ("unusual traffic", "recaptcha", "/sorry/", "not a robot",
                 "consent.google", "enablejs", "our systems have detected")


def test_maps(proxy: str, timeout: int) -> str:
    """Devuelve 'ok', 'block' o 'fail'."""
    try:
        r = subprocess.run(
            ["curl", "-s", "-L", "--compressed", "--proxy", proxy,
             "--max-time", str(timeout),
             "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
             "-w", "\n__HTTP__%{http_code}", MAPS_URL],
            capture_output=True, text=True, timeout=timeout + 5, errors="ignore",
        )
        body = r.stdout.lower()
        code = ""
        if "__http__" in body:
            code = body.rsplit("__http__", 1)[-1].strip()
        if not r.stdout or code not in ("200", ""):
            return "fail" if code == "" else "block"
        if any(s in body for s in BLOCK_SIGNALS):
            return "block"
        # Señal de contenido real de Maps
        if "/maps/" in body and ("apploadstate" in body or "window.app_" in body
                                 or "role=\"main\"" in body or "maps.google" in body):
            return "ok"
        return "block"
    except Exception:
        return "fail"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--infile", default="data/proxies.txt")
    ap.add_argument("--concurrency", type=int, default=150)
    ap.add_argument("--timeout", type=int, default=12)
    args = ap.parse_args()

    proxies = [l.strip() for l in Path(args.infile).read_text(encoding="utf-8").splitlines()
               if l.strip() and not l.startswith("#")]
    print(f"Probando {len(proxies)} proxies contra Google MAPS real "
          f"(concurrency={args.concurrency}, timeout={args.timeout}s)")

    ok, block, fail = [], 0, 0
    done = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(test_maps, p, args.timeout): p for p in proxies}
        for fut in as_completed(futs):
            done += 1
            res = fut.result()
            if res == "ok":
                ok.append(futs[fut])
            elif res == "block":
                block += 1
            else:
                fail += 1
            if done % 100 == 0:
                print(f"  {done}/{len(proxies)}: {len(ok)} OK, {block} block, {fail} fail")

    out = Path(args.infile)
    header = (f"# proxy_maps_test.py | probados={len(proxies)} "
              f"usables_en_maps={len(ok)} block={block} fail={fail}\n")
    out.write_text(header + "\n".join(ok) + "\n", encoding="utf-8")
    print(f"\n== RESULTADO MAPS: {len(ok)} usables | {block} bloqueados | {fail} caidos ==")
    print(f"data/proxies.txt reescrito con los {len(ok)} usables en Maps")


if __name__ == "__main__":
    main()
