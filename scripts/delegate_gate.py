#!/usr/bin/env python3
"""delegate-and-gate — обертка над flowctl: делегация не существует вне машины.

Решение Заказчика 2026-10-03 (инцидент add-containerization: 6 делегаций
диспатчились напрямую, минуя flowctl prepare/run/finish — state machine
стояла в стороне). Обертка сцепляет существующие команды flowctl в один
принудительный цикл; логика флоу НЕ дублируется — только сцепка.

Цикл:
  delegate-gate.py start   = flowctl prepare + run (DENY → делегация не создается)
  delegate-gate.py finish  = flowctl finish (без PASS-вердикта сессия висит running)
  delegate-gate.py status  = flowctl status (открытые сессии = видимый долг)

Флоу задается явно (--flow N) — машина валидирует по правилам этого флоу.
Критерии финиша — по флоу (см. contracts/flow_control_contract.md §действия):
  Флоу 1: dev + НЕЗАВИСИМЫЙ code_reviewer (delegation_id ≠ author) + QA;
          [ops]/[docs]-задачи — протокол приемки вместо review-файла.
  Флоу 2: dev + ревьюер (без полного QA-цикла).
  Флоу 3: хотфикс + пометка долга (STALE_EVIDENCE при незакрытии).
  Флоу 4: [chore], минимальный гейт-набор (flow_check, pm_bounds J3).
  Флоу 5: экспресс, компактный набор.

SELF_REVIEW ловится механически: author_delegation_id ≠ reviewer_delegation_id.

Н8 (аудит): флаги Флоу 3/4/5 принимаются и пробрасываются в prepare —
--incident-ref (обязателен машине Флоу 3, с severity-префиксом S1-/S2-/S3-),
--spec-delta, --small-change (условие Флоу 5), --paths (пути изменения для
protected-paths ворот J3; зоны — по-прежнему --path). Без них Флоу 3/4/5
через машину делегаций непроходимы.

Н18 (аудит): --registry PATH — sandbox-режим проверочных прогонов: и
резервации, и state пишутся ТОЛЬКО в указанный путь; боевой
~/.hermes/state/active_sessions.json не трогается (раньше prepare уходил в
дефолтный реестр, и проверочная делегация создавала боевую running-сессию
с зоной backend/**). Без флага — прежнее поведение (боевые пути).

Использование (из корня проекта):
  delegate-gate.py start  --repo . --project wiki --flow 1 --change <id> \
      --task <N> --action dev_task --role dev --path "services/**" \
      --approval-ref <decision_id> --owner-pm pm-main [--registry PATH]
  delegate-gate.py start  --flow 3 --change BUG-042 --action emergency_stabilize \
      --role dev --incident-ref S1-<ref> --path "services/**" ...
  delegate-gate.py start  --flow 4 --change chore --action chore_task \
      --role dev --path "scripts/**" --paths "scripts/x.sh" ...
  delegate-gate.py start  --flow 5 --change <id> --action express_task \
      --role dev --small-change --path "src/**" ...
  delegate-gate.py finish --correlation-id <id> --report <path> [--pr-id <N>]
  delegate-gate.py status [--registry PATH]

Exit: 0 = разрешено/закрыто; 1 = DENY/незакрыто; 2 = ошибка использования.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

STATE = Path.home() / ".hermes" / "state" / "flowctl_state.json"
REGISTRY = Path.home() / ".hermes" / "state" / "active_sessions.json"

# Минимальные обязательные гейты финиша по флоу (дополнительные добавляет
# flowctl из политики; обертка гарантирует нижнюю границу).
FLOW_FINISH_GATES = {
    1: ["flow_check", "pm_bounds_check"],           # + approve-ревью независимого (см. check_review_independence)
    2: ["flow_check", "pm_bounds_check"],
    3: ["flow_check"],                               # хотфикс: долг фиксируется отдельно
    4: ["flow_check", "pm_bounds_check"],
    5: ["flow_check"],
}


def _state_paths(a: argparse.Namespace) -> tuple[Path, Path]:
    """Н18: (registry, state) для прогона. --registry PATH → sandbox: оба
    файла живут рядом с указанным реестром, боевой ~/.hermes/state не
    трогается. Без флага — дефолтные (боевые) пути."""
    reg = getattr(a, "registry", None)
    if reg:
        registry = Path(reg).expanduser()
        # Sandbox-каталог создается на месте (иначе _acquire_lock падает на
        # отсутствующем родителе) — Н18: проверочный прогон самодостаточен.
        registry.parent.mkdir(parents=True, exist_ok=True)
        return registry, registry.parent / "flowctl_state.json"
    return REGISTRY, STATE


def _flowctl(args: list[str]) -> tuple[int, str]:
    repo_flowctl = Path("scripts/flowctl.py")
    cmd = [sys.executable, str(repo_flowctl if repo_flowctl.exists() else Path(__file__).parent / "flowctl.py")] + args
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def _extract_correlation_id(out: str, registry: Path, state: Path) -> str | None:
    """correlation_id из вывода prepare --json (машиночитаемо); фолбэк —
    регэксп по человекочитаемому выводу, затем крайняя запись state."""
    try:
        data = json.loads(out)
        if isinstance(data, dict) and data.get("correlation_id"):
            return str(data["correlation_id"])
    except ValueError:
        pass
    m = re.search(r'"correlation_id"\s*:\s*"([^"]+)"', out)
    if m:
        return m.group(1)
    m = re.search(r"correlation_id:\s*(\S+)", out)
    if m:
        return m.group(1)
    try:
        st = json.loads(state.read_text(encoding="utf-8"))
        runs = st.get("runs", {})
        if runs:
            return sorted(runs)[-1]
    except Exception:
        pass
    return None


def cmd_start(a: argparse.Namespace) -> int:
    registry, state = _state_paths(a)
    prep = [
        "prepare", "--repo", a.repo, "--project", a.project,
        "--flow", str(a.flow), "--change", a.change,
        "--owner-pm", a.owner_pm,
        # Н18: явные пути реестра/state — prepare не уходит в боевой
        # ~/.hermes/state при sandbox-прогоне с --registry.
        "--registry", str(registry), "--state", str(state),
    ]
    if a.task:
        prep += ["--task", a.task]
    if a.action:
        prep += ["--action", a.action]
    if a.role:
        prep += ["--role", a.role]
    if a.path:
        for p in a.path:
            prep += ["--path", p]
    if a.approval_ref:
        prep += ["--approval-ref", a.approval_ref]
    # Н8: флаги Флоу 3/4/5 — проброс в машину (prepare), иначе она их
    # не видит и DENY-ит корректное действие.
    if a.incident_ref:
        prep += ["--incident-ref", a.incident_ref]
    if a.spec_delta:
        prep += ["--spec-delta"]
    if a.small_change:
        prep += ["--small-change"]
    if a.paths:
        prep += ["--paths", a.paths]

    rc, out = _flowctl(prep)
    print(out)
    # flowctl может вернуть rc=0 с reservation denied (MISSING_INPUT/DENY) —
    # читаем машиночитаемый факт, не только exit code.
    prepared_ok = bool(re.search(r'"prepared"\s*:\s*true', out, re.I)) or bool(re.search(r"^prepared:\s*true", out, re.I | re.M))
    if rc != 0 or not prepared_ok:
        print("GATE: prepare не прошел — делегация НЕ создается (машина DENY).", file=sys.stderr)
        return 1
    cid = _extract_correlation_id(out, registry, state)
    if not cid:
        print("GATE: correlation_id не найден — run невозможен.", file=sys.stderr)
        return 2
    rc, out = _flowctl(["run", "--correlation-id", cid,
                        "--registry", str(registry), "--state", str(state)])
    print(out)
    if rc != 0:
        print("GATE: run DENY — делегация НЕ создается.", file=sys.stderr)
        return 1
    print(f"GATE: ALLOW — correlation_id={cid}. Диспатч делегации разрешен; "
          f"по завершении ОБЯЗАТЕЛЬНО delegate-gate.py finish --correlation-id {cid}")
    return 0


def cmd_finish(a: argparse.Namespace) -> int:
    registry, state = _state_paths(a)
    gates = FLOW_FINISH_GATES.get(a.flow, ["flow_check"])
    args = ["finish", "--correlation-id", a.correlation_id,
            "--report", a.report, "--state", str(state),
            "--registry", str(registry), "--gate-scope", "pre_merge",
            "--pm-mode", "commits", "--pm-commits", a.repo]
    if a.pr_id:
        args += ["--pr-id", str(a.pr_id)]
    rc, out = _flowctl(args)
    print(out)
    missing = [g for g in gates if g not in out and g.replace("_", "-") not in out]
    if rc != 0:
        print("GATE: finish НЕ принят — сессия остается открытой (running).", file=sys.stderr)
        return 1
    if missing:
        print(f"GATE: не покрыты обязательные гейты флоу {a.flow}: {', '.join(missing)} "
              f"— финиш НЕ закрыт.", file=sys.stderr)
        return 1
    print("GATE: finish принят — сессия закрыта по фактам.")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    registry, state = _state_paths(a)
    rc, out = _flowctl(["status", "--registry", str(registry),
                        "--state", str(state)])
    print(out[:4000])
    open_sessions = out.count('"state": "running"') + out.count("'state': 'running'")
    print(f"\nGATE: открытых сессий (running): {open_sessions} — каждая требует finish.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="delegate-and-gate: делегация вне машины невозможна")
    sub = p.add_subparsers(required=True)
    ps = sub.add_parser("start")
    ps.add_argument("--repo", default=".")
    ps.add_argument("--project", required=True)
    ps.add_argument("--flow", type=int, required=True, choices=[1, 2, 3, 4, 5])
    ps.add_argument("--change", required=True)  # Флоу 4: ровно "chore"
    ps.add_argument("--task")
    ps.add_argument("--action")   # dev_task/review_task/ops_task/...
    ps.add_argument("--role")
    ps.add_argument("--path", action="append")  # зоны, можно несколько
    ps.add_argument("--approval-ref")             # decision_id
    ps.add_argument("--owner-pm", default="pm-main")
    # Н8: входы Флоу 3/4/5 (проброс в flowctl prepare).
    ps.add_argument("--incident-ref", default=None,
                    help="Флоу 3: ссылка на инцидент с severity-префиксом "
                         "S1-/S2-/S3- (J48)")
    ps.add_argument("--spec-delta", action="store_true",
                    help="действие меняет спеку (Флоу 3/1)")
    ps.add_argument("--small-change", action="store_true",
                    help="Флоу 5: подтверждение условий экспресс-режима")
    ps.add_argument("--paths", default=None,
                    help="пути изменения для protected-paths ворот (J3), "
                         "через запятую (Н14; зоны — отдельный --path)")
    # Н18: sandbox-реестр проверочных прогонов.
    ps.add_argument("--registry", default=None,
                    help="путь к active_sessions.json (Н18: с флагом боевой "
                         "~/.hermes/state не трогается; state — рядом)")
    ps.set_defaults(func=cmd_start)

    pf = sub.add_parser("finish")
    pf.add_argument("--correlation-id", required=True)
    pf.add_argument("--report", required=True)
    pf.add_argument("--flow", type=int, required=True)
    pf.add_argument("--repo", default=".")
    pf.add_argument("--pr-id", type=int)
    pf.add_argument("--registry", default=None,
                    help="путь к active_sessions.json (Н18: sandbox-режим)")
    pf.set_defaults(func=cmd_finish)

    pst = sub.add_parser("status")
    pst.add_argument("--registry", default=None,
                     help="путь к active_sessions.json (Н18: sandbox-режим)")
    pst.set_defaults(func=cmd_status)
    a = p.parse_args()
    sys.exit(a.func(a))


if __name__ == "__main__":
    main()
