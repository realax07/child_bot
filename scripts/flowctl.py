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

Exit codes: 0 — успех/ALLOW/accepted (run/prepare — разрешено);
1 — DENY/STALE_SNAPSHOT/returned; 2 — UNKNOWN/ошибка входа/blocked.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import flow_mode  # noqa: E402
import flow_state  # noqa: E402
import flow_transition as ft  # noqa: E402
import gate_runner as gr  # noqa: E402
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
                        reason: str, evidence: str) -> dict:
    """Смена статуса reservation с evidence (жизненный цикл ТЗ 04)."""
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
        paths=tuple(args.zone or ()),
        pipeline_marker=args.pipeline_marker,
        rules_change=args.rules_change,
        small_change=args.small_change,
    )


def _status_exit(status: str) -> int:
    return {ft.ALLOW: 0, ft.DENY: 1}.get(status, 2)


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
        "branch": args.branch,
        "gates": decision.required_gates,
        "human_gate": _approval_summary(args.approval_ref),
        "decision": decision.to_dict(),
    }

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
        "worktree": args.worktree, "branch": args.branch,
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
            "branch": args.branch,
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
                "record": record,
                "note": "enforcing: решение "
                        f"{decision.status} запрещает подготовку (blocks_on)"
                        " — reservation/worktree/goal не создавались",
            }, args.as_json)
        else:
            _emit({
                "schema_version": OUTPUT_SCHEMA, "command": "prepare",
                "correlation_id": correlation, "prepared": False,
                "record": record,
                "note": "действие не разрешено (ALLOW обязателен) — "
                        "reservation/worktree/goal не создавались",
            }, args.as_json)
        return _status_exit(decision.status)

    # 5) Worktree (реальный side effect — до reservation, чтобы записать путь;
    #    при отказе reservation worktree НЕ удаляется молча — разбор ПМ).
    worktree = None
    if args.worktree:
        worktree = str(Path(args.worktree).resolve())
        if sc.git_toplevel(Path(worktree)) is None:
            print(f"FLOWCTL-ERROR: --worktree не git-дерево: {worktree}",
                  file=sys.stderr)
            return 2
    elif args.create_worktree:
        branch = args.branch or f"flow/{correlation[:8]}"
        proc = subprocess.run(
            ["bash", str(SCRIPTS / "session_worktree.sh"), "create",
             str(repo), delegation_id, branch, _git_head(repo)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(f"FLOWCTL-ERROR: worktree не создан: {proc.stderr.strip()}",
                  file=sys.stderr)
            return 2
        worktree = proc.stdout.strip().splitlines()[-1]

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
        "branch": args.branch,
        "base_sha": _git_head(base_tree),
        "snapshot_digest": snapshot["snapshot_digest"],
    }, registry_path)
    if not res["allowed"]:
        _emit({
            "schema_version": OUTPUT_SCHEMA, "command": "prepare",
            "correlation_id": correlation, "prepared": False,
            "reservation": res,
            "note": "reservation отклонена — сессия не создана",
        }, args.as_json)
        return 1

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
            "branch": args.branch,
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
        "branch": args.branch,
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
        "record": record, "reservation": res.get("session"),
        "note": "session/worktree/goal подготовлены; ЗАПУСКА НЕТ "
                "(adapter=manual): исполни delegate_task внешне с goal-"
                "файлом, затем flowctl run --correlation-id "
                f"{correlation} отметит старт",
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
    if record["status"] != "running":
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


# --------------------------------------------------------------------- CLI


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
        p.add_argument("--flow", required=True, type=int,
                       choices=flow_state.FLOW_IDS)
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
    p_ph.add_argument("--flow", required=True, type=int,
                      choices=flow_state.FLOW_IDS)
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

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
