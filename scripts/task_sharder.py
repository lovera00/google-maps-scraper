#!/usr/bin/env python3
"""Divide un archivo JSONL de tareas en N shards balanceados.

Uso:
    python -m scripts.task_sharder --input data/tasks.jsonl --shards 5 --output-dir data/shards/
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Dividir tareas en shards")
    parser.add_argument("--input", required=True, help="Archivo JSONL con todas las tareas")
    parser.add_argument("--shards", type=int, required=True, help="Numero de shards")
    parser.add_argument("--output-dir", default="data/shards", help="Directorio de salida")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: archivo no encontrado: {args.input}")
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]

    total = len(lines)
    if total == 0:
        print("Error: el archivo de tareas esta vacio")
        sys.exit(1)

    n = args.shards
    if n > total:
        print(f"Advertencia: {n} shards para {total} tareas. Se crearan solo {total} shards.")
        n = total

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    base = total // n
    extra = total % n

    start = 0
    for i in range(n):
        size = base + (1 if i < extra else 0)
        chunk = lines[start:start + size]
        shard_path = output_dir / f"shard_{i + 1:03d}.jsonl"
        with open(shard_path, "w", encoding="utf-8") as f:
            f.writelines(chunk)
        print(f"shard_{i + 1:03d}.jsonl: {len(chunk)} tareas ({100 * len(chunk) / total:.1f}%)")
        start += size

    print(f"\n{total} tareas divididas en {n} shards en {output_dir}")


if __name__ == "__main__":
    main()
