#!/usr/bin/env python3
"""flow_scope_check.py — чек «содержимое ветки соответствует заявленному
change-id» (аудит Н21).

Инцидент-класс: зеркальный IR-02 коммит ушел в чужую ветку (chore/
p23-fastline-dev с p12-линейкой) незамеченным; cherry-pick маркеров — тот же
класс. Сверяем commit subjects ветки (merge-base..HEAD, без merge-коммитов)
с id из маркера PR:

  - конкретный id ([change-id] / [BUG-NNN]): коммит «свой», если subject
    содержит заявленный id (скобочный маркер, conventional `type(id):` или
    вхождение в тексте); ЧУЖОЙ конкретный id в subject — WARN (главный
    сигнал); subject вообще без id (только generic-маркер или ничего) — WARN;
  - generic id ([chore]/[docs]/[ops]): generic-маркер в subject — «свой»;
    чужой конкретный id — WARN; пусто — WARN.

Модуль автономен (stdlib only), exit: 0 — чисто; 1 — есть WARN и --strict;
2 — ошибка. Вывод JSON в stdout.

Usage:
    python3 scripts/flow_scope_check.py --repo PATH --change-id add-widget \
        [--base main] [--strict] [--json]
    python3 scripts/flow_scope_check.py --repo PATH --pr-text pr.txt [--strict]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

CHANGE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
GENERIC = ("chore", "docs", "ops")

# Скобочные маркеры в subject — та же грамматика, что pr_validate.MARKER_RE.
MARKER_RE = re.compile(
    r"\[(BUG-\d+|[a-z0-9]+(?:-[a-z0-9]+)+|\bchore|\bdocs|\bops)\]")
# Conventional-форма: `chore(j58-ir02-precision): ...` — без скобок.
CONVENTIONAL_RE = re.compile(r"^\w+\(([A-Za-z0-9][A-Za-z0-9._-]*)\):")


def parse_marker_id(text: str) -> tuple[str, str] | None:
    """(тип, id) из маркера текста PR — грамматика pr_validate (копия без
    импорта: модуль автономен)."""
    m = re.search(r"\[(BUG-\d+)\]", text)
    if m:
        return "bug", m.group(1)
    for generic in GENERIC:
        if re.search(rf"\[{generic}\]", text, re.I):
            return generic, generic
    m = re.search(r"\[([a-z0-9]+(?:-[a-z0-9]+)+)\]", text)
    if m:
        return "change", m.group(1)
    return None


def git_out(repo: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def merge_base(repo: Path) -> str | None:
    for base in ("main", "origin/main"):
        mb = git_out(repo, "merge-base", base, "HEAD")
        if mb:
            return mb.strip()
    return None


def branch_commits(repo: Path, mb: str) -> list[tuple[str, str]]:
    """(sha, subject) не-merge коммитов ветки, старые→новые."""
    out = git_out(repo, "log", "--no-merges", "--reverse",
                  "--format=%H%x1f%s", f"{mb}..HEAD")
    if not out:
        return []
    commits = []
    for line in out.splitlines():
        if "\x1f" in line:
            sha, subject = line.split("\x1f", 1)
            commits.append((sha.strip(), subject.strip()))
    return commits


def subject_ids(subject: str) -> tuple[set[str], set[str]]:
    """(конкретные id, generic-маркеры) из subject."""
    concrete: set[str] = set()
    generic: set[str] = set()
    for m in MARKER_RE.finditer(subject):
        tok = m.group(1)
        if tok in GENERIC:
            generic.add(tok)
        else:
            concrete.add(tok)
    m = CONVENTIONAL_RE.match(subject)
    if m and m.group(1) not in GENERIC:
        concrete.add(m.group(1))
    return concrete, generic


def check_scope(commits: list[tuple[str, str]],
                declared: str) -> list[dict]:
    """WARN-список: коммиты, не соответствующие заявленному id."""
    declared_low = declared.lower()
    generic_declared = declared_low in GENERIC
    warns: list[dict] = []
    for sha, subject in commits:
        concrete, generic = subject_ids(subject)
        foreign = {c for c in concrete if c.lower() != declared_low}
        if not generic_declared:
            own = any(c.lower() == declared_low for c in concrete)
        else:
            own = bool(generic & set(GENERIC)) and not foreign
        if own and not foreign:
            continue
        if foreign:
            reason = ("чужой id в subject: " + ", ".join(sorted(foreign))
                      + f" (заявлен {declared})")
        elif not concrete and not generic:
            reason = f"в subject нет маркера id (заявлен {declared})"
        else:
            reason = (f"в subject нет заявленного id {declared}"
                      + (f" (есть generic: {', '.join(sorted(generic))})"
                         if generic else ""))
        warns.append({"commit": sha, "subject": subject, "reason": reason})
    return warns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flow_scope_check",
        description="Коммиты ветки vs change-id маркера (Н21)")
    parser.add_argument("--repo", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--change-id", help="id из маркера PR")
    group.add_argument("--pr-text", help="файл title+body PR (маркер в тексте)")
    parser.add_argument("--base", default=None,
                        help="база ветки (default: merge-base с main)")
    parser.add_argument("--strict", action="store_true", help="WARN → exit 1")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    if args.pr_text:
        p = Path(args.pr_text)
        if not p.is_file():
            print(json.dumps({"check": "scope", "status": "ERROR",
                              "error": f"файл не читается: {args.pr_text}"},
                             ensure_ascii=False))
            return 2
        parsed = parse_marker_id(p.read_text(encoding="utf-8", errors="replace"))
        if not parsed:
            print(json.dumps({"check": "scope", "status": "ERROR",
                              "error": "в тексте PR маркер не найден"},
                             ensure_ascii=False))
            return 2
        _, declared = parsed
    else:
        declared = args.change_id  # type: ignore[assignment]
        if not CHANGE_ID_RE.fullmatch(declared) or ".." in declared:
            print(json.dumps({"check": "scope", "status": "ERROR",
                              "error": f"недопустимый change-id: {declared!r}"},
                             ensure_ascii=False))
            return 2

    mb = args.base or merge_base(repo)
    if not mb:
        print(json.dumps({"check": "scope", "status": "SKIP",
                          "warns": [], "error":
                          "merge-base не определен"}, ensure_ascii=False))
        return 0
    commits = branch_commits(repo, mb)
    if not commits:
        print(json.dumps({"check": "scope", "status": "SKIP", "warns": [],
                          "error": "коммитов ветки нет (MB..HEAD пуст)"},
                         ensure_ascii=False))
        return 0
    warns = check_scope(commits, declared)
    status = "WARN" if warns else "OK"
    print(json.dumps({
        "check": "scope", "status": status, "change_id": declared,
        "base": mb, "commits": len(commits), "warns": warns,
    }, ensure_ascii=False, indent=2))
    return 1 if (warns and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
