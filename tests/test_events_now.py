# Юнит-тесты событийных кнопок и «🕐 Сейчас» (change-пакет add-core-diary,
# задача 2.3).
#
# Критерии готовности 2.3 (FR-7, FR-8, FR-15; design D2, D5, D6):
#   - event:sleep/event:wake → меню §7.10 (time:now / time:manual);
#   - time:now создаёт событие с фактическим временем, is_backfilled=0,
#     меню переключается, ответ §7.5 (с «Крайнее пробуждение» из настройки
#     wake_deadline) / §7.2 (без строки окна — Пакет 2);
#   - guard устаревшей кнопки (design D5): записи нет, answerCallbackQuery,
#     клавиатура перерисована в актуальное состояние (F-3 review-001);
#   - observer (design D13): нажатие игнорируется с вежливым сообщением;
#   - повторный перевод состояния через меню корректен (sleep→wake→sleep).
#
# Правило rule 6: каждый тест несёт TC-EVNOW-NNN в первой строке докстринга.

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.db import init_db
from src.handlers import events
from src.handlers.events import (
    CALLBACK_TIME_MANUAL,
    CALLBACK_TIME_NOW,
    OBSERVER_DENIED,
    fell_asleep_text,
    woke_up_text,
)
from src.repositories.settings import SettingsRepo
from src.repositories.sleep_events import SleepEventsRepo
from src.repositories.users import UsersRepo
from src.services import state
from src.services.event import create_event
from src.services.settings import SettingsService


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


def _press(db_path, data: str, telegram_id: int = 1001, pending: str | None = None):
    """Провести нажатие кнопки через реальный обработчик с фейковым PTB."""
    update = _FakeUpdate(telegram_id, data)
    context = _FakeContext(db_path)
    if pending is not None:
        context.user_data[events.PENDING_KIND_KEY] = pending
    handler = (
        events.event_button_callback
        if data.startswith("event:")
        else events.time_choose_callback
    )
    asyncio.run(handler(update, context))  # type: ignore[arg-type]
    return update, context


def _uid(conn, telegram_id: int = 1001) -> int:
    """Внутренний users.id по telegram_id (FK в sleep_events — users.id)."""
    return UsersRepo(conn).get_or_create(telegram_id)["id"]


def _button_texts(markup) -> list[str]:
    if markup is None:
        return []
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _sleep_count(conn, user_id: int) -> int:
    return len(
        [e for e in SleepEventsRepo(conn).list_by_user(user_id)]
    )


# ------------------------------------------------------------------- Тесты

def test_event_button_opens_time_choose_menu_7_10(db_path, conn):
    """TC-EVNOW-001: нажатие «😴 Заснул» при бодрствующем открывает §7.10
    («Когда это произошло?» с «🕐 Сейчас»/time:now и «⏪ Ввести время
    вручную»/time:manual), событие ещё НЕ создаётся (FR-7, FR-15, D2)."""
    user_id = _uid(conn)  # внутренний id пользователя telegram_id=1001
    update, _ = _press(db_path, "event:sleep")

    assert update.callback_query.message.text == "Когда это произошло?"
    markup = update.callback_query.message.reply_markup
    assert markup is not None
    texts_ = _button_texts(markup)
    datas = [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
    ]
    assert "🕐 Сейчас" in texts_ and "⏪ Ввести время вручную" in texts_
    assert CALLBACK_TIME_NOW in datas and CALLBACK_TIME_MANUAL in datas
    assert _sleep_count(conn, user_id) == 0  # запись появится только после «Сейчас»


def test_time_now_creates_sleep_event_and_answers_7_5(db_path, conn):
    """TC-EVNOW-002: «😴 Заснул» → «🕐 Сейчас» создаёт sleep-событие с
    фактическим временем (UTC, is_backfilled=0), меню переключается на
    «☀️ Проснулся», ответ — §7.5 с фактическим временем и «Крайнее
    пробуждение: 15:30» из настройки wake_deadline; строки окна нет
    (FR-7, design D6)."""
    user_id = _uid(conn)  # внутренний id пользователя telegram_id=1001
    _press(db_path, "event:sleep")  # открытие §7.10
    update, _ = _press(
        db_path, CALLBACK_TIME_NOW, pending="sleep"
    )

    rows = SleepEventsRepo(conn).list_by_user(user_id)
    assert len(rows) == 1
    event = rows[0]
    assert event["event_type"] == "sleep"
    assert event["is_backfilled"] is False  # is_backfilled=0
    assert event["sleep_type"] is None and event["is_merged"] is None  # D6

    created = datetime.strptime(event["timestamp"], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    assert abs((datetime.now(timezone.utc) - created).total_seconds()) < 300

    # Меню переключилось (redraw по свежему состоянию, FR-6)
    texts_ = _button_texts(update.callback_query.message.reply_markup)
    assert state.BTN_WAKE in texts_ and state.BTN_SLEEP not in texts_

    # Ответ §7.5 с фактическим временем; окно не выводится
    assert len(update.effective_message.replies) == 1
    answer = update.effective_message.replies[0]
    assert answer == fell_asleep_text(
        answer.split("в ")[1].split(".")[0], "15:30"
    )
    assert "✅ Засыпание записано в" in answer
    assert "Крайнее пробуждение: 15:30" in answer
    assert "Окно" not in answer


def test_time_now_creates_wake_event_and_answers_7_2_without_window(db_path, conn):
    """TC-EVNOW-003: «☀️ Проснулся» → «🕐 Сейчас» создаёт wake-событие,
    меню переключается на «😴 Заснул», ответ — только подтверждающая строка
    §7.2 («✅ Пробуждение записано в ЧЧ:ММ.») БЕЗ строки окна дневного сна
    (FR-8, окна — Пакет 2)."""
    user_id = _uid(conn)  # внутренний id пользователя telegram_id=1001
    # Сон, начатый час назад — точно в прошлом относительно «сейчас»
    past = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    ).strftime("%Y-%m-%d %H:%M:%S")
    SleepEventsRepo(conn).insert(user_id, "sleep", past)
    _press(db_path, "event:wake")
    update, _ = _press(
        db_path, CALLBACK_TIME_NOW, pending="wake"
    )

    rows = SleepEventsRepo(conn).list_by_user(user_id)
    assert [e["event_type"] for e in rows] == ["sleep", "wake"]
    assert rows[1]["is_backfilled"] is False
    assert rows[1]["sleep_type"] is None and rows[1]["is_merged"] is None

    texts_ = _button_texts(update.callback_query.message.reply_markup)
    assert state.BTN_SLEEP in texts_ and state.BTN_WAKE not in texts_

    assert len(update.effective_message.replies) == 1
    answer = update.effective_message.replies[0]
    assert answer == woke_up_text(answer.split("в ")[1].split(".")[0])
    assert answer.startswith("✅ Пробуждение записано в ")
    assert answer.rstrip().endswith(".")
    assert "Окно" not in answer
    assert "\n" not in answer  # одна строка — без окна и без «Крайнего»


def test_stale_event_button_guard_blocks_creation(db_path, conn):
    """TC-EVNOW-004: guard устаревшей кнопки (design D5, F-3 review-001):
    ребёнок уже спит, нажатие «😴 Заснул» из старого сообщения — события
    НЕТ, answerCallbackQuery выполнен, клавиатура сообщения перерисована
    в актуальную («☀️ Проснулся»)."""
    user_id = _uid(conn)  # внутренний id пользователя telegram_id=1001
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-26 13:30:00")

    update, context = _press(db_path, "event:sleep")

    assert _sleep_count(conn, user_id) == 1  # новое событие не создано
    assert update.callback_query.answers == [None]  # answerCallbackQuery
    assert context.user_data.get(events.PENDING_KIND_KEY) is None
    texts_ = _button_texts(update.callback_query.message.reply_markup)
    assert state.BTN_WAKE in texts_ and state.BTN_SLEEP not in texts_


def test_stale_time_now_guard_blocks_creation(db_path, conn):
    """TC-EVNOW-005: guard и на шаге «🕐 Сейчас» (design D5): пока было
    открыто §7.10, состояние изменилось (сон записан другим путём) —
    нажатие «Сейчас» событие НЕ создаёт, меню перерисовано."""
    user_id = _uid(conn)  # внутренний id пользователя telegram_id=1001
    _press(db_path, "event:sleep")  # pending=sleep
    # Пока диалог открыт, сон отметили из другого сообщения/устройства
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-26 14:00:00")

    update, context = _press(
        db_path, CALLBACK_TIME_NOW, pending="sleep"
    )

    assert _sleep_count(conn, user_id) == 1  # дубль не создан
    assert update.callback_query.answers == [None]
    assert context.user_data.get(events.PENDING_KIND_KEY) is None
    texts_ = _button_texts(update.callback_query.message.reply_markup)
    assert state.BTN_WAKE in texts_  # перерисовано в актуальное состояние


def test_latest_wake_line_comes_from_setting(db_path, conn):
    """TC-EVNOW-006: строка «Крайнее пробуждение: …» в §7.5 берётся из
    настройки wake_deadline (ТЗ §5, FR-7), не из константы: сменили
    настройку на 16:00 — текст ответа меняется."""
    user_id = _uid(conn)  # внутренний id пользователя telegram_id=1001
    SettingsService(conn).seed_defaults(user_id)
    SettingsRepo(conn).upsert(user_id, "wake_deadline", "16:00")

    _press(db_path, "event:sleep")
    update, _ = _press(
        db_path, CALLBACK_TIME_NOW, pending="sleep"
    )

    answer = update.effective_message.replies[0]
    assert "Крайнее пробуждение: 16:00" in answer
    assert "15:30" not in answer


def test_observer_press_is_ignored_with_polite_message(db_path, conn):
    """TC-EVNOW-007: наблюдатель (users.role='observer') жмёт «😴 Заснул» —
    событие НЕ создаётся, есть вежливое сообщение, меню не переключается
    (design D13)."""
    user_id = _uid(conn)  # внутренний id пользователя telegram_id=1001
    UsersRepo(conn).get_or_create(1001)
    conn.execute("UPDATE users SET role='observer' WHERE telegram_id=1001")
    conn.commit()

    update, context = _press(db_path, "event:sleep")

    assert _sleep_count(conn, user_id) == 0  # записи нет
    assert update.callback_query.answers == [OBSERVER_DENIED]
    assert context.user_data.get(events.PENDING_KIND_KEY) is None
    # §7.10 не показан, меню не редактировалось под диалог времени
    assert update.callback_query.message.text != "Когда это произошло?"


def test_state_transitions_through_menu_are_repeatable(db_path, conn):
    """TC-EVNOW-008: повторный перевод состояния через меню корректен:
    sleep → wake → sleep; каждый раз guard пропускает валидное нажатие,
    меню показывает ровно одну событийную кнопку, события сырые
    (is_backfilled=0, D6)."""
    user_id = _uid(conn)  # внутренний id пользователя telegram_id=1001
    for _ in range(2):
        _press(db_path, "event:sleep")
        update, _ = _press(
            db_path, CALLBACK_TIME_NOW, pending="sleep"
        )
        assert state.BTN_WAKE in _button_texts(
            update.callback_query.message.reply_markup
        )
        assert state.get_state(conn, user_id) == state.STATE_ASLEEP

        _press(db_path, "event:wake")
        update, _ = _press(
            db_path, CALLBACK_TIME_NOW, pending="wake"
        )
        assert state.BTN_SLEEP in _button_texts(
            update.callback_query.message.reply_markup
        )
        assert state.get_state(conn, user_id) == state.STATE_AWAKE

    rows = SleepEventsRepo(conn).list_by_user(user_id)
    assert [e["event_type"] for e in rows] == ["sleep", "wake", "sleep", "wake"]
    assert all(e["is_backfilled"] is False for e in rows)


def test_create_event_rejects_contradicting_state_and_duplicate(db_path, conn):
    """TC-EVNOW-009: сервис create_event (design D5/D6): событие,
    противоречащее состоянию из БД, и незакрытый дубль того же вида
    отклоняются ValueError без записи; валидная запись — сырые поля."""
    user_id = _uid(conn)  # внутренний id пользователя telegram_id=1001
    with pytest.raises(ValueError):
        create_event(conn, user_id, "wake", "2026-10-26 12:00:00")  # БД пуста → awake
    assert _sleep_count(conn, user_id) == 0

    created = create_event(conn, user_id, "sleep", "2026-10-26 13:30:00")
    assert created["event_type"] == "sleep" and created["sleep_type"] is None

    with pytest.raises(ValueError):  # ребёнок уже спит — дубль sleep
        create_event(conn, user_id, "sleep", "2026-10-26 13:35:00")
    assert _sleep_count(conn, user_id) == 1

    # Валидное закрытие: wake при спящем проходит, повторный wake — нет
    create_event(conn, user_id, "wake", "2026-10-26 15:30:00")
    with pytest.raises(ValueError):  # ребёнок бодрствует — дубль wake
        create_event(conn, user_id, "wake", "2026-10-26 15:35:00")
    assert _sleep_count(conn, user_id) == 2
