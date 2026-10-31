#!/usr/bin/env python3
"""Аудит локаторов playwright-тестов: где остался «сырой» код.

Скилл: playwright-pro. Считает вызовы локаторов по тест-файлам, проверяет
приоритет локаторов (get_by_role → … → CSS/XPath — крайний случай) и
покрытие Page Objects (тест без импорта pages/ и с большим числом сырых
локаторов — кандидат на PO-конвертацию).

Usage:
    python locator_audit.py tests/web
    python locator_audit.py tests/web --pages-dir tests/web/pages --threshold 10
    python locator_audit.py tests/web --strict   # violations > 0 → exit 2
"""
import argparse
import re
import sys
from pathlib import Path

# Приоритет локаторов (playwright-pro): 1..6 — семантические, 7 test_id, 8 CSS/XPath
LOCATORS = [
    ("get_by_role", 1), ("get_by_label", 2), ("get_by_text", 3),
    ("get_by_placeholder", 4), ("get_by_alt_text", 5), ("get_by_title", 6),
    ("get_by_test_id", 7),
]
RAW_RE = re.compile(r"\b(" + "|".join(n for n, _ in LOCATORS) + r")\b")
CSS_XPATH_RE = re.compile(r"page\.locator\(\s*['\"]|^from playwright|xpath=")


def scan_file(path: Path) -> dict:
    text: str = path.read_text(encoding="utf-8", errors="replace")
    counts = {name: len(re.findall(rf"\.{name}\(|\bpage\.{name}\(", text))
              for name, _ in LOCATORS}
    raw_total = sum(counts.values())
    css = len(re.findall(r"\.locator\(\s*['\"](?:#|\.|//|/)", text))
    uses_pages = "pages" in text and ("import" in text)
    return {"file": str(path), "raw": raw_total, "counts": counts,
            "css_xpath": css, "uses_pages": uses_pages}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tests_dir", help="каталог web-тестов (tests/web)")
    ap.add_argument("--pages-dir", default=None,
                    help="каталог Page Objects (default: <tests_dir>/pages)")
    ap.add_argument("--threshold", type=int, default=10,
                    help="сырых локаторов в файле без PO = кандидат (default 10)")
    ap.add_argument("--strict", action="store_true", help="violations → exit 2")
    args = ap.parse_args()

    base = Path(args.tests_dir)
    pages_dir = Path(args.pages_dir) if args.pages_dir else base / "pages"
    files = sorted(p for p in base.glob("*.py") if p.name != "conftest.py")

    rows = [scan_file(f) for f in files]
    total_raw = sum(r["raw"] for r in rows)
    po_files = len(list(pages_dir.glob("*.py"))) if pages_dir.is_dir() else 0

    print(f"Тест-файлов: {len(rows)}; PO-файлов: {po_files}; "
          f"сырых локаторов всего: {total_raw}")

    candidates = [r for r in rows
                  if r["raw"] >= args.threshold and not r["uses_pages"]]
    if candidates:
        print(f"\nКАНДИДАТЫ НА PO-КОНВЕРТАЦИЮ ({len(candidates)}):")
        for r in sorted(candidates, key=lambda r: -r["raw"]):
            print(f"  - {r['file']}: {r['raw']} сырых локаторов, pages/ не использует")
    else:
        print("\nКандидатов на конвертацию нет (все крупные файлы на PO).")

    css_users = [r for r in rows if r["css_xpath"]]
    if css_users:
        print(f"\nCSS/XPath-локаторы (крайний случай по playwright-pro): "
              f"{sum(r['css_xpath'] for r in css_users)} в {len(css_users)} файлах:")
        for r in sorted(css_users, key=lambda r: -r["css_xpath"])[:5]:
            print(f"  - {r['file']}: {r['css_xpath']}")

    violations = len(candidates)
    if args.strict and violations:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
