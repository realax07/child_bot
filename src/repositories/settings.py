# Репозиторий settings + settings_history (ТЗ §9.3–9.4).
#
# settings — change-пакет add-core-diary, задача 1.3: настройки пользователя
# по ключам, seed 13 дефолтов делает сервис /start (задача 2.1); повторная
# инициализация идемпотентна за счёт upsert. Значения хранятся как TEXT;
# сервисы читают при каждом расчёте (FR-24), без кэша и без агрегатов.
#
# settings_history — change-пакет add-settings-history, задача 1.1
# (ТЗ §9.4, design D21, FR-46): журнал изменений настроек (user_id, key,
# old_value, new_value, changed_at). INSERT выполняется БЕЗ собственного
# commit: запись истории и обновление settings обязаны попасть в одну
# транзакцию (§8.3 — «частичные изменения запрещены»); commit делает
# сервис, композирирующий оба репозитория. changed_at — UTC-строка
# (design D4).
#
# Вызывается только слоем services (design D1, FR-1).

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _utcnow_iso() -> str:
    """Текущий момент в UTC (ISO-строка) — design D4."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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

    def stage_upsert(self, user_id: int, key: str, value: str | None) -> None:
        """upsert БЕЗ commit — для одной транзакции с settings_history.

        Задача 1.1 (FR-46): обновление settings и INSERT в историю
        обязаны коммититься одной операцией; транзакцию открывает и
        завершает вызывающий сервис.
        """
        self._conn.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, key, value),
        )

    def upsert(self, user_id: int, key: str, value: str | None) -> None:
        """Создать или обновить настройку; дублей (user_id, key) нет — UNIQUE в схеме."""
        self.stage_upsert(user_id, key, value)
        self._conn.commit()


class SettingsHistoryRepo:
    """INSERT/read-доступ к settings_history (ТЗ §9.4, FR-46)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, user_id: int, key: str, old_value: str | None, new_value: str | None) -> str:
        """Добавить запись истории; changed_at — текущий UTC-момент.

        Без commit (см. шапку модуля): одна транзакция с обновлением
        settings — обязанность сервиса. Возвращает changed_at.
        """
        changed_at = _utcnow_iso()
        self._conn.execute(
            "INSERT INTO settings_history "
            "(user_id, key, old_value, new_value, changed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, key, old_value, new_value, changed_at),
        )
        return changed_at

    def list_for_user(self, user_id: int, limit: int = 100) -> list[dict]:
        """История пользователя, новые записи первыми (§5.1)."""
        rows = self._conn.execute(
            "SELECT id, user_id, key, old_value, new_value, changed_at "
            "FROM settings_history WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [
            {
                "id": r[0],
                "user_id": r[1],
                "key": r[2],
                "old_value": r[3],
                "new_value": r[4],
                "changed_at": r[5],
            }
            for r in rows
        ]
