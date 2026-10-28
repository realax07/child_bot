#!/usr/bin/env python3
"""Инвентаризация артефактов стадии QA — кандидаты на чистку.

Сканирует конвейерный репозиторий: test-model/ (new, approved, checklists,
reports, regress, impact, bugs) — осиротевшие наборы без живого change;
tests/ — файлы без TC-маркеров в докстрингах, дубли имен тестов; покрытие
TC-ID approved-кейсов автотестами; давность regress/reports. Ничего не
удаляет — только инвентаризация для сабагента-чистильщика.

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

TC_RE = re.compile(r"TC-[A-Za-z0-9]+-?\d*")
TESTDEF = re.compile(r"^def (test_\w+)", re.MULTILINE)


def git(repo: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def last_commit_ts(repo: Path, rel: str) -> float | None:
    out = git(repo, "log", "-1", "--format=%ct", "--", rel)
    return float(out) if out.isdigit() else None


def age_days(ts):
    return None if ts is None else (time.time() - ts) / 86400


def human(days):
    return "n/a" if days is None else f"{days:.0f} дн."


def scan_test_model(repo: Path) -> dict:
    tm = repo / "test-model"
    result: dict = {}
    if not tm.is_dir():
        return result
    active_changes = {p.name for p in (repo / "openspec" / "changes").iterdir()} \
        if (repo / "openspec" / "changes").is_dir() else set()
    archive = repo / "openspec" / "archive"
    archived = {p.name for p in archive.iterdir()} if archive.is_dir() else set()
    for sub in sorted(p for p in tm.iterdir() if p.is_dir()):
        entries = sorted(p for p in sub.iterdir())
        if not entries:
            result[sub.name] = {"empty": True}
            continue
        historical = sub.name == "archive"  # история релизов — не кандидаты
        items = []
        infra = []
        for e in entries:
            if e.is_file() and e.suffix in (".json", ".cfg", ".ini", ".lock"):
                infra.append(e.name)  # служебные манифесты — не кандидаты
                continue
            name = e.name
            if historical or name.startswith("_"):
                continue
            change_id = next((c for c in active_changes | archived
                              if name.startswith(c)), None)
            ts = last_commit_ts(repo, str(e.relative_to(repo)))
            items.append({
                "entry": f"test-model/{sub.name}/{name}",
                "change_id": change_id,
                "change_alive": change_id in active_changes if change_id else None,
                "age": age_days(ts),
            })
        result[sub.name] = {
            "empty": False,
            "infra_files": infra,
            "items": items,
            "orphans": [i for i in items if i["change_id"] is None],
            "archived_only": [i for i in items
                              if i["change_id"] and not i["change_alive"]],
        }
    return result


def scan_tests(repo: Path) -> dict:
    out = {"no_tc_marker": [], "duplicates": [], "total_files": 0}
    seen: dict[str, list[str]] = {}
    for tdir in ("tests/web", "tests/api"):
        d = repo / tdir
        if not d.is_dir():
            continue
        for f in sorted(d.glob("test_*.py")):
            out["total_files"] += 1
            text = f.read_text(encoding="utf-8", errors="replace")
            if not TC_RE.search(text):
                out["no_tc_marker"].append(str(f.relative_to(repo)))
            for m in TESTDEF.finditer(text):
                seen.setdefault(m.group(1), []).append(str(f.relative_to(repo)))
    out["duplicates"] = {k: v for k, v in seen.items() if len(v) > 1}
    return out


def coverage_approved_vs_tests(repo: Path) -> dict:
    """TC-ID утвержденных кейсов, которых нет ни в одном автотесте."""
    appr = repo / "test-model" / "approved"
    tests_text = ""
    for d in ("tests/web", "tests/api"):
        dd = repo / d
        if dd.is_dir():
            tests_text += "\n".join(
                f.read_text(encoding="utf-8", errors="replace")
                for f in dd.glob("test_*.py"))
    gaps: dict[str, list[str]] = {}
    if appr.is_dir():
        for change_dir in sorted(p for p in appr.iterdir() if p.is_dir()):
            case_ids = set()
            for f in change_dir.glob("TC-*.md"):
                case_ids.update(TC_RE.findall(f.stem) or [f.stem])
            missing = sorted(cid for cid in case_ids if cid not in tests_text)
            if missing:
                gaps[change_dir.name] = missing
    return gaps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"[FAIL] не git-репозиторий: {repo}", file=sys.stderr)
        return 2

    tm = scan_test_model(repo)
    tests = scan_tests(repo)
    gaps = coverage_approved_vs_tests(repo)
    data = {"repo": str(repo), "test_model": tm, "tests": tests, "coverage_gaps": gaps}
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(f"# Инвентаризация QA-артефактов: {repo.name}\n")
    print("## test-model/ — осиротевшие наборы (нет живого change)\n")
    orphans = [(sub, i) for sub, v in tm.items() if isinstance(v, dict) and not v.get("empty")
               for i in v.get("orphans", [])]
    if not orphans:
        print("- нет\n")
    for sub, i in orphans:
        print(f"- {i['entry']} — change не определен (кандидат: сверить руками)")
    print("\n## test-model/ — наборы только архивного change\n")
    arch = [(sub, i) for sub, v in tm.items() if isinstance(v, dict) and not v.get("empty")
            for i in v.get("archived_only", [])]
    if not arch:
        print("- нет\n")
    for sub, i in arch:
        print(f"- {i['entry']} — change в archive (норма для regress/reports; "
              f"подозрительно для new/)")
    print("\n## Пустые подкаталоги test-model/\n")
    empties = [s for s, v in tm.items() if v.get("empty")]
    print(("\n".join(f"- test-model/{s}/" for s in empties)) if empties else "- нет")
    print(f"\n## Автотесты (tests/web, tests/api): файлов {tests['total_files']}\n")
    print("### Без TC-маркера в тексте\n")
    print(("\n".join(f"- {f}" for f in tests["no_tc_marker"])) if tests["no_tc_marker"] else "- нет")
    print("\n### Дубли имен тестов\n")
    if tests["duplicates"]:
        for name, files in tests["duplicates"].items():
            print(f"- {name}: {', '.join(files)}")
    else:
        print("- нет")
    print("\n## Разрыв покрытия: TC-ID из approved, которых нет в тестах\n")
    if gaps:
        for ch, miss in gaps.items():
            print(f"- {ch}: {', '.join(miss)}")
    else:
        print("- нет")
    print("\nДальше: сабагент-чистильщик сверяет кандидатов (git log, ссылки на "
          "спеку), архивирует/удаляет; approved-кейсы не трогает без ПМ.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
