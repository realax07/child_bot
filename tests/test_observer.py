# Тесты роли «наблюдатель» (change-пакет add-core-diary, задача 5.1).
#
# design D13, FR-22 (решение 26.10 №5): observer жмёт событийные кнопки
# («😴 Заснул», «🕐 Сейчас») и «↩️ Отменить последнее» → вежливый отказ
# (единый текст texts.OBSERVER_DENIED через services.access.deny_message),
# событие не создаётся / запись не удаляется. parent — отметки работают.
# UI смены роли НЕ делается: роль меняется данными (UPDATE users.role),
# проверки (services.access.is_observer) читают её из БД при каждом вызове
# и реагируют без перезапуска.

from __future__ import annotations

import asyncio

import pytest

from src import texts
from src.db import init_db
from src.handlers import events, undo
from src.handlers.events import CALLBACK_TIME_NOW
from src.handlers.undo import CALLBACK_UNDO_LAST
from src.repositories.sleep_events import SleepEventsRepo
from src.repositories.users import UsersRepo
from src.services import state
from src.services import access

# ------------------------------------------------------------- Заглушки PTB


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

    async def edit_message_text(self, text=None, reply_markup=None):
        self.message.text = text
        self.message.reply_markup = reply_markup

    async def edit_message_reply_markup(self, reply_markup=None):
        await self.message.edit_reply_markup(reply_markup=reply_markup)


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeUpdate:
    def __init__(self, user_id: int, data: str) -> None:
        self.effective_user = _FakeUser(user_id)
        self.callback_query = _FakeCallbackQuery(data)
        # Для callback-запросов effective_message — сообщение с кнопками.
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


DENIED = texts.OBSERVER_DENIED


def _press(db_path, data: str, telegram_id: int = 1001, pending: str | None = None):
    """Провести нажатие кнопки через реальный обработчик с фейковым PTB."""
    update = _FakeUpdate(telegram_id, data)
    context = _FakeContext(db_path)
    if pending is not None:
        context.user_data[events.PENDING_KIND_KEY] = pending
    if data == CALLBACK_UNDO_LAST:
        handler = undo.undo_last_callback
    elif data.startswith("event:"):
        handler = events.event_button_callback
    else:
        handler = events.time_choose_callback
    asyncio.run(handler(update, context))  # type: ignore[arg-type]
    return update, context


def _uid(conn, telegram_id: int = 1001) -> int:
    """Внутренний users.id по telegram_id (FK в sleep_events — users.id)."""
    return UsersRepo(conn).get_or_create(telegram_id)["id"]


def _set_role(conn, user_id: int, role: str) -> None:
    """Смена роли данными (design D13: UI смены роли не делается)."""
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()


def _sleep_count(conn, user_id: int) -> int:
    return len(SleepEventsRepo(conn).list_by_user(user_id))


# ------------------------------------------------------------------- Тесты


def test_access_service_single_point_of_role_checks(conn):
    """TC-OBS-001: services.access — единственная точка проверки роли:
    get_role читает users.role по users.id, is_observer различает роли,
    deny_message возвращает единый текст texts.OBSERVER_DENIED (D13,
    review-001-2.4 note 1)."""
    user_id = _uid(conn)
    assert access.get_role(conn, user_id) == "parent"  # дефолт схемы (1.2)
    assert access.is_observer(conn, user_id) is False

    _set_role(conn, user_id, "observer")
    assert access.get_role(conn, user_id) == "observer"
    assert access.is_observer(conn, user_id) is True

    assert access.deny_message() == texts.OBSERVER_DENIED
    # Единый текст: алиасы в handlers не дублируют литерал.
    assert events.OBSERVER_DENIED is texts.OBSERVER_DENIED
    assert undo.OBSERVER_DENIED is texts.OBSERVER_DENIED


def test_observer_press_sleep_no_record_with_message(db_path, conn):
    """TC-OBS-002: observer жмёт «😴 Заснул» — записи НЕТ, есть вежливое
    сообщение (единый текст отказа), §7.10 не открывается (FR-22, D13)."""
    user_id = _uid(conn)
    _set_role(conn, user_id, "observer")

    update, context = _press(db_path, "event:sleep")

    assert _sleep_count(conn, user_id) == 0  # записи нет
    assert update.callback_query.answers == [DENIED]  # сообщение есть
    assert update.effective_message.replies == []  # меню не переключалось
    assert context.user_data.get(events.PENDING_KIND_KEY) is None
    assert update.callback_query.message.text != "Когда это произошло?"


def test_parent_press_sleep_creates_record(db_path, conn):
    """TC-OBS-003: parent (роль по умолчанию) жмёт «😴 Заснул» → «🕐 Сейчас» —
    запись есть, ответ §7.5, меню переключилось (поведение без изменений,
    FR-7)."""
    user_id = _uid(conn)
    assert access.get_role(conn, user_id) == "parent"

    _press(db_path, "event:sleep")
    update, _ = _press(db_path, CALLBACK_TIME_NOW, pending="sleep")

    assert _sleep_count(conn, user_id) == 1  # запись есть
    row = SleepEventsRepo(conn).list_by_user(user_id)[0]
    assert row["event_type"] == "sleep"
    assert state.get_state(conn, user_id) == state.STATE_ASLEEP
    assert update.effective_message.replies[0].startswith(
        "✅ Засыпание записано в "
    )
    texts_ = [
        btn.text
        for row_ in update.callback_query.message.reply_markup.inline_keyboard
        for btn in row_
    ]
    assert state.BTN_WAKE in texts_  # меню переключилось


def test_observer_press_undo_denied_no_deletion(db_path, conn):
    """TC-OBS-004: observer жмёт «↩️ Отменить последнее» — отказ без
    удаления: запись осталась, состояние не изменилось (FR-22, D13)."""
    user_id = _uid(conn)
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-26 13:30:00")
    _set_role(conn, user_id, "observer")

    update, _ = _press(db_path, CALLBACK_UNDO_LAST)

    assert _sleep_count(conn, user_id) == 1  # удаления нет
    assert state.get_state(conn, user_id) == state.STATE_ASLEEP
    assert update.callback_query.answers == [DENIED]
    assert update.effective_message.replies == []


def test_observer_on_time_now_denied_no_record(db_path, conn):
    """TC-OBS-005: observer на «🕐 Сейчас» (time:now) — отказ, событие не
    создаётся даже при уже открытой фазе диалога (FR-22, D13)."""
    user_id = _uid(conn)
    _set_role(conn, user_id, "observer")

    update, context = _press(
        db_path, CALLBACK_TIME_NOW, pending="sleep"
    )

    assert _sleep_count(conn, user_id) == 0  # записи нет
    assert update.callback_query.answers == [DENIED]
    assert context.user_data.get(events.PENDING_KIND_KEY) is None  # фаза снята


def test_role_changed_by_data_reacts_without_restart(db_path, conn):
    """TC-OBS-006: роль меняется данными (UPDATE users.role) — проверки
    реагируют без перезапуска: parent → observer (отметки закрылись) и
    обратно (отметки открылись); is_observer читает БД при каждом вызове
    (design D13, критерий 5.1)."""
    user_id = _uid(conn)

    # parent: «😴 Заснул» → «🕐 Сейчас» работает
    _press(db_path, "event:sleep")
    update, _ = _press(db_path, CALLBACK_TIME_NOW, pending="sleep")
    assert _sleep_count(conn, user_id) == 1

    # Роль сменили данными: те же кнопки сразу отказывают
    _set_role(conn, user_id, "observer")
    update, _ = _press(db_path, "event:sleep")
    assert update.callback_query.answers == [DENIED]
    update, _ = _press(db_path, CALLBACK_UNDO_LAST)
    assert update.callback_query.answers == [DENIED]
    assert _sleep_count(conn, user_id) == 1  # запись не удалена

    # Роль вернули данными: отметки снова работают
    _set_role(conn, user_id, "parent")
    _press(db_path, "event:wake")
    update, _ = _press(db_path, CALLBACK_TIME_NOW, pending="wake")
    assert _sleep_count(conn, user_id) == 2  # wake записан
    assert update.callback_query.answers == [None]  # без отказа
