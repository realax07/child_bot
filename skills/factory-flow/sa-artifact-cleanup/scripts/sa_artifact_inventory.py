#!/usr/bin/env python3
"""Инвентаризация артефактов стадии SA (аналитика) — кандидаты на чистку.

Сканирует конвейерный репозиторий и печатает markdown-отчет (или JSON c --json)
о промежуточных и основных артефактах аналитики: активные changes в
openspec/changes/, выполненные, но неархивированные; заброшенные (нет коммитов
N дней); docs/ba/ — требования и ответы; битые ссылки на несуществующие
изменения. Ничего не удаляет — только инвентаризация для сабагента-чистильщика.

stdlib-only. Выход: 0 всегда (инвентаризация).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

CHECKED = re.compile(r"^- \[x\]", re.IGNORECASE | re.MULTILINE)
UNCHECKED = re.compile(r"^- \[ \]", re.MULTILINE)


def git(repo: Path, *args: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def last_commit_ts(repo: Path, rel: str) -> float | None:
    out = git(repo, "log", "-1", "--format=%ct", "--", rel)
    return float(out) if out.isdigit() else None


def age_days(ts: float | None) -> float | None:
    return None if ts is None else (time.time() - ts) / 86400


def human_age(days: float | None) -> str:
    return "n/a" if days is None else f"{days:.0f} дн."


def scan_changes(repo: Path, stale_days: int) -> list[dict]:
    """Активные changes: выполненность задач, давность, следы завершения."""
    root = repo / "openspec" / "changes"
    if not root.is_dir():
        return []
    out = []
    for d in sorted(p for p in root.iterdir()
                    if p.is_dir() and p.name not in ("archive", "_template")
                    and not p.name.startswith("_")):
        tasks = d / "tasks.md"
        total = done = 0
        if tasks.is_file():
            text = tasks.read_text(encoding="utf-8", errors="replace")
            done = len(CHECKED.findall(text))
            total = done + len(UNCHECKED.findall(text))
        ts = last_commit_ts(repo, f"openspec/changes/{d.name}")
        age = age_days(ts)
        archived_hint = (d / "archive").is_dir()
        out.append({
            "change": d.name,
            "tasks_total": total,
            "tasks_done": done,
            "all_done": total > 0 and done == total,
            "no_commits_days": age,
            "stale": age is not None and age >= stale_days,
            "archive_dir_present": archived_hint,
            "last_commit": ts,
        })
    return out


def scan_ba_docs(repo: Path) -> list[dict]:
    """docs/ba/*.md: давность и упоминания несуществующих changes."""
    docs = repo / "docs" / "ba"
    if not docs.is_dir():
        return []
    changes_root = repo / "openspec" / "changes"
    known = {p.name.lower() for p in changes_root.iterdir()} if changes_root.is_dir() else set()
    archive = changes_root.parent / "archive"
    if archive.is_dir():
        known |= {p.name.lower() for p in archive.iterdir()}
    out = []
    for f in sorted(docs.glob("*.md")):
        ts = last_commit_ts(repo, f"docs/ba/{f.name}")
        refs = set(re.findall(r"openspec/changes/([A-Za-z0-9._-]+)", f.read_text(encoding="utf-8", errors="replace")))
        dead = sorted(r for r in refs if r.lower() not in known and (changes_root / r).parent == changes_root)
        out.append({
            "doc": f"docs/ba/{f.name}",
            "age": age_days(ts),
            "referenced_changes": sorted(refs),
            "dead_change_refs": dead,
        })
    return out


def scan_orphan_impact(repo: Path) -> list[str]:
    """impact-файлы вне какого-либо change-каталога (осиротевшие)."""
    out = []
    for p in repo.rglob("impact*.md"):
        rel = p.relative_to(repo)
        parts = rel.parts
        if len(parts) >= 2 and parts[0] == "openspec" and "changes" in parts:
            continue
        if any(s in str(rel) for s in (".git", "node_modules", "archive", "test-model")):
            continue
        out.append(str(rel))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repo", nargs="?", default=".", help="путь к репозиторию проекта")
    ap.add_argument("--stale-days", type=int, default=14,
                    help="порог заброшенности в днях (по умолчанию 14)")
    ap.add_argument("--json", action="store_true", help="JSON вместо markdown")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"[FAIL] не git-репозиторий: {repo}", file=sys.stderr)
        return 2

    changes = scan_changes(repo, args.stale_days)
    ba_docs = scan_ba_docs(repo)
    orphans = scan_orphan_impact(repo)

    data = {
        "repo": str(repo),
        "stale_days": args.stale_days,
        "changes": changes,
        "ba_docs": ba_docs,
        "orphan_impacts": orphans,
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(f"# Инвентаризация SA-артефактов: {repo.name}")
    print(f"Порог заброшенности: {args.stale_days} дн.\n")
    print("## Активные changes (openspec/changes/)\n")
    if not changes:
        print("- каталога нет или пуст\n")
    for c in changes:
        flags = []
        if c["all_done"]:
            flags.append("ВСЕ ЗАДАЧИ ЗАКРЫТЫ → кандидат в archive_change")
        if c["stale"]:
            flags.append(f"ЗАБРОШЕН ({human_age(c['no_commits_days'])} без коммитов)")
        if c["archive_dir_present"]:
            flags.append("есть каталог archive/ внутри — неканоничное место")
        print(f"- **{c['change']}** — задач {c['tasks_done']}/{c['tasks_total']}, "
              f"коммиты: {human_age(c['no_commits_days'])}"
              + (f" — {'; '.join(flags)}" if flags else ""))
    print("\n## docs/ba/ (требования, ответы ОВ)\n")
    if not ba_docs:
        print("- каталога нет\n")
    for d in ba_docs:
        line = f"- {d['doc']} — обновлен {human_age(d['age'])}"
        if d["dead_change_refs"]:
            line += f" — БИТЫЕ ССЫЛКИ на changes: {', '.join(d['dead_change_refs'])}"
        print(line)
    print("\n## Осиротевшие impact-файлы (вне openspec/changes/)\n")
    print(("\n".join(f"- {o}" for o in orphans)) if orphans else "- нет")
    print("\nДальше: сабагент-чистильщик проверяет каждого кандидата (ссылки, "
          "git log) и архивирует/удаляет. Решение об удалении approved-артефактов — за ПМ.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
