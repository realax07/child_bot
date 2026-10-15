#!/usr/bin/env python3
"""session_check.py — admission сессий, зоны записи и post-check границы (поставка 04).

Дополняет существующие ворота, не заменяет их: `pm_bounds_check.py --sessions`
продолжает проверять обязательные поля реестра (J2), `session_worktree.sh` —
создавать worktree. Здесь: (1) атомарная резервация зоны записи (лок-файл +
tmp+rename, идемпотентность по delegation_id), (2) post-check результата
сессии (git status + changed paths против разрешенной зоны), (3) reconcile
после падения процесса (PID/worktree/git status → stale/needs_attention,
ничего не удаляется молча).

Жизненный цикл (ТЗ 04): reserved → running → finished → accepted/returned/
failed → closed. Смена статуса несет evidence и причину. Реестр
`active_sessions.json`: срез 2 вводит поля zones/worktree/base_sha/pid/lifecycle;
миграция — с резервной копией, старый формат (только обязательные поля J2)
читается как есть; path к реестру всегда параметр (в тестах реальный
~/.hermes/state/ не используется).

Зоны: канонические пути (posix, repo-relative) + glob-семантика; glob '**'
соответствует вложенным путям, '*' — внутри одного сегмента. Symlink,
уводящий измененный путь за пределы repo/разрешенной зоны — отказ. Пересечение
зон активных сессий того же repo → ZONE_CONFLICT: ровно одна сессия владеет
путем. P0.3: резервация требует policy_version — редакцию политики зон роли
(ROLE_ZONE_POLICY; сужение выполняет flowctl prepare до резервации); версия
хранится в записи и попадает в verdict finish (ZONE_POLICY_MISMATCH при
расхождении редакций).

Usage:
    python3 scripts/session_check.py reserve --registry PATH --repo PATH \
        --delegation-id ID --role ROLE --project ID --owner-pm PM \
        --path 'src/**' [--path ...] [--worktree PATH] [--branch NAME]
        [--base-sha SHA] [--snapshot-digest SHA] [--policy-version V] [--pid N]
        [--json]
    python3 scripts/session_check.py check --registry PATH --repo PATH \
        --delegation-id ID [--json]
    python3 scripts/session_check.py reconcile --registry PATH [--repo PATH] \
        [--delegation-id ID] [--json]
    python3 scripts/session_check.py status --registry PATH [--delegation-id ID] [--json]

Exit codes: 0 — успех (reserve: зарезервировано/идемпотентно; check: зона
соблюдена; reconcile: сверка выполнена; status: запись найдена); 1 — отказ
(конфликт зоны/идемпотентности, выход за зону, needs_attention); 2 — ошибка
входа/нечитаемый реестр.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import flow_mode  # noqa: E402  (режим shadow/enforcing в ответах)

REGISTRY_SCHEMA = "active-sessions/2"
LIFECYCLE_ACTIVE = ("reserved", "running", "finished")
# Статусы, в которых зона записи считается занятой (проверка пересечений).
ZONE_HOLDING = ("reserved", "running", "finished", "needs_attention")

REASON_OK = "OK"
ZONE_CONFLICT = "ZONE_CONFLICT"
DUPLICATE_PAYLOAD = "DUPLICATE_PAYLOAD"
MISSING_INPUT = "MISSING_INPUT"
REGISTRY_ERROR = "REGISTRY_ERROR"
OUT_OF_ZONE = "OUT_OF_ZONE"
WRONG_WORKTREE = "WRONG_WORKTREE"
WRONG_BRANCH = "WRONG_BRANCH"
SESSION_MD_MISSING = "SESSION_MD_MISSING"
STALE = "STALE"
NEEDS_ATTENTION = "NEEDS_ATTENTION"


# ------------------------------------------------------------- канонические пути


def canonical(rel: str) -> str:
    """Канонический repo-relative путь: posix-разделители, без ведущего ./ и /."""
    p = rel.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def _fnmatch_segments(pattern: str, path: str) -> bool:
    """fnmatch посегментно: '*' внутри сегмента не переходит через '/'."""
    psegs, usegs = pattern.split("/"), path.split("/")
    if len(psegs) != len(usegs):
        return False
    return all(fnmatch.fnmatchcase(u, p) for p, u in zip(psegs, usegs))


def glob_matches(pattern: str, rel: str) -> bool:
    """Glob-семантика зон (тесты ТЗ 04: вложенные пути, префиксы, symlink).

    - 'src/file.py' — точный путь;
    - 'src/*' — сегмент внутри src/ (fnmatch посегментно), НЕ углубление;
    - 'src/**' — все вложенные пути src/ (включая сам префикс);
    - 'a/**/b' — '**' как любое число сегментов между частями;
    - '**' — все пути.
    Префикс без метасимволов матчит и сам путь, и вложенные (зона-каталог).
    """
    pat = canonical(pattern)
    path = canonical(rel)
    if not pat or not path:
        return False
    if pat == "**":
        return True
    if any(ch in pat for ch in "*?["):
        if _fnmatch_segments(pat, path):
            return True
        if pat.endswith("/**") and (
            path == pat[:-3] or path.startswith(pat[:-3] + "/")
        ):
            return True
        if "**/" in pat or "/**" in pat:
            head, _, tail = pat.partition("**")
            # '**' → любое число сегментов (включая ноль).
            head_prefix = head.rstrip("/")
            if head_prefix and not (
                path == head_prefix or path.startswith(head_prefix + "/")
            ):
                return False
            rest = path[len(head_prefix):].lstrip("/")
            tail_seg = tail.lstrip("/")
            if not tail_seg:
                return True
            return any(
                _fnmatch_segments(tail_seg, "/".join(rest.split("/")[n:]))
                for n in range(len(rest.split("/")) + 1)
            )
        return False
    return path == pat or path.startswith(pat + "/")

def zone_holds(zone_patterns: list, rel: str) -> bool:
    return any(glob_matches(p, rel) for p in (zone_patterns or []))


def _pattern_matches_pattern(pa: str, pb: str) -> bool:
    """Матчит ли glob-паттерн pa хотя бы один путь, матчемый паттерном pb
    (пересечение языков двух паттернов), посегментной рекурсией:
    '**' — любое число сегментов (включая ноль), '*' — любой один сегмент
    (fnmatch внутри сегмента), прочие сегменты — посегментный fnmatch."""
    pa_segs, pb_segs = pa.split("/"), pb.split("/")

    def seg_match(pat: str, seg: str) -> bool:
        if any(ch in pat for ch in "*?["):
            return fnmatch.fnmatchcase(seg, pat)
        return pat == seg

    def rec(i: int, j: int) -> bool:
        while True:
            if i == len(pa_segs) and j == len(pb_segs):
                return True
            if i == len(pa_segs) or j == len(pb_segs):
                return False
            a, b = pa_segs[i], pb_segs[j]
            if a == "**":
                # '**' съедает 0..k сегментов другой стороны (до конца).
                return any(rec(i + 1, jj) for jj in range(j, len(pb_segs) + 1))
            if b == "**":
                return any(rec(ii, j + 1) for ii in range(i, len(pa_segs) + 1))
            if not seg_match(a, b) and not seg_match(b, a):
                return False
            i, j = i + 1, j + 1

    return rec(0, 0)


def zones_overlap(a: list, b: list) -> tuple[str, str] | None:
    """Пересечение двух зон: существует путь, покрываемый паттерном из a И
    паттерном из b. Кроме прямых сравнений (паттерн-как-путь, конкретные
    префиксы) пары паттернов сверяются пересечением их языков через
    посегментную рекурсию — покрывает 'head/**/tail'-формы (например
    '**/test/**' vs 'src/**': путь src/a/test/x.py принадлежит обеим зонам)
    и не дает ложных срабатываний на 'src/**' vs 'srcx/**'. Возвращает пару
    сошедшихся паттернов."""
    for pa in a or []:
        for pb in b or []:
            if pa == pb:
                return pa, pb
            if _pattern_matches_pattern(pa, pb):
                return pa, pb
            na = _concrete_prefix(pa)
            nb = _concrete_prefix(pb)
            if na and nb and (na.startswith(nb + "/") or nb.startswith(na + "/")
                              or na == nb):
                return pa, pb
            if glob_matches(pa, nb or pb) or glob_matches(pb, na or pa):
                return pa, pb
    return None


def _concrete_prefix(pattern: str) -> str:
    """Конкретный (без метасимволов) префикс паттерна: 'src/**/x' → 'src'."""
    out = []
    for seg in canonical(pattern).split("/"):
        if any(ch in seg for ch in "*?["):
            break
        out.append(seg)
    return "/".join(out)


# ------------------------------------------------------------------ реестр


def registry_load(path: Path) -> tuple[dict, str | None]:
    """Читает реестр (оба формата). Ошибка чтения/JSON → (данные, причина)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"sessions": []}, None  # несуществующий → пустой, создастся
    except OSError as exc:
        return {}, f"реестр не читается: {exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"битый JSON: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        return {}, "нет массива sessions"
    return data, None


def _migrate(data: dict) -> dict:
    """Старый формат (v1: только поля J2) → v2 (schema_version, lifecycle).
    Записи не выбрасываются: старые поля сохраняются как есть."""
    if data.get("schema_version") == REGISTRY_SCHEMA:
        return data
    out = dict(data)
    out["schema_version"] = REGISTRY_SCHEMA
    for s in out.get("sessions", []):
        if isinstance(s, dict) and not s.get("lifecycle"):
            s["lifecycle"] = {
                "state": s.get("status", "reserved"),
                "history": [{
                    "from": None,
                    "to": s.get("status", "reserved"),
                    "at": s.get("reserved_at", ""),
                    "reason": "migrated-from-v1",
                    "evidence": "существующая запись реестра",
                }],
            }
    return out


def _load_or_migrate(registry_path: Path, data: dict) -> dict:
    """Общий путь «миграция v1→v2 с бэкапом» (MUST ТЗ 04: схему менять
    миграцией с резервной копией). Бэкап делается перед миграцией на ВСЕХ
    путях записи (reserve/check/reconcile), а не только в reserve."""
    if data.get("schema_version") == REGISTRY_SCHEMA:
        return data
    backup_registry(registry_path)
    return _migrate(data)


def registry_write(path: Path, data: dict) -> None:
    """Атомарная запись: tmp-файл рядом + os.replace (rename на той же ФС)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".active_sessions.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def backup_registry(path: Path) -> Path | None:
    """Резервная копия перед миграцией схемы (ТЗ 04, совместимость).

    Имя уникально: timestamp с микросекундами + pid — два срабатывания
    в одну секунду не перезаписывают друг друга.
    """
    if not path.is_file():
        return None
    stamp = (time.strftime("%Y%m%dT%H%M%S")
             + f"-{time.time_ns() % 1_000_000_000:09d}-{os.getpid()}")
    bck = path.with_suffix(path.suffix + f".bck-{stamp}")
    bck.write_bytes(path.read_bytes())
    return bck


def _acquire_lock(path: Path, timeout: float = 10.0) -> Path:
    """Лок-каталог реестра: mkdir-атомарность (O_EXCL-семантика каталога).

    Внутрь лока пишется pid владельца: если владелец умер (crash/SIGKILL),
    лок снимается автоматически и захват повторяется — мертвый лок после
    падения процесса не держит admission навсегда. Живой владелец →
    TimeoutError с его pid.
    """
    lock = path.parent / (path.name + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock.mkdir()
            (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")
            return lock
        except FileExistsError:
            stale = False
            pid_file = lock / "pid"
            if pid_file.is_file():
                try:
                    stale = not pid_alive(pid_file.read_text(
                        encoding="utf-8").strip())
                except OSError:
                    stale = False
            else:
                # Лок без pid-файла: либо создается прямо сейчас (не мешаем
                # короткую границу), либо оставлен старой версией — снимаем
                # только после полного таймаута.
                if time.monotonic() > deadline:
                    try:
                        import shutil as _shutil
                        _shutil.rmtree(lock, ignore_errors=True)
                    except OSError:
                        pass
                    continue
            if stale:
                # Мертвый владелец — снимаем мертвый лок и пробуем снова.
                try:
                    import shutil as _shutil
                    _shutil.rmtree(lock, ignore_errors=True)
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                owner = "?"
                if pid_file.is_file():
                    try:
                        owner = pid_file.read_text(encoding="utf-8").strip()
                    except OSError:
                        pass
                raise TimeoutError(
                    f"не дождались освобождения лока реестра {lock} "
                    f"(владелец pid {owner} жив)")
            time.sleep(0.05)


def _release_lock(lock: Path) -> None:
    """Снимает лок: pid-файл удаляется первым (каталог должен стать пустым)."""
    try:
        (lock / "pid").unlink()
    except OSError:
        pass
    try:
        lock.rmdir()
    except OSError:
        pass


# ------------------------------------------------------------- git-факты


def git(repo: Path, *args: str) -> tuple[int, str, str]:
    p = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        timeout=30,
    )
    return p.returncode, p.stdout, p.stderr


def git_toplevel(repo: Path) -> str | None:
    rc, out, _ = git(repo, "rev-parse", "--show-toplevel")
    return out.strip() if rc == 0 else None


def git_branch(repo: Path) -> str | None:
    rc, out, _ = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if rc == 0 else None


def changed_paths(repo: Path, base_sha: str | None) -> tuple[list[str], list[str]]:
    """(committed, uncommitted) пути: base..HEAD плюс незакоммиченные.

    Имена читаются с -z (кавычки/пробелы не ломают разбор). Для committed
    используется `git log --name-only --no-renames` — все пути всех коммитов
    диапазона, включая созданные-и-переименованные (rename-эскейп: файл,
    рожденный вне зоны и переехавший в зону, виден по старому пути;
    `git diff --name-only` показал бы только новый путь внутри зоны).
    Для porcelain-статуса `R` сохраняются ОБА пути: старый путь — удаление,
    и удаление вне зоны само по себе нарушение. SESSION.md — маркер сессии
    (session_worktree.sh), в границах не проверяется.
    """
    committed: list[str] = []
    if base_sha:
        rc, out, err = git(
            repo, "log", "--name-only", "--no-renames", "--format=%x2D", "-z",
            f"{base_sha}..HEAD")
        if rc != 0:
            return [], [f"git log {base_sha}..HEAD: {err.strip()}"]
        committed = sorted({
            p.strip() for p in out.split("\0")
            if p.strip() and p.strip() != "-"})
    rc, out, err = git(repo, "status", "--porcelain", "-z", "-uall")
    if rc != 0:
        return committed, [f"git status: {err.strip()}"]
    uncommitted: list[str] = []
    toks = out.split("\0")
    i = 0
    while i < len(toks):
        tok = toks[i]
        if len(tok) < 4:
            i += 1
            continue
        entry = tok[3:]
        if entry.startswith('"') and entry.endswith('"'):
            entry = entry[1:-1]
        if entry != "SESSION.md":
            uncommitted.append(entry)
        if tok[:2] in ("R ", "RM", " R") and i + 1 < len(toks):
            i += 1
            old = toks[i]
            if old.startswith('"') and old.endswith('"'):
                old = old[1:-1]
            if old != "SESSION.md":
                uncommitted.append(old)  # старый путь: удаление вне зоны — тоже нарушение
        i += 1
    return committed, uncommitted


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # процесс чужого пользователя существует


# ------------------------------------------------------------- резервация
# P0.3 (пересмотр плана Заказчика): резервация несет policy_version —
# редакцию политики зон роли (ROLE_ZONE_POLICY, agents/README.md), которой
# была сужена зона. Без policy_version резервация отклоняется (MISSING_INPUT);
# идемпотентный повтор с ДРУГОЙ версией политики — DUPLICATE_PAYLOAD.
# Сужение выполняет вызывающий (flowctl prepare): session_check хранит и
# возвращает версию как есть.

# --------------------------------------------------------------- reserve


def _validate_request(req: dict) -> list[str]:
    problems = []
    for field in ("delegation_id", "role", "project", "owner_pm", "repo"):
        if not req.get(field):
            problems.append(f"{MISSING_INPUT}: отсутствует {field}")
    if not req.get("paths"):
        problems.append(f"{MISSING_INPUT}: пустая зона записи (paths)")
    if not req.get("policy_version"):
        problems.append(
            f"{MISSING_INPUT}: отсутствует policy_version (P0.3: резервация "
            "фиксирует редакцию политики зон роли — сузь зону через "
            "flowctl prepare или передай role_zone_policy.policy_version())")
    return problems


def reserve(req: dict, registry_path: Path | str) -> dict:
    """Атомарная резервация зоны. Идемпотентность: тот же delegation_id с
    идентичным payload → OK (existing); иной payload → DUPLICATE_PAYLOAD."""
    registry_path = Path(registry_path)
    problems = _validate_request(req)
    if problems:
        return {"allowed": False, "reason": MISSING_INPUT,
                "details": problems, "delegation_id": req.get("delegation_id", "")}

    repo = Path(req["repo"])
    toplevel = git_toplevel(repo)
    if toplevel:
        req = dict(req)
        req["repo"] = toplevel  # канонический repo — base сравнения зон

    lock = _acquire_lock(registry_path)
    try:
        data, err = registry_load(registry_path)
        if err:
            return {"allowed": False, "reason": REGISTRY_ERROR,
                    "details": [err],
                    "delegation_id": req["delegation_id"]}
        migrated = data.get("schema_version") != REGISTRY_SCHEMA
        if migrated:
            data = _load_or_migrate(registry_path, data)

        zone_patterns = [canonical(p) for p in req["paths"]]
        sessions = data.get("sessions", [])

        # Идемпотентность по delegation_id.
        mine = next((s for s in sessions
                     if isinstance(s, dict)
                     and s.get("delegation_id") == req["delegation_id"]), None)
        if mine is not None:
            payload_new = {
                "role": req["role"], "project": req["project"],
                "owner_pm": req["owner_pm"], "repo": req.get("repo"),
                "paths": sorted(zone_patterns),
                "policy_version": req.get("policy_version"),
                "worktree": req.get("worktree"), "branch": req.get("branch"),
            }
            payload_old = {
                "role": mine.get("role"), "project": mine.get("project"),
                "owner_pm": mine.get("owner_pm"), "repo": mine.get("repo"),
                "paths": sorted(mine.get("zones") or mine.get("paths") or []),
                "policy_version": mine.get("policy_version"),
                "worktree": mine.get("worktree"), "branch": mine.get("branch"),
            }
            if payload_new == payload_old:
                return {"allowed": True, "reason": REASON_OK,
                        "idempotent": True, "migrated": migrated, "details": [],
                        **flow_mode.mode_payload(),
                        "delegation_id": req["delegation_id"],
                        "session": mine}
            return {"allowed": False, "reason": DUPLICATE_PAYLOAD,
                    "details": [f"delegation_id {req['delegation_id']} уже "
                                f"зарезервирован с другим payload"],
                    **flow_mode.mode_payload(),
                    "delegation_id": req["delegation_id"]}

        # Пересечение зон активных сессий того же repo.
        conflicts: list[str] = []
        for s in sessions:
            if not isinstance(s, dict):
                continue
            if s.get("status") not in ZONE_HOLDING:
                continue
            other_repo = s.get("repo")
            if other_repo and req.get("repo") and other_repo != req["repo"]:
                continue
            other_zones = [canonical(p) for p in
                           (s.get("zones") or s.get("paths") or [])]
            hit = zones_overlap(zone_patterns, other_zones)
            if hit:
                conflicts.append(
                    f"{s.get('delegation_id', '?')} ({s.get('project', '?')}/"
                    f"{s.get('status', '?')}): паттерн {hit[0]!r} пересекается "
                    f"с {hit[1]!r}")
        if conflicts:
            return {"allowed": False, "reason": ZONE_CONFLICT,
                    "details": conflicts,
                    **flow_mode.mode_payload(),
                    "delegation_id": req["delegation_id"]}

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        entry = {
            "delegation_id": req["delegation_id"],
            "role": req["role"],
            "project": req["project"],
            "owner_pm": req["owner_pm"],
            "status": "reserved",
            "repo": req.get("repo"),
            "zones": zone_patterns,
            "policy_version": req.get("policy_version"),
            "worktree": req.get("worktree"),
            "branch": req.get("branch"),
            "base_sha": req.get("base_sha"),
            "snapshot_digest": req.get("snapshot_digest"),
            "pid": req.get("pid"),
            "reserved_at": now,
            "lifecycle": {
                "state": "reserved",
                "history": [{
                    "from": None, "to": "reserved", "at": now,
                    "reason": "zone reserved",
                    "evidence": f"registry: {registry_path}",
                }],
            },
        }
        sessions.append(entry)
        registry_write(registry_path, data)
        return {"allowed": True, "reason": REASON_OK, "idempotent": False,
                "migrated": migrated, "details": [],
                **flow_mode.mode_payload(),
                "delegation_id": req["delegation_id"], "session": entry}
    finally:
        _release_lock(lock)


def transition(session: dict, to: str, reason: str, evidence: str) -> None:
    """Смена статуса с evidence и причиной (жизненный цикл ТЗ 04)."""
    hist = session.setdefault("lifecycle", {"state": session.get("status"),
                                            "history": []})
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    hist.setdefault("history", []).append(
        {"from": session.get("status"), "to": to, "at": now,
         "reason": reason, "evidence": evidence})
    hist["state"] = to
    session["status"] = to


# ----------------------------------------------------------------- check


def check(session_req: dict, registry_path: Path) -> dict:
    """Post-check сессии: branch/worktree/SESSION.md + changed paths против
    разрешенной зоны. Ничего не пишет, кроме evidence-разметки статуса."""
    delegation_id = session_req.get("delegation_id")
    if not delegation_id or not session_req.get("repo"):
        return {"ok": False, "reason": MISSING_INPUT,
                "details": ["нужны delegation_id и repo"],
                "violations": [], "delegation_id": delegation_id or ""}
    repo = Path(session_req["repo"])
    data, err = registry_load(registry_path)
    if err:
        return {"ok": False, "reason": REGISTRY_ERROR, "details": [err],
                "violations": [], "delegation_id": delegation_id}
    session = next((s for s in data.get("sessions", [])
                    if isinstance(s, dict)
                    and s.get("delegation_id") == delegation_id), None)
    if session is None:
        return {"ok": False, "reason": MISSING_INPUT,
                "details": [f"reservation {delegation_id} не найден в реестре "
                            f"(check без reservation не гарантирует права)"],
                "violations": [], "delegation_id": delegation_id}
    zones = [canonical(p) for p in
             (session.get("zones") or session.get("paths") or [])]
    violations: list[str] = []

    # Физическая привязка: cwd процесса — worktree сессии (не «текстовое cwd»).
    expected_wt = session.get("worktree")
    toplevel = git_toplevel(repo)
    if expected_wt:
        exp = str(Path(expected_wt).resolve())
        if toplevel != exp:
            violations.append(
                f"{WRONG_WORKTREE}: git toplevel {toplevel} ≠ worktree сессии "
                f"{exp} (проверка rev-parse --show-toplevel)")
        rc, out, _ = git(Path(expected_wt), "branch", "--list",
                         "--format=%(refname:short)", session.get("branch") or "")
        current = git_branch(repo)
        if session.get("branch") and (not out.strip() or current
                                      != session["branch"]):
            violations.append(
                f"{WRONG_BRANCH}: текущая ветка {current} ≠ ожидаемой "
                f"{session['branch']}")
        if not (Path(expected_wt) / "SESSION.md").is_file():
            violations.append(
                f"{SESSION_MD_MISSING}: {expected_wt}/SESSION.md отсутствует "
                f"(маркер сессии, session_worktree.sh)")

    committed, uncommitted = changed_paths(repo, session.get("base_sha"))

    out_of_zone: list[str] = []
    for p in sorted(set(committed + uncommitted)):
        if not zone_holds(zones, p):
            out_of_zone.append(p)
    if out_of_zone:
        violations.append(
            f"{OUT_OF_ZONE}: измененные пути вне разрешенной зоны: "
            + ", ".join(out_of_zone[:20]))

    # Symlink: измененный symlink не должен указывать вне repo И вне зоны.
    repo_resolved = repo.resolve()
    for p in sorted(set(committed + uncommitted)):
        f = repo / p
        if f.is_symlink():
            target = f.resolve()
            if repo_resolved != target and repo_resolved not in target.parents:
                violations.append(
                    f"{OUT_OF_ZONE}: {p}: symlink указывает вне репозитория "
                    f"({target})")
            elif repo_resolved != target:
                # Цель внутри repo, но вне разрешенной зоны — тоже отказ
                # (приемка ТЗ 04: symlink за пределы зоны дают отказ).
                target_rel = canonical(
                    str(target.relative_to(repo_resolved)))
                if not zone_holds(zones, target_rel):
                    violations.append(
                        f"{OUT_OF_ZONE}: {p}: symlink указывает вне "
                        f"разрешенной зоны (цель {target_rel} не в "
                        f"{zones})")

    ok = not violations
    # Evidence-разметка результата в реестре (атомарно, с локом).
    lock = _acquire_lock(registry_path)
    try:
        data2, err2 = registry_load(registry_path)
        if not err2:
            if data2.get("schema_version") != REGISTRY_SCHEMA:
                data2 = _load_or_migrate(registry_path, data2)
            s2 = next((s for s in data2.get("sessions", [])
                       if isinstance(s, dict)
                       and s.get("delegation_id") == delegation_id), None)
            if s2 is not None:
                new_state = s2.get("status")
                if not ok and s2.get("status") in LIFECYCLE_ACTIVE:
                    new_state = "needs_attention"
                    s2["last_check"] = {
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime()),
                        "ok": False, "violations": violations,
                    }
                elif ok and s2.get("status") == "reserved":
                    new_state = "running"
                    s2["last_check"] = {
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime()), "ok": True,
                        "violations": [],
                    }
                if new_state and new_state != s2.get("status"):
                    transition(
                        s2, new_state,
                        "post-check failed" if not ok else "post-check ok",
                        "session_check check: " + "; ".join(violations or
                                                            ["в границах зоны"]),
                    )
                registry_write(registry_path, data2)
    finally:
        _release_lock(lock)
    return {"ok": ok,
            "reason": REASON_OK if ok else (violations[0].split(":")[0]
                                            if violations else OUT_OF_ZONE),
            "details": violations, "violations": violations,
            **flow_mode.mode_payload(),
            "out_of_zone": out_of_zone,
            "changed": {"committed": committed, "uncommitted": uncommitted},
            "zones": zones, "delegation_id": delegation_id}


# ------------------------------------------------------------- reconcile


def reconcile(registry_path: Path, repo: Path | None = None,
              delegation_id: str | None = None) -> dict:
    """Сверка после падения процесса: PID/worktree/git status/лог.

    Ничего не удаляет молча: запись помечается stale (процесс мертв, дерево
    чисто) или needs_attention (живые следы: dirty-дерево, live PID).
    Явное решение ПМ — отдельное действие (не в этом скрипте).
    """
    data, err = registry_load(registry_path)
    if err:
        return {"ok": False, "reason": REGISTRY_ERROR, "details": [err],
                "results": []}
    sessions = data.get("sessions", [])
    results: list[dict] = []
    dirty_any = False
    for s in sessions:
        if not isinstance(s, dict):
            continue
        if delegation_id and s.get("delegation_id") != delegation_id:
            continue
        if s.get("status") not in ("reserved", "running", "finished"):
            continue
        marks: list[str] = []
        srepo = None
        if repo is not None:
            srepo = repo
        elif s.get("worktree") and Path(s["worktree"]).is_dir():
            srepo = Path(s["worktree"])
        elif s.get("repo"):
            srepo = Path(s["repo"])
        uncommitted: list[str] = []
        if srepo is not None and git_toplevel(srepo):
            _, un = changed_paths(srepo, None)
            uncommitted = un
        pid = s.get("pid")
        alive = pid_alive(pid) if pid else None
        if alive:
            marks.append(f"PID {pid} жив — процесс еще работает, не stale")
        if uncommitted:
            marks.append(
                f"незакоммиченные изменения в дереве: "
                f"{', '.join(uncommitted[:10])} — ничего не удалять")
        wt = s.get("worktree")
        if wt and not Path(wt).is_dir():
            marks.append(f"worktree отсутствует: {wt}")
        if not marks:
            marks.append(
                f"PID отсутствует/мертв, дерево чисто — reservation устарел "
                f"(stale); решение ПМ обязательно")
        new_state = ("needs_attention" if alive or uncommitted else "stale")
        results.append({
            "delegation_id": s.get("delegation_id", ""),
            "status_before": s.get("status"),
            "marks": marks,
            "uncommitted": uncommitted,
            "pid_alive": alive,
        })
        if new_state == "needs_attention":
            dirty_any = True
    # Пишем разметку атомарно (с evidence в lifecycle), но ничего не удаляем.
    if results:
        lock = _acquire_lock(registry_path)
        try:
            data2, err2 = registry_load(registry_path)
            if not err2:
                if data2.get("schema_version") != REGISTRY_SCHEMA:
                    data2 = _load_or_migrate(registry_path, data2)
                by_id = {r["delegation_id"]: r for r in results}
                for s in data2.get("sessions", []):
                    if not isinstance(s, dict):
                        continue
                    r = by_id.get(s.get("delegation_id"))
                    if not r:
                        continue
                    r["status_after"] = r.get("status_before")
                    if s.get("status") in ("reserved", "running", "finished"):
                        new_state = ("needs_attention" if r["pid_alive"]
                                     or r["uncommitted"] else "stale")
                        if new_state != s.get("status"):
                            transition(
                                s, new_state,
                                "reconcile: " + "; ".join(r["marks"]),
                                "session_check reconcile: PID/worktree/"
                                "git status/лог сверены; запись сохранена")
                        r["status_after"] = new_state
                registry_write(registry_path, data2)
        finally:
            _release_lock(lock)
    return {"ok": True, "reason": REASON_OK, "details": [],
            "results": results,
            **flow_mode.mode_payload(),
            "needs_attention": dirty_any or any(
                r.get("status_after") == "needs_attention" for r in results)}


# ----------------------------------------------------------------- status


def status(registry_path: Path, delegation_id: str | None = None) -> dict:
    data, err = registry_load(registry_path)
    if err:
        return {"ok": False, "reason": REGISTRY_ERROR, "details": [err],
                "sessions": []}
    sessions = [s for s in data.get("sessions", [])
                if isinstance(s, dict)
                and (not delegation_id
                     or s.get("delegation_id") == delegation_id)]
    return {"ok": True, "reason": REASON_OK, "details": [],
            "schema_version": data.get("schema_version", "v1"),
            **flow_mode.mode_payload(),
            "sessions": sessions}


# -------------------------------------------------------------------- CLI


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for k, v in sorted(payload.items()):
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, sort_keys=True)
        print(f"{k}: {v}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="session_check.py",
        description="резервация зон, post-check границы, reconcile (поставка 04)",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    def add_registry(p):
        p.add_argument("--registry", required=True,
                       help="путь к active_sessions.json (в тестах — fixture)")

    p_res = sub.add_parser("reserve", help="атомарная резервация зоны")
    add_registry(p_res)
    p_res.add_argument("--repo", required=True)
    p_res.add_argument("--delegation-id", required=True)
    p_res.add_argument("--role", required=True)
    p_res.add_argument("--project", required=True)
    p_res.add_argument("--owner-pm", required=True)
    p_res.add_argument("--path", dest="paths", action="append", required=True,
                       help="паттерн зоны (glob; повторяемый)")
    p_res.add_argument("--worktree", default=None)
    p_res.add_argument("--branch", default=None)
    p_res.add_argument("--base-sha", default=None)
    p_res.add_argument("--snapshot-digest", default=None)
    p_res.add_argument("--pid", type=int, default=None)
    p_res.add_argument("--policy-version", default=None,
                       help="версия политики зон роли (P0.3: обязательна — "
                            "резервация фиксирует редакцию таблицы зон; "
                            "значение: role_zone_policy.policy_version())")
    p_res.add_argument("--json", action="store_true", dest="as_json")

    p_chk = sub.add_parser("check", help="post-check границы сессии")
    add_registry(p_chk)
    p_chk.add_argument("--repo", required=True)
    p_chk.add_argument("--delegation-id", required=True)
    p_chk.add_argument("--json", action="store_true", dest="as_json")

    p_rec = sub.add_parser("reconcile", help="сверка после падения процесса")
    add_registry(p_rec)
    p_rec.add_argument("--repo", default=None)
    p_rec.add_argument("--delegation-id", default=None)
    p_rec.add_argument("--json", action="store_true", dest="as_json")

    p_st = sub.add_parser("status", help="состояние резерваций")
    add_registry(p_st)
    p_st.add_argument("--delegation-id", default=None)
    p_st.add_argument("--json", action="store_true", dest="as_json")

    args = ap.parse_args(argv)

    if args.command == "reserve":
        registry_file = Path(args.registry)
        if registry_file.parent != Path(".") and not registry_file.parent.is_dir():
            result = {"allowed": False, "reason": REGISTRY_ERROR,
                      "details": [f"каталог реестра не существует: "
                                  f"{registry_file.parent}"],
                      "delegation_id": args.delegation_id}
            _emit(result, args.as_json)
            return 2
        result = reserve(
            {"repo": args.repo, "delegation_id": args.delegation_id,
             "role": args.role, "project": args.project,
             "owner_pm": args.owner_pm, "paths": args.paths,
             "policy_version": args.policy_version,
             "worktree": args.worktree, "branch": args.branch,
             "base_sha": args.base_sha, "snapshot_digest": args.snapshot_digest,
             "pid": args.pid},
            Path(args.registry),
        )
        result.setdefault("policy_version", args.policy_version)
        _emit(result, args.as_json)
        return 0 if result["allowed"] else 1

    if args.command == "check":
        result = check({"delegation_id": args.delegation_id,
                        "repo": args.repo}, Path(args.registry))
        _emit(result, args.as_json)
        return 0 if result["ok"] else 1

    if args.command == "reconcile":
        result = reconcile(Path(args.registry),
                           repo=Path(args.repo) if args.repo else None,
                           delegation_id=args.delegation_id)
        _emit(result, args.as_json)
        return 0 if result["ok"] else 2

    if args.command == "status":
        result = status(Path(args.registry), delegation_id=args.delegation_id)
        _emit(result, args.as_json)
        if args.delegation_id:
            return 0 if result["sessions"] else 1
        return 0 if result["ok"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
