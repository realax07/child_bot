# Сервис событий сна (change-пакет add-core-diary, задача 2.3).
#
# design D5: перед записью состояние ребёнка перепроверяется по БД (выводится
# из последнего события, не хранится) — событие, противоречащее состоянию,
# не создаётся.
# design D6: при записи пишутся ТОЛЬКО сырые поля (event_type, timestamp,
# created_at, is_backfilled); sleep_type и is_merged — NULL (резерв схемы,
# разметка/склейка вычисляются на лету при чтении).
# design D4: timestamp хранится в UTC (строка «YYYY-MM-DD HH:MM:SS»).
#
# Гарантии create_event (FR-7, FR-8):
#   1) состояние соответствует кнопке (sleep — из «бодрствует», wake — из «спит»);
#   2) нет незакрытого такого же события (повторный sleep при спящем /
#      повторный wake при бодрствующем отвергается).
# Нарушение — ValueError; слой handlers переводит это в guard-поведение
# (answerCallbackQuery + перерисовка меню, design D5).
#
# Вызывается только слоем handlers (design D1, FR-1); репозитории — только
# отсюда.

from __future__ import annotations

import sqlite3

from src.repositories.sleep_events import SleepEventsRepo
from src.repositories.users import UsersRepo
from src.services import state
from src.services.state import KIND_SLEEP, KIND_WAKE

# Виды событий, создаваемые событийными кнопками (ТЗ §9.2 CHECK).
VALID_KINDS = (KIND_SLEEP, KIND_WAKE)

# Роли пользователей (ТЗ §9.1, решение 26.10 №5); observer — см. D13.
ROLE_PARENT = "parent"
ROLE_OBSERVER = "observer"


def get_user(conn: sqlite3.Connection, telegram_id: int) -> dict:
    """Пользователь по telegram_id: {id, telegram_id, role, timezone}.

    Создаётся при первом обращении (идемпотентно, как в /start — FR-5).
    Временный мост для обработчиков событий до выделения
    src/services/access.py (задача 5.1, design D13): handlers обращаются
    к сервису, не к репозиторию (design D1).
    """
    return UsersRepo(conn).get_or_create(telegram_id)


def create_event(
    conn: sqlite3.Connection,
    user_id: int,
    kind: str,
    when_utc: str,
) -> dict:
    """Создать событие sleep/wake с сырыми полями (FR-7, FR-8, design D6).

    when_utc — момент события строкой UTC «YYYY-MM-DD HH:MM:SS» (design D4).
    Проверки перед записью (design D5):
      - kind допустим (sleep/wake);
      - состояние ребёнка соответствует (перечитано из БД);
      - нет незакрытого такого же события (последняя запись не того же вида).
    Возвращает созданную запись (dict из SleepEventsRepo-полей).
    ValueError — любая проверка не пройдена: событие НЕ создаётся.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"недопустимый вид события: {kind!r}")

    # Состояние всегда перечитывается из БД (design D5, без кэша).
    current_state = state.get_state(conn, user_id)
    expected = state.STATE_AWAKE if kind == KIND_SLEEP else state.STATE_ASLEEP
    if current_state != expected:
        raise ValueError(
            f"состояние {current_state!r} не соответствует кнопке {kind!r}"
        )

    # Незакрытое событие того же вида (последнее — по created_at, затем id,
    # design D12): повторное sleep при спящем / wake при бодрствующем.
    last = SleepEventsRepo(conn).last(user_id)
    if last is not None and last["event_type"] == kind:
        raise ValueError(f"незакрытое событие {kind!r} уже существует")

    event_id = SleepEventsRepo(conn).insert(
        user_id, kind, when_utc, is_backfilled=False
    )
    return {
        "id": event_id,
        "user_id": user_id,
        "event_type": kind,
        "timestamp": when_utc,
        "is_backfilled": False,
        "sleep_type": None,  # design D6: разметка — расчёт при чтении
        "is_merged": None,   # design D9: склейка — расчёт при чтении
    }
