#!/usr/bin/env python3
"""state_backup.py — бэкап и восстановление state движка фабрики (J64.6).

State движка (factory_paths.state_dir(): реестр сессий active_sessions.json,
flowctl_state.json, flow_mode.json, инциденты вотчдогов, gate-отчеты) лежит
вне git; при enforcing без него работа встает. Решение Заказчика 2026-11-10 —
«Бэкап + восстановление».

Usage:
  python3 scripts/state_backup.py backup  [--dest DIR] [--keep N] [--include-archive]
  python3 scripts/state_backup.py list    [--dest DIR]
  python3 scripts/state_backup.py restore ARCHIVE [--force]

backup  — архив state-<UTC>.tar.gz в --dest (по умолчанию
          factory_paths.backup_dir(), env AF_BACKUP_DIR); ротация — хранить
          N последних (--keep, по умолчанию 14). session-archive/
          (транскрипты, объемные) — только с --include-archive. Молчит при
          успехе (крон: чисто = пустой stdout), путь архива — с --verbose.
list    — архивы в --dest, новые первыми.
restore — распаковка архива в state_dir. Текущий state не удаляется:
          переименовывается в <state>.before-restore-<UTC>; любой сбой
          возвращает прежний state на место. Если в реестре есть сессии,
          держащие зоны (reserved/running/finished/needs_attention), — отказ
          без --force. Архив проверяется: каталог state/, только относительные
          пути внутри него, без ссылок и устройств.

Архив — права 0600, каталог бэкапов — 0700 (в state реестр и инциденты, с
--include-archive — транскрипты). Ротация трогает только автоматические
архивы state-<UTC>[-N].tar.gz. Файлы вне state_dir (FLOW_MODE_FILE,
FLOW_ENFORCER_STATE_DIR, вынесенные env) в бэкап не попадают.

Exit: 0 — ок; 1 — отказ (активные сессии, битый архив, нет state); 2 —
ошибка использования.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tarfile
import time
from pathlib import Path

import factory_paths as fp  # noqa: E402  (J64.5: пути машины — только оттуда)

PREFIX = "state-"
SUFFIX = ".tar.gz"
DEFAULT_KEEP = 14
ARCHIVE_SUBDIR = "session-archive"
# Статусы реестра, при которых сессия держит зону (session_check ZONE_HOLDING
# + finished/needs_attention): restore перепишет их резервации.
HOLDING_STATUSES = ("reserved", "running", "finished", "needs_attention")
# Имя автоматического архива: state-YYYYmmddTHHMMSSZ[-N].tar.gz — ротация
# трогает только такие (сохраненные вручную под другим именем не удаляются).
AUTO_RE = re.compile(r"^state-(\d{8}T\d{6}Z)(?:-(\d+))?\.tar\.gz$")


def _utc() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _unique(base: Path) -> Path:
    """Свободное имя рядом: base, base-1, base-2, …"""
    cand, n = base, 1
    while cand.exists():
        cand = base.with_name(f"{base.name}-{n}")
        n += 1
    return cand


def _auto_key(p: Path):
    m = AUTO_RE.match(p.name)
    return (m.group(1), int(m.group(2) or 0)) if m else None


def archives(dest: Path) -> list[Path]:
    """Автоматические архивы, новые первыми (время, затем номер в секунде)."""
    if not dest.is_dir():
        return []
    return sorted((p for p in dest.iterdir() if _auto_key(p)),
                  key=_auto_key, reverse=True)


def backup(state: Path, dest: Path, keep: int = DEFAULT_KEEP,
           include_archive: bool = False) -> Path:
    real = state.resolve()           # state может быть симлинком на каталог
    if not real.is_dir():
        raise RuntimeError(f"state не найден: {state}")
    dest = dest.resolve()
    if dest == real or real in dest.parents:
        raise RuntimeError(f"--dest внутри state ({dest}) — бэкап уйдет вместе с ним")
    try:
        dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(dest, 0o700)        # state — реестр, инциденты, транскрипты
    except OSError as exc:
        raise RuntimeError(f"каталог бэкапов не создан: {exc}") from exc
    stamp = _utc()
    out, n = dest / f"{PREFIX}{stamp}{SUFFIX}", 1
    while out.exists():              # два бэкапа в одну секунду
        out = dest / f"{PREFIX}{stamp}-{n}{SUFFIX}"
        n += 1
    tmp = out.with_name(out.name + ".part")

    def _filter(info: tarfile.TarInfo):
        rel = Path(info.name)
        if not include_archive and rel.parts[1:2] == (ARCHIVE_SUBDIR,):
            return None
        if info.issym() or info.islnk() or info.isdev():
            return None
        return info

    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
            tar.add(real, arcname="state", filter=_filter)
        with tarfile.open(tmp, "r:gz") as tar:
            if not any(m.name == "state" and m.isdir() for m in tar.getmembers()):
                raise RuntimeError("в архиве нет каталога state/ — бэкап пуст")
        tmp.rename(out)
    except (OSError, tarfile.TarError, RuntimeError) as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"бэкап не записан: {exc}") from exc
    for old in archives(dest)[max(keep, 1):]:
        old.unlink()
    return out


def active_sessions(state: Path) -> list[str]:
    reg = state / fp.registry_path().name
    try:
        data = json.loads(reg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    sessions = data.get("sessions", []) if isinstance(data, dict) else data
    return [s.get("delegation_id", "?") for s in (sessions or [])
            if isinstance(s, dict) and s.get("status") in HOLDING_STATUSES]


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = tar.getmembers()
    for m in members:
        p = Path(m.name)
        if p.is_absolute() or ".." in p.parts or p.parts[:1] != ("state",):
            raise RuntimeError(f"недопустимый путь в архиве: {m.name}")
        if not (m.isfile() or m.isdir()):
            raise RuntimeError(f"недопустимый тип записи в архиве: {m.name}")
    if not any(m.name == "state" and m.isdir() for m in members):
        raise RuntimeError("в архиве нет каталога state/")
    return members


def restore(archive: Path, state: Path, force: bool = False) -> Path | None:
    """Распаковывает архив в state; возвращает путь отложенного прежнего state.
    Любой сбой оставляет прежний state на месте."""
    if not archive.is_file():
        raise RuntimeError(f"архив не найден: {archive}")
    if state.is_symlink():
        # state вынесен ссылкой (другой диск) — восстанавливаем по месту
        # назначения, ссылка остается
        state = state.resolve()
    if state.is_dir() and not force:
        active = active_sessions(state)
        if active:
            raise RuntimeError(
                "в текущем state сессии, держащие зоны: " + ", ".join(active)
                + " — восстановление перепишет их резервации (сначала разбор/"
                  "reconcile; --force, если это осознанно)")
    staging = _unique(state.with_name(state.name + f".restore-{_utc()}"))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = _safe_members(tar)
            staging.mkdir(parents=True)
            # filter="data" (3.12+): та же защита на уровне tarfile
            extra = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(staging, members=members, **extra)
        if not (staging / "state").is_dir():
            raise RuntimeError("после распаковки нет каталога state/")
    except (tarfile.TarError, OSError, EOFError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"архив не читается: {exc}") from exc
    except RuntimeError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    previous = None
    if state.exists():
        previous = _unique(state.with_name(state.name + f".before-restore-{_utc()}"))
        try:
            state.rename(previous)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise RuntimeError(f"прежний state не отложен ({exc}) — восстановление "
                               "не выполнено, state на месте") from exc
    try:
        (staging / "state").rename(state)
    except OSError as exc:
        if previous is not None:
            previous.rename(state)   # откат: прежний state на место
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"state не установлен ({exc}) — прежний возвращен") from exc
    staging.rmdir()
    return previous


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--dest", default=None)
    b.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    b.add_argument("--include-archive", action="store_true")
    b.add_argument("--verbose", action="store_true")
    li = sub.add_parser("list")
    li.add_argument("--dest", default=None)
    r = sub.add_parser("restore")
    r.add_argument("archive")
    r.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    state = fp.state_dir()
    dest = Path(args.dest).expanduser() if getattr(args, "dest", None) else fp.backup_dir()
    try:
        if args.cmd == "backup":
            out = backup(state, dest, args.keep, args.include_archive)
            if args.verbose:
                print(f"state_backup: {out}")
        elif args.cmd == "list":
            for p in archives(dest):
                print(f"{p}  {p.stat().st_size} байт")
        else:
            prev = restore(Path(args.archive).expanduser(), state, args.force)
            print(f"state_backup: state восстановлен из {args.archive} в {state}"
                  + (f"; прежний state — {prev}" if prev else ""))
    except RuntimeError as exc:
        print(f"state_backup: ОТКАЗ — {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
