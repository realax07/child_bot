# -*- coding: utf-8 -*-
"""flow-enforcer — ядро pre_tool_call-энфорсера делегаций (J43, J43.1 flow-aware).

Перехватывает только tool_name == 'delegate_task'. Из args.goal/args.context
парсит маркеры конвейера:

  1. Заголовок (канонический формат J43.1, см. templates/task_delegation.md):
         flow: flow=<1-5> role=<роль> repo=<путь> scope=<идентификатор>
               [paths=<путь1,путь2,...>] [kind=docs|product]
     scope — по флоу: Ф1 change-id; Ф2 BUG-<КОМПЛЕКС>-NNN (репорт
     test-model/bugs/; BUG-001 и BUG-AF-RECHECK-001 оба валидны);
     Ф3 S<1|2|3>-<краткий-ид> — incident-id с severity-префиксом
     (J48/add-incident-protocol: severity-классификация обязательна до
     реакции, FR-1/FR-4); Ф4 chore-slug; Ф5 slug.
     paths — относительные пути задачи через запятую (ТОЛЬКО относительные,
     без '..'-сегментов — иначе FLOW_MARKER_INVALID; MAJ-2 ревью
     BUG-AF-RECHECK); попадают в flowctl prepare как --path (зоны/вороты
     видят РЕАЛЬНЫЕ пути задачи; BUG-AF-RECHECK-001: отсутствие путей ≠
     docs-only). kind — тип задачи: docs | product. kind=docs без путей
     валиден ТОЛЬКО для docs_task/chore_task (MAJ-1 ревью BUG-AF-RECHECK:
     для dev_task kind=docs без путей — отказ PATHS_MISSING, продуктовая
     задача не может быть docs по определению). kind не указан → WARNING и
     трактовка как product: без paths — block PATHS_MISSING (fail-closed,
     честный MISSING-класс).
  2. Старый формат (обратная совместимость): 'flow: role=R repo=P change=C'
     без flow= — трактуется как Флоу 1 (env-fallback), с WARNING в лог.
  3. Одиночные маркеры: 'role: X', 'repo: P', 'change:/scope: C' в тех же
     строках; task: — опциональный (dev_task уровня задачи).

Очевидные ошибки маркера (flow=9, пустой scope, Ф2 без формата
BUG-<КОМПЛЕКС>-NNN) → block FLOW_MARKER_INVALID в ЛЮБОМ режиме: битый
маркер хуже отсутствующего.

Без маркеров решение по режиму FLOW_ENFORCER_MODE:
  - 'strict'     → block MARKERS_MISSING;
  - 'permissive' (дефолт) → пропуск + запись в лог (аудит-след).

Bypass: дословная строка 'flow: bypass --by customer' в goal — ЗАЯВКА на
обход, но сама по себе ничего не разрешает (BUG-AF-RECHECK-002: goal
формирует оркестратор, цитата маркера в тексте тоже выглядит как маркер).
Обход действует ТОЛЬКО при действующей записи в реестре решений Заказчика
~/.hermes/state/flow-bypass-decisions.json (список записей
{repo, scope, action, decision_ref, expires}; expires — UTC ISO): запись
ищется по (repo, scope, action) и обязана быть неистекшей. Нет записи /
просрочена / другой scope или action → bypass-ветка НЕ срабатывает, идет
обычная проверка (как правило DENY). Ошибка чтения/разбора реестра — не
разрешение (fail-closed). Запись в bypass-лог дополняется decision_ref и
match (что именно совпало).

Валидация: subprocess flowctl prepare --dry-run --json (--dry-run, чтобы
энфорсер НЕ создавал reservation/zone-резерв; --json — чтобы вердикт парсился
машинно). Номер флоу — из маркера (flow=); env FLOW_ENFORCER_FLOW — ТОЛЬКО
fallback, когда в маркере нет flow= (с WARNING в лог). prepare вызывается с
--flow <N> и --change <scope>; роль отображается в действие СВОЕГО флоу
(ROLE_ACTIONS_FLOW: Ф2 dev→bug_fix, Ф3 — полная карта ролей этапов
STAGE_TABLE[3] включая post_mortem/archive_change, Ф4 →chore_task,
Ф5 →express_task/express_close).

Post-filter «no-change scope» (J43.1): для Флоу 2/4/5 отказ prepare, в котором
ВСЕ причины — ожидаемый класс «нет change-пакета / scope-id другого формата»,
а scope-файл существует (Ф2 test-model/bugs/<scope>.md; Ф4/Ф5 PLAN-<scope>.md
или <scope>.md в корне), трактуется как ALLOW с записью в лог
'flow-N no-change scope ok'. Выбран post-filter вердикта, а не отдельный
лайт-валидатор: лайт-валидатор дублировал бы логику ворот flow_transition
(два источника истины → расхождение), post-filter переиспользует ЕДИНУЮ
проверку prepare и сужает ровно один класс отказов, независимо подтверждая
существование scope-файла. Ошибки по существу (нет плана Ф2/Ф4, нет
BUG-репорта, нет спеки, WRONG_ROLE, ...) — block как раньше.

Память фазы и последовательность (J43.1, опционально): flowctl phase
возвращает последний run scope из flowctl_state.json. При
FLOW_ENFORCER_SEQUENCE=1 после prepare-ALLOW проверяется, что запрошенное
действие имеет выполненных обязательных предшественников среди завершенных
этапов scope (SEQUENCE_PREREQS — производная от STAGE_TABLE
flow_transition.py; синхронизируется тестом test_j431). Нарушение → block
SEQUENCE_VIOLATION (дефолт off; фаза недоступна → WARNING, не блок —

проверка best-effort поверх ворот prepare).

Fail-closed: любое исключение в хуке → block
'flow-enforcer internal error: ... (fail-closed)'. Плюс платформенный
fail-closed на таймаут (hooks.md §pre_tool_call).

Env:
  FLOW_ENFORCER_MODE       strict | permissive (дефолт permissive)
  FLOW_ENFORCER_STATE_DIR  каталог логов (дефолт ~/.hermes/state)
  AI_FACTORY_HOME          путь к фабрике (дефолт ~/ai-factory)
  FLOW_ENFORCER_REGISTRY   путь к active_sessions.json (проброс --registry;
                           рядом с ним ищется flowctl_state.json для phase)
  FLOW_ENFORCER_PROJECT    project-id для prepare/phase (дефолт: имя repo)
  FLOW_ENFORCER_FLOW       fallback-номер флоу, если в маркере нет flow=
  FLOW_ENFORCER_SEQUENCE   1 → проверка последовательности этапов (дефолт off)
  FLOW_BYPASS_REGISTRY     путь к реестру bypass-решений (дефолт
                           ~/.hermes/state/flow-bypass-decisions.json)
  FLOW_ENFORCER_PUSH       0/false/no → выключить push-политику J42 (L4)
  FLOW_MODEL_ENFORCE       0/false/no/off → выключить правило MODEL_ENFORCE
                           J60 (ДЕФОЛТ ON — решение Заказчика: механика
                           5.3-ролей включена; off — деградация при
                           недоступности Pi-инфраструктуры)

J60 (MODEL_ENFORCE): боевой этап 5.3-роли (SA/архитектор/DEV/BA — регруп
J59, AGENTS.md §Оркестрация 2.1) распознается ЭВРИСТИКОЙ по ключевым словам
роли в goal («СА», «create_change», «архитектор», «arch-review», «dev_task»,
«задача dev», «BA», «requirements»). Ни одно слово не совпало → не боевой
вызов, без директивы (свобода выбора исполнителя для Флоу 2/4/5,
devops/qa/docs-вызовов сохранена; goal с маркером flow=2|4|5 гасится
guard'ом явно). На боевом этапе goal обязан нести маркер исполнителя
[pi:<роль>] / [hermes:<роль>]:
  - [hermes:<роль5.3>] → DENY MODEL_ENFORCE с подсказкой «используй pi
    роль-пак skills/pm-factory/pi/role-packs/<роль> (J59); делегация —
    только с обоснованием в журнале PLAN.md»;
  - [pi:<роль>] → ALLOW (запись model_enforce_pass в лог);
  - маркера нет вообще → WARN в лог (НЕ блок: миграционный период).
Проверка стоит ДО разбора flow-маркеров и работает даже для делегаций без
канонического заголовка (в permissive те сегодня проходят молча).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TRIGGER_TOOL = "delegate_task"
BYPASS_MARKER = "flow: bypass --by customer"
FLOW_MARKER_INVALID = "FLOW_MARKER_INVALID"

# Канонический заголовок J43.1: 'flow: flow=N role=R repo=P scope=S'
# (порядок ключей не строгий; change= — легаси-алиас scope).
HEADER_LINE_RE = re.compile(r"^\s*flow:\s*(?P<rest>\S.*?)\s*$")
HEADER_PAIR_RE = re.compile(
    r"\b(flow|role|repo|scope|change|task|kind|paths)=([^\s]+)")
EMPTY_VALUE_RE = re.compile(r"\b(?:scope|change|paths)=(?:\s|$)")
MARKER_KINDS = ("docs", "product")
# MAJ-1 ревью BUG-AF-RECHECK: kind=docs валиден ТОЛЬКО для этих действий —
# документная/обслуживающая задача без путей; для dev_task (и любого другого
# продуктового действия) kind=docs без paths → PATHS_MISSING (иначе
# продуктовая dev-задача с kind=docs обходит arch-gate).
DOCS_KIND_ACTIONS = ("docs_task", "chore_task")
SINGLE_RE = {
    "role": re.compile(r"\brole:\s*([^\s,;]+)"),
    "repo": re.compile(r"\brepo:\s*([^\s,;]+)"),
    "change": re.compile(r"\bchange:\s*([^\s,;]+)"),
    "scope": re.compile(r"\bscope:\s*([^\s,;]+)"),
    "task": re.compile(r"\btask:\s*([^\s,;]+)"),  # опциональный (dev_task)
    "kind": re.compile(r"\bkind:\s*(docs|product)\b"),
    # paths: список через запятую — запятая НЕ разделитель маркера
    "paths": re.compile(r"\bpaths:\s*(\S+)"),
}

# Формат scope Флоу 2 (BUG-AF-RECHECK: синхронно с правилом имен репортов
# test-model/bugs/): единая грамматика — регекс-строка BUG_ID_PATTERN из
# scripts/flow_check.py (MIN-3 ревью BUG-AF-RECHECK: один источник истины;
# покрывает BUG-001, BUG-AF-RECHECK-001 и репорты с суффиксом-slug вида
# BUG-001-search-card-click.md; lowercase/кириллица/пробелы — отклонение,
# поэтому БЕЗ re.IGNORECASE). Фабрика недоступна → встроенная fallback-копия
# (синхронизируется тестом tests/test_recheck2_fixes.py).
def _bug_id_pattern() -> str:
    try:
        scripts_dir = str(factory_home() / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import flow_check
        return flow_check.BUG_ID_PATTERN
    except Exception:  # noqa: BLE001 — плагин не зависит от доступности фабрики
        return r"^BUG-(?:[A-Z0-9]+-)*\d{3}(?:-[a-z0-9][a-z0-9.-]*)?$"


BUG_ID_RE = re.compile(_bug_id_pattern())

# J48 (add-incident-protocol, D1/I-1): severity-allowlist Флоу 3 — единый
# источник истины flow_transition.SEVERITY_PREFIXES (расширение одной правкой);
# фабрика недоступна → встроенная fallback-копия (синхронизируется тестом).
def _severity_prefixes() -> tuple:
    try:
        scripts_dir = str(factory_home() / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import flow_transition
        return tuple(flow_transition.SEVERITY_PREFIXES)
    except Exception:  # noqa: BLE001 — плагин не зависит от доступности фабрики
        return ("S1", "S2", "S3")


SEVERITY_PREFIX_RE = re.compile(
    "^(?:" + "|".join(re.escape(p) for p in _severity_prefixes()) + ")-")
FLOW_IDS = (1, 2, 3, 4, 5)

# Действие по роли: этапы STAGE_TABLE Флоу 1 (flow_transition.py).
ROLE_ACTIONS = {
    "dev": "dev_task",
    "sa": "create_change",
    # J46: архитектор — актор architecture_review (синхронно с STAGE_TABLE:
    # этап после create_change, до dev_task).
    "architect": "architecture_review",
    "code_reviewer": "code_review",
    "qa_automation": "qa_automation",
    # remove-qa-checklist: роль/действие qa_checklist упразднены.
    "qa_author": "qa_cases",
    "qa_case_author": "qa_cases",
    "qa_case_reviewer": "qa_review",
    "qa_impact_analyst": "qa_impact_analysis",
    "design_validator": "design_validation",
    "ui_designer": "design_task",
}
DEFAULT_ACTION = "dev_task"

# J43.1: flow-aware отображение роли в действие (этапы STAGE_TABLE Флоу 2–5;
# не переопределенные роли берутся из ROLE_ACTIONS — code_review/merge и др.
# совпадают по флоу).
# MAJ-1 ревью 4.1: Флоу 3 — ПОЛНАЯ карта ролей по STAGE_TABLE[3] (все 6
# этапов воспроизводимы хотя бы одной ролью; тест TC-J48-020). Роль,
# входящая в несколько этапов Флоу 3, маппится в КЛЮЧЕВОЙ этап своего
# участия (single role→action): devops→post_mortem (не emergency_stabilize —
# тот остается за dev; deploy_rollback — за pm), sa/dev_lead→archive_change
# (merge_task остается за integrator), code_reviewer→accept_review (этапа
# code_review во Флоу 3 нет), pm→deploy_rollback, integrator→merge_task.
ROLE_ACTIONS_FLOW = {
    2: {"dev": "bug_fix"},
    3: {
        "dev": "emergency_stabilize",
        "devops": "post_mortem",
        "code_reviewer": "accept_review",
        "dev_lead": "archive_change",
        "integrator": "merge_task",
        "pm": "deploy_rollback",
        "sa": "archive_change",
    },
    4: {"dev": "chore_task"},
    5: {"dev": "express_task", "pm": "express_close"},
}


def action_for_role(role: str, flow: int) -> str:
    """Действие роли В СВОЕМ флоу (J43.1): dev во Флоу 2 — bug_fix, не dev_task."""
    return ROLE_ACTIONS_FLOW.get(flow, {}).get(
        role, ROLE_ACTIONS.get(role, DEFAULT_ACTION))


# Ожидаемый класс отказов «нет change-пакета» (J43.1): Флоу 2/4/5 живут БЕЗ
# change-пакета openspec/changes/<id>/, поэтому отказ prepare, в котором все
# причины — из этого класса, при существующем scope-файле трактуется как ALLOW.
NO_CHANGE_DETAIL_PATTERNS = (
    # тексты ворот Флоу 1 о отсутствующем/несозданном change-пакете
    "change-пакет openspec/changes/",
    # Ф4: scope=chore-slug легален маркеру, но не формату [chore] flow_state
    "scope не соответствует выбранному флоу",
)

# Последовательность этапов (J43.1, FLOW_ENFORCER_SEQUENCE=1): действие →
# обязательные предшественники среди ЗАВЕРШЕННЫХ этапов того же scope.
# Производная от STAGE_TABLE (scripts/flow_transition.py): каждый предшественник
# стоит в таблице флоу РАНЬШЕ действия — синхронизация проверяется тестом
# (tests/test_j431_flow_aware.py::test_sequence_prereqs_match_stage_table).
# Возврат на доработку легален: предшественники уже в completed_actions.
SEQUENCE_PREREQS: dict[int, dict[str, frozenset]] = {
    1: {
        "approve_requirements": frozenset(),
        "create_change": frozenset(),
        "architecture_review": frozenset({"create_change"}),
        "dev_task": frozenset({"create_change"}),
        "design_task": frozenset({"create_change"}),
        "code_review": frozenset({"dev_task"}),
        "accept_review": frozenset({"code_review"}),
        "merge_task": frozenset({"accept_review"}),
        # remove-qa-checklist: этап qa_checklist удален — qa_cases принимает
        # вход напрямую от merge_task (и design_validation, когда фаза была).
        "qa_cases": frozenset({"merge_task"}),
        "qa_review": frozenset({"qa_cases"}),
        "qa_impact_analysis": frozenset({"merge_task"}),
        "qa_automation": frozenset({"qa_cases"}),
        "design_validation": frozenset({"merge_task"}),
        "archive_change": frozenset({"merge_task"}),
        "release": frozenset({"merge_task"}),
    },
    2: {
        "bug_fix": frozenset(),
        "code_review": frozenset({"bug_fix"}),
        "accept_review": frozenset({"code_review"}),
        "merge_task": frozenset({"accept_review"}),
    },
    3: {
        "emergency_stabilize": frozenset(),
        "accept_review": frozenset({"emergency_stabilize"}),
        "merge_task": frozenset({"accept_review"}),
        "deploy_rollback": frozenset({"merge_task"}),
        # J48 (D3): этапы post_mortem/archive_change — производные STAGE_TABLE[3]
        # (flow_transition.py); архивация без пройденного post_mortem
        # недостижима по порядку этапов.
        "post_mortem": frozenset({"deploy_rollback"}),
        "archive_change": frozenset({"post_mortem"}),
    },
    4: {
        "chore_task": frozenset(),
        "code_review": frozenset({"chore_task"}),
        "accept_review": frozenset({"code_review"}),
        "merge_task": frozenset({"accept_review"}),
    },
    5: {
        "express_task": frozenset(),
        "express_close": frozenset({"express_task"}),
    },
}
# finished_status, означающий, что этап СОСТОЯЛСЯ (зеркало
# flowctl.PHASE_COMPLETED_STATUSES; returned = завершен возвратом на доработку).
COMPLETED_STATUSES = ("accepted", "returned")

# --------------------------------------------- worktree-per-delegation (J51.1)
# Две активные делегации одного repo обязаны работать в РАЗНЫХ worktree
# (фактура J51: параллельные dev/QA в общем дереве — транзиентные фейлы
# тестов на чужих git-операциях). Проверка по реестру сессий
# (active_sessions.json): другая активная резервация того же repo без
# выделенного worktree — либо несколько активных в одном worktree → block
# WORKTREE_MISSING. СВОЙ scope-повтор не отличим в реестре (scope там не
# хранится), но повторная делегация того же repo с имеющимся worktree
# проходит (конфликт только «без worktree»/«общий worktree»).
WORKTREE_MISSING = "WORKTREE_MISSING"
# Активные статусы резервации (session_check): finished — сессия закрыта.
WORKTREE_ACTIVE_STATUSES = ("reserved", "running", "needs_attention")


def worktree_registry_path() -> Path:
    registry = os.environ.get("FLOW_ENFORCER_REGISTRY")
    if registry:
        return Path(registry)
    return state_dir() / "active_sessions.json"


def _norm_repo_path(p: str) -> str:
    try:
        return str(Path(p).expanduser().resolve())
    except OSError:
        return str(p)


def worktree_conflict(markers: dict) -> str | None:
    """Текст нарушения worktree-per-delegation (J51.1) или None. Реестр
    нечитаем → None + WARNING: проверка best-effort поверх ворот prepare
    (fail-open ТОЛЬКО у этой вспомогательной проверки, сами ворота
    fail-closed)."""
    try:
        data = json.loads(
            worktree_registry_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None  # реестра еще нет — норма, без спама в логе
    except (OSError, ValueError) as exc:
        log_event({"event": "worktree_gate_unavailable", "level": "WARNING",
                   "detail": str(exc)})
        return None
    if not isinstance(data, dict):
        return None
    repo = _norm_repo_path(markers["repo"])
    others = [s for s in data.get("sessions", [])
              if isinstance(s, dict)
              and s.get("status") in WORKTREE_ACTIVE_STATUSES
              and _norm_repo_path(str(s.get("repo") or "")) == repo]
    if not others:
        return None
    without_wt = [s for s in others if not s.get("worktree")]
    if without_wt:
        ids = ", ".join(str(s.get("delegation_id", "?"))
                        for s in without_wt)
        return (f"активные делегации того же repo без выделенного worktree: "
                f"{ids}; параллельная делегация обязана работать в СВОЕМ "
                "worktree — flowctl prepare --create-worktree (J51.1)")
    by_wt: dict[str, list[str]] = {}
    for s in others:
        by_wt.setdefault(str(s.get("worktree")), []).append(
            str(s.get("delegation_id", "?")))
    shared = {wt: ids for wt, ids in by_wt.items() if len(ids) > 1}
    if shared:
        detail = "; ".join(f"{', '.join(ids)} → {wt}"
                           for wt, ids in sorted(shared.items()))
        return (f"несколько активных делегаций в одном worktree: {detail}; "
                "каждой параллельной делегации — отдельный worktree (J51.1)")
    return None


def worktree_gate(markers: dict, task_id: str) -> dict | None:
    """Ворота worktree-per-delegation (J51.1, включены по умолчанию;
    FLOW_ENFORCER_WORKTREE=0 — выключить): после prepare-ALLOW — нет ли
    другой активной делегации того же repo без выделенного/различного
    worktree. Нарушение → block WORKTREE_MISSING."""
    flag = os.environ.get("FLOW_ENFORCER_WORKTREE", "1").strip().lower()
    if flag in ("0", "false", "no"):
        return None
    conflict = worktree_conflict(markers)
    if conflict:
        log_event({"event": "worktree_missing", "level": "WARNING",
                   "task_id": task_id, "repo": markers["repo"],
                   "scope": markers["scope"]})
        return {"action": "block", "message": (
            f"FLOW-ENFORCER [{WORKTREE_MISSING}]: параллельные делегации "
            f"одного repo без разных worktree: {conflict}. Создай делегации "
            "через flowctl prepare --create-worktree (каждой — свое дерево; "
            "деградация «без worktree» допустима только для одиночной "
            "делегации).")}
    return None

SLOW_PREPARE_SECONDS = 10.0


# --------------------------------------------- push-политика (J42, слой L4)
# Сабагент в активной делегации пушит ТОЛЬКО ветку из ALLOW-объекта делегации
# (prepare: session['branch']; допустимые формы — 'origin <branch>' /
# 'HEAD:<branch>' / 'refs/heads/<branch>') и открывает PR; push main, чужой
# ветки, тегов, удаление и массовые операции — DENY. Вне делегации (ПМ) —
# не блокировать. Деградация: активная делегация есть, но branch в
# ALLOW-объекте нет — прежнее поведение (пропуск + WARNING в лог).
# Контекст делегации определяется по cwd команды Bash flow-aware (как J43.1):
# делегация с выделенным worktree владеет только своим деревом (cwd в
# worktree); делегация без worktree работает в дереве repo (cwd в repo) —
# push ПМ из главного дерева repo при активной worktree-делегации НЕ
# блокируется. cwd неизвестен → контекстом считаются все активные делегации
# (консервативно: реестр — единственный источник факта делегации).
PUSH_POLICY = "PUSH_POLICY"
PUSH_TOOLS = ("bash",)
PUSH_DISABLE_ENV = "FLOW_ENFORCER_PUSH"  # 0/false/no — выключить (best-effort)
PUSH_DELEGATION_MSG = (
    "Сабагент пушит только свою ветку pipeline/** и открывает PR; "
    "main и чужие зоны запрещены (ruleset+enforcer). Допустимая форма: "
    "git push origin <ALLOW-ветка> или git push origin HEAD:<ALLOW-ветка>.")

# Опции push, принимающие отдельное значение (следующий токен — не refspec).
PUSH_VALUE_OPTS = frozenset({
    "-o", "--push-option", "--receive-pack", "--exec", "--repo",
    "--recurse-submodules", "--server-option",
})
# Опции/формы, которые сами по себе нарушают политику: удаление refs,
# массовые операции (все ветки/теги/зеркало).
PUSH_DENY_OPTS = ("-d", "--delete", "--tags", "--all", "--mirror")


def _shell_segments(command: str) -> list[str]:
    """Сегменты составной команды (&&, ||, ;, |, перевод строки)."""
    return [s for s in re.split(r"&&|\|\||;|\||\n", command or "") if s.strip()]


def parse_git_push(segment: str) -> dict | None:
    """Разбор сегмента команды как 'git [opts] push [opts] [remote] [refspec]'.
    None — сегмент не git push. Иначе словарь:
      {"repo": путь из `git -C`, "dst": ветка-назначение | None,
       "reason": текст нарушения | None, "dry_run": bool}
    dst нормализован (префикс refs/heads/ снят); reason имеет приоритет."""
    tokens = segment.split()
    if not tokens or tokens[0] != "git":
        return None
    i, repo = 1, None
    while i < len(tokens) and tokens[i] != "push":
        if tokens[i] == "-C" and i + 1 < len(tokens):
            repo = tokens[i + 1]
            i += 2
        else:
            i += 1
    if i >= len(tokens) or tokens[i] != "push":
        return None
    i += 1
    dry_run, positionals = False, []
    while i < len(tokens):
        token = tokens[i]
        if token in PUSH_VALUE_OPTS:
            i += 2  # опция и её значение
            continue
        if token in PUSH_DENY_OPTS:
            return {"repo": repo, "dst": None, "dry_run": False,
                    "reason": (f"опция {token} (удаление refs / массовая "
                               "операция) запрещена политикой")}
        if token in ("-n", "--dry-run"):
            dry_run = True
            i += 1
            continue
        if token.startswith("-"):
            i += 1  # прочие флаги (-u, --force, ...) политике не равны
            continue
        positionals.append(token)
        i += 1
    if dry_run:
        return {"repo": repo, "dst": None, "dry_run": True, "reason": None}
    if not positionals:
        return {"repo": repo, "dst": None, "dry_run": False,
                "reason": ("нет явного refspec — укажи "
                           "'git push origin <ветка>' явно")}
    if len(positionals) == 1:
        return {"repo": repo, "dst": None, "dry_run": False,
                "reason": ("указан только remote без refspec — укажи "
                           "'git push origin <ветка>' явно")}
    if len(positionals) > 2:
        return {"repo": repo, "dst": None, "dry_run": False,
                "reason": "несколько refspec за один push — запрещено"}
    refspec = positionals[1].lstrip("+")
    if ":" in refspec:
        src, dst = refspec.split(":", 1)
        if src == "":
            return {"repo": repo, "dst": None, "dry_run": False,
                    "reason": f"удаление remote-ref {dst!r} запрещено"}
    else:
        dst = refspec
    if dst.startswith("refs/tags/") or dst.startswith("tag "):
        return {"repo": repo, "dst": None, "dry_run": False,
                "reason": f"push тегов ({dst}) запрещен — только ветка из ALLOW"}
    if dst == "HEAD":
        return {"repo": repo, "dst": None, "dry_run": False,
                "reason": "голый HEAD не проверяем — укажи HEAD:<ветка>"}
    if dst.startswith("refs/heads/"):
        dst = dst[len("refs/heads/"):]
    return {"repo": repo, "dst": dst, "dry_run": False, "reason": None}


def active_push_context(cwd: str | None) -> tuple[list[str], list[str]]:
    """(ALLOW-ветки, delegation_id) активных делегаций в контексте cwd
    (см. комментарий блока J42). Реестр нечитаем → ([], []) — вне делегации
    (проверка best-effort, сами ворота prepare fail-closed)."""
    try:
        data = json.loads(
            worktree_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    if not isinstance(data, dict):
        return [], []

    def _in_context(session: dict) -> bool:
        if cwd is None:
            return True
        c = _norm_repo_path(cwd)
        base = session.get("worktree") or session.get("repo")
        if not base:
            return False
        b = _norm_repo_path(str(base))
        return c == b or c.startswith(b.rstrip("/") + "/")

    branches: list[str] = []
    ids: list[str] = []
    for s in data.get("sessions", []):
        if not isinstance(s, dict):
            continue
        if s.get("status") not in WORKTREE_ACTIVE_STATUSES:
            continue
        if not _in_context(s):
            continue
        ids.append(str(s.get("delegation_id", "?")))
        if s.get("branch"):
            branches.append(str(s["branch"]))
    return branches, ids


def push_gate(args: dict, task_id: str = "") -> dict | None:
    """Ворота push-политики J42 (L4) для Bash-вызовов git push в контексте
    активной делегации: разрешен только push в ветку из ALLOW-объекта
    (session['branch']). Вне делегации / FLOW_ENFORCER_PUSH=0 / нет
    ALLOW-ветки — прежнее поведение (пропуск)."""
    flag = os.environ.get(PUSH_DISABLE_ENV, "1").strip().lower()
    if flag in ("0", "false", "no"):
        return None
    command = args.get("command") or args.get("cmd") or ""
    if not isinstance(command, str) or "push" not in command:
        return None
    pushes = [p for p in (parse_git_push(seg)
                          for seg in _shell_segments(command))
              if p is not None]
    if not pushes:
        return None
    cwd = None
    for key in ("cwd", "workdir", "working_dir"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            cwd = v
            break
    for p in pushes:
        if cwd is None and p["repo"]:
            cwd = p["repo"]
    branches, ids = active_push_context(cwd)
    if not ids:
        # Вне делегации — контекст ПМ (J57): в продуктовом репо push
        # требует интеграции (маркер в HEAD или merged), фабрика исключена.
        return pm_integration_gate(pushes, cwd, task_id)
    if not branches:
        log_event({"event": "push_policy_no_allow", "level": "WARNING",
                   "task_id": task_id, "delegations": ids,
                   "note": "активная делегация без branch в ALLOW-объекте — "
                           "push-политика J42 не применяется (деградация)"})
        return None
    allowed = ", ".join(sorted(set(branches)))
    for p in pushes:
        if p["reason"] is None and p["dst"] is not None and p["dst"] in branches:
            continue  # эта команда пуша легальна
        reason = p["reason"] or (
            f"push в {p['dst']!r}" if p["dst"] is not None
            else "refspec не проверяем")
        log_event({"event": "push_denied", "level": "WARNING",
                   "task_id": task_id, "reason": reason,
                   "allowed": branches, "delegations": ids})
        return {"action": "block", "message": (
            f"FLOW-ENFORCER [{PUSH_POLICY}]: push-политика J42: разрешена "
            f"только ветка {allowed} из ALLOW (делегация: {', '.join(ids)}); "
            f"нарушение: {reason}. {PUSH_DELEGATION_MSG}")}
    log_event({"event": "push_allowed", "task_id": task_id,
               "allowed": branches, "delegations": ids})
    return None


# --------------------------------------------------- J57: pm_bounds → push_gate
# ПМ-push в продуктовый репо (вне делегации) требует интеграции: маркер
# [integrate]/[pipeline] в subject HEAD (сборка, переданная dev-lead'у) ИЛИ
# HEAD уже влит в origin/main (merged PR). Фабрика — КОНВЕЙЕРНОЕ репо: пуш
# docs-заявок/бэклога ПМ туда не ломаем (правило только для продуктовых).
PM_PUSH_POLICY = "PM_PUSH_POLICY"
PRODUCT_REPOS_ENV = "FLOW_ENFORCER_PRODUCT_REPOS"
INTEGRATE_MARKERS = ("[integrate]", "[pipeline]")
CONVEYOR_PROJECT = "ai-factory"
PM_PUSH_HANDOFF_MSG = (
    "ПМ git продукта не трогает (J9) — передай сборку dev-lead'у "
    "(flowctl integrate)")


def product_repo_paths() -> list[str]:
    """Продуктовые репо: env-список FLOW_ENFORCER_PRODUCT_REPOS (имена
    проектов/пути через запятую) или — деривация из реестра сессий, как
    pm_bounds_check.load_pm_projects (project != 'ai-factory' с существующим
    ~/project/.git). Фабрика исключена всегда (конвейерное репо)."""
    raw = os.environ.get(PRODUCT_REPOS_ENV, "").strip()
    if raw:
        out = []
        for part in (x.strip() for x in raw.split(",") if x.strip()):
            cand = Path(part).expanduser()
            if not cand.is_absolute():
                cand = Path.home() / part
            out.append(str(cand))
        return out
    try:
        data = json.loads(
            worktree_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    repos: list[str] = []
    for s in (data.get("sessions", []) if isinstance(data, dict) else []):
        if not isinstance(s, dict):
            continue
        project = str(s.get("project") or "")
        if not project or project == CONVEYOR_PROJECT:
            continue
        cand = Path.home() / project
        if (cand.is_dir() and (cand / ".git").exists()
                and str(cand) not in repos):
            repos.append(str(cand))
    return repos


def _repo_is_product(repo_str: str | None) -> bool:
    """repo (cwd/`git -C`) — продуктовый? Фабрика (factory_home) — нет."""
    if not repo_str:
        return False
    r = _norm_repo_path(repo_str)
    factory = _norm_repo_path(str(factory_home()))
    if r == factory or r.startswith(factory.rstrip("/") + "/"):
        return False
    for cand in product_repo_paths():
        c = _norm_repo_path(cand)
        if r == c or r.startswith(c.rstrip("/") + "/"):
            return True
    return False


def _head_integrated(repo: Path) -> tuple[bool, str]:
    """(интегрирован, subject): True — маркер [integrate]/[pipeline] в
    subject HEAD ИЛИ HEAD — предок origin/main, т.е. контент уже влит
    (merged PR). Сверка ТОЛЬКО с remote-tracking origin/main: локальный
    main — это сам HEAD при пуше main (проверка вырождалась бы в always-true).
    git недоступен → (False, '') — fail-closed."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", "-s", "--format=%s", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    subject = proc.stdout.strip() if proc.returncode == 0 else ""
    if any(m in subject for m in INTEGRATE_MARKERS):
        return True, subject
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor",
             "HEAD", "origin/main"],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False, subject
    return (proc.returncode == 0, subject)


def pm_integration_gate(pushes: list[dict], cwd: str | None,
                        task_id: str = "") -> dict | None:
    """J57 (pm_bounds_check → push_gate): ПМ-push (вне делегации) в
    продуктовый репо без интеграции → DENY [PM_PUSH_POLICY] с подсказкой
    «передай сборку dev-lead'у». Пуш в фабрику и продуктовые пушы с
    маркером/merged — прежнее поведение (пропуск)."""
    offenders: list[str] = []
    for p in pushes:
        if p.get("dry_run"):
            continue
        repo_str = p["repo"] or cwd
        if not repo_str or not _repo_is_product(repo_str):
            continue
        integrated, subject = _head_integrated(Path(repo_str).expanduser())
        if integrated:
            log_event({"event": "pm_push_allowed", "task_id": task_id,
                       "repo": repo_str, "head_subject": subject[:120],
                       "note": "маркер интеграции в HEAD или merged в "
                               "origin/main"})
            continue
        offenders.append(str(repo_str))
    if not offenders:
        return None
    log_event({"event": "pm_push_denied", "level": "WARNING",
               "task_id": task_id, "repos": offenders,
               "policy": PM_PUSH_POLICY})
    return {"action": "block", "message": (
        f"FLOW-ENFORCER [{PM_PUSH_POLICY}]: ПМ-push в продуктовый репо "
        f"({', '.join(sorted(set(offenders)))}) без интеграции: маркера "
        f"[integrate]/[pipeline] в HEAD нет, HEAD не влит в origin/main. "
        f"{PM_PUSH_HANDOFF_MSG}.")}


# ------------------------------------------------------- J60: MODEL_ENFORCE
# Модели-регруп (J59, решение Заказчика 2026-10-31): боевые волны
# SA/архитектор/DEV/BA исполняются через Pi роль-паки (glm-5.3), не через
# general-делегацию. Правило — AGENTS.md §Оркестрация 2.1; здесь —
# механический энфорсер (J60).
MODEL_ENFORCE_ENV = "FLOW_MODEL_ENFORCE"
MODEL_ENFORCE_POLICY = "MODEL_ENFORCE"
MODEL_ROLES = ("sa", "architect", "dev", "ba")
# Маркер исполнителя в goal: [pi:sa] / [hermes:dev] / ... (регистр не важен).
EXECUTOR_MARKER_RE = re.compile(
    r"\[(pi|hermes):(" + "|".join(MODEL_ROLES) + r")\]", re.IGNORECASE)
# Эвристика боевого этапа: ключевые слова роли 5.3 в goal (ТЗ J60). «СА» и
# «BA» — ТОЛЬКО верхним регистром целым словом (иначе «Са»/латиница в
# обычном тексте дают ложное срабатывание). Флоу 1-воины: create_change /
# arch-review / dev_task; «requirements» — этап BA.
STAGE_KEYWORD_RE = {
    "sa": re.compile(r"\bСА\b|\bcreate_change\b"),
    "architect": re.compile(
        r"\bархитектор\w*|\barch[-_ ]?review\b", re.IGNORECASE),
    "dev": re.compile(
        r"\bdev_task\b|\bзадач\w+\s+dev\b|\bdev-задач\w*", re.IGNORECASE),
    "ba": re.compile(r"\bBA\b|\brequirements\b"),
}
# Свобода выбора исполнителя (ТЗ J60): Флоу 2/4/5 — НЕ боевые волны 5.3;
# явный маркер flow=2|4|5 в goal гасит правило (guard), до эвристики.
MODEL_FLOW_GUARD_RE = re.compile(r"\bflow=[245]\b")


def model_enforce_enabled() -> bool:
    """Флаг FLOW_MODEL_ENFORCE: ДЕФОЛТ ON (решение Заказчика — механика
    включена); 0/false/no — полное отключение (деградация при недоступности
    Pi-инфраструктуры)."""
    flag = os.environ.get(MODEL_ENFORCE_ENV, "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


def executor_markers(goal: str | None) -> list[tuple[str, str]]:
    """Маркеры исполнителя [(kind, role)] в goal: pi | hermes × 5.3-роль."""
    return [(m.group(1).lower(), m.group(2).lower())
            for m in EXECUTOR_MARKER_RE.finditer(goal or "")]


def stage_roles(goal: str | None) -> list[str]:
    """5.3-роли, чьи ключевые слова найдены в goal (эвристика боевого
    этапа). Пусто → не боевой вызов, правило не применяется."""
    return [role for role, rx in STAGE_KEYWORD_RE.items()
            if rx.search(goal or "")]


def model_enforce_gate(goal: str | None, task_id: str = "") -> dict | None:
    """Ворота MODEL_ENFORCE (J60) для delegate_task: на боевом этапе
    5.3-роли goal обязан нести маркер исполнителя. [hermes:<роль>] → block
    (general-делегация запрещена J59); [pi:<роль>] → пропуск; маркера нет →
    WARN в лог (НЕ блок — миграционный период). Не боевой вызов / Флоу
    2/4/5 / флаг off — без директивы."""
    if not model_enforce_enabled():
        return None
    if MODEL_FLOW_GUARD_RE.search(goal or ""):
        return None  # Флоу 2/4/5: свобода выбора исполнителя сохранена
    stages = stage_roles(goal)
    if not stages:
        return None  # не боевой вызов (devops/qa/docs/Ф2/Ф4/Ф5) — молча
    markers = executor_markers(goal)
    if not markers:
        log_event({"event": "model_enforce_warn", "level": "WARNING",
                   "task_id": task_id, "stages": stages,
                   "note": "боевой этап 5.3-роли без маркера исполнителя "
                           "([pi:<роль>]/[hermes:<роль>]) — миграционный "
                           "WARN (J60), делегация не блокирована"})
        return None
    hermes = [role for kind, role in markers if kind == "hermes"]
    if hermes:
        role = hermes[0]
        log_event({"event": "model_enforce_denied", "level": "WARNING",
                   "task_id": task_id, "role": role, "stages": stages})
        return {"action": "block", "message": (
            f"FLOW-ENFORCER [{MODEL_ENFORCE_POLICY}]: боевой этап "
            f"5.3-роли ({', '.join(stages)}) с маркером [hermes:{role}] — "
            "general-делегация запрещена (J59, AGENTS.md §Оркестрация 2.1): "
            f"используй pi роль-пак skills/pm-factory/pi/role-packs/{role} "
            "(J59); делегация — только с обоснованием в журнале PLAN.md. "
            f"Отключение механики: {MODEL_ENFORCE_ENV}=off (деградация).")}
    log_event({"event": "model_enforce_pass", "task_id": task_id,
               "pi_roles": sorted({role for kind, role in markers
                                   if kind == "pi"}),
               "stages": stages})
    return None


class FlowMarkerError(ValueError):
    """Очевидная ошибка маркера → block FLOW_MARKER_INVALID (J43.1)."""


def state_dir() -> Path:
    return Path(os.environ.get(
        "FLOW_ENFORCER_STATE_DIR", str(Path.home() / ".hermes" / "state")))


def factory_home() -> Path:
    return Path(os.environ.get(
        "AI_FACTORY_HOME", str(Path.home() / "ai-factory"))).expanduser()


def mode() -> str:
    return os.environ.get("FLOW_ENFORCER_MODE", "permissive").strip().lower()


def _marker_texts(args: dict) -> list[str]:
    """Строки, в которых ищутся маркеры: goal + context (args delegate_task)."""
    texts = []
    for key in ("goal", "context"):
        v = (args or {}).get(key)
        if isinstance(v, str) and v.strip():
            texts.append(v)
        elif isinstance(v, dict):
            texts.extend(str(x) for x in v.values() if isinstance(x, str))
    return texts


def _parse_header(texts: list[str]) -> dict | None:
    """Канонический/легаси заголовок 'flow: k=v ...' → пары или None."""
    for text in texts:
        for line in text.splitlines():
            m = HEADER_LINE_RE.match(line)
            if not m:
                continue
            rest = m.group("rest")
            if rest.startswith("bypass"):
                continue  # bypass-маркер обрабатывается до разбора
            if EMPTY_VALUE_RE.search(rest):
                which = "paths" if re.search(r"\bpaths=", rest) else "scope"
                if which == "paths":
                    raise FlowMarkerError(
                        "пустой paths в заголовке flow: — перечисли "
                        "относительные пути через запятую (paths=src/x.py,"
                        "backend/y.py) или убери поле (kind=docs — пути не "
                        "обязательны)")
                raise FlowMarkerError(
                    "пустой scope в заголовке flow: — scope обязателен "
                    "(Ф1 change-id, Ф2 BUG-<КОМПЛЕКС>-NNN, Ф3 incident-id, "
                    "Ф4 chore-slug, Ф5 slug; см. templates/task_delegation.md)")
            pairs = dict(HEADER_PAIR_RE.findall(rest))
            if pairs.get("role") and pairs.get("repo") and (
                pairs.get("scope") or pairs.get("change")
            ):
                return pairs
    return None


def parse_markers(args: dict) -> dict | None:
    """Достает role/repo/scope/flow/kind/paths: сначала заголовок 'flow: ...',
    затем одиночные маркеры 'role:/repo:/change:/scope:/kind:/paths:'.
    flow — ТОЛЬКО из заголовка (flow=N); None — если в маркере нет flow=
    (энфорсер возьмет env-fallback с WARNING). paths — список относительных
    путей (запятая-разделитель; BUG-AF-RECHECK-001). Очевидные ошибки (flow
    вне 1–5, пустой scope, неизвестный kind, пустой paths) → FlowMarkerError.
    Возвращает None, если обязательных маркеров нет."""
    texts = _marker_texts(args)
    header = _parse_header(texts)
    found: dict = {"role": None, "repo": None, "scope": None,
                   "task": None, "flow": None, "kind": None, "paths": None}
    if header:
        found["role"] = header.get("role")
        found["repo"] = header.get("repo")
        found["scope"] = header.get("scope") or header.get("change")
        found["task"] = header.get("task")
        found["kind"] = header.get("kind")
        found["paths"] = header.get("paths")
        raw_flow = header.get("flow")
        if raw_flow is not None:
            found["flow"] = _validate_flow(raw_flow)
    for text in texts:
        for key, rx in SINGLE_RE.items():
            if not found.get(key):
                m = rx.search(text)
                if m:
                    found[key] = m.group(1).strip("<>")
        # MIN-2 ревью BUG-AF-RECHECK: НЕ прерываем цикл, пока не собраны
        # ВСЕ маркеры — kind/paths могут жить в другом тексте (context), чем
        # role/repo/scope; ранний выход обрывал их поиск.
        if all(found.get(k)
               for k in ("role", "repo", "scope", "task", "kind", "paths")):
            break
    role, repo, scope = found["role"], found["repo"], found["scope"]
    if role and repo and scope:
        return {"role": role, "repo": repo, "scope": scope,
                "task": found["task"], "flow": found["flow"],
                "kind": _validate_kind(found["kind"]),
                "paths": _split_paths(found["paths"])}
    return None


def _validate_kind(kind: str | None) -> str | None:
    """kind маркера (BUG-AF-RECHECK-001): docs | product; неизвестный —
    очевидная ошибка маркера (FLOW_MARKER_INVALID)."""
    if kind is None:
        return None
    if kind not in MARKER_KINDS:
        raise FlowMarkerError(
            f"kind={kind!r} не поддерживается — ожидается kind=docs|product "
            "(docs — облегченный маршрут без путей; product — пути "
            "обязательны)")
    return kind


def _validate_rel_path(p: str) -> str:
    """MAJ-2 ревью BUG-AF-RECHECK: --path принимает ТОЛЬКО относительный путь
    без подъема выше корня. Абсолютный путь или '..'-сегмент — очевидная
    ошибка маркера (fail-closed на границе энфорсера, до ворот prepare)."""
    norm = p.replace("\\", "/")
    if norm.startswith("/") or (len(norm) >= 2 and norm[1] == ":"):
        raise FlowMarkerError(
            f"paths={p!r} — абсолютный путь запрещен: --path ожидает путь "
            "ОТНОСИТЕЛЬНО корня репозитория (например backend/app/x.py)")
    if ".." in norm.split("/"):
        raise FlowMarkerError(
            f"paths={p!r} — сегмент '..' запрещен (подъем выше корня "
            "репозитория; укажи путь от корня, например backend/app/x.py)")
    return p


def _split_paths(raw: str | None) -> list[str]:
    """paths=<путь1,путь2,...> → список относительных путей (без пустых).
    Каждый путь валидируется (_validate_rel_path): абсолютные и '..'-пути —
    FlowMarkerError (MAJ-2 ревью BUG-AF-RECHECK)."""
    if not raw:
        return []
    return [_validate_rel_path(p) for p in
            (s.strip() for s in raw.split(",")) if p]


def _validate_flow(raw_flow: str) -> int:
    try:
        flow = int(str(raw_flow).strip())
    except ValueError:
        raise FlowMarkerError(
            f"flow={raw_flow!r} не число — ожидается flow=<1-5> (J43.1)")
    if flow not in FLOW_IDS:
        raise FlowMarkerError(
            f"flow={raw_flow} вне 1–5 — ожидается flow=<1-5> (J43.1)")
    return flow


def validate_scope_for_flow(flow: int, scope: str) -> None:
    """Формат scope своего флоу (J43.1): Ф2 — BUG-<КОМПЛЕКС>-NNN (формат
    репорта test-model/bugs/, расширен 5c21489: BUG-001 и BUG-AF-RECHECK-001
    оба валидны); Ф3 S<1|2|3>-<краткий-ид> (J48: severity-классификация
    инцидента обязательна до реакции, FR-1/FR-4; невалидный формат —
    FLOW_MARKER_INVALID); Ф1 change-id / Ф4 chore-slug /
    Ф5 slug — непустая строка (пустой отсечен при разборе)."""
    if not scope:
        raise FlowMarkerError(
            "пустой scope — scope обязателен (Ф1 change-id, "
            "Ф2 BUG-<КОМПЛЕКС>-NNN, Ф3 S<1|2|3>-<краткий-ид>, Ф4 chore-slug, "
            "Ф5 slug)")
    if flow == 2 and not BUG_ID_RE.match(scope):
        raise FlowMarkerError(
            f"Флоу 2 требует scope формата BUG-<КОМПЛЕКС>-NNN "
            f"(BUG-001 или BUG-AF-RECHECK-001; A-Z0-9, дефис-разделитель), "
            f"получено {scope!r} (репорт test-model/bugs/<scope>.md)")
    if flow == 3 and not SEVERITY_PREFIX_RE.match(scope):
        # J48 (D1/FR-4): severity внутри incident_ref — маркер обязан нести
        # классификацию; неклассифицированный инцидент не запускает реакцию
        # (очевидная ошибка маркера → FLOW_MARKER_INVALID).
        raise FlowMarkerError(
            f"Флоу 3 требует scope формата S<1|2|3>-<краткий-ид> — "
            f"severity-классификация инцидента обязательна до реакции "
            f"(J48; FR-1/FR-4; получено {scope!r})")


def resolve_flow(markers: dict) -> tuple[int, bool]:
    """(flow, from_marker). Из маркера — без предупреждений; env
    FLOW_ENFORCER_FLOW — ТОЛЬКО fallback (WARNING в лог), дефолт 1."""
    if markers.get("flow"):
        return int(markers["flow"]), True
    raw = os.environ.get("FLOW_ENFORCER_FLOW", "1")
    try:
        flow = int(str(raw).strip())
    except ValueError:
        flow = 1
    if flow not in FLOW_IDS:
        flow = 1
    log_event({"event": "flow_fallback", "level": "WARNING",
               "flow": flow, "source": "FLOW_ENFORCER_FLOW/default",
               "note": "в маркере нет flow= — флоу взят из env/дефолта; "
                       "обнови заголовок до 'flow: flow=<1-5> role=... "
                       "repo=... scope=...' (J43.1)"})
    return flow, False


def bypass_requested(goal: str) -> bool:
    """Маркер 'flow: bypass --by customer' в goal. Это ЗАЯВКА на обход, а не
    разрешение (BUG-AF-RECHECK-002): goal формирует сам оркестратор, цитата
    маркера внутри текста неотличима от распоряжения. Разрешает только
    find_bypass_decision() — действующая запись в реестре решений."""
    return BYPASS_MARKER in (goal or "")


def bypass_registry_path() -> Path:
    """Путь реестра решений Заказчика (env FLOW_BYPASS_REGISTRY — для
    изоляции тестов; дефолт ~/.hermes/state/flow-bypass-decisions.json)."""
    return Path(os.environ.get(
        "FLOW_BYPASS_REGISTRY",
        str(state_dir() / "flow-bypass-decisions.json"))).expanduser()


def find_bypass_decision(repo: str, scope: str, action: str) -> dict | None:
    """Действующая запись реестра решений для (repo, scope, action) или None.

    Запись действует, если совпадают repo/scope/action И expires (UTC, ISO —
    ОБЯЗАТЕЛЬНО с таймзоной) еще не наступил. Нет файла, битый JSON, битая
    запись, нет/битый срок, expires БЕЗ таймзоны (naive — MAJ-3 ревью
    BUG-AF-RECHECK: молчаливая трактовка naive как UTC — fail-open сдвиг),
    просрочено, другой scope/action — обхода НЕТ (ошибка проверки решения не
    превращается в разрешение; BUG-AF-RECHECK-002). НЕСКОЛЬКО одновременных
    неистекших записей — отказ (неоднозначность, fail-closed; MAJ-3).
    """
    try:
        data = json.loads(bypass_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    now = datetime.now(timezone.utc)
    live: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if (str(entry.get("repo", "")) != repo
                or str(entry.get("scope", "")) != scope
                or str(entry.get("action", "")) != action):
            continue
        raw = entry.get("expires")
        try:
            expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue  # нет/нечитаемый срок — запись не действует
        if expires.tzinfo is None:
            continue  # naive expires — НЕВАЛИДНАЯ запись (не «UTC по умолчанию»)
        if expires <= now:
            continue  # истекла
        live.append(entry)
    if len(live) == 1:
        return live[0]
    return None  # ни одной действующей ИЛИ несколько (неоднозначность)


def _append_log(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": datetime.now(timezone.utc).isoformat(
            timespec="seconds"), **payload}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass  # лог — best-effort; на отказ лога вердикт не меняется


def log_event(payload: dict) -> None:
    _append_log(state_dir() / "flow-enforcer.log", payload)


def log_bypass(task_id: str, goal: str, decision_ref: str | None = None,
               match: dict | None = None) -> None:
    """Аудит bypass: привязка к решению Заказчика (decision_ref из реестра) и
    что именно совпало (repo/scope/action); BUG-AF-RECHECK-002."""
    _append_log(state_dir() / "flow-enforcer-bypass.log", {
        "task_id": task_id, "goal_head": (goal or "")[:200],
        "kind": "bypass --by customer",
        "decision_ref": decision_ref,
        "match": match or {},
    })


def _project(repo: str) -> str:
    return os.environ.get("FLOW_ENFORCER_PROJECT") or Path(repo).name


def prepare_command(markers: dict, flow: int, action: str) -> list[str]:
    """argv проверки: flowctl prepare --dry-run --json (без side effects;
    JSON — чтобы вердикт парсился машинно). J43.1: --flow <N> из маркера,
    --change <scope>."""
    role = markers["role"]
    repo = markers["repo"]
    argv = [
        sys.executable, str(factory_home() / "scripts" / "flowctl.py"),
        "prepare", "--dry-run", "--json",
        "--role", role, "--repo", repo, "--change", markers["scope"],
        "--action", action,
        "--project", _project(repo), "--flow", str(flow),
        "--owner-pm", "flow-enforcer",
    ]
    if markers.get("task"):
        argv += ["--task", markers["task"]]
    # BUG-AF-RECHECK-001: пути задачи попадают в prepare (--path, повторяемый)
    # — ворота зон/конфликтов/arch-review работают с РЕАЛЬНЫМИ путями задачи.
    for path in markers.get("paths") or ():
        argv += ["--path", path]
    # Флоу 3: scope = incident-id с severity-префиксом — фиксируется как
    # incident_ref (ТЗ 03 п.4; J48 FR-4) БЕЗ искажений: маркер не переписывает
    # код инцидента (severity живет внутри incident_ref, design D1).
    if flow == 3:
        argv += ["--incident-ref", markers["scope"]]
    # Флоу 5: условия экспресс-режима подтверждает сам выбор Флоу 5 ПМ;
    # содержательную проверку несет express_close (ретроспективные артефакты).
    if flow == 5:
        argv += ["--small-change"]
    registry = os.environ.get("FLOW_ENFORCER_REGISTRY")
    if registry:
        argv += ["--registry", registry]
    return argv


def run_prepare(markers: dict, flow: int, action: str) -> tuple[int, str, float]:
    """Прогон flowctl prepare --dry-run: (returncode, вывод, длительность)."""
    argv = prepare_command(markers, flow, action)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        rc, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        rc = 124
        out = f"flowctl prepare timeout: {exc}"
    duration = time.monotonic() - t0
    if duration > SLOW_PREPARE_SECONDS:
        log_event({"event": "slow_prepare", "level": "WARNING",
                   "duration_s": round(duration, 2), "argv": argv[2:]})
    return rc, out, duration


def parse_decision_output(out: str) -> dict | None:
    """Decision-словарь из вывода prepare --json (устойчиво к stderr-мусору)."""
    idx = out.find("{")
    if idx < 0:
        return None
    try:
        data = json.loads(out[idx:])
    except ValueError:
        return None
    return data.get("decision") if isinstance(data, dict) else None


def scope_file_candidates(flow: int, repo: Path, scope: str) -> list[Path]:
    """Scope-файлы Флоу 2/4/5 (J43.1): независимое подтверждение, что работа
    этого scope существует, хотя change-пакета нет и не должно быть."""
    if flow == 2:
        return [repo / "test-model" / "bugs" / f"{scope}.md"]
    if flow in (4, 5):
        return [repo / f"PLAN-{scope}.md", repo / f"{scope}.md"]
    return []


def no_change_scope_ok(markers: dict, flow: int, decision: dict | None) -> bool:
    """Post-filter J43.1: отказ prepare ровно из ожидаемого класса «нет
    change-пакета» (все причины — NO_CHANGE_DETAIL_PATTERNS) + существующий
    scope-файл → ожидаемая ситуация Флоу 2/4/5, не блок."""
    if flow not in (2, 4, 5) or not isinstance(decision, dict):
        return False
    details = [str(d) for d in (decision.get("details") or [])]
    if not details:
        return False
    for d in details:
        if not any(p in d for p in NO_CHANGE_DETAIL_PATTERNS):
            return False  # есть причина по существу — фильтр не применяется
    repo = Path(markers["repo"])
    return any(p.is_file() for p in scope_file_candidates(flow, repo,
                                                          markers["scope"]))


# --------------------------------------------------- память фазы (J43.1)


def _flowctl_state_path() -> Path:
    """flowctl_state.json: рядом с реестром (как default_state_path flowctl),
    иначе в FLOW_ENFORCER_STATE_DIR."""
    registry = os.environ.get("FLOW_ENFORCER_REGISTRY")
    if registry:
        return Path(registry).parent / "flowctl_state.json"
    return state_dir() / "flowctl_state.json"


def phase_command(markers: dict, flow: int) -> list[str]:
    return [
        sys.executable, str(factory_home() / "scripts" / "flowctl.py"),
        "phase",
        "--repo", markers["repo"], "--project", _project(markers["repo"]),
        "--flow", str(flow), "--scope", markers["scope"],
        "--state", str(_flowctl_state_path()),
    ]


def run_phase(markers: dict, flow: int) -> dict | None:
    """flowctl phase → JSON-словарь; недоступность (нет state/битый вывод) —
    None + WARNING: проверка последовательности best-effort, ворота prepare
    уже прошли (fail-open ТОЛЬКО у опциональной проверки, не у ворот)."""
    argv = phase_command(markers, flow)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log_event({"event": "phase_unavailable", "level": "WARNING",
                   "detail": str(exc)})
        return None
    idx = (proc.stdout or "").find("{")
    if proc.returncode != 0 or idx < 0:
        log_event({"event": "phase_unavailable", "level": "WARNING",
                   "rc": proc.returncode, "out_head": (proc.stdout or "")[:200]})
        return None
    try:
        return json.loads(proc.stdout[idx:])
    except ValueError:
        log_event({"event": "phase_unavailable", "level": "WARNING",
                   "detail": "phase stdout не JSON"})
        return None


def sequence_violation(flow: int, action: str, phase_data: dict | None) -> str | None:
    """Текст нарушения SEQUENCE_VIOLATION или None (J43.1): у запрошенного
    действия обязаны быть выполнены предшественники среди завершенных этапов
    scope. Возврат на доработку легален — предшественники уже завершены."""
    prereqs = SEQUENCE_PREREQS.get(flow, {}).get(action)
    if prereqs is None or phase_data is None:
        return None  # неизвестное действие/нет данных — решают ворота prepare
    completed = set(phase_data.get("completed_actions") or [])
    phase = phase_data.get("phase")
    last = phase.get("action") if isinstance(phase, dict) else None
    if not completed and isinstance(phase, dict) and (
        phase.get("finished_status") in COMPLETED_STATUSES
    ):
        completed = {last} if last else set()
    missing = sorted(prereqs - completed)
    if missing:
        return (f"последний завершенный этап был {last or '— (ничего)'}, "
                f"запрошен {action}; не завершены обязательные "
                f"предшественники: {', '.join(missing)}")
    return None


def sequence_gate(markers: dict, flow: int, action: str,
                  task_id: str) -> dict | None:
    """Опциональная проверка последовательности (FLOW_ENFORCER_SEQUENCE=1,
    дефолт off): после prepare-ALLOW — допустим ли action как следующий этап
    этого scope. Нарушение → block SEQUENCE_VIOLATION."""
    flag = os.environ.get("FLOW_ENFORCER_SEQUENCE", "").strip().lower()
    if flag not in ("1", "true", "yes"):
        return None
    data = run_phase(markers, flow)
    violation = sequence_violation(flow, action, data)
    if violation:
        log_event({"event": "sequence_violation", "task_id": task_id,
                   "flow": flow, "action": action, "scope": markers["scope"]})
        return {"action": "block", "message": (
            f"FLOW-ENFORCER [SEQUENCE_VIOLATION]: нарушение порядка этапов "
            f"Флоу {flow} (scope={markers['scope']}): {violation}. "
            f"Заверши предшествующие этапы через flowctl prepare/run/finish." )}
    return None


def flow_enforce(tool_name: str, args: dict, task_id: str = "",
                 **kwargs):  # noqa: ANN201 — None или directive-dict
    """pre_tool_call-хук: валидирует delegate_task против ворот конвейера."""
    try:
        if tool_name != TRIGGER_TOOL:
            # J42 (L4): Bash-вызовы git push в контексте активной делегации —
            # push-политика (разрешена только ветка из ALLOW-объекта);
            # остальное — без директивы.
            if isinstance(tool_name, str) and tool_name.lower() in PUSH_TOOLS:
                return push_gate(args or {}, task_id)
            return None  # не делегация и не Bash — без директивы

        args = args or {}
        goal = args.get("goal") if isinstance(args.get("goal"), str) else ""
        log_event({"event": "delegate_task_seen", "task_id": task_id})

        # J60 (MODEL_ENFORCE): маркер исполнителя на боевом этапе 5.3-роли —
        # ДО разбора flow-маркеров (работает и для делегаций без заголовка).
        model_block = model_enforce_gate(goal, task_id)
        if model_block is not None:
            return model_block

        # BUG-AF-RECHECK-002: маркер bypass — ЗАЯВКА; разрешает только запись
        # в реестре решений. Проверка возможна только когда известны
        # repo/scope/action — после разбора маркеров (см. ниже).
        bypass_wanted = bypass_requested(goal)

        try:
            markers = parse_markers(args)
        except FlowMarkerError as exc:
            log_event({"event": "flow_marker_invalid", "task_id": task_id,
                       "detail": str(exc)})
            return {"action": "block", "message": (
                f"FLOW-ENFORCER [{FLOW_MARKER_INVALID}]: {exc}. Канонический "
                "заголовок: 'flow: flow=<1-5> role=<роль> repo=<путь> "
                "scope=<идентификатор>' (см. templates/task_delegation.md).")}
        if markers is None:
            if mode() == "strict":
                return {"action": "block", "message": (
                    "FLOW-ENFORCER [MARKERS_MISSING]: делегация без маркеров "
                    "конвейера. Добавь в goal заголовок "
                    "'flow: flow=<1-5> role=<роль> repo=<путь> "
                    "scope=<идентификатор>' (см. templates/task_delegation.md) "
                    "или пройди flowctl prepare. Bypass — только 'flow: "
                    "bypass --by customer' ПРИ действующей записи в реестре "
                    "решений (~/.hermes/state/flow-bypass-decisions.json).")}
            # MIN-1 ревью BUG-AF-RECHECK: bypass-ЗАЯВКА без маркеров в
            # permissive-режиме — тоже аудит-факт: пишем в bypass-лог
            # bypass_requested_unfulfilled (заявка не могла быть проверена
            # против реестра — обход не состоялся).
            if bypass_wanted:
                _append_log(state_dir() / "flow-enforcer-bypass.log", {
                    "task_id": task_id,
                    "goal_head": (goal or "")[:200],
                    "kind": "bypass_requested_unfulfilled",
                    "note": "заявка bypass без маркеров конвейера "
                            "(permissive-пропуск) — запись реестра решений "
                            "не проверялась и не найдена",
                })
            log_event({"event": "markers_missing_permissive",
                       "task_id": task_id})
            return None

        flow, from_marker = resolve_flow(markers)
        validate_scope_for_flow(flow, markers["scope"])
        action = action_for_role(markers["role"], flow)
        if not from_marker:
            # WARNING уже в логе (flow_fallback); дублируем факт в prepare_result
            pass

        if bypass_wanted:
            # BUG-AF-RECHECK-002: обход — только по действующей записи реестра
            # решений Заказчика (repo/scope/action, неистекший expires).
            decision = find_bypass_decision(markers["repo"], markers["scope"],
                                            action)
            if decision is not None:
                match = {"repo": markers["repo"], "scope": markers["scope"],
                         "action": action}
                log_bypass(task_id, goal,
                           decision_ref=decision.get("decision_ref"),
                           match=match)
                log_event({"event": "bypass", "task_id": task_id,
                           "decision_ref": decision.get("decision_ref")})
                return None
            log_event({"event": "bypass_denied", "level": "WARNING",
                       "task_id": task_id,
                       "note": "маркер bypass без действующей записи реестра "
                               "решений (нет/просрочена/другой scope|action) "
                               "— обычная проверка ворот"})

        # BUG-AF-RECHECK-001: отсутствие путей ≠ docs-only. kind не указан →
        # WARNING + трактовка как product (без paths честный MISSING-отказ).
        # MAJ-1 ревью BUG-AF-RECHECK: kind=docs валиден ТОЛЬКО для
        # документных/обслуживающих действий (docs_task/chore_task).
        # Продуктовая dev-задача с kind=docs без paths обходит arch-gate →
        # честный отказ PATHS_MISSING (dev-задача без путей не может быть
        # docs по определению).
        kind = markers.get("kind")
        paths = markers.get("paths") or []
        if kind is None:
            log_event({"event": "kind_assumed_product", "level": "WARNING",
                       "task_id": task_id,
                       "note": "kind не указан — задача трактуется как "
                               "product (fail-closed); для docs-задачи укажи "
                               "kind=docs в заголовке flow:"})
        if not paths and (kind != "docs"
                          or action not in DOCS_KIND_ACTIONS):
            why_docs = ("действие не docs_task/chore_task"
                        if kind == "docs" else
                        "отсутствие путей ≠ docs-only (BUG-AF-RECHECK-001)")
            return {"action": "block", "message": (
                "FLOW-ENFORCER [PATHS_MISSING]: задача без путей "
                f"({why_docs}; MAJ-1 ревью BUG-AF-RECHECK: kind=docs валиден "
                "только для docs_task/chore_task). Добавь в заголовок "
                "paths=<путь1,путь2,...> (относительные пути задачи через "
                "запятую) или kind=docs для docs-задачи (действие "
                "docs_task/chore_task) без продуктовых путей.")}

        rc, out, duration = run_prepare(markers, flow, action)
        log_event({"event": "prepare_result", "task_id": task_id,
                   "rc": rc, "duration_s": round(duration, 2),
                   "flow": flow, "from_marker": from_marker,
                   "role": markers["role"], "repo": markers["repo"],
                   "scope": markers["scope"], "action": action})
        allowed = rc == 0
        if not allowed:
            # Post-filter J43.1: ожидаемый отказ «нет change-пакета» при
            # существующем scope-файле — ALLOW (Флоу 2/4/5 живут без пакета).
            decision = parse_decision_output(out)
            if no_change_scope_ok(markers, flow, decision):
                log_event({"event": f"flow-{flow} no-change scope ok",
                           "task_id": task_id, "scope": markers["scope"],
                           "prepare_rc": rc})
                allowed = True
        if allowed:
            # J51.1: worktree-per-delegation — вторая параллельная делегация
            # того же repo без разных worktree не проходит (WORKTREE_MISSING).
            wt_block = worktree_gate(markers, task_id)
            if wt_block is not None:
                return wt_block
            return sequence_gate(markers, flow, action, task_id)
        return {"action": "block", "message": (
            f"FLOW-ENFORCER: flowctl prepare отклонил делегацию "
            f"(exit {rc}, flow={flow}, role={markers['role']}, "
            f"repo={markers['repo']}, scope={markers['scope']}). "
            f"Вывод ворот:\n"
            f"{out.strip()[-2000:]}")}
    except FlowMarkerError as exc:
        log_event({"event": "flow_marker_invalid", "task_id": task_id,
                   "detail": str(exc)})
        return {"action": "block", "message": (
            f"FLOW-ENFORCER [{FLOW_MARKER_INVALID}]: {exc}. Канонический "
            "заголовок: 'flow: flow=<1-5> role=<роль> repo=<путь> "
            "scope=<идентификатор>'.")}
    except Exception as e:  # noqa: BLE001 — сознательный fail-closed (J43)
        return {"action": "block", "message": (
            f"flow-enforcer internal error: {e} (fail-closed)")}


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", flow_enforce)
