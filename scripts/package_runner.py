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
          zones: [backend/**, tests/unit/**]
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
                         `pi --no-session` с zone-guard, env PI_ZONE_WRITE
                         — зоны в префикс-форме guard'а: glob-хвосты `/**`,
                         `/*`, `**` и слэши срезаются normalize_zone, J61);
    run: gate:<команда>— shell-команда в worktree (env стенда E6 —
                         EKOTOV_WIKI_BASE_URL/EKOTOV_WIKI_DB_PATH — срезается;
                         скоуп тестов — ответственность gate-команды);
                         ненулевой exit → STOP;
    run: stand:<команда> — подъем стенда в worktree (J62): из stdout парсятся
                         строки «export VAR=value» (формат scripts/qa_stand_up.sh)
                         и сохраняются в state (stand_env) — env всех
                         ПОСЛЕДУЮЩИХ pi/gate стадий пакета получает эти
                         переменные. В stdout нет ни одного export → STOP
                         (стенд не отчитался env). Гашение стенда НЕ
                         выполняется автоматически: сироту подберет
                         reaper/gc (scripts/stand_reaper.py) или погасит ПМ
                         вручную (`bash scripts/qa_stand_up.sh stop` в
                         worktree); в конце прогона раннер печатает напоминание.
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
раннер факт ПМ-работы не проверяет). stand_env (J62) тоже живет в state —
resume докатывает пакет с тем же env стенда.

Вердикт-ворота (J62): у pi/gate стадии может быть ключ `verdict:`
{file: путь, contains: маркер} — после успешной стадии runner проверяет,
что файл в worktree содержит маркер (например «Одобрить» в review-файле);
не найдено → STOP «ЖДУ ПМ: возврат ревью» (exit 2, stage stopped, при
повторе стадия переисполняется — как gate-отказ, ретраев нет).

Опциональные стадии (J62-остаток, «шаги могут быть опциональными, типа
дизайнера»): ключ стадии `optional: true` — для pi/gate-стадий отказ НЕ
останавливает пакет: gate fail / verdict fail / pi fail / таймаут → стадия
помечается в state статусом `skipped`, в журнал плана и stderr пишется
WARNING, runner идет дальше (при pi-успехе — обычный проход, включая
gate/verdict). Обязательные стадии (без optional) — STOP как раньше.
У stand/hermes ключ не действует: молчаливый пропуск stand-стадии только
переносит гарантированный отказ глубже (env стенда нужен следующим
стадиям), hermes — ручная точка ПМ. На resume skipped-стадии не
переисполняются (аналог done/waited — ретраев нет).

Вывод стадий не теряется: хвост stdout/stderr каждой стадии (≤50 строк)
сохраняется в state (stdout_tail/stderr_tail) и в лог <план>.runner.log.

Семантика останова: таймаут (env PIPELINE_TIMEOUT, сек; дефолт 1800),
ненулевой exit pi/gate, отсутствие роль-пака — STOP: exit 2 + диагностика
(stage, код, хвост stdout/stderr ≤30 строк) + строка в журнале плана.
Воротные отказы НЕ ретраятся. Секреты раннер не трогает: ключ pi берет сам
из ~/.pi/agent/models.json. hermes:-шаг и останов НЕ считаются ошибкой
исполнения ПМ-слоя: ПМ — владелец входа/выхода пакета.

Ворота pi-стадии (J63): pi-стадия проходит жизненный цикл делегации —
`flowctl prepare --repo <worktree>` (снимок фактов — из дерева пакета, где
живет работа прошлых стадий; зона в реестре — по основному клону): план
релиза, требования, политика зон роли, ZONE_CONFLICT; DENY/UNKNOWN → STOP с
кодом → `flowctl run` → pi → `flowctl finish --gate-scope post_agent` (дифф ⊆
суженная зона + гейты этапа; returned/blocked → STOP) → повторная сверка
диффа с зоной → коммит стадии в ветку пакета → резервация finished → closed.
Этап — по роли из STAGE_TABLE флоу (`action:` стадии выбирает другой этап
роли; `task_id:` → --task, `approval_ref:` → --approval-ref). Роль без этапа
во флоу — резервация зоны по политике роли + post-check диффа
(ROLE_WITHOUT_STAGE, WARNING); роль вне политики — STOP ZONE_OUTSIDE_POLICY.
optional-стадия при отказе — skipped, изменения попытки — в git stash, зона
освобождается; обязательная — STOP, зона держится до resume (повтор стадии
освобождает зону прошлой попытки). Реестр/state — как у делегаций (env
PIPELINE_FLOW_REGISTRY / PIPELINE_FLOW_STATE переопределяют;
PIPELINE_OPENSPEC_CMD — OpenSpec CLI гейта openspec_validate). Ролевой
AGENTS-файл раннера после pi удаляется, SESSION.md не коммитится. Отчет
стадии для finish — <план>.runner-reports/<stage>-<попытка>.md.

flowctl.py не зависит от этого скрипта (отдельный слой); раннер — клиент
движка (flowctl CLI, session_check), который ищется рядом, в
AI_FACTORY_HOME и ~/ai-factory (нет — pi-стадия STOP
FACTORY_ENGINE_MISSING).

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
import itertools
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
# Движок ворот (flowctl/session_check/…) живет в фабрике и на проект не
# копируется: раннер ищет его рядом с собой, затем в AI_FACTORY_HOME и
# ~/ai-factory. Нет движка — pi-стадия получает STOP FACTORY_ENGINE_MISSING
# (без ворот pi не запускается), gate/stand/hermes-стадии работают.
sys.path.insert(0, str(SCRIPTS))
import factory_paths as fp  # noqa: E402  (J64.5: пути машины — только оттуда)
import flow_check  # noqa: E402  (J65.4: единый парсер вердикта ревью)

for _engine in (SCRIPTS, fp.factory_home() / "scripts"):
    if (_engine / "flowctl.py").is_file():
        sys.path.insert(0, str(_engine))
        break
try:
    import flowctl as fc  # noqa: E402  (registry_transition, пути state)
    import flow_transition as ft  # noqa: E402  (STAGE_TABLE: роль → этап)
    import role_zone_policy as rzp  # noqa: E402  (зоны ролей без этапа)
    import session_check as sc  # noqa: E402  (резервация/post-check)
    import gate_runner as gr  # noqa: E402  (provenance ревью, J65.4)
    ENGINE_ERROR = ""
except (ImportError, RuntimeError) as _exc:  # нет движка/реестра ролей (J65.1)
    fc = ft = rzp = sc = gr = None
    ENGINE_ERROR = str(_exc)

REPO_ROOT = SCRIPTS.parent
ROLE_PACKS = REPO_ROOT / "skills" / "pm-factory" / "pi" / "role-packs"
WORKTREE_SCRIPT = SCRIPTS / "session_worktree.sh"
ZONE_GUARD = fp.pi_agent_dir() / "extensions" / "zone-guard.ts"
FLOWCTL = Path(fc.__file__).resolve() if fc else SCRIPTS / "flowctl.py"
# Ворота pi-стадии: владелец резервации в реестре сессий и область finish.
FLOW_OWNER = "package_runner"
FLOW_GATE_SCOPE = "post_agent"
FLOWCTL_TIMEOUT = 600
# SESSION.md — маркер сессии session_worktree.sh, в коммит стадии не идет.
COMMIT_EXCLUDE = ("SESSION.md",)
REPORTS_SUFFIX = ".runner-reports"
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
# J64.5: список — из манифеста проекта (`pipeline.gate_env_strip`); дефолт —
# переменные стенда пилота ekotov-wiki (совместимость манифестов без ключа).
GATE_ENV_STRIP_DEFAULT = ("EKOTOV_WIKI_BASE_URL", "EKOTOV_WIKI_DB_PATH")
GATE_ENV_STRIP = GATE_ENV_STRIP_DEFAULT  # cmd_run ставит список манифеста
# Glob-хвост зон манифеста: zone-guard матчит зоны ПРЕФИКСОМ
# (norm === z || norm.startsWith(z + "/")), поэтому glob-форма
# `services/images/**` как префикс `services/images/***/` не матчится
# ни с одним путем — guard уходил в дефолтные зоны (J61, пилот BUG-026).
# Перед склейкой PI_ZONE_WRITE хвостовые `/**`, `/*`, `**` и слэши
# срезаются (normalize_zone).
_ZONE_GLOB_TAIL_RE = re.compile(r"(?:/\*\*?)+/?$")
_ZONE_LEADING_DOT_RE = re.compile(r"^\./")
# Строки «export VAR=value» в stdout stand-стадии (формат scripts/qa_stand_up.sh:
# ведущие пробелы допустимы — скрипт печатает их с отступом «  export …»).
STAND_EXPORT_RE = re.compile(
    r"^[ \t]*export[ \t]+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.MULTILINE)
STATE_SCHEMA = "package-runner-state/1"

RUN_KINDS = ("pi", "hermes", "gate", "stand")
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


def _yaml_strip_comment(line: str) -> str:
    """Комментарий в конце строки (` # …` вне кавычек) срезается — паритет
    с yaml.safe_load (`run: pi:sa  # пояснение` → `run: pi:sa`)."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'" and line[:i].rstrip()[-1:] in ("", ":", "-", "[", ","):
            quote = ch  # кавычка открывает скаляр только в его начале
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _yaml_rows(text: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = _yaml_strip_comment(raw).rstrip()
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


def policy_zones(pack: str, package: str) -> list[str]:
    """Зона роли пака из реестра ролей (J65.1) с подстановкой change-id
    пакета вместо плейсхолдера '<id>' ('*' реестра — маска имени, не id);
    движка нет или зоны нет — []."""
    if rzp is None:
        return []
    return [z.replace(rzp.CHANGE_ID, package)
            for z in rzp.registry_zones_for(flow_role(pack))]


def parse_manifest(plan_path: Path) -> dict:
    """Валидирует манифест плана. Возвращает нормализованный dict:
    {package, flow, stages: [{id, kind, payload, zones, inputs, task,
    description, gate, depends, verdict, optional}]}. Ошибки — ManifestError
    (без side effects)."""
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
        if kind == "pi" and not zones and "zones" not in raw:
            # J65.1: без zones — зона роли целиком из реестра agents/roles.json
            # ('*' — change-id пакета); зона манифеста ее только сужает.
            zones = policy_zones(payload, package)
            raw = {**raw, "zones": zones}
        if kind == "pi" and not (isinstance(zones, list) and zones):
            raise ManifestError(
                f"{where}.zones обязателен для pi-шага (список globs — "
                "env PI_ZONE_WRITE роль-пака): у роли нет зоны в реестре "
                "agents/roles.json или движок фабрики недоступен")
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
        verdict = raw.get("verdict")
        if verdict is not None:
            # Вердикт-ворота стадии (J62 → J65.4): вердикт файла решает
            # flow_check.parse_verdict (заголовок `## Вердикт: approve|return`,
            # последний решает); `contains` — устаревший ключ, игнорируется.
            if not isinstance(verdict, dict):
                raise ManifestError(
                    f"{where}.verdict: ожидается отображением {{file: путь}}")
            val = verdict.get("file")
            if not isinstance(val, str) or not val.strip():
                raise ManifestError(
                    f"{where}.verdict.file обязателен (непустая строка)")
            legacy = verdict.get("contains")
            if legacy is not None and not isinstance(legacy, str):
                raise ManifestError(
                    f"{where}.verdict.contains: ожидается строка (ключ "
                    "устарел и игнорируется, J65.4)")
        for field in ("action", "task_id", "approval_ref"):
            # Ворота pi-стадии: этап STAGE_TABLE (если роль участвует в
            # нескольких), id задачи tasks.md, ссылка на решение Заказчика —
            # прокидываются в flowctl prepare как есть.
            val = raw.get(field)
            if val is not None and not (isinstance(val, str) and val.strip()):
                raise ManifestError(
                    f"{where}.{field}: ожидается непустая строка, "
                    f"получено: {val!r}")
        optional = raw.get("optional")
        if optional is not None and not isinstance(optional, bool):
            # Опциональная стадия (J62-остаток): отказ pi/gate/verdict —
            # skipped + WARNING, пакет едет дальше (не STOP).
            raise ManifestError(
                f"{where}.optional: ожидается true/false, "
                f"получено: {optional!r}")
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
            "verdict": ({"file": verdict["file"].strip(),
                         **({"contains": verdict["contains"].strip()}
                            if verdict.get("contains") else {})}
                        if isinstance(verdict, dict) else None),
            "optional": bool(optional),
            "action": (raw.get("action") or "").strip(),
            "task_id": (raw.get("task_id") or "").strip(),
            "approval_ref": (raw.get("approval_ref") or "").strip(),
        }
        stages.append(stage)
    strip = pipe.get("gate_env_strip")
    if strip is not None and not (isinstance(strip, list) and all(
            isinstance(x, str) and x.strip() for x in strip)):
        # J64.5: env стенда проекта, срезаемый у gate-команд, — из манифеста.
        raise ManifestError("pipeline.gate_env_strip: ожидается список имен "
                            "переменных окружения")
    return {"package": package, "flow": flow, "stages": stages,
            "gate_env_strip": ([x.strip() for x in strip]
                               if strip is not None else None)}


def normalize_zone(zone: str) -> str:
    """Glob-зона манифеста → префикс для zone-guard (J61): guard матчит
    зоны префиксом (`norm === z || norm.startsWith(z + "/")`), поэтому
    glob-форма `services/images/**` не матчится ни с одним путем.
    Срезает хвостовые `/**`, `/*`, `**` и слэши; ведущий `./` снимается
    (паритет с guard'ом). Плоская форма проходит без изменений:
    `services/images/**` → `services/images`, `tests` → `tests`.
    Glob в середине (зоны политики ролей: `code-reviews/*/review-*.md`)
    → конкретный префикс до первого glob-сегмента (`code-reviews`): guard —
    грубая граница, точную сверку диффа с зоной делает session_check
    (ворота pi-стадии). Голый `**`/`*` («весь репо») префиксом не
    выражается — остается как есть (guard такую зону не совпадет;
    осознанное ограничение v0).
    """
    z = _ZONE_LEADING_DOT_RE.sub("", zone.strip())
    z = _ZONE_GLOB_TAIL_RE.sub("", z).rstrip("/")
    prefix = "/".join(itertools.takewhile(
        lambda seg: not any(ch in seg for ch in "*?["), z.split("/")))
    return prefix or z


def parse_stand_env(stdout: str) -> dict[str, str]:
    """Строки «export VAR=value» из stdout stand-стадии → dict (J62).
    Формат scripts/qa_stand_up.sh: ведущие пробелы допустимы; парные
    кавычки вокруг значения снимаются. Строки без export игнорируются."""
    env: dict[str, str] = {}
    for m in STAND_EXPORT_RE.finditer(stdout or ""):
        name, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        env[name] = val
    return env


def check_verdict(worktree: Path, verdict: dict,
                  reviewed_sha: str = "") -> tuple[str | None, str | None]:
    """Вердикт-ворота стадии (J62 → J65.4) → (вердикт, отказ).

    Вердикт решает flow_check.parse_verdict — тот же парсер, что у
    pr_validate/gate_runner (J10): заголовок `## Вердикт: approve|return`
    (Одобрить/Вернуть), последний решает, «Не одобрить» и шаблон с обоими
    словами — return. Строка `Commit: <sha>` в файле, если есть, обязана
    указывать на проверявшийся коммит (иначе STALE). Отказ — None, если
    вердикт approve и не STALE."""
    if verdict.get("contains"):
        print(f"RUNNER: WARNING: verdict.contains устарел и игнорируется "
              f"(J65.4) — вердикт {verdict['file']} читается по заголовку "
              f"«## Вердикт: approve|return»")
    path = worktree / verdict["file"]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, (f"вердикт-ворота: файл вердикта не читается: "
                      f"{verdict['file']} ({exc})")
    value = flow_check.parse_verdict(text)
    if value is None:
        return None, (f"вердикт-ворота: в {verdict['file']} нет вердикта — "
                      f"нужен заголовок «## Вердикт: approve» или "
                      f"«## Вердикт: return» (Одобрить/Вернуть)")
    stated = flow_check.review_commit(text)
    if stated and reviewed_sha and not reviewed_sha.lower().startswith(stated):
        return value, (f"вердикт-ворота: STALE — вердикт в {verdict['file']} "
                       f"выдан на коммит {stated}, а проверялся "
                       f"{reviewed_sha[:12]}")
    if value == "return":
        return value, f"вердикт-ворота: {verdict['file']} — return"
    return value, None


def record_review_provenance(plan_path: Path, worktree: Path, manifest: dict,
                             stage: dict, state: dict, gate: dict,
                             verdict: str) -> str:
    """Аудит-запись provenance ревью стадии Pi (J65.4) в каталоге отчетов
    раннера `<план>.runner-reports/<stage>-<попытка>.provenance.json`.

    Запись — для аудита: на какой коммит выдан вердикт (HEAD до стадии
    ревью), digest диффа от начала пакета, ревьюер — делегация стадии, авторы
    — делегации pi-стадий других ролей с коммитом после предыдущей
    review-стадии. Рядом с review-файлом sidecar НЕ кладется: его читает
    flow_transition (provenance_findings) и J10 со своей семантикой
    (`HEAD^..HEAD`, задача tasks.md, Reviewer-Delegation из реестра Hermes);
    интеграция ревью стадий Pi с этими воротами — отдельная задача.
    Возвращает путь записи или бросает RunnerStop."""
    if gr is None:
        raise RunnerStop(f"provenance ревью: движок фабрики недоступен "
                         f"({ENGINE_ERROR})")
    gates = state.get("flow_gates", {})
    authors: list[str] = []
    for prev in manifest["stages"]:
        if prev["id"] == stage["id"]:
            break
        g = gates.get(prev["id"])
        if prev["kind"] != "pi" or not g:
            continue
        if prev.get("verdict"):
            authors = []  # предыдущее ревью закрыло свой отрезок работы
        elif prev["payload"] != stage["payload"] and g.get("commit"):
            authors.append(g["delegation_id"])
    reviewed = gate["base_sha"]
    first = next((g for g in gates.values() if g.get("base_sha")), gate)
    base = first["base_sha"]
    rdir = plan_path.with_name(plan_path.name + REPORTS_SUFFIX)
    out = rdir / f"{stage['id']}-{gate.get('attempt', 1)}.provenance.json"
    try:
        rdir.mkdir(parents=True, exist_ok=True)
        digest = gr.compute_diff_digest(worktree, base, reviewed)
        _, payload = gr.write_review_provenance(
            worktree / stage["verdict"]["file"], repo_of_name(worktree),
            manifest["package"], [stage.get("task_id") or manifest["package"]],
            ",".join(authors) or f"package:{manifest['package']}",
            gate["delegation_id"], reviewed, verdict,
            diff_digest=digest, diff_base=base, sidecar_path=out)
        payload["authors"] = authors
        payload["scope"] = "runner-audit"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                  sort_keys=True), encoding="utf-8")
    except (ValueError, OSError) as exc:
        raise RunnerStop(f"provenance ревью не записан: {exc}") from exc
    return str(out)


def repo_of_name(worktree: Path) -> str:
    """Имя проекта для sidecar: каталог основного клона (не worktree)."""
    common = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
        capture_output=True, text=True).stdout.strip()
    if common:
        path = Path(common)
        if not path.is_absolute():
            path = (worktree / path).resolve()
        return path.parent.name
    return worktree.name


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


def gate_env(stand_env: dict | None = None) -> dict:
    """Env для gate-команд: наследуется от раннера, но переменные стенда
    (E6) срезаются (эквивалент env -u по списку GATE_ENV_STRIP: манифест
    `pipeline.gate_env_strip`, иначе GATE_ENV_STRIP_DEFAULT).
    Скоуп тестов — ответственность самой gate-команды. stand_env (J62) —
    переменные, отчитавшиеся stand-стадией пакета: накладываются ПОСЛЕ
    срезки (осознанный export манифеста проходит, случайное наследование
    окружения раннера — нет)."""
    env = {k: v for k, v in os.environ.items() if k not in GATE_ENV_STRIP}
    if stand_env:
        env.update(stand_env)
    return env


def run_shell(cmd: str, cwd: Path, timeout: int,
              stand_env: dict | None = None) -> tuple[int, str, str]:
    """Точка подмены в тестах (gate/stand-команды); env — gate_env()
    (без стенда из окружения раннера, со stand_env stand-стадий пакета)."""
    proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                          text=True, timeout=timeout,
                          env=gate_env(stand_env))
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


# ----------------------------------------------------------- flow gates
# Ворота pi-стадии: тот же жизненный цикл, что у делегации delegate_task
# (flowctl prepare → run → агент → finish), — pi-исполнитель не обходит
# движок (DENY на входе) и реестр сессий (ZONE_CONFLICT между параллельными
# пакетами/делегациями). Роль без этапа в STAGE_TABLE своего флоу (ba,
# regress_runner, devops вне Ф3) — решение flowctl не выносится: только
# резервация зоны по политике роли и post-check диффа (session_check),
# WARNING ROLE_WITHOUT_STAGE. После приемки стадия коммитится в ветку
# пакета, резервация закрывается (finished → closed): следующая стадия
# стартует от чистого дерева, ее дифф — только ее работа.


class GateDenied(Exception):
    """Ворота pi-стадии не пропустили (код причины + диагностика)."""


def flow_role(pack: str) -> str:
    """Роль-пак → роль политики зон / STAGE_TABLE: code-reviewer → code_reviewer."""
    return pack.replace("-", "_")


def stage_action(role: str, flow: int, override: str = "") -> str | None:
    """Этап STAGE_TABLE флоу, в котором участвует роль: первый по порядку
    таблицы; `action:` манифеста выбирает другой этап той же роли. Роль без
    этапа во флоу → None (ворота зоны без решения flowctl)."""
    actions = [s.action for s in ft.STAGE_TABLE.get(flow, ())
               if role in s.roles]
    if override:
        if override not in actions:
            raise GateDenied(
                f"STAGE_ACTION_INVALID: action {override!r} не этап роли "
                f"{role} во Флоу {flow} (этапы роли: "
                f"{', '.join(actions) or '—'})")
        return override
    return actions[0] if actions else None


def flow_paths() -> tuple[Path, Path]:
    """Реестр сессий и state flowctl: те же, что у делегаций (иначе
    конфликт зон с ними не виден); env PIPELINE_FLOW_REGISTRY /
    PIPELINE_FLOW_STATE переопределяют (тесты, второй инстанс фабрики)."""
    registry = Path(os.environ.get("PIPELINE_FLOW_REGISTRY")
                    or fp.registry_path())
    state = Path(os.environ.get("PIPELINE_FLOW_STATE")
                 or fc.default_state_path(str(registry)))
    return registry, state


def run_flowctl(argv: list[str]) -> tuple[int, dict, str]:
    """Точка подмены в тестах: flowctl <argv> --json → (rc, payload, stderr)."""
    try:
        proc = subprocess.run([sys.executable, str(FLOWCTL), *argv],
                              capture_output=True, text=True,
                              timeout=FLOWCTL_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        # Сбой вызова движка — отказ ворот (STOP с записью стадии), не
        # traceback раннера.
        return 2, {}, f"flowctl {argv[0]}: {type(exc).__name__}: {exc}"
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = {}
    return proc.returncode, payload if isinstance(payload, dict) else {}, \
        proc.stderr


def _flowctl_reason(payload: dict, stderr: str) -> str:
    """Код причины отказа flowctl + блокирующие причины решения/дефекты
    приемки (решение отказа prepare лежит в record.decision)."""
    decision = (payload.get("decision")
                or (payload.get("record") or {}).get("decision") or {})
    reservation = payload.get("reservation") or {}
    code = (payload.get("reason") or payload.get("verdict")
            or reservation.get("reason") or decision.get("status")
            or "UNKNOWN")
    parts = [str(x) for x in reservation.get("details") or []]
    parts += [str(x) for x in decision.get("details") or []]
    if not parts:
        parts = [str(x) for x in decision.get("blocking_reasons") or []]
    parts += [str(x) for x in payload.get("field_diagnostics") or []]
    for item in payload.get("defects") or []:
        if isinstance(item, dict) and item.get("code") != "SKIPPED":
            parts.append(str(item.get("detail") or item.get("code")))
    for key in ("zone_violations", "details"):
        parts += [str(x) for x in payload.get(key) or []]
    if not parts and stderr.strip():
        parts.append(stderr.strip().splitlines()[-1])
    return code + (f" ({'; '.join(dict.fromkeys(parts))})" if parts else "")


def _only_pr_gate_pending(payload: dict) -> bool:
    """finish provisional, где единственное невыполненное — pr_validate
    (нет PR-контекста: это ворота PR/CI, flow.yml, а не стадии). Любой
    другой дефект (зона, FAIL гейта) — не принято."""
    if payload.get("verdict") != "provisional":
        return False
    return all(isinstance(d, dict) and d.get("code") == "SKIPPED"
               and str(d.get("detail", "")).startswith("pr_validate")
               for d in payload.get("defects") or [])


def _git_head(path: Path) -> str:
    proc = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _registry_status(registry: Path, delegation_id: str) -> str | None:
    data, err = sc.registry_load(registry)
    if err:
        return None
    session = next((s for s in data.get("sessions", [])
                    if isinstance(s, dict)
                    and s.get("delegation_id") == delegation_id), None)
    return session.get("status") if session else None


def _transition(registry: Path, delegation_id: str, to: str, reason: str,
                evidence: str, expect_from: str) -> None:
    """Смена статуса резервации; отказ — WARNING (зона может остаться
    занятой — разбор ПМ: flowctl status/reconcile), не молча."""
    tr = fc.registry_transition(registry, delegation_id, to, reason,
                                evidence, expect_from=expect_from)
    if not tr.get("ok"):
        print(f"RUNNER-WARNING: резервация {delegation_id}: {expect_from} → "
              f"{to} не выполнена ({tr.get('reason')}: "
              f"{'; '.join(map(str, tr.get('details') or []))})",
              file=sys.stderr)


def gate_new(stage: dict, repo: Path, worktree: Path) -> dict:
    """Запись ворот попытки — создается и сохраняется в runner-state ДО
    prepare: сбой между prepare и сохранением не оставляет резервацию без
    владельца (повтор стадии ее освободит)."""
    if fc is None:
        raise GateDenied(f"FACTORY_ENGINE_MISSING: движок ворот фабрики "
                         f"(flowctl.py) не найден рядом с раннером, в "
                         f"AI_FACTORY_HOME и ~/ai-factory ({ENGINE_ERROR})")
    registry, fstate = flow_paths()
    cid = uuid.uuid4().hex  # каждая попытка — своя запись цикла flowctl
    return {"cid": cid, "delegation_id": f"deleg-{cid[:16]}",
            "role": flow_role(stage["payload"]), "kind": "pending",
            "registry": str(registry), "state": str(fstate),
            "repo": str(repo), "base_sha": _git_head(worktree)}


def gate_begin(manifest: dict, stage: dict, repo: Path, worktree: Path,
               branch: str, gate: dict) -> dict:
    """Вход pi-стадии: решение flowctl + резервация зоны. Отказ — GateDenied
    (созданная резервация освобождается). Снимок фактов — из worktree
    пакета: работа прошлых стадий живет в его ветке, не в основном клоне;
    зона в реестре — по основному клону (session_check.git_main_root)."""
    role = gate["role"]
    flow = manifest["flow"]
    registry = Path(gate["registry"])
    action = stage_action(role, flow, stage.get("action", ""))
    if action is None:
        return _zone_begin(gate, stage, repo, worktree, branch, registry)
    gate.update(kind="flowctl", action=action)
    cid = gate["cid"]
    common = ["--registry", gate["registry"], "--state", gate["state"],
              "--json"]
    argv = ["prepare", "--repo", str(worktree), "--project", repo.name,
            "--flow", str(flow), "--change", manifest["package"],
            "--action", action, "--role", role,
            "--worktree", str(worktree), "--owner-pm", FLOW_OWNER,
            "--correlation-id", cid, *common]
    for zone in stage["zones"]:
        argv += ["--path", zone]
    if stage.get("task_id"):
        argv += ["--task", stage["task_id"]]
    if stage.get("approval_ref"):
        argv += ["--approval-ref", stage["approval_ref"]]
    # Паритет с flow-enforcer: Ф3 — scope = incident_ref, Ф5 — экспресс.
    if flow == 3:
        argv += ["--incident-ref", manifest["package"]]
    if flow == 5:
        argv += ["--small-change"]
    rc, payload, err = run_flowctl(argv)
    if rc != 0 or not payload.get("prepared"):
        # prepare мог создать резервацию и отказать после нее (решение
        # перестало быть ALLOW) — освобождаем, не оставляя сироту.
        gate_abandon(gate, f"prepare отклонен ({action})")
        raise GateDenied(f"flowctl prepare {action}: "
                         f"{_flowctl_reason(payload, err)}")
    gate["zones"] = ((payload.get("record") or {}).get("zones")
                     or stage["zones"])
    rc, payload, err = run_flowctl(["run", "--correlation-id", cid, *common])
    if rc != 0:
        gate_abandon(gate, "flowctl run отклонен")
        raise GateDenied(f"flowctl run: {_flowctl_reason(payload, err)}")
    return gate


def _zone_begin(gate: dict, stage: dict, repo: Path, worktree: Path,
                branch: str, registry: Path) -> dict:
    """Роль без этапа во флоу: зона по политике роли + резервация."""
    role = gate["role"]
    gate["kind"] = "zone"
    narrowed = rzp.narrow_zones(role, [sc.canonical(z)
                                       for z in stage["zones"]])
    if narrowed["violations"] or not narrowed["zones"]:
        raise GateDenied(
            f"ZONE_OUTSIDE_POLICY: зоны вне политики роли {role} "
            f"({rzp.POLICY_SOURCE}): "
            f"{', '.join(narrowed['violations'] or stage['zones'])}")
    res = sc.reserve({
        "repo": str(worktree), "delegation_id": gate["delegation_id"],
        "role": role, "project": repo.name, "owner_pm": FLOW_OWNER,
        "paths": narrowed["zones"],
        "policy_version": narrowed["policy_version"],
        "role_zone_digest": narrowed["role_zone_digest"],
        "worktree": str(worktree), "branch": branch,
        "base_sha": gate["base_sha"],
    }, registry)
    if not res.get("allowed"):
        raise GateDenied(f"session_check reserve: {res.get('reason')} "
                         f"({'; '.join(res.get('details') or [])})")
    _transition(registry, gate["delegation_id"], "running",
                           "package_runner: старт pi-стадии",
                           f"stage {stage['id']}", expect_from="reserved")
    gate["zones"] = narrowed["zones"]
    print(f"RUNNER-WARNING: ROLE_WITHOUT_STAGE: роль {role} без этапа "
          "STAGE_TABLE своего флоу — решение flowctl не выносится, только "
          "зона по политике роли и post-check диффа", file=sys.stderr)
    return gate


def gate_end(gate: dict, worktree: Path, report: Path) -> tuple[bool, str]:
    """Приемка pi-стадии: дифф ⊆ зона + гейты этапа (flowctl finish) или
    post-check зоны (роль без этапа). (accepted, деталь)."""
    registry = Path(gate["registry"])
    if gate["kind"] == "zone":
        res = sc.check({"delegation_id": gate["delegation_id"],
                        "repo": str(worktree)}, registry)
        if not res.get("ok"):
            return False, (f"session_check check: {res.get('reason')} "
                           f"({'; '.join(map(str, res.get('violations') or res.get('details') or []))})")
        _transition(registry, gate["delegation_id"], "finished",
                               "package_runner: дифф в зоне",
                               f"report={report}", expect_from="running")
        return True, "zone ok"
    argv = ["finish", "--correlation-id", gate["cid"], "--report",
            str(report), "--gate-scope", FLOW_GATE_SCOPE,
            "--registry", gate["registry"], "--state", gate["state"],
            "--json"]
    # pm_bounds_check (гейт этапов Флоу 4 и др., J3) — только в явном
    # режиме: для стадии раннера — sessions (реестр, где лежит резервация
    # стадии; коммитов стадии на момент finish еще нет).
    argv += ["--pm-mode", "sessions", "--pm-registry", gate["registry"]]
    # Команда OpenSpec CLI для гейта openspec_validate (дефолт flowctl —
    # `npx openspec`; на машине с глобальным CLI — `openspec`).
    if os.environ.get("PIPELINE_OPENSPEC_CMD"):
        argv += ["--openspec-cmd", os.environ["PIPELINE_OPENSPEC_CMD"]]
    rc, payload, err = run_flowctl(argv)
    if rc == 0 and payload.get("verdict") == "accepted":
        return True, "flowctl finish: accepted"
    if _only_pr_gate_pending(payload):
        # Дифф в зоне, гейты этапа PASS; pr_validate — ворота PR пакета в
        # CI (flow.yml). Запись цикла flowctl остается provisional с
        # пометкой раннера (gate_close); зона стадии освобождается
        # (running → finished → closed). Повторный finish по такой записи
        # движок отклоняет: резервация уже закрыта.
        _transition(
            Path(gate["registry"]), gate["delegation_id"], "finished",
            "package_runner: provisional (pr_validate — на PR CI)",
            f"report={report}", expect_from="running")
        gate["pr_gate"] = "pending"
        return True, "flowctl finish: provisional (pr_validate — на PR CI)"
    return False, f"flowctl finish: {_flowctl_reason(payload, err)}"


def gate_close(gate: dict, commit: str | None) -> None:
    """Принятая и закоммиченная стадия освобождает зону (finished → closed);
    запись цикла flowctl получает пометку раннера (коммит, отложенный
    pr_validate) — аудит видит, чем закончилась provisional-запись."""
    _transition(
        Path(gate["registry"]), gate["delegation_id"], "closed",
        "package_runner: стадия принята и закоммичена",
        f"commit={commit or 'без изменений'}", expect_from="finished")
    if gate.get("kind") != "flowctl":
        return

    def _mark(st: dict) -> None:
        rec = st.get("runs", {}).get(gate["cid"])
        if rec is not None:
            rec["package_runner"] = {
                "closed_at": utcnow_iso(), "commit": commit,
                "pr_validate": ("на PR CI пакета (flow.yml)"
                                if gate.get("pr_gate") else "не требовался"),
            }

    try:
        fc.state_write_locked(Path(gate["state"]),
                              fc.state_load(Path(gate["state"])), _mark)
    except OSError as exc:
        print(f"RUNNER-WARNING: пометка записи flowctl {gate['cid']} не "
              f"записана: {exc}", file=sys.stderr)


def gate_abandon(gate: dict, reason: str) -> None:
    """Отказ/повтор стадии: зона освобождается, worktree и файлы сохранены
    (ничего не удаляется молча). running → needs_attention → released;
    finished (вердикт returned) → released."""
    registry = Path(gate["registry"])
    did = gate["delegation_id"]
    status = _registry_status(registry, did)
    if status == "running":
        _transition(registry, did, "needs_attention",
                               f"package_runner: {reason}",
                               "pi-стадия не принята", expect_from="running")
        status = "needs_attention"
    if status in ("reserved", "needs_attention") \
            and gate.get("kind") == "flowctl":
        run_flowctl(["release", gate["cid"], "--reason",
                     f"package_runner: {reason}",
                     "--registry", gate["registry"], "--state", gate["state"],
                     "--json"])
    elif status in ("reserved", "needs_attention", "finished"):
        _transition(registry, did, "released",
                               f"package_runner: {reason}",
                               "worktree сохранен", expect_from=status)


def commit_stage(worktree: Path, message: str) -> str | None:
    """Коммит принятой стадии в ветку пакета; нечего коммитить → None."""
    excludes = [f":(exclude){p}" for p in COMMIT_EXCLUDE]
    add = subprocess.run(["git", "-C", str(worktree), "add", "-A", "--", ".",
                          *excludes], capture_output=True, text=True)
    if add.returncode != 0:
        raise RunnerStop(f"git add стадии не выполнен: "
                         f"{add.stderr.strip() or add.stdout.strip()}")
    if subprocess.run(["git", "-C", str(worktree), "diff", "--cached",
                       "--quiet"]).returncode == 0:
        return None
    proc = subprocess.run(["git", "-C", str(worktree), "commit", "-q", "-m",
                           message], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RunnerStop(f"коммит стадии не создан: "
                         f"{proc.stderr.strip() or proc.stdout.strip()}")
    return _git_head(worktree)


def zone_recheck(gate: dict, worktree: Path) -> list[str]:
    """Повторная сверка диффа с зоной резервации прямо перед коммитом:
    файлы, появившиеся после приемки (артефакты гейтов finish), в коммит
    стадии без сверки не попадают. Возвращает пути вне зоны."""
    data, err = sc.registry_load(Path(gate["registry"]))
    session = next((s for s in (data or {}).get("sessions", [])
                    if isinstance(s, dict)
                    and s.get("delegation_id") == gate["delegation_id"]),
                   None)
    if err or session is None:
        return [f"резервация {gate['delegation_id']} не читается "
                f"({err or 'нет в реестре'})"]
    zones = [sc.canonical(p) for p in session.get("zones") or []]
    committed, uncommitted = sc.changed_paths(worktree,
                                              session.get("base_sha"))
    return [p for p in dict.fromkeys(committed + uncommitted)
            if not sc.zone_holds(zones, p)]


def shelve_attempt(worktree: Path, base_sha: str, label: str) -> str:
    """optional-стадия не принята после входа: ее изменения (и коммиты
    агента поверх base_sha) откладываются в git stash, а не остаются в
    дереве следующей стадии и не удаляются молча. Возвращает пометку."""
    head = _git_head(worktree)
    if base_sha and head and head != base_sha:
        reset = subprocess.run(["git", "-C", str(worktree), "reset", "-q",
                                "--soft", base_sha],
                               capture_output=True, text=True)
        if reset.returncode != 0:
            # Коммиты агента остались бы в ветке пакета без сверки зоны.
            raise RunnerStop(f"изменения попытки не отложены (git reset "
                             f"--soft {base_sha[:12]}): "
                             f"{reset.stderr.strip() or reset.stdout.strip()}")
    excludes = [f":(exclude){p}"
                for p in (*COMMIT_EXCLUDE, "AGENTS.pi.md")]
    proc = subprocess.run(
        ["git", "-C", str(worktree), "stash", "push", "-u", "-m", label,
         "--", ".", *excludes], capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return f"stash не создан: {out}"
    return "изменений нет" if "No local changes" in out else f"stash «{label}»"


def write_report(plan_path: Path, stage: dict, attempt: int,
                 stdout: str, stderr: str) -> Path:
    """Отчет агента для finish (указатель на файлы — хвост вывода pi)."""
    rdir = plan_path.with_name(plan_path.name + REPORTS_SUFFIX)
    rdir.mkdir(parents=True, exist_ok=True)
    path = rdir / f"{stage['id']}-{attempt}.md"
    path.write_text(
        f"# Отчет pi:{stage['payload']} — stage {stage['id']} "
        f"(попытка {attempt})\n\n## stdout\n\n```\n"
        f"{tail(stdout, STAGE_TAIL_LINES)}\n```\n\n## stderr\n\n```\n"
        f"{tail(stderr, STAGE_TAIL_LINES)}\n```\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- main


def cmd_run(plan_path: Path, dry_run: bool) -> int:
    try:
        manifest = parse_manifest(plan_path)
        global GATE_ENV_STRIP
        strip = manifest.get("gate_env_strip")
        GATE_ENV_STRIP = tuple(GATE_ENV_STRIP_DEFAULT if strip is None
                               else strip)  # [] — не срезать ничего
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

    def _skip(stage: dict, detail: str, exit_code: int | None = None,
              stdout: str = "", stderr: str = "") -> int:
        """Опциональная стадия упала (J62-остаток): НЕ STOP — статус
        `skipped`, WARNING в журнал плана и stderr, пакет едет дальше."""
        state_record(state, stage["id"], "skipped",
                     f"optional: {detail}", exit_code, stdout, stderr)
        try:
            state_write(spath, state)
            try:
                log_append(plan_path, stage, "SKIPPED", stdout, stderr)
            except OSError:
                pass
            journal_append(plan_path, f"{utcnow_iso()} | WARNING | stage "
                           f"{stage['id']} | {stage['kind']}:"
                           f"{stage['payload']} | SKIPPED (optional: "
                           f"{detail})")
        except OSError:
            pass  # план удален/readonly — диагностика важнее state
        print(f"RUNNER-WARNING: stage {stage['id']} помечен skipped "
              f"(optional: true) — {detail}; пакет едет дальше",
              file=sys.stderr)
        return 0

    def _fail(stage: dict, detail: str, exit_code: int | None,
              stdout: str = "", stderr: str = "") -> int:
        """Отказ стадии: optional → skipped+WARNING (не STOP), иначе STOP."""
        if stage.get("optional"):
            return _skip(stage, detail, exit_code, stdout, stderr)
        return _stop(stage, detail, exit_code, stdout, stderr)

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
            if rec.get("status") in ("done", "waited", "skipped")}
    # Env стенда stand-стадий пакета (J62): живет в state — resume докатывает
    # пакет с тем же env (stand-стадия не переисполняется, она done).
    stand_env: dict[str, str] = dict(state.get("stand_env") or {})

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
            if state.get("worktree"):
                # Ворота следующих pi-стадий читают факты из дерева пакета:
                # правки ПМ (утверждение требований и т.п.) — коммитом туда.
                print(f"  worktree пакета (правки ПМ — коммитом в ветку "
                      f"{state.get('branch')}): {state['worktree']}")
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
                rc, out, err = run_shell(stage["payload"], worktree, timeout,
                                         stand_env=stand_env)
            except subprocess.TimeoutExpired as exc:
                if _fail(stage, f"таймаут gate ({timeout}с)", None,
                         timeout_stream(exc.stdout),
                         timeout_stream(exc.stderr)):
                    return EXIT_STOP
                continue
            if rc != 0:
                # Воротный отказ НЕ ретраится — разбор ПМ (optional — skip).
                if _fail(stage, f"gate exit {rc}", rc, out, err):
                    return EXIT_STOP
                continue
            state_record(state, stage["id"], "done", "gate ok", 0, out, err)
            state_write(spath, state)
            try:
                log_append(plan_path, stage, "ok", out, err)
            except OSError:
                pass
            journal_append(plan_path, f"{utcnow_iso()} | stage "
                           f"{stage['id']} | gate:{stage['payload']} | ok")
            done.add(stage["id"])
            if stage["verdict"]:
                # gate-стадия не знает «коммит до ревью» — STALE не сверяется
                _, problem = check_verdict(worktree, stage["verdict"])
                if problem and _fail(
                        stage, f"{problem} — ЖДУ ПМ: возврат ревью", None):
                    return EXIT_STOP
            continue

        if stage["kind"] == "stand":
            # Подъем стенда (J62): stdout обязан отчитаться «export VAR=value»
            # (формат scripts/qa_stand_up.sh) — иначе STOP: без env стенда
            # последующие стадии гарантированно красные, молчаливый проход
            # только переносил бы отказ глубже.
            try:
                rc, out, err = run_shell(stage["payload"], worktree, timeout)
            except subprocess.TimeoutExpired as exc:
                return _stop(stage, f"таймаут stand ({timeout}с)", None,
                             timeout_stream(exc.stdout),
                             timeout_stream(exc.stderr))
            if rc != 0:
                return _stop(stage, f"stand exit {rc}", rc, out, err)
            parsed = parse_stand_env(out)
            if not parsed:
                return _stop(stage, "stand: в stdout нет export VAR=value "
                             "(стенд не отчитался env — формат "
                             "scripts/qa_stand_up.sh)", rc, out, err)
            stand_env.update(parsed)
            state["stand_env"] = dict(stand_env)
            exported = ", ".join(sorted(parsed))
            state_record(state, stage["id"], "done",
                         f"stand ok: {exported}", 0, out, err)
            state_write(spath, state)
            try:
                log_append(plan_path, stage, "ok", out, err)
            except OSError:
                pass
            journal_append(plan_path, f"{utcnow_iso()} | stage "
                           f"{stage['id']} | stand:{stage['payload']} | ok "
                           f"({exported})")
            print(f"RUNNER: stage {stage['id']} — стенд поднят, "
                  f"export: {exported}")
            print("RUNNER: гашение стенда НЕ выполняется автоматически — "
                  "сироту подберет reaper/gc (scripts/stand_reaper.py) "
                  "или погаси вручную")
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

        # Ворота входа (flowctl prepare/run или резервация зоны роли без
        # этапа). Повтор стадии после STOP сначала освобождает зону своей
        # прошлой попытки (worktree и файлы сохранены).
        gates = state.setdefault("flow_gates", {})
        prev_gate = gates.get(stage["id"])
        if prev_gate and prev_gate.get("open"):
            gate_abandon(prev_gate, "resume: повтор стадии после STOP")
            prev_gate["open"] = False
        attempt = (prev_gate or {}).get("attempt", 0) + 1
        try:
            gate = gate_new(stage, repo, worktree)
            gate.update(open=True, attempt=attempt)
            gates[stage["id"]] = gate
            state_write(spath, state)  # до prepare: резервация не осиротеет
            gate_begin(manifest, stage, repo, worktree, branch, gate)
        except GateDenied as exc:
            if gates.get(stage["id"]) is not None:
                gates[stage["id"]]["open"] = False
                state_write(spath, state)
            if _fail(stage, f"ворота входа: {exc}", None):
                return EXIT_STOP
            continue
        state_write(spath, state)
        print(f"RUNNER: stage {stage['id']} — ворота входа пройдены "
              f"({gate['kind']}: {gate.get('action') or gate['role']}, "
              f"correlation {gate['cid']})")

        def _reject(detail: str, exit_code: int | None = None,
                    stdout: str = "", stderr: str = "") -> int:
            """Отказ после входа: optional — изменения попытки откладываются
            в git stash, зона освобождается, skipped; обязательная — STOP,
            дерево и зона держатся до разбора ПМ/resume."""
            if stage.get("optional"):
                agents_file.unlink(missing_ok=True)  # файл раннера — не в stash
                try:
                    shelf = shelve_attempt(
                        worktree, gate.get("base_sha", ""),
                        f"package_runner: {manifest['package']}/{stage['id']} "
                        f"попытка {gate['attempt']} (optional skipped)")
                except RunnerStop as exc:
                    state_write(spath, state)
                    return _stop(stage, f"{detail}; {exc}", exit_code,
                                 stdout, stderr)
                gate["shelved"] = shelf
                gate_abandon(gate, f"optional skipped: {detail}")
                gate["open"] = False
                detail = f"{detail}; изменения попытки: {shelf}"
            elif _registry_status(Path(gate["registry"]),
                                  gate["delegation_id"]) == "running":
                _transition(
                    Path(gate["registry"]), gate["delegation_id"],
                    "needs_attention", f"package_runner: {detail}",
                    "STOP раннера", expect_from="running")
            state_write(spath, state)
            return _fail(stage, detail, exit_code, stdout, stderr)

        # Роль-пак → AGENTS.md в корень worktree (а если там уже есть
        # AGENTS.md проекта — рядом как AGENTS.pi.md, проектные правила не
        # затираются; дефект пилота BUG-026). Файл раннера убирается после
        # pi: в дифф/коммит стадии он не попадает.
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
        # Зоны — в префикс-форме guard'а: срез glob-хвостов (J61,
        # дефект пилота BUG-026 — guard матчит префиксом, не glob'ом).
        env["PI_ZONE_WRITE"] = ":".join(
            normalize_zone(z) for z in stage["zones"])
        # Env стенда stand-стадий пакета (J62) — всем последующим pi-стадиям.
        env.update(stand_env)
        args = ["pi", "--provider", provider, "--model", model,
                "--no-session", "-e", str(ZONE_GUARD),
                "-p", build_brief(manifest, stage, agents_file.name)]
        try:
            rc, out, err = run_pi(args, worktree, env, timeout)
        except subprocess.TimeoutExpired as exc:
            if _reject(f"таймаут pi ({timeout}с)", None,
                       timeout_stream(exc.stdout),
                       timeout_stream(exc.stderr)):
                return EXIT_STOP
            continue
        finally:
            agents_file.unlink(missing_ok=True)
        if rc != 0:
            if _reject(f"pi exit {rc}", rc, out, err):
                return EXIT_STOP
            continue
        # Опциональный gate-этап стадии (после успешного pi; отказ — STOP).
        if stage["gate"]:
            try:
                grc, gout, gerr = run_shell(stage["gate"], worktree, timeout)
            except subprocess.TimeoutExpired as exc:
                if _reject(f"таймаут gate stage ({timeout}с)", None,
                           timeout_stream(exc.stdout),
                           timeout_stream(exc.stderr)):
                    return EXIT_STOP
                continue
            if grc != 0:
                if _reject(f"gate stage exit {grc}", grc, gout, gerr):
                    return EXIT_STOP
                continue
            out += f"\n--- gate ---\n{gout}"
            err += f"\n--- gate ---\n{gerr}"
        # Ворота выхода: дифф ⊆ зона + гейты этапа, затем коммит стадии в
        # ветку пакета и закрытие резервации.
        try:
            report = write_report(plan_path, stage, attempt, out, err)
        except OSError as exc:
            if _reject(f"отчет стадии не записан: {exc}", None, out, err):
                return EXIT_STOP
            continue
        accepted, detail = gate_end(gate, worktree, report)
        if not accepted:
            if _reject(f"ворота выхода: {detail}", None, out, err):
                return EXIT_STOP
            continue
        outside = zone_recheck(gate, worktree)
        if outside:
            if _reject("перед коммитом: изменения вне зоны резервации: "
                       + ", ".join(outside), None, out, err):
                return EXIT_STOP
            continue
        try:
            commit = commit_stage(
                worktree, f"[pipeline] {manifest['package']}/{stage['id']}: "
                f"pi:{stage['payload']} (correlation {gate['cid']})")
        except RunnerStop as exc:
            if _reject(str(exc), None, out, err):
                return EXIT_STOP
            continue
        gate_close(gate, commit)
        gate.update(open=False, commit=commit)
        # Вердикт-ворота (J62 → J65.4) ДО отметки done: вердикт файла — по
        # единому парсеру; approve/return фиксируется аудит-записью provenance
        # (прерывание между ними не оставит done-стадию без записи);
        # return/STALE/нет вердикта — STOP.
        verdict_problem = None
        if stage["verdict"]:
            value, verdict_problem = check_verdict(
                worktree, stage["verdict"], gate["base_sha"])
            if value in ("approve", "return") and not (
                    verdict_problem and "STALE" in verdict_problem):
                try:
                    record = record_review_provenance(
                        plan_path, worktree, manifest, stage, state, gate,
                        value)
                except RunnerStop as exc:
                    return _stop(stage, str(exc), None)
                print(f"RUNNER: stage {stage['id']} — provenance ревью "
                      f"(аудит): {record}")
            if verdict_problem:
                state_write(spath, state)
                if _fail(stage, f"{verdict_problem} — ЖДУ ПМ: возврат ревью",
                         None):
                    return EXIT_STOP
                continue
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
    if any(s["kind"] == "stand" for s in manifest["stages"]):
        # Гашение стенда НЕ автоматом (J62): сироту подберет reaper/gc.
        print("RUNNER: стенд пакета оставлен работать — гашение НЕ автоматом "
              "(reaper/gc подберет сироту, scripts/stand_reaper.py), "
              "или погаси вручную (`bash scripts/qa_stand_up.sh stop` в worktree)")
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
