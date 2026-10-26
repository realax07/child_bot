# Репозиторий settings (ТЗ §9.3; change-пакет add-core-diary, задача 1.3).
#
# design D14: настройки пользователя в settings по ключам, seed 13 дефолтов
# делает сервис /start (задача 2.1); повторная инициализация идемпотентна
# за счёт upsert. Значения хранятся как TEXT; сервисы читают при каждом
# расчёте (FR-24), без кэша и без агрегатов.
#
# Вызывается только слоем services (design D1, FR-1).

from __future__ import annotations

import sqlite3


class SettingsRepo:
    """CRUD-доступ к таблице settings (ТЗ §9.3)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_all(self, user_id: int) -> dict[str, str | None]:
        """Все настройки пользователя одним dict: key -> value."""
        rows = self._conn.execute(
            "SELECT key, value FROM settings WHERE user_id = ? ORDER BY key",
            (user_id,),
        ).fetchall()
        return {key: value for key, value in rows}

    def get(self, user_id: int, key: str) -> str | None:
        """Значение одной настройки или None, если ключа нет."""
        row = self._conn.execute(
            "SELECT value FROM settings WHERE user_id = ? AND key = ?",
            (user_id, key),
        ).fetchone()
        return None if row is None else row[0]

    def upsert(self, user_id: int, key: str, value: str | None) -> None:
        """Создать или обновить настройку; дублей (user_id, key) нет — UNIQUE в схеме."""
        self._conn.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, key, value),
        )
        self._conn.commit()
