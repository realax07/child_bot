#!/usr/bin/env python3
"""factory_lock.py — версия фабрики, чьи ворота гоняет CI проекта (J65.7).

Проект хранит в корне `factory.lock`:

    # factory.lock — ворота фабрики для CI проекта (J65.7)
    repo=realax07/ai-factory
    sha=<40 hex коммита фабрики>

`.github/workflows/flow.yml` проекта делает checkout фабрики на этот SHA в
`.factory/` и запускает ее flow_check и pr_validate против проекта.
Обновление ворот в проекте = PR с одной строкой `sha=` (этой командой).
Без factory.lock CI берет копии `scripts/` проекта (как до J65.7).

Usage:
  python3 scripts/factory_lock.py show   --target <проект>
  python3 scripts/factory_lock.py update --target <проект> [--sha SHA] [--repo owner/name]

update без --sha берет HEAD фабрики — только если он влит в origin/main
(закрепляется проверенный код main фабрики); битый lock update перезаписывает. Exit: 0 — ок; 1 — отказ (show: битый lock; update: HEAD не в origin/main); 2 —
ошибка использования.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

FACTORY = Path(__file__).resolve().parent.parent
LOCK_NAME = "factory.lock"
DEFAULT_REPO = "realax07/ai-factory"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HEADER = "# factory.lock — ворота фабрики для CI проекта (J65.7)\n"
LINE_RE = re.compile(r"^(repo|sha)=(\S+)$")
MAIN_REF = "origin/main"


class LockError(Exception):
    pass


def read_lock(target: Path) -> dict | None:
    """{'repo', 'sha'} или None, если файла нет; битый файл — LockError."""
    path = target / LOCK_NAME
    if not path.is_file():
        return None
    # Грамматика одна с шагом CI (flow.yml): строки ровно `repo=<owner/name>`
    # и `sha=<40 hex>` без пробелов, каждая ровно один раз; комментарии `#` и
    # пустые строки допустимы; прочее — ошибка.
    raw = path.read_bytes().decode("utf-8")   # без перевода \r\n → \n
    if "\r" in raw:      # шаг CI (grep/sed) CRLF не принимает — та же грамматика
        raise LockError(f"{LOCK_NAME}: переводы строк CRLF — нужен LF")
    data: dict[str, str] = {}
    for line in raw.split("\n"):
        if not line.strip() or line.startswith("#"):
            continue
        m = LINE_RE.match(line)
        if not m:
            raise LockError(f"{LOCK_NAME}: строка вне грамматики "
                            f"(repo=<owner/name> | sha=<40 hex>): {line!r}")
        if m.group(1) in data:
            raise LockError(f"{LOCK_NAME}: {m.group(1)}= повторяется")
        data[m.group(1)] = m.group(2)
    if not SHA_RE.match(data.get("sha", "")):
        raise LockError(f"{LOCK_NAME}: sha должен быть 40 hex, есть {data.get('sha')!r}")
    if not REPO_RE.match(data.get("repo", "")):
        raise LockError(f"{LOCK_NAME}: repo должен быть owner/name, есть {data.get('repo')!r}")
    return {"repo": data["repo"], "sha": data["sha"]}


def write_lock(target: Path, sha: str, repo: str = DEFAULT_REPO) -> Path:
    if not SHA_RE.match(sha):
        raise LockError(f"sha должен быть 40 hex: {sha!r}")
    if not REPO_RE.match(repo):
        raise LockError(f"repo должен быть owner/name: {repo!r}")
    path = target / LOCK_NAME
    path.write_text(f"{HEADER}repo={repo}\nsha={sha}\n", encoding="utf-8")
    return path


def _git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(FACTORY), *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise LockError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def published_head() -> str:
    """HEAD фабрики, если он влит в origin/main (по локальной ссылке — сеть
    не трогается; свежесть ссылки — `git fetch` перед update): закрепляется
    только код, прошедший ревью и CI фабрики, — не ветка pipeline/** или
    chore/** до мержа. Иначе LockError."""
    sha = _git("rev-parse", "HEAD")
    if subprocess.run(["git", "-C", str(FACTORY), "rev-parse", "--verify", "-q",
                       MAIN_REF], capture_output=True).returncode != 0:
        raise LockError(f"в клоне фабрики нет ссылки {MAIN_REF} — `git fetch "
                        "origin main` или передай --sha")
    r = subprocess.run(["git", "-C", str(FACTORY), "merge-base", "--is-ancestor",
                        sha, MAIN_REF], capture_output=True, text=True)
    if r.returncode != 0:
        raise LockError(f"HEAD фабрики {sha[:12]} не влит в {MAIN_REF} — "
                        "закрепляется только код main фабрики; `git fetch` и "
                        "переключись на main, или передай --sha")
    return sha


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("show", "update"):
        p = sub.add_parser(name)
        p.add_argument("--target", required=True)
        if name == "update":
            p.add_argument("--sha")
            p.add_argument("--repo", default=None)
    args = ap.parse_args(argv)
    target = Path(args.target).resolve()
    try:
        if args.cmd == "show":
            lock = read_lock(target)
            print(f"{LOCK_NAME}: нет — CI берет scripts/ проекта" if lock is None
                  else f"{LOCK_NAME}: {lock['repo']}@{lock['sha']}")
            return 0
        try:
            current = read_lock(target)
        except LockError as exc:     # update чинит битый lock
            print(f"factory_lock: битый {LOCK_NAME} перезаписывается ({exc})",
                  file=sys.stderr)
            current = None
        repo = args.repo or (current or {}).get("repo") or DEFAULT_REPO
        sha = args.sha or published_head()
        write_lock(target, sha, repo)
        print(f"{LOCK_NAME}: {repo}@{sha}"
              + (f" (было {current['sha'][:12]})" if current else ""))
        return 0
    except LockError as exc:
        print(f"factory_lock: ОТКАЗ — {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
