#!/usr/bin/env python3
"""Проверка швов нарезки задач (seam checker).

Скилл: service-decomposition. Принимает зоны задач (файлы/каталоги) и ищет
ПЕРЕСЕЧЕНИЯ — нарушение правила «зоны задач не пересекаются» (§4 контракта).
Порядок аргументов = порядок задач из tasks.md.

Usage:
    python seam_checker.py T1:backend/app/wiki.py T2:backend/app/pages.py
    python seam_checker.py T1:backend/ T2:backend/app/wiki.py --strict
"""
import argparse
import sys
from pathlib import Path


def norm(p: str) -> str:
    return str(Path(p.strip()).as_posix()).rstrip("/") or "/"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("zones", nargs="+",
                    help="зоны в формате ID:путь (T1:backend/app/)")
    ap.add_argument("--strict", action="store_true",
                    help="пересечение = код выхода 2 (для CI)")
    args = ap.parse_args()

    parsed = []
    for z in args.zones:
        if ":" not in z:
            print(f"ОШИБКА: зона без ID: '{z}' (формат ID:путь)")
            return 1
        tid, _, path = z.partition(":")
        parsed.append((tid.strip(), norm(path)))

    problems = []
    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            a_id, a = parsed[i]
            b_id, b = parsed[j]
            # пересечение префиксов путей в обе стороны
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                problems.append(f"ПЕРЕСЕЧЕНИЕ {a_id} ({a}) ↔ {b_id} ({b})")

    print(f"Зон: {len(parsed)}; пересечений: {len(problems)}")
    for p in problems:
        print(" ", p)
    if problems:
        print("Нарушено §4 (зоны не пересекаются): пере-нарезать задачи "
              "или явно оформить [P]-параллель с договоренностью о порядке коммитов.")
        return 2 if args.strict else 1
    print("Швы чистые.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
