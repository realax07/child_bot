# Репозиторий sleep_events (ТЗ §9.2; change-пакет add-core-diary, задача 1.3).
#
# ВАЖНО (FR-3, design D6, review-001 F-1): пишутся ТОЛЬКО сырые поля записи
# (event_type, timestamp, created_at, is_backfilled). sleep_type и is_merged
# при записи НЕ заполняются (NULL — резерв схемы): разметка день/ночь и
# склейка вычисляются на лету при чтении, агрегаты не хранятся (ТЗ §9.5).
#
# design D12: «последняя» запись — по created_at, затем id (не по timestamp:
# событие может быть введено задним числом с ранним timestamp).
# design D11: overwrite — атомарный UPDATE целевой записи, без дублей.
#
# Вызывается только слоем services (design D1, FR-1).

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

_COLUMNS = "id, user_id, event_type, timestamp, created_at, is_backfilled, sleep_type, is_merged"


def _utcnow_iso() -> str:
    """Текущий момент в UTC (ISO-строка) — design D4."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "user_id": row[1],
        "event_type": row[2],
        "timestamp": row[3],
        "created_at": row[4],
        "is_backfilled": bool(row[5]),
        "sleep_type": row[6],
        "is_merged": row[7],
    }


class SleepEventsRepo:
    """CRUD-доступ к таблице sleep_events (ТЗ §9.2)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(
        self,
        user_id: int,
        event_type: str,
        timestamp: str,
        *,
        is_backfilled: bool = False,
        created_at: str | None = None,
    ) -> int:
        """Записать сыгое событие; вернуть id строки.

        Только сырые поля (FR-3, design D6): sleep_type и is_merged остаются
        NULL. Валидность event_type гарантирует CHECK в схеме (ТЗ §9.2).
        """
        cur = self._conn.execute(
            "INSERT INTO sleep_events (user_id, event_type, timestamp, "
            "created_at, is_backfilled, sleep_type, is_merged) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL)",
            (user_id, event_type, timestamp, created_at or _utcnow_iso(), int(is_backfilled)),
        )
        self._conn.commit()
        row_id = cur.lastrowid
        assert row_id is not None  # всегда задан после успешного INSERT
        return row_id

    def list_by_user(self, user_id: int) -> list[dict]:
        """Все события пользователя в хронологическом порядке (по timestamp, затем id)."""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM sleep_events WHERE user_id = ? "
            "ORDER BY timestamp, id",
            (user_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def last(self, user_id: int) -> dict | None:
        """Последняя запись пользователя (D12): максимум по created_at, затем id."""
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM sleep_events WHERE user_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return None if row is None else _row_to_dict(row)

    def delete_last(self, user_id: int) -> bool:
        """Удалить последнюю запись (D12, FR-21). False — событий нет, данных нет изменений."""
        row = self._conn.execute(
            "SELECT id FROM sleep_events WHERE user_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row is None:
            return False
        self._conn.execute("DELETE FROM sleep_events WHERE id = ?", (row[0],))
        self._conn.commit()
        return True

    def overwrite(
        self,
        event_id: int,
        *,
        event_type: str | None = None,
        timestamp: str | None = None,
    ) -> bool:
        """Атомарно перезаписать целевую запись (D11, FR-18): один UPDATE по id.

        Меняются только переданные поля; sleep_type/is_merged не трогаются
        (остаются NULL — design D6). False — записи с таким id нет.
        """
        sets: list[str] = []
        params: list = []
        if event_type is not None:
            sets.append("event_type = ?")
            params.append(event_type)
        if timestamp is not None:
            sets.append("timestamp = ?")
            params.append(timestamp)
        if not sets:
            return False
        params.append(event_id)
        cur = self._conn.execute(
            f"UPDATE sleep_events SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        self._conn.commit()
        return cur.rowcount > 0
