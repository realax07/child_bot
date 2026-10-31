# Юнит-тесты кнопки «↩️ Отменить последнее» (change-пакет add-core-diary,
# задача 2.4).
#
# Критерии готовности 2.4 (FR-21; design D12, D5, D13):
#   - callback undo:last удаляет последнюю запись sleep_events; состояние
#     и меню пересчитываются (после отмены засыпания ребёнок снова
#     бодрствует, меню показывает «😴 Заснул»);
#   - отменяется только последняя отметка, более ранние события не
#     удаляются (FR-21);
#   - при пустой БД — сообщение «отменять нечего», данные не меняются;
#   - guard (design D5): undo валидна в обоих состояниях
#     (is_stale_callback с KIND_UNDO всегда False), пустые данные
#     отсекает delete_last;
#   - observer (design D13, FR-22): нажатие игнорируется, удаления нет.
#
# design D12: «последняя» — по created_at, затем id: задним числом
# введённое событие с ранним timestamp удаляется как последнее.
#
# Правило rule 6: каждый тест несёт TC-UNDO-NNN в первой строке докстринга.

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.db import init_db
from src.handlers import events, undo
from src.handlers.undo import (
    CALLBACK_UNDO_LAST,
    NOTHING_TO_UNDO,
    OBSERVER_DENIED,
    undo_last_callback,
)
from src.repositories.sleep_events import SleepEventsRepo
from src.repositories.users import UsersRepo
from src.services import state

# Фиксированное «сейчас» отмены (правило волны 6 — время фиксируется в
# фикстуре, ассерты без «живых» ЧЧ:ММ): 15:00 UTC = 18:00 МСК 26.10.
FIXED_NOW_UTC = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """datetime с замороженным now() — мини-состояние детерминировано.

    Подменяется и undo.datetime («сейчас» для страницы/окна мини-состояния),
    и events.datetime (расчётный момент сводки day_summary_lines), чтобы
    обе точки видели один и тот же момент.
    """

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        if tz is not None:
            return FIXED_NOW_UTC.astimezone(tz)
        return FIXED_NOW_UTC.replace(tzinfo=None)


@pytest.fixture()
def frozen_now(monkeypatch):
    """Фиксировать «сейчас» отмены и сводки (правило волны 6)."""
    monkeypatch.setattr(undo, "datetime", _FrozenDatetime)
    monkeypatch.setattr(events, "datetime", _FrozenDatetime)


# ------------------------------------------------------------ Заглушки PTB

class _FakeMessage:
    def __init__(self) -> None:
        self.text: str | None = None
        self.reply_markup = None
        self.replies: list[str] = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup


class _FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.answers: list[str | None] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        await self.message.edit_reply_markup(reply_markup=reply_markup)


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeUpdate:
    def __init__(self, user_id: int, data: str) -> None:
        self.effective_user = _FakeUser(user_id)
        self.callback_query = _FakeCallbackQuery(data)
        self.effective_message = self.callback_query.message


class _FakeContext:
    def __init__(self, db_path) -> None:
        self.bot_data = {"db_path": db_path}
        self.user_data: dict = {}


# --------------------------------------------------------------- Фикстуры

@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "bot.db"


@pytest.fixture()
def conn(db_path):
    c = init_db(db_path)
    yield c
    c.close()


def _press(db_path, data: str, telegram_id: int = 1001):
    """Провести нажатие «↩️ Отменить последнее» через реальный обработчик."""
    update = _FakeUpdate(telegram_id, data)
    context = _FakeContext(db_path)
    asyncio.run(undo_last_callback(update, context))  # type: ignore[arg-type]
    return update, context


def _uid(conn, telegram_id: int = 1001) -> int:
    """Внутренний users.id по telegram_id (FK в sleep_events — users.id)."""
    return UsersRepo(conn).get_or_create(telegram_id)["id"]


def _button_texts(markup) -> list[str]:
    if markup is None:
        return []
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _set_role(conn, user_id: int, role: str) -> None:
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()


# ------------------------------------------------------------------- Тесты

def test_undo_sleep_child_awake_again_record_deleted(db_path, conn, frozen_now):
    """TC-UNDO-001: после отмены засыпания запись sleep удалена, ребёнок
    снова считается бодрствующим, меню перерисовано в «😴 Заснул»,
    answerCallbackQuery + подтверждение с ЧТО удалено («Отменено: Заснул
    16:30» — локальное время записи, прочитанное ДО delete) и
    мини-состоянием: дневник опустел — «Сегодня записей пока нет» (FR-21
    в ред. волны 6, PB-4; design D12)."""
    user_id = _uid(conn)  # внутренний id пользователя telegram_id=1001
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-26 13:30:00")
    assert state.get_state(conn, user_id) == state.STATE_ASLEEP

    update, _ = _press(db_path, CALLBACK_UNDO_LAST)

    rows = SleepEventsRepo(conn).list_by_user(user_id)
    assert rows == []  # запись удалена
    assert state.get_state(conn, user_id) == state.STATE_AWAKE  # бодрствует

    texts_ = _button_texts(update.callback_query.message.reply_markup)
    assert state.BTN_SLEEP in texts_ and state.BTN_WAKE not in texts_
    assert state.BTN_UNDO in texts_  # «Отменить последнее» на месте
    assert update.callback_query.answers == [None]  # answerCallbackQuery
    # 13:30 UTC = 16:30 МСК; «сейчас» зафиксировано (15:00 UTC = 18:00 МСК)
    assert update.effective_message.replies == [
        "Отменено: Заснул 16:30.\n\nСегодня записей пока нет."
    ]


def test_undo_removes_only_last_event_earlier_kept(db_path, conn, frozen_now):
    """TC-UNDO-002: отменяется только последняя отметка — после undo wake
    удалён, более ранний sleep остался, ребёнок снова «спит» (FR-21:
    отмена NOT MUST удалять более ранние события). Подтверждение —
    «Отменено: Проснулся 10:30» (локальное время wake; оставшийся sleep
    04:00 МСК — на странице «вчера», поэтому мини-состояние за сегодня —
    «Сегодня записей пока нет»)."""
    user_id = _uid(conn)
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-26 01:00:00")
    SleepEventsRepo(conn).insert(user_id, "wake", "2026-10-26 07:30:00")

    update, _ = _press(db_path, CALLBACK_UNDO_LAST)

    rows = SleepEventsRepo(conn).list_by_user(user_id)
    assert [e["event_type"] for e in rows] == ["sleep"]  # wake удалён
    assert state.get_state(conn, user_id) == state.STATE_ASLEEP
    texts_ = _button_texts(update.callback_query.message.reply_markup)
    assert state.BTN_WAKE in texts_ and state.BTN_SLEEP not in texts_
    # 07:30 UTC = 10:30 МСК
    assert update.effective_message.replies == [
        "Отменено: Проснулся 10:30.\n\nСегодня записей пока нет."
    ]


def test_undo_on_empty_db_no_changes_nothing_message(db_path, conn):
    """TC-UNDO-003: отмена при пустой БД — данные не изменены, сообщение
    «отменять нечего», меню (актуальное и так) перерисовано (FR-21,
    design D12)."""
    user_id = _uid(conn)
    assert SleepEventsRepo(conn).list_by_user(user_id) == []

    update, _ = _press(db_path, CALLBACK_UNDO_LAST)

    assert SleepEventsRepo(conn).list_by_user(user_id) == []  # данных нет изменений
    assert update.effective_message.replies == [NOTHING_TO_UNDO]
    assert update.callback_query.answers == [None]
    texts_ = _button_texts(update.callback_query.message.reply_markup)
    assert state.BTN_SLEEP in texts_  # меню в актуальном состоянии


def test_undo_after_backfill_removes_latest_by_created_at(
    db_path, conn, frozen_now
):
    """TC-UNDO-004: design D12 — «последняя» запись по created_at, затем id:
    задним числом введённый sleep (ранний timestamp, поздний created_at)
    удаляется, несмотря на более поздний timestamp «обычной» записи.
    Подтверждение — «Отменено: Заснул 14:30» + мини-состояние за сегодня:
    осталась 1 запись (wake 15:00 МСК), факты дня PB-3 (без пары —
    только «Бодрствование общее»)."""
    user_id = _uid(conn)
    # created_at фиксирован у обеих вставок: «последнее» по created_at
    # не зависит от реальных часов (та же детерминированность, что в
    # test_state.py). backfilled-sleep задним числом — его created_at
    # позже созданного «обычно» wake.
    SleepEventsRepo(
        conn,
    ).insert(
        user_id, "wake", "2026-10-26 12:00:00", created_at="2026-10-26 12:30:00"
    )
    SleepEventsRepo(
        conn,
    ).insert(
        user_id,
        "sleep",
        "2026-10-26 11:30:00",
        is_backfilled=True,
        created_at="2026-10-26 15:00:00",
    )
    assert state.get_state(conn, user_id) == state.STATE_ASLEEP

    update, _ = _press(db_path, CALLBACK_UNDO_LAST)

    rows = SleepEventsRepo(conn).list_by_user(user_id)
    assert [e["event_type"] for e in rows] == ["wake"]  # backfilled-sleep удалён
    assert state.get_state(conn, user_id) == state.STATE_AWAKE
    # 11:30 UTC = 14:30 МСК; остался wake 12:00 UTC = 15:00 МСК
    assert update.effective_message.replies == [
        "Отменено: Заснул 14:30.\n\n"
        "Записей за сегодня: 1.\n"
        "Бодрствование общее: 24 ч"
    ]


def test_observer_undo_politely_denied_no_deletion(db_path, conn):
    """TC-UNDO-005: observer жмёт «↩️ Отменить последнее» — вежливый отказ
    (design D13, FR-22), записи НЕ удаляются, меню не трогается."""
    user_id = _uid(conn)
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-26 13:30:00")
    _set_role(conn, user_id, "observer")

    update, _ = _press(db_path, CALLBACK_UNDO_LAST)

    rows = SleepEventsRepo(conn).list_by_user(user_id)
    assert len(rows) == 1  # удаления нет
    assert state.get_state(conn, user_id) == state.STATE_ASLEEP
    assert update.callback_query.answers == [OBSERVER_DENIED]
    assert update.effective_message.replies == []  # без подтверждений/отказов


def test_undo_guard_valid_in_both_states_empty_cutoff(
    db_path, conn, frozen_now
):
    """TC-UNDO-006: guard (design D5): «undo» валидна в обоих состояниях —
    is_stale_callback(KIND_UNDO) всегда False (awake и asleep); при этом
    пустые данные отсекаются обработчиком сообщением «отменять нечего»
    (TC-UNDO-003), а нажатие undo при обоих состояниях удаляет запись
    (подтверждение PB-4 — как в TC-UNDO-001)."""
    user_id = _uid(conn)
    # Бодрствует: undo валидна
    assert (
        state.is_stale_callback(conn, user_id, state.KIND_UNDO) is False
    )
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-26 13:30:00")
    # Спит: undo тоже валидна (перечитано из БД)
    assert (
        state.is_stale_callback(conn, user_id, state.KIND_UNDO) is False
    )

    update_asleep, _ = _press(db_path, CALLBACK_UNDO_LAST)
    assert update_asleep.effective_message.replies == [
        "Отменено: Заснул 16:30.\n\nСегодня записей пока нет."
    ]
    assert SleepEventsRepo(conn).list_by_user(user_id) == []

    # После удаления всех записей повторное undo — «отменять нечего».
    update_empty, _ = _press(db_path, CALLBACK_UNDO_LAST)
    assert update_empty.effective_message.replies == [NOTHING_TO_UNDO]


def test_undo_registered_handler_pattern_routed(db_path, conn):
    """TC-UNDO-007: обработчик зарегистрирован в register_handlers на
    pattern ^undo:last$ (согласованная точка регистрации задачи 2.4)."""
    from unittest.mock import MagicMock

    from src.handlers import register_handlers

    app = MagicMock()
    register_handlers(app)
    undo_calls = [
        c
        for c in app.add_handler.call_args_list
        if any("undo" in str(a) for a in c.args + tuple(c.kwargs.values()))
    ]
    assert undo_calls, "undo-обработчик не зарегистрирован"
    handler = undo_calls[0].args[0]
    assert handler.pattern.pattern == r"^undo:last$"
    assert handler.callback is undo_last_callback
