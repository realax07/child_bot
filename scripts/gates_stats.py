#!/usr/bin/env python3
"""gates_stats.py — сколько раз срабатывал каждый гейт и код отказа (J65.9).

Дисциплина роста ворот: новый гейт — при повторе класса инцидента или
высокой цене одиночного промаха; гейт без срабатываний за период —
кандидат на удаление или перевод в предупреждение (квартальный пересмотр,
резолюция — в бэклоге). Эта команда дает фактуру для пересмотра; решение —
за Заказчиком.

Источники — уже существующие файлы памяти движка (новых журналов нет):
  1. отчеты gate_runner      <state>/gate-reports/gate-report-*.json
                             (gate_id × статус PASS/FAIL/ERROR/SKIPPED);
  2. циклы flowctl           <state>/flowctl_state.json, runs:
                             решение prepare (статус, blocking_reasons —
                             коды flow_transition.REASON_CODES), итог цикла,
                             дефекты finish (код × источник);
  3. энфорсер                <state>/flow-enforcer.log (JSONL, события).
<state> — каталог flowctl_state.json (по умолчанию factory_paths.state_dir(),
env AF_STATE_DIR); лог энфорсера — FLOW_ENFORCER_STATE_DIR, если задан
(как у энфорсера), иначе <state>. Отчеты gate_runner из других каталогов
(`finish --report-dir`, прямой `gate_runner run` → `<repo>/.gate-logs`,
CI) — флагом --reports DIR (повторяемый). Окно — по UTC-дате поля
времени записи (started_at / created_at / ts; все источники пишут UTC),
даты включительно; записи без времени при заданном окне не считаются —
их число в отчете (undated_skipped).

Не видит (честно), поэтому такие коды идут отдельным списком
«не наблюдаемы по этим источникам», а не в «без срабатываний»:
  - отказы prepare ДО записи цикла: зона вне политики роли
    (ZONE_OUTSIDE_POLICY), защищенные пути без [pipeline] (J3),
    ZONE_CONFLICT резервации (session_check.reserve);
  - STALE_SNAPSHOT отказа `flowctl run` (виден только код решения prepare);
  - отказы на спавне через энфорсер: хук зовет `prepare --dry-run`, state
    не пишется, в логе энфорсера — только rc (`prepare_result`), не коды;
  - STOP раннера — в его state рядом с планом проекта.

Usage:
  python3 scripts/gates_stats.py [--state PATH] [--since YYYY-MM-DD]
                                 [--until YYYY-MM-DD] [--json]
  python3 scripts/flowctl.py gates-stats ...   (то же)

Exit: 0 — отчет построен (в т.ч. пустой); 2 — ошибка использования/чтения.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import factory_paths as fp  # noqa: E402
import flow_transition as ft  # noqa: E402
import gate_runner as gr  # noqa: E402

SCHEMA = "gates-stats/1"
# Коды REASON_CODES, которые по этим источникам не видны целиком или
# частично (см. шапку) — в «без срабатываний» не попадают. Новый код без
# Finding(...) в flow_transition обязан попасть сюда (тест TC-J65-GST-006).
UNOBSERVABLE_CODES = {
    "ZONE_CONFLICT": "резервация session_check в prepare — цикл не пишется",
    "STALE_SNAPSHOT": "отказ flowctl run (переснимок) в runs не пишется",
}
SPAWN_NOTE = ("отказы на спавне (энфорсер → prepare --dry-run) в state не "
              "пишутся: в логе энфорсера только rc prepare_result, без кодов")


class StatsError(Exception):
    pass


def _day(ts: object) -> str | None:
    """'2026-11-11T14:12:20+00:00' → '2026-11-11'; иначе None."""
    s = str(ts or "")
    try:
        date.fromisoformat(s[:10])
    except ValueError:
        return None
    return s[:10]


def _in_window(ts: object, since: str | None, until: str | None) -> bool:
    day = _day(ts)
    if day is None:
        return since is None and until is None  # без времени — только без окна
    return (since is None or day >= since) and (until is None or day <= until)


def _read_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatsError(f"{path}: не читается ({exc})") from exc
    if not isinstance(data, dict):
        raise StatsError(f"{path}: ожидался JSON-объект, есть {type(data).__name__}")
    return data


def enforcer_log_default(state_dir: Path) -> Path:
    """Лог энфорсера там же, где его пишет энфорсер (FLOW_ENFORCER_STATE_DIR)."""
    env = os.environ.get("FLOW_ENFORCER_STATE_DIR", "").strip()
    return (Path(env).expanduser() if env else state_dir) / "flow-enforcer.log"


def collect(state_path: Path, since: str | None = None,
            until: str | None = None, report_dirs: list[Path] | None = None,
            enforcer_log: Path | None = None) -> dict:
    state_dir = state_path.parent
    gates: dict[str, Counter] = {}
    reports = 0
    undated = 0
    broken: list[str] = []
    windowed = since is not None or until is not None
    # flowctl finish пишет отчеты в <state>/gate-reports (flowctl.py: report_dir
    # = state_path.parent / "gate-reports"); прочие каталоги — --reports
    dirs = [state_dir / "gate-reports", *(report_dirs or [])]
    seen_files: set[Path] = set()
    files = []
    for rdir in dirs:
        if rdir in (report_dirs or []) and not rdir.is_dir():
            broken.append(f"{rdir}: каталог отчетов (--reports) не найден")
        for rp in sorted(rdir.glob("gate-report-*.json")) if rdir.is_dir() else []:
            if rp.resolve() not in seen_files:
                seen_files.add(rp.resolve())
                files.append(rp)
    for rp in files:
        try:
            rep = _read_json(rp)
        except StatsError as exc:
            broken.append(str(exc))
            continue
        if windowed and _day(rep.get("started_at")) is None:
            undated += 1
            continue
        if not _in_window(rep.get("started_at"), since, until):
            continue
        items = rep.get("gates") or []
        if not isinstance(items, list) or not all(isinstance(g, dict) for g in items):
            broken.append(f"{rp}: gates — не список объектов")
            continue
        reports += 1
        for g in items:
            gates.setdefault(str(g.get("gate_id", "?")), Counter())[
                str(g.get("status", "?"))] += 1

    decisions: Counter = Counter()          # статус решения prepare
    reasons: Counter = Counter()            # коды blocking_reasons
    reasons_by_action: dict[str, Counter] = {}
    outcomes: Counter = Counter()           # итог цикла (accepted/returned/…)
    defects: Counter = Counter()            # дефекты finish: "код (источник)"
    runs = 0
    if state_path.is_file():
        st = _read_json(state_path)
        runs_map = st.get("runs") or {}
        if not isinstance(runs_map, dict):
            raise StatsError(f"{state_path}: runs — не объект")
        for rec in runs_map.values():
            if not isinstance(rec, dict):
                broken.append(f"{state_path}: запись runs — не объект")
                continue
            ts = rec.get("created_at") or rec.get("started_at")
            if windowed and _day(ts) is None:
                undated += 1
                continue
            if not _in_window(ts, since, until):
                continue
            runs += 1
            dec = rec.get("decision") or {}
            decisions[str(dec.get("status", "?"))] += 1
            for code in dec.get("blocking_reasons") or []:
                reasons[str(code)] += 1
                reasons_by_action.setdefault(str(rec.get("action", "?")),
                                             Counter())[str(code)] += 1
            outcomes[str(rec.get("status", "?"))] += 1
            for d in rec.get("defects") or []:
                defects[f"{d.get('code', '?')} ({d.get('source', '?')})"] += 1

    events: Counter = Counter()
    warnings: Counter = Counter()
    elog = enforcer_log or enforcer_log_default(state_dir)
    if elog.is_file():
        for line in elog.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                broken.append(f"{elog}: строка не JSON")
                continue
            if not isinstance(ev, dict):
                continue
            if windowed and _day(ev.get("ts")) is None:
                undated += 1
                continue
            if not _in_window(ev.get("ts"), since, until):
                continue
            name = str(ev.get("event", "?"))
            events[name] += 1
            if ev.get("level") == "WARNING":
                warnings[name] += 1

    fired = {g for g, c in gates.items()
             if c.get(gr.STATUS_FAIL) or c.get(gr.STATUS_ERROR)}
    # исполнялся (PASS) без отказов — честный кандидат пересмотра; только
    # SKIPPED (pr_validate без PR-контекста) или ни разу — отдельно
    executed = {g for g, c in gates.items() if c.get(gr.STATUS_PASS)}
    return {
        "schema_version": SCHEMA,
        "state": str(state_path),
        "window": {"since": since, "until": until},
        "gate_reports": reports,
        "gates": {g: dict(sorted(c.items())) for g, c in sorted(gates.items())},
        "gates_never_failed": sorted((set(gr.KNOWN_GATES) & executed) - fired),
        "gates_not_executed": sorted(set(gr.KNOWN_GATES) - executed - fired),
        "flowctl_runs": runs,
        "decisions": dict(sorted(decisions.items())),
        "reason_codes": dict(reasons.most_common()),
        "reason_codes_by_action": {a: dict(c.most_common())
                                   for a, c in sorted(reasons_by_action.items())},
        "reason_codes_never_fired": [c for c in ft.REASON_CODES
                                     if not reasons[c] and c not in UNOBSERVABLE_CODES],
        "reason_codes_unobservable": dict(UNOBSERVABLE_CODES),
        "run_outcomes": dict(sorted(outcomes.items())),
        "finish_defects": dict(defects.most_common()),
        "enforcer_events": dict(events.most_common()),
        "enforcer_warnings": dict(warnings.most_common()),
        "undated_skipped": undated,
        "unreadable": broken,
        "not_observed": SPAWN_NOTE,
    }


def render(s: dict) -> str:
    w = s["window"]
    out = [f"gates-stats: {s['state']} "
           f"(окно {w['since'] or '…'} — {w['until'] or '…'})", ""]
    out.append(f"Гейты gate_runner — отчетов: {s['gate_reports']}")
    for g, c in s["gates"].items():
        out.append(f"  {g}: " + ", ".join(f"{k} {v}" for k, v in c.items()))
    out.append("  исполнялись без FAIL/ERROR: "
               + (", ".join(s["gates_never_failed"]) or "—"))
    out.append("  не исполнялись (только SKIPPED или нет отчетов): "
               + (", ".join(s["gates_not_executed"]) or "—"))
    out += ["", f"Решения flowctl prepare — циклов: {s['flowctl_runs']}"]
    out.append("  статусы: " + (", ".join(f"{k} {v}" for k, v in
                                           s["decisions"].items()) or "—"))
    out.append("  коды отказа: " + (", ".join(f"{k} {v}" for k, v in
                                              s["reason_codes"].items()) or "—"))
    for a, c in s["reason_codes_by_action"].items():
        out.append(f"    {a}: " + ", ".join(f"{k} {v}" for k, v in c.items()))
    out.append("  коды без срабатываний: "
               + (", ".join(s["reason_codes_never_fired"]) or "—"))
    out.append("  не наблюдаемы по этим источникам: "
               + "; ".join(f"{k} — {v}" for k, v in s["reason_codes_unobservable"].items()))
    out.append("  итог циклов: " + (", ".join(f"{k} {v}" for k, v in
                                              s["run_outcomes"].items()) or "—"))
    out.append("  дефекты finish: " + (", ".join(f"{k} {v}" for k, v in
                                                 s["finish_defects"].items()) or "—"))
    out += ["", "Энфорсер — события: " + (", ".join(
        f"{k} {v}" for k, v in s["enforcer_events"].items()) or "—")]
    if s["enforcer_warnings"]:
        out.append("  из них WARNING: " + ", ".join(
            f"{k} {v}" for k, v in s["enforcer_warnings"].items()))
    if s["undated_skipped"]:
        out.append(f"WARN: записей без времени вне окна: {s['undated_skipped']}")
    for b in s["unreadable"]:
        out.append(f"WARN: {b}")
    out += ["", "Не видно: отказы prepare до записи цикла (ZONE_OUTSIDE_POLICY, "
                "защищенные пути J3, ZONE_CONFLICT), " + s["not_observed"]
            + ", STOP раннера (state рядом с планом)."]
    return "\n".join(out)


def add_arguments(ap: argparse.ArgumentParser) -> None:
    """Флаги команды — одни для gates_stats.py и `flowctl gates-stats`."""
    ap.add_argument("--state", default=None,
                    help="flowctl_state.json (по умолчанию — state движка)")
    ap.add_argument("--since", default=None, help="UTC-дата YYYY-MM-DD включительно")
    ap.add_argument("--until", default=None, help="UTC-дата YYYY-MM-DD включительно")
    ap.add_argument("--reports", action="append", default=[],
                    help="доп. каталог отчетов gate_runner (повторяемый)")
    ap.add_argument("--enforcer-log", default=None,
                    help="лог энфорсера (по умолчанию FLOW_ENFORCER_STATE_DIR или <state>)")
    ap.add_argument("--json", action="store_true", dest="as_json")


def run(args: argparse.Namespace) -> int:
    for name in ("since", "until"):
        val = getattr(args, name)
        if val is not None and _day(val) != val:
            print(f"gates-stats: --{name} — дата YYYY-MM-DD, есть {val!r}",
                  file=sys.stderr)
            return 2
    if args.since and args.until and args.since > args.until:
        print(f"gates-stats: --since {args.since} позже --until {args.until}",
              file=sys.stderr)
        return 2
    state = Path(args.state) if args.state else fp.flowctl_state_path()
    try:
        stats = collect(state, args.since, args.until,
                        [Path(d) for d in args.reports],
                        Path(args.enforcer_log) if args.enforcer_log else None)
    except StatsError as exc:
        print(f"gates-stats: ОШИБКА — {exc}", file=sys.stderr)
        return 2
    print(json.dumps(stats, ensure_ascii=False, indent=2) if args.as_json
          else render(stats))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
