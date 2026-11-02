#!/usr/bin/env python3
"""package_runner.py — раннер v0 конвейера пакетов (слои 1–2 диспетчера,
решение Заказчика: ПМ на входе/выходе пакета, середина — детерминированный
скрипт, вызывающий Pi роль-паки; развитие J28).

Слой 1 — формат манифеста: секция `pipeline:` в YAML-блоке PLAN-<id>.md
(гайд и пример — docs/guides/pipeline-manifest.md)::

    ```yaml
    pipeline:
      package: my-package
      flow: 4
      stages:
        - id: impl
          run: pi:dev
          zones: [backend/**, tests/**]
          inputs: [openspec/changes/my-package/spec.md]
          task: реализовать …
        - id: check
          run: gate:python3 scripts/flow_check.py
        - id: plan
          run: hermes:pm
    ```

    run: pi:<роль>     — Pi роль-пак skills/pm-factory/pi/role-packs/<роль>/
                         (роль — плоский идентификатор [A-Za-z0-9._-]+;
                         role-agents.md → AGENTS.md в worktree, а если там
                         уже есть AGENTS.md проекта — рядом как AGENTS.pi.md
                         с пометкой «читай вместе с AGENTS.md»; вызов
                         `pi --no-session` с zone-guard, env PI_ZONE_WRITE);
    run: gate:<команда>— shell-команда в worktree (env стенда E6 —
                         EKOTOV_WIKI_BASE_URL/EKOTOV_WIKI_DB_PATH — срезается;
                         скоуп тестов — ответственность gate-команды);
                         ненулевой exit → STOP;
    run: hermes:<роль> — стоп-точка ПМ: раннер печатает «ЖДУ ПМ …», exit 0;
                         после действий ПМ повторный запуск докатывает пакет.

Слой 2 — исполнение: v0 строго последовательный (без параллелей). depends[]
сверяется с порядком: зависимость от более позднего/несуществующего stage —
WARNING и игнор (не STOP). Worktree создается от текущего HEAD репо (не
origin/main — иначе теряется ветка ПМ с PLAN/карточками; дефект пилота
BUG-026 на ekotov-wiki). Состояние — JSON рядом с планом
(<план>.runner-state.json; путь печатается в начале прогона и при STOP);
resume = пропуск stage со статусом done (остановленный stage при повторе
исполняется заново — ретраев нет); чужой state (другой package/plan) —
RUNNER-STOP. hermes:-шаг получает статус waited: повторный запуск после
ПМ-действий считает его обработанным и докатывает пакет (диспатч — ручной,
раннер факт ПМ-работы не проверяет).

Вывод стадий не теряется: хвост stdout/stderr каждой стадии (≤50 строк)
сохраняется в state (stdout_tail/stderr_tail) и в лог <план>.runner.log.

Семантика останова: таймаут (env PIPELINE_TIMEOUT, сек; дефолт 1800),
ненулевой exit pi/gate, отсутствие роль-пака — STOP: exit 2 + диагностика
(stage, код, хвост stdout/stderr ≤30 строк) + строка в журнале плана.
Воротные отказы НЕ ретраятся. Секреты раннер не трогает: ключ pi берет сам
из ~/.pi/agent/models.json. hermes:-шаг и останов НЕ считаются ошибкой
исполнения ПМ-слоя: ПМ — владелец входа/выхода пакета.

flowctl.py не зависит от этого скрипта (отдельный слой); совместимость
импортов не затронута.

Usage:
    python3 scripts/package_runner.py <PLAN-файл> [--dry-run]

Exit codes: 0 — пакет докатан ИЛИ ожидается ручной диспатч ПМ (hermes:);
2 — STOP (манифест невалиден, битый state, таймаут, ненулевой exit pi/gate).
После «докатан» раннер печатает готовую команду очистки сессии
(session_worktree.sh remove); при STOP/hermes worktree сохраняется (resume).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent
ROLE_PACKS = REPO_ROOT / "skills" / "pm-factory" / "pi" / "role-packs"
WORKTREE_SCRIPT = SCRIPTS / "session_worktree.sh"
ZONE_GUARD = Path.home() / ".pi" / "agent" / "extensions" / "zone-guard.ts"
DEFAULT_TIMEOUT = 1800
DEFAULT_PROVIDER = "zai-glm"
DEFAULT_MODEL = "glm-5.3"
DIAG_TAIL_LINES = 30
STAGE_TAIL_LINES = 50
JOURNAL_HEADER = "## Журнал раннера"
LOG_SUFFIX = ".runner.log"
# Env стенда (E6), наследуемый раннером: в gate-команде срезается — с ним
# pytest коллекция цепляет tests/web (нужен стенд, E6-класс) и падает с
# collection error (дефект пилота BUG-026). Скоуп тестов — ответственность
# самой gate-команды (см. docs/guides/pipeline-manifest.md).
GATE_ENV_STRIP = ("EKOTOV_WIKI_BASE_URL", "EKOTOV_WIKI_DB_PATH")
STATE_SCHEMA = "package-runner-state/1"

RUN_KINDS = ("pi", "hermes", "gate")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FENCED_YAML_RE = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)


class ManifestError(Exception):
    """Невалидный манифест pipeline (STOP, exit 2)."""


class RunnerStop(Exception):
    """Остановка конвейера: stage, код, диагностика (STOP, exit 2)."""

ROLE_RE = re.compile(r"[A-Za-z0-9._-]+")
EXIT_STOP = 2


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------- парсинг
# Мини-парсер YAML-подмножества манифеста: скрипт самодостаточен (в CI —
# только pytest, PyYAML нет; прецедент стиля — pm_bounds_check повторяет
# parse_verdict без импорта). Поддерживается ровно то, что задокументировано
# в docs/guides/pipeline-manifest.md: вложенные отображения, списки
# (блочные `- x` и инлайновые `[a, b]`), скаляры (строка в кавычках/без,
# целое, true/false), комментарии целой строкой. Многострочных скаляров,
# якорей и тегов нет — за границами формата.


def _yaml_split_commas(inner: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _yaml_scalar(s: str):
    """Скаляр: кавычки снимаются, [..] — инлайн-список, целое — int."""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        return [_yaml_scalar(p) for p in _yaml_split_commas(s[1:-1])]
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    return s


def _yaml_rows(text: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        rows.append((len(line) - len(line.lstrip(" ")), stripped))
    return rows


def _yaml_dict(rows: list[tuple[int, str]], i: int,
               indent: int) -> tuple[dict, int]:
    out: dict = {}
    while i < len(rows) and rows[i][0] == indent \
            and not rows[i][1].startswith("- "):
        key, _, val = rows[i][1].partition(":")
        i += 1
        val = val.strip()
        if val:
            out[key.strip()] = _yaml_scalar(val)
        elif i < len(rows) and (rows[i][0] > indent
                                or (rows[i][0] == indent
                                    and rows[i][1].startswith("- "))):
            nested, i = _yaml_value(rows, i, rows[i][0])
            out[key.strip()] = nested
        else:
            out[key.strip()] = None
    return out, i


def _yaml_key_ok(key: str) -> bool:
    """Ключ отображения — идентификатор формата манифеста (package, run,
    zones…). Защита от трактовки 'quoted: scalar' как ключа."""
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*\s*", key) is not None


def _yaml_list(rows: list[tuple[int, str]], i: int,
               indent: int) -> tuple[list, int]:
    out: list = []
    while i < len(rows) and rows[i][0] == indent \
            and rows[i][1].startswith("- "):
        item = rows[i][1][2:].strip()
        i += 1
        if not item:
            nested, i = _yaml_value(rows, i, rows[i][0])
            out.append(nested)
        elif ":" in item:
            # первый ключ элемента-отображения в строке маркера
            key, _, val = item.partition(":")
            if not _yaml_key_ok(key):
                out.append(_yaml_scalar(item))
                continue
            val = val.strip()
            entry: dict = {}
            if val:
                entry[key.strip()] = _yaml_scalar(val)
                if i < len(rows) and rows[i][0] > indent:
                    rest, i = _yaml_dict(rows, i, rows[i][0])
                    entry.update(rest)
            else:
                nested, i = _yaml_value(rows, i, rows[i][0])
                entry[key.strip()] = nested
            out.append(entry)
        else:
            out.append(_yaml_scalar(item))
    return out, i


def _yaml_value(rows, i, indent):
    if rows[i][1].startswith("- "):
        return _yaml_list(rows, i, indent)
    return _yaml_dict(rows, i, indent)


def yaml_subset_load(text: str):
    """Манифест-подмножество YAML → dict. Синтаксическая ошибка отступов —
    ManifestError на уровне extract_pipeline/parse_manifest."""
    rows = _yaml_rows(text)
    if not rows:
        return {}
    value, _ = _yaml_value(rows, 0, rows[0][0])
    return value


def extract_pipeline(text: str) -> dict:
    """Достает секцию pipeline из первого YAML-блока, где она есть."""
    for m in _FENCED_YAML_RE.finditer(text):
        try:
            data = yaml_subset_load(m.group(1))
        except Exception as exc:  # noqa: BLE001 — диагностика, не молчание
            raise ManifestError(f"YAML-блок не парсится: {exc}") from exc
        if isinstance(data, dict) and "pipeline" in data:
            pipe = data["pipeline"]
            if not isinstance(pipe, dict):
                raise ManifestError("pipeline: должен быть отображением "
                                    "{package, flow, stages[]}")
            return pipe
    raise ManifestError("PLAN не содержит YAML-блока с секцией pipeline: "
                        "(```yaml … pipeline: …```)")


def parse_manifest(plan_path: Path) -> dict:
    """Валидирует манифест плана. Возвращает нормализованный dict:
    {package, flow, stages: [{id, kind, payload, zones, inputs, task,
    description, gate, depends}]}. Ошибки — ManifestError (без side effects)."""
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"PLAN не читается: {plan_path} ({exc})") from exc
    pipe = extract_pipeline(text)
    package = pipe.get("package")
    if not isinstance(package, str) or not package.strip():
        raise ManifestError("pipeline.package обязателен (непустая строка)")
    package = package.strip()
    flow = pipe.get("flow")
    if not isinstance(flow, int) or isinstance(flow, bool) \
            or not 1 <= flow <= 5:
        raise ManifestError("pipeline.flow обязателен (целое число 1–5)")
    stages_raw = pipe.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw:
        raise ManifestError("pipeline.stages обязателен "
                            "(непустой список stage)")
    seen: set[str] = set()
    stages: list[dict] = []
    for i, raw in enumerate(stages_raw):
        where = f"stages[{i}]"
        if not isinstance(raw, dict):
            raise ManifestError(f"{where}: stage должен быть отображением")
        sid = raw.get("id")
        if not isinstance(sid, str) or not sid.strip():
            raise ManifestError(f"{where}.id обязателен (непустая строка)")
        sid = sid.strip()
        if sid in seen:
            raise ManifestError(f"{where}.id дублируется: {sid!r}")
        seen.add(sid)
        run = raw.get("run")
        if not isinstance(run, str) or ":" not in run:
            raise ManifestError(
                f"{where}.run обязателен в виде pi:<роль> | hermes:<роль> | "
                f"gate:<команда>; получено: {run!r}")
        kind, _, payload = run.partition(":")
        if kind not in RUN_KINDS:
            raise ManifestError(
                f"{where}.run: неизвестный вид {kind!r} "
                f"(разрешены: {', '.join(k + ':' for k in RUN_KINDS)})")
        payload = payload.strip()
        if not payload:
            raise ManifestError(f"{where}.run: пустой payload после "
                                f"{kind}:")
        if kind == "pi" and not ROLE_RE.fullmatch(payload):
            # Роли — плоские каталоги роль-паков: без '/', без выхода наружу.
            raise ManifestError(
                f"{where}.run: роль pi — идентификатор "
                f"[A-Za-z0-9._-]+, получено: {payload!r}")
        zones = raw.get("zones") or []
        if kind == "pi" and not (isinstance(zones, list) and zones):
            raise ManifestError(
                f"{where}.zones обязателен для pi-шага (список globs — "
                "env PI_ZONE_WRITE роль-пака)")
        for field in ("zones", "inputs", "depends"):
            val = raw.get(field) or []
            if not isinstance(val, list) or not all(
                    isinstance(x, str) and x.strip() for x in val):
                raise ManifestError(
                    f"{where}.{field}: ожидается список непустых строк")
        if any(":" in z for z in zones):
            # Зоны склеиваются в PI_ZONE_WRITE через ':' — двоеточие
            # внутри зоны ломает парсинг guard'а.
            bad = next(z for z in zones if ":" in z)
            raise ManifestError(
                f"{where}.zones: зона с ':' недопустима "
                f"(PI_ZONE_WRITE разделяется по ':'): {bad!r}")
        stage = {
            "id": sid,
            "kind": kind,
            "payload": payload,
            "zones": [z.strip() for z in zones],
            "inputs": [x.strip() for x in (raw.get("inputs") or [])],
            "task": (raw.get("task") or "").strip(),
            "description": (raw.get("description") or "").strip(),
            "gate": (raw.get("gate") or "").strip(),
            "depends": [d.strip() for d in (raw.get("depends") or [])],
        }
        stages.append(stage)
    return {"package": package, "flow": flow, "stages": stages}


def sequence_warnings(stages: list[dict]) -> list[str]:
    """depends вне порядка (v0 — строгая последовательность): WARNING,
    зависимость игнорируется, не STOP."""
    warns: list[str] = []
    earlier: set[str] = set()
    for st in stages:
        for dep in st["depends"]:
            if dep not in earlier:
                warns.append(f"stage {st['id']}: depends[{dep!r}] вне "
                             "порядка/не существует — игнорируется "
                             "(v0: строгая последовательность)")
        earlier.add(st["id"])
    return warns


# --------------------------------------------------------------- state


def state_path_for(plan_path: Path) -> Path:
    return plan_path.parent / (plan_path.name + ".runner-state.json")


def state_load(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RunnerStop(f"битый runner-state (JSON): {path} ({exc})") from exc
    if not isinstance(data, dict) or data.get("schema_version") != STATE_SCHEMA:
        raise RunnerStop(f"битый runner-state: {path}")
    return data


def state_write(path: Path, state: dict) -> None:
    state["schema_version"] = STATE_SCHEMA
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2,
                              sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def state_record(state: dict, stage_id: str, status: str,
                 detail: str, exit_code: int | None = None,
                 stdout: str = "", stderr: str = "") -> None:
    """Запись стадии: + хвост stdout/stderr (≤50 строк) — вывод успешных
    стадий тоже сохраняется (дефект пилота: stdout терялся при успехе)."""
    entry = {
        "status": status, "at": utcnow_iso(),
        "detail": detail, "exit_code": exit_code,
        "stdout_tail": tail(stdout, STAGE_TAIL_LINES),
        "stderr_tail": tail(stderr, STAGE_TAIL_LINES),
    }
    state.setdefault("stages", {})[stage_id] = entry
    state.setdefault("history", []).append({
        "at": entry["at"], "stage": stage_id,
        "status": status, "detail": detail, "exit_code": exit_code,
    })


# ------------------------------------------------------------- journal


def journal_append(plan_path: Path, line: str) -> None:
    """Строка в «## Журнал раннера» плана: заголовка нет → секция создается
    в конце файла; есть → строка append'ится в конец файла (секция —
    последняя по формату плана)."""
    text = plan_path.read_text(encoding="utf-8")
    if JOURNAL_HEADER not in text:
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n{JOURNAL_HEADER}\n\n"
    plan_path.write_text(text + f"- {line}\n", encoding="utf-8")


def log_append(plan_path: Path, stage: dict, status: str,
               stdout: str, stderr: str) -> None:
    """Хвост stdout/stderr стадии (≤50 строк) — в лог рядом с планом
    (<план>.runner.log): вывод успешных стадий тоже доступен после прогона
    (дефект пилота BUG-026: stdout/stderr терялись при успехе)."""
    header = (f"--- {utcnow_iso()} | stage {stage['id']} | "
              f"{stage['kind']}:{stage['payload']} | {status} ---")
    log = plan_path.parent / (plan_path.name + LOG_SUFFIX)
    with open(log, "a", encoding="utf-8") as f:
        f.write(header + "\n")
        for name, data in (("stdout", stdout), ("stderr", stderr)):
            body = tail(data, STAGE_TAIL_LINES)
            if body.strip():
                f.write(f"[{name}]\n{body}\n")


# ------------------------------------------------------------ worktree


def repo_of(plan_path: Path) -> Path:
    """Git-репозиторий, которому принадлежит PLAN (worktree создается в нем)."""
    proc = subprocess.run(
        ["git", "-C", str(plan_path.parent), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RunnerStop(f"PLAN вне git-репозитория: {plan_path} "
                         f"({proc.stderr.strip()})")
    return Path(proc.stdout.strip())


def ensure_worktree(repo: Path, session_id: str, branch: str) -> Path:
    """Worktree сессии через session_worktree.sh (E5); уже существует —
    переиспользуется (resume/idempotency). База — текущий HEAD репо:
    дефолт скрипта (origin/main) теряет ветку ПМ — PLAN/карточки бага
    отсутствуют в сцене pi/gate (дефект пилота BUG-026 на ekotov-wiki).
    Скрипт умеет base-ref пятым аргументом — передаем HEAD."""
    expected = repo.parent / f"{repo.name}-worktrees" / session_id
    if expected.is_dir():
        return expected
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    cmd = ["bash", str(WORKTREE_SCRIPT), "create", str(repo),
           session_id, branch]
    if head.returncode == 0 and head.stdout.strip():
        cmd.append(head.stdout.strip())  # HEAD репо, не origin/main
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RunnerStop(f"worktree не создан "
                         f"({proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else 'без диагностики'})")
    return Path(proc.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------ pi


def pi_provider_model(role: str) -> tuple[str, str]:
    """Провайдер/модель роль-пака (settings.example.json: defaultProvider/
    defaultModel); пака/полей нет — дефолт zai-glm/glm-5.3."""
    settings = ROLE_PACKS / role / "settings.example.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_PROVIDER, DEFAULT_MODEL
    provider = data.get("defaultProvider") or DEFAULT_PROVIDER
    model = data.get("defaultModel") or DEFAULT_MODEL
    return str(provider), str(model)


def build_brief(manifest: dict, stage: dict,
                agents_file: str = "AGENTS.md") -> str:
    """Бриф pi-вызова: входы списком, задача — stage.task, иначе description."""
    task = stage["task"] or stage["description"]
    read_note = "прочитай сам"
    if agents_file != "AGENTS.md":
        read_note += " вместе с AGENTS.md проекта"
    lines = [
        f"# Пакет {manifest['package']} (Флоу {manifest['flow']}), "
        f"stage {stage['id']}",
        f"Роль: {stage['payload']} — ролевая инструкция в {agents_file} "
        f"корня worktree ({read_note})",
    ]
    if task:
        lines.append(f"Задача: {task}")
    if stage["inputs"]:
        lines.append("Входы (прочитай перед работой):")
        lines += [f"- {p}" for p in stage["inputs"]]
    lines.append("Зоны записи: " + (", ".join(stage["zones"]) or "—")
                 + " — запись вне зоны запрещена (zone-guard); push НЕТ")
    lines.append("Отчет: реализовано / проверено / вопросы — списком.")
    return "\n".join(lines)


def run_pi(args: list[str], cwd: Path, env: dict,
           timeout: int) -> tuple[int, str, str]:
    """Точка подмены в тестах (pi в тестах не вызывается)."""
    proc = subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def gate_env() -> dict:
    """Env для gate-команд: наследуется от раннера, но переменные стенда
    (E6) срезаются (эквивалент env -u EKOTOV_WIKI_BASE_URL EKOTOV_WIKI_DB_PATH).
    Скоуп тестов — ответственность самой gate-команды."""
    return {k: v for k, v in os.environ.items() if k not in GATE_ENV_STRIP}


def run_shell(cmd: str, cwd: Path, timeout: int) -> tuple[int, str, str]:
    """Точка подмены в тестах (gate-команды); env — gate_env() (без стенда)."""
    proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                          text=True, timeout=timeout, env=gate_env())
    return proc.returncode, proc.stdout, proc.stderr


def tail(text: str, lines: int = DIAG_TAIL_LINES) -> str:
    rows = [ln for ln in (text or "").splitlines()]
    return "\n".join(rows[-lines:])


def timeout_stream(data) -> str:
    """Частичный вывод TimeoutExpired (bytes даже в text-режиме) → str."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def parse_timeout(raw: str | None) -> int:
    """PIPELINE_TIMEOUT → сек; кривое/неположительное — STOP (exit 2)."""
    if raw is None or raw.strip() == "":
        return DEFAULT_TIMEOUT
    try:
        val = int(raw.strip())
    except ValueError as exc:
        raise RunnerStop(f"PIPELINE_TIMEOUT не целое: {raw!r} "
                         f"(дефолт {DEFAULT_TIMEOUT})") from exc
    if val <= 0:
        raise RunnerStop(f"PIPELINE_TIMEOUT должен быть > 0: {val} "
                         f"(дефолт {DEFAULT_TIMEOUT})")
    return val


# ---------------------------------------------------------------- main


def cmd_run(plan_path: Path, dry_run: bool) -> int:
    try:
        manifest = parse_manifest(plan_path)
    except ManifestError as exc:
        print(f"RUNNER-STOP: манифест: {exc}", file=sys.stderr)
        return 2
    for w in sequence_warnings(manifest["stages"]):
        print(f"RUNNER-WARNING: {w}", file=sys.stderr)

    if dry_run:
        print(json.dumps(manifest, ensure_ascii=False, indent=2,
                         sort_keys=True))
        return 0

    spath = state_path_for(plan_path)

    def _stop(stage: dict | None, detail: str, exit_code: int | None,
              stdout: str = "", stderr: str = "") -> int:
        if stage is not None:
            state_record(state, stage["id"], "stopped", detail, exit_code,
                         stdout, stderr)
            try:
                state_write(spath, state)
                try:
                    log_append(plan_path, stage, "STOP", stdout, stderr)
                except OSError:
                    pass
                journal_append(plan_path, f"{utcnow_iso()} | stage "
                               f"{stage['id']} | {stage['kind']}:"
                               f"{stage['payload']} | STOP ({detail})")
            except OSError:
                pass  # план удален/readonly — диагностика важнее state
        print(f"ЖДУ ПМ: STOP — {detail}", file=sys.stderr)
        # Дефект пилота BUG-026: после STOP ПМ не знал, где state-файл
        # (путь печатались только на hermes-шаге/успехе) — печатаем всегда.
        print(f"  state: {spath}", file=sys.stderr)
        if stdout.strip():
            print(f"--- stdout (хвост ≤{DIAG_TAIL_LINES} строк) ---",
                  file=sys.stderr)
            print(tail(stdout), file=sys.stderr)
        if stderr.strip():
            print(f"--- stderr (хвост ≤{DIAG_TAIL_LINES} строк) ---",
                  file=sys.stderr)
            print(tail(stderr), file=sys.stderr)
        return EXIT_STOP

    try:
        state = state_load(spath) or {
            "plan": str(plan_path), "package": manifest["package"],
            "flow": manifest["flow"], "stages": {}, "history": [],
        }
        # Resume-логика: state-файл лежит по имени плана — чужой/скопированный
        # state (другой package или plan) молча «докатил» бы не тот пакет.
        if state.get("package") not in (None, manifest["package"]) \
                or (state.get("plan") and state["plan"] != str(plan_path)):
            raise RunnerStop(
                f"runner-state чужой (package/plan не совпадают): {spath} "
                f"(state: package={state.get('package')!r}, "
                f"plan={state.get('plan')!r}; манифест: "
                f"package={manifest['package']!r}, plan={plan_path})")
        repo = repo_of(plan_path)
        timeout = parse_timeout(os.environ.get("PIPELINE_TIMEOUT"))
    except RunnerStop as exc:
        print(f"RUNNER-STOP: {exc}", file=sys.stderr)
        return EXIT_STOP
    # Дефект пилота BUG-026: путь state-файла печатается в начале каждого
    # прогона — файл «не теряется» после прогона на реальном репо.
    print(f"RUNNER: state: {spath}")
    done = {sid for sid, rec in state.get("stages", {}).items()
            if rec.get("status") in ("done", "waited")}

    # F12: суффикс-хэш пакета — «a b»/«a/b»/«a-b» не делят одну сессию.
    sane = _UNSAFE_RE.sub("-", manifest["package"]).strip("-")
    suffix = hashlib.sha1(manifest["package"].encode("utf-8")).hexdigest()[:8]
    session_id = f"pipeline-{sane}-{suffix}"
    branch = f"pipeline/{sane}-{suffix}"

    for stage in manifest["stages"]:
        if stage["id"] in done:
            print(f"RUNNER: stage {stage['id']} — уже выполнен (resume, "
                  "пропуск)")
            continue

        if stage["kind"] == "hermes":
            # Стоп-точка ПМ: статус «waited» (НЕ done) — при повторном
            # запуске после ПМ-действий stage считается обработанным и
            # пакет докатывается; диспатч и приемка — ручные.
            done.add(stage["id"])
            state_record(state, stage["id"], "waited", "стоп-точка ПМ")
            state_write(spath, state)
            pending = [s["id"] for s in manifest["stages"]
                       if s["id"] not in done]
            print(f"ЖДУ ПМ: stage {stage['id']}, роль {stage['payload']}")
            print(f"  пакет: {manifest['package']} (Флоу {manifest['flow']})")
            print(f"  выполнено: {', '.join(sorted(done)) or '—'}")
            print(f"  осталось после ПМ-шага: {', '.join(pending) or '—'}")
            print(f"  state: {spath}")
            journal_append(plan_path, f"{utcnow_iso()} | stage "
                           f"{stage['id']} | hermes:{stage['payload']} | "
                           "ЖДУ ПМ (стоп-точка)")
            return 0

        # pi/gate работают в worktree сессии пакета.
        try:
            worktree = ensure_worktree(repo, session_id, branch)
        except RunnerStop as exc:
            return _stop(stage, str(exc), None)
        state["worktree"] = str(worktree)
        state["branch"] = branch

        if stage["kind"] == "gate":
            try:
                rc, out, err = run_shell(stage["payload"], worktree, timeout)
            except subprocess.TimeoutExpired as exc:
                return _stop(stage, f"таймаут gate ({timeout}с)", None,
                             timeout_stream(exc.stdout),
                             timeout_stream(exc.stderr))
            if rc != 0:
                # Воротный отказ НЕ ретраится — разбор ПМ.
                return _stop(stage, f"gate exit {rc}", rc, out, err)
            state_record(state, stage["id"], "done", "gate ok", 0, out, err)
            state_write(spath, state)
            try:
                log_append(plan_path, stage, "ok", out, err)
            except OSError:
                pass
            journal_append(plan_path, f"{utcnow_iso()} | stage "
                           f"{stage['id']} | gate:{stage['payload']} | ok")
            done.add(stage["id"])
            continue

        # kind == pi: роль-пак → AGENTS.md в корень worktree (а если там
        # уже есть AGENTS.md проекта — рядом как AGENTS.pi.md, проектные
        # правила не затираются; дефект пилота BUG-026), вызов pi.
        role_pack = ROLE_PACKS / stage["payload"]
        role_agents = role_pack / "role-agents.md"
        if not role_agents.is_file():
            return _stop(stage, f"роль-пак не найден: {role_agents}", None)
        role_text = role_agents.read_text(encoding="utf-8")
        agents_target = worktree / "AGENTS.md"
        if agents_target.exists():
            agents_file = worktree / "AGENTS.pi.md"
            agents_file.write_text(
                role_text + "\n---\n> Читай вместе с AGENTS.md проекта: "
                "этот файл — ролевая инструкция Pi, проектные правила — "
                "в AGENTS.md.\n", encoding="utf-8")
        else:
            agents_file = agents_target
            agents_target.write_text(role_text, encoding="utf-8")
        provider, model = pi_provider_model(stage["payload"])
        env = dict(os.environ)
        env["PI_ZONE_WRITE"] = ":".join(stage["zones"])
        args = ["pi", "--provider", provider, "--model", model,
                "--no-session", "-e", str(ZONE_GUARD),
                "-p", build_brief(manifest, stage, agents_file.name)]
        try:
            rc, out, err = run_pi(args, worktree, env, timeout)
        except subprocess.TimeoutExpired as exc:
            return _stop(stage, f"таймаут pi ({timeout}с)", None,
                         timeout_stream(exc.stdout),
                         timeout_stream(exc.stderr))
        if rc != 0:
            return _stop(stage, f"pi exit {rc}", rc, out, err)
        # Опциональный gate-этап стадии (после успешного pi; отказ — STOP).
        if stage["gate"]:
            try:
                grc, gout, gerr = run_shell(stage["gate"], worktree, timeout)
            except subprocess.TimeoutExpired as exc:
                return _stop(stage, f"таймаут gate stage ({timeout}с)", None,
                             timeout_stream(exc.stdout),
                             timeout_stream(exc.stderr))
            if grc != 0:
                return _stop(stage, f"gate stage exit {grc}", grc, gout, gerr)
            out += f"\n--- gate ---\n{gout}"
            err += f"\n--- gate ---\n{gerr}"
        state_record(state, stage["id"], "done", "pi ok", 0, out, err)
        state_write(spath, state)
        try:
            log_append(plan_path, stage, "ok", out, err)
        except OSError:
            pass
        journal_append(plan_path, f"{utcnow_iso()} | stage {stage['id']} | "
                       f"pi:{stage['payload']} | ok")
        done.add(stage["id"])

    print(f"RUNNER: пакет {manifest['package']} докатан "
          f"({len(manifest['stages'])} stage); state: {spath}")
    print(f"RUNNER: очистка сессии (по желанию): "
          f"{WORKTREE_SCRIPT.name} remove {repo} {session_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Раннер v0 конвейера пакетов (слои 1–2 диспетчера)")
    ap.add_argument("plan", help="PLAN-файл с секцией pipeline:")
    ap.add_argument("--dry-run", action="store_true",
                    help="только распарсить и показать манифест")
    args = ap.parse_args(argv)
    return cmd_run(Path(args.plan).resolve(), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
