#!/usr/bin/env python3
"""smoke_static.py — пост-деплойная проверка отдачи статики (E10, класс DEF-002).

Гиперлокальная детекция «прод не отдает статику»: сверяет (1) все статик-файлы,
на которые реально ссылаются шаблоны страниц, с (2) файлами на диске, затем
опционально дергает каждый по прод-URL (или любому base) и требует 200.

Usage:
  python3 scripts/smoke_static.py --repo /opt/ekotov-wiki --base https://HOST:10443
  python3 scripts/smoke_static.py --repo /opt/ekotov-wiki          # без base: только сверка ссылок с диском

Exit: 0 = OK, 1 = есть проблемы (детально в выводе).
"""
import argparse
import re
import sys
import urllib.request
import ssl
from pathlib import Path

RX = re.compile(r"(?:static_v\(\s*')?(?:/?)static/([a-z]+/[A-Za-z0-9_./-]+\.(?:css|js))")


def collect_linked(repo: Path) -> set[str]:
    """Статик-пути, на которые ссылаются шаблоны/страницы backend и HTML frontend."""
    linked: set[str] = set()
    for pat in ("backend/app/*.py", "frontend/templates/*.html"):
        for f in repo.glob(pat):
            text = f.read_text(encoding="utf-8", errors="replace")
            for m in RX.finditer(text):
                linked.add(m.group(1))
    return linked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", default="")
    ap.add_argument("--insecure", action="store_true", help="не проверять TLS (self-signed)")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()

    linked = collect_linked(repo)
    if not linked:
        print("SMOKE-STATIC FAIL: не найдено ни одной статик-ссылки — проверь regex/шаблоны")
        return 1

    problems = []
    # 1. Сверка: каждая ссылка существует на диске
    for rel in sorted(linked):
        if not (repo / "frontend/static" / rel).is_file():
            problems.append(f"битая ссылка: static/{rel} — файла нет на диске")

    # 2. Прод-проверка: каждый файл по HTTP = 200
    if args.base:
        ctx = ssl.create_default_context()
        if args.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        for rel in sorted(linked):
            url = f"{args.base}/static/{rel}"
            try:
                code = urllib.request.urlopen(url, timeout=5, context=ctx).status
            except Exception as e:  # noqa: BLE001
                problems.append(f"{url}: недоступен ({e})")
                continue
            if code != 200:
                problems.append(f"{url}: HTTP {code} (ожидался 200)")

    print(f"SMOKE-STATIC: {len(linked)} статик-ресурсов, проблем: {len(problems)}")
    for p in problems:
        print(f"  - {p}")
    if problems:
        print("SMOKE-STATIC FAIL — класс DEF-002 (статика не отдается)")
        return 1
    print("SMOKE-STATIC OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
