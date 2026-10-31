#!/usr/bin/env python3
"""Реестр нумерации требований (FR-N/NFR-N/US-N/TC-N).

Скилл: requirements-elaboration. Сканирует файлы с требованиями и строит
реестр: занятые номера по префиксу, максимальный номер, дубликаты.
Использование при продолжении нумерации (не выдумывать номера) и при
ревью (дубликаты = дефект).

Usage:
    python numbering_registry.py openspec/ docs/ba/
    python numbering_registry.py openspec/add-wiki/specs/ --prefix FR
"""
import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

# Префиксы требований конвейера (контракт requirements-elaboration)
KNOWN_PREFIXES = ["FR", "NFR", "US", "TC", "SC"]
# FR-3.1 / NFR-12 / US-7 / TC-wiki-403
REQ_RE = re.compile(r"\b(FR|NFR|US|TC|SC)-([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)-(\d+(?:\.\d+)?)\b")


def scan(paths, prefix=None):
    """→ {prefix: {'ids': {id: [file:line]}, 'max_main': int}}"""
    registry = defaultdict(lambda: defaultdict(list))
    targets = [Path(p) for p in paths]
    files = []
    for t in targets:
        if t.is_file():
            files.append(t)
        elif t.is_dir():
            files.extend(
                f for f in sorted(t.rglob("*.md"))
                if "archive" not in f.parts and "node_modules" not in f.parts
            )
    for f in files:
        try:
            text: str = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for pref, mid, num in REQ_RE.findall(line):
                if prefix and pref != prefix:
                    continue
                registry[pref][f"{pref}-{mid}-{num}"].append(f"{f}:{i}")
    return registry


def report(registry, check_duplicates=True):
    lines = []
    problems = 0
    for pref in sorted(registry):
        ids = registry[pref]
        mains = []
        for rid in sorted(ids):
            main = rid.rsplit("-", 1)[-1].split(".")[0]
            try:
                mains.append((int(main), rid))
            except ValueError:
                continue
        max_main = max((m for m, _ in mains), default=0)
        lines.append(f"== {pref}: {len(ids)} идентификаторов, max N = {max_main}")
        # Продолжение нумерации: следующий свободный
        used_mains = sorted({m for m, _ in mains})
        gaps = [n for n in range(1, max_main + 1) if n not in used_mains]
        if gaps:
            lines.append(f"   пропуски в основной нумерации: {gaps}")
        if check_duplicates:
            for rid, locs in sorted(ids.items()):
                if len(locs) > 1:
                    problems += 1
                    lines.append(f"   ДУБЛИКАТ {rid}: {', '.join(locs[:4])}"
                                 + (f" (+{len(locs)-4})" if len(locs) > 4 else ""))
    return "\n".join(lines), problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", help="каталоги/файлы с требованиями")
    ap.add_argument("--prefix", choices=KNOWN_PREFIXES, help="фильтр по префиксу")
    ap.add_argument("--no-dup-check", action="store_true")
    args = ap.parse_args()
    registry = scan(args.paths, args.prefix)
    if not registry:
        print("Идентификаторы требований не найдены")
        return 1
    text, problems = report(registry, not args.no_dup_check)
    print(text)
    if problems:
        print(f"\nДЕФЕКТ: {problems} дубликатов — нумерацию продолжать только "
              "после разбора (требование numbering-дисциплины)")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
