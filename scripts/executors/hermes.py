"""Исполнитель hermes — делегации Hermes (delegate_task) по решению ПМ.

Для пакетного раннера — ручная стоп-точка (`run: hermes:<роль>`): раннер не
запускает процесс, печатает «ЖДУ ПМ» и ждет (как до J65.5). Активные запуски
— делегации платформенного реестра `<HERMES_HOME>/state.db →
async_delegations` (пишется платформой при каждом delegate_task): по ним
delegate_watchdog сверяет обход с записями flowctl.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from . import ActiveRun, Executor

ACTIVE_STATES = ("running", "dispatched", "pending")


def _db() -> Path:
    import factory_paths as fp
    return fp.hermes_home() / "state.db"


class HermesExecutor(Executor):
    name = "hermes"
    manual = True

    def delegations(self, db: Path | None = None) -> list[dict] | None:
        """Все делегации реестра Hermes; нет/битый state.db — None."""
        db = db or _db()
        if not db.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT delegation_id, parent_session_id, state, delivery_state,"
                " dispatched_at, completed_at FROM async_delegations").fetchall()
            conn.close()
        except sqlite3.Error as e:
            print(f"WARN: state.db недоступен ({e})", file=sys.stderr)
            return None
        return [{k: r[k] for k in ("delegation_id", "state", "delivery_state",
                                   "dispatched_at", "completed_at",
                                   "parent_session_id")} for r in rows]

    def list_active(self) -> list[ActiveRun]:
        return [ActiveRun(self.name, None, "", "delegate_task", d["delegation_id"])
                for d in (self.delegations() or [])
                if d.get("state") in ACTIVE_STATES]

    def transcript(self, ref: str) -> str | None:
        import factory_paths as fp
        log = fp.hermes_home() / "cache" / "delegation" / "live" / ref / "task-0.log"
        try:
            return log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None


EXECUTOR = HermesExecutor()
