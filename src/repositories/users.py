# Репозиторий users (ТЗ §9.1; change-пакет add-core-diary, задача 1.3).
#
# design D3: таблица users строго по ТЗ §9.1 + role TEXT NOT NULL DEFAULT
# 'parent' (FR-22, решение 26.10 №5).
# design D4: created_at пишем в UTC; TZ пользователя хранится как IANA-имя
# в users.timezone и применяется на границе ввода/вывода, не здесь.
#
# Вызывается только слоем services (design D1, FR-1).

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _utcnow_iso() -> str:
    """Текущий момент в UTC (ISO-строка) — design D4."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class UsersRepo:
    """CRUD-доступ к таблице users (ТЗ §9.1)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_or_create(
        self,
        telegram_id: int,
        timezone: str | None = None,
    ) -> dict:
        """Найти пользователя по telegram_id или создать при первом обращении.

        Идемпотентно: повторный вызов возвращает того же пользователя
        (FR-5). Возвращает dict с id, telegram_id, role, timezone.
        """
        row = self._conn.execute(
            "SELECT id, telegram_id, role, timezone FROM users "
            "WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if row is not None:
            return {"id": row[0], "telegram_id": row[1], "role": row[2], "timezone": row[3]}

        self._conn.execute(
            "INSERT INTO users (telegram_id, timezone, created_at) "
            "VALUES (?, ?, ?)",
            (telegram_id, timezone, _utcnow_iso()),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id, telegram_id, role, timezone FROM users "
            "WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return {"id": row[0], "telegram_id": row[1], "role": row[2], "timezone": row[3]}

    def list_all(self, role: str | None = None) -> list[dict]:
        """Список пользователей (ТЗ §9.1), порядок по id.

        Read-only; запись отсюда не выполняется никогда. role — фильтр
        по users.role (например 'parent' — владельцы дневника, получатели
        пушей, design D19); None — все пользователи. Форма строк — та же,
        что get_or_create: dict с id, telegram_id, role, timezone.

        (Сведение техдолга пакета 2, п. 3.1: разовый read-only SQL
        owner_users в scheduler.py перенесён в репозиторий — слой
        services SQL не пишет, design D1.)
        """
        if role is None:
            rows = self._conn.execute(
                "SELECT id, telegram_id, role, timezone FROM users ORDER BY id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, telegram_id, role, timezone FROM users "
                "WHERE role = ? ORDER BY id",
                (role,),
            ).fetchall()
        return [
            {"id": r[0], "telegram_id": r[1], "role": r[2], "timezone": r[3]}
            for r in rows
        ]

    def get_timezone(self, telegram_id: int) -> str | None:
        """Прочитать часовой пояс пользователя (FR-4) или None."""
        row = self._conn.execute(
            "SELECT timezone FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return None if row is None else row[0]

    def set_timezone(self, telegram_id: int, timezone: str) -> bool:
        """Обновить часовой пояс пользователя (FR-4). True если найден."""
        cur = self._conn.execute(
            "UPDATE users SET timezone = ? WHERE telegram_id = ?",
            (timezone, telegram_id),
        )
        self._conn.commit()
        return cur.rowcount > 0
