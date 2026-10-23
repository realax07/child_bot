#!/usr/bin/env python3
"""flow_transition.py — проверка действий по снимку FlowSnapshot (поставка 03).

Чистая функция check_action(snapshot, action) -> Decision и CLI check/next.
Правила Флоу 1–5 — таблица этапов (design D3): этап = требуемые факты + роль
+ gates; check_action сверяет факты snapshot с этапом действия. Отсутствующий
критерий правила = UNKNOWN (честно), а не DENY (ложный запрет); UNKNOWN никогда
не разрешает исполнение. DENY перечисляет ВСЕ блокирующие причины.

Provenance-переходы (accept_review/merge): до поставки 05 идентичность
diff/SHA/даты не проверяема — явный UNKNOWN с STALE_EVIDENCE (compatibility
mode, ТЗ 03 п.8, design D4, спека Requirement «Provenance review»).

Shadow mode (срез 1): решение вычисляется честно, но ничего не блокирует,
не запускает агентов и не пишет ни в репозиторий, ни в реестр (спека
deterministic-flow, Requirement «Режим shadow»; контракт §14).

Парсеры flow_check (closed_dev_tasks, approved_review_tasks, parse_verdict)
переиспользуются импортом, не дублируются (ТЗ 03; design D1).

Usage:
    python3 scripts/flow_transition.py check --repo PATH --project ID --flow N \
        --change ID --action ACTION --role ROLE [--task ID] [--approval-ref S]
        [--json] [--registry PATH]
    python3 scripts/flow_transition.py next --repo PATH --project ID --flow N \
        --change ID [--json] [--registry PATH]

Exit codes: 0 — ALLOW; 1 — DENY; 2 — UNKNOWN или ошибка входа.
"""
from __future__ import annotations

import argparse
import json
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import flow_check
import flow_state

DECISION_SCHEMA = "flow-decision/1"

# P0.4 (пересмотр плана Заказчика): непустая строка approval_ref больше не
# достаточна — строка обязана быть decision_id записи журнала решений
# decisions/<YYYY-MM-DD>-<slug>.md с машиночитаемым блоком decision-record/1.
# Журнал решений НЕ является защищенной подписью и НЕ независимым одобрением
# личности Заказчика: в отчетах — «решение зафиксировано», не «личность
# подтверждена» (контракт §10; спека «Честная граница enforcement»).
DECISION_RECORD_SCHEMA = "decision-record/1"
DECISIONS_DIR = "decisions"
# decision_id = имя файла без .md: YYYY-MM-DD-slug (kebab/lower). Path traversal
# исключен разбором по этому шаблону — никаких «..» и разделителей путей.
DECISION_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9._-]*$")

ALLOW = "ALLOW"
DENY = "DENY"
UNKNOWN = "UNKNOWN"

# Стабильные коды причин (контракт §9; FR-4).
MISSING_INPUT = "MISSING_INPUT"
INVALID_GATE = "INVALID_GATE"
HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
WRONG_ROLE = "WRONG_ROLE"
ZONE_CONFLICT = "ZONE_CONFLICT"
STALE_EVIDENCE = "STALE_EVIDENCE"
AMBIGUOUS_STATE = "AMBIGUOUS_STATE"
STALE_SNAPSHOT = "STALE_SNAPSHOT"
EXTERNAL_ENFORCEMENT_UNKNOWN = "EXTERNAL_ENFORCEMENT_UNKNOWN"
# J41 (план релиза — обязательный артефакт ворот): план релиза не найден /
# в плане не объявлен воркфлоу. Отдельные стабильные коды — Заказчик требует
# различать «артефакта нет» и «артефакт неполон» (контракт §9: код — машина-
# читаемая причина, DENY перечисляет все).
PLAN_RELEASE_MISSING = "PLAN_RELEASE_MISSING"
PLAN_FLOW_UNDECLARED = "PLAN_FLOW_UNDECLARED"
# J46 (архитектор — актор ревью, вердикт-ворота): dev_task продуктового пакета
# без architecture-ревью с вердиктом — DENY (ворота не дают devs стартовать,
# пока архитектор не вынес вердикт; урок P15 — новый домен без арх-ревью).
ARCH_REVIEW_MISSING = "ARCH_REVIEW_MISSING"
REASON_CODES = (
    MISSING_INPUT, INVALID_GATE, HUMAN_APPROVAL_REQUIRED, WRONG_ROLE,
    ZONE_CONFLICT, STALE_EVIDENCE, AMBIGUOUS_STATE, STALE_SNAPSHOT,
    EXTERNAL_ENFORCEMENT_UNKNOWN, PLAN_RELEASE_MISSING, PLAN_FLOW_UNDECLARED,
    ARCH_REVIEW_MISSING,
)

# J46: продуктовые дельты пакета — пути в этих зонах (те же зоны, что и в
# политике зон dev/architect; docs-only и бэклог-пакеты продуктовых дельт
# не имеют и architecture-ревью не требуют).
PRODUCT_DELTA_PREFIXES = ("backend/", "services/", "frontend/", "design/")

# Политика перехода к merge/release (пересмотр плана P0.1, п.3; вопрос ПМ —
# дефолт true): действие, требующее внешний факт (branch protection),
# достижимо до ALLOW только при external_enforcement=PASS. При true внешний
# UNKNOWN не превращается скрыто в ALLOW (общий статус остается UNKNOWN,
# разделение local_ready/external_enforcement показывается явно); внешний
# DENY («защита не настроена» — знание) остается DENY. При false решение
# принимается по локальным фактам, а внешний статус показывается рядом
# (явно выбранная политика, не молчаливое упрощение).
MERGE_REQUIRES_EXTERNAL = True

# Статусы external_enforcement в Decision.
EXTERNAL_PASS = "PASS"
EXTERNAL_DENY = "DENY"
EXTERNAL_UNKNOWN = "UNKNOWN"

# Защищенные пути конвейера (Флоу 4, J3 pm_bounds_check).
PROTECTED_PATHS = ("openspec/", "contracts/", "AGENTS.md", "agents/README.md")

# Формат зависимостей в строке задачи tasks.md: «(после 1.1, 1.2)» / «depends: 1.1».
DEPS_RE = re.compile(
    r"(?:после|depends:|зависимости:)\s*"
    r"(\d+(?:\.\d+)*(?:\s*,\s*\d+(?:\.\d+)*)*)",
    re.IGNORECASE,
)
TASK_LINE_RE = re.compile(r"^[-*]\s*\[( |x)\]\s*(\d+(?:\.\d+)*)\s*(.*)$", re.M)
PARALLEL_MARKER = "[P]"


# ---------------------------------------------------------------- модели


@dataclass
class Finding:
    code: str
    detail: str
    unknown: bool = False  # True → критерий не проверяем (UNKNOWN), False → DENY


@dataclass
class ActionRequest:
    """Запрос действия (контракт §6; срез 1 — дополнительные опциональные поля)."""

    actor_role: str
    requested_action: str
    task_id: str | None = None
    approval_ref: object = None       # str (ссылка/цитата) | dict customer_decision v1
    expected_snapshot_digest: str | None = None
    # параллель/зависимости ([P], AGENTS.md п.10):
    task_parallel: bool | None = None
    # подтверждение маркера [P] в tasks.md (R5): CLI ставит после разбора
    # tasks.md; API-вызывающий — после собственной проверки маркера.
    parallel_confirmed: bool | None = None
    task_dependencies: tuple = ()
    dependency_evidence: dict | None = None  # {dep: {task_closed, review_approved}}
    # Флоу 2: фиксирует ввод нового поведения/API (эскалация):
    spec_delta: bool | None = None
    # Флоу 3: ссылка на инцидент/причину:
    incident_ref: str | None = None
    # Флоу 4: затрагиваемые пути и [pipeline]-маркер:
    paths: tuple = ()
    pipeline_marker: bool | None = None
    rules_change: bool | None = None
    # Флоу 5: подтверждение условий экспресс-режима (BACKLOG/README):
    small_change: bool | None = None

    def to_dict(self) -> dict:
        return {
            "actor_role": self.actor_role,
            "requested_action": self.requested_action,
            "task_id": self.task_id,
            "approval_ref": self.approval_ref
            if isinstance(self.approval_ref, (str, type(None))) else "<decision>",
            "task_parallel": self.task_parallel,
            "parallel_confirmed": self.parallel_confirmed,
            "task_dependencies": list(self.task_dependencies),
            "dependency_evidence": {
                str(k): dict(v) if isinstance(v, dict) else v
                for k, v in (self.dependency_evidence or {}).items()
            } or None,
            "expected_snapshot_digest": self.expected_snapshot_digest,
            "spec_delta": self.spec_delta,
            "incident_ref": self.incident_ref,
            "paths": list(self.paths),
            "pipeline_marker": self.pipeline_marker,
            "rules_change": self.rules_change,
            "small_change": self.small_change,
        }


@dataclass
class Decision:
    allowed: bool
    status: str                        # ALLOW | DENY | UNKNOWN
    action: str
    scope: dict
    actor_role: str
    snapshot_digest: str
    requirements_checked: list = field(default_factory=list)
    blocking_reasons: list = field(default_factory=list)
    details: list = field(default_factory=list)
    required_gates: list = field(default_factory=list)
    evidence_refs: list = field(default_factory=list)
    next_candidates: list = field(default_factory=list)
    # Пересмотр плана P0.1 п.3: локальная готовность и внешний enforcement
    # раздельно. local_ready — ALLOW/DENY по локальным фактам; allowed_local
    # — True, когда локальные находки не содержат DENY-причин.
    # external_enforcement — PASS/DENY/UNKNOWN по факту защиты (или None,
    # когда действие внешний факт не проверяет).
    local_ready: str = ""
    allowed_local: bool | None = None
    external_enforcement: str | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": DECISION_SCHEMA,
            "allowed": self.allowed,
            "status": self.status,
            "action": self.action,
            "scope": self.scope,
            "actor_role": self.actor_role,
            "snapshot_digest": self.snapshot_digest,
            "requirements_checked": self.requirements_checked,
            "blocking_reasons": self.blocking_reasons,
            "details": self.details,
            "required_gates": self.required_gates,
            "evidence_refs": self.evidence_refs,
            "next_candidates": self.next_candidates,
            "local_ready": self.local_ready or None,
            "allowed_local": self.allowed_local,
            "external_enforcement": self.external_enforcement,
        }


# ------------------------------------------------------------ доступ к фактам


def _fact(snapshot: dict, key: str, ctx: dict) -> dict | None:
    """Первый факт с ключом; источник фиксируется как evidence."""
    ctx["checked"].add(key)
    for f in snapshot.get("facts", []):
        if f.get("key") == key:
            src = f.get("source")
            if src:
                ctx["evidence"].add(str(src))
            return f
    return None


def _fact_ready(snapshot: dict, key: str, ctx: dict) -> tuple[object, str, list]:
    """(value, status, findings). missing → MISSING_INPUT; unknown/invalid → UNKNOWN."""
    f = _fact(snapshot, key, ctx)
    if f is None or f.get("status") == "missing":
        return None, "missing", [
            Finding(MISSING_INPUT, f"факт «{key}»: отсутствует в снимке — "
                    f"артефакт не найден (источник: inspect, поставка 02)")]
    status = f.get("status", "unknown")
    if status in ("unknown", "invalid"):
        return f.get("value"), status, [
            Finding(MISSING_INPUT, f"факт «{key}»: источник нечитаем/непригоден "
                    f"(status={status}) — проверка невозможна, UNKNOWN честнее "
                    f"предположения (контракт §2, §9)", True)]
    return f.get("value"), status, []


def _require_task(snapshot: dict, action: ActionRequest, ctx: dict,
                  *, closed_denied: bool = True) -> list:
    """Задача tasks.md: существует и открыта (для действий по конкретной задаче)."""
    if not action.task_id:
        return [Finding(MISSING_INPUT,
                        "не указан task_id — действие уровня задачи требует задачу "
                        "tasks.md (scope, контракт §3)")]
    key = f"task.{action.task_id}.status"
    value, status, out = _fact_ready(snapshot, key, ctx)
    if status != "ready":
        out.extend([
            Finding(MISSING_INPUT, f"задача {action.task_id}: факт отсутствует в "
                    f"снимке — пересобери inspect с --task {action.task_id} "
                    f"(openspec/changes/<id>/tasks.md)")])
        return out
    if value == "not_found":
        out.append(Finding(MISSING_INPUT,
                           f"задача {action.task_id} не найдена в "
                           f"openspec/changes/<id>/tasks.md (контракт §3)"))
    elif value == "closed" and closed_denied:
        out.append(Finding(INVALID_GATE,
                           f"задача {action.task_id} уже закрыта — повторный "
                           f"проход этапа не разрешен (AGENTS.md, история не "
                           f"переписывается)"))
    return out


# ------------------------------------------------------- общие проверки этапов


def _requirements_approved(snapshot: dict, ctx: dict) -> list:
    value, status, out = _fact_ready(snapshot, "requirements.status", ctx)
    if status != "ready":
        return out
    if value != "approved":
        out.append(Finding(
            INVALID_GATE,
            f"requirements.md не УТВЕРЖДЕН (status={value}) — этап "
            f"approve_requirements пропущен (ТЗ 03 п.2; evidence: requirements.md)"))
    return out


# J41 (план релиза — обязательный артефакт ворот; урок P15 add-wiki: релиз
# стартовал БЕЗ PLAN-файла — план существовал только в tasks.md СА и в
# переписке ПМ, «по сути» план был, «по форме» артефакта не было, и ворота
# не могли его требовать). Решение Заказчика 2026-10-11: «перед релизом план
# набрасываем, СА лишь детализацию» — план релиза пишет ПМ ДО запуска СА,
# ворота его обязаны требовать машиночитаемо. Легальные расположения артефакта
# (оба, как у J41 backlog): корень репо и каталог change-пакета.
PLAN_RELEASE_FILENAME = "PLAN-{change}.md"
PLAN_FLOW_RE = re.compile(
    r"^\s*\*{0,2}\s*Флоу:\s*([1-5])\s*\*{0,2}[^\n]*$", re.M)


def plan_release_paths(repo: Path, change_id: str, flow: int = 1) -> list[Path]:
    """Кандидаты-пути плана релиза (J41 + J43.1: легкий план Флоу 2/4).

    Флоу 1 (change-пакет): корень репо и каталог change-пакета — оба
    расположения легальны, порядок не важен.
    Флоу 2 (J43.1, матрица Заказчика 2026-10-12: план обязателен, легкий):
    PLAN-BUG-NNN.md в корне репо ИЛИ общий test-model/bugs/PLAN.md (один
    на все баги допустим — содержит строку «Флоу: 2» и покрывает).
    Флоу 4 (легкий): PLAN-<slug>.md в корне репо.
    Флоу 3/5: план не требуем (решение Заказчика: incident_ref уже есть /
    экспресс-режим) — путь не вызывается воротами этих флоу.
    """
    if flow == 2:
        return [
            Path(repo) / f"PLAN-{change_id}.md",
            Path(repo) / "test-model" / "bugs" / "PLAN.md",
        ]
    if flow == 4:
        return [Path(repo) / f"PLAN-{change_id}.md"]
    rel = f"openspec/changes/{change_id}"
    return [
        Path(repo) / f"PLAN-{change_id}.md",
        Path(repo) / rel / "PLAN.md",
    ]


def _plan_release(snapshot: dict, ctx: dict) -> list:
    """План релиза существует и объявляет воркфлоу (J41; J43.1 для Флоу 2/4).

    В духе _requirements_approved: change-идентификатор берется из scope
    снимка (тот же, что использует change.package); файл ищется в легальных
    расположениях СВОЕГО флоу (plan_release_paths): Флоу 1 — корень репо /
    каталог change-пакета; Флоу 2 — PLAN-BUG-NNN.md в корне или общий
    test-model/bugs/PLAN.md; Флоу 4 — PLAN-<slug>.md в корне (матрица
    Заказчика 2026-10-12: Флоу 1/2/4 — план обязателен, 2/4 легкий; Флоу 3/5 —
    план не требуем, ворота этих флоу _plan_release не вызывают). Отсутствие
    файла — DENY PLAN_RELEASE_MISSING (урок P15: без артефакта ворота
    бессильны); файл без объявления воркфлоу (строка вида «Флоу: N», N 1–5,
    допускается жирное выделение) — DENY PLAN_FLOW_UNDECLARED: воркфлоу
    фиксирует, по какой схеме ПМ поведет релиз, без него план — не план.
    Файл есть и воркфлоу объявлен — находок нет (ALLOW не «рисуем», просто
    не добавляем причин).
    """
    scope = snapshot.get("scope", {})
    change_id = scope.get("change")
    repo = scope.get("repo")
    flow = scope.get("flow", 1)
    ctx["checked"].add("plan.release")
    # Как _requirements_approved: нет входа — MISSING_INPUT (честный отказ),
    # не молчаливое разрешение.
    if not repo or not change_id:
        return [Finding(
            MISSING_INPUT,
            "план релиза не проверяем: scope без repo/change — пересобери "
            "inspect (J41; контракт §2)")]
    paths = plan_release_paths(Path(repo), str(change_id), flow)
    for path in paths:
        if not path.is_file():
            continue
        ctx["evidence"].add(str(path))
        text = path.read_text(encoding="utf-8", errors="replace")
        if not PLAN_FLOW_RE.search(text):
            return [Finding(
                PLAN_FLOW_UNDECLARED,
                f"план релиза {path.name} не объявляет воркфлоу — нужна строка "
                f"«Флоу: N» (N 1–5, допускается **жирное**) (J41; правило "
                f"фабрики: план фиксирует, по какой схеме релиз ведется)")]
        return []
    return [Finding(
        PLAN_RELEASE_MISSING,
        f"план релиза не найден (Флоу {flow}): ни одного из: "
        f"{', '.join(str(p) for p in paths)} (J41/J43.1; правило фабрики: "
        f"релиз стартует с плана ПМ, СА лишь детализирует; легкий план "
        f"Флоу 2/4: цель, строка «Флоу: N», зоны записи, критерий приемки)")]


def _canon_rel_path(p: str) -> str:
    """Канонизация относительного пути (MAJ-2 ревью BUG-AF-RECHECK): срез
    ведущих '/' и './', схлопывание сегментов (a/./b/, a/x/../b → b).
    Оставшийся '..' — путь мимо канона (энфорсер такие --path отвергает
    раньше); здесь он просто не матчится с зонами (fail-closed)."""
    q = p.replace("\\", "/")
    while q.startswith("./"):
        q = q[2:]
    q = q.lstrip("/")
    q = posixpath.normpath(q)
    return "" if q == "." else q


def product_paths(action: "ActionRequest") -> list[str]:
    """Пути запроса в продуктовых зонах (J46): backend/services/frontend/design.

    Путь канонизируется (_canon_rel_path) и матчится по префиксу зоны
    (backend/app/x.py) или совпадает с ней самой (backend) — тот же стиль,
    что protected-пути check_chore_task. 'backendx/evil' с зоной 'backend'
    НЕ матчится; 'backend/' и 'backend/./x' матчится после нормализации
    (MAJ-2 ревью BUG-AF-RECHECK).
    """
    out: list[str] = []
    for p in (action.paths or ()):
        c = _canon_rel_path(p)
        if c and any(c == pe.rstrip("/") or c.startswith(pe)
                     for pe in PRODUCT_DELTA_PREFIXES):
            out.append(p)
    return out


ARCH_REVIEW_FILE_RE = re.compile(r"^review-(\d{3})-architecture\.md$", re.I)


def architecture_review_verdict(repo: Path, change_id: str) -> tuple[bool, list[str]]:
    """(есть_верdict_approve, файлы) по test-model/reviews/<change>/ (J46).

    Вердикт парсится как для code_review (flow_check.parse_verdict, формат
    «## Вердикт: …»). Действует вердикт ПОСЛЕДНЕЙ итерации —
    review-NNN-architecture с максимальным NNN (BUG-AF-RECHECK-003): поздний
    return отменяет ранее approve; approve→return→approve — последняя.
    return вердиктом не считается. Неоднозначность — файлы вне канонической
    схемы review-NNN-architecture.md (номер итерации неопределим) или
    несколько файлов с одинаковым максимальным NNN → (False, files) —
    решает человек, молчаливой подмены старым approve нет.
    """
    rev_dir = Path(repo) / "test-model" / "reviews" / change_id
    if not rev_dir.is_dir():
        return False, []
    files = sorted(rev_dir.glob("review-*-architecture.md"))
    if not files:
        return False, []
    parsed = []
    for rf in files:
        m = ARCH_REVIEW_FILE_RE.match(rf.name)
        if m is None:
            # Неопределимый номер итерации — неоднозначность (не молчаливое
            # «файла нет»): разрешение не рискуем
            return False, [str(f) for f in files]
        parsed.append((int(m.group(1)), rf))
    max_rev = max(rev for rev, _ in parsed)
    latest = [rf for rev, rf in parsed if rev == max_rev]
    str_files = [str(f) for f in files]
    if len(latest) > 1:
        return False, str_files  # неоднозначность: одинаковый максимальный NNN
    text = latest[0].read_text(encoding="utf-8", errors="replace")
    return flow_check.parse_verdict(text) == "approve", str_files


def _arch_review_gate(snapshot: dict, action: "ActionRequest", ctx: dict) -> list:
    """Вердикт-ворота архитектора (J46): dev_task пакета с продуктовыми
    дельтами требует test-model/reviews/<change>/review-NNN-architecture.md
    с вердиктом approve. Пакеты без продуктовых дельт (docs-only, бэклог)
    ворота не проходят. Отсутствие scope-входов — честный MISSING_INPUT
    (контракт §2), не молчаливое разрешение.
    """
    if not product_paths(action):
        return []
    ctx["checked"].add("arch.review")
    scope = snapshot.get("scope", {})
    repo = scope.get("repo")
    change_id = scope.get("change")
    if not repo or not change_id:
        return [Finding(
            MISSING_INPUT,
            "architecture-ревью не проверяемо: scope без repo/change — "
            "пересобери inspect (J46; контракт §2)")]
    ok, files = architecture_review_verdict(Path(repo), str(change_id))
    if ok:
        return []
    if files:
        return [Finding(
            ARCH_REVIEW_MISSING,
            f"architecture-ревью без вердикта approve актуальной итерации "
            f"(J46; BUG-AF-RECHECK-003: вердикт review-NNN с максимальным "
            f"номером решает, поздний return отменяет старый approve): "
            f"{', '.join(Path(f).name for f in files)} — нужен "
            f"«## Вердикт: approve» (формат agents/architect_agent.md); "
            f"этап architecture_review — роль архитектора")]
    return [Finding(
        ARCH_REVIEW_MISSING,
        f"architecture-ревью отсутствует: нет "
        f"test-model/reviews/{change_id}/review-NNN-architecture.md — "
        f"пакет с продуктовыми дельтами ({', '.join(product_paths(action))}) "
        f"не стартует dev-задачи без вердикта архитектора (J46; "
        f"этап architecture_review — роль architect)")]


def _sdd_skip_actor(snapshot: dict, ctx: dict) -> str | None:
    """Кто явно решил пропускать SDD (P17): факт sdd.approved_skip.

    Действителен только fact status=ready и value={approved_by: <кто>}.
    Отсутствие факта, битый/безавторный skip — None (гейт не пройден),
    молчаливого skip нет.
    """
    f = _fact(snapshot, "sdd.approved_skip", ctx)
    if not isinstance(f, dict) or f.get("status") != "ready":
        return None
    value = f.get("value")
    if isinstance(value, dict):
        who = str(value.get("approved_by") or "").strip()
        if who:
            return who
    return None


def _arch_review_done(snapshot: dict, ctx: dict) -> list:
    """Этап architecture review: change-пакет + sdd.md + дельты (контракт 2)."""
    out: list = []
    pkg_value, pkg_status, out0 = _fact_ready(snapshot, "change.package", ctx)
    out.extend(out0)
    if pkg_status == "ready":
        if not isinstance(pkg_value, dict) or not pkg_value.get("present"):
            out.append(Finding(INVALID_GATE,
                               "change-пакет openspec/changes/<id>/ не создан — "
                               "этап create_change пропущен (контракт 2)"))
        else:
            missing_files = pkg_value.get("missing_files") or []
            if missing_files:
                out.append(Finding(
                    MISSING_INPUT,
                    f"change-пакет неполон: {', '.join(missing_files)} — "
                    f"architecture review невозможен (контракт 2; "
                    f"evidence: openspec/changes/<id>/)", True))
    else:
        out.append(Finding(
            INVALID_GATE,
            "change-пакет openspec/changes/<id>/ отсутствует — этап create_change "
            "пропущен (контракт 2; ТЗ 03 п.2)"))
    deltas, d_status, out1 = _fact_ready(snapshot, "change.spec_deltas", ctx)
    out.extend(out1)
    if d_status == "ready" and not deltas:
        out.append(Finding(INVALID_GATE,
                           "нет дельт specs/*/spec.md в change-пакете (контракт 2)"))
    sdd_value, sdd_status, out2 = _fact_ready(snapshot, "sdd.present", ctx)
    # P17 (реш. 2026-10-08-arch-review-policy): sdd.md на пакете с обязательным
    # arch-этапом — не опция. Раньше ветка срабатывала только при status=ready,
    # и MISSING-факт (sdd.md нет в репо) уходил в generic MISSING_INPUT без
    # именованной ворота-причины. Теперь отсутствие sdd.md — явный INVALID_GATE;
    # единственный законный проход без sdd.md — явное skip-решение (факт
    # sdd.approved_skip из sdd-skip.md с approved_by), которое НЕ молчаливо:
    # в гейт-выводе обязательная строка «arch: sdd skipped by <кто>».
    skip_actor = _sdd_skip_actor(snapshot, ctx) if sdd_status == "missing" else None
    if skip_actor:
        # Skip покрывает только «sdd.md отсутствует» (MISSING): гасим факт-находку
        # и штраф complete-set по sdd. UNKNOWN/invalid sdd.md skip не покрывает.
        out2 = []
    else:
        out.extend(out2)
    sdd_missing = sdd_status != "ready" or sdd_value is not True
    if sdd_missing:
        if skip_actor:
            ctx.setdefault("extra_gates", []).append(
                f"arch: sdd skipped by {skip_actor} "
                f"(sdd-skip.md; реш. 2026-10-08-arch-review-policy)")
        else:
            out.append(Finding(
                INVALID_GATE,
                "sdd.md отсутствует — активный change требует SDD "
                "(контракт 2; evidence: sdd.md). Проход без SDD — только "
                "явным skip-решением: sdd-skip.md со строкой "
                "«approved_by: <кто>» (реш. 2026-10-08-arch-review-policy)"))
    if out0 or out2 or (d_status != "ready"):
        out.append(Finding(
            INVALID_GATE,
            "пропущен этап architecture review: нет complete-набора "
            "(change-пакет + sdd.md + дельты) (ТЗ 03 п.2; evidence: "
            "openspec/changes/<id>/, sdd.md)"))
    return out


def _approvals_for_task(snapshot: dict, action: ActionRequest, ctx: dict) -> list:
    """Есть ли approve-вердикт ревью для задачи (J10)."""
    value, status, out = _fact_ready(snapshot, "reviews.approved", ctx)
    if status != "ready":
        out.extend([
            Finding(MISSING_INPUT,
                    f"каталог code-reviews/{snapshot.get('scope', {}).get('change', '')}/ "
                    f"пуст или нечитаем — нет approve-вердикта (J10)")])
        return out
    approved = value.get("approved") if isinstance(value, dict) else None
    covered = False
    if approved and action.task_id:
        for name in approved:
            nums = re.findall(r"\d+(?:\.\d+)*", str(name))
            if action.task_id in nums:
                covered = True
                break
    if not covered:
        out.append(Finding(
            INVALID_GATE,
            f"нет approve-вердикта review для задачи {action.task_id} "
            f"(J10; evidence: code-reviews/<change-id>/review-*.md, "
            f"approved={approved})"))
    return out


def _deps_findings(action: ActionRequest, ctx: dict) -> list:
    """Зависимости задачи (AGENTS.md п.10; ТЗ 03 п.1).

    Статусы зависимостей — evidence от flow_check (closed_dev_tasks +
    approved_review_tasks: чекбокс [x] + approve-вердикт, J10). Без evidence —
    UNKNOWN (критерий не собран), не молчаливое разрешение.
    """
    deps = action.task_dependencies or ()
    if not deps:
        return []
    ctx["checked"].add("task.dependencies")
    out: list = []
    if action.dependency_evidence is None:
        return [Finding(
            MISSING_INPUT,
            f"зависимости {', '.join(deps)} не проверяемы по снимку: нет evidence "
            f"(closed_dev_tasks + approved_review_tasks) — UNKNOWN (ТЗ 03 п.1; "
            f"AGENTS.md п.10)", True)]
    for d in deps:
        ev = (action.dependency_evidence or {}).get(d)
        if ev is None:
            out.append(Finding(
                MISSING_INPUT,
                f"зависимость {d}: нет evidence о состоянии (ТЗ 03 п.1)", True))
            continue
        if not ev.get("task_closed"):
            out.append(Finding(
                INVALID_GATE,
                f"зависимая задача {d} ждёт merge предшественницы (AGENTS.md п.10: "
                f"задачи с зависимостями — строго последовательно; "
                f"evidence: openspec/changes/<id>/tasks.md)"))
        elif not ev.get("review_approved"):
            # MIN-4 ревью BUG-AF-RECHECK: причина «не покрыта» из warns
            # парсера (нечитаемый/противоречивый вердикт, SELF_REVIEW) —
            # в диагностике отказа, не молчаливый факт.
            warn = ev.get("review_warning")
            out.append(Finding(
                INVALID_GATE,
                f"зависимость {d} закрыта без code-review approve (J10; "
                f"evidence: code-reviews/<change-id>/review-*.md)"
                + (f"; диагностика: {warn}" if warn else "")))
    return out


# ------------------------------------------------- журнал решений (P0.4)


def parse_decision_record(path: Path, decision_id: str) -> tuple[dict | None,
                                                                 str | None]:
    """Разбор записи журнала решений decisions/<id>.md (P0.4).

    Машиночитаемый блок — ```decision-record fenced JSON (schema
    decision-record/1) в конце человекочитаемого файла. Возвращает
    (record|None, err|None): побитая/неполная запись — (None, причина).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"запись нечитаема: {exc}"
    m = re.search(r"```decision-record\s*(\{.*?\})\s*```", text, re.S)
    if not m:
        return None, ("нет машиночитаемого блока ```decision-record "
                      "(формат: decisions/<YYYY-MM-DD>-<slug>.md, §10)")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        return None, f"блок decision-record — битый JSON: {exc}"
    if not isinstance(data, dict):
        return None, "блок decision-record — не словарь"
    if str(data.get("schema_version", "")) != DECISION_RECORD_SCHEMA:
        return None, (f"schema_version={data.get('schema_version')!r} не "
                      f"{DECISION_RECORD_SCHEMA} — запись не распознана "
                      f"(не интерпретируется «на глаз», контракт §2)")
    if str(data.get("decision_id", "")) != decision_id:
        return None, (f"decision_id записи ({data.get('decision_id')!r}) не "
                      f"совпадает с запрошенным ({decision_id!r})")
    required = ("decision_id", "date", "scope", "action", "commit", "source")
    missing = [k for k in required if not data.get(k)]
    if missing:
        return None, f"запись неполна — нет полей: {', '.join(missing)}"
    return data, None


def load_decision_record(repo: Path, decision_id: str) -> tuple[dict | None,
                                                                str | None]:
    """Ищет запись по decision_id в decisions/ (repo/decisions/<id>.md).

    decision_id обязан соответствовать шаблону YYYY-MM-DD-slug (path
    traversal исключен разбором). Возвращает (record|None, err|None):
    нет файла — (None, None) → «нет записи», побитая — (None, причина).
    """
    did = str(decision_id or "").strip()
    if not DECISION_ID_RE.match(did):
        return None, (f"decision_id {did!r} не соответствует формату "
                      f"YYYY-MM-DD-slug (файл decisions/{did}.md; §10)")
    path = Path(repo) / DECISIONS_DIR / f"{did}.md"
    if not path.is_file():
        return None, None
    return parse_decision_record(path, did)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )
    return (proc.stdout or "").strip()


def _decision_sha_is_fresh(record: dict, repo: Path) -> str | None:
    """Свежесть SHA записи (P0.4): SHA записи — предок HEAD или равен ему.

    Решение не может быть принято ПОСЛЕ изменения, ломающего его
    применимость: если commit записи не в истории HEAD — решение «из
    будущего» относительно текущей версии работы. Аналог releases-журнала
    (решение В1): допуск HEAD ИЛИ родитель коммита записи — журнал
    фиксирует решение после факта. Выбранный принцип: «SHA в истории»,
    НЕ TTL ≤24ч (fact TTL — для наблюдаемых фактов уровня 2; решение —
    норма уровня 1). Возвращает причину отказа или None.
    """
    sha = str(record.get("commit") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        return (f"commit записи ({sha!r}) не похож на SHA — привязка решения "
                f"к версии работы отсутствует")
    head = _git(Path(repo), "rev-parse", "HEAD").lower()
    if not head:
        return "HEAD репо нечитаем — сверка SHA решения невозможна"
    if head.startswith(sha):
        return None
    # Предок HEAD? (git merge-base --is-ancestor, exit 0 = да).
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, head],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return None
    return (f"SHA решения {sha[:12]}… не в истории HEAD {head[:12]}… — решение "
            f"принято после изменения, применимость его к текущей работе не "
            f"подтверждается")


def _decision_log_finding(ref: str, action: ActionRequest, scope: dict,
                          stage_name: str, repo: Path,
                          ctx: dict | None = None) -> Finding | None:
    """Строковый approval_ref = decision_id записи журнала решений (P0.4).

    Запись найдена и валидна (action/scope/SHA/expiration соответствуют
    запросу) → ворота исполнены («решение зафиксировано»). Нет записи,
    чужой action/scope, истекший expiration, строка не из журнала →
    HUMAN_APPROVAL_REQUIRED с конкретикой.
    """
    ctx_evidence = f"{DECISIONS_DIR}/{ref}.md"
    record, err = load_decision_record(repo, ref)
    if err:
        return Finding(
            HUMAN_APPROVAL_REQUIRED,
            f"approval_ref {ref!r}: запись журнала решений не проходит "
            f"проверку: {err} (формат: decisions/<YYYY-MM-DD>-<slug>.md, "
            f"машиночитаемый блок decision-record/1; контракт §10; P0.4)")
    if record is None:
        return Finding(
            HUMAN_APPROVAL_REQUIRED,
            f"approval_ref {ref!r}: записи нет в журнале решений "
            f"({ctx_evidence} не найден). Непустая строка без записи журнала "
            f"решением не является (P0.4); формат: decisions/"
            f"<YYYY-MM-DD>-<slug>.md, decision_id в approval_ref; контракт §10")

    # 1) action записи покрывает запрошенное действие.
    granted = record.get("action")
    if isinstance(granted, list):
        covered = action.requested_action in granted
    else:
        covered = str(granted) == action.requested_action
    if not covered:
        return Finding(
            HUMAN_APPROVAL_REQUIRED,
            f"решение {ref} покрывает действие «{granted}», а не "
            f"«{action.requested_action}» — разрешение чужого действия не "
            f"переносится (P0.4; контракт §10; запись: {ctx_evidence})")

    # 2) scope записи покрывает scope запроса (заполненные поля сверяются).
    rec_scope = record.get("scope")
    if not isinstance(rec_scope, dict):
        return Finding(
            HUMAN_APPROVAL_REQUIRED,
            f"решение {ref}: scope записи ({rec_scope!r}) не словарь — "
            f"привязка к scope отсутствует (P0.4; контракт §10)")
    scope_bindings = (
        ("project", scope.get("project")),
        ("change_id", scope.get("change")),
        ("phase", scope.get("flow")),
    )
    for key, expected in scope_bindings:
        declared = rec_scope.get(key)
        if declared is not None and str(declared) != str(expected):
            return Finding(
                HUMAN_APPROVAL_REQUIRED,
                f"решение {ref} относится к другому scope ({key}={declared}, "
                f"действие требует {expected}) — разрешение не переносится "
                f"(P0.4; спека «Этапные ворота Заказчика»; запись: "
                f"{ctx_evidence})")

    # 3) Свежесть: SHA записи — предок HEAD или равен ему.
    stale = _decision_sha_is_fresh(record, repo)
    if stale:
        return Finding(
            STALE_EVIDENCE,
            f"решение {ref}: {stale} (P0.4; запись: {ctx_evidence})")

    # 4) expiration не истек (если задан).
    expiration = str(record.get("expiration") or "").strip()
    if expiration:
        try:
            exp_dt = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return Finding(
                AMBIGUOUS_STATE,
                f"решение {ref}: expiration {expiration!r} не разбирается "
                f"(ожидается ISO-дата) — проверка истечения невозможна, "
                f"UNKNOWN честнее разрешения (P0.4; запись: {ctx_evidence})",
                True)
        if datetime.now(timezone.utc) > exp_dt:
            return Finding(
                HUMAN_APPROVAL_REQUIRED,
                f"решение {ref} истекло (expiration={expiration}) — требуется "
                f"новое решение (P0.4; запись: {ctx_evidence})")
    if ctx is not None:
        # Валидная запись — evidence уровня 2 (файл журнала в репо).
        ctx.setdefault("evidence", set()).add(ctx_evidence)
    return None


def _approval_finding(action: ActionRequest, scope: dict, stage_name: str,
                      repo: Path | None = None,
                      ctx: dict | None = None) -> Finding | None:
    """Этапные ворота Заказчика (ТЗ 03 п.7; контракт §10; design D5; P0.4).

    P0.4 (пересмотр плана Заказчика): непустая строка approval_ref больше НЕ
    принимается «как есть». Строка обязана быть decision_id записи журнала
    решений decisions/<YYYY-MM-DD>-<slug>.md с машиночитаемым блоком
    decision-record/1. Запись проверяется на соответствие запросу:
      - action записи покрывает requested_action;
      - scope записи (project/change_id/phase) покрывает scope запроса
        (заполненные поля записи сверяются; расхождение заполненного поля
        — отказ, «чужое» решение не переносится);
      - SHA записи (commit) — предок текущего HEAD или равен ему: решение не
        могло быть принято ПОСЛЕ изменения, ломающего его применимость.
        Свежесть — «SHA в истории» (аналогично releases-журналу: допускается
        HEAD и родитель коммита записи), а не TTL ≤24ч: решение — уровень 1
        (норма), а не наблюдаемый факт уровня 2;
      - expiration, если задан, не истек.
    Отказы (нет записи, чужой action/scope, истекший, строка не из журнала) —
    HUMAN_APPROVAL_REQUIRED с указанием формата (decisions/<...>.md, §10).
    Журнал решений — НЕ защищенная подпись и НЕ независимое одобрение
    личности: ворота исполнены = «решение зафиксировано».

    Словарь customer_decision v1 сохраняется для обратной совместимости
    (программные вызовы; D5): проверка grants/scope — как раньше.
    """
    ref = action.approval_ref
    if not ref:
        return Finding(
            HUMAN_APPROVAL_REQUIRED,
            f"действие «{stage_name}» требует зафиксированного решения Заказчика "
            f"(дословная фиксация; «ПМ считает согласованным» решением не является; "
            f"формат: запись в журнале решений decisions/<YYYY-MM-DD>-<slug>.md, "
            f"decision_id в approval_ref; контракт §10; AGENTS.md «Этапные "
            f"ворота Заказчика»)")
    if isinstance(ref, str):
        if repo is None:
            # Журнал не передан (чистый вызов без контекста репо) — проверить
            # запись невозможно: честный отказ, не молчаливое разрешение.
            return Finding(
                HUMAN_APPROVAL_REQUIRED,
                f"approval_ref — строка, но журнал решений недоступен (нет "
                f"репозитория): строковый ref обязан быть decision_id записи "
                f"decisions/<YYYY-MM-DD>-<slug>.md (контракт §10; P0.4)")
        return _decision_log_finding(ref, action, scope, stage_name, repo,
                                     ctx=ctx)
    if isinstance(ref, dict):
        if not ref.get("decision_id") or not isinstance(ref.get("grants"), list):
            return Finding(
                MISSING_INPUT,
                "approval_ref: словарь не соответствует customer_decision v1 — "
                "нет decision_id/grants (контракт §10)")
        if action.requested_action not in ref["grants"]:
            return Finding(
                HUMAN_APPROVAL_REQUIRED,
                f"решение {ref['decision_id']} не покрывает действие "
                f"«{action.requested_action}» (grants: "
                f"{', '.join(map(str, ref['grants']))}; контракт §10)")
        bindings = (
            ("project", scope.get("project")),
            ("change_id", scope.get("change")),
            ("phase", scope.get("flow")),
        )
        for key, expected in bindings:
            declared = ref.get(key)
            if declared is not None and str(declared) != str(expected):
                return Finding(
                    HUMAN_APPROVAL_REQUIRED,
                    f"решение {ref['decision_id']} относится к другой фазе/scope "
                    f"({key}={declared}, действие требует {expected}) — разрешение "
                    f"не переносится после закрытия фазы (ТЗ 03 п.7; спека "
                    f"«Этапные ворота Заказчика»)")
        return None
    return Finding(MISSING_INPUT,
                   "approval_ref: неподдерживаемый тип (ожидается str | dict)")


# ------------------------------------------------------------ проверки этапов
# Каждая функция: (snapshot, action, ctx, flow) -> list[Finding].


def check_approve_requirements(snapshot, action, ctx, flow):
    out: list = []
    value, status, out0 = _fact_ready(snapshot, "requirements.status", ctx)
    out.extend(out0)
    if status == "ready" and value not in ("draft", "approved"):
        out.append(Finding(AMBIGUOUS_STATE,
                           f"requirements.status={value!r}: неожиданное значение "
                           f"(ожидается draft|approved) — источник: requirements.md",
                           True))
    return out


def check_create_change(snapshot, action, ctx, flow):
    # J41: create_change — старт change-пакета; ворота требуют план релиза
    # (PLAN-<change>.md / openspec/changes/<id>/PLAN.md с объявленным
    # воркфлоу) вместе с утвержденными requirements — иначе план появился бы
    # только «когда удобно» (урок P15: план жил в переписке, не артефактом).
    out = list(_requirements_approved(snapshot, ctx))
    out.extend(_plan_release(snapshot, ctx))
    return out


def check_needs_arch(snapshot, action, ctx, flow):
    out = list(_requirements_approved(snapshot, ctx))
    out.extend(_arch_review_done(snapshot, ctx))
    return out


def check_dev_task(snapshot, action, ctx, flow):
    # Транзитивный порядок Флоу 1 (ТЗ 03 п.2 дословно): «утверждённые требования
    # → change/SDD → architecture review → dev task». dev_task проверяет ВСЕХ
    # предшественников, а не только ближайшего этапа — иначе цепочка рвется
    # (review-001 R4: dev_task при draft requirements молчаливо ALLOW).
    out = list(_requirements_approved(snapshot, ctx))
    out.extend(_arch_review_done(snapshot, ctx))
    # J41 (транзитивная защита): dev не стартует без плана релиза даже при
    # проскоченном СА-этапе — план обязан существовать на КАЖДОМ этапе работы
    # change-пакета, а не «всплывать» к релизу (урок P15 add-wiki).
    out.extend(_plan_release(snapshot, ctx))
    # J46 (вердикт-ворота архитектора): продуктовый пакет не стартует dev
    # без architecture-ревью с вердиктом approve (транзитивно с этапа
    # architecture_review — тот же транзитивный принцип, что у плана J41).
    out.extend(_arch_review_gate(snapshot, action, ctx))
    out.extend(_require_task(snapshot, action, ctx))
    out.extend(_deps_findings(action, ctx))
    if action.task_parallel:
        ctx["checked"].add("zones.admit_session")
        # Зоны проверяет admit_session (поставка 04): здесь — обязательная пометка
        # в required_gates, не блокировка (контракт §7 «Параллель и зоны»).
        ctx.setdefault("extra_gates", []).append(
            "admit_session: непересекающиеся зоны записи (поставка 04; AGENTS.md п.10–11)")
        # ТЗ 03 п.1: параллельная задача допускается только при [P] в tasks.md
        # (review-001 R5). Снимок среза 1 не несет факт маркера — подтверждение
        # должно прийти извне (CLI-парсер tasks.md: parallel_confirmed); флаг
        # «на веру» не принимается — без подтверждения UNKNOWN (D3), не ALLOW.
        if action.parallel_confirmed is not True:
            ctx["checked"].add("task.parallel_marker")
            out.append(Finding(
                MISSING_INPUT,
                "task_parallel=True без подтвержденного маркера [P] в tasks.md — "
                "параллельная задача допускается только при [P] (ТЗ 03 п.1); факт "
                "маркера отсутствует в снимке (координация с поставкой 02) → "
                "UNKNOWN честнее доверия флагу (контракт §2, §9; design D3)",
                True))
    return out


def check_design_task(snapshot, action, ctx, flow):
    """design_task (ui_designer, Флоу 1) — решение 2026-10-05-design-task-flow1.

    Тот же транзитивный порядок предшественников, что dev_task (requirements
    approved + arch review + задача существует); параллель-блока нет —
    [design]-мокапы не воюют за стенд. J10 не применяется (мокап не код,
    приемка — утверждение Заказчиком; реш. 2026-10-05-design-task-flow1 п.4).
    """
    out = list(_requirements_approved(snapshot, ctx))
    out.extend(_arch_review_done(snapshot, ctx))
    out.extend(_require_task(snapshot, action, ctx))
    out.extend(_deps_findings(action, ctx))
    ctx.setdefault("checked", {}).update({
        "zones.admit_session": True,
        "task.parallel_marker": True,
    })
    return out


def check_code_review(snapshot, action, ctx, flow):
    if flow == 1:
        return list(_require_task(snapshot, action, ctx))
    # chore/bug PR: ревью по диффу, задача tasks.md не обязательна.
    ctx["checked"].add("diff (внешний, PR)")
    return []


# ------------------------------------------------------- provenance (05)

# Sidecar-файл review-provenance/1 пишется рядом с человекочитаемым
# review-файлом (gate_runner.py record-review); путь по умолчанию:
# <review>.provenance.json.
PROVENANCE_SIDECAR_SUFFIX = ".provenance.json"


def _sha256_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _review_file_revision(name: str | None) -> int:
    """Ревизия из имени review-файла: ЕДИНЫЙ парс с flow_check (review-005
    m3 — ревизия rev-first NNN-task или task-first task-NNN, без второго
    частичного regex). review-006 m4: имена sidecar несут суффикс
    .provenance.json — срезаем его перед матчингом .md$, иначе оба regex
    заанкорены на .md и ни разу не совпадают (rev всегда 0)."""
    if not name:
        return 0
    if name.endswith(PROVENANCE_SIDECAR_SUFFIX):
        name = name[:-len(PROVENANCE_SIDECAR_SUFFIX)]
    m = flow_check.REVIEW_FILE_REV_FIRST.match(name)
    if m:
        return int(m.group(1))
    m = flow_check.REVIEW_FILE_TASK_FIRST.match(name)
    if m:
        return int(m.group(2))
    return 0


def load_review_provenance(repo: Path, change_id: str, task_id: str
                           ) -> tuple[dict | None, str | None, str | None,
                                      dict | None]:
    """Последний sidecar под задачи, покрывающие task_id (ревизия — max).
    Возвращает (sidecar, sidecar_path|None, err|None, foreign|None):
    - побитый sidecar — (None, path, err, None): честный UNKNOWN/AMBIGUOUS;
    - sidecar, не покрывающий task_id, НЕ отбрасывается молча (review-005
      M1): (None, path, None, foreign) — «чужой» approve под задачей есть,
      но он не покрывает эту задачу → DENY, а не невидимость;
    - sidecar под задачу нет вовсе — (None, None, None, None) → легаси."""
    cr_dir = repo / "code-reviews" / change_id
    if not cr_dir.is_dir():
        return None, None, None, None
    candidates: list[tuple[int, str, Path]] = []
    foreign: dict | None = None
    foreign_path: str | None = None
    for pf in sorted(cr_dir.glob(f"review-*{PROVENANCE_SIDECAR_SUFFIX}")):
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, str(pf), f"sidecar нечитаем: {exc}", None
        if not isinstance(data, dict):
            return None, str(pf), "sidecar не словарь", None
        tasks = data.get("task_ids")
        if not isinstance(tasks, list):
            return None, str(pf), "sidecar без task_ids", None
        if task_id and not any(
            task_id in re.findall(r"\d+(?:\.\d+)*", str(t)) for t in tasks
        ):
            # Чужой sidecar запоминается (не отбрасывается молча, M1):
            # максимум по ревизии, чтобы DENY назвал фактический sidecar.
            rev = _review_file_revision(pf.name)
            if foreign is None or rev > _review_file_revision(foreign_path):
                foreign = data
                foreign_path = str(pf)
            continue
        candidates.append((_review_file_revision(pf.name), pf.name, pf))
    if not candidates:
        if foreign is not None:
            return None, foreign_path, None, foreign
        return None, None, None, None
    _, name, pf = max(candidates, key=lambda x: (x[0], x[1]))
    return (json.loads(pf.read_text(encoding="utf-8")), str(pf), None, None)


def provenance_findings(sidecar: dict, snapshot: dict, action, ctx: dict,
                        sidecar_path: str) -> list:
    """Строгая проверка sidecar (ТЗ 05; спека «Provenance review»):
    schema → task/change/SHA → digest → независимость роли.
    Любое несоответствие = DENY; нехватка проверяемых входов = UNKNOWN."""
    out: list = []
    scope = snapshot.get("scope", {})
    change_id = scope.get("change")
    if str(sidecar.get("schema_version", "")) != "review-provenance/1":
        out.append(Finding(
            AMBIGUOUS_STATE,
            f"sidecar {sidecar_path}: schema_version="
            f"{sidecar.get('schema_version')!r} не распознана — проверка "
            f"невозможна (контракт §2: нераспознаваемый формат не "
            f"интерпретируется «на глаз»)", True))
        return out
    if sidecar.get("project") and sidecar["project"] != scope.get("project"):
        out.append(Finding(
            HUMAN_APPROVAL_REQUIRED,
            f"sidecar {sidecar_path}: project {sidecar['project']} не "
            f"соответствует scope {scope.get('project')} — approve чужого "
            f"scope не принимается (ТЗ 05)"))
    if sidecar.get("change") and str(sidecar["change"]) != str(change_id):
        out.append(Finding(
            MISSING_INPUT,
            f"sidecar {sidecar_path}: change {sidecar['change']} ≠ scope "
            f"{change_id} — approve другого change отклоняется (ТЗ 05)"))
    tasks = sidecar.get("task_ids") or []
    if action.task_id and tasks and not any(
        action.task_id in re.findall(r"\d+(?:\.\d+)*", str(t)) for t in tasks
    ):
        out.append(Finding(
            MISSING_INPUT,
            f"sidecar {sidecar_path}: task_ids {tasks} не покрывают задачу "
            f"{action.task_id} — approve другой задачи отклоняется (ТЗ 05)"))
    head = str(snapshot.get("repo_head") or "")
    sha = str(sidecar.get("reviewed_commit_sha") or "")
    if not sha:
        out.append(Finding(
            MISSING_INPUT,
            f"sidecar {sidecar_path}: reviewed_commit_sha отсутствует — "
            f"provenance неполна, UNKNOWN (ТЗ 05)", True))
    elif head and sha != head:
        out.append(Finding(
            STALE_EVIDENCE,
            f"sidecar {sidecar_path}: approve зафиксирован для SHA {sha[:12]}…, "
            f"текущий HEAD {head[:12]}… — после нового коммита прежний approve "
            f"устарел; повторное review обязательно (ТЗ 05; спека «Provenance "
            f"review»); проверка времени остаётся дополнительной, основная — "
            f"идентичность SHA/diff"))
    # review-005 M3: diff_digest обязателен. Digest — единственная защита от
    # approve «того же SHA, другой content» (history rewrite, force-push):
    # sidecar с task/change/SHA и независимой ролью, но без digest —
    # неполное доказательство → UNKNOWN, не ALLOW (контракт §9).
    diff_digest = sidecar.get("diff_digest")
    if not diff_digest:
        out.append(Finding(
            MISSING_INPUT,
            f"sidecar {sidecar_path}: diff_digest отсутствует — утвержденная "
            f"версия работы не зафиксирована, approve «того же SHA, другой "
            f"content» (history rewrite / force-push) неотличим → "
            f"UNKNOWN, не ALLOW (ТЗ 05; контракт §9; review-005 M3); "
            f"перепиши sidecar: gate_runner.py record-review --diff-digest …",
            True))
    elif head:
        actual = _sha256_text(_git_diff_for_head(snapshot, head))
        if actual != diff_digest:
            out.append(Finding(
                STALE_EVIDENCE,
                f"sidecar {sidecar_path}: diff_digest не совпадает с диффом "
                f"HEAD {head[:12]}… — утвержденная версия работы изменилась "
                f"(ТЗ 05)"))
    if str(sidecar.get("author_delegation") or "") == \
            str(sidecar.get("reviewer_delegation") or ""):
        out.append(Finding(
            WRONG_ROLE,
            f"sidecar {sidecar_path}: author_delegation == "
            f"reviewer_delegation — ревью собственной работы запрещено "
            f"(ТЗ 05; спека «Provenance review»)"))
    if sidecar.get("verdict") not in ("approve", "return"):
        out.append(Finding(
            AMBIGUOUS_STATE,
            f"sidecar {sidecar_path}: verdict={sidecar.get('verdict')!r} "
            f"не распознан (ожидается approve|return)", True))
    return out


def _git_diff_for_head(snapshot: dict, head: str) -> str:
    """Дифф, покрывающий HEAD (родитель..HEAD); при отсутствии родителя —
    пустой коммит-дифф (пустая строка, digest пустого входа)."""
    import subprocess
    repo = snapshot.get("scope", {}).get("repo")
    if not repo:
        return ""
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff", f"{head}^..{head}"],
        capture_output=True, text=True,
    )
    return proc.stdout or ""


def _provenance_findings(snapshot, action, ctx: dict, stage: str) -> list:
    """Точка provenance для accept_review/merge_task (поставка 05).

    - sidecar по задаче найден → строгая проверка (DENY при несоответствии
      task/change/SHA/роли; ALLOW возможен);
    - review-файлы есть, sidecar нет → легаси-review: compatibility mode —
      `legacy evidence` + UNKNOWN, новый автоматический merge не разрешается,
      старые merge задним числом незаконными не объявляются (ТЗ 05);
    - review-файлов нет вовсе → факт уже отработан _approvals_for_task.
    """
    ctx["checked"].add("provenance.sidecar")
    scope = snapshot.get("scope", {})
    change_id = scope.get("change")
    repo = scope.get("repo")
    out: list = []
    if not repo or not change_id or not action.task_id:
        ctx["checked"].add("provenance.unverifiable")
        return [Finding(
            MISSING_INPUT,
            f"provenance для {stage}: scope без repo/change/task_id — "
            f"проверка sidecar невозможна, UNKNOWN (ТЗ 05)", True)]
    sidecar, sidecar_path, err, foreign = load_review_provenance(
        Path(repo), change_id, action.task_id)
    if err:
        out.append(Finding(AMBIGUOUS_STATE,
                           f"provenance: {err} — {sidecar_path}", True))
        return out
    if sidecar is not None:
        ctx["evidence"].add(sidecar_path or "")
        return provenance_findings(sidecar, snapshot, action, ctx,
                                   sidecar_path or "")
    if foreign is not None:
        # review-005 M1: sidecar под change есть, но task_ids его не
        # покрывают — это «approve другой задачи», отклонение (DENY),
        # а не тихая невидимость с legacy-UNKNOWN.
        tasks = foreign.get("task_ids") or []
        out.append(Finding(
            MISSING_INPUT,
            f"sidecar {sidecar_path}: task_ids {tasks} не покрывают задачу "
            f"{action.task_id} — approve другой задачи под этим change "
            f"отклоняется (ТЗ 05; review-005 M1); нужен sidecar, "
            f"покрывающий задачу {action.task_id}"))
        return out
    # Легаси: review-файлы без sidecar — compatibility mode (уже не «до
    # поставки 05», а явный ограниченный режим для старых review).
    cr_dir = Path(repo) / "code-reviews" / change_id
    has_md = any(cr_dir.glob("review-*.md")) if cr_dir.is_dir() else False
    if not has_md:
        return []
    ctx["evidence"].add(f"code-reviews/{change_id}/ (legacy evidence)")
    ctx.setdefault("extra_gates", []).append(
        "legacy evidence без provenance-sidecar: новый автоматический merge "
        "запрещен без дополнительной проверки (ТЗ 05; compatibility mode)")
    out.append(Finding(
        STALE_EVIDENCE,
        f"review без provenance-sidecar ({sidecar_path or 'sidecar не найден'}) "
        f"— идентичность task/change/SHA/diff и независимость роли не "
        f"подтверждаемы: legacy evidence, UNKNOWN; запиши sidecar "
        f"(gate_runner.py record-review) или пройди отдельный консервативный "
        f"gate (ТЗ 05; спека «Provenance review»)", True))
    return out


def _hotfix_debt_findings(snapshot, action, ctx, flow) -> list:
    """Хотфикс-долг Флоу 3 (review-001 R6; ТЗ 03 п.4; контракт §7 Флоу 3).

    Пока PR-цикл хотфикса не закрыт, ВСЕ последующие действия этого scope
    получают STALE_EVIDENCE-пометку незакрытого долга. Факт `hotfix.pr_pending`
    в срезе 1 не строится (координация с поставкой 02), поэтому на срезе 1 —
    честный fallback: пометка долга на каждый post-emergency шаг accept_review/
    merge_task Флоу 3 (по D3 — пометка/UNKNOWN, не молчаливое ALLOW).
    """
    if flow != 3:
        return []
    ctx["checked"].add("hotfix.pr_pending")
    ctx.setdefault("extra_gates", []).append(
        "незакрытый долг хотфикса: PR-цикл (review + pr_validate) после "
        "emergency_stabilize обязателен до завершения (контракт §7 Флоу 3; "
        "ТЗ 03 п.4)")
    return [Finding(
        STALE_EVIDENCE,
        "незакрытый долг хотфикса: PR-цикл после emergency_stabilize не "
        "подтвержден закрытым (факт hotfix.pr_pending отсутствует в снимке "
        "среза 1, координация с поставкой 02) — хотфикс не «завершен» merge "
        "без последующего PR (ТЗ 03 п.4; контракт §7 Флоу 3)",
        True)]


def check_accept_review(snapshot, action, ctx, flow):
    """Provenance-переход: строгая проверка по sidecar review-provenance/1
    (поставка 05). Совместимость остаётся ТОЛЬКО для легаси-review без
    sidecar: фиксируется как `legacy evidence` + отдельный UNKNOWN, новый
    автоматический merge не разрешается. Согласованный sidecar с
    task/change/SHA и независимой ролью — ALLOW (ТЗ 05; спека «Provenance
    review»; design D4)."""
    out = _hotfix_debt_findings(snapshot, action, ctx, flow) if flow == 3 else []
    if action.task_id:
        out.extend(_approvals_for_task(snapshot, action, ctx))
    out.extend(_provenance_findings(snapshot, action, ctx, "accept_review"))
    return out


def _optional_fact(snapshot: dict, key: str, ctx: dict) -> tuple[object, str]:
    """(value, status) факта без DENY-находки _fact_ready: отсутствующий
    или missing-факт — «нет данных» (missing), а не блокирующий вход."""
    f = _fact(snapshot, key, ctx)
    if f is None or f.get("status") == "missing":
        return None, "missing"
    return f.get("value"), f.get("status", "unknown")


def _external_protection_status(snapshot, ctx: dict) -> str:
    """Статус внешнего enforcement (branch protection, решение А).

    PASS — свежий (≤24ч) положительный факт с сошедшейся привязкой;
    DENY — отрицательный факт (защита не настроена: 404/нет ruleset с
    enforcement=active + pull_request) — знание, не незнание; UNKNOWN —
    проверки не было или факт нечитаем/чужой/протухший.
    """
    ctx["checked"].add("github.protection")
    value, status = _optional_fact(snapshot, "github.protection", ctx)
    if status == "ready":
        return EXTERNAL_PASS
    if status == "invalid" and isinstance(value, dict) \
            and value.get("protection_ok") is False:
        return EXTERNAL_DENY
    return EXTERNAL_UNKNOWN


def _external_protection_findings(snapshot, ctx: dict) -> tuple[list, str]:
    """Находки + статус внешнего enforcement для merge/release.

    ready (свежий ≤24ч отчет, protection_ok, привязка сошлась) → PASS,
    пометка EXTERNAL_ENFORCEMENT_UNKNOWN не добавляется: внешний enforcement
    подтвержден, полный ALLOW достижим. missing/unknown (проверка не
    проводилась или отчет нечитаем) → честный UNKNOWN-пометка, как раньше.
    invalid (адаптер ответил 404 / отчет чужой, протухший или привязан
    к другому HEAD) → DENY-находка: отрицательный факт, не незнание
    (контракт §9, §12).
    """
    ext = _external_protection_status(snapshot, ctx)
    if ext == EXTERNAL_PASS:
        return [], EXTERNAL_PASS
    if ext == EXTERNAL_DENY:
        return [Finding(
            EXTERNAL_ENFORCEMENT_UNKNOWN,
            "branch protection main не настроена: проверка github_protection "
            "ответила отрицательно (нет ruleset с enforcement=active + "
            "pull_request) — настрой защиту main и запиши отчет адаптером "
            "(контракт §12)")], EXTERNAL_DENY
    return [Finding(
        EXTERNAL_ENFORCEMENT_UNKNOWN,
        "branch protection на main не подтверждена локальным прогоном — пометка "
        "не повышает статус разрешения (FR-7; контракт §12)", True)], EXTERNAL_UNKNOWN


def _set_external_enforcement(ctx: dict, status: str,
                              findings: list | None = None) -> None:
    """Фиксирует статус внешнего enforcement в ctx (поле Decision).

    detail первой внешней находки запоминается как маркер — так _decide
    отделяет внешние находки от локальных для local_ready, даже если
    EXTERNAL_ENFORCEMENT_UNKNOWN добавлен и другими проверками.
    """
    ctx["external_enforcement"] = status
    ctx["external_enforcement_seen"] = True
    if findings:
        ctx["external_enforcement_detail"] = findings[0].detail


def check_merge_task(snapshot, action, ctx, flow):
    out: list = []
    if action.actor_role == "pm":
        out.append(Finding(WRONG_ROLE,
                           "ПМ не делает merge в продуктовых репозиториях (J9; "
                           "pm_bounds_check --product-commits; merge — через "
                           "dev-lead)"))
    out.extend(_approvals_for_task(snapshot, action, ctx))
    out.extend(_hotfix_debt_findings(snapshot, action, ctx, flow))
    out.extend(_provenance_findings(snapshot, action, ctx, "merge_task"))
    # P0.1 (решение А + пересмотр п.3): локальные факты и внешний enforcement
    # раздельно. Статус внешнего факта пишется в ctx и попадает в Decision
    # (external_enforcement); политика MERGE_REQUIRES_EXTERNAL решает,
    # блокирует ли внешний не-PASS общий статус.
    ext_findings, ext_status = _external_protection_findings(snapshot, ctx)
    out.extend(ext_findings)
    _set_external_enforcement(ctx, ext_status, ext_findings)
    return out


def _test_model_sub(snapshot, ctx, sub, deny_detail):
    value, status, out = _fact_ready(snapshot, "test_model.present", ctx)
    if status == "ready":
        if not isinstance(value, dict) or not value.get(sub):
            out.append(Finding(INVALID_GATE, deny_detail))
    return out


# remove-qa-checklist: этап qa_checklist упразднен — check_qa_checklist и
# его Stage удалены (PLAN-remove-qa-checklist.md; матрица Заказчика 2026-10-12).


def check_qa_cases(snapshot, action, ctx, flow):
    """Вход этапа qa_cases — от merge_task/design_validation, без чеклиста
    (remove-qa-checklist): кейсы пишутся по спеке НАПРЯМУЮ, трассировка
    TC → ### Requirement:/#### Scenario:. Источник требований обязан
    существовать: дельты change или существующие спеки (контракт 4)."""
    out: list = []
    deltas, d_status, out0 = _fact_ready(snapshot, "change.spec_deltas", ctx)
    out.extend(out0)
    specs_value, specs_status, out1 = _fact_ready(snapshot, "specs.present", ctx)
    out.extend(out1)
    has_source = (
        (d_status == "ready" and deltas)
        or (specs_status == "ready" and isinstance(specs_value, dict)
            and specs_value.get("count", 0) > 0)
    )
    if not has_source and not (out0 or out1):
        out.append(Finding(INVALID_GATE,
                           "кейсы требуют спеку/дельту — источника нет "
                           "(контракт 4; evidence: openspec/specs/, "
                           "openspec/changes/<id>/specs/)"))
    return out


def check_qa_review(snapshot, action, ctx, flow):
    return list(_test_model_sub(
        snapshot, ctx, "new",
        "нет кейсов в test-model/new/ — ревьюить нечего (контракт 4/5)"))


def check_qa_impact_analysis(snapshot, action, ctx, flow):
    """Роль практики qa_impact_analyst (agents/README.md, решение 3.1):
    impact-анализ пишется в test-model/impact/<change-id>.md на основе дельт
    или существующих спек — источника фактов достаточно, отдельного факта
    impact-файла в снимке среза 1 нет (запись роли — зона поставки 04/05).
    """
    ctx["checked"].add("test_model.impact_file")
    out: list = []
    deltas, d_status, out0 = _fact_ready(snapshot, "change.spec_deltas", ctx)
    out.extend(out0)
    specs_value, specs_status, out1 = _fact_ready(snapshot, "specs.present", ctx)
    out.extend(out1)
    has_source = (
        (d_status == "ready" and deltas)
        or (specs_status == "ready" and isinstance(specs_value, dict)
            and specs_value.get("count", 0) > 0)
    )
    if not has_source and not (out0 or out1):
        out.append(Finding(INVALID_GATE,
                           "impact-анализ требует источник (дельты change или "
                           "существующие спеки) — проверять нечего "
                           "(контракт 3/4; evidence: openspec/changes/<id>/specs/, "
                           "openspec/specs/)"))
    return out


def check_qa_automation(snapshot, action, ctx, flow):
    """Решение Заказчика 3.1-А (S5, 2026-10-02): автоматизация требует
    approved-кейсов ИМЕННО этого change (факт approved_cases_of_change),
    а не глобальной непустоты test-model/approved/ (ложное разрешение S5
    shadow-R6: старые пакеты других change маскировали пустоту своего).
    Глобальная проверка сохраняется: она проверяет сам контур test-model.
    """
    out = list(_test_model_sub(
        snapshot, ctx, "approved",
        "автотесты требуют approved-кейсы — test-model/approved/ пуст "
        "(контракт 5/6)"))
    value, status, out2 = _fact_ready(snapshot, "approved_cases_of_change", ctx)
    out.extend(out2)
    if status == "ready":
        if not isinstance(value, dict) or not value.get("count"):
            out.append(Finding(
                MISSING_INPUT,
                "автотесты без approved-кейсов СВОЕГО change: "
                "test-model/approved/<change-id>/ пуст или отсутствует "
                "(решение Заказчика 3.1-А; контракт 6; evidence: "
                "test-model/approved/<change-id>/)"))
    return out


def check_design_validation(snapshot, action, ctx, flow):
    """Решение Заказчика 2026-10-07-design-validation-flow1: design_validation
    (design_validator, Флоу 1, этап QA) — сверка реализованного UI с
    утвержденными мокапами дизайн-фазы. Требует задачу tasks.md (как
    code_review flow=1); содержательная сверка с мокапами — работа агента
    (agents/design_validator_agent.md), ядро проверяет только скоуп.
    Артефакт — review-файл вердикта в зоне роли
    (test-model/reviews/*/review-*-design.md).
    """
    return list(_require_task(snapshot, action, ctx))


def check_archive_change(snapshot, action, ctx, flow):
    out: list = []
    value, status, out0 = _fact_ready(snapshot, "change.tasks", ctx)
    out.extend(out0)
    if status == "ready":
        open_n = value.get("open") if isinstance(value, dict) else None
        if open_n is None:
            out.append(Finding(AMBIGUOUS_STATE,
                               f"change.tasks={value!r}: неожиданный формат",
                               True))
        elif open_n > 0:
            out.append(Finding(
                INVALID_GATE,
                f"не все задачи закрыты (open={open_n}) — архивация требует [x] "
                f"по всем задачам (контракт 7; evidence: "
                f"openspec/changes/<id>/tasks.md)"))
    # Решение Заказчика 3.1-А (S5, 2026-10-02): архивация требует
    # approved-кейсы ИМЕННО этого change — QA-контур не сводится к глобальной
    # непустоте test-model/approved/ (ложное разрешение S5 shadow-R6).
    ac_value, ac_status, out_ac = _fact_ready(
        snapshot, "approved_cases_of_change", ctx)
    out.extend(out_ac)
    if ac_status == "ready":
        if not isinstance(ac_value, dict) or not ac_value.get("count"):
            out.append(Finding(
                MISSING_INPUT,
                "QA-контур не завершен: нет approved-кейсов СВОЕГО change — "
                "test-model/approved/<change-id>/ пуст или отсутствует "
                "(решение Заказчика 3.1-А; контракты 3–6; evidence: "
                "test-model/approved/<change-id>/)"))
    if not action.approval_ref:
        out.append(Finding(
            MISSING_INPUT,
            "явное разрешение ПМ на архивацию не передано в action.approval_ref "
            "(контракт 7) — проверка невозможна, UNKNOWN", True))
    return out


def check_release(snapshot, action, ctx, flow):
    out: list = []
    # P0.1 (решение Б): факт архивации — пакет openspec/changes/archive/<id>/.
    # ready → UNKNOWN-пометка архивации не добавляется; missing → DENY
    # (релиз до архивации запрещен, решение 3.2-Б); unknown → честный UNKNOWN.
    arch_value, arch_status = _optional_fact(snapshot, "change.archived", ctx)
    if arch_status == "ready":
        pass  # change заархивирован: активного tasks.md нет — это и есть цель
    else:
        # change еще активен (или факт нечитаем): закрытость чекбоксов
        # активного пакета проверяется как раньше.
        value, status, out0 = _fact_ready(snapshot, "change.tasks", ctx)
        out.extend(out0)
        if status == "ready" and isinstance(value, dict) and value.get("open", 1) > 0:
            out.append(Finding(
                INVALID_GATE,
                f"архивация не завершена (open={value.get('open')}) — релиз требует "
                f"закрытого change (ТЗ 03 п.2: release gate после archive)"))
    if arch_status == "missing":
        out.append(Finding(
            INVALID_GATE,
            "release до архивации: пакет openspec/changes/archive/<id>/ не "
            "найден — порядок Флоу 1 строго archive_change → release "
            "(решение Заказчика 3.2-Б; контракт §7)"))
    elif arch_status != "ready":
        out.append(Finding(
            MISSING_INPUT,
            "факт завершенного archive_change (дельты слиты в openspec/specs/, "
            "openspec validate --strict пройден) не проверяем: пакет archive/"
            "не читается — закрытые чекбоксы tasks.md не равны архивации; "
            "UNKNOWN (ТЗ 03 п.2; design D3; контракт 7)",
            True))
    # P0.1 (решение В1, усилено пересмотром): релизное решение Заказчика —
    # файл releases/<id>.md с change-id, словом согласия и привязкой к SHA.
    # ready («решение зафиксировано») → часть HUMAN_APPROVAL_REQUIRED уходит;
    # missing/invalid/unknown (в т.ч. конфликт записей AMBIGUOUS_STATE и
    # устаревший SHA) → требование решения остается. Журнал в том же репо —
    # НЕ независимое одобрение личности Заказчика.
    rel_value, rel_status = _optional_fact(snapshot, "release.approval", ctx)
    if rel_status != "ready" and not action.approval_ref:
        if rel_status == "unknown" and isinstance(rel_value, dict) \
                and rel_value.get("conflict"):
            out.append(Finding(
                AMBIGUOUS_STATE,
                "релизное решение неоднозначно: " +
                str(rel_value.get("reason")) + " — устраните конфликт записей "
                "(releases/<change-id>.md) или передайте approval_ref",
                True))
        else:
            out.append(Finding(
                HUMAN_APPROVAL_REQUIRED,
                "релиз/старт релизной фазы требует решения Заказчика: файла "
                "releases/<change-id>.md нет (или он без change-id/слова "
                "согласия/строки SHA или SHA не совпадает с HEAD), "
                "approval_ref не передан — формат решения: "
                "контракт §10 (этапные ворота Заказчика)"))
    # approval_ref без файла: строковая ссылка срез 1 принимает (D5) —
    # ворота Заказчика считаются пройденными, файл — усиление, не дубликат.
    # P0.1 (решение А + пересмотр п.3): branch protection — тот же факт,
    # что для merge; статус пишется в ctx раздельно от локальных фактов.
    ext_findings, ext_status = _external_protection_findings(snapshot, ctx)
    out.extend(ext_findings)
    _set_external_enforcement(ctx, ext_status, ext_findings)
    return out


def check_bug_fix(snapshot, action, ctx, flow):
    out: list = []
    # J43.1 (матрица Заказчика 2026-10-12): Флоу 2 — план релиза ОБЯЗАТЕЛЕН
    # (легкий: PLAN-BUG-NNN.md в корне или общий test-model/bugs/PLAN.md).
    # Транзитивно с первого этапа: фикс не стартует без плана (урок P15).
    out.extend(_plan_release(snapshot, ctx))
    scope = snapshot.get("scope", {})
    bug_id = str(scope.get("change") or "")
    repo = scope.get("repo")
    value, status, out0 = _fact_ready(snapshot, "test_model.present", ctx)
    out.extend(out0)
    if status == "ready" and isinstance(value, dict) and not value.get("bugs"):
        out.append(Finding(
            MISSING_INPUT,
            f"баг-репорт test-model/bugs/{bug_id}.md (или {bug_id}-*.md) "
            f"отсутствует — Флоу 2 требует BUG-NNN репорт (ТЗ 03 п.3)"))
    elif (status == "ready" and repo and bug_id and not any(
            p.get("code") == "FLOW_ID_INVALID"
            for p in snapshot.get("problems", []))
            and not flow_check.bug_report_paths(Path(repo), bug_id)):
        # J43.1: легальный общий план test-model/bugs/PLAN.md делает каталог
        # bugs/ непустым (факт test_model.present грубый: dir non-empty) —
        # требуем репорт ИМЕННО этого BUG-NNN. При невалидном scope-id
        # (FLOW_ID_INVALID) точная проверка не применяется — scope и так
        # дает AMBIGUOUS_STATE/UNKNOWN.
        # Н5 (аудит 2026-10-23): контракт единый с pr_validate — точное имя
        # BUG-<id>.md ИЛИ BUG-<id>-<slug>.md (flow_check.bug_report_paths);
        # прежде здесь принималось только точное имя, а pr_validate — только
        # glob с суффиксом: файл, прошедший переход Флоу 2, валил PR.
        out.append(Finding(
            MISSING_INPUT,
            f"баг-репорт test-model/bugs/{bug_id}.md (или {bug_id}-*.md) "
            f"отсутствует — Флоу 2 требует BUG-NNN репорт (ТЗ 03 п.3; "
            f"план каталога не считается репортом)"))
    specs_value, specs_status, out1 = _fact_ready(snapshot, "specs.present", ctx)
    out.extend(out1)
    if specs_status != "ready" or (
        isinstance(specs_value, dict) and specs_value.get("count", 0) == 0
    ):
        out.append(Finding(
            MISSING_INPUT,
            "существующая спека, описывающая ожидаемое поведение, обязательна "
            "(ТЗ 03 п.3; evidence: openspec/specs/)"))
    if action.spec_delta:
        out.append(Finding(
            INVALID_GATE,
            "Флоу 2 запрещен: фикс вводит новое ожидаемое поведение/API (дельта "
            "спеки) — эскалация на Флоу 1 / решение Заказчика (ТЗ 03 п.3; "
            "контракт §7 Флоу 2)"))
        ctx.setdefault("override_next", []).extend([
            {"action": "create_change", "task": None, "actor_role": "sa"},
            {"action": "escalate_to_customer", "task": None, "actor_role": "pm"},
        ])
    return out


def check_emergency_stabilize(snapshot, action, ctx, flow):
    out: list = []
    severity = incident_severity(action.incident_ref)
    if not action.incident_ref:
        out.append(Finding(
            MISSING_INPUT,
            "причина инцидента не зафиксирована (action.incident_ref) — "
            "emergency-действие обязано фиксировать причину (ТЗ 03 п.4)", True))
    elif flow == 3 and severity is None:
        # J48 (add-incident-protocol, D1/FR-4): инцидент без severity-префикса
        # S1-/S2-/S3- — неклассифицирован. Это отсутствие входа классификации
        # (DENY-вход), не UNKNOWN-факт: состав реакции выбирается классом (FR-1),
        # ad hoc-реакция без классификации — дефект J48. При этом archival-
        # семантика FR-3 неклассифицированному архивацию не разрешает (см.
        # check_archive_flow3 — там severity перепарсивается независимо, I-2).
        out.append(Finding(
            MISSING_INPUT,
            "инцидент неклассифицирован: incident_ref без severity-префикса "
            "S1-/S2-/S3- (FR-1/FR-4; design D1) — классифицируй инцидент "
            "(S1-…/S2-…/S3-…) до начала реакции"))
    if severity:
        # D1: разобранный severity доступен последующим check-функциям через
        # ctx (контекст ОДНОГО check_action-вызова; между CLI-вызовами не
        # переносится — ворота перепарсивают сами, I-2).
        ctx["incident_severity"] = severity
    ctx.setdefault("extra_gates", []).append(
        "обязательный последующий PR-цикл: review + pr_validate (контракт §7 "
        "Флоу 3; хотфикс без PR не считается завершенным)")
    return out


# ------------------------------------------- инцидент-протокол (J48, Флоу 3)
# add-incident-protocol: severity внутри incident_ref (D1), хранилище
# post-mortem (D4), этапы post_mortem/archive_change и ворота архивации
# (D2/D3). Затрагивается ТОЛЬКО Флоу 3 (NFR-3: Флоу 1/2/4/5 не меняются).

# I-1 ревью: severity-уровни — одним allowlist-множеством; новый уровень
# (например S4) = одна правка + отдельный change-пакет (rules_change).
SEVERITY_PREFIXES = ("S1", "S2", "S3")
# MIN-1 ревью 4.1: regex ВЫВОДИТСЯ из тупла (захват группы для
# incident_severity: m.group(1) → "S1"/"S2"/…), не дублируется литералом.
SEVERITY_PREFIX_RE = re.compile(
    "^(" + "|".join(re.escape(p) for p in SEVERITY_PREFIXES) + ")-")
# D4: хранилище post-mortem — append-only, именование
# <YYYY-MM-DD>-<incident_ref>.md; корреляция ворот — по вхождению incident_ref
# в имя файла ПО ГРАНИЦАМ (начало имени или «-» … «.» или конец; MIN-2 ревью
# 4.1: сырая подстрока удовлетворялась чужим файлом, ref "S1-prod" — файлом
# "2026-10-15-S1-prod-down.md"; детерминированный файловый матч, NFR-1).
INCIDENTS_DIR = "test-model/incidents"
# D4: секции проверяются по ЗАГОЛОВКАМ (## …), не по вхождению в текст
# (MIN-3 ревью 4.1: «root cause» в прозе не считается секцией).
POST_MORTEM_SECTIONS = ("таймлайн", "root cause", "action items")


def incident_severity(incident_ref: str | None) -> str | None:
    """(D1): severity-префикс incident_ref ('S1'…'S3') или None — инцидент
    неклассифицирован (префикса нет)."""
    m = SEVERITY_PREFIX_RE.match(incident_ref or "")
    return m.group(1) if m else None


def _post_mortem_findings(snapshot, action, ctx: dict) -> list:
    """Ворота post-mortem (D2/D4) — общая проверка check_post_mortem и
    check_archive_flow3.

    Требование I-2 ревью: severity перепарсивается из action.incident_ref
    КАЖДЫЙ вызов — CLI-вызов архивации отдельный процесс, ctx от
    emergency_stabilize не переносится; полагаться на ctx["incident_severity"]
    (D1) здесь запрещено. S1/S2 без post-mortem в test-model/incidents/ (с
    секциями таймлайн/root cause/action items) — DENY; S3 — протокол не
    применяется (FR-1); неклассифицированный — DENY MISSING_INPUT.
    """
    ctx["checked"].add("incident.post_mortem")
    severity = incident_severity(action.incident_ref)  # I-2: сам, не из ctx
    if severity is None:
        return [Finding(
            MISSING_INPUT,
            "инцидент неклассифицирован: incident_ref без severity-префикса "
            "S1-/S2-/S3- (FR-4; design D1) — архивацию семантика FR-3 не "
            "разрешает")]
    if severity == "S3":
        ctx["evidence"].add(f"{INCIDENTS_DIR}/ (S3: протокол не применяется)")
        return []
    repo = (snapshot.get("scope", {}) or {}).get("repo")
    if not repo:
        return [Finding(
            AMBIGUOUS_STATE,
            f"post-mortem: scope без repo — проверка {INCIDENTS_DIR}/ "
            f"невозможна, UNKNOWN (design D4)", True)]
    inc_dir = Path(repo) / INCIDENTS_DIR
    ctx["evidence"].add(str(inc_dir))
    ref = action.incident_ref
    # MIN-2 ревью 4.1: матч по границам, не сырой подстрокой — чужой файл
    # (ref "S1-prod" vs "2026-10-15-S1-prod-down.md") не засчитывается.
    ref_re = re.compile(rf"(?:^|-){re.escape(ref)}(?:\.|$)")
    matches = [p for p in sorted(inc_dir.glob("*.md")) if ref_re.search(p.name)] \
        if inc_dir.is_dir() else []
    if not matches:
        return [Finding(
            MISSING_INPUT,
            f"post-mortem инцидента {ref} (severity {severity}) не найден в "
            f"{INCIDENTS_DIR}/ — архивация Флоу 3 S1/S2 без post-mortem "
            f"блокируется (FR-3; design D2/D4; именование "
            f"<YYYY-MM-DD>-<incident_ref>.md)")]
    text = matches[-1].read_text(encoding="utf-8", errors="replace")
    # MIN-3 ревью 4.1: секции — по заголовкам ## (D4), регистронезависимо;
    # упоминание в прозе секцией не считается.
    missing = [s for s in POST_MORTEM_SECTIONS if not any(
        re.match(rf"^#+\s*{re.escape(s)}", line, re.IGNORECASE)
        for line in text.splitlines())]
    if missing:
        return [Finding(
            INVALID_GATE,
            f"post-mortem {matches[-1].name} без заголовков секций: "
            f"{', '.join(missing)} "
            f"— обязательны заголовки ## Таймлайн (timestamps initial "
            f"response), ## Root cause, ## Action items (FR-3; design D4)")]
    return []


def check_post_mortem(snapshot, action, ctx, flow):
    """Этап post_mortem (D3): производство артефакта после стабилизации и
    закрытия PR-цикла, до архивации; роли dev/devops — участники реакции.
    Проверяет тот же факт, что и ворота архивации, — наличие post-mortem
    (дублирование на этапе и воротах — контракт 7: артефакт проверяется в
    момент ворот независимо)."""
    if flow != 3:
        return []
    return _post_mortem_findings(snapshot, action, ctx)


def check_archive_flow3(snapshot, action, ctx, flow):
    """Ворота архивации Флоу 3 (D2; образец — _hotfix_debt_findings):
    flow==3-специфичная Finding-ветка. Флоу 3 S1/S2 без post-mortem в
    test-model/incidents/ — DENY (ненулевой exit CLI, сценарий FR-3); S3 —
    протокол не применяется; неклассифицированный incident_ref — DENY
    MISSING_INPUT (архивацию не разрешает)."""
    if flow != 3:
        return []
    return _post_mortem_findings(snapshot, action, ctx)


def check_deploy_rollback(snapshot, action, ctx, flow):
    """Полномочия проверяет _approval_finding (stage.approval=True)."""
    return [Finding(
        EXTERNAL_ENFORCEMENT_UNKNOWN,
        "деплой/откат: внешние полномочия и состояние среды не подтверждаемы "
        "локально — пометка не повышает статус разрешения (FR-7; контракт §12)",
        True)]


def check_chore_task(snapshot, action, ctx, flow):
    out: list = []
    # J43.1 (матрица Заказчика 2026-10-12): Флоу 4 — план релиза ОБЯЗАТЕЛЕН
    # (легкий: PLAN-<slug>.md в корне репо; та же «Флоу: N»-строка).
    out.extend(_plan_release(snapshot, ctx))
    ctx["checked"].add("protected paths (J3)")
    protected = [
        p for p in action.paths
        if any(p == pe or p.startswith(pe) for pe in PROTECTED_PATHS)
    ]
    if protected and action.pipeline_marker is not True:
        out.append(Finding(
            INVALID_GATE,
            f"изменение защищенных путей ({', '.join(protected)}) требует "
            f"[pipeline]-маркер в subject (J3; pm_bounds_check; ТЗ 03 п.5)"))
    if action.rules_change:
        out.append(Finding(
            HUMAN_APPROVAL_REQUIRED,
            "изменение правил фабрики — отдельный change-пакет по решению "
            "Заказчика: обслуживание не меняет openspec/ (ТЗ 03 п.5; контракт §7 "
            "Флоу 4; pr_validate check_chore)"))
        ctx.setdefault("override_next", []).extend([
            {"action": "create_change", "task": None, "actor_role": "sa"},
        ])
    return out


def check_express_task(snapshot, action, ctx, flow):
    out: list = []
    if action.small_change is not True:
        out.append(Finding(
            MISSING_INPUT,
            "условия экспресс-режима (BACKLOG/README: мелкое изменение, "
            "ограниченный объем) не подтверждены в action.small_change — "
            "UNKNOWN (ТЗ 03 п.6)", True))
    return out


def check_express_close(snapshot, action, ctx, flow):
    """Ретроспективные артефакты обязательны до закрытия (ТЗ 03 п.6; R1.3)."""
    out: list = []
    value, status, out0 = _fact_ready(snapshot, "requirements.status", ctx)
    out.extend(out0)
    if status == "ready" and value != "approved":
        out.append(Finding(
            MISSING_INPUT,
            "ретроспективные requirements обязательны до закрытия цикла (ТЗ 03 "
            "п.6; урок R1.3; evidence: requirements.md)"))
    specs_value, specs_status, out1 = _fact_ready(snapshot, "specs.present", ctx)
    out.extend(out1)
    if specs_status == "ready" and (
        not isinstance(specs_value, dict) or specs_value.get("count", 0) == 0
    ):
        out.append(Finding(
            MISSING_INPUT,
            "ретроспективная spec delta обязательна до закрытия (ТЗ 03 п.6)"))
    tm_value, tm_status, out2 = _fact_ready(snapshot, "test_model.present", ctx)
    out.extend(out2)
    if tm_status == "ready" and isinstance(tm_value, dict) and not tm_value.get("new"):
        out.append(Finding(
            MISSING_INPUT,
            "ретроспективные кейсы обязательны до закрытия (ТЗ 03 п.6)"))
    return out


# --------------------------------------------------------------- таблица этапов
# D3: этап = действие + роли + gates + обязательное решение Заказчика + проверка.


@dataclass(frozen=True)
class Stage:
    action: str
    roles: tuple
    approval: bool
    gates: tuple
    check: Callable


STAGE_TABLE: dict[int, tuple[Stage, ...]] = {
    1: (
        Stage("approve_requirements", ("customer", "pm"), True,
              ("flow_check C1/E3",), check_approve_requirements),
        Stage("create_change", ("sa",), True,
              ("openspec validate --strict",), check_create_change),
        # J46 (решение Заказчика 2026-10-12: «Давай. Но ты еще забыл про map»):
        # architecture_review — этап роли architect, не pm (легаси; реальных
        # вызовов архитектора — 1 за историю). Зона роли в политике уже есть
        # (architecture/** + test-model/reviews/*/review-*-architecture.md).
        Stage("architecture_review", ("architect",), False,
              ("flow_check (контракт 2)",), check_needs_arch),
        Stage("dev_task", ("dev",), False,
              ("flow_check", "pm_bounds_check (J9/J10)"), check_dev_task),
        Stage("design_task", ("ui_designer",), False,
              ("flow_check", "pm_bounds_check (J9/J10)"), check_design_task),
        Stage("code_review", ("code_reviewer",), False,
              ("review-файл с вердиктом (agents/code_reviewer_agent.md)",),
              check_code_review),
        Stage("accept_review", ("code_reviewer",), False,
              ("provenance-блок (поставка 05)",), check_accept_review),
        Stage("merge_task", ("dev_lead", "integrator"), False,
              ("pm_bounds_check --require-review", "pr_validate",
               "branch protection (внеш.)"), check_merge_task),
        # Решение Заказчика 3.1 (S5): роли практики qa_author/qa_case_author —
        # легальные роли конвейера (agents/README.md). remove-qa-checklist:
        # этап qa_checklist удален из Флоу 1, qa_cases принимает вход напрямую
        # от merge_task/design_validation (PLAN-remove-qa-checklist.md).
        Stage("qa_cases", ("qa_author", "qa_case_author"), False,
              ("flow_check (контракт 4)",), check_qa_cases),
        Stage("qa_review", ("qa_case_reviewer",), False,
              ("flow_check (контракт 5)",), check_qa_review),
        Stage("qa_impact_analysis", ("qa_impact_analyst",), False,
              ("flow_check (контракт 4: impact)",),
              check_qa_impact_analysis),
        Stage("qa_automation", ("qa_automation",), False,
              ("flow_check (контракт 6)",), check_qa_automation),
        # Решение Заказчика 2026-10-07-design-validation-flow1: design_validator
        # (сверка реализованного UI с утвержденными мокапами, этап QA) получил
        # ПАРУ действие+роль в графе Флоу 1. До этого зона роли была в политике
        # (test-model/reviews/*/review-*-design.md), но действия не было —
        # enforcing DENY все подстановки (code_review→code_reviewer,
        # qa_automation→qa_automation, qa_review→INVALID_GATE), и проход шел
        # обходом (R7: в составе qa-задачи ролью dev).
        Stage("design_validation", ("design_validator",), False,
              ("flow_check (контракт 5: review-файл вердикта)",),
              check_design_validation),
        # Решение Заказчика 3.3 (S7, 2026-10-02): sa добавлен как легальная роль
        # архивации — контракт 7: автор спек сливает дельты (практика Р6: sa
        # заархивировал f925a57). dev_lead/integrator остаются допустимыми.
        Stage("archive_change", ("sa", "dev_lead", "integrator"), False,
              ("flow_check (контракт 7)", "openspec validate"), check_archive_change),
        Stage("release", ("pm",), True, (), check_release),
    ),
    2: (
        Stage("bug_fix", ("dev",), False,
              ("pr_validate [BUG-NNN]",), check_bug_fix),
        # Решение Заказчика 2026-10-07-code-review-flow2: независимое ревью
        # багфикса — до этого «написать ревью» во Флоу 2 было нечем
        # (accept_review требует уже существующий approve — замкнутый круг),
        # инцидент BUG-010: фикс влит без независимого ревью.
        Stage("code_review", ("code_reviewer",), False,
              ("review-файл с вердиктом (agents/code_reviewer_agent.md)",),
              check_code_review),
        Stage("accept_review", ("code_reviewer",), False,
              ("provenance-блок (поставка 05)",), check_accept_review),
        Stage("merge_task", ("dev_lead", "integrator"), False,
              ("pm_bounds_check --require-review", "pr_validate",
               "branch protection (внеш.)"), check_merge_task),
    ),
    3: (
        Stage("emergency_stabilize", ("dev", "devops"), False,
              ("pr_validate (последующий PR-цикл)",), check_emergency_stabilize),
        Stage("accept_review", ("code_reviewer",), False,
              ("provenance-блок (поставка 05)",), check_accept_review),
        Stage("merge_task", ("dev_lead", "integrator"), False,
              ("pm_bounds_check --require-review", "pr_validate",
               "branch protection (внеш.)"), check_merge_task),
        Stage("deploy_rollback", ("devops", "pm"), True,
              ("внешние полномочия (ТЗ 03 п.4)",), check_deploy_rollback),
        # J48 (D3): post_mortem — этап производства артефакта (после
        # стабилизации и закрытия PR-цикла, до архивации); роли dev/devops —
        # участники реакции. severity-классификация — НЕ отдельный этап, а
        # check на emergency_stabilize (D1).
        Stage("post_mortem", ("dev", "devops"), False,
              ("post-mortem в test-model/incidents/ (секции: таймлайн, root "
               "cause, action items)",), check_post_mortem),
        # J48 (D3): archive_change — этап ворот (D2); роли зеркалируют
        # архивацию Флоу 1 (решение Заказчика 3.3 S7), новых ролей нет.
        Stage("archive_change", ("sa", "dev_lead", "integrator"), False,
              ("flow_transition ворота post-mortem (D2)",),
              check_archive_flow3),
    ),
    4: (
        Stage("chore_task", ("dev",), False,
              ("pr_validate check_chore", "pm_bounds_check (J3)"), check_chore_task),
        Stage("code_review", ("code_reviewer",), False,
              ("review-файл с вердиктом (agents/code_reviewer_agent.md)",),
              check_code_review),
        Stage("accept_review", ("code_reviewer",), False,
              ("provenance-блок (поставка 05)",), check_accept_review),
        Stage("merge_task", ("dev_lead", "integrator"), False,
              ("pm_bounds_check --require-review", "pr_validate",
               "branch protection (внеш.)"), check_merge_task),
    ),
    5: (
        Stage("express_task", ("dev",), False,
              ("pr_validate [change-id]",), check_express_task),
        Stage("express_close", ("pm",), False,
              ("flow_check", "openspec validate"), check_express_close),
    ),
}


# --------------------------------------------------------------- check_action


ACTION_REQUIRES_EXTERNAL = {
    # Пересмотр плана P0.1 п.3: действия, для которых внешний факт branch
    # protection обязателен (политика MERGE_REQUIRES_EXTERNAL применяется).
    "merge_task": True,
    "release": True,
}


def _action_requires_external(action) -> bool:
    """Действие требует внешний факт (branch protection) по политике."""
    return bool(ACTION_REQUIRES_EXTERNAL.get(
        getattr(action, "requested_action", ""), False))


def _split_external_findings(findings: list, ctx: dict) -> list:
    """Находки БЕЗ внешне-фактных (EXTERNAL_ENFORCEMENT_UNKNOWN, добавленных
    _external_protection_findings, когда внешний статус уже вынесен в
    external_enforcement и деталь совпадает с зафиксированной в ctx).
    Локальные находки остаются для local_ready."""
    if not ctx.get("external_enforcement_seen"):
        return findings
    marker = ctx.get("external_enforcement_detail")
    return [f for f in findings
            if not (f.code == EXTERNAL_ENFORCEMENT_UNKNOWN
                    and marker is not None and f.detail == marker)]


def _decide(snapshot, action, findings, ctx, stage, include_next) -> Decision:
    scope = dict(snapshot.get("scope", {})) if isinstance(snapshot, dict) else {}
    ext_status = ctx.get("external_enforcement")
    ext_seen = bool(ctx.get("external_enforcement_seen"))
    # local_ready: ALLOW/DENY только по локальным находкам (внешние находки
    # исключены — у них код EXTERNAL_ENFORCEMENT_UNKNOWN и источник факт
    # github.protection, статус которого уже отражен в external_enforcement).
    local_findings = _split_external_findings(findings, ctx)
    local_deny = any(not f.unknown for f in local_findings)
    local_ready = DENY if local_deny else (
        ALLOW if not local_findings else UNKNOWN)
    allowed_local = not local_deny
    # Политика действия (пересмотр п.3): требует ли действие внешний факт.
    requires_external = ext_seen and _action_requires_external(action)
    if requires_external and ext_status != EXTERNAL_PASS:
        if ext_status == EXTERNAL_DENY or local_deny:
            # Отрицательный внешний факт — знание: общий DENY (находка уже
            # в findings); локальный DENY сильнее любой внешней политики.
            status = DENY
        elif MERGE_REQUIRES_EXTERNAL:
            # UNKNOWN внешний при политике strict — общий UNKNOWN
            # (не скрытое превращение в ALLOW).
            status = UNKNOWN
        else:
            # Явно выбранная политика: решают локальные факты.
            status = local_ready
    else:
        status = DENY if any(not f.unknown for f in findings) else (
            UNKNOWN if findings else ALLOW)
    blocking: list = []
    for f in findings:
        if f.code not in blocking:
            blocking.append(f.code)
    gates = list(stage.gates) if stage else []
    gates.extend(ctx.get("extra_gates") or [])
    next_candidates = list(ctx.get("override_next") or [])
    if include_next and stage is not None:
        flow = scope.get("flow") or 0
        for s in STAGE_TABLE.get(flow, ()):
            probe = ActionRequest(
                actor_role=s.roles[0],
                requested_action=s.action,
                task_id=action.task_id,
                approval_ref=action.approval_ref,
                task_parallel=action.task_parallel,
                task_dependencies=action.task_dependencies,
                dependency_evidence=action.dependency_evidence,
                spec_delta=action.spec_delta,
                incident_ref=action.incident_ref,
                paths=action.paths,
                pipeline_marker=action.pipeline_marker,
                rules_change=action.rules_change,
                small_change=action.small_change,
            )
            probe_decision = check_action(snapshot, probe, include_next=False)
            if probe_decision.status == ALLOW:
                cand = {"action": s.action, "task": probe.task_id,
                        "actor_role": s.roles[0]}
                if cand not in next_candidates:
                    next_candidates.append(cand)
    return Decision(
        allowed=(status == ALLOW),
        status=status,
        action=action.requested_action,
        scope=scope,
        actor_role=action.actor_role,
        snapshot_digest=str(snapshot.get("snapshot_digest", "")) if isinstance(snapshot, dict) else "",
        requirements_checked=sorted(ctx["checked"]),
        blocking_reasons=blocking,
        details=[f"{f.code}: {f.detail}" for f in findings],
        required_gates=gates,
        evidence_refs=sorted(ctx["evidence"]),
        next_candidates=next_candidates,
        local_ready=local_ready,
        allowed_local=allowed_local,
        external_enforcement=(ext_status if ext_seen else None),
    )


def check_action(snapshot: dict, action: ActionRequest,
                 include_next: bool = False) -> Decision:
    """Чистая проверка действия по снимку (контракт §4, §6; FR-3).

    Читает только snapshot + action; ничего не пишет, не запускает агентов.
    UNKNOWN никогда не разрешает исполнение; DENY перечисляет все причины.
    """
    ctx: dict = {"checked": set(), "evidence": set()}
    findings: list = []
    stage = None

    if not isinstance(snapshot, dict) or not str(
        snapshot.get("schema_version", "")
    ).startswith("flow-snapshot"):
        findings.append(Finding(
            AMBIGUOUS_STATE,
            "снимок не распознан (schema_version не flow-snapshot/*) — пересобери "
            "inspect (поставка 02)", True))
        return _decide(snapshot, action, findings, ctx, None, include_next)

    scope = snapshot.get("scope", {})
    if action.expected_snapshot_digest and (
        action.expected_snapshot_digest != snapshot.get("snapshot_digest")
    ):
        findings.append(Finding(
            STALE_SNAPSHOT,
            f"ожидаемый digest {action.expected_snapshot_digest[:12]}… ≠ "
            f"фактическому {str(snapshot.get('snapshot_digest'))[:12]}… — снимок "
            f"устарел, вызови inspect заново (контракт §6)"))

    flow = scope.get("flow")
    if flow not in STAGE_TABLE:
        findings.append(Finding(
            AMBIGUOUS_STATE,
            f"неизвестный flow {flow!r} (ожидается 1–5) — неоднозначный scope "
            f"блокирует действие (контракт §3)", True))
        return _decide(snapshot, action, findings, ctx, None, include_next)

    for p in snapshot.get("problems", []):
        if p.get("code") == "FLOW_ID_INVALID":
            findings.append(Finding(
                AMBIGUOUS_STATE,
                f"scope не соответствует выбранному флоу: {p.get('detail')} "
                f"(ТЗ 02 п.6)", True))

    if not action.requested_action or not action.actor_role:
        findings.append(Finding(
            MISSING_INPUT,
            "actor_role и requested_action обязательны (контракт §6)"))
        return _decide(snapshot, action, findings, ctx, None, include_next)

    stages = {s.action: s for s in STAGE_TABLE[flow]}
    stage = stages.get(action.requested_action)
    if stage is None:
        findings.append(Finding(
            MISSING_INPUT,
            f"действие «{action.requested_action}» не входит в граф Флоу {flow} "
            f"(доступно: {', '.join(sorted(stages))}; контракт §7)", True))
        return _decide(snapshot, action, findings, ctx, None, include_next)

    ctx["checked"].add(f"role:{action.actor_role}")
    if action.actor_role not in stage.roles:
        findings.append(Finding(
            WRONG_ROLE,
            f"действие «{action.requested_action}» зарезервировано ролью "
            f"{'/'.join(sorted(stage.roles))}, фактическая «{action.actor_role}» "
            f"(agents/README.md; контракт §7)"))
    if stage.approval:
        # P0.1 (решение В1): для release зафиксированным решением Заказчика
        # является и файл releases/<change-id>.md (уровень 2: файл в репо с
        # change-id и словом согласия). Если такой факт ready — этапные
        # ворота считаются пройденными без approval_ref.
        waived = False
        if action.requested_action == "release":
            rel_fact = next(
                (f for f in snapshot.get("facts", [])
                 if f.get("key") == "release.approval"), None)
            waived = bool(rel_fact and rel_fact.get("status") == "ready")
        if not waived:
            af = _approval_finding(action, scope, action.requested_action,
                                   repo=scope.get("repo"), ctx=ctx)
            if af is not None:
                findings.append(af)
    findings.extend(stage.check(snapshot, action, ctx, flow))
    return _decide(snapshot, action, findings, ctx, stage, include_next)


# ------------------------------------------------------------------- CLI


def _decision_human(d: Decision) -> str:
    scope = d.scope
    lines = [
        f"flow_transition: {d.status} action={d.action} role={d.actor_role} "
        f"flow={scope.get('flow')} change={scope.get('change')}"
        + (f" task={scope.get('task')}" if scope.get("task") else "")
    ]
    # Пересмотр плана P0.1 п.3: локальная готовность и внешний enforcement
    # показываются раздельно (если действие внешний факт проверяет).
    if d.local_ready or d.external_enforcement is not None:
        lines.append(
            f"local_ready: {d.local_ready or UNKNOWN}"
            + (f"; external_enforcement: {d.external_enforcement}"
               if d.external_enforcement is not None else ""))
    if d.details:
        lines.append("причины/замечания:")
        lines.extend(f"  {x}" for x in d.details)
    else:
        lines.append("причины: нет")
    lines.append(f"gates: {', '.join(d.required_gates) if d.required_gates else '—'}")
    if d.evidence_refs:
        lines.append(f"evidence: {', '.join(d.evidence_refs)}")
    if d.next_candidates:
        lines.append("next_candidates:")
        for c in d.next_candidates:
            task = f" task={c['task']}" if c.get("task") else ""
            lines.append(f"  {c['action']} ({c['actor_role']}){task}")
    lines.append(f"snapshot_digest: {d.snapshot_digest}")
    return "\n".join(lines)


def _task_lines(tasks_text: str) -> dict[str, dict]:
    """task_id → {closed, parallel, deps, title} из текста tasks.md."""
    out: dict[str, dict] = {}
    for m in TASK_LINE_RE.finditer(tasks_text):
        tid = m.group(2)
        body = m.group(3)
        deps_m = DEPS_RE.search(body)
        deps = []
        if deps_m:
            deps = [d.strip() for d in deps_m.group(1).split(",")]
        out[tid] = {
            "closed": m.group(1) == "x",
            "parallel": PARALLEL_MARKER in body,
            "deps": deps,
            "title": body.strip(),
        }
    return out


def _dep_evidence(repo: Path, change_id: str, deps: tuple) -> dict:
    """Evidence по зависимостям: чекбокс (flow_check.closed_dev_tasks) + approve
    (flow_check.approved_review_tasks) — J10-логика, без дублирования парсеров.

    MIN-4 ревью BUG-AF-RECHECK: warns парсера вердиктов пробрасываются в
    evidence каждой зависимости (review_warning) — ворота выводят их в
    диагностику отказа, вместо молчаливого «не покрыта».
    """
    tasks_file = repo / "openspec" / "changes" / change_id / "tasks.md"
    text = ""
    if tasks_file.is_file():
        text = tasks_file.read_text(encoding="utf-8", errors="replace")
    closed = set(flow_check.closed_dev_tasks(text))
    review_warns: list[str] = []
    approved = flow_check.approved_review_tasks(repo, change_id, review_warns)

    def _dep_warn(d: str) -> str | None:
        for w in review_warns:
            if f"задача {d}:" in w:
                return w
        return None

    ev = {}
    for d in deps:
        ev[d] = {
            "task_closed": d in closed,
            "review_approved": any(
                d in re.findall(r"\d+(?:\.\d+)*", name) for name in approved
            ),
            "review_warning": _dep_warn(d),
        }
    return ev


def _task_deps_from_repo(repo: Path, change_id: str, task_id: str) -> tuple[dict, bool]:
    """[P]-маркер и зависимости задачи из tasks.md (read-only)."""
    tasks_file = repo / "openspec" / "changes" / change_id / "tasks.md"
    if not tasks_file.is_file():
        return {}, False
    meta = _task_lines(tasks_file.read_text(encoding="utf-8", errors="replace"))
    info = meta.get(task_id, {})
    return info, bool(info.get("parallel"))


def _snapshot_ctx(args) -> tuple[dict, Path]:
    snapshot = flow_state.inspect(
        repo_arg=args.repo,
        project=args.project,
        flow=args.flow,
        change_id=args.change,
        task_id=args.task,
        registry=args.registry,
    )
    return snapshot, Path(args.repo)


def _action_from_args(args, deps: tuple = (), dep_ev: dict | None = None,
                      task_parallel: bool | None = None,
                      parallel_confirmed: bool | None = None) -> ActionRequest:
    paths = tuple(p for p in (args.paths or "").split(",") if p) \
        if getattr(args, "paths", None) else ()
    return ActionRequest(
        actor_role=args.role,
        requested_action=args.action,
        task_id=args.task,
        approval_ref=args.approval_ref,
        expected_snapshot_digest=args.expected_digest,
        task_parallel=task_parallel if task_parallel is not None else args.parallel,
        parallel_confirmed=parallel_confirmed,
        task_dependencies=deps,
        dependency_evidence=dep_ev,
        spec_delta=args.spec_delta,
        incident_ref=args.incident_ref,
        paths=paths,
        pipeline_marker=args.pipeline_marker,
        rules_change=args.rules_change,
        small_change=args.small_change,
    )


def _cmd_check(args) -> int:
    try:
        snapshot, repo = _snapshot_ctx(args)
    except ValueError as exc:
        print(f"FLOW-TRANSITION-ERROR: {exc}")
        return 2
    deps: tuple = ()
    dep_ev = None
    parallel = args.parallel          # None | True | False (три состояния, R5)
    parallel_confirmed = None
    if args.flow == 1 and args.task:
        info, parallel_auto = _task_deps_from_repo(repo, args.change, args.task)
        deps = tuple(info.get("deps", ()))
        if parallel is None:
            parallel = parallel_auto
        # Маркер [P] подтвержден разбором tasks.md (read-only) — тогда
        # parallel_confirmed=True; флаг --parallel вручную без маркера —
        # остается неподтвержденным → UNKNOWN в check_dev_task (R5).
        if parallel:
            parallel_confirmed = bool(info.get("parallel"))
    if deps:
        dep_ev = _dep_evidence(repo, args.change, deps)
    action = _action_from_args(args, deps=deps, dep_ev=dep_ev,
                               task_parallel=parallel,
                               parallel_confirmed=parallel_confirmed)
    decision = check_action(snapshot, action)
    if args.as_json:
        print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2,
                         sort_keys=True))
    else:
        print(_decision_human(decision))
    return {ALLOW: 0, DENY: 1, UNKNOWN: 2}[decision.status]


def _cmd_next(args) -> int:
    try:
        snapshot, repo = _snapshot_ctx(args)
    except ValueError as exc:
        print(f"FLOW-TRANSITION-ERROR: {exc}")
        return 2
    stages = STAGE_TABLE[args.flow]
    candidates: list = []
    waiting: list = []
    seen = set()

    def probe_and_add(action: ActionRequest) -> None:
        key = (action.requested_action, action.task_id)
        if key in seen:
            return
        seen.add(key)
        # Каждая задача — свой снимок: task-факт гранулярный (поставка 02).
        probe_snapshot = snapshot
        if action.task_id and action.task_id != args.task:
            try:
                probe_snapshot = flow_state.inspect(
                    repo_arg=args.repo, project=args.project, flow=args.flow,
                    change_id=args.change, task_id=action.task_id,
                    registry=args.registry,
                )
            except ValueError:
                probe_snapshot = snapshot
        d = check_action(probe_snapshot, action)
        if d.status == ALLOW:
            candidates.append({"action": action.requested_action,
                               "task": action.task_id,
                               "actor_role": action.actor_role})
        elif d.status == DENY:
            waiting.append({"action": action.requested_action,
                            "task": action.task_id,
                            "reasons": d.details})

    # 1) действия без задачи (или с указанной задачей)
    for s in stages:
        probe_and_add(ActionRequest(
            actor_role=s.roles[0], requested_action=s.action, task_id=args.task,
            approval_ref=args.approval_ref,
            task_parallel=args.parallel, spec_delta=args.spec_delta,
            incident_ref=args.incident_ref,
            paths=tuple(p for p in (args.paths or "").split(",") if p) if args.paths else (),
            pipeline_marker=args.pipeline_marker,
            rules_change=args.rules_change, small_change=args.small_change,
        ))

    # 2) Флоу 1: открытые dev-задачи tasks.md — каждая как кандидат dev_task
    if args.flow == 1:
        tasks_file = repo / "openspec" / "changes" / args.change / "tasks.md"
        if tasks_file.is_file():
            meta = _task_lines(
                tasks_file.read_text(encoding="utf-8", errors="replace"))
            all_deps = {d for info in meta.values() for d in info.get("deps", ())}
            dep_ev = _dep_evidence(repo, args.change, tuple(all_deps)) \
                if all_deps else {}
            for tid, info in sorted(meta.items()):
                if info["closed"]:
                    continue
                deps = tuple(info.get("deps", ()))
                probe_and_add(ActionRequest(
                    actor_role="dev", requested_action="dev_task", task_id=tid,
                    task_parallel=info["parallel"],
                    parallel_confirmed=info["parallel"],  # маркер из tasks.md
                    task_dependencies=deps,
                    dependency_evidence={d: dep_ev[d] for d in deps} if deps else None,
                ))
    result = {
        "schema_version": DECISION_SCHEMA,
        "action": "next",
        "flow": args.flow,
        "scope": snapshot["scope"],
        "candidates": candidates,
        "waiting": waiting,
        "snapshot_digest": snapshot["snapshot_digest"],
    }
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"flow_transition: next flow={args.flow} "
              f"change={args.change} — кандидатов: {len(candidates)}")
        for c in candidates:
            task = f" task={c['task']}" if c.get("task") else ""
            print(f"  → {c['action']} ({c['actor_role']}){task}")
        for w in waiting:
            task = f" task={w['task']}" if w.get("task") else ""
            print(f"  ⛔ {w['action']}{task}: {w['reasons'][0] if w['reasons'] else ''}")
        print(f"snapshot_digest: {snapshot['snapshot_digest']}")
    return 0 if candidates else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="flow_transition.py",
        description="проверка действий по снимку flow_state (поставка 03, shadow)",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p, need_action: bool):
        p.add_argument("--repo", required=True, help="путь к репозиторию/worktree")
        p.add_argument("--project", required=True, help="ID проекта")
        p.add_argument("--flow", required=True, type=int, choices=flow_state.FLOW_IDS)
        p.add_argument("--change", required=True,
                       help="change-id / BUG-NNN / chore")
        p.add_argument("--task", default=None, help="задача tasks.md")
        p.add_argument("--registry", default=None, help="путь к active_sessions.json")
        if need_action:
            p.add_argument("--action", required=True, help="действие из графа (§7)")
            p.add_argument("--role", required=True, help="роль из agents/README.md")
            p.add_argument("--approval-ref", default=None,
                           help="ссылка/цитата решения Заказчика (или JSON decision v1)")
            p.add_argument("--expected-digest", default=None,
                           help="ожидаемый snapshot_digest (STALE_SNAPSHOT)")
            p.add_argument("--parallel", action="store_true", default=None,
                           help="[P]-параллельная задача (без флага — автодетект "
                                "из tasks.md; флаг без [P] в tasks.md → UNKNOWN)")
            p.add_argument("--spec-delta", action="store_true",
                           help="Флоу 2: фикс вводит новое поведение/API")
            p.add_argument("--incident-ref", default=None,
                           help="Флоу 3: ссылка на причину инцидента")
            p.add_argument("--paths", default=None,
                           help="Флоу 4: затрагиваемые пути через запятую")
            p.add_argument("--pipeline-marker", action="store_true",
                           help="Флоу 4: [pipeline]-маркер стоит")
            p.add_argument("--rules-change", action="store_true",
                           help="Флоу 4: действие меняет правила фабрики")
            p.add_argument("--small-change", action="store_true",
                           help="Флоу 5: условия экспресс-режима подтверждены")
        p.add_argument("--json", action="store_true", dest="as_json",
                       help="машинный JSON")

    p_check = sub.add_parser("check", help="проверить одно действие")
    common(p_check, need_action=True)
    p_check.set_defaults(func=_cmd_check)

    p_next = sub.add_parser("next", help="допустимые следующие действия")
    common(p_next, need_action=False)
    p_next.add_argument("--approval-ref", default=None)
    p_next.add_argument("--parallel", action="store_true", default=None)
    p_next.add_argument("--spec-delta", action="store_true")
    p_next.add_argument("--incident-ref", default=None)
    p_next.add_argument("--paths", default=None)
    p_next.add_argument("--pipeline-marker", action="store_true")
    p_next.add_argument("--rules-change", action="store_true")
    p_next.add_argument("--small-change", action="store_true")
    p_next.set_defaults(func=_cmd_next)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
