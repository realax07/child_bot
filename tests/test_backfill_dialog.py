# Юнит-тесты диалога ввода задним числом, фаза WAIT_EVENT_TIME
# (change-пакет add-core-diary, задача 4.1).
#
# Критерии готовности 4.1 (FR-15; design D10):
#   - после «⏪ Ввести время вручную» приходит §7.11 и фаза WAIT_EVENT_TIME
#     сохранена в context.user_data (вместе с видом события — задача 4.4);
#   - текст вне диалога не вызывает никакой реакции (не мешает);
#   - текст в фазе доходит до обработчика: фаза сохранена, ответ пользователю
#     не отправляется (парсер — задача 4.2, заглушка без вывода);
#   - «🕐 Сейчас» снимает фазу (диалог завершён).
#
# Правило rule 6: каждый тест несёт TC-BFD-NNN в первой строке докстринга.

from __future__ import annotations

import asyncio

import pytest

from src.db import init_db
from src.handlers import backfill, events
from src.handlers.events import CALLBACK_TIME_MANUAL, CALLBACK_TIME_NOW
from src import texts

# ------------------------------------------------------------ Заглушки PTB
# (те же контракты, что в tests/test_events_now.py — задача 2.3)


class _FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.reply_markup = None
        self.replies: list[str] = []

    async def reply_text(self, reply, reply_markup=None):
        self.replies.append(reply)


class _FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.answers: list[str | None] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text=None, reply_markup=None):
        self.message.text = text

    async def edit_message_reply_markup(self, reply_markup=None):
        self.message.reply_markup = reply_markup


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeCallbackUpdate:
    def __init__(self, user_id: int, data: str) -> None:
        self.effective_user = _FakeUser(user_id)
        self.callback_query = _FakeCallbackQuery(data)
        self.effective_message = self.callback_query.message


class _FakeTextUpdate:
    def __init__(self, user_id: int, text: str) -> None:
        self.effective_user = _FakeUser(user_id)
        self.callback_query = None
        self.message = _FakeMessage(text)
        self.effective_message = self.message


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


def _press_callback(
    db_path, data: str, telegram_id: int = 2001, user_data: dict | None = None
):
    """Провести нажатие кнопки через реальный обработчик с фейковым PTB.

    user_data можно передать, чтобы фазы диалога (design D10) жили между
    нажатиями — как в реальном PTB в рамках одного пользователя.
    """
    update = _FakeCallbackUpdate(telegram_id, data)
    context = _FakeContext(db_path)
    if user_data is not None:
        context.user_data = user_data
    handler = (
        events.event_button_callback
        if data.startswith("event:")
        else events.time_choose_callback
    )
    asyncio.run(handler(update, context))  # type: ignore[arg-type]
    return update, context


def _send_text(db_path, text: str, user_data: dict, telegram_id: int = 2001):
    """Провести текстовое сообщение через реальный обработчик backfill."""
    update = _FakeTextUpdate(telegram_id, text)
    context = _FakeContext(db_path)
    context.user_data = user_data
    asyncio.run(backfill.backfill_time_message(update, context))  # type: ignore[arg-type]
    return update, context


# ------------------------------------------------------------------ Тесты


def test_tc_bfd_001_manual_shows_7_11_and_sets_phase(db_path, conn):
    """TC-BFD-001: time:manual → §7.11 и фаза WAIT_EVENT_TIME сохранена."""
    user_data: dict = {}
    _press_callback(db_path, "event:sleep", telegram_id=2001, user_data=user_data)
    update, context = _press_callback(
        db_path, CALLBACK_TIME_MANUAL, telegram_id=2001, user_data=user_data
    )

    # §7.11 отправлен пользователю дословно.
    assert update.effective_message.replies == [texts.BACKFILL_ASK_TIME]
    # Фаза диалога в context.user_data (design D10).
    assert context.user_data.get(events.WAIT_EVENT_TIME_KEY) is True
    # Вид события сохранён для последующих задач 4.2–4.4.
    assert context.user_data.get(events.PENDING_KIND_KEY) is not None


def test_tc_bfd_002_text_outside_dialog_ignored(db_path, conn):
    """TC-BFD-002: текст вне диалога не вызывает никакой реакции."""
    user_data: dict = {}
    update, context = _send_text(db_path, "10:50", user_data)

    # Ни ответа, ни изменения состояния диалога.
    assert update.effective_message.replies == []
    assert events.WAIT_EVENT_TIME_KEY not in context.user_data
    assert events.PENDING_KIND_KEY not in context.user_data


def test_tc_bfd_003_text_in_phase_reaches_handler(db_path, conn):
    """TC-BFD-003: текст в фазе доходит до обработчика, фаза сохранена."""
    user_data = {
        events.WAIT_EVENT_TIME_KEY: True,
        events.PENDING_KIND_KEY: "sleep",
    }
    update, context = _send_text(db_path, "вчера 22:30", user_data)

    # Текст дошёл до заглушки: ответ пользователю НЕ отправляется
    # (парсер — задача 4.2), фаза осталась активной.
    assert update.effective_message.replies == []
    assert context.user_data.get(events.WAIT_EVENT_TIME_KEY) is True
    assert context.user_data.get(events.PENDING_KIND_KEY) == "sleep"


def test_tc_bfd_004_time_now_clears_phase(db_path, conn):
    """TC-BFD-004: «🕐 Сейчас» завершает диалог — фаза снята."""
    user_data: dict = {}
    _press_callback(db_path, "event:sleep", telegram_id=2002, user_data=user_data)
    _press_callback(
        db_path, CALLBACK_TIME_MANUAL, telegram_id=2002, user_data=user_data
    )
    _, context = _press_callback(
        db_path, CALLBACK_TIME_NOW, telegram_id=2002, user_data=user_data
    )

    assert events.WAIT_EVENT_TIME_KEY not in context.user_data
    assert events.PENDING_KIND_KEY not in context.user_data
