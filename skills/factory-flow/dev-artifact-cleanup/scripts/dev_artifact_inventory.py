#!/usr/bin/env python3
"""Инвентаризация артефактов стадии dev — кандидаты на чистку.

Сканирует конвейерный репозиторий: протухшие worktrees (git worktree list +
давность каталога), ветки, влитые в HEAD (кандидаты на удаление), TODO/FIXME/
XXX-маркеры, скрипты без ссылок в репозитории, python-модули без импортов
(легкий граф по grep). Ничего не удаляет — только инвентаризация для
сабагента-чистильщика.

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

TODO_RE = re.compile(r"\b(TODO|FIXME|XXX)\b(.{0,120})")
IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([.\w]+)", re.MULTILINE)


def git(repo: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def path_age(p: Path) -> float | None:
    try:
        return (time.time() - p.stat().st_mtime) / 86400
    except OSError:
        return None


def human(days):
    return "n/a" if days is None else f"{days:.0f} дн."


def scan_worktrees(repo: Path) -> list[dict]:
    out = git(repo, "worktree", "list", "--porcelain")
    result = []
    if not out:
        return result
    path = None
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1]
        elif line == "" and path:
            p = Path(path)
            outside = not str(p).startswith(str(repo)) or p != repo
            if outside and p.exists():
                result.append({
                    "path": str(p),
                    "age": path_age(p),
                    "branch": git(p, "branch", "--show-current") if (p / ".git").exists() else "?",
                })
            path = None
    return sorted(result, key=lambda w: -(w["age"] or 0))


def scan_merged_branches(repo: Path) -> list[str]:
    out = git(repo, "branch", "--merged", "HEAD")
    merged = []
    for line in out.splitlines():
        b = line.strip().lstrip("* ").strip()
        if b and b not in ("main", "master") and not line.strip().startswith("*"):
            merged.append(b)
    return merged


def scan_todos(repo: Path) -> dict:
    counts: dict[str, int] = {}
    examples: dict[str, str] = {}
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", ".hermes"}
    for p in repo.rglob("*"):
        if not p.is_file() or p.suffix not in (".py", ".js", ".ts", ".sh", ".yaml", ".yml", ".md"):
            continue
        parts = set(p.relative_to(repo).parts)
        if parts & skip_dirs:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = TODO_RE.findall(text)
        if hits:
            rel = str(p.relative_to(repo))
            counts[rel] = len(hits)
            examples.setdefault(rel, f"{hits[0][0]}: {hits[0][1].strip()}")
    top = dict(sorted(counts.items(), key=lambda kv: -kv[1])[:20])
    return {"total": sum(counts.values()), "files": len(counts),
            "top": top, "examples": {k: examples[k] for k in top}}


def module_references(repo: Path, stem: str, self_path: Path) -> int:
    """Сколько файлов упоминают имя модуля (import или строкой)."""
    n = 0
    for p in repo.rglob(stem + "*"):
        pass  # заглушка-скорость: основной проход ниже
    for p in repo.rglob("*"):
        if p == self_path or not p.is_file():
            continue
        if p.suffix not in (".py", ".sh", ".md", ".yaml", ".yml", ".json", ".js"):
            continue
        if ".git" in p.parts or "__pycache__" in p.parts:
            continue
        try:
            if stem in p.read_text(encoding="utf-8", errors="replace"):
                n += 1
        except OSError:
            continue
    return n


def scan_unreferenced_scripts(repo: Path, max_scan: int = 40) -> list[dict]:
    """Скрипты в scripts/, на которые нет ссылок в репо (кандидаты на вывод)."""
    d = repo / "scripts"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.py"))[:max_scan]:
        refs = module_references(repo, p.stem, p)
        if refs == 0:
            out.append({"script": f"scripts/{p.name}",
                        "age": path_age(p)})
    return out


def scan_unimported_modules(repo: Path, pkg: str = "backend/app", max_scan: int = 60) -> list[dict]:
    """Модули пакета, которые никто не импортирует (легкий граф, эвристика)."""
    d = repo / pkg
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.py"))[:max_scan]:
        if p.stem in ("__init__", "main", "app"):
            continue
        refs = module_references(repo, p.stem, p)
        if refs == 0:
            out.append({"module": f"{pkg}/{p.name}", "age": path_age(p)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"[FAIL] не git-репозиторий: {repo}", file=sys.stderr)
        return 2

    worktrees = scan_worktrees(repo)
    merged = scan_merged_branches(repo)
    todos = scan_todos(repo)
    scripts = scan_unreferenced_scripts(repo)
    modules = scan_unimported_modules(repo)
    data = {"repo": str(repo), "worktrees": worktrees, "merged_branches": merged,
            "todos": todos, "unreferenced_scripts": scripts,
            "unimported_modules": modules}
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(f"# Инвентаризация dev-артефактов: {repo.name}\n")
    print("## Worktrees вне репо (кандидаты на git worktree remove)\n")
    if worktrees:
        for w in worktrees:
            print(f"- {w['path']} — ветка: {w['branch'] or 'detached'}, "
                  f"не тронут {human(w['age'])}")
    else:
        print("- нет")
    print("\n## Ветки, влитые в HEAD (кандидаты на branch -d)\n")
    print(("\n".join(f"- {b}" for b in merged)) if merged else "- нет")
    print(f"\n## TODO/FIXME/XXX: всего {todos['total']} в {todos['files']} файлах "
          "(топ-20)\n")
    if todos["top"]:
        for f, n in todos["top"].items():
            print(f"- {f}: {n} — напр. {todos['examples'][f][:100]}")
    else:
        print("- нет")
    print("\n## Скрипты scripts/ без ссылок в репо (вывод из эксплуатации)\n")
    print(("\n".join(f"- {s['script']} (не тронут {human(s['age'])})" for s in scripts))
          if scripts else "- нет")
    print("\n## Модули backend/app без импортов (эвристика — проверять руками)\n")
    print(("\n".join(f"- {m['module']} (не тронут {human(m['age'])})" for m in modules))
          if modules else "- нет")
    print("\nДальше: сабагент-чистильщик перепроверяет каждого кандидата "
          "(git log, grep, прогон тестов после удаления) и выносит вердикт.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
