#!/usr/bin/env python3
"""stand_reaper.py — гашение осиротевших тестовых стендов (J29).

Цель: автоматически находить и (по флагу) гасить uvicorn/http.server/nginx,
поднятые сабагентами и осиротевшие (владелец мертв: worktree удален, сессия
завершена). Никогда не трогает прод (docker-контейнеры, чужие пользователи,
allowlist).

Логика (согласована с Заказчиком 2026-10-09):
  1. Кандидаты: процессы openclaw с cmdline uvicorn|http.server|nginx.
  2. Категория по /proc/<pid>/cwd:
     - cwd НЕ существует            → REAP (удаленный worktree = сирота)
     - cwd в *-worktrees/<session>  → REAP, если сессия не в active_sessions.json
                                      и каталога нет в <repo>-worktrees/
     - cwd — обычный клон (/tmp/*, /home/*/ekotov-wiki и т.п.) → REPORT only
       (ручное решение: false positive опаснее false negative)
  3. Стоп-листы: не-openclaw владелец; возраст < min-age (гонка с живой
     сессией); allowlist-файл (по строке на подстроку cmdline).
  4. Режимы: по умолчанию dry-run (таблица + exit 1 если были кандидаты);
     --kill = SIGTERM, 3s grace, SIGKILL выжившим; --json — машиночитаемо.

Запуск: python3 stand_reaper.py [--kill] [--json] [--min-age SEC] [--allowlist FILE]
Cron (опционально): dry-run каждые 30 мин с алертом; авто-kill — только руками.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

CMDLINE_PATTERNS = ("uvicorn", "http.server", "nginx")
ALLOWED_USERS = {"openclaw"}
DEFAULT_MIN_AGE_SEC = 600  # 10 минут: защита от гонки с живой сессией
GRACE_SEC = 3.0
WORKTREE_MARKERS = ("-worktrees/", "-worktree/")
HOME = os.path.expanduser("~")


@dataclass
class Proc:
    pid: int
    user: str
    cmdline: str
    cwd: str | None
    cwd_exists: bool
    age_sec: float | None
    category: str  # reap | report | skip
    reason: str
    killed: bool = False


def _read_cmdline(pid: int) -> str | None:
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read()
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None


def _proc_user(pid: int) -> str | None:
    try:
        st = os.stat(f"/proc/{pid}")
        import pwd

        return pwd.getpwuid(st.st_uid).pw_name
    except (FileNotFoundError, PermissionError, KeyError, ProcessLookupError):
        return None


def _proc_start_time(pid: int) -> float | None:
    try:
        st = os.stat(f"/proc/{pid}")
        return st.st_mtime
    except (FileNotFoundError, ProcessLookupError):
        return None


def _cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None


def _active_session_ids() -> set[str]:
    """session-id из active_sessions.json (если читается)."""
    path = os.path.expanduser("~/.hermes/state/active_sessions.json")
    ids: set[str] = set()
    try:
        data = json.load(open(path, encoding="utf-8"))

        def walk(o):
            if isinstance(o, dict):
                sid = o.get("session_id") or o.get("delegation_id")
                if sid:
                    ids.add(str(sid))
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(data)
    except Exception:
        pass
    return ids


def _existing_worktree_dirs() -> set[str]:
    """Каталоги *-worktrees/*, реально существующие (живые сессии)."""
    out: set[str] = set()
    for root in (HOME, "/tmp"):
        if not os.path.isdir(root):
            continue
        try:
            for name in os.listdir(root):
                full = os.path.join(root, name)
                if "-worktree" in name and os.path.isdir(full):
                    out.add(full)
                    for sub in os.listdir(full):
                        out.add(os.path.join(full, sub))
        except PermissionError:
            continue
    return out


def classify(p: Proc, active_ids: set[str], wt_dirs: set[str], min_age: float) -> None:
    if p.user not in ALLOWED_USERS:
        p.category, p.reason = "skip", f"чужой пользователь: {p.user}"
        return
    if p.age_sec is not None and p.age_sec < min_age:
        p.category, p.reason = "skip", f"моложе {int(min_age)}с — возможна живая сессия"
        return
    if p.cwd is None:
        p.category, p.reason = "reap", "cwd не читается (процесс вероятно сирота)"
        return
    if not p.cwd_exists:
        p.category, p.reason = "reap", f"cwd не существует: {p.cwd} (worktree/каталог удален)"
        return
    if any(m in p.cwd for m in WORKTREE_MARKERS):
        if p.cwd in wt_dirs:
            p.category, p.reason = "skip", "worktree существует и сессия жива"
        else:
            p.category, p.reason = "reap", f"worktree-каталог не существует/сессия не активна: {p.cwd}"
        return
    # Обычный клон или /tmp — ручное решение
    p.category, p.reason = "report", "cwd — обычный клон/tmp (не сирота по worktree)"


def collect(min_age: float) -> list[Proc]:
    procs: list[Proc] = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        cmdline = _read_cmdline(int(pid))
        if not cmdline or not any(pat in cmdline for pat in CMDLINE_PATTERNS):
            continue
        if "stand_reaper" in cmdline or "grep" in cmdline:
            continue
        user = _proc_user(int(pid))
        cwd = _cwd(int(pid))
        started = _proc_start_time(int(pid))
        age = time.time() - started if started else None
        p = Proc(
            pid=int(pid),
            user=user or "?",
            cmdline=cmdline[:160],
            cwd=cwd,
            cwd_exists=bool(cwd and os.path.isdir(cwd)),
            age_sec=age,
            category="",
            reason="",
        )
        classify(p, _active_session_ids(), _existing_worktree_dirs(), min_age)
        procs.append(p)
    return procs


def kill_targets(targets: list[Proc]) -> None:
    for p in targets:
        try:
            os.kill(p.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    time.sleep(GRACE_SEC)
    for p in targets:
        try:
            os.kill(p.pid, 0)  # жив?
            os.kill(p.pid, signal.SIGKILL)
            p.killed = True
        except (ProcessLookupError, PermissionError):
            p.killed = True  # исчез сам или не наш


def main() -> int:
    ap = argparse.ArgumentParser(description="Гашение осиротевших тестовых стендов (J29)")
    ap.add_argument("--kill", action="store_true", help="реально гасить (по умолчанию dry-run)")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    ap.add_argument("--min-age", type=float, default=DEFAULT_MIN_AGE_SEC)
    ap.add_argument("--allowlist", help="файл: подстрока cmdline, которую не трогать")
    args = ap.parse_args()

    allow: list[str] = []
    if args.allowlist and os.path.isfile(args.allowlist):
        allow = [ln.strip() for ln in open(args.allowlist, encoding="utf-8") if ln.strip()]

    procs = collect(args.min_age)
    if allow:
        for p in procs:
            if any(a in p.cmdline for a in allow):
                p.category, p.reason = "skip", "allowlist"

    reaps = [p for p in procs if p.category == "reap"]
    reports = [p for p in procs if p.category == "report"]
    skips = [p for p in procs if p.category == "skip"]

    if args.kill and reaps:
        kill_targets(reaps)

    if args.json:
        print(json.dumps({
            "reaped": [{"pid": p.pid, "cmdline": p.cmdline, "cwd": p.cwd, "reason": p.reason,
                        "killed": p.killed} for p in (reaps if args.kill else [])],
            "candidates": [{"pid": p.pid, "cmdline": p.cmdline, "cwd": p.cwd,
                            "reason": p.reason} for p in reaps],
            "reports": [{"pid": p.pid, "cmdline": p.cmdline, "cwd": p.cwd} for p in reports],
            "skipped": len(skips),
        }, ensure_ascii=False, indent=2))
    else:
        print(f"stand_reaper: режим {'KILL' if args.kill and reaps else 'DRY-RUN'}")
        for p in reaps:
            act = "УБИТ" if p.killed else "БУДЕТ УБИТ (--kill)"
            print(f"  [REAP]   pid={p.pid} {p.cmdline[:70]}\n           cwd={p.cwd} — {p.reason} → {act}")
        for p in reports:
            print(f"  [REPORT] pid={p.pid} {p.cmdline[:70]}\n           cwd={p.cwd} — {p.reason}")
        if not reaps and not reports:
            print("  чисто: осиротевших стендов нет")

    return 1 if reaps and not args.kill else 0


if __name__ == "__main__":
    sys.exit(main())
