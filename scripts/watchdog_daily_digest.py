#!/usr/bin/env python3
"""watchdog_daily_digest.py — ежедневная сводка Заказчику (23:00 UTC).

Формат (решение Заказчика 2026-10-05: чат только для суточной сводки):
  - сколько прогонов делал delegate_watchdog за 24ч (exit 0 / exit 1);
  - сколько инцидентов найдено (bypass / stale_finish), список id;
  - сколько ньюджей доставлено ПМ (pm_nudge sent_history);
  - сколько раз ПМ отреагировал: ack в ACK-файле за окно + decisions/
    с decision_id, созданными за окно, и указанием реф. на инциденты;
  - что осталось неразобранным (новые id без ack).

Детерминирован: читает только state-файлы, ничего не пишет.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

WATCHDOG_LOG = Path.home() / ".hermes" / "state" / "delegate_watchdog.log"
INCIDENTS = Path.home() / ".hermes" / "state" / "delegate_bypass_incidents.json"
ACK_STATE = Path.home() / ".hermes" / "state" / "delegate_watchdog_ack.json"
NUDGE_STATE = Path.home() / ".hermes" / "state" / "delegate_watchdog_pm_nudge.json"
DECISIONS_DIR = Path.home() / "ekotov-wiki" / "decisions"
REGISTRY_DB = Path.home() / ".hermes" / "state.db"
WINDOW = 24 * 3600


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def main() -> int:
    now = time.time()
    since = now - WINDOW

    # 1. прогоны вотчдога за окно: строки в логе без дат — считаем по
    # mtime-окну файла нельзя; поэтому прогоны меряем cron-output-каталогом.
    out_dir = Path.home() / ".hermes" / "cron" / "output" / "f4e89e20fe2c"
    runs_total = runs_fail = 0
    if out_dir.is_dir():
        for f in out_dir.glob("*.md"):
            try:
                # имя вида 2026-10-05_19-05-13.md
                stamp = time.mktime(time.strptime(
                    f.stem, "%Y-%m-%d_%H-%M-%S"))
            except ValueError:
                continue
            if stamp < since:
                continue
            runs_total += 1
            if "script failed" in f.read_text(errors="replace")[:400]:
                runs_fail += 1

    # 2. инциденты: снимок INCIDENTS + платформенные делегации за окно
    inc = json.loads(INCIDENTS.read_text()) if INCIDENTS.exists() else {}
    bypass_ids = [b["delegation_id"] for b in inc.get("bypass", [])
                  if (b.get("created_at") or 0) >= since]
    # делегации за окно из платформенного реестра (для полноты)
    deleg_new = []
    try:
        conn = sqlite3.connect(f"file:{REGISTRY_DB}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT delegation_id, state, dispatched_at FROM async_delegations"
        ).fetchall()
        for did, state, disp in rows:
            try:
                t = float(disp) if disp else 0
            except (TypeError, ValueError):
                continue
            if t >= since:
                deleg_new.append((did, state))
    except sqlite3.Error:
        pass

    # 3. ньюджи ПМ за окно
    nudge = json.loads(NUDGE_STATE.read_text()) if NUDGE_STATE.exists() else {}
    nudges = [t for t in nudge.get("sent_history", []) if t >= since]

    # 4. реакции ПМ: ack-файл + decisions за окно
    ack = json.loads(ACK_STATE.read_text()) if ACK_STATE.exists() else {}
    acked = set(ack.get("acknowledged", []))
    decisions = []
    if DECISIONS_DIR.is_dir():
        for f in sorted(DECISIONS_DIR.glob("*.md")):
            if f.stat().st_mtime >= since:
                decisions.append(f.stem)
    unresolved = [i for i in bypass_ids if i not in acked]

    print(f"[WATCHDOG DIGEST] окно 24ч, на {iso(now)}")
    print(f"Прогонов delegate-watchdog: {runs_total} (с инцидентами: {runs_fail})")
    print(f"Делегаций за сутки: {len(deleg_new)} — "
          + (", ".join(f"{d}:{s}" for d, s in deleg_new) or "—"))
    print(f"Новых инцидентов bypass: {len(bypass_ids)} — "
          + (", ".join(bypass_ids) or "—"))
    print(f"Ньюджей ПМ доставлено: {len(nudges)}")
    print(f"Реакции ПМ: ack-файл пополнен"
          f"{' да' if any(i in acked for i in bypass_ids) else ' (без новых)'}; "
          f"decisions за сутки: {len(decisions)} — "
          + (", ".join(decisions) or "—"))
    print(f"Неразобранных (без ack): {len(unresolved)} — "
          + (", ".join(unresolved) or "—"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
