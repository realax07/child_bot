# Сервис доступа по роли (change-пакет add-core-diary, задача 5.1).
#
# design D13 (FR-22, решение 26.10 №5): ЕДИНСТВЕННАЯ точка проверки роли
# users.role в обработчиках событийных кнопок («😴 Заснул» / «☀️ Проснулся»,
# «🕐 Сейчас») и отмены («↩️ Отменить последнее»). observer → вежливый отказ,
# событие не создаётся / запись не удаляется; просмотр сводок (пакеты 2–3) —
# без ограничений.
#
# UI смены роли НЕ делается: роль меняется данными (UPDATE users.role) —
# is_observer читает её из БД при каждом вызове, никаких кэшей (D14).
# Связь observer → владелец данных (linkage) — additive-миграция пакета 2/3,
# здесь НЕ создаётся (design D13): хранится только role.
#
# Текст отказа — единый, из texts.OBSERVER_DENIED (review-001-2.4, note 1:
# дубль литерала OBSERVER_DENIED в handlers/events.py и handlers/undo.py
# убран; там оставлены алиасы только для обратной совместимости импортов).
#
# Слой services (design D1): repositories и SQL — только здесь; handlers
# зовут is_observer / deny_message, не репозиторий и не SQL.

from __future__ import annotations

import sqlite3

from src import texts
from src.repositories.users import UsersRepo

# Роли пользователей (ТЗ §9.1, решение 26.10 №5).
ROLE_PARENT = "parent"
ROLE_OBSERVER = "observer"


def get_user(conn: sqlite3.Connection, telegram_id: int) -> dict:
    """Пользователь по telegram_id: {id, telegram_id, role, timezone}.

    Создаётся при первом обращении (идемпотентно, FR-5) — единая обёртка
    над UsersRepo для обработчиков (design D1: handlers → services).
    """
    return UsersRepo(conn).get_or_create(telegram_id)


def get_role(conn: sqlite3.Connection, user_id: int) -> str:
    """Роль пользователя по внутреннему users.id.

    Свежее чтение из БД (роль меняется данными — design D13). Неизвестный
    id трактуется как 'parent' — безопасный дефолт схемы users (NOT NULL
    DEFAULT 'parent', задача 1.2).
    """
    row = conn.execute(
        "SELECT role FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return row[0] if row is not None else ROLE_PARENT


def is_observer(conn: sqlite3.Connection, user_id: int) -> bool:
    """True, если users.role = 'observer' (FR-22, design D13).

    Роль перечитывается из БД при каждом вызове — смена роли данными
    реагирует без перезапуска (критерий задачи 5.1).
    """
    return get_role(conn, user_id) == ROLE_OBSERVER


def deny_message() -> str:
    """Единый текст отказа наблюдателю (texts.OBSERVER_DENIED, D13)."""
    return texts.OBSERVER_DENIED
