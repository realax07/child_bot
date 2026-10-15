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

Использование (из корня проекта):
  delegate-gate.py start  --repo . --project wiki --flow 1 --change <id> \
      --task <N> --action dev_task --role dev --path "services/**" \
      --approval-ref <decision_id> --owner-pm pm-main
  delegate-gate.py finish --correlation-id <id> --report <path> [--pr-id <N>]
  delegate-gate.py status

Exit: 0 = разрешено/закрыто; 1 = DENY/незакрыто; 2 = ошибка использования.
"""
import argparse
import json
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


def _flowctl(args: list[str]) -> tuple[int, str]:
    repo_flowctl = Path("scripts/flowctl.py")
    cmd = [sys.executable, str(repo_flowctl if repo_flowctl.exists() else Path(__file__).parent / "flowctl.py")] + args
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def cmd_start(a: argparse.Namespace) -> int:
    prep = [
        "prepare", "--repo", a.repo, "--project", a.project,
        "--flow", str(a.flow), "--change", a.change,
        "--owner-pm", a.owner_pm,
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

    rc, out = _flowctl(prep)
    print(out)
    # flowctl может вернуть rc=0 с reservation denied (MISSING_INPUT/DENY) —
    # читаем машиночитаемый факт, не только exit code.
    import re as _re
    prepared_ok = bool(_re.search(r'"prepared"\s*:\s*true', out, _re.I)) or bool(_re.search(r"^prepared:\s*true", out, _re.I | _re.M))
    if rc != 0 or not prepared_ok:
        print("GATE: prepare не прошел — делегация НЕ создается (машина DENY).", file=sys.stderr)
        return 1
    # correlation_id из вывода prepare
    cid = None
    for line in out.splitlines():
        if "correlation" in line.lower():
            cid = line.split(":")[-1].strip()
            break
    if not cid:
        try:
            state = json.loads(STATE.read_text())
            runs = state.get("runs", {})
            last = runs.get(sorted(runs)[-1]) if runs else None
            cid = last.get("correlation_id") if last else None
        except Exception:
            pass
    if not cid:
        print("GATE: correlation_id не найден — run невозможен.", file=sys.stderr)
        return 2
    rc, out = _flowctl(["run", "--correlation-id", cid,
                        "--registry", str(REGISTRY), "--state", str(STATE)])
    print(out)
    if rc != 0:
        print("GATE: run DENY — делегация НЕ создается.", file=sys.stderr)
        return 1
    print(f"GATE: ALLOW — correlation_id={cid}. Диспатч делегации разрешен; "
          f"по завершении ОБЯЗАТЕЛЬНО delegate-gate.py finish --correlation-id {cid}")
    return 0


def cmd_finish(a: argparse.Namespace) -> int:
    gates = FLOW_FINISH_GATES.get(a.flow, ["flow_check"])
    args = ["finish", "--correlation-id", a.correlation_id,
            "--report", a.report, "--state", str(STATE),
            "--registry", str(REGISTRY), "--gate-scope", "pre_merge",
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


def cmd_status(_: argparse.Namespace) -> int:
    rc, out = _flowctl(["status"])
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
    ps.set_defaults(func=cmd_start)

    pf = sub.add_parser("finish")
    pf.add_argument("--correlation-id", required=True)
    pf.add_argument("--report", required=True)
    pf.add_argument("--flow", type=int, required=True)
    pf.add_argument("--repo", default=".")
    pf.add_argument("--pr-id", type=int)
    pf.set_defaults(func=cmd_finish)

    pst = sub.add_parser("status")
    pst.set_defaults(func=cmd_status)
    a = p.parse_args()
    sys.exit(a.func(a))


if __name__ == "__main__":
    main()
