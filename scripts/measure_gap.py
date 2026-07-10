#!/usr/bin/env python3
"""Mide el gap de cobertura de tu base contra un reporte HTML de PlaceAnalyzer.

Extrae los comercios embebidos en el HTML (coordenadas + nombre + categoria) y los
cruza espacialmente contra tu base SQLite. Reporta cuantos tenes, cuantos faltan de
verdad, y en que categorias/tipos se concentra el gap.

Uso:
    python -m scripts.measure_gap --html "C:/ruta/al/Reporte.html" --db data/paraguay_businesses.db
"""
import argparse
import math
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def haversine(a1, o1, a2, o2):
    R = 6371000
    p1, p2 = math.radians(a1), math.radians(a2)
    dp = math.radians(a2 - a1)
    do = math.radians(o2 - o1)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(do / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or "").lower())


def safe(s):
    # imprime sin romper en consolas cp1252
    return s.encode("ascii", "replace").decode("ascii")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--html", required=True, help="Ruta al HTML de PlaceAnalyzer")
    p.add_argument("--db", default="data/paraguay_businesses.db")
    p.add_argument("--examples", type=int, default=20)
    args = p.parse_args()

    html = open(args.html, encoding="utf-8", errors="replace").read()
    pat = re.compile(
        r'\{"coordinates":"POINT\(([-\d.]+) ([-\d.]+)\)","type":"(.*?)","category":"(.*?)","name":"(.*?)"')
    pa = []
    for m in pat.finditer(html):
        lng, lat, typ, cat, name = m.groups()
        try:
            lng, lat = float(lng), float(lat)
        except ValueError:
            continue
        try:
            name = name.encode().decode('unicode_escape', errors='replace')
        except Exception:
            pass
        pa.append({"lat": lat, "lng": lng, "type": typ, "cat": cat, "name": name})

    if not pa:
        print("No se extrajo ningun comercio del HTML (patron no coincide).")
        return
    print(f"PlaceAnalyzer: {len(pa)} comercios")

    lats = [b["lat"] for b in pa]
    lngs = [b["lng"] for b in pa]
    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    cur.execute("""SELECT name, lat, lng FROM businesses WHERE is_active=1
                   AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?""",
                (min(lats) - 0.02, max(lats) + 0.02, min(lngs) - 0.02, max(lngs) + 0.02))
    db = cur.fetchall()
    print(f"Tu DB en la zona: {len(db)} comercios")

    db_norm = set(norm(n) for n, _, _ in db)
    grid = {}
    for n, lat, lng in db:
        grid.setdefault((round(lat, 3), round(lng, 3)), []).append((norm(n), lat, lng))

    prox = name_far = truly = 0
    truly_list = []
    for b in pa:
        nb = norm(b["name"])
        ka, ko = round(b["lat"], 3), round(b["lng"], 3)
        cand = []
        for da in (-1, 0, 1):
            for do in (-1, 0, 1):
                cand += grid.get((round(ka + da * 0.001, 3), round(ko + do * 0.001, 3)), [])
        hit = False
        for nn, lat, lng in cand:
            d = haversine(b["lat"], b["lng"], lat, lng)
            if d <= 25:
                hit = True
                break
            if d <= 70 and nb and nn and (nb[:8] == nn[:8] or nb in nn or nn in nb):
                hit = True
                break
        if hit:
            prox += 1
        elif nb and len(nb) >= 5 and nb in db_norm:
            name_far += 1
        else:
            truly += 1
            truly_list.append(b)

    total = len(pa)
    print("\n=== RESULTADO ===")
    print(f"Lo tenemos (cerca):                 {prox} ({prox/total*100:.1f}%)")
    print(f"Nombre en zona, coords distintas:   {name_far} ({name_far/total*100:.1f}%)")
    print(f"AUSENCIA REAL:                      {truly} ({truly/total*100:.1f}%)")

    print("\n=== Ausencias reales por TIPO (top 25) ===")
    for t, n in Counter(b['type'] for b in truly_list).most_common(25):
        print(f"  {n:>4}  {safe(t)}")

    if args.examples:
        print(f"\n=== Ejemplos ({args.examples}) ===")
        for b in truly_list[:args.examples]:
            print(f"  [{safe(b['type'])}] {safe(b['name'])}  ({b['lat']:.5f},{b['lng']:.5f})")


if __name__ == "__main__":
    main()
