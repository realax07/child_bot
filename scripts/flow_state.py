#!/usr/bin/env python3
"""flow_state.py — read-only снимок состояния конвейера (FlowSnapshot, поставка 02).

Чистая операция inspect(repo, scope) -> FlowSnapshot: нормализует факты из
requirements.md, openspec/changes/, openspec/specs/, sdd.md, tasks.md,
code-reviews/, test-model/, Git и реестра активных сессий
(~/.hermes/state/active_sessions.json — путь параметром). Никаких записей,
fetch, checkout, запуска агентов. Двукратный вызов на неизменном вводе дает
эквивалентный JSON (временные поля исключены из digest).

Отличаются четыре статуса: missing (артефакта нет), invalid (есть, но не
пригоден), unknown (источник нечитаем/неоднозначен), ready (есть + пригоден).
Готовность НЕ выводится из имени файла или чекбокса tasks.md: метка
«approved» ставится только по evidence конкретной версии (вердикт в
review-файле, статус в requirements.md).

Ключевое правило честности: отсутствие реестра сессий или битый JSON — это
unknown («нет данных»), а НЕ «активных сессий нет».

Usage:
    python3 scripts/flow_state.py inspect --repo PATH --project ID --flow N \
        --change ID [--task ID] [--json] [--registry PATH]

Exit codes: 0 — снимок построен (даже при unknown-фактах без критических
проблем чтения); 1 — есть проблемы чтения Git/файла/реестра (снимок при этом
все равно напечатан); 2 — неверные аргументы CLI / недоступный repo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = "flow-snapshot/1"

FLOW_IDS = (1, 2, 3, 4, 5)

CHANGE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
BUG_ID_RE = re.compile(r"^BUG-\d{3}$", re.IGNORECASE)
CHORE_RE = re.compile(r"^chore$", re.IGNORECASE)

# Флоу 1 — kebab-case change-id; Флоу 2/3 — BUG-NNN; Флоу 4 — [chore];
# Флоу 5 — экспресс-режим: идентификатор change + evidence ретроспективных
# артефактов (requirements/спека-дельта/кейсы) по BACKLOG I1 (см. README фабрики).
FLOW_EXPECTATIONS = {
    1: "kebab-case change-id (openspec/changes/<id>/)",
    2: "BUG-NNN (test-model/bugs/)",
    3: "BUG-NNN (test-model/bugs/)",
    4: "[chore] (обслуживание, спека не меняется)",
    5: "change-id + ретроспективные артефакты (requirements/спека/кейсы)",
}

# Обязательный идентификатор выбранного Флоу (ТЗ 02, п.6; ТЗ 03 п.6).
REQUIRED_MARKERS = {
    1: "change",
    2: "bug",
    3: "bug",
    4: "chore",
    5: "express",
}

CONFIDENCE_VERIFIED = "verified"
CONFIDENCE_UNKNOWN = "unknown"

# Статусы пригодности: ready | missing | invalid | unknown.
STATUS_MISSING = "missing"
STATUS_INVALID = "invalid"
STATUS_UNKNOWN = "unknown"
STATUS_READY = "ready"

# Факт branch protection (решение Б): машиночитаемый JSON gate_runner.py
# github_protection (--report). Свежесть ≤ 24ч; привязка к repo/branch и HEAD.
PROTECTION_REPORT_NAME = "github-protection.json"
PROTECTION_TTL_HOURS = 24
PROTECTION_BRANCH = "main"
# Поля JSON-отчета адаптера github_protection (gate_runner.py).
PROTECTION_SCHEMA = "github-protection/1"

# Усиление change.archived (пересмотр плана, P0.1): слитые дельты —
# переиспользование логики контракта 7 из flow_check (импорт, не дубликат);
# openspec validate — subprocess с таймаутом (CLI может отсутствовать).
ARCHIVED_VALIDATE_TIMEOUT = 120  # секунд на openspec validate --strict
# Подмена команды validate в тестах (иначе tmp-репо гоняют настоящий CLI):
# None → обычный поиск CLI (openspec → npx); callable → подменяет запуск
# (принимает (repo, argv), возвращает exit code); кортеж/строка → фиксированная
# argv с поиском argv[0] в PATH (отсутствие argv[0] = CLI недоступен).
OPENSPEC_VALIDATE_CMD: object = None


# ---------------------------------------------------------------- модели


@dataclass
class Problem:
    code: str
    detail: str
    source: str = ""


@dataclass
class Fact:
    key: str
    value: object
    source: str
    observed_at: str
    fingerprint: str
    confidence: str  # verified | unknown
    status: str = STATUS_READY  # ready | missing | invalid | unknown

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "observed_at": self.observed_at,
            "fingerprint": self.fingerprint,
            "confidence": self.confidence,
            "status": self.status,
        }


# observed_at исключается из digest: повторный вызов дает эквивалентный JSON.
DIGEST_EXCLUDED = ("observed_at",)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------- безопасное чтение


def safe_read(repo: Path, rel: str, problems: list) -> tuple[str | None, str | None]:
    """Читает repo-relative путь. Symlink за пределы repo не следует.

    Возвращает (text, fingerprint) либо (None, None) + проблему в problems
    при ошибке чтения. Отсутствующий файл — не проблема (проверяет вызыватель).
    """
    path = repo / rel
    try:
        resolved = path.resolve(strict=True)
        repo_resolved = repo.resolve()
        if repo_resolved != resolved and repo_resolved not in resolved.parents:
            problems.append(Problem(
                code="PATH_OUTSIDE_REPO",
                detail=f"{rel}: symlink выходит за пределы репозитория",
                source=rel,
            ))
            return None, None
        data = resolved.read_bytes()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        problems.append(Problem(
            code="FILE_READ_ERROR", detail=f"{rel}: не читается: {exc}", source=rel,
        ))
        return None, None
    return data.decode("utf-8", errors="replace"), _sha256_bytes(data)


# ------------------------------------------------------------ Git факты


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30,
    )


def git_facts(repo: Path, problems: list) -> tuple[str, list[str]]:
    """repo_head (SHA HEAD) и dirty_paths. Ошибки — структурированные проблемы."""
    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        problems.append(Problem(
            code="GIT_HEAD_ERROR",
            detail=f"git rev-parse HEAD: {(head.stderr or 'unknown error').strip()}",
            source="git",
        ))
        head_sha = ""
    else:
        head_sha = head.stdout.strip()

    status = _git(repo, "status", "--porcelain")
    if status.returncode != 0:
        problems.append(Problem(
            code="GIT_STATUS_ERROR",
            detail=f"git status: {(status.stderr or 'unknown error').strip()}",
            source="git",
        ))
        dirty = []
    else:
        dirty = sorted(
            ln[3:].strip().strip('"') for ln in status.stdout.splitlines() if ln.strip()
        )
    return head_sha, dirty


# --------------------------------------------------------- файловые факты


def requirements_fact(repo: Path, problems: list) -> Fact:
    """requirements.md: статус утверждения из текста, не из имени файла."""
    key = "requirements.status"
    rel = "requirements.md"
    text, fp = safe_read(repo, rel, problems)
    if text is None:
        status = STATUS_UNKNOWN if any(p.source == rel for p in problems) else STATUS_MISSING
        return Fact(key, None, rel, "", "", CONFIDENCE_UNKNOWN, status)
    m = re.search(r"^>\s*Статус:\s*(.+)$", text, re.M)
    raw = m.group(1).strip() if m else ""
    approved = bool(re.search(r"\bУТВЕРЖДЕН\b", raw, re.IGNORECASE)) if raw else False
    if not raw:
        return Fact(key, None, rel, "", fp, CONFIDENCE_UNKNOWN, STATUS_INVALID)
    return Fact(
        key,
        "approved" if approved else "draft",
        rel, "", fp,
        CONFIDENCE_VERIFIED if approved else CONFIDENCE_UNKNOWN,
        STATUS_READY,
    )


def tasks_fact(repo: Path, change_id: str, problems: list) -> Fact:
    """tasks.md активного change: counts по чекбоксам — факт, не статус готовности."""
    rel = f"openspec/changes/{change_id}/tasks.md"
    text, fp = safe_read(repo, rel, problems)
    if text is None:
        status = STATUS_UNKNOWN if any(p.source == rel for p in problems) else STATUS_MISSING
        return Fact("change.tasks", None, rel, "", "", CONFIDENCE_UNKNOWN, status)
    all_boxes = re.findall(r"^[-*]\s*\[( |x)\]", text, re.M)
    done = sum(1 for b in all_boxes if b == "x")
    return Fact(
        "change.tasks",
        {"total": len(all_boxes), "done": done, "open": len(all_boxes) - done},
        rel, "", fp, CONFIDENCE_VERIFIED, STATUS_READY,
    )


def reviews_fact(repo: Path, change_id: str, problems: list) -> Fact:
    """code-reviews/<change-id>/: наличие и пригодность (вердикт approve)."""
    rel_dir = f"code-reviews/{change_id}"
    d = repo / rel_dir
    if not d.is_dir():
        return Fact("reviews.approved", None, rel_dir, "", "", CONFIDENCE_UNKNOWN, STATUS_MISSING)
    reviews = []
    approved_files = []
    for rf in sorted(d.glob("review-*.md")):
        text, fp = safe_read(repo, str(rf.relative_to(repo)), problems)
        if text is None:
            continue
        verdict = "unknown"
        m = re.search(r"^#{1,4}\s*Вердикт\s*:?\s*(.+)$", text, re.I | re.M)
        if m:
            v = m.group(1).strip().strip("*").lower()
            if re.search(r"\b(approve|approved|одобрен\w*)\b", v):
                verdict = "approve"
            elif re.search(r"\b(return|доработк\w*)\b", v):
                verdict = "return"
        reviews.append({"file": rf.name, "verdict": verdict})
        if verdict == "approve":
            approved_files.append(rf.name)
    if any(r["verdict"] == "unknown" for r in reviews):
        return Fact(
            "reviews.approved",
            {"files": reviews, "approved": approved_files},
            rel_dir, "", _sha256_text(json.dumps(reviews, sort_keys=True, ensure_ascii=False)),
            CONFIDENCE_UNKNOWN, STATUS_UNKNOWN,
        )
    return Fact(
        "reviews.approved",
        {"files": reviews, "approved": approved_files},
        rel_dir, "", _sha256_text(json.dumps(reviews, sort_keys=True, ensure_ascii=False)),
        CONFIDENCE_VERIFIED, STATUS_READY,
    )


def task_fact(repo: Path, change_id: str, task_id: str, problems: list) -> Fact:
    """Статус одного task ID внутри change — независимо от других задач."""
    rel = f"openspec/changes/{change_id}/tasks.md"
    text, fp = safe_read(repo, rel, problems)
    if text is None:
        status = STATUS_UNKNOWN if any(p.source == rel for p in problems) else STATUS_MISSING
        return Fact(f"task.{task_id}.status", None, rel, "", "", CONFIDENCE_UNKNOWN, status)
    m = re.search(
        rf"^[-*]\s*\[( |x)\]\s*{re.escape(task_id)}(?:\.\d+)*\b", text, re.M,
    )
    if m is None:
        return Fact(f"task.{task_id}.status", "not_found", rel, "", fp,
                    CONFIDENCE_UNKNOWN, STATUS_MISSING)
    return Fact(
        f"task.{task_id}.status",
        "closed" if m.group(1) == "x" else "open",
        rel, "", fp, CONFIDENCE_VERIFIED, STATUS_READY,
    )


# ------------------------------------------------------- openspec факты


def change_facts(repo: Path, change_id: str, problems: list) -> list[Fact]:
    """Наличие/пригодность change-пакета openspec/changes/<id>/."""
    facts: list[Fact] = []
    rel = f"openspec/changes/{change_id}"
    d = repo / rel
    if not d.is_dir():
        facts.append(Fact("change.package", None, rel, "", "", CONFIDENCE_UNKNOWN, STATUS_MISSING))
        return facts
    files_present = {name: (repo / rel / name).is_file() for name in
                     ("proposal.md", "design.md", "tasks.md")}
    missing_files = sorted(n for n, ok in files_present.items() if not ok)
    facts.append(Fact(
        "change.package",
        {"present": True, "missing_files": missing_files},
        rel, "", _sha256_text(rel),
        CONFIDENCE_VERIFIED,
        STATUS_INVALID if missing_files else STATUS_READY,
    ))
    facts.append(tasks_fact(repo, change_id, problems))
    # spec deltas
    specs_dir = d / "specs"
    deltas = sorted(
        str(p.relative_to(repo)) for p in specs_dir.rglob("spec.md")
    ) if specs_dir.is_dir() else []
    facts.append(Fact(
        "change.spec_deltas",
        deltas, rel + "/specs", "", _sha256_text("\n".join(deltas)),
        CONFIDENCE_VERIFIED, STATUS_READY,
    ))
    return facts


def specs_fact(repo: Path, problems: list) -> Fact:
    rel = "openspec/specs"
    d = repo / rel
    if not d.is_dir():
        return Fact("specs.present", None, rel, "", "", CONFIDENCE_UNKNOWN, STATUS_MISSING)
    spec_files = sorted(str(p.relative_to(repo)) for p in d.rglob("spec.md"))
    return Fact(
        "specs.present", {"count": len(spec_files), "files": spec_files[:20]},
        rel, "", _sha256_text("\n".join(spec_files)), CONFIDENCE_VERIFIED, STATUS_READY,
    )


def sdd_fact(repo: Path, problems: list) -> Fact:
    text, fp = safe_read(repo, "sdd.md", problems)
    if text is None:
        had_problem = any(p.source == "sdd.md" for p in problems)
        return Fact("sdd.present", None, "sdd.md", "", "", CONFIDENCE_UNKNOWN,
                    STATUS_UNKNOWN if had_problem else STATUS_MISSING)
    return Fact("sdd.present", True, "sdd.md", "", fp, CONFIDENCE_VERIFIED, STATUS_READY)


def test_model_fact(repo: Path, problems: list) -> Fact:
    rel = "test-model"
    d = repo / rel
    if not d.is_dir():
        return Fact("test_model.present", None, rel, "", "", CONFIDENCE_UNKNOWN, STATUS_MISSING)
    sub = {}
    for name in ("checklists", "new", "reviews", "approved", "bugs"):
        p = d / name
        sub[name] = bool(p.is_dir() and any(p.iterdir()))
    return Fact(
        "test_model.present", sub, rel, "", _sha256_text(json.dumps(sub, sort_keys=True)),
        CONFIDENCE_VERIFIED, STATUS_READY,
    )


def approved_cases_fact(repo: Path, change_id: str, problems: list) -> Fact:
    """Решение Заказчика 3.1-А (S5, 2026-10-02): approved-кейсы ИМЕННО этого change.

    Глобальная непустота test-model/approved/ (там лежат старые пакеты) не
    доказывает завершение QA своего change — ложное разрешение S5 в shadow-R6.
    Факт строится по содержимому test-model/approved/<change-id>/: есть файлы
    кейсов → ready/verified; каталога нет → missing; нечитаем/symlink наружу →
    unknown + проблема (не «пусто»).
    """
    key = "approved_cases_of_change"
    rel = f"test-model/approved/{change_id}"
    d = repo / rel
    try:
        resolved = d.resolve(strict=False)
        repo_resolved = repo.resolve()
        if repo_resolved != resolved and repo_resolved not in resolved.parents:
            problems.append(Problem(
                code="PATH_OUTSIDE_REPO",
                detail=f"{rel}: symlink выходит за пределы репозитория",
                source=rel,
            ))
            return Fact(key, None, rel, "", "", CONFIDENCE_UNKNOWN, STATUS_UNKNOWN)
    except OSError as exc:
        problems.append(Problem(
            code="FILE_READ_ERROR", detail=f"{rel}: не читается: {exc}", source=rel,
        ))
        return Fact(key, None, rel, "", "", CONFIDENCE_UNKNOWN, STATUS_UNKNOWN)
    if not resolved.is_dir():
        return Fact(key, None, rel, "", "", CONFIDENCE_UNKNOWN, STATUS_MISSING)
    cases = sorted(str(p.relative_to(resolved)) for p in resolved.rglob("*.md"))
    value = {"change": change_id, "count": len(cases), "cases": cases}
    return Fact(
        key, value, rel, "",
        _sha256_text(json.dumps(value, sort_keys=True, ensure_ascii=False)),
        CONFIDENCE_VERIFIED, STATUS_READY,
    )


# ------------------------------------------------- факты enforcement (P0.1)


def _openspec_validate(repo: Path, problems: list) -> str:
    """openspec validate --strict: 'ok' | 'unavailable' | 'failed'.

    CLI может отсутствовать (нет node/npx в среде) — тогда факт строится
    как unknown с явной причиной (validate=unavailable), а НЕ как успех:
    отсутствие проверки не превращается молчаливо в пройденную (контракт §2).
    Ошибки чтения/запуска добавляются в problems (кроме 'command not found').
    OPENSPEC_VALIDATE_CMD подменяет команду в тестах: callable (repo, argv)
    → exit code; кортеж/строка → фиксированная argv (argv[0] ищется в PATH).
    """
    import shutil
    if callable(OPENSPEC_VALIDATE_CMD):
        rc = OPENSPEC_VALIDATE_CMD(repo, None)
        if rc != 0:
            problems.append(Problem(
                code="OPENSPEC_VALIDATE_ERROR",
                detail=f"openspec validate: exit {rc}",
                source="openspec validate",
            ))
        return "ok" if rc == 0 else "failed"
    if isinstance(OPENSPEC_VALIDATE_CMD, (list, tuple)):
        argv = list(OPENSPEC_VALIDATE_CMD)
        if not shutil.which(argv[0]):
            return "unavailable"
    elif shutil.which("openspec"):
        argv = ["openspec", "validate", "--all", "--strict"]
    elif shutil.which("npx"):
        argv = ["npx", "--no-install", "openspec", "validate", "--all", "--strict"]
    else:
        return "unavailable"
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=ARCHIVED_VALIDATE_TIMEOUT, cwd=str(repo),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        problems.append(Problem(
            code="OPENSPEC_VALIDATE_ERROR",
            detail=f"openspec validate не выполнен: {exc}",
            source="openspec validate",
        ))
        return "failed"
    if proc.returncode != 0:
        problems.append(Problem(
            code="OPENSPEC_VALIDATE_ERROR",
            detail=(proc.stderr or proc.stdout or "").strip()[:400]
                   or f"openspec validate: exit {proc.returncode}",
            source="openspec validate",
        ))
        return "failed"
    return "ok"


def archived_fact(repo: Path, change_id: str, problems: list) -> Fact:
    """Решение Б, усиленное пересмотром плана (P0.1): change заархивирован =
    пакет в openspec/changes/archive/<id>/ И дельты слиты в openspec/specs/
    (логика контракта 7 из flow_check, переиспользуется импортом) И
    `openspec validate --all --strict` проходит.

    ready — каталог существует, дельты слиты, validate ok; missing — каталога
    нет (change еще активен); invalid — дельты не слиты или validate упал
    (знание об ошибке, не незнание); unknown — чтение каталога упало или
    openspec CLI недоступен (validate=unavailable; причина в value.reason).

    До усиления факт отвечал только на вопрос «пакет перемещен?»: слитые
    дельты и успешная проверка OpenSpec оставались за flow_check — теперь
    они часть самого факта (пересмотр плана, п.1).
    """
    key = "change.archived"
    rel = f"openspec/changes/archive/{change_id}"
    d = repo / rel
    try:
        resolved = d.resolve(strict=False)
        repo_resolved = repo.resolve()
        if repo_resolved != resolved and repo_resolved not in resolved.parents:
            problems.append(Problem(
                code="PATH_OUTSIDE_REPO",
                detail=f"{rel}: symlink выходит за пределы репозитория",
                source=rel,
            ))
            return Fact(key, None, rel, "", "", CONFIDENCE_UNKNOWN, STATUS_UNKNOWN)
    except OSError as exc:
        problems.append(Problem(
            code="FILE_READ_ERROR", detail=f"{rel}: не читается: {exc}", source=rel,
        ))
        return Fact(key, None, rel, "", "", CONFIDENCE_UNKNOWN, STATUS_UNKNOWN)
    if not resolved.is_dir():
        return Fact(key, False, rel, "", "", CONFIDENCE_VERIFIED, STATUS_MISSING)
    # Каталог есть: слитые дельты (контракт 7) + openspec validate.
    import flow_check
    all_problems = flow_check.archived_delta_problems(repo)
    delta_problems = sorted(all_problems.get(change_id, []))
    validate_result = _openspec_validate(repo, problems)
    value: dict = {
        "archived": True,
        "deltas_merged": not delta_problems,
        "delta_problems": delta_problems,
        "validate": validate_result,
    }
    fp = _sha256_text(json.dumps(value, sort_keys=True, ensure_ascii=False)
                      + str(resolved))
    if delta_problems:
        return Fact(key, {**value, "reason": "дельты не слиты в openspec/specs/"},
                    rel, "", fp, CONFIDENCE_VERIFIED, STATUS_INVALID)
    if validate_result != "ok":
        return Fact(
            key,
            {**value, "reason": (
                "openspec CLI недоступен — validate не выполнен"
                if validate_result == "unavailable"
                else "openspec validate --strict завершился ошибкой")},
            rel, "", fp, CONFIDENCE_UNKNOWN, STATUS_UNKNOWN,
        )
    return Fact(
        key, value, rel, "", fp, CONFIDENCE_VERIFIED, STATUS_READY,
    )


def release_approval_fact(repo: Path, change_id: str, problems: list,
                          head_sha: str | None = None) -> Fact:
    """Решение В1, усиленное пересмотром плана (P0.1): релизное решение
    Заказчика — файл releases/<change-id>.md с change-id, словом согласия
    и привязкой к SHA.

    Формат (минимальный, машиночитаемый): файл + упоминание change-id +
    маркер согласия («разрешаю»/«погнали»/…) + строка «SHA: <hash>» или
    «commit: <hash>», где hash — актуальный HEAD репо (40+ hex; допускается
    сокращенный префикс ≥7). Журнал решения в том же репо НЕ является
    независимым одобрением Заказчика: в отчетах «решение зафиксировано»,
    не «личность подтверждена».

    ready — все условия и SHA совпадает с HEAD; invalid — чужой change,
    нет слова согласия, нет строки SHA или SHA чужой (DENY-факт, не
    разрешение); AMBIGUOUS_STATE (unknown + причина) — НЕСКОЛЬКО записей
    на один change с РАЗНЫМИ SHA (два файла releases/<id>*.md или две
    строки SHA в одном файле): неоднозначность честнее выбора «на глаз»;
    нечитаемый файл = unknown; нет файла = missing.
    """
    key = "release.approval"
    rel = f"releases/{change_id}.md"
    text, fp = safe_read(repo, rel, problems)
    if text is None:
        had_problem = any(p.source == rel for p in problems)
        status = STATUS_UNKNOWN if had_problem else STATUS_MISSING
        return Fact(key, None, rel, "", "", CONFIDENCE_UNKNOWN, status)
    mentions_change = bool(re.search(rf"\b{re.escape(change_id)}\b", text))
    approval_word = bool(
        re.search(r"\b(разрешаю|погнали|запускай|утверждаю|approved)\b", text, re.I)
    )
    if not (mentions_change and approval_word):
        return Fact(key, None, rel, "", fp or "", CONFIDENCE_UNKNOWN, STATUS_INVALID)
    # Привязка к SHA: строки «SHA: <hash>» / «commit: <hash>» (полный 40+ hex
    # или сокращение ≥7; допускаются завершающие «.»/«»» — договоренность
    # формата из примеров задачи). Пустой HEAD — сверка невозможна.
    sha_lines = re.findall(
        r"^\s*(?:SHA|commit)\s*:\s*`?([0-9a-fA-F]{7,64})`?[.\"']?\s*$",
        text, re.M)
    if not sha_lines:
        return Fact(
            key, {"change": change_id, "file": rel,
                  "reason": "нет строки «SHA: <hash>»/«commit: <hash>» — "
                            "привязка решения к версии работы отсутствует"},
            rel, "", fp or "", CONFIDENCE_UNKNOWN, STATUS_INVALID)
    distinct = {s.lower() for s in sha_lines}
    if len(distinct) > 1:
        # Конфликт записей: два разных SHA на один change — неоднозначно.
        return Fact(
            key, {"change": change_id, "file": rel,
                  "conflict": "несколько записей с разными SHA",
                  "shas": sorted(distinct),
                  "reason": "AMBIGUOUS_STATE: конфликтующие записи релизного "
                            "решения — выбор записи «на глаз» запрещен"},
            rel, "", fp or "", CONFIDENCE_UNKNOWN, STATUS_UNKNOWN)
    sha = next(iter(distinct))
    # Конфликт файлов: другие releases/<id>*.md с иной строкой SHA на тот же
    # change — две записи с разными SHA, выбор «на глаз» запрещен.
    other_shas: set[str] = set()
    rel_dir = repo / "releases"
    if rel_dir.is_dir():
        for cand in sorted(rel_dir.glob(f"{change_id}*.md")):
            if cand.name == f"{change_id}.md":
                continue
            try:
                cand_text = cand.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for s in re.findall(
                    r"^\s*(?:SHA|commit)\s*:\s*`?([0-9a-fA-F]{7,64})`?[.\"']?\s*$",
                    cand_text, re.M):
                if s.lower() != sha:
                    other_shas.add(s.lower())
    if other_shas:
        return Fact(
            key, {"change": change_id, "file": rel,
                  "conflict": "несколько файлов-записей с разными SHA",
                  "shas": sorted({sha, *other_shas}),
                  "reason": "AMBIGUOUS_STATE: конфликтующие записи релизного "
                            "решения — выбор записи «на глаз» запрещен"},
            rel, "", fp or "", CONFIDENCE_UNKNOWN, STATUS_UNKNOWN)
    head = (head_sha if head_sha is not None
            else _git(repo, "rev-parse", "HEAD").stdout.strip()).lower()
    if not head:
        return Fact(
            key, {"change": change_id, "file": rel, "sha": sha,
                  "reason": "HEAD репо нечитаем — сверка SHA решения "
                            "невозможна"},
            rel, "", fp or "", CONFIDENCE_UNKNOWN, STATUS_UNKNOWN)
    if sha != head:
        # Журнал «решение о HEAD, записанного после HEAD»: строка SHA о
        # версии работы, к которой применено решение; коммит самой строки —
        # следующий. Поэтому сверка допускает SHA в текущем HEAD ИЛИ в
        # HEAD^ (родителе коммита, добавившего запись). Новые коммиты ПОВЕРХ
        # записи (HEAD^^ и старше) решение устаревают.
        parent = _git(repo, "rev-parse", "HEAD^").stdout.strip().lower()
        if not parent or sha != parent:
            return Fact(
                key, {"change": change_id, "file": rel, "sha": sha,
                      "reason": f"SHA решения {sha[:12]}… не совпадает с "
                                f"актуальным HEAD {head[:12]}… — решение "
                                f"устарело после нового коммита"},
                rel, "", fp or "", CONFIDENCE_UNKNOWN, STATUS_INVALID)
    return Fact(
        key, {"change": change_id, "file": rel, "sha": head,
              "note": "решение зафиксировано (журнал в репо, не независимое "
                      "одобрение личности)"},
        rel, "", fp or "", CONFIDENCE_VERIFIED, STATUS_READY,
    )


def _parse_iso_utc(text: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def github_protection_fact(repo: Path, problems: list) -> Fact:
    """Решение А: машиночитаемый факт адаптера gate_runner.py github_protection.

    Источник — JSON-отчет <repo>/.flow-evidence/github-protection.json
    (записывается адаптером после GET /repos/{repo}/branches/main/protection).
    Свежесть ≤ 24ч (observed_at); привязка к repo/branch/HEAD обязательна:
    отчет про другой repo/branch или старый HEAD = invalid (не разрешение).
    Отчета нет = missing (проверка не проводилась → UNKNOWN на merge);
    нечитаем/битый/не та схема = unknown + проблема.
    """
    key = "github.protection"
    rel = f".flow-evidence/{PROTECTION_REPORT_NAME}"
    text, fp = safe_read(repo, rel, problems)
    if text is None:
        had_problem = any(p.source == rel for p in problems)
        status = STATUS_UNKNOWN if had_problem else STATUS_MISSING
        return Fact(key, None, rel, "", "", CONFIDENCE_UNKNOWN, status)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        problems.append(Problem(
            code="FILE_READ_ERROR",
            detail=f"{rel}: битый JSON — факт protection нечитаем",
            source=rel,
        ))
        return Fact(key, None, rel, "", fp, CONFIDENCE_UNKNOWN, STATUS_UNKNOWN)
    if not isinstance(data, dict) or data.get("schema_version") != PROTECTION_SCHEMA:
        problems.append(Problem(
            code="FILE_READ_ERROR",
            detail=f"{rel}: schema_version не {PROTECTION_SCHEMA} — факт не "
                   f"распознан, не интерпретируется «на глаз»",
            source=rel,
        ))
        return Fact(key, None, rel, "", fp or "", CONFIDENCE_UNKNOWN, STATUS_UNKNOWN)
    ok = data.get("protection_ok") is True
    if not ok:
        # Адаптер добрался до GitHub, но защита не настроена/не соответствует:
        # это отрицательный факт (invalid), а не незнание.
        detail = str(data.get("detail") or "защита main не настроена")
        return Fact(
            key, {"protection_ok": False, "detail": detail}, rel, "", fp or "",
            CONFIDENCE_VERIFIED, STATUS_INVALID,
        )
    # Привязка: repo (origin), branch, HEAD репо на момент проверки.
    binding: dict = {}
    try:
        origin = _git(repo, "remote", "get-url", "origin").stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        origin = ""
    branch = str(data.get("branch") or "")
    if origin and data.get("repo") and str(data["repo"]) not in origin:
        binding["repo"] = f"{data['repo']} != origin {origin}"
    if branch and branch != PROTECTION_BRANCH:
        binding["branch"] = f"{branch} != {PROTECTION_BRANCH}"
    head_now = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if head_now and data.get("repo_head") and str(data["repo_head"]) != head_now:
        binding["repo_head"] = f"{data['repo_head']} != HEAD {head_now}"
    observed = _parse_iso_utc(str(data.get("observed_at") or ""))
    stale = observed is None or (
        datetime.now(timezone.utc) - observed > timedelta(hours=PROTECTION_TTL_HOURS)
    )
    if stale:
        binding["observed_at"] = (
            f"отчет старше {PROTECTION_TTL_HOURS}ч или без даты"
        )
    if binding:
        return Fact(
            key,
            {"protection_ok": True,
             "binding_problems": sorted(binding.values())},
            rel, "", fp or "", CONFIDENCE_UNKNOWN, STATUS_INVALID,
        )
    return Fact(
        key,
        {"protection_ok": True, "observed_at": str(data.get("observed_at") or ""),
         "branch": branch or PROTECTION_BRANCH},
        rel, "", fp or "", CONFIDENCE_VERIFIED, STATUS_READY,
    )


# -------------------------------------------------------------- реестр


def registry_fact(registry_path: Path, problems: list) -> tuple[Fact, str]:
    """Факт по active_sessions.json + digest реестра.

    Отсутствие/битый JSON = unknown («нет данных»), НЕ «активных сессий нет».
    """
    rel_source = str(registry_path)
    try:
        data = registry_path.read_bytes()
    except FileNotFoundError:
        problems.append(Problem(
            code="REGISTRY_MISSING",
            detail=f"реестр сессий не найден: {registry_path} — состояние сессий unknown",
            source=rel_source,
        ))
        fact = Fact("sessions.state", None, rel_source, "", "", CONFIDENCE_UNKNOWN, STATUS_UNKNOWN)
        return fact, ""
    except OSError as exc:
        problems.append(Problem(
            code="REGISTRY_READ_ERROR",
            detail=f"реестр сессий не читается: {exc}", source=rel_source,
        ))
        fact = Fact("sessions.state", None, rel_source, "", "", CONFIDENCE_UNKNOWN, STATUS_UNKNOWN)
        return fact, ""
    digest = _sha256_bytes(data)
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        problems.append(Problem(
            code="REGISTRY_INVALID_JSON",
            detail=f"реестр сессий: битый JSON: {exc}", source=rel_source,
        ))
        fact = Fact("sessions.state", None, rel_source, "", digest, CONFIDENCE_UNKNOWN, STATUS_UNKNOWN)
        return fact, digest
    sessions = parsed.get("sessions", []) if isinstance(parsed, dict) else None
    if not isinstance(sessions, list):
        problems.append(Problem(
            code="REGISTRY_INVALID_JSON",
            detail="реестр сессий: нет массива sessions", source=rel_source,
        ))
        fact = Fact("sessions.state", None, rel_source, "", digest, CONFIDENCE_UNKNOWN, STATUS_UNKNOWN)
        return fact, digest
    matching = [s for s in sessions
                if isinstance(s, dict) and s.get("project") and isinstance(sessions, list)]
    return Fact(
        "sessions.state",
        {"count": len(sessions), "projects": sorted({
            s.get("project") for s in sessions
            if isinstance(s, dict) and s.get("project")
        })},
        rel_source, "", digest, CONFIDENCE_VERIFIED, STATUS_READY,
    ), digest


# ------------------------------------------------------------ проверки scope


def check_flow_identifier(repo: Path, flow: int, change_id: str, problems: list) -> None:
    """Обязательный идентификатор выбранного Флоу (ТЗ 02 п.6)."""
    if flow in (2, 3):
        if not BUG_ID_RE.match(change_id):
            problems.append(Problem(
                code="FLOW_ID_INVALID",
                detail=f"Флоу {flow} требует идентификатор BUG-NNN, получено: {change_id!r}",
                source="scope",
            ))
    elif flow == 1:
        if not CHANGE_ID_RE.match(change_id):
            problems.append(Problem(
                code="FLOW_ID_INVALID",
                detail=f"Флоу 1 требует kebab-case change-id, получено: {change_id!r}",
                source="scope",
            ))
    elif flow == 4:
        if not CHORE_RE.match(change_id):
            problems.append(Problem(
                code="FLOW_ID_INVALID",
                detail=f"Флоу 4 (обслуживание) требует маркер [chore], получено: {change_id!r}",
                source="scope",
            ))
    elif flow == 5:
        if not CHANGE_ID_RE.match(change_id):
            problems.append(Problem(
                code="FLOW_ID_INVALID",
                detail=f"Флоу 5 (экспресс) требует change-id, получено: {change_id!r}",
                source="scope",
            ))
        # Ретроспективные артефакты Флоу 5: их отсутствие = unknown, не ready.
        retro_missing = []
        for rel in ("requirements.md",):
            if not (repo / rel).is_file():
                retro_missing.append(rel)
        if retro_missing:
            problems.append(Problem(
                code="FLOW5_RETRO_MISSING",
                detail="Флоу 5: нет ретроспективных артефактов: " + ", ".join(retro_missing),
                source="scope",
            ))


# ---------------------------------------------------------------- inspect


def inspect(
    repo_arg: str | os.PathLike,
    project: str,
    flow: int,
    change_id: str,
    task_id: str | None = None,
    registry: str | os.PathLike | None = None,
) -> dict:
    """Чистый снимок FlowSnapshot. Ничего не пишет и не меняет."""
    problems: list[Problem] = []
    repo = Path(repo_arg)
    try:
        repo = repo.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"репозиторий недоступен: {repo_arg}: {exc}") from exc
    if not repo.is_dir():
        raise ValueError(f"репозиторий не найден: {repo_arg}")

    if flow not in FLOW_IDS:
        raise ValueError(f"неизвестный flow: {flow!r} (ожидается 1–5)")
    if not project:
        raise ValueError("project обязателен")
    if not change_id:
        raise ValueError("change обязателен")

    check_flow_identifier(repo, flow, change_id, problems)

    facts: list[Fact] = []

    head_sha, dirty_paths = git_facts(repo, problems)
    facts.append(Fact(
        "git.head", head_sha or None, "git", "", _sha256_text(head_sha),
        CONFIDENCE_VERIFIED if head_sha else CONFIDENCE_UNKNOWN,
        STATUS_READY if head_sha else STATUS_UNKNOWN,
    ))
    facts.append(Fact(
        "git.dirty_paths", dirty_paths, "git", "", _sha256_text(json.dumps(dirty_paths)),
        CONFIDENCE_VERIFIED, STATUS_READY,
    ))

    facts.append(requirements_fact(repo, problems))
    facts.append(specs_fact(repo, problems))
    facts.append(sdd_fact(repo, problems))
    facts.append(test_model_fact(repo, problems))
    facts.extend(change_facts(repo, change_id, problems))
    facts.append(reviews_fact(repo, change_id, problems))
    # Решение Заказчика 3.1-А (S5, 2026-10-02): факт approved-кейсов ИМЕННО
    # этого change — по нему qa_automation допускает автоматизацию (ТЗ 04).
    facts.append(approved_cases_fact(repo, change_id, problems))
    # P0.1 (факты enforcement): archive (решение Б), релизное решение
    # Заказчика (решение В1) и машиночитаемый факт branch protection
    # (решение А). Все три — read-only факты репозитория.
    facts.append(archived_fact(repo, change_id, problems))
    facts.append(release_approval_fact(repo, change_id, problems))
    facts.append(github_protection_fact(repo, problems))

    # task: независимые состояния внутри change (не один линейный статус)
    if task_id:
        facts.append(task_fact(repo, change_id, task_id, problems))

    reg_path = Path(registry) if registry else (
        Path.home() / ".hermes" / "state" / "active_sessions.json"
    )
    reg_fact, registry_digest = registry_fact(reg_path, problems)
    facts.append(reg_fact)

    facts_dicts = [f.to_dict() for f in facts]
    # observed_at — временное поле: исключается из digest.
    digest_payload = [
        {k: v for k, v in d.items() if k not in DIGEST_EXCLUDED}
        for d in facts_dicts
    ]
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "scope": {
            "repo": str(repo),
            "project": project,
            "flow": flow,
            "change": change_id,
            "task": task_id,
        },
        "repo_head": head_sha,
        "dirty_paths": dirty_paths,
        "registry_digest": registry_digest,
        "facts": facts_dicts,
        "problems": [
            {"code": p.code, "detail": p.detail, "source": p.source} for p in problems
        ],
    }
    snapshot["snapshot_digest"] = _sha256_text(
        json.dumps(digest_payload, sort_keys=True, ensure_ascii=False)
    )
    return snapshot


# ---------------------------------------------------------------- CLI


def human_output(snapshot: dict) -> str:
    lines = []
    scope = snapshot["scope"]
    lines.append(
        f"flow_state: {scope['project']} flow={scope['flow']} "
        f"change={scope['change']}"
        + (f" task={scope['task']}" if scope["task"] else "")
    )
    lines.append(f"repo_head: {snapshot['repo_head'] or '<unknown>'}")
    dirty = snapshot["dirty_paths"]
    lines.append(f"dirty_paths: {len(dirty)}")
    for p in dirty[:10]:
        lines.append(f"  {p}")
    if len(dirty) > 10:
        lines.append(f"  ... и еще {len(dirty) - 10}")
    lines.append(f"registry_digest: {snapshot['registry_digest'] or '<none>'}")
    lines.append("факты:")
    for f in snapshot["facts"]:
        mark = {
            "ready": "[ready]",
            "missing": "[miss ]",
            "invalid": "[inval]",
            "unknown": "[unkn ]",
        }.get(f.get("status", "ready"), "[  ?  ]")
        value = f["value"]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True, ensure_ascii=False)
        lines.append(f"  {mark} {f['key']}: {value} (source: {f['source']})")
    if snapshot["problems"]:
        lines.append("проблемы:")
        for p in snapshot["problems"]:
            lines.append(f"  {p['code']}: {p['detail']}")
    else:
        lines.append("проблемы: нет")
    lines.append(f"snapshot_digest: {snapshot['snapshot_digest']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="flow_state.py",
        description="read-only снимок состояния конвейера (поставка 02)",
    )
    sub = ap.add_subparsers(dest="command", required=True)
    pi = sub.add_parser("inspect", help="построить FlowSnapshot")
    pi.add_argument("--repo", required=True, help="путь к репозиторию/worktree")
    pi.add_argument("--project", required=True, help="ID проекта")
    pi.add_argument("--flow", required=True, type=int, choices=FLOW_IDS)
    pi.add_argument("--change", required=True, help="change-id / BUG-NNN / chore")
    pi.add_argument("--task", default=None, help="опциональный task ID")
    pi.add_argument("--json", action="store_true", dest="as_json", help="машинный JSON")
    pi.add_argument("--registry", default=None, help="путь к active_sessions.json")
    args = ap.parse_args(argv)

    if args.command == "inspect":
        try:
            snapshot = inspect(
                repo_arg=args.repo,
                project=args.project,
                flow=args.flow,
                change_id=args.change,
                task_id=args.task,
                registry=args.registry,
            )
        except ValueError as exc:
            print(f"FLOW-STATE-ERROR: {exc}")
            return 2

        blocking_codes = {
            "GIT_HEAD_ERROR", "GIT_STATUS_ERROR", "FILE_READ_ERROR",
            "REGISTRY_READ_ERROR", "PATH_OUTSIDE_REPO",
        }
        hard = [p for p in snapshot["problems"] if p["code"] in blocking_codes]
        if args.as_json:
            print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(human_output(snapshot))
        if hard:
            print(
                f"FLOW-STATE: {len(hard)} проблем(а) чтения источников — "
                "снимок содержит unknown-факты, код 1",
                file=sys.stderr,
            )
            return 1
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
