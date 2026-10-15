#!/usr/bin/env python3
"""gate_runner.py — единый запуск существующих gates со структурированным
GateReport и append-only audit JSONL (поставка 05, ТЗ
docs/chatgpt-deterministic-flow/05-evidence-and-gates.md).

Скрипты-ворота НЕ заменяются и НЕ меняются (00-README, огр. 2): адаптеры
запускают `flow_check.py`, `pm_bounds_check.py`, `pr_validate.py` и
`openspec validate --all --strict` как subprocess с явными repo/cwd и
timeout. Путь к openspec-CLI — параметр (в этой среде CLI нет — тесты
используют fake-executable с управляемым exit code).

GateReport (ТЗ 05; контракт §11): gate_id, scope, command, adapter_version,
input_head, input_digest, started_at, duration, exit_code,
status PASS|FAIL|ERROR|SKIPPED, executed, log_path, краткая диагностика.
  - exit 0 → PASS; exit 1 → FAIL; любой другой exit, timeout, отсутствие
    исполняемого файла, ошибка записи лога → ERROR (блокирует);
  - конфигурационная ошибка адаптера (pm_bounds_check без явного режима или
    без обязательного аргумента режима) → ERROR, НЕ SKIPPED: «отсутствующий
    gate блокирует переход» (ТЗ 05; review-005 B1);
  - SKIPPED — только по явному правилу неприменимости (pr_validate без
    PR-контекста); SKIPPED не считается пройденным: любой SKIPPED делает
    overall=SKIPPED (не PASS) и exit 1 (review-005 B1/M2), в отчете
    перечислен в skipped_gates с пометкой «не выполнен»;
  - input_head/input_digest фиксируют вход; повтор после изменения входного
    SHA обязателен: `status` помечает отчет STALE, если HEAD репо изменился
    (отчет без repo_head при валидном HEAD — тоже STALE, review-005 m1).

pm_bounds_check запускается в ЯВНОМ режиме (параметр --pm-mode): commits /
sessions / product-commits. `--all-projects` намеренно не предоставляется как
доказательство проверки выбранного repo (ТЗ 05).

Provenance review (ТЗ 05): подкоманда record-review пишет рядом с
человекочитаемым review-файлом machine-readable sidecar JSON
(review-provenance/1). Строгая проверка sidecar — в flow_transition.py
(accept_review/merge_task); сам review-.md не переписывается.

Audit JSONL: append-only события request/decision/reservation/agent_started/
agent_finished/gate_report/accepted/returned/recovery с correlation_id,
scope и digest. Секреты и полный stdout в JSONL не пишутся (запрещенные
ключи фильтруются, события отклоняются); логи gate — отдельные файлы с
ограничением размера. JSONL помогает разбору, но не является единственным
доказательством (контракт §13).

Usage:
    python3 scripts/gate_runner.py run --repo PATH --scope NAME
        [--gates a,b,c] [--openspec-cmd CMD] [--timeout SEC]
        [--pm-mode MODE] [--pm-commits H1,H2] [--pm-range A..B]
        [--pm-registry PATH] [--pm-require-review]
        [--pr-id ID] [--log-dir PATH] [--report-dir PATH] [--audit PATH]
        [--correlation-id ID] [--json]
    python3 scripts/gate_runner.py status --report-dir PATH [--repo PATH] [--json]
    python3 scripts/gate_runner.py record-review --review-path PATH
        --project ID --change ID --tasks T1,T2 --author-delegation ID
        --reviewer-delegation ID --commit SHA --verdict approve
        --diff-digest D [--diff-base B] [--repo PATH] [--sidecar PATH] [--json]
    python3 scripts/gate_runner.py diff-digest --repo PATH --base B --head H [--json]
    python3 scripts/gate_runner.py github-protection --repo PATH
        [--github-repo OWNER/NAME] [--github-token-env ENV_NAME] [--branch main]
        [--report PATH] [--json]
    python3 scripts/gate_runner.py record-decision --repo PATH
        --decision-id YYYY-MM-DD-SLUG (--project ID | --change ID | --phase N
        | --scope-json JSON) --action ACTION --commit SHA|--auto --source SRC
        [--quote TEXT] [--expiration ISO] [--force] [--json]

Exit codes (run/status): 0 — PASS без STALE; 1 — FAIL, SKIPPED-отчет
(«не все gates выполнены») или STALE (повтор обязателен); 2 — ERROR,
нет отчета или ошибка входа.
Exit codes (github-protection): 0 — защита подтверждена (факт protection_ok;
ruleset enforcement=active + rules type=pull_request + без bypass always);
1 — защита не настроена/не соответствует (факт protection_ok=False, HTTP
404 и др.); 3 — SKIPPED по явному правилу («--github-repo/--github-token-env
не заданы» или env-переменная отсутствует — проверка не настроена, НЕ ERROR);
2 — ошибка входа (репо не найден).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import flow_mode  # noqa: E402  (enforcing: overall!=PASS → exit 1)

GATE_REPORT_SCHEMA = "gate-report/1"
AUDIT_SCHEMA = "audit-jsonl/1"
PROVENANCE_SCHEMA = "review-provenance/1"
# P0.4 (пересмотр плана Заказчика): журнал решений Заказчика —
# decisions/<YYYY-MM-DD>-<slug>.md с машиночитаемым блоком decision-record/1.
# record-decision записывает запись; НЕ является защищенной подписью и НЕ
# независимым одобрением личности: «решение зафиксировано», не «личность
# подтверждена» (контракт §10).
DECISION_RECORD_SCHEMA = "decision-record/1"
DECISIONS_DIR = "decisions"
ADAPTER_VERSION = "gate-runner/1"

# Решение А (P0.1): машиночитаемый факт branch protection (честная граница
# enforcement, контракт §12). Адаптер вызывает GitHub API только через
# subprocess (curl или python urllib); токен читается subprocess'ом из env по
# ИМЕНИ переменной — сам токен в argv/env/отчет не попадает.
PROTECTION_SCHEMA = "github-protection/1"
PROTECTION_BRANCH = "main"
PROTECTION_API_TIMEOUT = 30  # секунд на HTTP-запрос

SCRIPTS = Path(__file__).resolve().parent

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_ERROR = "ERROR"
STATUS_SKIPPED = "SKIPPED"
REPORT_STATUSES = (STATUS_PASS, STATUS_FAIL, STATUS_ERROR, STATUS_SKIPPED)

SCOPES = ("preflight", "post_agent", "pre_accept", "pre_merge", "ci")
KNOWN_GATES = ("openspec_validate", "flow_check", "pm_bounds_check", "pr_validate")
PM_MODES = ("commits", "sessions", "product-commits")

# Точка запуска → список обязательных проверок (ТЗ 05; контракт §11).
DEFAULT_GATES: dict[str, tuple[str, ...]] = {
    "preflight": ("openspec_validate",),
    "post_agent": ("flow_check",),
    "pre_accept": ("flow_check", "pm_bounds_check"),
    "pre_merge": ("flow_check", "pm_bounds_check", "pr_validate"),
    "ci": ("openspec_validate", "flow_check", "pm_bounds_check", "pr_validate"),
}

AUDIT_EVENT_TYPES = (
    "request", "decision", "reservation", "agent_started", "agent_finished",
    "gate_report", "accepted", "returned", "recovery",
)

# Секреты и полный stdout/stderr агента в JSONL не копируются (ТЗ 05;
# контракт §13): события с такими ключами отклоняются, а не фильтруются молча.
AUDIT_FORBIDDEN_KEYS = frozenset({
    "password", "token", "secret", "api_key", "apikey", "cvc", "stdout",
    "stderr", "prompt", "authorization", "cookie",
})

MAX_LOG_BYTES = 131072          # ограничение размера gate-лога (голова+хвост)
DEFAULT_TIMEOUT = 120           # секунд на gate


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_digest(obj) -> str:
    return sha256_text(json.dumps(obj, sort_keys=True, ensure_ascii=False))


def new_correlation_id() -> str:
    return uuid.uuid4().hex


# ------------------------------------------------------------------ audit


def _assert_no_forbidden(obj, path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k).lower()
            if key in AUDIT_FORBIDDEN_KEYS:
                raise ValueError(
                    f"audit-событие содержит запрещенный ключ «{path}{k}» — "
                    f"секреты и полный stdout в JSONL не пишутся (ТЗ 05)")
            _assert_no_forbidden(v, f"{path}{k}.")


def append_audit_event(audit_path: str | Path, event: str,
                       correlation_id: str, scope: dict | None = None,
                       **fields) -> dict:
    """Append-only JSONL-событие. Файл открывается в append-режиме, существующие
    строки не переписываются. Лог — не единственное доказательство: событие
    несет ссылки (пути, digest), а не содержимое stdout."""
    if event not in AUDIT_EVENT_TYPES:
        raise ValueError(
            f"неизвестный тип audit-события: {event!r} "
            f"(ожидается один из {', '.join(AUDIT_EVENT_TYPES)})")
    record = {
        "schema_version": AUDIT_SCHEMA,
        "event": event,
        "timestamp": utcnow_iso(),
        "correlation_id": correlation_id,
        "scope": scope or {},
    }
    record.update(fields)
    _assert_no_forbidden(record)
    p = Path(audit_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


# ------------------------------------------------------------------ git


def git_out(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )
    return (proc.stdout or "").strip()


def git_head(repo: Path) -> str | None:
    head = git_out(repo, "rev-parse", "HEAD")
    return head or None


def compute_diff_digest(repo: Path, base: str, head: str) -> str:
    """sha256 диффа base..head — digest утвержденной версии работы."""
    return sha256_text(git_out(repo, "diff", f"{base}..{head}"))


# ------------------------------------------------------------------ GateReport


@dataclass
class GateReport:
    gate_id: str
    scope: str
    command: list
    adapter_version: str
    input_head: str | None
    input_digest: str
    started_at: str
    duration: float
    exit_code: int | None
    status: str
    executed: bool
    log_path: str | None
    diagnostics: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _script_digest(path: Path) -> str:
    try:
        return sha256_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return "unavailable"


def compute_input_digest(repo: Path, gate_id: str, cmd: list,
                         extra: dict | None = None) -> str:
    """Digest входа gate: HEAD репо + версия самих скриптов-ворот + параметры.
    Изменение любого из них инвалидирует прежний PASS (digest-check, ТЗ 05)."""
    payload: dict = {
        "gate_id": gate_id,
        "command": [str(c) for c in cmd],
        "repo_head": git_head(repo),
    }
    if gate_id == "flow_check":
        payload["gate_script_sha"] = _script_digest(SCRIPTS / "flow_check.py")
    elif gate_id == "pm_bounds_check":
        payload["gate_script_sha"] = _script_digest(SCRIPTS / "pm_bounds_check.py")
    elif gate_id == "pr_validate":
        payload["gate_script_sha"] = _script_digest(SCRIPTS / "pr_validate.py")
    if extra:
        payload["extra"] = extra
    return canonical_digest(payload)


def _write_log(log_dir: Path, gate_id: str, started: str,
               stdout: str, stderr: str) -> Path:
    """Gate-лог отдельным файлом с ограничением размера (голова + хвост)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = started.replace(":", "").replace("+", "")
    path = log_dir / f"{gate_id}-{stamp}.log"
    head, tail = stdout or "", stderr or ""
    budget = MAX_LOG_BYTES // 2
    if len(head) > budget:
        head = head[:budget] + f"\n[...усечено, всего {len(stdout)} байт...]\n"
    if len(tail) > budget:
        tail = f"[...усечено, всего {len(stderr)} байт...]\n" + tail[-budget:]
    path.write_text(
        f"# gate: {gate_id}\n# started_at: {started}\n\n--- stdout ---\n{head}\n"
        f"--- stderr ---\n{tail}\n",
        encoding="utf-8",
    )
    return path


def run_gate(gate_id: str, cmd: list, repo: Path, scope_name: str,
             timeout: int, log_dir: Path, extra: dict | None = None,
             skip_reason: str | None = None,
             applicable_by_rule: bool = False) -> GateReport:
    """Запуск одного gate как subprocess (явный cwd=repo, timeout).

    skip_reason задается ТОЛЬКО по явному правилу неприменимости
    (applicable_by_rule=True — например, pr_validate без PR-контекста).
    Конфигурационная ошибка адаптера (applicable_by_rule=False) — это
    «отсутствующий gate», а он блокирует переход: статус ERROR, не SKIPPED
    (ТЗ 05; review-005 B1)."""
    started = utcnow_iso()
    input_head = git_head(repo)
    input_digest = compute_input_digest(repo, gate_id, cmd, extra)
    base: dict = dict(
        gate_id=gate_id, scope=scope_name, command=[str(c) for c in cmd],
        adapter_version=ADAPTER_VERSION, input_head=input_head,
        input_digest=input_digest, started_at=started, duration=0.0,
        exit_code=None, status=STATUS_ERROR, executed=False, log_path=None,
        diagnostics="",
    )
    if skip_reason is not None:
        if applicable_by_rule:
            base["status"] = STATUS_SKIPPED
            base["executed"] = False
            base["diagnostics"] = (
                f"{skip_reason} — gate НЕ ВЫПОЛНЕН (SKIPPED, явное правило "
                f"неприменимости; не считается пройденным, ТЗ 05)")
        else:
            base["status"] = STATUS_ERROR
            base["executed"] = False
            base["diagnostics"] = (
                f"{skip_reason} — конфигурационная ошибка запускающего: "
                f"обязательный gate не выполнен и БЛОКИРУЕТ переход "
                f"(«отсутствующий gate блокирует», ТЗ 05; review-005 B1)")
        return GateReport(**base)

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [str(c) for c in cmd], cwd=str(repo), capture_output=True,
            text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        base["duration"] = round(time.monotonic() - t0, 3)
        base["diagnostics"] = (
            f"timeout: gate не завершился за {timeout}s — ERROR и блок "
            f"(ТЗ 05: зависший gate блокирует переход)")
        return GateReport(**base)
    except FileNotFoundError:
        base["duration"] = round(time.monotonic() - t0, 3)
        base["diagnostics"] = (
            f"исполняемый файл не найден: {cmd[0]} — ERROR и блок (ТЗ 05)")
        return GateReport(**base)
    except OSError as exc:
        base["duration"] = round(time.monotonic() - t0, 3)
        base["diagnostics"] = f"ошибка запуска gate: {exc} — ERROR и блок"
        return GateReport(**base)

    base["duration"] = round(time.monotonic() - t0, 3)
    base["exit_code"] = proc.returncode
    log_path = _write_log(log_dir, gate_id, started, proc.stdout, proc.stderr)
    base["log_path"] = str(log_path)
    base["executed"] = True
    if proc.returncode == 0:
        base["status"] = STATUS_PASS
        base["diagnostics"] = (proc.stdout or "").strip().splitlines()[-1] \
            if (proc.stdout or "").strip() else "exit 0"
    elif proc.returncode == 1:
        base["status"] = STATUS_FAIL
        out = (proc.stdout or "").strip().splitlines()
        base["diagnostics"] = out[0] if out else "gate сообщил FAIL (exit 1)"
    else:
        base["status"] = STATUS_ERROR
        out = (proc.stdout or "").strip().splitlines()
        base["diagnostics"] = (
            f"неожиданный exit code {proc.returncode} (ожидается 0/1) — "
            f"ошибка запуска/parse error, ERROR и блок: "
            f"{out[0] if out else '(stdout пуст)'}")
    return GateReport(**base)


# ------------------------------------------------------------------ адаптеры


def adapter_openspec_validate(repo: Path, openspec_cmd: str
                              ) -> tuple[list, dict, str | None, bool]:
    """`openspec validate --all --strict` в репозитории проекта."""
    cmd = openspec_cmd.split() + ["validate", "--all", "--strict"]
    return cmd, {"openspec_cmd": openspec_cmd}, None, False


def adapter_flow_check(repo: Path) -> tuple[list, dict, str | None, bool]:
    return ([sys.executable, str(SCRIPTS / "flow_check.py"), str(repo)],
            {}, None, False)


def adapter_pm_bounds(repo: Path, mode: str | None, commits: str | None,
                      rev_range: str | None, registry: str | None,
                      require_review: bool
                      ) -> tuple[list, dict, str | None, bool]:
    """pm_bounds_check в ЯВНОМ режиме (ТЗ 05: не --all-projects).

    Возвращаемый 4-й элемент — applicable_by_rule: у pm_bounds_check явного
    правила неприменимости НЕТ; незаданный режим или неполные аргументы
    режима — конфигурационная ошибка запускающего → ERROR (review-005 B1)."""
    cmd = [sys.executable, str(SCRIPTS / "pm_bounds_check.py")]
    extra: dict = {"pm_mode": mode}
    if mode == "commits":
        if not commits:
            return cmd, extra, ("--pm-mode=commits требует --pm-commits — "
                                "конфигурация gate неполна"), False
        cmd += ["--commits", commits, "--repo", str(repo)]
        extra["commits"] = commits
    elif mode == "sessions":
        if not registry:
            return cmd, extra, ("--pm-mode=sessions требует --pm-registry — "
                                "конфигурация gate неполна"), False
        cmd += ["--sessions", registry]
        extra["registry"] = str(registry)
    elif mode == "product-commits":
        if not rev_range:
            return cmd, extra, ("--pm-mode=product-commits требует --pm-range — "
                                "конфигурация gate неполна"), False
        cmd += ["--product-commits", rev_range, "--repo", str(repo)]
        extra["rev_range"] = rev_range
    else:
        return cmd, extra, (
            f"режим pm_bounds_check не задан (pm_mode={mode!r}) — запуск без "
            f"явного режима запрещен (ТЗ 05)"), False
    if require_review:
        cmd.append("--require-review")
        extra["require_review"] = True
    return cmd, extra, None, False


def adapter_pr_validate(repo: Path, pr_id: str | None
                        ) -> tuple[list, dict, str | None, bool]:
    """pr_validate — только при PR-контексте; иначе SKIPPED по явному правилу
    неприменимости («не запускается без PR-контекста и не считается
    пройденным», ТЗ 05)."""
    event_path = Path(os.environ["GITHUB_EVENT_PATH"]) \
        if os.environ.get("GITHUB_EVENT_PATH") else None
    if not pr_id and not (event_path and event_path.is_file()):
        return [], {}, (
            "нет PR-контекста (--pr-id / GITHUB_EVENT_PATH) — pr_validate "
            "не запускается и не считается пройденным (ТЗ 05)"), True
    cmd = [sys.executable, str(SCRIPTS / "pr_validate.py"), str(repo)]
    if pr_id:
        cmd += ["--id", pr_id]
    return cmd, {"pr_id": pr_id}, None, False


def build_gate_command(gate_id: str, repo: Path, opts: argparse.Namespace
                       ) -> tuple[list, dict, str | None, bool]:
    """(cmd, extra-входы, skip_reason|None, applicable_by_rule) для gate."""
    if gate_id == "openspec_validate":
        return adapter_openspec_validate(repo, opts.openspec_cmd)
    if gate_id == "flow_check":
        return adapter_flow_check(repo)
    if gate_id == "pm_bounds_check":
        return adapter_pm_bounds(repo, opts.pm_mode, opts.pm_commits,
                                 opts.pm_range, opts.pm_registry,
                                 opts.pm_require_review)
    if gate_id == "pr_validate":
        return adapter_pr_validate(repo, opts.pr_id)
    raise ValueError(f"неизвестный gate: {gate_id!r}")


# ------------------------------------------------------------------ run/status


def run_gates(repo: Path, scope_name: str, gates: list[str],
              opts: argparse.Namespace) -> tuple[dict, int]:
    """run_gates(scope, phase) -> GateReport (контракт §4, §11)."""
    head = git_head(repo)
    correlation_id = opts.correlation_id or new_correlation_id()
    log_dir = Path(opts.log_dir) if opts.log_dir else repo / ".gate-logs"
    started = utcnow_iso()
    if opts.audit:
        try:
            append_audit_event(
                opts.audit, "request", correlation_id,
                scope={"repo": str(repo), "phase": scope_name},
                gates=list(gates), repo_head=head,
            )
        except ValueError as exc:
            print(f"GATE-RUNNER-ERROR: {exc}", file=sys.stderr)
            return {}, 2

    reports: list[GateReport] = []
    for gate_id in gates:
        cmd, extra, skip, applicable_by_rule = build_gate_command(
            gate_id, repo, opts)
        report = run_gate(gate_id, cmd, repo, scope_name,
                          opts.timeout, log_dir, extra=extra,
                          skip_reason=skip,
                          applicable_by_rule=applicable_by_rule)
        reports.append(report)
        if opts.audit:
            # В audit — только факты и ссылки; stdout gate остается в log_path.
            append_audit_event(
                opts.audit, "gate_report", correlation_id,
                scope={"repo": str(repo), "phase": scope_name, "gate": gate_id},
                status=report.status, exit_code=report.exit_code,
                input_head=report.input_head,
                input_digest=report.input_digest,
                log_path=report.log_path, duration=report.duration,
            )

    statuses = [r.status for r in reports]
    skipped = [r.gate_id for r in reports if r.status == STATUS_SKIPPED]
    if STATUS_ERROR in statuses:
        overall = STATUS_ERROR
    elif STATUS_FAIL in statuses:
        overall = STATUS_FAIL
    elif skipped:
        # SKIPPED не считается пройденным (ТЗ 05; review-005 B1/M2): отчет
        # без выполненных обязательных gates не зеленый — overall=SKIPPED,
        # exit 1 (повтор с корректным контекстом обязателен).
        overall = STATUS_SKIPPED
    else:
        overall = STATUS_PASS
    report = {
        "schema_version": GATE_REPORT_SCHEMA,
        "adapter_version": ADAPTER_VERSION,
        "correlation_id": correlation_id,
        "scope": scope_name,
        "repo": str(repo),
        "repo_head": head,
        "started_at": started,
        "gates": [r.to_dict() for r in reports],
        "skipped_gates": [
            {"gate_id": g.gate_id, "reason": g.diagnostics}
            for g in reports if g.status == STATUS_SKIPPED
        ],
        "overall": overall,
    }
    report_dir = Path(opts.report_dir) if opts.report_dir else log_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"gate-report-{started.replace(':', '')}-{correlation_id[:8]}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                               sort_keys=True), encoding="utf-8")
    report["report_ref"] = str(path)
    (report_dir / "latest.json").write_text(
        json.dumps({"report_ref": str(path)}, ensure_ascii=False),
        encoding="utf-8")
    return report, exit_code_for(report, repo)


def exit_code_for(report: dict, repo: Path | None = None) -> int:
    overall = report.get("overall")
    if overall == STATUS_ERROR:
        return 2
    if overall == STATUS_FAIL:
        return 1
    if overall == STATUS_SKIPPED:
        # SKIPPED не считается пройденным (ТЗ 05; review-005 B1/M2):
        # повтор запуска с корректным контекстом обязателен. До включения
        # enforcement (review-007 m11) SKIPPED-отчет давал «зеленый» exit 0;
        # решение Заказчика 2026-10-02: в enforcing любой не-PASS exit 1.
        if flow_mode.get_mode() == "enforcing":
            return 1
        return 0
    if repo is not None and is_stale(report, repo):
        return 1
    return 0


def is_stale(report: dict, repo: Path) -> bool:
    """Digest-check: PASS при измененном входном SHA невалиден — повтор
    проверки обязателен (ТЗ 05). Отчет без repo_head при валидном HEAD —
    поврежденный отчет, тоже STALE (review-005 m1)."""
    head = git_head(repo)
    report_head = report.get("repo_head")
    if report_head is None:
        return bool(head)  # валидный HEAD есть, факт отчета — нет → STALE
    return bool(head) and report_head != head


def load_latest_report(report_dir: Path) -> tuple[dict | None, str | None]:
    latest = report_dir / "latest.json"
    if not latest.is_file():
        return None, f"нет отчета в {report_dir} (latest.json отсутствует)"
    try:
        ref = json.loads(latest.read_text(encoding="utf-8"))["report_ref"]
        return json.loads(Path(ref).read_text(encoding="utf-8")), None
    except (OSError, ValueError) as exc:
        return None, f"отчет нечитаем: {exc}"


def _report_human(report: dict, repo: Path | None) -> str:
    lines = [
        f"gate_runner: {report['overall']} scope={report['scope']} "
        f"correlation_id={report['correlation_id']}"
    ]
    stale = repo is not None and is_stale(report, repo)
    if stale:
        lines.append("STALE: HEAD репо изменился после отчета — повторная "
                     "проверка обязательна (digest-check, ТЗ 05)")
    for g in report["gates"]:
        lines.append(
            f"  [{g['status']}] {g['gate_id']} "
            f"executed={g.get('executed', g['status'] != STATUS_SKIPPED)} "
            f"exit={g['exit_code']} "
            f"digest={g['input_digest'][:12]} dur={g['duration']}s "
            f"— {g['diagnostics']}")
        if g.get("log_path"):
            lines.append(f"      log: {g['log_path']}")
    skipped = report.get("skipped_gates") or []
    if skipped:
        ids = ", ".join(s["gate_id"] for s in skipped)
        lines.append(
            f"НЕ ВЫПОЛНЕНЫ (SKIPPED, не считаются пройденными — ТЗ 05): {ids}")
    return "\n".join(lines)


# ------------------------------------------------------- provenance sidecar


def write_review_provenance(
    review_path: Path, project: str, change: str, task_ids: list[str],
    author_delegation: str, reviewer_delegation: str, commit_sha: str,
    verdict: str, diff_digest: str | None = None, diff_base: str | None = None,
    sidecar_path: Path | None = None,
) -> tuple[Path, dict]:
    """Sidecar JSON рядом с человекочитаемым review-файлом (ТЗ 05).
    Сам .md не переписывается. Обязательные поля валидируются; верификация
    соответствия task/change/SHA и независимости роли — в flow_transition."""
    review_path = review_path.resolve()
    if not review_path.is_file():
        raise ValueError(f"review-файл не найден: {review_path}")
    if not project or not change:
        raise ValueError("project и change обязательны")
    if not task_ids:
        raise ValueError("task_ids обязателен (хотя бы одна задача)")
    if not author_delegation or not reviewer_delegation:
        raise ValueError("author_delegation и reviewer_delegation обязательны")
    if not commit_sha:
        raise ValueError("reviewed_commit_sha обязателен")
    if verdict not in ("approve", "return"):
        raise ValueError(f"verdict должен быть approve|return, получен {verdict!r}")
    payload = {
        "schema_version": PROVENANCE_SCHEMA,
        "project": project,
        "change": change,
        "task_ids": task_ids,
        "author_delegation": author_delegation,
        "reviewer_delegation": reviewer_delegation,
        "reviewed_commit_sha": commit_sha,
        "diff_digest": diff_digest,
        "diff_base": diff_base,
        "verdict": verdict,
        "timestamp": utcnow_iso(),
        "review_path": str(review_path),
    }
    path = sidecar_path or Path(str(review_path) + ".provenance.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                               sort_keys=True), encoding="utf-8")
    return path, payload


# ------------------------------------------------------------ github_protection


def protection_report_path(repo: Path) -> Path:
    return Path(repo) / ".flow-evidence" / "github-protection.json"


def check_branch_protection(repo: Path, github_repo: str, token_env: str,
                            branch: str = PROTECTION_BRANCH,
                            report: Path | None = None,
                            token_env_os=None) -> dict:
    """Решение А (P0.1, endpoint по пересмотру плана): проверка branch
    protection через GitHub Rulesets API.

    GET /repos/{repo}/rules/branches/{branch} с токеном, прочитанным
    subprocess'ом из env-переменной {token_env} (имя, НЕ значение).
    Ожидается массив активных rulesets; защита подтверждена, когда среди них
    есть ruleset с enforcement=active, rules содержит type=pull_request, а
    bypass для роли не разрешает обход (bypass отсутствует ИЛИ ни у одного
    bypass-элемента нет bypass_mode=always). Дополнительно: при наличии
    required_status_checks в ruleset он должен включать flow.yml.

    Итог — машиночитаемый JSON-факт (schema github-protection/1) с датой,
    HTTP-кодом и машиночитаемым разбором rulesets (ruleset_report).

    Возвращает dict факта; поле protection_ok=True только при выполнении
    всех условий. Отсутствие токена/env/repo — SKIPPED-ситуация вызывающего
    (gate по явному правилу не задан), поэтому поднят ValueError с маркером
    "skip:" — CLI маппит ее в SKIPPED-отчет, не ERROR.
    token_env_os — подмена доступа к env для тестов: словарь или callable
    (по умолчанию os.environ.get).
    """
    import os as _os
    if token_env_os is None:
        getenv = _os.environ.get
    elif callable(token_env_os):
        getenv = token_env_os
    else:
        getenv = token_env_os.get
    fact: dict = {
        "schema_version": PROTECTION_SCHEMA,
        "adapter_version": ADAPTER_VERSION,
        "endpoint": "rules/branches",
        "repo": github_repo,
        "branch": branch,
        "observed_at": utcnow_iso(),
        "protection_ok": False,
        "http_status": None,
        "detail": "",
        "ruleset_report": [],
    }
    if not github_repo:
        raise ValueError("skip: --github-repo не задан — gate не настроен")
    if not token_env:
        raise ValueError("skip: --github-token-env не задан — gate не настроен")
    token = str(getenv(token_env) or "")
    if not token:
        raise ValueError(
            f"skip: env-переменная {token_env} пуста/отсутствует — gate не настроен")

    api_url = f"https://api.github.com/repos/{github_repo}/rules/branches/{branch}"
    code, body = _github_api_get(api_url, token)
    fact["http_status"] = code
    if code == 404:
        fact["detail"] = "активных rulesets для ветки нет (404) — защита не настроена"
        _write_protection_report(report or protection_report_path(repo), fact)
        return fact
    if code != 200:
        fact["detail"] = f"github api недоступен (HTTP {code})"
        _write_protection_report(report or protection_report_path(repo), fact)
        return fact
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if not isinstance(data, list):
        fact["detail"] = "github api вернул не массив rulesets"
        _write_protection_report(report or protection_report_path(repo), fact)
        return fact

    problems: list[str] = []
    ruleset_report: list[dict] = []
    satisfied = False
    for rs in data:
        if not isinstance(rs, dict):
            continue
        entry = {
            "id": rs.get("id"),
            "name": rs.get("name"),
            "enforcement": rs.get("enforcement"),
            "pull_request": False,
            "bypass_always": False,
            "status_checks_flow_yml": None,
        }
        rules = rs.get("rules") if isinstance(rs.get("rules"), list) else []
        for r in rules:
            if not isinstance(r, dict):
                continue
            if r.get("type") == "pull_request":
                entry["pull_request"] = True
            if r.get("type") == "required_status_checks":
                params = r.get("parameters")
                if not isinstance(params, dict):
                    params = {}
                ctxs = params.get("required_status_checks")
                if not isinstance(ctxs, list):
                    ctxs = []
                names = [
                    (c.get("context") if isinstance(c, dict) else str(c))
                    for c in ctxs
                ]
                entry["status_checks_flow_yml"] = any(
                    "flow.yml" in str(n) for n in names if n)
        bypass = rs.get("bypass") if isinstance(rs.get("bypass"), list) else []
        entry["bypass_always"] = any(
            isinstance(b, dict) and b.get("bypass_mode") == "always"
            for b in bypass)
        ruleset_report.append(entry)
        if rs.get("enforcement") != "active":
            continue
        if not entry["pull_request"]:
            continue
        if entry["bypass_always"]:
            continue
        if entry["status_checks_flow_yml"] is False:
            continue
        satisfied = True
    fact["ruleset_report"] = ruleset_report
    if not ruleset_report:
        problems.append("активных rulesets с rules не найдено")
    if not satisfied:
        if not any(e.get("enforcement") == "active" for e in ruleset_report):
            problems.append("нет ruleset с enforcement=active")
        elif not any(e.get("pull_request") for e in ruleset_report
                     if e.get("enforcement") == "active"):
            problems.append("нет rules c type=pull_request среди активных")
        else:
            problems.append(
                "активный ruleset с pull_request обходится ролью "
                "(bypass always) или не содержит требуемых status checks")
    if problems:
        fact["detail"] = "; ".join(problems)
    else:
        fact["protection_ok"] = True
        fact["detail"] = (
            "ruleset enforcement=active с rules type=pull_request, "
            "bypass always не найден")
    _write_protection_report(report or protection_report_path(repo), fact)
    return fact


def _github_api_get(url: str, token: str) -> tuple[int, str]:
    """GitHub API GET через subprocess (urllib в дочернем процессе).

    Токен передается дочернему процессу через его env (не argv, не файлы,
    не stdout родителя). Возвращает (HTTP-код, тело).
    """
    child = (
        "import os, sys, urllib.request, urllib.error\n"
        "url, name = sys.argv[1], sys.argv[2]\n"
        "token = os.environ[name]\n"
        "req = urllib.request.Request(url, headers={\n"
        "    'Authorization': 'Bearer ' + token,\n"
        "    'Accept': 'application/vnd.github+json',\n"
        "    'X-GitHub-Api-Version': '2022-11-28',\n"
        "    'User-Agent': 'ai-factory-gate-runner',\n"
        "})\n"
        "try:\n"
        "    with urllib.request.urlopen(req, timeout=30) as resp:\n"
        "        print(resp.status); print(resp.read().decode('utf-8', errors='replace'))\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(e.code); print(e.read().decode('utf-8', errors='replace'))\n"
        "except Exception as e:\n"
        "    print(0); print(str(e))\n"
    )
    env = dict(os.environ)
    env["_FLOW_PROTECTION_TOKEN"] = token
    try:
        proc = subprocess.run(
            [sys.executable, "-c", child, url, "_FLOW_PROTECTION_TOKEN"],
            capture_output=True, text=True, timeout=PROTECTION_API_TIMEOUT + 10,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return 0, "timeout"
    lines = (proc.stdout or "").split("\n", 1)
    try:
        code = int(lines[0].strip())
    except (ValueError, IndexError):
        return 0, "unparseable response"
    return code, lines[1] if len(lines) > 1 else ""


def _write_protection_report(path: Path, fact: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(fact, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")


def load_protection_fact(repo: Path) -> tuple[dict | None, str | None]:
    """Читает последний JSON-факт github_protection (для проверок/тестов)."""
    path = protection_report_path(repo)
    if not path.is_file():
        return None, f"факт не найден: {path}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as exc:
        return None, f"факт нечитаем: {exc}"


# ------------------------------------------------------------------- CLI


def _cmd_run(args) -> int:
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"GATE-RUNNER-ERROR: репозиторий не найден: {repo}", file=sys.stderr)
        return 2
    gates = ([g.strip() for g in args.gates.split(",") if g.strip()]
             if args.gates else list(DEFAULT_GATES[args.scope]))
    unknown = [g for g in gates if g not in KNOWN_GATES]
    if unknown:
        print(f"GATE-RUNNER-ERROR: неизвестные gates: {', '.join(unknown)} "
              f"(доступны: {', '.join(KNOWN_GATES)})", file=sys.stderr)
        return 2
    report, code = run_gates(repo, args.scope, gates, args)
    if not report:
        return 2
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_report_human(report, repo))
    return code


def _cmd_status(args) -> int:
    report_dir = Path(args.report_dir)
    try:
        report, err = load_latest_report(report_dir)
    except (OSError, ValueError, KeyError) as exc:
        report, err = None, f"отчет нечитаем: {exc}"
    if report is None:
        print(f"GATE-RUNNER-ERROR: {err}", file=sys.stderr)
        return 2
    repo = Path(args.repo).resolve() if args.repo else None
    if args.as_json:
        out = dict(report)
        out["stale"] = repo is not None and is_stale(report, repo)
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_report_human(report, repo))
    return exit_code_for(report, repo)


def _cmd_record_review(args) -> int:
    import flow_check  # локально: единый парсер вердикта/имен (review-005 m2/m3)

    review_path = Path(args.review_path)
    # review-005 M3: diff_digest обязателен — единственная защита от approve
    # «тот же SHA, другой content» (history rewrite / force-push).
    if not args.diff_digest:
        print("GATE-RUNNER-ERROR: --diff-digest обязателен (gate_runner.py "
              "diff-digest) — без него approve «того же SHA, другого "
              "content» неотличим (ТЗ 05; review-005 M3)", file=sys.stderr)
        return 2
    # review-005 m2: вердикт sidecar сверяется с вердиктом человекочитаемого
    # .md — машинное и человеческое доказательства не расходятся.
    try:
        md_verdict = flow_check.parse_verdict(
            review_path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        print(f"GATE-RUNNER-ERROR: review-файл нечитаем: {exc}", file=sys.stderr)
        return 2
    if md_verdict != args.verdict:
        print(f"GATE-RUNNER-ERROR: вердикт в {review_path} — "
              f"{md_verdict!r}, а --verdict {args.verdict!r}: sidecar поверх "
              f"человеческого вердикта не записывается (review-005 m2)",
              file=sys.stderr)
        return 2
    # review-005 m2: reviewed_commit_sha должен существовать в репо.
    if args.repo:
        repo = Path(args.repo).resolve()
        if not repo.is_dir():
            print(f"GATE-RUNNER-ERROR: репозиторий не найден: {repo}",
                  file=sys.stderr)
            return 2
        if git_out(repo, "rev-parse", "--verify", "--quiet",
                   f"{args.commit}^{{commit}}") != args.commit:
            print(f"GATE-RUNNER-ERROR: reviewed_commit_sha {args.commit} "
                  f"не найден в {repo}", file=sys.stderr)
            return 2
    try:
        path, payload = write_review_provenance(
            review_path, args.project, args.change,
            [t.strip() for t in args.tasks.split(",") if t.strip()],
            args.author_delegation, args.reviewer_delegation, args.commit,
            args.verdict, diff_digest=args.diff_digest,
            diff_base=args.diff_base,
            sidecar_path=Path(args.sidecar) if args.sidecar else None,
        )
    except ValueError as exc:
        print(f"GATE-RUNNER-ERROR: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps({"sidecar_path": str(path),
                          "sidecar_digest": sha256_text(
                              path.read_text(encoding="utf-8")),
                          **payload},
                         ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"gate_runner: sidecar записан: {path}")
    return 0


# ------------------------------------------------------------ decision log (P0.4)


def write_decision_record(
    repo: Path, decision_id: str, scope: dict, action: str | list,
    commit: str, source: str, quote: str | None = None,
    expiration: str | None = None, decisions_dir: str | None = None,
    exist_ok: bool = False,
) -> tuple[Path, dict]:
    """Запись решения Заказчика в журнал (P0.4): decisions/<decision_id>.md.

    decision_id — YYYY-MM-DD-slug (например 2026-10-02-start-add-widget);
    обязан соответствовать шаблону, по которому flow_transition ищет запись
    (path traversal исключен разбором). Человекочитаемая часть + обязательный
    машиночитаемый блок ```decision-record (schema decision-record/1):
    decision_id, date, scope (project/change_id/phase), action (какое действие
    разрешает), commit (SHA на момент решения), source (канал/дословная
    цитата), expiration (опционально). Журнал в том же репо — НЕ независимое
    одобрение личности Заказчика; в отчетах «решение зафиксировано».
    Возвращает (путь, payload); существующая запись не перезаписывается
    (append-only история, AGENTS.md п.7) без exist_ok=True.
    """
    import re as _re
    did = str(decision_id or "").strip()
    if not _re.match(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9._-]*$", did):
        raise ValueError(
            f"decision_id {did!r} не соответствует формату YYYY-MM-DD-slug "
            f"(пример: 2026-10-02-start-add-widget)")
    if not isinstance(scope, dict) or not scope:
        raise ValueError("scope обязателен (словарь: project/change_id/phase)")
    if not action:
        raise ValueError("action обязателен (какое действие разрешает решение)")
    commit = str(commit or "").strip()
    if not _re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
        raise ValueError(
            f"commit {commit!r} не похож на SHA — запись без привязки к "
            f"версии работы не принимается (P0.4)")
    if not source:
        raise ValueError("source обязателен (канал/дословная цитата Заказчика)")
    rec_scope = {k: scope[k] for k in ("project", "change_id", "phase")
                 if scope.get(k) is not None}
    date_part = did[:10]
    payload = {
        "schema_version": DECISION_RECORD_SCHEMA,
        "decision_id": did,
        "date": date_part,
        "scope": rec_scope,
        "action": action,
        "commit": commit,
        "source": source,
    }
    if expiration:
        payload["expiration"] = str(expiration)
    ddir = Path(decisions_dir) if decisions_dir else Path(repo) / DECISIONS_DIR
    ddir.mkdir(parents=True, exist_ok=True)
    path = ddir / f"{did}.md"
    if path.exists() and not exist_ok:
        raise ValueError(
            f"запись уже существует: {path} — история решений append-only, "
            f"перезапись запрещена (AGENTS.md п.7)")
    quote_block = (
        f"\n> Цитата: {quote}\n" if quote else ""
    )
    text = (
        f"# Решение: {did}\n\n"
        f"- Дата: {date_part}\n"
        f"- Scope: {rec_scope}\n"
        f"- Действие: {action}\n"
        f"- Commit: {commit}\n"
        f"- Источник: {source}\n"
        f"{quote_block}"
        f"\nЖурнал решения: «решение зафиксировано», не «личность "
        f"подтверждена» — журнал в том же репо НЕ является независимым "
        f"одобрением Заказчика (контракт §10; P0.4).\n\n"
        f"```decision-record\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)}\n"
        f"```\n"
    )
    path.write_text(text, encoding="utf-8")
    return path, payload


def _cmd_record_decision(args) -> int:
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"GATE-RUNNER-ERROR: репозиторий не найден: {repo}",
              file=sys.stderr)
        return 2
    scope: dict = {}
    if args.scope_json:
        try:
            scope = json.loads(args.scope_json)
        except json.JSONDecodeError as exc:
            print(f"GATE-RUNNER-ERROR: --scope-json не разбирается: {exc}",
                  file=sys.stderr)
            return 2
    else:
        if args.project:
            scope["project"] = args.project
        if args.change:
            scope["change_id"] = args.change
        if args.phase is not None:
            scope["phase"] = args.phase
    if not scope:
        print("GATE-RUNNER-ERROR: scope решения обязателен (--project/--change/"
              "--phase или --scope-json)", file=sys.stderr)
        return 2
    # Без явного --commit решение не привязывается к версии работы — отказ,
    # а не молчаливая запись (P0.4: привязка к SHA обязательна).
    commit = args.commit
    if not commit:
        print("GATE-RUNNER-ERROR: --commit обязателен (SHA, к которому "
              "относится решение; 'auto' — текущий HEAD)", file=sys.stderr)
        return 2
    if commit == "auto":
        commit = git_head(repo) or ""
        if not commit:
            print("GATE-RUNNER-ERROR: HEAD репо нечитаем — --commit auto "
                  "невозможен", file=sys.stderr)
            return 2
    try:
        # action через запятую в CLI = список разрешенных действий.
        action = args.action
        if "," in action:
            action = [a.strip() for a in action.split(",") if a.strip()]
        path, payload = write_decision_record(
            repo, args.decision_id, scope, action, commit,
            args.source, quote=args.quote, expiration=args.expiration,
            exist_ok=args.force,
        )
    except ValueError as exc:
        print(f"GATE-RUNNER-ERROR: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps({"record_path": str(path),
                          "record_digest": sha256_text(
                              path.read_text(encoding="utf-8")),
                          **payload},
                         ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"gate_runner: решение зафиксировано: {path} "
              f"(decision_id={args.decision_id}; «решение зафиксировано», "
              f"не «личность подтверждена»)")
    return 0


def _cmd_diff_digest(args) -> int:
    repo = Path(args.repo).resolve()
    print(compute_diff_digest(repo, args.base, args.head))
    return 0


def _cmd_github_protection(args) -> int:
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print("GATE-RUNNER-ERROR: репозиторий не найден: {repo}".format(repo=repo),
              file=sys.stderr)
        return 2
    # SKIPPED — только по явному правилу «параметр не задан» (аналог
    # pr_validate без PR-контекста): отсутствие настройки gate не ERROR.
    if not args.github_repo or not args.github_token_env:
        print(json.dumps({
            "schema_version": PROTECTION_SCHEMA,
            "endpoint": "rules/branches",
            "status": "SKIPPED",
            "reason": "--github-repo / --github-token-env не заданы — "
                      "проверка branch protection не настроена (явное "
                      "правило неприменимости, не ERROR)",
        }, ensure_ascii=False))
        return 3
    try:
        fact = check_branch_protection(
            repo, args.github_repo, args.github_token_env,
            branch=args.branch,
            report=Path(args.report) if args.report else None,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("skip:"):
            print(json.dumps({
                "schema_version": PROTECTION_SCHEMA,
                "status": "SKIPPED",
                "reason": msg[len("skip:"):].strip(),
            }, ensure_ascii=False))
            return 3
        print(f"GATE-RUNNER-ERROR: {msg}", file=sys.stderr)
        return 2
    ok = fact.get("protection_ok") is True
    if args.as_json:
        print(json.dumps({"status": "PASS" if ok else "FAIL", **fact},
                         ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"gate_runner: github-protection "
              f"{'PASS' if ok else 'FAIL'} HTTP={fact.get('http_status')} "
              f"— {fact.get('detail')}")
        print(f"  факт: {args.report or protection_report_path(repo)}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="gate_runner.py",
        description="единый запуск gates с GateReport и audit JSONL (поставка 05)",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="запустить gates фазы")
    p_run.add_argument("--repo", required=True, help="репозиторий проекта")
    p_run.add_argument("--scope", required=True, choices=SCOPES,
                       help="точка запуска (preflight/post_agent/...)")
    p_run.add_argument("--gates", default=None,
                       help="список gate через запятую (по умолчанию — набор фазы)")
    p_run.add_argument("--openspec-cmd", default="npx openspec",
                       help="команда openspec CLI (в этой среде — npx или fake)")
    p_run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                       help=f"таймаут gate в секундах (по умолчанию {DEFAULT_TIMEOUT})")
    p_run.add_argument("--pm-mode", choices=PM_MODES, default=None,
                       help="явный режим pm_bounds_check (обязателен для gate)")
    p_run.add_argument("--pm-commits", default=None,
                       help="коммиты для --pm-mode=commits (через запятую)")
    p_run.add_argument("--pm-range", default=None,
                       help="диапазон A..B для --pm-mode=product-commits")
    p_run.add_argument("--pm-registry", default=None,
                       help="путь к active_sessions.json для --pm-mode=sessions")
    p_run.add_argument("--pm-require-review", action="store_true",
                       help="добавить --require-review к pm_bounds_check (J10)")
    p_run.add_argument("--pr-id", default=None,
                       help="трассировочный ID PR (PR-контекст для pr_validate)")
    p_run.add_argument("--log-dir", default=None, help="каталог gate-логов")
    p_run.add_argument("--report-dir", default=None, help="каталог GateReport JSON")
    p_run.add_argument("--audit", default=None, help="путь к audit JSONL (append-only)")
    p_run.add_argument("--correlation-id", default=None,
                       help="сквозной ID (по умолчанию — новый uuid)")
    p_run.add_argument("--json", action="store_true", dest="as_json")
    p_run.set_defaults(func=_cmd_run)

    p_st = sub.add_parser("status", help="последний GateReport + digest-check")
    p_st.add_argument("--report-dir", required=True)
    p_st.add_argument("--repo", default=None,
                      help="репо для digest-check (STALE при смене HEAD)")
    p_st.add_argument("--json", action="store_true", dest="as_json")
    p_st.set_defaults(func=_cmd_status)

    p_rr = sub.add_parser("record-review",
                          help="записать provenance-sidecar рядом с review-файлом")
    p_rr.add_argument("--review-path", required=True)
    p_rr.add_argument("--project", required=True)
    p_rr.add_argument("--change", required=True)
    p_rr.add_argument("--tasks", required=True, help="task IDs через запятую")
    p_rr.add_argument("--author-delegation", required=True)
    p_rr.add_argument("--reviewer-delegation", required=True)
    p_rr.add_argument("--commit", required=True, help="reviewed commit SHA")
    p_rr.add_argument("--verdict", required=True, choices=("approve", "return"))
    p_rr.add_argument("--diff-digest", required=True,
                      help="sha256 диффа base..head (ОБЯЗАТЕЛЕН: без него "
                           "approve «того же SHA, другой content» "
                           "неотличим — ТЗ 05, review-005 M3)")
    p_rr.add_argument("--diff-base", default=None)
    p_rr.add_argument("--repo", default=None,
                      help="репо для проверки существования --commit "
                           "(рекомендуется)")
    p_rr.add_argument("--sidecar", default=None, help="явный путь sidecar JSON")
    p_rr.add_argument("--json", action="store_true", dest="as_json")
    p_rr.set_defaults(func=_cmd_record_review)

    p_dd = sub.add_parser("diff-digest", help="sha256 диффа base..head")
    p_dd.add_argument("--repo", required=True)
    p_dd.add_argument("--base", required=True)
    p_dd.add_argument("--head", required=True)
    p_dd.set_defaults(func=_cmd_diff_digest)

    p_gp = sub.add_parser(
        "github-protection",
        help="решение А (P0.1): проверить branch protection main через "
             "GitHub Rulesets API (GET /repos/{repo}/rules/branches/{branch}) "
             "и записать машиночитаемый факт")
    p_gp.add_argument("--repo", required=True, help="локальный репозиторий "
                      "(сюда пишется факт .flow-evidence/github-protection.json)")
    p_gp.add_argument("--github-repo", dest="github_repo", default=None,
                      help="GitHub репозиторий OWNER/NAME (не задан → SKIPPED)")
    p_gp.add_argument("--github-token-env", dest="github_token_env", default=None,
                      help="ИМЯ env-переменной с токеном (НЕ сам токен); "
                           "переменная читается subprocess'ом (не задана → "
                           "SKIPPED)")
    p_gp.add_argument("--branch", default=PROTECTION_BRANCH,
                      help=f"ветка защиты (по умолчанию {PROTECTION_BRANCH})")
    p_gp.add_argument("--report", default=None,
                      help="явный путь JSON-факта (по умолчанию "
                           ".flow-evidence/github-protection.json в репо)")
    p_gp.add_argument("--json", action="store_true", dest="as_json")
    p_gp.set_defaults(func=_cmd_github_protection)

    p_rd = sub.add_parser(
        "record-decision",
        help="P0.4: записать решение Заказчика в журнал решений/"
             "<YYYY-MM-DD>-<slug>.md (машиночитаемый блок decision-record/1)")
    p_rd.add_argument("--repo", required=True,
                      help="репозиторий, в чей decisions/ пишется запись")
    p_rd.add_argument("--decision-id", dest="decision_id", required=True,
                      help="ID записи = имя файла без .md, формат "
                           "YYYY-MM-DD-slug (например 2026-10-02-start-x)")
    p_rd.add_argument("--project", default=None, help="project scope решения")
    p_rd.add_argument("--change", default=None, help="change_id scope решения")
    p_rd.add_argument("--phase", type=int, default=None,
                      help="flow (фаза) scope решения")
    p_rd.add_argument("--scope-json", default=None,
                      help="явный scope JSON (переопределяет --project/--change/"
                           "--phase)")
    p_rd.add_argument("--action", required=True,
                      help="какое действие разрешает решение (например "
                           "create_change; или список через запятую)")
    p_rd.add_argument("--commit", required=True,
                      help="SHA, к которому относится решение ('auto' — "
                           "текущий HEAD); обязателен")
    p_rd.add_argument("--source", required=True,
                      help="где зафиксировано решение (канал: чат-лог, "
                           "answers_roundN.md, PLAN.md)")
    p_rd.add_argument("--quote", default=None,
                      help="дословная цитата Заказчика")
    p_rd.add_argument("--expiration", default=None,
                      help="опциональная ISO-дата истечения решения")
    p_rd.add_argument("--force", action="store_true",
                      help="перезаписать существующую запись (по умолчанию "
                           "запрещено: append-only)")
    p_rd.add_argument("--json", action="store_true", dest="as_json")
    p_rd.set_defaults(func=_cmd_record_decision)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
