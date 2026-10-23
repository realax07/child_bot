#!/usr/bin/env python3
"""flowctl.py — единый CLI-вход локального оркестратора (поставка 06, ТЗ
docs/chatgpt-deterministic-flow/06-local-orchestrator.md).

Один управляемый цикл запуска агента с recovery. inspect/check/next — прокси
на flow_state/flow_transition (поставки 02–03, exit codes сохранены);
prepare/run/finish/status/reconcile — исполнение разрешенного действия.

Принципы (ТЗ 06; контракт §4, §13):

- adapter=manual по умолчанию: prepare готовит session/worktree/goal и ЖДЕТ
  внешнего исполнения delegate_task. Подготовленный goal — НЕ факт запуска;
  статус running — факт разрешения и отметки старта, не факт работы агента.
  Автоматический Hermes-adapter НЕ входит в поставку (только после отдельного
  живого прогона и решения Заказчика).
- run разрешен только после явного решения (ALLOW из check_action на prepare)
  и реальной reservation в реестре сессий (поставка 04).
- Идемпотентность: повтор run с тем же correlation ID не создает вторую
  сессию и не повторяет делегацию; повтор prepare с тем же correlation ID и
  тем же payload возвращает существующую запись (иной payload — отказ
  DUPLICATE_PAYLOAD).
- STALE_SNAPSHOT: непосредственно перед стартом снимок переснимается;
  несовпадение digest с моментом prepare → отказ, статус не меняется.
- Timeout/crash не продвигают на следующий шаг: reconcile помечает
  needs_attention/stale (ничего не удаляется молча, worktree сохраняется);
  - finish на такой сессии — blocked. После завершения агентский отчет
    рассматривается как указатель на файлы: вердикт accepted/returned/blocked
    по фактическому Git diff, разрешенной зоне и gates; при возврате —
    структурированный список дефектов.
  - P0.2 (пересмотр плана Заказчика): обязательные gates вычисляются из
    политики (required_gates_for: DEFAULT_GATES точки запуска + машинные
    ворота этапа из STAGE_TABLE), а не из флагов. --gates может только
    ДОБАВИТЬ проверки — попытка заменить обязательный набор отвергается
    с перечнем недостающих (MISSING_REQUIRED_GATES). --diagnostic прогоняет
    сокращенный список БЕЗ права accepted: вердикт diagnostic_only, сессия
    не закрывается. Вердикт accepted несет список реально выполненных gates
    и их digest.
- P0.3 (пересмотр плана Заказчика): --zone ограничен политикой роли
  (ROLE_ZONE_POLICY — таблица зон agents/README.md): CLI может только
  СУЗИТЬ. Запрос пути вне политики роли — отказ (ZONE_OUTSIDE_POLICY) с
  перечнем недопустимых путей; сужение прозрачно (в выводе — фактические
  суженные зоны и policy_version). finish сверяет diff ИМЕННО с суженной
  (политико-валидной) зоной; реестр хранит policy_version для аудита.
- Никакого авто-push/merge/deploy и авто-старта фаз: только исполнение
  разрешенного; этапные ворота Заказчика — check_action
  (HUMAN_APPROVAL_REQUIRED, контракт §10).

Состояние цикла: JSON-файл (--state, по умолчанию рядом с реестром),
атомарная запись под локом. Реестр сессий и state — всегда явные пути
(тесты не трогают ~/.hermes/state/).

Usage:
    flowctl.py inspect <опции flow_state inspect>     # прокси, exit как там
    flowctl.py check <опции flow_transition check>    # прокси, exit как там
    flowctl.py next <опции flow_transition next>      # прокси, exit как там
    flowctl.py prepare --repo R --project P --flow N --change C
        --action A --role ROLE [--task T] [--correlation-id ID] [--dry-run]
        [--path ZONE ...] --owner-pm PM [--worktree PATH]
        [--create-worktree --branch B] [--goal-path F] [--approval-ref S]
        [--expected-digest D] [--parallel] [--spec-delta] [--incident-ref S]
        [--pipeline-marker] [--rules-change] [--small-change]
        [--registry PATH] [--state PATH] [--audit PATH] [--json]
    flowctl.py run --correlation-id ID [--registry PATH] [--state PATH]
        [--audit PATH] [--json]
    flowctl.py finish --correlation-id ID --report PATH [--gate-scope S]
        [--gates a,b] [--diagnostic] [--openspec-cmd CMD] [--timeout SEC] [--pm-mode M]
        [--pm-commits X] [--pm-range A..B] [--pm-registry PATH]
        [--pm-require-review] [--pr-id ID] [--log-dir D] [--report-dir D]
        [--registry PATH] [--state PATH] [--audit PATH] [--json]
    flowctl.py status [--correlation-id ID] [--registry PATH] [--state PATH]
    flowctl.py phase --repo PATH --project ID --flow N --scope S
        [--registry PATH] [--state PATH]
    flowctl.py reconcile [--repo PATH] [--delegation-id ID]
        [--registry PATH] [--state PATH] [--audit PATH] [--json]
    flowctl.py release <correlation-id> [--reason R] [--registry PATH]
        [--state PATH] [--audit PATH] [--json]
    flowctl.py regress --change C [--repo PATH] [--base REF] [--tier domain|smoke]
        [--final] [--xdist N] [--suggest-baseline] [--timeout SEC] [--json]
    flowctl.py integrate <repo> [--cid ID] [--base REF] [--registry PATH]
        [--json]

Exit codes: 0 — успех/ALLOW/accepted (run/prepare — разрешено; regress —
PASS); 1 — DENY/STALE_SNAPSHOT/returned (regress — FAIL); 2 — UNKNOWN/ошибка
входа/blocked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import flow_mode  # noqa: E402
import flow_state  # noqa: E402
import flow_transition as ft  # noqa: E402
import gate_runner as gr  # noqa: E402
import pm_bounds_check as pmb  # noqa: E402  (Н14: J3-префиксы защищенных путей)
import role_zone_policy as rzp  # noqa: E402
import session_check as sc  # noqa: E402

OUTPUT_SCHEMA = "flowctl-output/1"
STATE_SCHEMA = "flowctl-state/1"
ADAPTER = "manual"

# J43.1: память фазы (flowctl phase).
PHASE_SCHEMA = "flowctl-phase/1"
# «Завершенный этап» для памяти фазы/последовательности: вердикт вынесен
# (accepted — этап принят; returned — этап завершен возвратом на доработку,
# сам факт этапа состоялся). prepared/running — этап еще не завершен;
# blocked — завершения не было.
PHASE_COMPLETED_STATUSES = ("accepted", "returned")

# ------------------------------------------- P0.3: политика зон записи ролей
# Источник политики — ROLE_ZONE_POLICY (role_zone_policy.py) из таблицы
# «Зона записи (только она)» agents/README.md. --zone (CLI) может ТОЛЬКО
# СУЗИТЬ политику роли: запрос пути вне политики — отказ с перечнем
# недопустимых путей; сужение — прозрачно (выводит фактические зоны).
ZONE_OUTSIDE_POLICY = "ZONE_OUTSIDE_POLICY"

# ------------------------------------------- Н14: protected-paths (J3) ворот
# Аудит Н14: protected-paths ворота prepare всегда видели пустой список путей
# (он заполнялся из зоны --path, а зона-паттерн «deploy/**» не выглядит
# защищенным путем) — ворота молча проходили. Флаг --paths (через запятую)
# подает КОНКРЕТНЫЕ пути изменения в protected-paths ворота: пересечение с
# защищенными префиксами (J3/J26: openspec/specs/, contracts/, AGENTS.md,
# agents/README.md, deploy/, RUNBOOK.md) без [pipeline]-маркера — DENY
# ДО side effects (reservation/worktree/goal не создаются).
J3_PROTECTED_PREFIXES = tuple(pmb.PROTECTED_PATHS) + tuple(pmb.DEPLOY_PATHS)
J3_PROTECTED_REASON = "J3_PROTECTED_PATHS"


def parse_paths_arg(raw: str | None) -> list[str]:
    """--paths «deploy/x, src/a.py» → список канонических путей (Н14)."""
    return [sc.canonical(p) for p in (raw or "").split(",") if p.strip()]


def protected_path_hits(paths: list[str]) -> list[str]:
    """Пути запроса, попадающие в защищенные префиксы (J3/J26)."""
    hits: list[str] = []
    for p in paths or []:
        if any(p == pe.rstrip("/") or p.startswith(pe)
               for pe in J3_PROTECTED_PREFIXES):
            hits.append(p)
    return hits


# ------------------------------------------------ Н12: диагностика scope
# Аудит Н12: AMBIGUOUS_STATE без диагностики — вызывающий не видит, КАКОЕ
# поле scope невалидно. Здесь — конкретное указание поля по формату флоу
# (flow_state.FLOW_EXPECTATIONS): «scope=xxx не соответствует формату
# Флоу 3 (BUG-NNN)». Невалидный --flow ловится до inspect и отдается
# JSON-диагностикой (не сырым argparse).


def ambiguous_field_hints(scope: dict) -> list[str]:
    """Конкретные невалидные поля scope для AMBIGUOUS_STATE (Н12)."""
    scope = scope or {}
    flow = scope.get("flow")
    change = str(scope.get("change") or "")
    if flow not in flow_state.FLOW_IDS:
        return [f"flow={flow!r} вне допустимых 1–5 "
                "(ожидается целое число 1–5)"]
    hints: list[str] = []
    if flow in (2, 3) and not flow_state.BUG_ID_RE.match(change):
        hints.append(f"scope={change} не соответствует формату "
                     f"Флоу {flow} (BUG-NNN, например BUG-042)")
    elif flow == 1 and not flow_state.CHANGE_ID_RE.match(change):
        hints.append(f"scope={change} не соответствует формату Флоу 1 "
                     "(kebab-case change-id, например add-widget)")
    elif flow == 4 and not flow_state.CHORE_RE.match(change):
        hints.append(f"scope={change} не соответствует формату Флоу 4 "
                     "([chore])")
    elif flow == 5 and not flow_state.CHANGE_ID_RE.match(change):
        hints.append(f"scope={change} не соответствует формату Флоу 5 "
                     "(change-id)")
    return hints


def _decision_has_ambiguous(decision: ft.Decision) -> bool:
    parts = [str(x) for x in
             list(decision.blocking_reasons) + list(decision.details)]
    return any(ft.AMBIGUOUS_STATE in p for p in parts)


def zone_policy_check(role: str, requested: list) -> dict:
    """Сужение запрошенных --zone до политики роли + прозрачный план
    (запрошено → сужено, версия политики)."""
    narrowed = rzp.narrow_zones(role, requested)
    return {
        "narrowed": narrowed,
        "requested": [sc.canonical(p) for p in (requested or [])],
        "policy_version": narrowed["policy_version"],
        "policy_source": rzp.POLICY_SOURCE,
    }

# ------------------------------------------------------- P0.2: политика gates
# Источник политики (P0.2, пересмотр плана Заказчика): контракт §4/§5/§11
# (run_gates — ворота ФАЗЫ, определяются политикой, а не вызывающим) и
# STAGE_TABLE из flow_transition.py (машинные gates этапа действия).
# Смысл: finish НЕ может выбрать набор проверок — обязательные gates
# вычисляются из области (gate_scope) и действия записи; --gates умеет
# только ДОБАВЛЯТЬ, --diagnostic прогоняет сокращенный список без accepted.

# Имена этапов из STAGE_TABLE (flow_transition.py), для которых ворота этапа
# исполнимы здесь как machine-gate (маркеры вида «flow_check (контракт 3)» —
# человеческие формулировки контракта, их проверяет flow_check целиком).
_STAGE_IMPLEMENTS_MACHINE_GATE = {
    "flow_check": "flow_check",
    "pm_bounds_check (J9/J10)": "pm_bounds_check",
    "pm_bounds_check --require-review": "pm_bounds_check",
    "pm_bounds_check (J3)": "pm_bounds_check",
    "pr_validate [BUG-NNN]": "pr_validate",
    "pr_validate check_chore": "pr_validate",
    "pr_validate [change-id]": "pr_validate",
    "openspec validate --strict": "openspec_validate",
    "openspec validate": "openspec_validate",
}

# Диагностический прогон: разрешенный сокращенный список (без pm_bounds —
# там нужны явные аргументы режима). Все равно не дает accepted.
DIAGNOSTIC_GATES = ("flow_check",)


def required_gates_for(scope: dict, action: str | None = None,
                       gate_scope: str = "post_agent") -> tuple[str, ...]:
    """Обязательный набор gates для области finish (P0.2; контракт §4/§11:
    run_gates(scope, phase) — ворота фазы из политики, не из флагов).

    Состав: DEFAULT_GATES[gate_scope] (точка запуска, gate_runner) плюс
    машинные ворота этапа действия из STAGE_TABLE (flow 1–5), которые
    выражаются известными исполнимыми адаптерами (см.
    _STAGE_IMPLEMENTS_MACHINE_GATE). Порядок — как в таблице политики.
    """
    gates: list[str] = list(gr.DEFAULT_GATES[gate_scope])
    flow = (scope or {}).get("flow")
    stage_gates: tuple = ()
    if action and flow in ft.STAGE_TABLE:
        stage = next((s for s in ft.STAGE_TABLE[flow]
                      if s.action == action), None)
        if stage is not None:
            stage_gates = stage.gates
    for g in stage_gates:
        machine = _STAGE_IMPLEMENTS_MACHINE_GATE.get(g)
        if machine and machine not in gates:
            gates.append(machine)
    return tuple(gates)


RECORD_ACTIVE = ("prepared", "running", "blocked", "needs_attention")
RECORD_CLOSED = ("accepted", "returned")


# ------------------------------------------------------------- состояние


def state_load(path: Path) -> dict:
    """Читает state-файл цикла. Отсутствует → пустой (создастся записью)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"schema_version": STATE_SCHEMA, "runs": {}}
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("runs"), dict):
        raise ValueError(f"битый state-файл: {path}")
    return data


def state_write(path: Path, data: dict) -> None:
    """Атомарная запись (tmp + os.replace). Лок берется снаружи
    (state_write_locked); здесь — совместимая обертка для вызовов без
    внешнего лока. ВАЖНО: _acquire_lock не реентерабелен — вызывать
    state_write под уже взятым локом нельзя (смертельная блокировка)."""
    data["schema_version"] = STATE_SCHEMA
    sc.registry_write(path, data)


def state_write_locked(path: Path, data: dict, mutate) -> None:
    """Чтение + правка + атомарная запись под одним локом (mutate(st)
    меняет state на месте). Лок берется ровно один раз."""
    lock = sc._acquire_lock(path)
    try:
        st = state_load(path)
        mutate(st)
        state_write(path, st)
    finally:
        sc._release_lock(lock)


def default_state_path(registry: str | None) -> Path:
    base = Path(registry) if registry else (
        Path.home() / ".hermes" / "state" / "active_sessions.json"
    )
    return base.parent / "flowctl_state.json"


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for k, v in sorted(payload.items()):
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, sort_keys=True)
        print(f"{k}: {v}")


def registry_transition(registry_path: Path, delegation_id: str, to: str,
                        reason: str, evidence: str,
                        expect_from: str | None = None) -> dict:
    """Смена статуса reservation с evidence (жизненный цикл ТЗ 04).

    expect_from (review-001 #1, гонка release×run): если задан,
    фактический статус сверяется ПОД ЛОКОМ — после захвата лока, перед
    transition. Пара «prev→to», прочитанная снаружи, не пройдет, если
    между чтением и локом сессию перевел другой процесс (например,
    `flowctl run` агента: reserved→running).
    """
    lock = sc._acquire_lock(registry_path)
    try:
        data, err = sc.registry_load(registry_path)
        if err:
            return {"ok": False, "reason": sc.REGISTRY_ERROR, "details": [err]}
        if data.get("schema_version") != sc.REGISTRY_SCHEMA:
            data = sc._load_or_migrate(registry_path, data)
        session = next(
            (s for s in data.get("sessions", [])
             if isinstance(s, dict) and s.get("delegation_id") == delegation_id),
            None)
        if session is None:
            return {"ok": False, "reason": sc.MISSING_INPUT,
                    "details": [f"reservation {delegation_id} не найден"]}
        current = session.get("status")
        if expect_from is not None and current != expect_from:
            return {"ok": False, "reason": "status_changed",
                    "details": [f"статус изменился под локом: ожидался "
                                f"{expect_from!r}, фактический {current!r}"],
                    "current_status": current}
        sc.transition(session, to, reason, evidence)
        sc.registry_write(registry_path, data)
        return {"ok": True, "session": session}
    finally:
        sc._release_lock(lock)


def _git_head(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _active_parallel_note(registry_path: Path, exclude: str) -> str:
    """Список активных сессий/зон для блока «Параллель» goal (AGENTS.md п.10)."""
    data, err = sc.registry_load(registry_path)
    if err:
        return f"реестр сессий нечитаем ({err}) — считай, что параллельные сессии ЕСТЬ, зону не покидать"
    rows = []
    for s in data.get("sessions", []):
        if not isinstance(s, dict) or s.get("delegation_id") == exclude:
            continue
        if s.get("status") in sc.ZONE_HOLDING:
            rows.append(f"{s.get('delegation_id', '?')}: "
                        f"{', '.join(s.get('zones') or []) or '?'}")
    return "; ".join(rows) if rows else "нет"


def _approval_summary(ref) -> str:
    if ref is None:
        return "нет (действие не требует этапного решения Заказчика)"
    if isinstance(ref, dict):
        return (f"decision {ref.get('decision_id', '?')} "
                f"(grants: {', '.join(map(str, ref.get('grants') or []))})")
    return str(ref)


def build_goal_text(record: dict, decision: ft.Decision,
                    registry_path: Path) -> str:
    """Минимальный goal по чеклисту templates/task_delegation.md (блоки 1–9).
    Никаких секретов и полных промптов — только пути, scope, зоны, ссылки."""
    scope = record["scope"]
    wt = (f"worktree {record['worktree']} (ветка {record['branch']})"
          if record.get("worktree") else f"рабочее дерево {scope['repo']}")
    return "\n".join([
        f"# Делегация {record['correlation_id']}",
        f"Роль: {record['actor_role']} (agents/{record['actor_role']}_agent.md — прочитай сам)",
        "Правила: AGENTS.md, contracts/artifact_contract.md (контракты входа/выхода роли)",
        f"Задача: исполнить действие «{record['action']}»"
        + (f" по задаче {record['task_id']} tasks.md" if record.get("task_id") else ""),
        f"Scope: project={scope['project']} flow={scope['flow']} "
        f"change={scope['change']} task={scope.get('task')}",
        f"Рабочее дерево: {wt}",
        f"Зона записи (сужена до политики роли): {', '.join(record['zones']) or '—'} — "
        "запись вне зоны запрещена; push НЕТ",
        "Версия политики зон: "
        f"{record.get('policy_version') or rzp.policy_version()}",
        f"Решения Заказчика: {_approval_summary(record.get('approval_ref'))}",
        f"Параллельные сессии и их зоны: "
        f"{_active_parallel_note(registry_path, record['delegation_id'])}",
        "Отчет: путь к файлу отчета (указатель на измененные/созданные "
        "артефакты), результаты самопроверок, вопросы/эскалации списком",
        "Изоляция: контекста диалога у тебя нет; всё необходимое — в файлах "
        "и этом тексте; недостающее — эскалация, не домысливание",
        "Проверка (gates): " + (", ".join(decision.required_gates) or "—"),
        f"snapshot_digest (момент разрешения): {record['prepared_digest']}",
        "Примечание оркестратора: adapter=manual — этот файл подготовлен к "
        "внешнему запуску delegate_task; сам факт запуска фиксируется "
        "отдельно и не следует из наличия файла.",
        "",
    ])


def _build_action(args, repo: Path, flow: int) -> ft.ActionRequest:
    """ActionRequest из аргументов prepare (та же логика зависимостей/[P],
    что в flow_transition _cmd_check — переиспользуются хелперы импортом)."""
    deps: tuple = ()
    dep_ev = None
    parallel = args.parallel            # None | True | False (три состояния)
    parallel_confirmed = None
    if flow == 1 and args.task:
        info, parallel_auto = ft._task_deps_from_repo(repo, args.change, args.task)
        deps = tuple(info.get("deps", ()))
        if parallel is None:
            parallel = parallel_auto
        if parallel:
            parallel_confirmed = bool(info.get("parallel"))
    if deps:
        dep_ev = ft._dep_evidence(repo, args.change, deps)
    return ft.ActionRequest(
        actor_role=args.role,
        requested_action=args.action,
        task_id=args.task,
        approval_ref=args.approval_ref,
        expected_snapshot_digest=args.expected_digest,
        task_parallel=parallel,
        parallel_confirmed=parallel_confirmed,
        task_dependencies=deps,
        dependency_evidence=dep_ev,
        spec_delta=args.spec_delta,
        incident_ref=args.incident_ref,
        paths=tuple(list(args.zone or ())
                    + parse_paths_arg(getattr(args, "paths", None))),
        pipeline_marker=args.pipeline_marker,
        rules_change=args.rules_change,
        small_change=args.small_change,
    )


def _status_exit(status: str) -> int:
    return {ft.ALLOW: 0, ft.DENY: 1}.get(status, 2)


# ------------------------------------------------- Н7: устаревшие держатели зоны
# Аудит Н7: finished-сессия (работа не влита, вердикт returned) продолжает
# держать зону — следующий prepare по ней = ZONE_CONFLICT без выхода. Здесь:
# (1) диагностика — перечень таких держателей с ГОТОВОЙ командой
# авто-освобождения (flowctl release); (2) флаг --release-stale — явное
# разрешение prepare автоматически перевести их в released (expect_from под
# локом, ничего не удаляется) и повторить резервацию.


def find_stale_returned_holders(registry_path: Path, state_path: Path,
                                zones: list[str]) -> list[dict]:
    """finished-сессии, держащие пересекаемую зону, чей вердикт returned."""
    data, err = sc.registry_load(registry_path)
    if err:
        return []
    state = state_load(state_path)
    want = [sc.canonical(p) for p in (zones or [])]
    out: list[dict] = []
    for s in data.get("sessions", []):
        if not isinstance(s, dict) or s.get("status") != "finished":
            continue
        other = [sc.canonical(p) for p in
                 (s.get("zones") or s.get("paths") or [])]
        if not sc.zones_overlap(want, other):
            continue
        rec = next((r for r in state.get("runs", {}).values()
                    if isinstance(r, dict)
                    and r.get("delegation_id") == s.get("delegation_id")),
                   None)
        if rec is None or rec.get("status") != "returned":
            continue
        cid = str(rec.get("correlation_id") or "")
        out.append({
            "delegation_id": s.get("delegation_id"),
            "correlation_id": cid,
            "verdict": "returned",
            "zones": other,
            "release_cmd": (f"flowctl.py release {cid} "
                            f"--registry {registry_path} --state {state_path}"),
        })
    return out


# ------------------------------------------------- ветка (J52-w1.1: деривация)


# Шов пилота J51 #1: ветки деривились обрезкой correlation-id до 8 символов
# (j51-pilot-A/B коллизировали в одну ветку flow/j51-pilo). Правило J52:
# ветка — из ПОЛНОГО correlation-id; если полный id в имя ветки не помещается
# (длина) или небезопасен как компонент git-ref (чужие символы, "..", ".lock",
# ведущий "-") — детерминированный суффикс-хэш sha1[:8] ОТ ПОЛНОГО id (не
# обрезка: разные id с общим префиксом дают разные хэши).
BRANCH_PREFIX = "flow"
BRANCH_MAX_ID_LEN = 40         # uuid4().hex (=32) помещается целиком
_BRANCH_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]*$")


def _branch_id_ok(cid: str) -> bool:
    """Полный correlation-id годится как хвост имени git-ветки."""
    if not cid or len(cid) > BRANCH_MAX_ID_LEN:
        return False
    if ".." in cid or "@{" in cid:
        return False
    if cid.endswith(".lock") or cid.endswith("/") or cid.endswith("."):
        return False
    # review-001 #2 (minor): _BRANCH_SAFE_RE пропускал формы, которые git
    # отвергает как имя ветки: пустые компоненты («a//b»), компонент с
    # ведущей точкой («a/.x») или с хвостом «.lock» («a.lock/b»). Для
    # таких cid ветка всегда невалидна → prepare деградировал бы в
    # WARN-работу в общем дереве вместо детерминированного хэш-фолбэка.
    if "//" in cid:
        return False
    parts = cid.split("/")
    if any(p.startswith(".") or p.endswith(".lock") for p in parts):
        return False
    if not _BRANCH_SAFE_RE.match(cid):
        return False
    # Финальный арбитр — сам git: не прошел check-ref-format → хэш-фолбэк.
    # git недоступен — доверяем явным правилам выше (для символов,
    # допускаемых _BRANCH_SAFE_RE, они покрывают правила git полностью).
    try:
        proc = subprocess.run(
            ["git", "check-ref-format", "--branch", f"{BRANCH_PREFIX}/{cid}"],
            capture_output=True, text=True,
        )
    except OSError:
        return True
    return proc.returncode == 0


def branch_name_for(correlation: str) -> str:
    """J52-w1.1: имя ветки делегации — полный correlation-id; длинный/небезопасный
    id → flow/<sha1[:8]> от полного id. Детерминированно, без обрезки префикса."""
    cid = (correlation or "").strip()
    if _branch_id_ok(cid):
        return f"{BRANCH_PREFIX}/{cid}"
    digest = hashlib.sha1(cid.encode("utf-8")).hexdigest()[:8]
    return f"{BRANCH_PREFIX}/{digest}"


# ---------------------------------------------------------------- prepare


def cmd_prepare(args) -> int:
    repo = Path(args.repo).resolve()
    if not repo.is_dir() or not _git_head(repo):
        print(f"FLOWCTL-ERROR: не git-репозиторий: {repo}", file=sys.stderr)
        return 2
    registry_path = Path(args.registry) if args.registry else (
        Path.home() / ".hermes" / "state" / "active_sessions.json")
    correlation = args.correlation_id or uuid.uuid4().hex
    delegation_id = f"deleg-{correlation[:16]}"

    # 1) Снимок и решение (чистые операции).
    try:
        snapshot = flow_state.inspect(
            repo_arg=str(repo), project=args.project, flow=args.flow,
            change_id=args.change, task_id=args.task, registry=args.registry,
        )
    except ValueError as exc:
        print(f"FLOWCTL-ERROR: {exc}", file=sys.stderr)
        return 2
    action = _build_action(args, repo, args.flow)
    decision = ft.check_action(snapshot, action)
    # Н12: AMBIGUOUS_STATE — диагностика указывает КОНКРЕТНОЕ невалидное
    # поле scope (формат идентификатора выбранного флоу), а не абстрактный
    # «неоднозначный scope».
    amb_hints = (ambiguous_field_hints(snapshot.get("scope", {}))
                 if decision.status != ft.ALLOW
                 and _decision_has_ambiguous(decision) else [])

    # 1a) P0.3: сужение --zone до политики роли (agents/README.md). CLI
    #     не может расширить политику: запрос пути вне зоны роли — отказ
    #     с перечнем недопустимых путей; сужение прозрачно (выводит
    #     фактические (суженные) зоны и policy_version).
    requested_zones = [sc.canonical(p) for p in (args.zone or [])]
    zp = zone_policy_check(args.role, requested_zones)
    narrowed = zp["narrowed"]
    zones = narrowed["zones"]
    policy_violations = narrowed["violations"]
    policy_version = narrowed["policy_version"]
    # J52-w1.1: ветка делегации — полный correlation-id (или хэш-суффикс),
    # не обрезка до 8 символов (шов пилота J51 #1: коллизии префиксов).
    derived_branch = args.branch or (
        branch_name_for(correlation) if args.create_worktree else None)
    # Н14: явный список путей изменения для protected-paths (J3) ворот.
    gate_paths = parse_paths_arg(getattr(args, "paths", None))
    protected_hits = protected_path_hits(gate_paths)
    plan = {
        "role": args.role,
        "action": args.action,
        "task": args.task,
        "zones": zones,
        "requested_zones": zp["requested"],
        "policy_version": policy_version,
        "policy_source": zp["policy_source"],
        "zone_violations": policy_violations,
        "worktree": args.worktree
        or (f"<repo>-worktrees/{delegation_id}" if args.create_worktree else None),
        "branch": derived_branch,
        "gates": decision.required_gates,
        "human_gate": _approval_summary(args.approval_ref),
        "protected_paths_probe": gate_paths,
        "decision": decision.to_dict(),
    }

    # 1a-Н14) protected-paths (J3) ворота prepare: конкретные пути изменения
    #     берутся из --paths (через запятую), а не молча из зоны. Пересечение
    #     с защищенными префиксами без [pipeline]-маркера — отказ ДО side
    #     effects (reservation/worktree/goal не создаются).
    if protected_hits and not args.pipeline_marker:
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "prepare",
            "correlation_id": correlation, "prepared": False,
            "reason": J3_PROTECTED_REASON,
            "protected_paths": protected_hits,
            "protected_prefixes": list(J3_PROTECTED_PREFIXES),
            "note": "изменение защищенных путей ("
                    + ", ".join(protected_hits)
                    + ") требует [pipeline]-маркер в subject (J3; "
                      "pm_bounds_check; ТЗ 03 п.5) — reservation не "
                      "создавалась; --paths — явный список путей изменения "
                      "для protected-paths ворот (Н14)",
        }, args.as_json)
        return 1

    # 1b) P0.3: запрос пути вне политики роли — отказ ДО side effects
    #     (записи в реестре/state нет, reservation не создается).
    if policy_violations:
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "prepare",
            "correlation_id": correlation, "prepared": False,
            "reason": ZONE_OUTSIDE_POLICY,
            "role": args.role,
            "requested_zones": zp["requested"],
            "zone_violations": policy_violations,
            "policy_version": policy_version,
            "policy_source": zp["policy_source"],
            "note": "запрошенные пути вне зоны записи роли "
                    f"«{args.role}» (политика: {rzp.POLICY_SOURCE}, таблица "
                    "«Зона записи (только она)»; CLI может только сузить "
                    "политику) — отклонены пути: "
                    f"{', '.join(policy_violations)}; reservation не создавалась",
        }, args.as_json)
        return 1

    # 2) dry-run: только показать план, без side effects (ТЗ 06).
    if args.dry_run:
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "prepare",
            "dry_run": True, "correlation_id": correlation,
            "adapter": ADAPTER, "prepared": decision.allowed,
            "mode": flow_mode.get_mode(),
            **plan,
            "field_diagnostics": amb_hints,
            "note": "dry-run: snapshot/reservation/worktree/goal НЕ созданы",
        }, args.as_json)
        return _status_exit(decision.status)

    # 4) Идемпотентность по correlation ID (до side effects).
    state_path = Path(args.state) if args.state else default_state_path(
        args.registry)
    state = state_load(state_path)
    fingerprint = gr.canonical_digest({
        "action": action.to_dict(),
        "scope": snapshot["scope"],
        "zones": sorted(zones),
        "owner_pm": args.owner_pm,
        "worktree": args.worktree, "branch": derived_branch,
    })
    existing = state["runs"].get(correlation)
    if existing is not None:
        if existing.get("request_fingerprint") == fingerprint:
            _emit({
                "schema_version": OUTPUT_SCHEMA, "command": "prepare",
                "correlation_id": correlation, "prepared": True,
                "idempotent": True, "adapter": ADAPTER,
                "record": existing,
                "note": "prepare уже выполнен для этого correlation ID — "
                        "повторная reservation не требуется",
            }, args.as_json)
            return 0
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "prepare",
            "correlation_id": correlation, "prepared": False,
            "reason": "DUPLICATE_PAYLOAD",
            "note": "correlation ID уже занят другим payload",
        }, args.as_json)
        return 1

    # 4a) Не разрешено — никаких side effects исполнения (worktree/
    #     reservation/goal не создаются). Запись цикла с decision сохраняется
    #     (state — вспомогательный, не реестр): она — источник blocks_on для
    #     run при последующем включении enforcing (решение Заказчика
    #     2026-10-02: в enforcing DENY/UNKNOWN останавливают и подготовку —
    #     note с mode/reason это называет явно; в shadow решение вычислено
    #     и видно в status, но исполнение не разрешено, как и раньше).
    if decision.status != ft.ALLOW:
        record = {
            "correlation_id": correlation,
            "delegation_id": delegation_id,
            "adapter": ADAPTER,
            "status": "prepared",
            "action": args.action,
            "actor_role": args.role,
            "task_id": args.task,
            "scope": snapshot["scope"],
            "prepared_digest": snapshot["snapshot_digest"],
            "zones": zones,
            "policy_version": policy_version,
            "worktree": None,
            "branch": derived_branch,
            "goal_path": None,
            "registry": str(registry_path),
            "owner_pm": args.owner_pm,
            "approval_ref": args.approval_ref,
            "decision": decision.to_dict(),
            "request_fingerprint": fingerprint,
            "created_at": gr.utcnow_iso(),
        }

        def _mutate(st: dict) -> None:
            st["runs"][correlation] = record

        state_write_locked(state_path, state, _mutate)
        if flow_mode.blocks_on_status(decision.status):
            _emit({
                "schema_version": OUTPUT_SCHEMA, "command": "prepare",
                "correlation_id": correlation, "prepared": False,
                "mode": flow_mode.get_mode(), "reason": decision.status,
                "field_diagnostics": amb_hints,
                "record": record,
                "note": "enforcing: решение "
                        f"{decision.status} запрещает подготовку (blocks_on)"
                        " — reservation/worktree/goal не создавались",
            }, args.as_json)
        else:
            _emit({
                "schema_version": OUTPUT_SCHEMA, "command": "prepare",
                "correlation_id": correlation, "prepared": False,
                "field_diagnostics": amb_hints,
                "record": record,
                "note": "действие не разрешено (ALLOW обязателен) — "
                        "reservation/worktree/goal не создавались",
            }, args.as_json)
        return _status_exit(decision.status)

    # 5) Worktree (реальный side effect — до reservation, чтобы записать путь;
    #    при отказе reservation worktree НЕ удаляется молча — разбор ПМ).
    #    J51.1 (worktree-per-delegation), деградация: если git worktree
    #    недоступен (session_worktree.sh вернул ошибку), делегация НЕ
    #    проваливается — ALLOW с WARN (warnings в payload/записи, worktree
    #    None, работа в общем дереве repo). FAIL был бы ложным блокером
    #    параллельной работы; деградация честно видна в ALLOW-объекте.
    worktree = None
    worktree_warning = None
    if args.worktree:
        worktree = str(Path(args.worktree).resolve())
        if sc.git_toplevel(Path(worktree)) is None:
            print(f"FLOWCTL-ERROR: --worktree не git-дерево: {worktree}",
                  file=sys.stderr)
            return 2
    elif args.create_worktree:
        branch = derived_branch or branch_name_for(correlation)
        proc = subprocess.run(
            ["bash", str(SCRIPTS / "session_worktree.sh"), "create",
             str(repo), delegation_id, branch, _git_head(repo)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            worktree_warning = (
                "WORKTREE_UNAVAILABLE: git worktree не создан "
                f"({proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else 'без диагностики'})"
                f" — делегация работает в общем дереве {repo} "
                "(деградация J51.1: WARN в ALLOW, не FAIL; при параллельных "
                "делегациях того же repo энфорсер требует разные worktree)")
            print(f"FLOWCTL-WARNING: {worktree_warning}", file=sys.stderr)
        else:
            worktree = proc.stdout.strip().splitlines()[-1]
    warnings = [worktree_warning] if worktree_warning else []

    # 6) Атомарная reservation зоны (поставка 04).
    base_tree = Path(worktree) if worktree else repo
    res = sc.reserve({
        "repo": str(repo),
        "delegation_id": delegation_id,
        "role": args.role,
        "project": args.project,
        "owner_pm": args.owner_pm,
        "paths": zones,
        "policy_version": policy_version,
        "worktree": worktree,
        "branch": derived_branch,
        "base_sha": _git_head(base_tree),
        "snapshot_digest": snapshot["snapshot_digest"],
    }, registry_path)
    if not res["allowed"]:
        # Н7: ZONE_CONFLICT с finished-сессией, вердикт которой returned
        # (работа не влита) — зона держится мертвой записью. В диагностике —
        # перечень таких держателей с готовой командой авто-освобождения;
        # флаг --release-stale разрешает автоматический reconcile (release +
        # повторная резервация) прямо в этом prepare.
        stale_holders: list[dict] = []
        released_stale: list[str] = []
        if res.get("reason") == sc.ZONE_CONFLICT:
            stale_holders = find_stale_returned_holders(
                registry_path, state_path, zones)
            if stale_holders and getattr(args, "release_stale", False):
                for holder in stale_holders:
                    tr = registry_transition(
                        registry_path, holder["delegation_id"], "released",
                        "prepare --release-stale: finished-сессия с вердиктом "
                        "returned (работа не влита) держит зону — "
                        "авто-освобождение (Н7)",
                        f"prepare {correlation}: авто-reconcile держателя "
                        f"{holder['delegation_id']}",
                        expect_from="finished")
                    if not tr.get("ok"):
                        continue
                    released_stale.append(holder["delegation_id"])
                    cid_h = holder["correlation_id"]

                    def _mutate_release(st: dict, _cid: str = cid_h) -> None:
                        r = st["runs"].get(_cid)
                        if r is None:
                            return
                        r["status"] = "released"
                        r.setdefault("lifecycle", {}).setdefault(
                            "history", []).append({
                                "at": gr.utcnow_iso(), "from": "returned",
                                "to": "released",
                                "reason": "prepare --release-stale (Н7): "
                                          "зона освобождена новым prepare",
                                "evidence": f"prepare {correlation}"})

                    state_write_locked(
                        state_path, state_load(state_path), _mutate_release)
                if released_stale:
                    res = sc.reserve({
                        "repo": str(repo),
                        "delegation_id": delegation_id,
                        "role": args.role,
                        "project": args.project,
                        "owner_pm": args.owner_pm,
                        "paths": zones,
                        "policy_version": policy_version,
                        "worktree": worktree,
                        "branch": derived_branch,
                        "base_sha": _git_head(base_tree),
                        "snapshot_digest": snapshot["snapshot_digest"],
                    }, registry_path)
        if not res["allowed"]:
            _emit({
                "schema_version": OUTPUT_SCHEMA, "command": "prepare",
                "correlation_id": correlation, "prepared": False,
                "reservation": res,
                "stale_holders": stale_holders,
                "released_stale": released_stale,
                "note": "reservation отклонена — сессия не создалась"
                        + ("; зону держат finished-сессии с вердиктом "
                           "returned (работа не влита) — освободи явным "
                           "release (команды в stale_holders) или повтори "
                           "prepare с --release-stale (Н7)"
                           if stale_holders else ""),
            }, args.as_json)
            return 1
        if released_stale:
            warnings.append(
                "RELEASED_STALE (Н7): авто-освобождены finished-сессии с "
                "вердиктом returned, державшие зону: "
                + ", ".join(released_stale))

    # 7) Переснимок ПОСЛЕ reservation: registry_digest входит в snapshot
    #    digest; run сравнивается с этим «осевшим» значением (иначе ложный
    #    STALE_SNAPSHOT на собственную reservation).
    snapshot2 = flow_state.inspect(
        repo_arg=str(repo), project=args.project, flow=args.flow,
        change_id=args.change, task_id=args.task, registry=args.registry,
    )
    decision2 = ft.check_action(snapshot2, action)
    if decision2.status != ft.ALLOW:
        # Крайне редкий случай (чужая параллельная запись между резервацией и
        # переснимком): честный отказ, reservation сохранена для разбора.
        # Запись цикла с decision2 — источник blocks_on для run при
        # последующем включении enforcing (та же семантика, что в п.8).
        record = {
            "correlation_id": correlation,
            "delegation_id": delegation_id,
            "adapter": ADAPTER,
            "status": "prepared",
            "action": args.action,
            "actor_role": args.role,
            "task_id": args.task,
            "scope": snapshot2["scope"],
            "prepared_digest": snapshot2["snapshot_digest"],
            "zones": zones,
            "policy_version": policy_version,
            "worktree": worktree,
            "branch": derived_branch,
            "goal_path": None,
            "registry": str(registry_path),
            "owner_pm": args.owner_pm,
            "approval_ref": args.approval_ref,
            "decision": decision2.to_dict(),
            "request_fingerprint": fingerprint,
            "created_at": gr.utcnow_iso(),
        }

        def _mutate(st: dict) -> None:
            st["runs"][correlation] = record

        state_write_locked(state_path, state, _mutate)
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "prepare",
            "correlation_id": correlation, "prepared": False,
            "record": record,
            "note": "после reservation решение перестало быть ALLOW — "
                    "сессия не готовится, reservation оставлена для разбора",
        }, args.as_json)
        return _status_exit(decision2.status)

    # 9) Goal-артефакт (без секретов; подготовка ≠ запуск).
    goal_path = Path(args.goal_path) if args.goal_path else (
        state_path.parent / f"goal-{correlation}.md")
    goal_path.parent.mkdir(parents=True, exist_ok=True)
    # 10) Запись цикла (state): решение последней проверки — источник для
    # enforcement на run (blocks_on). reservation уже создана; запись
    # prepare без нее не имеет смысла, поэтому отказ здесь не делаем.
    record = {
        "correlation_id": correlation,
        "delegation_id": delegation_id,
        "adapter": ADAPTER,
        "status": "prepared",
        "action": args.action,
        "actor_role": args.role,
        "task_id": args.task,
        "scope": snapshot2["scope"],
        "prepared_digest": snapshot2["snapshot_digest"],
        "zones": zones,
        "policy_version": policy_version,
        "requested_zones": zp["requested"],
        "zone_violations": policy_violations,
        "worktree": worktree,
        "warnings": warnings,
        "branch": derived_branch,
        "goal_path": str(goal_path),
        "registry": str(registry_path),
        "owner_pm": args.owner_pm,
        "approval_ref": args.approval_ref,
        "decision": decision2.to_dict(),
        "request_fingerprint": fingerprint,
        "created_at": gr.utcnow_iso(),
    }
    goal_path.write_text(
        build_goal_text(record, decision2, registry_path), encoding="utf-8")
    record["goal_digest"] = gr.sha256_text(
        goal_path.read_text(encoding="utf-8"))

    def _mutate(st: dict) -> None:
        st["runs"][correlation] = record

    state_write_locked(state_path, state, _mutate)
    if args.audit:
        gr.append_audit_event(args.audit, "reservation", correlation,
                              scope=record["scope"],
                              delegation_id=delegation_id,
                              zones=zones, adapter=ADAPTER,
                              policy_version=policy_version,
                              prepared_digest=record["prepared_digest"])
    _emit({
        "schema_version": OUTPUT_SCHEMA, "command": "prepare",
        "correlation_id": correlation, "prepared": True,
        "idempotent": bool(res.get("idempotent")), "adapter": ADAPTER,
        "warnings": warnings,
        "record": record, "reservation": res.get("session"),
        "note": "session/worktree/goal подготовлены; ЗАПУСКА НЕТ "
                "(adapter=manual): исполни delegate_task внешне с goal-"
                "файлом, затем flowctl run --correlation-id "
                f"{correlation} отметит старт"
                + ("; WARN: " + warnings[0] if warnings else ""),
    }, args.as_json)
    return 0


# -------------------------------------------------------------------- run


def cmd_run(args) -> int:
    state_path = Path(args.state) if args.state else default_state_path(
        args.registry)
    state = state_load(state_path)
    record = state["runs"].get(args.correlation_id)
    if record is None:
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "run",
            "correlation_id": args.correlation_id, "started": False,
            "reason": sc.MISSING_INPUT,
            "note": "нет записи prepare для этого correlation ID — run "
                    "разрешен только после prepare (реальная reservation)",
        }, args.as_json)
        return 2
    if record["status"] == "running":
        # Идемпотентность: вторая сессия не создается, вызов не повторяется.
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "run",
            "correlation_id": args.correlation_id, "started": False,
            "idempotent": True, "record": record,
            "note": "run уже выполнен для этого correlation ID — вторая "
                    "сессия не создается, делегация НЕ повторяется",
        }, args.as_json)
        return 0
    if record["status"] != "prepared":
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "run",
            "correlation_id": args.correlation_id, "started": False,
            "record": record,
            "note": f"статус {record['status']!r} не допускает запуск — "
                    "разбор через status/reconcile и явное решение ПМ",
        }, args.as_json)
        return 1

    # 0) Enforcement (решение Заказчика 2026-10-02): в enforcing DENY/
    # UNKNOWN из prepare останавливают запуск — отказ с кодом причины,
    # статус не меняется, делегация не стартует. В shadow решение уже
    # вычислено (запись prepared существует), но не исполняется.
    prepared_status = (record.get("decision") or {}).get("status")
    if prepared_status and flow_mode.blocks_on_status(prepared_status):
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "run",
            "correlation_id": args.correlation_id, "started": False,
            "mode": flow_mode.get_mode(), "reason": prepared_status,
            "decision_status": prepared_status, "record": record,
            "note": "enforcing: решение prepare "
                    f"{prepared_status} останавливает запуск (blocks_on) — "
                    "статус не изменен, делегация не стартовала; в shadow "
                    "этот run был бы разрешен",
        }, args.as_json)
        return 1 if prepared_status == ft.DENY else 2

    registry_path = Path(record["registry"])
    scope = record["scope"]
    # Переснимок непосредственно перед стартом (ТЗ 06: STALE_SNAPSHOT).
    try:
        snapshot = flow_state.inspect(
            repo_arg=scope["repo"], project=scope["project"],
            flow=scope["flow"], change_id=scope["change"],
            task_id=scope.get("task"), registry=record["registry"],
        )
    except ValueError as exc:
        print(f"FLOWCTL-ERROR: {exc}", file=sys.stderr)
        return 2
    if snapshot["snapshot_digest"] != record["prepared_digest"]:
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "run",
            "correlation_id": args.correlation_id, "started": False,
            "reason": ft.STALE_SNAPSHOT,
            "expected_digest": record["prepared_digest"],
            "actual_digest": snapshot["snapshot_digest"],
            "note": "снимок устарел между prepare и run — запуск запрещен, "
                    "статус не изменен; повтори prepare (контракт §6)",
        }, args.as_json)
        return 1

    tr = registry_transition(
        registry_path, record["delegation_id"], "running",
        "flowctl run: действие разрешено, старт делегации разрешен "
        f"(adapter={ADAPTER}; факт запуска фиксирует внешний исполнитель)",
        f"state: {state_path}; goal: {record['goal_path']}")
    if not tr["ok"] and not record.get("goal_path"):
        # DENY/UNKNOWN-prepare в реестре не резервировался: запуск невозможен
        # по определению (нет reservation) — в shadow честный отказ без
        # делегации, в enforcing сюда не доходим (blocks_on выше).
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "run",
            "correlation_id": args.correlation_id, "started": False,
            "reason": tr["reason"],
            "decision_status": (record.get("decision") or {}).get("status"),
            "note": "запись prepare без reservation (решение "
                    f"{(record.get('decision') or {}).get('status')} не "
                    "разрешало подготовку) — запуск невозможен; повтори "
                    "prepare с новым correlation ID после устранения причин",
        }, args.as_json)
        return 2
    if not tr["ok"]:
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "run",
            "correlation_id": args.correlation_id, "started": False,
            "reason": tr["reason"], "details": tr.get("details", []),
        }, args.as_json)
        return 2

    def _mutate(st: dict) -> None:
        rec = st["runs"][args.correlation_id]
        rec["status"] = "running"
        rec["started_at"] = gr.utcnow_iso()
        record.update(rec)

    state_write_locked(state_path, state, _mutate)
    if args.audit:
        gr.append_audit_event(args.audit, "agent_started",
                              args.correlation_id, scope=scope,
                              delegation_id=record["delegation_id"],
                              adapter=ADAPTER, goal_path=record["goal_path"],
                              goal_digest=record.get("goal_digest"))
    _emit({
        "schema_version": OUTPUT_SCHEMA, "command": "run",
        "correlation_id": args.correlation_id, "started": True,
        "idempotent": False, "record": record,
        "note": "статус running = разрешение и отметка старта; adapter=manual "
                "— исполнение delegate_task ВНЕШНЕЕ, этот статус не является "
                "фактом работы агента; timeout/crash продвигают только "
                "через finish/reconcile, повторный run не повторяет вызов",
    }, args.as_json)
    return 0


# ------------------------------------------------------------------ finish


def cmd_finish(args) -> int:
    state_path = Path(args.state) if args.state else default_state_path(
        args.registry)
    state = state_load(state_path)
    record = state["runs"].get(args.correlation_id)
    if record is None:
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "finish",
            "correlation_id": args.correlation_id,
            "reason": sc.MISSING_INPUT,
            "note": "нет записи для этого correlation ID",
        }, args.as_json)
        return 2
    if record["status"] in RECORD_CLOSED:
        # Идемпотентность: вердикт уже вынесен и хранится в state.
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "finish",
            "correlation_id": args.correlation_id,
            "verdict": record["status"], "idempotent": True,
            "defects": record.get("defects", []),
            "note": "вердикт уже вынесен; повтор не выполняется",
        }, args.as_json)
        return {"accepted": 0, "returned": 1}[record["status"]]
    if record["status"] not in ("running", "provisional"):
        # Повторная приемка blocked-сессии — только по явному решению ПМ
        # (--pm-resume): сценарий «blocked по конфигурационной ошибке
        # запускающего» (например, первый finish без --pm-mode — gate ERROR
        # «режим не задан», сам агент и его отчет без дефектов). Агентский
        # вердикт не выносился, прогон воротов не был по существу — повтор
        # finish с корректной конфигурацией честен. FAIL/zone-дефекты агент
        # не обходит: --pm-resume не принимается, если в записи есть
        # дефекты по существу (FAIL gates, ZONE_*).
        resume_requested = bool(getattr(args, "pm_resume", False))
        prev_defects = record.get("defects") or []
        config_only = bool(prev_defects) and all(
            d.get("code") in ("ERROR", "SKIPPED") and d.get("source") == "gate"
            for d in prev_defects)
        if record["status"] == "blocked" and resume_requested and config_only:
            def _mutate_resume(st: dict) -> None:
                r = st["runs"][args.correlation_id]
                r["status"] = "running"
                r.setdefault("lifecycle", {}).setdefault("history", []).append({
                    "at": gr.utcnow_iso(),
                    "from": "blocked", "to": "running",
                    "reason": "ПМ: --pm-resume — предыдущий blocked был "
                              "конфигурационной ошибкой запускающего (gate "
                              "ERROR без дефектов по существу); повтор finish",
                })
            state_write_locked(state_path, state, _mutate_resume)
        else:
            # Timeout/crash/reconcile не продвигают на следующий шаг.
            _emit({
                "schema_version": OUTPUT_SCHEMA, "command": "finish",
                "correlation_id": args.correlation_id, "verdict": "blocked",
                "record": record,
                "note": f"статус {record['status']!r}: приемка невозможна — "
                        "разбор (status/reconcile) и явное решение ПМ "
                        "(--pm-resume — только для blocked по gate ERROR без "
                        "дефектов по существу); сессия и worktree сохранены",
            }, args.as_json)
            return 2

    report_path = Path(args.report) if args.report else None
    if report_path is None or not report_path.is_file():
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "finish",
            "correlation_id": args.correlation_id, "verdict": "blocked",
            "note": "агентский отчет (указатель на файлы) не найден — "
                    "приемка без отчета не проводится",
        }, args.as_json)
        return 2

    registry_path = Path(record["registry"])
    worktree = Path(record["worktree"]) if record.get("worktree") else Path(
        record["scope"]["repo"])

    # 0) P0.2: обязательные gates — из политики (DEFAULT_GATES точки запуска
    #    + машинные ворота этапа из STAGE_TABLE), НЕ из флагов. --gates может
    #    только ДОБАВИТЬ проверки; попытка заменить обязательный набор — отказ
    #    с перечнем недостающих обязательных. --diagnostic — прогон
    #    сокращенного списка (DIAGNOSTIC_GATES) без права accepted.
    required = list(required_gates_for(
        record["scope"], record.get("action"), args.gate_scope))
    requested = ([g.strip() for g in args.gates.split(",") if g.strip()]
                 if args.gates else [])
    unknown = [g for g in requested if g not in gr.KNOWN_GATES]
    if unknown:
        print(f"FLOWCTL-ERROR: неизвестные gates: {', '.join(unknown)} "
              f"(доступны: {', '.join(gr.KNOWN_GATES)})", file=sys.stderr)
        return 2
    if args.diagnostic:
        if requested:
            print("FLOWCTL-ERROR: --diagnostic не сочетается с --gates "
                  "(диагностический список фиксирован политикой: "
                  f"{', '.join(DIAGNOSTIC_GATES)})", file=sys.stderr)
            return 2
        gates = [str(g) for g in DIAGNOSTIC_GATES]
        missing_required = [g for g in required if g not in gates]
    else:
        gates = list(dict.fromkeys(required + requested))
        missing_required = []
        # Явный отказ, если вызывающий пытался СУЗИТЬ набор относительно
        # политики (достижимо только когда в будущем изменят точку запуска
        # после prepare — защита от регрессии семантики флага).
        if requested and set(requested) != set(required) \
                and set(requested).issubset(set(required)):
            print("FLOWCTL-ERROR: --gates не может заменить обязательные "
                  f"gates ({', '.join(required)}) — недостающие обязательные: "
                  f"{', '.join(g for g in required if g not in requested)} "
                  "(P0.2: флаг только добавляет проверки)", file=sys.stderr)
            return 1

    # 1) Зона (P0.3): сверка фактического diff с СУЖЕННОЙ (политико-валидной)
    #    зоной из резервации, а не с исходной просьбой --path. Резервация
    #    создавалась уже суженной (prepare), поэтому sc.check сверяет с ней;
    #    дополнительно контролируем, что редакция политики в резервации —
    #    та же, что вычислил бы prepare сейчас (аудит policy_version), и что
    #    зона резервации по-прежнему валидна текущей политикой роли.
    zone_res = sc.check({"delegation_id": record["delegation_id"],
                         "repo": str(worktree)}, registry_path)

    # 3) Вердикт по фактам, не по самоотчету агента.
    #    (P0.3: zone-дефекты собираются здесь же — из суженной зоны резервации
    #    и сверки policy_version; gates исполняются в п.2 ниже.)
    defects = []
    reg = sc.registry_load(registry_path)[0]
    session = next(
        (s for s in reg.get("sessions", [])
         if isinstance(s, dict)
         and s.get("delegation_id") == record["delegation_id"]), None)
    stored_zones = [sc.canonical(p) for p in
                    ((session or {}).get("zones")
                     or (session or {}).get("paths") or [])]
    stored_policy_version = (session or {}).get("policy_version")
    # P0.3: зона резервации должна подтверждаться политикой роли —
    # сверяется именно суженная (политико-валидная) зона, а не исходная
    # просьба --path; расхождение редакции политики — дефект аудита.
    current_policy = rzp.narrow_zones(record.get("actor_role"), stored_zones)
    policy_stale = bool(stored_policy_version) and \
        stored_policy_version != rzp.policy_version()
    policy_invalid = bool(stored_zones) and (
        not current_policy["zones"] or current_policy["violations"])
    if policy_stale or policy_invalid:
        defects.append({
            "source": "zone", "code": "ZONE_POLICY_MISMATCH",
            "detail": "зона резервации не подтверждается текущей политикой "
                      f"роли «{record.get('actor_role')}» (policy_version "
                      f"резервации: {stored_policy_version or '—'}; текущая: "
                      f"{rzp.policy_version()}; непокрытые запросы: "
                      f"{', '.join(current_policy['violations']) or '—'}); "
                      "политика: " + rzp.POLICY_SOURCE,
        })

    # 2) Gates: реальные ворота фазы (поставка 05) на рабочем дереве.
    opts = argparse.Namespace(
        openspec_cmd=args.openspec_cmd, timeout=args.timeout,
        pm_mode=args.pm_mode, pm_commits=args.pm_commits,
        pm_range=args.pm_range, pm_registry=args.pm_registry,
        pm_require_review=args.pm_require_review, pr_id=args.pr_id,
        log_dir=args.log_dir or str(state_path.parent / "gate-logs"),
        report_dir=args.report_dir or str(state_path.parent / "gate-reports"),
        audit=args.audit, correlation_id=args.correlation_id,
    )
    gate_report, gate_exit = gr.run_gates(
        worktree, args.gate_scope, gates, opts)

    for v in zone_res.get("violations", []):
        defects.append({"source": "zone", "code": v.split(":")[0], "detail": v})
    if zone_res.get("reason") == sc.REGISTRY_ERROR:
        defects.append({"source": "zone", "code": sc.REGISTRY_ERROR,
                        "detail": zone_res["details"][0]})
    for g in gate_report.get("gates", []):
        if g["status"] != gr.STATUS_PASS:
            defects.append({"source": "gate", "code": g["status"],
                            "detail": f"{g['gate_id']}: {g['diagnostics']}"})
    if gate_report.get("skipped_gates"):
        for s in gate_report["skipped_gates"]:
            defects.append({"source": "gate", "code": "SKIPPED",
                            "detail": f"{s['gate_id']}: {s['reason']}"})
    if gr.is_stale(gate_report, worktree):
        defects.append({"source": "gate", "code": "STALE",
                        "detail": "HEAD изменился после gate-отчета — повтор обязателен"})

    # P0.2: диагностический прогон НЕ дает accepted ни при каком исходе.
    if args.diagnostic:
        if missing_required:
            defects.append({
                "source": "gate", "code": "MISSING_REQUIRED_GATES",
                "detail": "диагностический прогон выполнен без обязательных "
                          f"gates: {', '.join(missing_required)} — "
                          "accepted недостижим до полного прогона",
            })

    if zone_res.get("reason") == sc.REGISTRY_ERROR \
            or gate_report["overall"] == gr.STATUS_ERROR:
        verdict = "blocked"
    elif args.diagnostic:
        # Прогон сокращенного списка: вердикт diagnostic_only (не accepted),
        # сессия НЕ закрывается — run/finish повторяются после разбора.
        verdict = "diagnostic_only"
    elif zone_res["ok"] and gate_exit == 0 and not any(
            d["code"] == "ZONE_POLICY_MISMATCH" for d in defects):
        # P0.3: зона резервации подтверждена политикой роли — обязательное
        # условие accepted наравне с diff⊆зона и gates.
        verdict = "accepted"
    else:
        verdict = "returned"

    # Н6: pr_validate SKIPPED (нет PR-контекста: ни --pr-id, ни
    # GITHUB_EVENT_PATH) — вердикт НЕ финализируется. SKIPPED не считается
    # пройденным, но и дефектом работы агента не является: сессия остается
    # открытой (provisional), повторный finish с появившимся --pr-id
    # пересчитывает ворота и финализирует вердикт.
    pr_skipped = [g for g in gate_report.get("gates", [])
                  if g.get("gate_id") == "pr_validate"
                  and g.get("status") == gr.STATUS_SKIPPED]
    provisional_reason = None
    if pr_skipped and verdict in ("accepted", "returned"):
        verdict = "provisional"
        provisional_reason = (
            "pr_validate SKIPPED (нет PR-контекста: --pr-id / "
            "GITHUB_EVENT_PATH) — вердикт не финализируется (Н6); "
            "повторный finish с --pr-id пересчитает и финализирует")

    executed_gates = {
        g["gate_id"]: {"status": g["status"], "exit_code": g["exit_code"],
                       "digest": g["input_digest"],
                       "executed": g["executed"]}
        for g in gate_report.get("gates", [])
    }
    gates_digest = gr.canonical_digest(executed_gates)

    if verdict == "diagnostic_only":
        # Только фиксация в state: сессия НЕ закрывается (статус running
        # сохранен, реестр не тронут) — после разбора возможен штатный
        # полный finish, но accepted этим прогоном не выносится.
        def _mutate(st: dict) -> None:
            rec = st["runs"][args.correlation_id]
            rec["diagnostic"] = {
                "at": gr.utcnow_iso(),
                "verdict": "diagnostic_only",
                "required_gates": required,
                "missing_required": missing_required,
                "gates": {"overall": gate_report["overall"],
                          "report_ref": gate_report.get("report_ref")},
                "gates_executed": executed_gates,
                "gates_digest": gates_digest,
            }
            record.update(rec)

        state_write_locked(state_path, state, _mutate)
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "finish",
            "correlation_id": args.correlation_id,
            "verdict": "diagnostic_only", "diagnostic": True,
            "required_gates": required,
            "missing_required_gates": missing_required,
            "gates_executed": executed_gates, "gates_digest": gates_digest,
            "defects": defects, "record": record,
            "note": "диагностический прогон сокращенного списка НЕ дает "
                    "accepted (P0.2): сессия не закрыта, обязательные gates "
                    f"({', '.join(required)}) подлежат полному прогону; "
                    "вердикт выносит ПМ после разбора",
        }, args.as_json)
        return 2

    if verdict == "provisional":
        # Н6: вердикт НЕ финализируется — сессия остается открытой (running
        # в реестре, зона удерживается), state хранит provisional-снимок.
        # Повторный finish (с --pr-id или после появления PR-контекста)
        # перезапускает ворота и выносит финальный вердикт.
        def _mutate_prov(st: dict) -> None:
            rec = st["runs"][args.correlation_id]
            rec["status"] = "provisional"
            rec["provisional"] = {
                "at": gr.utcnow_iso(),
                "reason": provisional_reason,
                "skipped_gates": [g.get("gate_id") for g in pr_skipped],
                "gates_overall": gate_report["overall"],
                "defects": defects,
            }
            record.update(rec)

        state_write_locked(state_path, state, _mutate_prov)
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "finish",
            "correlation_id": args.correlation_id,
            "verdict": "provisional", "provisional": True,
            "skipped_gates": [g.get("gate_id") for g in pr_skipped],
            "gates_executed": executed_gates, "gates_digest": gates_digest,
            "defects": defects, "record": record,
            "note": (provisional_reason or "pr_validate SKIPPED")
                    + "; сессия НЕ закрыта (running в реестре) — повтори "
                      "finish с --pr-id <N>",
        }, args.as_json)
        return 2

    new_state = ("finished" if verdict in ("accepted", "returned")
                 else "needs_attention")
    tr = registry_transition(
        registry_path, record["delegation_id"], new_state,
        f"flowctl finish: {verdict}",
        f"zone={zone_res['reason']}; gates={gate_report['overall']}; "
        f"report={report_path}")
    if not tr["ok"]:
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "finish",
            "correlation_id": args.correlation_id, "verdict": "blocked",
            "reason": tr["reason"], "details": tr.get("details", []),
        }, args.as_json)
        return 2

    def _mutate(st: dict) -> None:
        rec = st["runs"][args.correlation_id]
        rec["status"] = verdict
        rec["finished_at"] = gr.utcnow_iso()
        rec["verdict"] = {
            "verdict": verdict,
            "zone": {"ok": zone_res["ok"], "reason": zone_res["reason"],
                     "violations": zone_res.get("violations", []),
                     "zones": stored_zones,
                     "policy_version": stored_policy_version,
                     "policy_confirmed": not (policy_stale or policy_invalid)},
            "gates": {"overall": gate_report["overall"],
                      "report_ref": gate_report.get("report_ref"),
                      "executed": executed_gates,
                      "digest": gates_digest},
            "report": {"path": str(report_path),
                       "digest": gr.sha256_text(
                           report_path.read_text(encoding="utf-8",
                                                 errors="replace"))},
        }
        rec["defects"] = defects
        record.update(rec)

    state_write_locked(state_path, state, _mutate)
    if args.audit:
        event = ("accepted" if verdict == "accepted"
                 else "returned" if verdict == "returned" else "recovery")
        gr.append_audit_event(args.audit, event, args.correlation_id,
                              scope=record["scope"],
                              delegation_id=record["delegation_id"],
                              verdict=verdict,
                              zone_reason=zone_res["reason"],
                              gates_overall=gate_report["overall"],
                              defects=defects)
    _emit({
        "schema_version": OUTPUT_SCHEMA, "command": "finish",
        "correlation_id": args.correlation_id, "verdict": verdict,
        "required_gates": required,
        "gates_executed": executed_gates, "gates_digest": gates_digest,
        "defects": defects, "record": record,
        "note": "push/merge/deploy НЕ выполняются — только локальный вердикт; "
                + ("следующий шаг (merge/review) — явное решение ПМ"
                   if verdict == "accepted" else
                   "исправь дефекты и повтори цикл с новым correlation ID"),
    }, args.as_json)
    return {"accepted": 0, "returned": 1, "blocked": 2}[verdict]


# ------------------------------------------------------------------ status


# ----------------------------------------------------------------- phase
# J43.1: память фазы. Читает flowctl_state.json и возвращает последний run
# заданного scope — {action, actor_role, finished_status, timestamp} — или
# {phase: 'none'}. Дополнительно completed_actions — действия scope,
# завершенные (accepted/returned): их использует опциональная проверка
# последовательности энфорсера (FLOW_ENFORCER_SEQUENCE=1).


def cmd_phase(args) -> int:
    state_path = Path(args.state) if args.state else default_state_path(
        args.registry)
    state = state_load(state_path)
    try:
        repo_resolved = str(Path(args.repo).resolve(strict=True))
    except OSError:
        repo_resolved = str(args.repo)
    best = None
    best_ts = ""
    completed: list[str] = []
    for rec in state.get("runs", {}).values():
        if not isinstance(rec, dict):
            continue
        sc = rec.get("scope") or {}
        if sc.get("change") != args.scope or sc.get("flow") != args.flow:
            continue
        if args.project and sc.get("project") != args.project:
            continue
        rec_repo = sc.get("repo")
        if rec_repo and str(Path(rec_repo)) != repo_resolved:
            continue
        ts = rec.get("finished_at") or rec.get("created_at") or ""
        if best is None or ts > best_ts:
            best, best_ts = rec, ts
        if rec.get("status") in PHASE_COMPLETED_STATUSES:
            completed.append(rec.get("action") or "")
    payload = {
        "schema_version": PHASE_SCHEMA, "command": "phase",
        "repo": repo_resolved, "project": args.project,
        "flow": args.flow, "scope": args.scope,
    }
    if best is None:
        payload["phase"] = "none"
    else:
        payload["phase"] = {
            "action": best.get("action"),
            "actor_role": best.get("actor_role"),
            "finished_status": best.get("status"),
            "timestamp": best_ts,
        }
        payload["completed_actions"] = sorted({a for a in completed if a})
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_status(args) -> int:
    state_path = Path(args.state) if args.state else default_state_path(
        args.registry)
    state = state_load(state_path)
    runs = [r for cid, r in sorted(state["runs"].items())
            if not args.correlation_id or cid == args.correlation_id]
    registry_path = Path(args.registry) if args.registry else (
        Path.home() / ".hermes" / "state" / "active_sessions.json")
    reg = sc.status(registry_path)
    _emit({
        "schema_version": OUTPUT_SCHEMA, "command": "status",
        "runs": runs,
        "sessions": reg.get("sessions", []),
        "registry_ok": reg.get("ok"),
        "registry_error": reg.get("details") or None,
        "note": "факты из реестра и state; восстановление — из фактов "
                "(контракт §13), state — вспомогателен",
    }, args.as_json)
    if args.correlation_id:
        return 0 if runs else 1
    return 0


# --------------------------------------------------------------- reconcile


def cmd_reconcile(args) -> int:
    registry_path = Path(args.registry) if args.registry else (
        Path.home() / ".hermes" / "state" / "active_sessions.json")
    state_path = Path(args.state) if args.state else default_state_path(
        args.registry)
    res = sc.reconcile(
        registry_path,
        repo=Path(args.repo) if args.repo else None,
        delegation_id=args.delegation_id,
    )
    if not res.get("ok"):
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "reconcile",
            "reason": res.get("reason"), "details": res.get("details", []),
        }, args.as_json)
        return 2
    # Синхронизация state-записей с фактами реестра (ничего не удаляется).
    by_delegation = {r.get("delegation_id"): r for r in res.get("results", [])}
    touched = []

    def _mutate(st: dict) -> None:
        for rec in st["runs"].values():
            if rec.get("status") not in ("prepared", "running"):
                continue
            match = by_delegation.get(rec.get("delegation_id"))
            if not match:
                continue
            if match.get("status_after") in ("stale", "needs_attention"):
                rec["status"] = "needs_attention"
                rec["reconcile"] = match
                touched.append(rec["correlation_id"])

    state_write_locked(state_path, state_load(state_path), _mutate)
    for cid in touched:
        if args.audit:
            gr.append_audit_event(args.audit, "recovery", cid,
                                  scope={}, source="reconcile",
                                  note="сессия помечена needs_attention; "
                                       "worktree/файлы сохранены; делегация "
                                       "не повторяется")
    _emit({
        "schema_version": OUTPUT_SCHEMA, "command": "reconcile",
        "results": res.get("results", []),
        "needs_attention": res.get("needs_attention"),
        "touched_runs": touched,
        "note": "ничего не удалено молча; worktree сохранен; запуск не "
                "продвинут — разбор и явное решение ПМ",
    }, args.as_json)
    return 0


# ---------------------------------------------------------------- release
# J52-w1.2 (шов пилота J51 #3): needs_attention/reserved-сессии держат зоны,
# явного выхода у ПМ не было (reconcile зоны не освобождает — «разбор и
# явное решение ПМ» без механики). release — это явное решение ПМ: сессия
# переводится в статус "released" (зона освобождена), НИЧЕГО не удаляется
# молча: запись в реестре и worktree сохраняются, решение с причиной
# фиксируется в audit (append-only JSONL) и в записи цикла state.

# RELEASE_SCHEMA (review-001 #5): версионирование release-события —
# записывается в audit-событие release (release_schema), не мертвая
# константа.
RELEASE_SCHEMA = "flowctl-release/1"
RELEASE_ELIGIBLE = ("reserved", "needs_attention")
RELEASE_DEFAULT_REASON = "явное решение ПМ: сброс сессии без штатного выхода"


def cmd_release(args) -> int:
    cid = (args.correlation_id_opt or args.correlation_pos or "").strip()
    if (args.correlation_id_opt and args.correlation_pos
            and args.correlation_id_opt.strip() != cid):
        print("FLOWCTL-ERROR: correlation-id задан дважды (позиционно и "
              "--correlation-id) с разными значениями", file=sys.stderr)
        return 2
    if not cid:
        print("FLOWCTL-ERROR: укажи correlation-id (позиционно или "
              "--correlation-id)", file=sys.stderr)
        return 2
    reason = (args.reason or "").strip() or RELEASE_DEFAULT_REASON
    registry_path = Path(args.registry) if args.registry else (
        Path.home() / ".hermes" / "state" / "active_sessions.json")
    state_path = Path(args.state) if args.state else default_state_path(
        args.registry)
    state = state_load(state_path)

    record = state["runs"].get(cid)
    if record is None:
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "release",
            "correlation_id": cid, "released": False,
            "reason": sc.MISSING_INPUT,
            "note": "нет записи цикла для этого correlation ID — release "
                    "не выполняется, ничего не тронуто",
        }, args.as_json)
        return 2
    delegation_id = record.get("delegation_id", "")

    # Источник решения — факт реестра (контракт §13), не state.
    data, err = sc.registry_load(registry_path)
    if err:
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "release",
            "correlation_id": cid, "released": False,
            "reason": sc.REGISTRY_ERROR, "details": [err],
        }, args.as_json)
        return 2
    session = next(
        (s for s in data.get("sessions", [])
         if isinstance(s, dict) and s.get("delegation_id") == delegation_id),
        None)
    if session is None:
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "release",
            "correlation_id": cid, "released": False,
            "reason": sc.MISSING_INPUT,
            "note": f"reservation {delegation_id} не найдена в реестре — "
                    "release не выполняется",
        }, args.as_json)
        return 2

    prev = session.get("status")
    if prev == "released":
        # Идемпотентность: сброс уже выполнен и зафиксирован; повтор —
        # no-op (audit не дублируется).
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "release",
            "correlation_id": cid, "released": True, "idempotent": True,
            "delegation_id": delegation_id, "from_status": prev,
            "note": "сессия уже released — повтор без действия",
        }, args.as_json)
        return 0
    if prev not in RELEASE_ELIGIBLE:
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "release",
            "correlation_id": cid, "released": False, "refused": True,
            "delegation_id": delegation_id, "from_status": prev,
            "note": f"статус {prev!r} не подлежит release (разрешены: "
                    f"{', '.join(RELEASE_ELIGIBLE)}) — сессия НЕ тронута; "
                    "штатные пути: finish (running), разбор ПМ",
        }, args.as_json)
        return 1

    tr = registry_transition(
        registry_path, delegation_id, "released",
        f"flowctl release: {reason}",
        f"явное решение ПМ (correlation {cid}); работа/файлы сохранены",
        expect_from=prev)
    if not tr["ok"]:
        if tr.get("reason") == "status_changed":
            # review-001 #1 (гонка release×run): между чтением prev и
            # захватом лока сессию перевел другой процесс (агент
            # стартовал). Валидация под локом — refusing, реестр не
            # тронут; решение за ПМ: повторить release по факту.
            now = tr.get("current_status")
            _emit({
                "schema_version": OUTPUT_SCHEMA, "command": "release",
                "correlation_id": cid, "released": False, "refused": True,
                "delegation_id": delegation_id, "from_status": prev,
                "status_now": now,
                "note": f"статус изменился, повтори release (ожидался "
                        f"{prev!r}, фактически {now!r}) — сессия НЕ тронута",
            }, args.as_json)
            return 1
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "release",
            "correlation_id": cid, "released": False,
            "reason": tr.get("reason"), "details": tr.get("details", []),
        }, args.as_json)
        return 2

    def _mutate(st: dict) -> None:
        rec = st["runs"][cid]
        rec["status"] = "released"
        rec.setdefault("lifecycle", {}).setdefault("history", []).append({
            "at": gr.utcnow_iso(), "from": prev, "to": "released",
            "reason": reason,
            "evidence": "flowctl release: явное решение ПМ"})
        rec["release"] = {
            "at": gr.utcnow_iso(), "from_status": prev, "reason": reason,
            "worktree_preserved": bool(rec.get("worktree")),
        }

    state_write_locked(state_path, state, _mutate)
    audit_written = False
    if args.audit:
        gr.append_audit_event(
            args.audit, "release", cid, scope=record.get("scope") or {},
            delegation_id=delegation_id, from_status=prev, reason=reason,
            release_schema=RELEASE_SCHEMA,
            worktree=record.get("worktree"),
            note="ничего не удалено молча: запись реестра (released) и "
                 "worktree сохранены; решение ПМ зафиксировано")
        audit_written = True
    _emit({
        "schema_version": OUTPUT_SCHEMA, "command": "release",
        "correlation_id": cid, "released": True,
        "delegation_id": delegation_id, "from_status": prev,
        "status": "released", "reason": reason,
        "worktree_preserved": bool(record.get("worktree")),
        "audit_written": audit_written,
        "note": "сессия сброшена решением ПМ: зона освобождена (статус "
                "released вне ZONE_HOLDING); запись реестра и worktree "
                "сохранены, решение — в audit",
    }, args.as_json)
    return 0


# ---------------------------------------------------------------- regress
# J51.2: регресс своего куска — механически. Дифф (base...HEAD, иначе staged)
# детерминированно маппится в затронутые тестовые домены, гоняются только они,
# факт прогона — артефакт test-model/regress/<C>/regress-<N>.md (PASS/FAIL,
# commit, сьюты, хвост лога ≤50 строк, дата). --final пишет regress-final.md
# (финальный прогон; нумерованные артефакты финальными не считаются — J51.3).

REGRESS_SCHEMA = "flowctl-regress/1"
REGRESS_DEFAULT_BASE = "main"
REGRESS_LOG_TAIL_LINES = 50
# Маппинг дифф-префикс → тестовый домен; всё, что не покрыто правилами
# (включая scripts/ и корень), идет в общий tests/.
REGRESS_DOMAIN_RULES = (
    ("frontend/", "tests/web"),
    ("backend/", "tests/api"),
)
# J52-w1.3 (решение Заказчика 2026-10-18): гранулярность доменов regress =
# структура тест-кейсов (approved-кейсы группируются по доменам; маппинг
# диффа резонирует с кейс-структурой, не с техническими путями). Проект
# задает карту доменов файлом test-model/regress-domains.json (контент у
# проекта — как J53-глоссарий):
#   {"rules": [{"prefix": "frontend/src/editor/", "domain": "tests/editor"},
#              {"prefix": "services/billing/",  "domain": "tests/billing"}]}
# Правила проекта применяются ДО дефолтных; файла нет (или он не читается /
# без валидных правил) — дефолтное поведение. При активной карте домены
# проекта НЕ сворачиваются в общий tests/ (кейс-структура проекта — его
# ответственность; несуществующий каталог домена → заметный WARN regress,
# не молчаливый PASS).
REGRESS_DOMAIN_MAP_REL = "test-model/regress-domains.json"

# ---------------------------------------------------- J54: эшелоны регресса
# Пять эшелонов (backlog J54, решение Заказчика 2026-10-18): 0 инстант (dev,
# юниты своего модуля), 1 доменный (`flowctl regress`, по умолчанию), 2
# cross-domain smoke (`--tier smoke`, полный контур перед code_review/merge),
# 3 regress-final (`--final`, пакетно перед плановым деплоем), 4 прод-приемка
# (Заказчик). Baseline известных фейлов main читают эшелоны 1–2; regress-final
# (--final) baseline ИГНОРИРУЕТ — полный честный прогон. xdist (--xdist N)
# ускоряет любой эшелон.
REGRESS_TIERS = ("domain", "smoke")
SMOKE_TIER_LABEL = "smoke (эшелон 2: cross-domain smoke)"
DOMAIN_TIER_LABEL = "domain (эшелон 1: доменный regress)"
# Эшелон 2: манифест cross-domain smoke — список сьютов (файлов/каталогов),
# контент у проекта; каталога/манифеста нет (или suites пуст) → WARN и exit 0,
# не FAIL (smoke-контур еще не настроен — это легитимное состояние проекта).
SMOKE_CROSS_MANIFEST_REL = "test-model/smoke-cross/manifest.json"
# Baseline известных фейлов main: список {"test", "reason", "added"} (J54).
# Тест из baseline, упавший на regress (не --final), не краснит вердикт —
# в артефакте «baseline: ожидаемо-падающий (reason)». --final читает baseline
# только как информацию (фон) — краснит всё.
REGRESS_BASELINE_REL = "test-model/regress/baseline.json"
# Полуавто-канонизация фона: --suggest-baseline дописывает упавшие (с
# заполнителем причины) в отдельный suggestions-файл; перенос в baseline —
# ручное решение ПМ, автоматической канонизации падений нет.
REGRESS_BASELINE_SUGGESTIONS_REL = "test-model/regress/baseline-suggestions.json"
SUGGEST_BASELINE_REASON = ("авто: падение на regress; причина не заполнена — "
                           "ПМ переносит в baseline.json вручную после триажа")


def load_domain_map(repo: Path) -> tuple[tuple[tuple[str, str], ...], str]:
    """Проектная карта доменов (J52-w1.3). Возвращает (правила, источник):
    карты нет → ((), "default ..."); правила проекта предшествуют дефолтным.
    Битая карта — НЕ молчаливый дефолт: диагностика уходит в исход regress
    и артефакт, решение о починке карты — за проектом/ПМ."""
    path = repo / REGRESS_DOMAIN_MAP_REL
    if not path.is_file():
        return (), f"default (карта {REGRESS_DOMAIN_MAP_REL} не задана)"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return (), (f"error: карта {REGRESS_DOMAIN_MAP_REL} не прочитана "
                    f"({exc}) — применен дефолт")
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        return (), (f"error: карта {REGRESS_DOMAIN_MAP_REL}: ожидается "
                    "{\"rules\": [...]} — применен дефолт")
    if not rules:
        # J54: factory_init ставит скелет с пустым rules — это легитимное
        # «карта задана, контент у проекта», а не ошибка чтения.
        return ((), f"project map {REGRESS_DOMAIN_MAP_REL} задана, "
                "правил нет — применен дефолт")
    parsed: list[tuple[str, str]] = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        prefix, domain = r.get("prefix"), r.get("domain")
        if (isinstance(prefix, str) and isinstance(domain, str)
                and prefix and domain):
            parsed.append((prefix.replace("\\", "/").lstrip("./"), domain))
    if not parsed:
        return (), (f"error: карта {REGRESS_DOMAIN_MAP_REL}: нет валидных "
                    "правил (нужны prefix+domain) — применен дефолт")
    return (tuple(parsed),
            f"project map {REGRESS_DOMAIN_MAP_REL} ({len(parsed)} правил(а))")


def regress_diff_files(repo: Path, base: str) -> tuple[list[str], str]:
    """Измененные файлы для regress: `git diff <base>...HEAD`; base недоступен
    или дифф пуст → staged (`git diff --cached --name-only`; тесты кладут дифф
    в индекс). Возвращает (файлы, источник — для артефакта)."""
    def _names(*git_args: str) -> list[str] | None:
        proc = subprocess.run(
            ["git", "-C", str(repo), *git_args],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return None
        return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]

    files = _names("diff", "--name-only", f"{base}...HEAD") if base else None
    if files:
        return files, f"git diff {base}...HEAD"
    staged = _names("diff", "--cached", "--name-only")
    if staged:
        return staged, "git diff --cached (staged)"
    return [], (f"git diff {base}...HEAD" if files is not None
                else "git diff --cached (staged)")


def regress_domains(files: list[str], rules=REGRESS_DOMAIN_RULES,
                    collapse: bool = True) -> list[str]:
    """J51.2: детерминированный маппинг диффа → затронутые тестовые домены:
    frontend/** → tests/web, backend/** → tests/api, всё остальное (в т.ч.
    scripts/) → tests/. Порядок — по правилам, без дублей.

    J52-w1.3: правила можно передать (проектная карта доменов = структура
    тест-кейсов; правила проекта предшествуют дефолтным). collapse=False
    (активная проектная карта) отключает свертку частичных доменов в общий
    tests/ — кейс-домены проекта это реальные каталоги; прогон только
    затронутых кейс-доменов и есть цель карты.

    J52 (review-001 #4, резидуальный риск BUG-005 при активной карте):
    multi-domain прогон — ОДИН pytest-вызов (`pytest tests/editor
    tests/billing`) — тот же профиль, что до дедупа: bare
    `from conftest import ...` в кейс-доменах уронит коллекцию
    (pytest exit 2 → verdict FAIL, ворота не пропустят). Карта
    «безопасна» только при правиле «хелперы через фикстуры»
    (шов J51 #8, скилл ekotov-factory-pipeline,
    references/regress-final-gates.md).

    BUG-video-001 (дедуп доменов, ДО прогона; дефолтное поведение): частичные
    домены (tests/api, tests/web) лежат под общим каталогом tests/; прогон
    частичных ВМЕСТЕ с tests/ (или нескольких частичных в одном
    pytest-вызове) рождает коллизию conftest для bare-импортов —
    pytest exit 2 на коллекции. Если в маппинге присутствует общий домен
    tests/ или затронуто больше одного частичного — весь прогон
    сворачивается в ОДИН общий домен tests/ (одиночный частичный домен
    сохраняется как есть). Если каталога tests/ нет, свернувшийся прогон
    дает заметный WARN исхода regress (domain_without_tests), не молчу.
    """
    domains: list[str] = []
    other = False
    for rel in files:
        norm = rel.replace("\\", "/").lstrip("./")
        for prefix, domain in rules:
            if norm.startswith(prefix):
                if domain not in domains:
                    domains.append(domain)
                break
        else:
            other = True
    if other and "tests" not in domains:
        domains.append("tests")
    if not collapse:
        return domains
    partial = [d for d in domains if d != "tests"]
    if "tests" in domains or len(partial) > 1:
        return ["tests"]
    return domains


def load_baseline(repo: Path) -> tuple[dict[str, dict], str]:
    """Baseline известных фейлов main (J54): test-model/regress/baseline.json —
    список записей {"test", "reason", "added"}. Возвращает ({test: запись},
    источник/диагностика). Файла нет → ({}, «нет (...)»); битый файл — НЕ
    молчаливый дефолт: диагностика уходит в исход и артефакт (как у карты
    доменов, J52-w1.3), но прогон не блокирует."""
    path = repo / REGRESS_BASELINE_REL
    if not path.is_file():
        return {}, f"нет ({REGRESS_BASELINE_REL} не задан)"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, (f"error: baseline {REGRESS_BASELINE_REL} не прочитан "
                    f"({exc}) — считаем пустым (фон краснит)")
    if not isinstance(data, list):
        return {}, (f"error: baseline {REGRESS_BASELINE_REL}: ожидается "
                    "список — считаем пустым (фон краснит)")
    entries: dict[str, dict] = {}
    for item in data:
        if (isinstance(item, dict) and isinstance(item.get("test"), str)
                and item["test"]):
            entries[item["test"]] = item
    return entries, f"{REGRESS_BASELINE_REL} ({len(entries)} записей(и))"


def suggest_baseline(repo: Path, failed: list[str],
                     change: str) -> tuple[list[str], str]:
    """Полуавто-канонизация фона (J54, --suggest-baseline): упавшие тесты
    дописываются в baseline-suggestions.json (с заполнителем reason) —
    отдельный файл, baseline НЕ меняется: перенос — ручное решение ПМ после
    триажа. Append + дедуп против baseline и предыдущих suggestions.
    Возвращает (добавленные тесты, путь файла)."""
    baseline, _ = load_baseline(repo)
    path = repo / REGRESS_BASELINE_SUGGESTIONS_REL
    existing: list[dict] = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = [e for e in data if isinstance(e, dict)]
        except (OSError, ValueError):
            existing = []
    seen = {e.get("test") for e in existing} | set(baseline)
    added: list[str] = []
    for t in failed:
        if t in seen:
            continue
        existing.append({"test": t, "reason": SUGGEST_BASELINE_REASON,
                         "added": gr.utcnow_iso()})
        added.append(t)
        seen.add(t)
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    return added, str(path)


def smoke_cross_plan(repo: Path) -> tuple[list[str], str]:
    """Эшелон 2 (J54): сьюты cross-domain smoke из манифеста
    test-model/smoke-cross/manifest.json — {"suites": ["tests/...", ...]}.
    Контент у проекта; манифеста нет (или suites пуст/бит) → ([], диагностика)
    — regress --tier smoke даст WARN и exit 0, не FAIL."""
    path = repo / SMOKE_CROSS_MANIFEST_REL
    if not path.is_file():
        return [], (f"манифест {SMOKE_CROSS_MANIFEST_REL} не найден — "
                    "smoke-контур не настроен (легитимно для новых проектов)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], (f"error: манифест {SMOKE_CROSS_MANIFEST_REL} не прочитан "
                    f"({exc})")
    suites = data.get("suites") if isinstance(data, dict) else None
    if (not isinstance(suites, list)
            or not all(isinstance(s, str) and s for s in suites)):
        return [], ("error: манифест " + SMOKE_CROSS_MANIFEST_REL
                    + ": ожидается {\"suites\": [\"tests/...\"]}")
    if not suites:
        return [], (f"манифест {SMOKE_CROSS_MANIFEST_REL} пуст (suites: []) "
                    "— smoke-сьюты не заведены")
    return list(dict.fromkeys(suites)), (
        f"манифест {SMOKE_CROSS_MANIFEST_REL} ({len(suites)} сьютов)")


def regress_pytest_args(suites: list[str],
                        xdist: int | None = None) -> list[str]:
    """Аргументы pytest-прогона regress. -rf — короткие FAILED-строки в хвосте
    лога: по ним regress сверяет упавших с baseline (J54). --xdist N →
    `pytest -n N` (pytest-xdist); требование к проекту: детерминированность
    при параллели (session-фикстуры/стенды не делятся между воркерами,
    стендозависимые тесты — serial-маркеры)."""
    args = [sys.executable, "-m", "pytest", *suites, "-q", "-rf"]
    if xdist:
        args += ["-n", str(int(xdist))]
    return args


def parse_failed_tests(output: str) -> list[str]:
    """ID упавших тестов из короткой сводки pytest (-rf): строки
    «FAILED <nodeid> - ...». Порядок появления, без дублей."""
    failed: list[str] = []
    for line in output.splitlines():
        m = re.match(r"^FAILED\s+(\S+)", line.strip())
        if m and m.group(1) not in failed:
            failed.append(m.group(1))
    return failed


def regress_next_number(art_dir: Path) -> int:
    """Следующий номер артефакта regress-<N>.md (regress-final.md не нумерованный —
    не считается)."""
    nums = []
    for p in art_dir.glob("regress-*.md"):
        m = re.fullmatch(r"regress-(\d+)\.md", p.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums, default=0) + 1


def cmd_regress(args) -> int:
    repo = Path(args.repo).resolve()
    if not repo.is_dir() or not _git_head(repo):
        print(f"FLOWCTL-ERROR: не git-репозиторий: {repo}", file=sys.stderr)
        return 2
    change = args.change
    # change-id протекает из маркеров/автоматизации (недоверенный вход) —
    # строго в путь записи не идет: единая валидация change_id_problem
    # (session_check; review-001 #1, review-003 #3 — та же функция на
    # читающем конце ворот pr_validate.check_regress_final).
    cid_problem = sc.change_id_problem(change)
    if cid_problem:
        print(f"FLOWCTL-ERROR: {cid_problem}", file=sys.stderr)
        return 2
    commit = _git_head(repo)
    tier = getattr(args, "tier", None) or "domain"
    xdist = getattr(args, "xdist", None)
    tier_label = (SMOKE_TIER_LABEL if tier == "smoke" else DOMAIN_TIER_LABEL)

    # Baseline известных фейлов (J54): читают эшелоны 1–2 (регресс без
    # --final); regress-final ИГНОРИРУЕТ baseline — полный честный прогон,
    # фон упоминается только информационно.
    baseline, baseline_note = load_baseline(repo)
    if args.final and baseline:
        baseline_note += "; --final: baseline ИГНОРИРУЕТСЯ (полный честный " \
                         "прогон — фон краснит наравне с остальными)"

    manifest_note = ""
    if tier == "smoke":
        # Эшелон 2: полный контур по манифесту (список сьютов — контент у
        # проекта). Манифеста/сьютов нет → WARN и exit 0 (см. ниже).
        manifest_suites, manifest_note = smoke_cross_plan(repo)
        suites = [s for s in manifest_suites if (repo / s).exists()]
        skipped = [s for s in manifest_suites if s not in suites]
        smoke_empty = not manifest_suites
        files, diff_source = [], "n/a (tier smoke: манифест, не дифф)"
        domains = []
        domain_map_note = "n/a (tier smoke)"
    else:
        files, diff_source = regress_diff_files(repo, args.base)
        # J52-w1.3: проектная карта доменов (структура тест-кейсов) приоритетна
        # перед техническим дефолтом; ее отсутствие/битость — не молчаливый
        # дефолт, источник/диагностика идут в исход и артефакт.
        project_rules, domain_map_note = load_domain_map(repo)
        domains = regress_domains(
            files, project_rules + REGRESS_DOMAIN_RULES,
            collapse=not bool(project_rules))
        suites = [d for d in domains if (repo / d).is_dir()]
        skipped = [d for d in domains if d not in suites]
        smoke_empty = False

    pytest_rc: int | None = None
    log_tail = ""
    timeout_hit = False
    failed_tests: list[str] = []
    duration_s: float | None = None
    if suites:
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                regress_pytest_args(suites, xdist), capture_output=True,
                text=True, cwd=str(repo), timeout=args.timeout,
            )
            duration_s = time.monotonic() - t0
            pytest_rc = proc.returncode
            out = (proc.stdout or "") + (proc.stderr or "")
            log_tail = "\n".join(
                out.strip().splitlines()[-REGRESS_LOG_TAIL_LINES:])
            failed_tests = parse_failed_tests(out)
        except subprocess.TimeoutExpired as exc:
            duration_s = time.monotonic() - t0
            timeout_hit = True
            out_head = (exc.stdout or "")
            if isinstance(out_head, bytes):
                out_head = out_head.decode("utf-8", errors="replace")
            log_tail = "\n".join(out_head.strip().splitlines()[
                -REGRESS_LOG_TAIL_LINES:]) or f"pytest timeout > {args.timeout}s"
    # Домен затронут диффом, но каталога тестов не существует — заметный
    # WARN-исход, не молчаливый PASS (review-001 #2/#3): exit 0, но вердикт
    # WARN и пометка в артефакте, чтобы воротам не пришел фиктивно зеленый
    # прогон. Частичный skip (часть доменов прогнана, часть без каталога) —
    # тоже WARN: затронутый диффом кейс-домен карты остался непокрыт.
    domain_without_tests = (tier != "smoke") and bool(domains) and not suites
    partial_skip = bool(skipped) and not domain_without_tests
    # J54: упавшие сверяются с baseline (только regress без --final):
    # тест из baseline, упавший снова, — ожидаемо-падающий фон, вердикт не
    # краснит; неожиданные падения краснят как раньше.
    baseline_skipped: list[tuple[str, str]] = []
    unexpected_failed: list[str] = []
    if failed_tests and not args.final:
        for t in failed_tests:
            entry = baseline.get(t)
            if entry is not None:
                baseline_skipped.append((t, str(entry.get("reason") or "—")))
            else:
                unexpected_failed.append(t)
    elif failed_tests and args.final:
        unexpected_failed = list(failed_tests)
    smoke_no_tests = tier == "smoke" and pytest_rc == 5
    if pytest_rc not in (None, 0) and not smoke_no_tests:
        if baseline_skipped and not unexpected_failed:
            verdict = "PASS"       # фон baseline не краснит (J54)
        else:
            verdict = "FAIL"
    elif smoke_empty or domain_without_tests or partial_skip or smoke_no_tests:
        verdict = "WARN"
    else:
        verdict = "PASS"

    # --suggest-baseline (J54, полуавто): упавшие НЕ из baseline дописываются
    # в отдельный suggestions-файл (дедуп против baseline и прошлых записей);
    # ПМ переносит в baseline.json вручную после триажа.
    suggest_added: list[str] = []
    suggest_path = ""
    if getattr(args, "suggest_baseline", False) and failed_tests:
        suggest_added, suggest_path = suggest_baseline(
            repo, failed_tests, change)

    art_dir = repo / "test-model" / "regress" / change
    art_dir.mkdir(parents=True, exist_ok=True)
    if args.final:
        art_name = "regress-final.md"
    else:
        art_name = f"regress-{regress_next_number(art_dir)}.md"
    art_path = art_dir / art_name
    lines = [
        f"# Regress {change} — {art_name}",
        "",
        f"- change: {change}",
        f"- date: {gr.utcnow_iso()}",
        f"- commit: {commit}",
        # J51 (review-003 #4): путь дерева прогона — ворота merge сверяют
        # HEAD этого дерева с commit артефакта (если worktree указан,
        # его текущий коммит обязан совпадать с проверяемым).
        f"- worktree: {repo}",
        f"- verdict: {verdict}",
        f"- tier: {tier_label}",
        f"- duration: {duration_s:.1f}s" if duration_s is not None
        else "- duration: — (прогон не выполнялся)",
        f"- diff source: {diff_source}",
        f"- domain map: {domain_map_note}",
    ]
    if tier == "smoke":
        lines.append(f"- smoke manifest: {manifest_note}")
    else:
        lines.append(f"- changed files: {len(files)}")
    if xdist:
        lines += [
            f"- xdist: pytest -n {xdist} (pytest-xdist)",
            "- note: xdist — предупреждение J54: session-фикстуры и стенды "
            "не должны делиться между воркерами (isolating fixtures, stand "
            "per worker или serial-маркеры); стендозависимые тесты — "
            "serial-only, serial-fallback — прогон без --xdist",
        ]
    if baseline:
        lines.append(f"- baseline: {baseline_note}")
    if tier != "smoke":
        lines += [f"- changed files: {len(files)}"]
        lines += [f"  - {f}" for f in files[:50]]
        if len(files) > 50:
            lines.append(f"  - … и еще {len(files) - 50}")
    lines += [
        f"- suites: {', '.join(suites) if suites else '— (нечего гнать)'}",
    ]
    if skipped:
        lines.append(f"- suites skipped (нет каталога): {', '.join(skipped)}")
    lines += [
        f"- pytest exit code: {pytest_rc if pytest_rc is not None else '—'}",
        "",
    ]
    if baseline_skipped:
        lines += ["## Baseline (ожидаемо-падающие, J54)", ""]
        lines += [f"- baseline: ожидаемо-падающий ({reason}) — {t}"
                  for t, reason in baseline_skipped]
        lines.append("")
    if suggest_added:
        lines += [
            "## Suggestions для baseline (--suggest-baseline, полуавто)", "",
            f"- файл: {suggest_path}",
            f"- добавлены ({len(suggest_added)}): "
            + ", ".join(suggest_added),
            "- перенос в baseline.json — вручную, после триажа ПМ", "",
        ]
    if timeout_hit:
        lines += [f"- note: pytest прерван по таймауту ({args.timeout}s)", ""]
    if smoke_empty:
        lines += ["- note: WARN — smoke-контур не настроен: " + manifest_note
                  + "; прогон не выполнялся (эшелон 2: дефолт — WARN и exit 0)",
                  ""]
    elif smoke_no_tests:
        lines += ["- note: WARN — smoke-сьюты собраны без тестов "
                  "(pytest exit 5): сьюты манифеста существуют, но пусты", ""]
    if domain_without_tests:
        lines += ["- note: WARN — домен(ы) без тестов: "
                  f"{', '.join(skipped)} — затронуты диффом, но каталогов "
                  "не существует; прогон не выполнялся", ""]
    elif partial_skip:
        lines += ["- note: WARN — частичный skip доменов (нет каталога): "
                  f"{', '.join(skipped)} — затронуты диффом, но не покрыты "
                  "прогоном", ""]
    elif not suites and not timeout_hit and tier != "smoke":
        lines += ["- note: дифф не затронул существующие тестовые домены — "
                  "прогон не выполнялся", ""]
    if unexpected_failed and args.final and baseline:
        lines += ["- note: --final: baseline-фейлы (" + ", ".join(
            t for t in failed_tests if t in baseline)
            + ") покраснели наравне с остальными (полный честный прогон)", ""]
    lines += ["## Хвост лога (до 50 строк)", "",
              "```", log_tail or "(пусто)", "```", ""]
    art_path.write_text("\n".join(lines), encoding="utf-8")

    payload = {
        "schema_version": REGRESS_SCHEMA, "command": "regress",
        "change": change, "repo": str(repo),
        "verdict": verdict,
        "tier": tier,
        "final": bool(args.final),
        "xdist": xdist,
        "duration_s": round(duration_s, 2) if duration_s is not None else None,
        "baseline": {
            "path": REGRESS_BASELINE_REL,
            "note": baseline_note,
            "entries": len(baseline),
            "skipped": [{"test": t, "reason": r}
                        for t, r in baseline_skipped],
            "ignored": bool(args.final),
        },
        "suggest_baseline": {
            "requested": bool(getattr(args, "suggest_baseline", False)),
            "added": suggest_added,
            "path": suggest_path or None,
        },
        "diff_source": diff_source,
        "domain_map": domain_map_note,
        "changed_files": len(files),
        "domains": domains,
        "suites": suites,
        "suites_skipped": skipped,
        "failed_tests": failed_tests,
        "pytest_exit": pytest_rc,
        "timeout": timeout_hit,
        "commit": commit,
        "artifact": str(art_path),
    }
    if tier == "smoke":
        payload["smoke_manifest"] = manifest_note
    payload["note"] = ("артефакт записан: " + str(art_path)
                       + ("; regress-final — вход ворот merge (J51.3)"
                          if args.final else
                          "; нумерованный regress-артефакт финальным не "
                          "считается"))
    _emit(payload, args.as_json)
    return 0 if verdict in ("PASS", "WARN") else 1


# --------------------------------------------------------------------- CLI


# ------------------------------------------------- J57: интеграция пакета
# Детерминированный каркас возврата к J9 (backlog/J57-pm-ops-worktree):
# сборка результатов делегаций — механика flowctl (integrate), разрешение
# конфликтов — семантика dev-lead (роль №14). ПМ ветки продукта не трогает.
INTEGRATE_BRANCH_PREFIX = "integration"
# Ветки пакета: деривация prepare (flow/<cid>), chore-ветки роли, pipeline/**
# (push-политика J42).
PACKAGE_BRANCH_PREFIXES = ("flow/", "chore/", "pipeline/")
# Активная сессия = сборка рано: сначала закрыть делегации (finish/release),
# иначе интеграционная ветка собирает недоделку. Синхронно с
# WORKTREE_ACTIVE_STATUSES энфорсера (finished/blocked/stale — не активные).
INTEGRATE_ACTIVE_STATUSES = ("reserved", "running", "needs_attention")
INTEGRATE_REASON_ACTIVE = "ACTIVE_SESSIONS"
INTEGRATE_REASON_BRANCH_EXISTS = "INTEGRATION_BRANCH_EXISTS"
INTEGRATE_REASON_CID_REQUIRED = "CID_REQUIRED"


def _integration_branch_name(cid: str) -> str:
    """integration/<cid>: полный cid (как ветки делегаций, J52-w1.1);
    небезопасный/длинный id → хэш-суффикс (та же деривация, что
    branch_name_for, но под префиксом integration/)."""
    tail = cid if _branch_id_ok(cid) else (
        hashlib.sha1(cid.encode("utf-8")).hexdigest()[:8])
    return f"{INTEGRATE_BRANCH_PREFIX}/{tail}"


def _session_in_repo(session: dict, repo: Path) -> bool:
    """Сессия этого репо: repo/worktree записи совпадает с repo или лежит
    под ним (worktrees репо; синхронно с _in_context энфорсера)."""
    repo_n = str(repo).replace("\\", "/").rstrip("/")
    for key in ("repo", "worktree"):
        base = session.get(key)
        if not base:
            continue
        b = str(base).replace("\\", "/").rstrip("/")
        if b == repo_n or b.startswith(repo_n + "/"):
            return True
    return False


def _ref_exists(repo: Path, ref: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
         f"{ref}^{{commit}}"],
        capture_output=True, text=True,
    )
    return proc.returncode == 0


def cmd_integrate(args) -> int:
    """J57: сборка пакета — детерминированный каркас возврата к J9.

    По реестру сессий показывает ветки пакета (flow/*, chore/*, pipeline/*)
    репозитория, проверяет отсутствие АКТИВНЫХ сессий (иначе REFUSE),
    создает интеграционную ветку integration/<cid> от origin/main (фолбэк
    main) и печатает список веток для dev-lead'а. Merge/конфликты —
    семантика dev-lead; ветка НЕ пушится и НЕ мерджится здесь.
    """
    raw_repo = getattr(args, "repo_pos", None) or getattr(args, "repo_opt", None)
    if not raw_repo:
        print("FLOWCTL-ERROR: укажи репозиторий: flowctl integrate <repo>",
              file=sys.stderr)
        return 2
    repo = Path(raw_repo).expanduser().resolve()
    if not repo.is_dir() or not _git_head(repo):
        print(f"FLOWCTL-ERROR: не git-репозиторий: {repo}", file=sys.stderr)
        return 2
    registry_path = Path(args.registry).expanduser() if args.registry else (
        Path.home() / ".hermes" / "state" / "active_sessions.json")
    data, err = sc.registry_load(registry_path)
    if err:
        # fail-closed: не умеем проверить отсутствие активных сессий —
        # сборку не начинаем (это отказ среды, не DENY пакета).
        print(f"FLOWCTL-ERROR: реестр сессий нечитаем ({registry_path}): {err}",
              file=sys.stderr)
        return 2
    sessions = [s for s in data.get("sessions", []) if isinstance(s, dict)]
    repo_sessions = [s for s in sessions if _session_in_repo(s, repo)]
    package_branches = sorted({
        str(s["branch"]) for s in repo_sessions
        if s.get("branch")
        and str(s["branch"]).startswith(PACKAGE_BRANCH_PREFIXES)
    })
    active_ids = sorted({
        str(s.get("delegation_id", "?")) for s in repo_sessions
        if s.get("status") in INTEGRATE_ACTIVE_STATUSES
    })

    cid = (args.cid or "").strip()
    if not cid:
        # Единственный correlation-id (или change) сессий этого репо — cid
        # пакета; неоднозначно/пусто → пусть caller назовет явно.
        cids = {str(s["correlation_id"]) for s in repo_sessions
                if s.get("correlation_id")}
        if len(cids) == 1:
            cid = cids.pop()
        else:
            changes = {str(s["change"]) for s in repo_sessions
                       if s.get("change")}
            if len(changes) == 1:
                cid = changes.pop()

    payload = {
        "schema_version": OUTPUT_SCHEMA,
        "command": "integrate",
        "repo": str(repo),
        "cid": cid or None,
        "package_branches": package_branches,
        "active_sessions": active_ids,
    }

    if active_ids:
        _emit({**payload, "integrated": False,
               "reason": INTEGRATE_REASON_ACTIVE,
               "note": "активные сессии этого репо — сначала закрой их "
                       "(finish/release); интеграционная ветка не создана "
                       "(собирать недоделку = гонка с делегациями)"},
              args.as_json)
        return 1

    if not cid:
        _emit({**payload, "integrated": False,
               "reason": INTEGRATE_REASON_CID_REQUIRED,
               "note": "в реестре нет однозначного correlation-id/change для "
                       "этого репо — укажи --cid явно"},
              args.as_json)
        return 2

    branch = _integration_branch_name(cid)
    if _ref_exists(repo, f"refs/heads/{branch}"):
        _emit({**payload, "integrated": False,
               "reason": INTEGRATE_REASON_BRANCH_EXISTS,
               "integration_branch": branch,
               "note": "интеграционная ветка уже существует — не пересоздаю; "
                       "передай ее dev-lead'у как есть"},
              args.as_json)
        return 1

    base = None
    for cand in ((args.base or "").strip(), "origin/main", "main"):
        if cand and _ref_exists(repo, cand):
            base = cand
            break
    if base is None:
        _emit({**payload, "integrated": False, "reason": "BASE_MISSING",
               "integration_branch": branch,
               "note": f"нет базы ({args.base or 'origin/main'}/main): "
                       "сделай git fetch origin в репо и повтори"},
              args.as_json)
        return 2

    proc = subprocess.run(
        ["git", "-C", str(repo), "branch", branch, base],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"FLOWCTL-ERROR: git branch {branch} {base}: "
              f"{proc.stderr.strip()}", file=sys.stderr)
        return 2

    _emit({**payload, "integrated": True, "integration_branch": branch,
           "base": base,
           "note": "сборка: передай dev-lead'у — merge веток пакета в "
                   f"{branch} (конфликты — семантика dev-lead, канон J9); "
                   "ветка локальная, не пушится отсюда"},
          args.as_json)
    return 0


def main(argv: list[str] | None = None) -> int:
    # Прокси на поставки 02–03: argv передается целиком (argparse.REMAINDER
    # не пробрасывает опции, начинающиеся с «-», поэтому маршрутизация до
    # общего парсера), exit codes сохранены.
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] in ("inspect", "check", "next"):
        cmd, *rest = argv
        if cmd == "inspect":
            return flow_state.main(["inspect"] + rest)
        return ft.main([cmd] + rest)

    ap = argparse.ArgumentParser(
        prog="flowctl.py",
        description="единый CLI-вход оркестратора (поставка 06; adapter=manual)",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    def add_scope(p):
        p.add_argument("--repo", required=True)
        p.add_argument("--project", required=True)
        # Н12: --flow валидируется ЯВНО (JSON-диагностика FLOW_ID_INVALID),
        # не сырым argparse (choices печатали usage в stderr без контекста).
        p.add_argument("--flow", required=True,
                       help="номер флоу 1–5 (целое)")
        p.add_argument("--change", required=True)
        p.add_argument("--task", default=None)
        p.add_argument("--registry", default=None,
                       help="путь к active_sessions.json (fixture в тестах)")
        p.add_argument("--state", default=None,
                       help="файл состояния цикла (по умолчанию рядом с реестром)")
        p.add_argument("--json", action="store_true", dest="as_json")

    def add_action_flags(p):
        p.add_argument("--action", required=True)
        p.add_argument("--role", required=True)
        p.add_argument("--approval-ref", default=None)
        p.add_argument("--expected-digest", default=None)
        p.add_argument("--parallel", action="store_true", default=None)
        p.add_argument("--spec-delta", action="store_true")
        p.add_argument("--incident-ref", default=None)
        p.add_argument("--path", dest="zone", action="append", default=None,
                       help="паттерн зоны записи (glob; повторяемый)")
        p.add_argument("--pipeline-marker", action="store_true")
        p.add_argument("--rules-change", action="store_true")
        p.add_argument("--small-change", action="store_true")
        p.add_argument("--paths", default=None,
                       help="пути изменения для protected-paths ворот (J3), "
                            "через запятую (Н14: без --paths ворота видели "
                            "только зону и молча проходили)")

    p_pre = sub.add_parser("prepare", help="подготовить session/worktree/goal")
    add_scope(p_pre)
    add_action_flags(p_pre)
    p_pre.add_argument("--correlation-id", default=None)
    p_pre.add_argument("--dry-run", action="store_true",
                       help="показать план без side effects")
    p_pre.add_argument("--owner-pm", default=None,
                       help="владелец сессии (обязателен без --dry-run)")
    p_pre.add_argument("--worktree", default=None,
                       help="существующее рабочее дерево сессии")
    p_pre.add_argument("--create-worktree", action="store_true",
                       help="создать worktree через session_worktree.sh")
    p_pre.add_argument("--branch", default=None)
    p_pre.add_argument("--goal-path", default=None)
    p_pre.add_argument("--audit", default=None, help="audit JSONL")
    p_pre.add_argument(
        "--release-stale", action="store_true",
        help="Н7: при ZONE_CONFLICT с finished-сессией, вердикт которой "
             "returned (работа не влита), — автоматически освободить ее "
             "(release) и повторить резервацию; без флага в диагностике "
             "печатаются готовые release-команды (stale_holders)")
    p_pre.set_defaults(func=cmd_prepare)

    p_run = sub.add_parser("run", help="разрешить и отметить старт (идемпотентно)")
    p_run.add_argument("--correlation-id", required=True)
    p_run.add_argument("--registry", default=None)
    p_run.add_argument("--state", default=None)
    p_run.add_argument("--audit", default=None)
    p_run.add_argument("--json", action="store_true", dest="as_json")
    p_run.set_defaults(func=cmd_run)

    p_fin = sub.add_parser("finish",
                           help="вердикт accepted/returned/blocked по фактам")
    p_fin.add_argument("--correlation-id", required=True)
    p_fin.add_argument("--report", required=True,
                       help="агентский отчет (указатель на файлы)")
    p_fin.add_argument("--gate-scope", default="post_agent",
                       choices=gr.SCOPES)
    p_fin.add_argument("--gates", default=None,
                       help="ДОПОЛНИТЕЛЬНЫЕ gates через запятую (P0.2: "
                            "обязательный набор вычисляется из политики; "
                            "замена набора — отказ с перечнем недостающих)")
    p_fin.add_argument("--diagnostic", action="store_true",
                       help="диагностический прогон сокращенного списка "
                            f"({', '.join(DIAGNOSTIC_GATES)}) БЕЗ права "
                            "accepted: вердикт diagnostic_only, сессия "
                            "не закрывается")
    p_fin.add_argument("--openspec-cmd", default="npx openspec")
    p_fin.add_argument("--timeout", type=int, default=gr.DEFAULT_TIMEOUT)
    p_fin.add_argument("--pm-mode", choices=gr.PM_MODES, default=None)
    p_fin.add_argument("--pm-commits", default=None)
    p_fin.add_argument("--pm-range", default=None)
    p_fin.add_argument("--pm-registry", default=None)
    p_fin.add_argument("--pm-resume", action="store_true", default=False,
                       help="явное решение ПМ: повтор finish blocked-сессии "
                            "(только blocked по gate ERROR без дефектов "
                            "по существу — конфигурационная ошибка запускающего)")
    p_fin.add_argument("--pm-require-review", action="store_true")
    p_fin.add_argument("--pr-id", default=None)
    p_fin.add_argument("--log-dir", default=None)
    p_fin.add_argument("--report-dir", default=None)
    p_fin.add_argument("--registry", default=None)
    p_fin.add_argument("--state", default=None)
    p_fin.add_argument("--audit", default=None)
    p_fin.add_argument("--json", action="store_true", dest="as_json")
    p_fin.set_defaults(func=cmd_finish)

    p_st = sub.add_parser("status", help="состояние циклов и сессий")
    p_st.add_argument("--correlation-id", default=None)
    p_st.add_argument("--registry", default=None)
    p_st.add_argument("--state", default=None)
    p_st.add_argument("--json", action="store_true", dest="as_json")
    p_st.set_defaults(func=cmd_status)

    # J43.1: память фазы — последний run scope из flowctl_state.json.
    p_ph = sub.add_parser(
        "phase", help="последний завершенный этап scope (память фазы)")
    p_ph.add_argument("--repo", required=True)
    p_ph.add_argument("--project", required=True)
    # Н12: валидация --flow явная (см. add_scope).
    p_ph.add_argument("--flow", required=True,
                      help="номер флоу 1–5 (целое)")
    p_ph.add_argument("--scope", required=True,
                      help="идентификатор scope (change/BUG-NNN/slug)")
    p_ph.add_argument("--registry", default=None)
    p_ph.add_argument("--state", default=None)
    p_ph.set_defaults(func=cmd_phase)

    p_rec = sub.add_parser("reconcile", help="сверка после сбоя (без удаления)")
    p_rec.add_argument("--repo", default=None)
    p_rec.add_argument("--delegation-id", default=None)
    p_rec.add_argument("--registry", default=None)
    p_rec.add_argument("--state", default=None)
    p_rec.add_argument("--audit", default=None)
    p_rec.add_argument("--json", action="store_true", dest="as_json")
    p_rec.set_defaults(func=cmd_reconcile)

    # J52-w1.2: явный сброс needs_attention/reserved-сессии решением ПМ
    # (ничего не удаляется молча; решение — в audit).
    p_rel = sub.add_parser(
        "release",
        help="явный сброс needs_attention/reserved-сессии решением ПМ")
    p_rel.add_argument("correlation_pos", nargs="?", default=None,
                       help="correlation-id (позиционно)")
    p_rel.add_argument("--correlation-id", dest="correlation_id_opt",
                       default=None)
    p_rel.add_argument("--reason", default=None,
                       help="причина/основание решения ПМ (в audit)")
    p_rel.add_argument("--registry", default=None)
    p_rel.add_argument("--state", default=None)
    p_rel.add_argument("--audit", default=None)
    p_rel.add_argument("--json", action="store_true", dest="as_json")
    p_rel.set_defaults(func=cmd_release)

    # J51.2: регресс затронутых доменов своего куска (+ regress-final, J51.3);
    # J54: эшелоны — --tier smoke (эшелон 2), baseline, --xdist, --suggest-baseline.
    p_reg = sub.add_parser(
        "regress", help="регресс затронутых тестовых доменов (J51.2/J54)")
    p_reg.add_argument("--repo", default=".")
    p_reg.add_argument("--change", required=True,
                       help="идентификатор change (артефакт test-model/"
                            "regress/<change>/)")
    p_reg.add_argument("--base", default=REGRESS_DEFAULT_BASE,
                       help="база диффа (default: main; дифф base...HEAD, "
                            "иначе staged)")
    p_reg.add_argument("--tier", choices=REGRESS_TIERS, default="domain",
                       help="эшелон (J54): domain — доменный regress своего "
                            "куска по диффу (default, эшелон 1); smoke — "
                            "cross-domain smoke по манифесту "
                            "test-model/smoke-cross/manifest.json (эшелон 2, "
                            "перед code_review/merge; пустой контур — WARN "
                            "и exit 0)")
    p_reg.add_argument("--final", action="store_true",
                       help="артефакт regress-final.md вместо нумерованного "
                            "regress-<N>.md (финальный прогон, эшелон 3; "
                            "baseline игнорируется — полный честный прогон)")
    p_reg.add_argument("--xdist", type=int, default=None, metavar="N",
                       help="pytest -n N (pytest-xdist); в артефакт пишется "
                            "предупреждение о детерминированности "
                            "(session-фикстуры/стенды не делятся между "
                            "воркерами)")
    p_reg.add_argument("--suggest-baseline", action="store_true",
                       dest="suggest_baseline",
                       help="полуавто (J54): упавшие тесты дописать в "
                            f"{REGRESS_BASELINE_SUGGESTIONS_REL} (с "
                            "заполнителем reason); перенос в baseline.json — "
                            "вручную, решение ПМ")
    p_reg.add_argument("--timeout", type=int, default=900,
                       help="таймаут pytest-прогона, сек (default: 900)")
    p_reg.add_argument("--json", action="store_true", dest="as_json")
    p_reg.set_defaults(func=cmd_regress)

    # J57: детерминированная сборка пакета — ветки из реестра, проверка
    # отсутствия активных сессий, integration/<cid> от origin/main;
    # merge/конфликты — семантика dev-lead (канон J9).
    p_int = sub.add_parser(
        "integrate",
        help="сборка пакета: ветки из реестра → integration/<cid> (J57)")
    p_int.add_argument("repo_pos", nargs="?", default=None,
                       help="путь к репозиторию пакета")
    p_int.add_argument("--repo", dest="repo_opt", default=None,
                       help="путь к репозиторию (альтернатива позиционному)")
    p_int.add_argument("--cid", default=None,
                       help="correlation-id/change пакета (по умолчанию — "
                            "единственный из реестра для этого репо)")
    p_int.add_argument("--base", default="origin/main",
                       help="база интеграционной ветки (default origin/main, "
                            "фолбэк main)")
    p_int.add_argument("--registry", default=None,
                       help="путь к active_sessions.json (fixture в тестах)")
    p_int.add_argument("--json", action="store_true", dest="as_json")
    p_int.set_defaults(func=cmd_integrate)

    args = ap.parse_args(argv)

    # Н12: невалидный --flow — машиночитаемая JSON-диагностика (с --json),
    # не сырой argparse-usage в stderr.
    if getattr(args, "flow", None) is not None:
        raw_flow = args.flow
        try:
            args.flow = int(raw_flow)
        except (TypeError, ValueError):
            args.flow = None
        if args.flow not in flow_state.FLOW_IDS:
            _emit({
                "schema_version": OUTPUT_SCHEMA,
                "command": args.command,
                "error": "FLOW_ID_INVALID",
                "field": "--flow",
                "value": str(raw_flow),
                "expected": "целое 1–5",
                "field_diagnostics": [f"flow={raw_flow!r} не соответствует "
                                      "формату (--flow N, N ∈ 1–5)"],
                "note": "невалидный --flow — укажи целое 1–5 "
                        "(флоу 1 change / 2 bugfix / 3 хотфикс / 4 chore / "
                        "5 экспресс)",
            }, getattr(args, "as_json", False))
            return 2

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
