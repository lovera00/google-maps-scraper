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
    parser.add_argument("--round-robin", action="store_true",
                        help="(Obsoleto: ya es el comportamiento por defecto) Reparte "
                             "intercalado tarea i -> shard i%%n.")
    parser.add_argument("--sequential", action="store_true",
                        help="Reparte en bloques contiguos (comportamiento viejo). El "
                             "archivo viene ordenado urbano->rural, asi que esto concentra "
                             "las zonas densas (mas lentas y con mas overflow) en el primer "
                             "shard y desbalancea el run distribuido. NO recomendado.")
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

    # Por defecto round-robin: como el JSONL viene ordenado por prioridad
    # (urbano -> rural, ver QueryPlanner.generate_initial_tasks), repartir
    # tarea i -> shard i%n produce un muestreo estratificado: cada shard recibe
    # una mezcla pareja de zonas densas y vacias y todos tardan parecido.
    # El modo contiguo (--sequential) concentraria todo lo urbano en el shard 1.
    use_round_robin = not args.sequential
    if use_round_robin:
        shards = [[] for _ in range(n)]
        for i, line in enumerate(lines):
            shards[i % n].append(line)
    else:
        # bloques secuenciales (comportamiento original, desbalanceado)
        base = total // n
        extra = total % n
        shards = []
        start = 0
        for i in range(n):
            size = base + (1 if i < extra else 0)
            shards.append(lines[start:start + size])
            start += size

    for i, chunk in enumerate(shards):
        shard_path = output_dir / f"shard_{i + 1:03d}.jsonl"
        with open(shard_path, "w", encoding="utf-8") as f:
            f.writelines(chunk)
        print(f"shard_{i + 1:03d}.jsonl: {len(chunk)} tareas ({100 * len(chunk) / total:.1f}%)")

    modo = "round-robin (balanceado)" if use_round_robin else "secuencial (contiguo, desbalanceado)"
    print(f"\n{total} tareas divididas en {n} shards ({modo}) en {output_dir}")


if __name__ == "__main__":
    main()
