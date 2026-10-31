#!/usr/bin/env python3
"""Счетчик серьезностей замечаний в review-файле.

Скиллы: case-review, code-review. Считает blocker/major/minor/info по
маркерам severity в review-файле и проверяет пороги вердикта:
- blocker ≥ 1 → BLOCK (возврат, не merge);
- major ≥ 3 → CONCERNS (возврат на доработку точечно).

Usage:
    python severity_counter.py review-001-architecture.md
    python severity_counter.py openspec/changes/add-wiki/review-002-code.md --strict
"""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path

SEV_RE = re.compile(r"\b(blocker|blocker-уровень|major|minor|info)\b", re.IGNORECASE)
THRESHOLDS = {"blocker": 1, "major": 3}  # пороги вердикта (контракты скиллов)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("review", help="review-файл (.md)")
    ap.add_argument("--strict", action="store_true",
                    help="нарушение порогов = exit 2")
    args = ap.parse_args()

    p = Path(args.review)
    if not p.is_file():
        print(f"Файл не найден: {p}")
        return 1
    text = p.read_text(encoding="utf-8", errors="replace")
    counts = Counter(m.group(1).lower() for m in SEV_RE.finditer(text))
    # нормализация: 'blocker-уровень' уже пойман как blocker
    total = sum(counts.values())

    print(f"{p}: замечаний с severity: {total}")
    for sev in ("blocker", "major", "minor", "info"):
        print(f"  {sev}: {counts.get(sev, 0)}")

    verdict, reason = "APPROVE", "пороги не превышены"
    for sev, limit in THRESHOLDS.items():
        if counts.get(sev, 0) >= limit:
            verdict = "BLOCK" if sev == "blocker" else "CONCERNS"
            reason = f"{sev} ≥ {limit}"
            break
    print(f"Вердикт-подсказка: {verdict} ({reason})")
    print("Финальный вердикт — за ревьюером; скрипт только проверяет пороги.")
    if args.strict and verdict != "APPROVE":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
