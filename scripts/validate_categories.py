#!/usr/bin/env python3
"""Validador de extraccion de categorias.

Analiza archivos HTML de debug (data/debug_html/) o un mock HTML y muestra
que categoria extrajo cada estrategia. Util para diagnosticar si los selectores
estan funcionando con el DOM actual de Google Maps.

Uso:
    python scripts/validate_categories.py data/debug_html/live_*.html
    python scripts/validate_categories.py --mock
    python scripts/validate_categories.py --all
"""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bs4 import BeautifulSoup
from src.agents.data_collector import DataCollector
from src.config.loader import load_config


def analyze_file(html_path: Path, collector: DataCollector) -> dict:
    """Analiza un archivo HTML y devuelve estadisticas de extraccion."""
    html = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")

    # Usar el nuevo parser (basado en contenedores Nv2PK)
    parsed = collector._parse_results(soup)

    results = []
    for r in parsed:
        results.append({
            "name": r["name"],
            "category": r.get("category", ""),
            "strategy": "parser Nv2PK",
            "href": r.get("source_url", "")[:80],
        })

    return {
        "file": html_path.name,
        "total_cards": len(soup.select('a.hfpxzc')),
        "unique_results": len(results),
        "with_category": sum(1 for r in results if r["category"]),
        "without_category": sum(1 for r in results if not r["category"]),
        "results": results,
    }


def print_report(stats: dict, verbose: bool = False):
    """Imprime el reporte de un archivo."""
    print(f"\n{'='*70}")
    print(f"  Archivo: {stats['file']}")
    print(f"  Cards encontrados: {stats['total_cards']}")
    print(f"  Resultados unicos: {stats['unique_results']}")
    print(f"  Con categoria:     {stats['with_category']}")
    print(f"  Sin categoria:     {stats['without_category']}")
    print(f"  Tasa de exito:     {stats['with_category']/max(stats['unique_results'],1)*100:.0f}%")

    if verbose and stats["results"]:
        print(f"\n  {'Nombre':<35s} {'Categoria':<25s} {'Estrategia'}")
        print(f"  {'-'*35} {'-'*25} {'-'*20}")
        for r in stats["results"]:
            cat = r["category"] if r["category"] else "(vacia)"
            print(f"  {r['name'][:34]:<35s} {cat[:24]:<25s} {r['strategy']}")


def main():
    parser = argparse.ArgumentParser(description="Validar extraccion de categorias de HTML")
    parser.add_argument("files", nargs="*", help="Archivos HTML a analizar")
    parser.add_argument("--mock", action="store_true", help="Usar mocks HTML locales")
    parser.add_argument("--all", action="store_true", help="Analizar todos los debug_html/*.html")
    parser.add_argument("--verbose", "-v", action="store_true", help="Mostrar cada resultado")
    args = parser.parse_args()

    config = load_config("config.yaml")
    config.test_mode = True
    collector = DataCollector(config)

    all_stats = []

    if args.all:
        debug_dir = Path("data/debug_html")
        if debug_dir.exists():
            args.files = list(debug_dir.glob("*.html"))
            if not args.files:
                print("No hay archivos HTML en data/debug_html/")
        else:
            print("Directorio data/debug_html/ no existe. Corre el scraper live primero.")
            return

    if args.mock:
        mock_dir = Path("mocks/google_maps")
        mock_files = list(mock_dir.glob("*.html"))
        if not mock_files:
            print("No hay mocks en mocks/google_maps/")
            return
        args.files = mock_files

    if not args.files:
        print("Especifica archivos HTML, --mock, o --all")
        print("Ejemplo: python scripts/validate_categories.py data/debug_html/*.html")
        return

    for f in args.files:
        path = Path(f)
        if not path.exists():
            print(f"  [SKIP] No existe: {f}")
            continue
        try:
            stats = analyze_file(path, collector)
            all_stats.append(stats)
            print_report(stats, verbose=args.verbose)
        except Exception as e:
            print(f"  [ERROR] {f}: {e}")

    # Resumen global
    if len(all_stats) > 1:
        total_cards = sum(s["total_cards"] for s in all_stats)
        total_results = sum(s["unique_results"] for s in all_stats)
        total_with = sum(s["with_category"] for s in all_stats)
        print(f"\n{'='*70}")
        print(f"  RESUMEN ({len(all_stats)} archivos)")
        print(f"  Total cards:       {total_cards}")
        print(f"  Total resultados:  {total_results}")
        print(f"  Con categoria:     {total_with}")
        print(f"  Tasa de exito:     {total_with/max(total_results,1)*100:.0f}%")

        # Distribucion de estrategias
        all_strategies = Counter()
        for s in all_stats:
            for r in s["results"]:
                if r["category"]:
                    all_strategies[r["strategy"]] += 1
        if all_strategies:
            print(f"\n  Estrategias usadas:")
            for strategy, count in all_strategies.most_common():
                print(f"    {strategy}: {count} resultados")


if __name__ == "__main__":
    main()
