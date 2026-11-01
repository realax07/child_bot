# Юнит-тесты состояния и динамического меню (change-пакет add-core-diary,
# задача 2.2).
#
# Критерии готовности 2.2 (FR-6, design D5): состояние выводится из
# последнего события (нет событий/wake → бодрствует, sleep → спит);
# клавиатура — ровно одна событийная кнопка + «↩️ Отменить последнее»;
# guard устаревших кнопок для задач 2.3/2.4. Состояние всегда перечитывается
# из БД (без кэша между нажатиями).
#
# Правило rule 6: каждый тест несёт TC-STATE-NNN в первой строке докстринга.

from __future__ import annotations

import asyncio

import pytest

from src.db import init_db
from src.handlers import common
from src.handlers.common import (
    BTN_SLEEP,
    BTN_UNDO,
    BTN_WAKE,
    CALLBACK_SLEEP,
    CALLBACK_UNDO,
    CALLBACK_WAKE,
    KIND_SLEEP,
    KIND_UNDO,
    KIND_WAKE,
    build_markup,
    current_markup,
)
from src.repositories.sleep_events import SleepEventsRepo
from src.repositories.users import UsersRepo
from src.services.state import (
    BTN_HINT,
    BTN_HISTORY,
    BTN_SETTINGS,
    CALLBACK_HISTORY,
    CALLBACK_SETTINGS_SHOW,
    REPLY_BUTTONS,
    REPLY_KEYBOARD,
    STATE_ASLEEP,
    STATE_AWAKE,
    build_menu,
    get_state,
    is_stale_callback,
    kind_of_button,
)


# ------------------------------------------------------------ Заглушки PTB

class _FakeMessage:
    def __init__(self) -> None:
        self.reply_markup = None

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup


class _FakeCallbackQuery:
    def __init__(self) -> None:
        self.message = _FakeMessage()

    async def edit_message_reply_markup(self, reply_markup=None):
        await self.message.edit_reply_markup(reply_markup=reply_markup)


class _FakeUpdate:
    def __init__(self) -> None:
        self.callback_query = _FakeCallbackQuery()


# --------------------------------------------------------------- Фикстуры

@pytest.fixture()
def conn(tmp_path):
    """Свежая БД; пользователь создаётся по мере необходимости."""
    c = init_db(tmp_path / "bot.db")
    yield c
    c.close()


def _user_id(conn, telegram_id: int = 1001) -> int:
    return UsersRepo(conn).get_or_create(telegram_id)["id"]


def _button_texts(markup) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _callback_datas(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


# ------------------------------------------------------------------- Тесты

def test_menu_after_start_awake_has_sleep_button(conn):
    """TC-STATE-001: после /start (событий нет) меню содержит «😴 Заснул»
    и не содержит «☀️ Проснулся» (FR-6, design D5)."""
    user_id = _user_id(conn)  # «пользователь после /start» — событий нет

    markup = current_markup(conn, user_id)
    texts_ = _button_texts(markup)
    assert BTN_SLEEP in texts_
    assert BTN_WAKE not in texts_

    # Событийная кнопка несёт контракт колбэка задачи 2.3
    assert CALLBACK_SLEEP in _callback_datas(markup)


def test_menu_after_sleep_event_flips_to_wake(conn):
    """TC-STATE-002: после sleep-события меню содержит «☀️ Проснулся»
    и не содержит «😴 Заснул»; после удаления последнего события (undo,
    D12) состояние возвращается к «бодрствует» — без кэша (FR-6, D5)."""
    user_id = _user_id(conn)
    events = SleepEventsRepo(conn)
    events.insert(user_id, "sleep", "2026-10-26 13:30:00")

    markup = current_markup(conn, user_id)
    texts_ = _button_texts(markup)
    assert BTN_WAKE in texts_
    assert BTN_SLEEP not in texts_
    assert CALLBACK_WAKE in _callback_datas(markup)

    # Перечитывание из БД при каждом построении: undo (D12) → снова awake
    events.delete_last(user_id)
    assert BTN_SLEEP in _button_texts(current_markup(conn, user_id))


def test_get_state_empty_db_and_event_types(conn):
    """TC-STATE-003: пустая БД → awake; последнее wake → awake;
    последнее sleep → asleep (design D5). «Последнее» — по created_at,
    затем id (D12), не по timestamp."""
    user_id = _user_id(conn)
    assert get_state(conn, user_id) == STATE_AWAKE  # пустая БД

    events = SleepEventsRepo(conn)
    # created_at фиксирован у всех вставок: тест не зависит от реального
    # времени (дефолт now() после 14:00 ломает порядок «последнего»).
    events.insert(
        user_id, "sleep", "2026-10-25 22:00:00", created_at="2026-10-26 10:00:00"
    )
    events.insert(
        user_id, "wake", "2026-10-26 07:20:00", created_at="2026-10-26 11:00:00"
    )
    assert get_state(conn, user_id) == STATE_AWAKE

    events.insert(
        user_id, "sleep", "2026-10-26 13:30:00", created_at="2026-10-26 13:30:00"
    )
    assert get_state(conn, user_id) == STATE_ASLEEP

    # Событие с ранним timestamp, записанное позже, — всё равно «последнее»
    events.insert(
        user_id,
        "wake",
        "2026-10-25 21:00:00",
        created_at="2026-10-26 14:00:00",
    )
    assert get_state(conn, user_id) == STATE_AWAKE

    # Состояние другого пользователя не зависит от чужих событий
    other = _user_id(conn, telegram_id=2002)
    assert get_state(conn, other) == STATE_AWAKE


def test_is_stale_callback_guard(conn):
    """TC-STATE-004: guard устаревших кнопок (FR-6, D5): «😴 Заснул» при
    состоянии asleep — устаревшая; «☀️ Проснулся» при awake — устаревшая;
    в своих состояниях — актуальные; «Отменить последнее» валидна всегда;
    состояние перечитывается из БД при каждом вызове."""
    user_id = _user_id(conn)
    # awake: «Заснул» актуальна, «Проснулся» устарела
    assert is_stale_callback(conn, user_id, KIND_SLEEP) is False
    assert is_stale_callback(conn, user_id, KIND_WAKE) is True

    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-26 13:30:00")
    # asleep: наоборот — без какой-либо синхронизации состояния
    assert is_stale_callback(conn, user_id, KIND_SLEEP) is True
    assert is_stale_callback(conn, user_id, KIND_WAKE) is False

    # «Отменить последнее» валидна в обоих состояниях (D12)
    assert is_stale_callback(conn, user_id, KIND_UNDO) is False

    # Неизвестный вид кнопки — безопасно трактуется как устаревшая
    assert is_stale_callback(conn, user_id, "bogus") is True

    # kind_of_button: текст → вид для обработчиков 2.3
    assert kind_of_button(BTN_SLEEP) == KIND_SLEEP
    assert kind_of_button(BTN_WAKE) == KIND_WAKE
    assert kind_of_button(BTN_SETTINGS) is None


def test_menu_has_undo_button_in_both_states(conn):
    """TC-STATE-005: клавиатура содержит «↩️ Отменить последнее» в обоих
    состояниях; событийная кнопка ровно одна (FR-6, design D12)."""
    user_id = _user_id(conn)

    for expected_state, event_btn in (
        (STATE_AWAKE, BTN_SLEEP),
        (STATE_ASLEEP, BTN_WAKE),
    ):
        markup = build_markup(expected_state)
        texts_ = _button_texts(markup)
        assert BTN_UNDO in texts_, expected_state
        event_buttons = [t for t in texts_ if t in (BTN_SLEEP, BTN_WAKE)]
        assert event_buttons == [event_btn], expected_state
        # callback-контракты задач 2.3/2.4 + новых кнопок 2.0 на месте
        datas = _callback_datas(markup)
        assert CALLBACK_UNDO in datas and len(datas) == 4
        # «Отменить последнее» — отдельной строкой под строкой события и
        # «История»/«Настройки» (ТЗ §6, add-settings-history 2.0)
        assert len(markup.inline_keyboard) == 3

        if expected_state == STATE_AWAKE:
            # и через реальное чтение БД то же самое
            assert _button_texts(current_markup(conn, user_id)) == texts_
            SleepEventsRepo(conn).insert(
                user_id, "sleep", "2026-10-26 15:00:00"
            )


def test_redraw_menu_repaints_actual_keyboard(conn):
    """TC-STATE-006: перерисовка (guard, FR-6): на нажатие устаревшей кнопки
    из истории чата клавиатура сообщения переставляется в актуальное
    состояние по свежему чтению БД — основа задач 2.3/2.4."""
    user_id = _user_id(conn)
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-26 13:30:00")

    update = _FakeUpdate()
    asyncio.run(common.redraw_menu(update, conn, user_id))  # type: ignore[arg-type]
    texts_ = _button_texts(update.callback_query.message.reply_markup)
    assert BTN_WAKE in texts_ and BTN_SLEEP not in texts_

    # Нажатие не из колбэка — перерисовка безопасно ничего не делает
    class _NoCallback:
        callback_query = None

    asyncio.run(common.redraw_menu(_NoCallback(), conn, user_id))  # type: ignore[arg-type]


def test_reply_panel_has_hint_button_in_both_states(conn):
    """TC-STATE-007 (ТЗ §6, FR-32, задача 2.2-fix): постоянная reply-панель
    содержит «⏰ Подсказка сейчас» рядом с событийными кнопками; клавиатура
    ОБЩАЯ для обоих состояний (панель статична, состояние перечитывается на
    нажатие — design D5). Событийная inline-кнопка по-прежнему ровно одна
    (TC-STATE-005 не задет); текст кнопки — не событие (kind_of_button →
    None, FR-32: подсказка не создаёт запись)."""
    assert BTN_HINT == "⏰ Подсказка сейчас"  # ТЗ §6 дословно
    flat = [t for row in REPLY_KEYBOARD for t in row]
    assert flat == [
        BTN_SLEEP,
        BTN_WAKE,
        BTN_HINT,
        BTN_HISTORY,
        BTN_SETTINGS,
        BTN_UNDO,
    ]
    assert REPLY_BUTTONS == (
        BTN_SLEEP,
        BTN_WAKE,
        BTN_HINT,
        BTN_HISTORY,
        BTN_SETTINGS,
        BTN_UNDO,
    )
    assert len(REPLY_KEYBOARD) == 3  # события+подсказка / история+настройки / отмена

    markup = common.build_reply_keyboard()
    texts_ = [btn.text for row in markup.keyboard for btn in row]
    assert BTN_HINT in texts_

    # Подсказка — не событийная кнопка: route определяется в common, kind нет.
    assert kind_of_button(BTN_HINT) is None


def test_menu_has_history_and_settings_buttons_in_both_states(conn):
    """TC-STATE-008 (ТЗ §6, FR-48/FR-42, add-settings-history 2.0):
    build_menu ОБОИХ состояний содержит «📊 История за неделю» и
    «⚙️ Настройки» — тексты дословно §6, callback-контракты §10.3
    («history:week», «settings:show»), layout §6: после строки события,
    до «↩️ Отменить последнее». Обе кнопки — НЕ событийные
    (kind_of_button → None): событий не создают, guard событийных
    кнопок не затронут."""
    assert BTN_HISTORY == "📊 История за неделю"  # ТЗ §6 дословно
    assert BTN_SETTINGS == "⚙️ Настройки"  # ТЗ §6 дословно

    for state_ in (STATE_AWAKE, STATE_ASLEEP):
        markup = build_markup(state_)
        texts_ = _button_texts(markup)
        assert BTN_HISTORY in texts_ and BTN_SETTINGS in texts_, state_
        datas = _callback_datas(markup)
        assert CALLBACK_HISTORY in datas, state_
        assert CALLBACK_SETTINGS_SHOW in datas, state_
        # Layout §6: обе новые кнопки между строкой события и «Отменить»
        assert texts_.index(BTN_HISTORY) < texts_.index(BTN_UNDO), state_
        assert texts_.index(BTN_SETTINGS) < texts_.index(BTN_UNDO), state_

    # Не событийные кнопки: kind нет — запись событий через них невозможна.
    assert kind_of_button(BTN_HISTORY) is None
    assert kind_of_button(BTN_SETTINGS) is None


class _FakeRouteMessage:
    """Сообщение reply-панели: текст кнопки + накопитель ответов."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, reply, reply_markup=None):
        self.replies.append(reply)


class _FakeRouteUpdate:
    def __init__(self, user_id: int, text: str) -> None:
        self.effective_user = type("U", (), {"id": user_id})()
        self.callback_query = None
        self.message = _FakeRouteMessage(text)
        self.effective_message = self.message


class _FakeRouteContext:
    def __init__(self, db_path) -> None:
        self.bot_data = {"db_path": db_path}
        self.user_data: dict = {}


def test_reply_button_message_routes_new_buttons(tmp_path):
    """TC-STATE-009 (ТЗ §6/§10.3, add-settings-history 2.0): обе новые
    текстовые кнопки панели распознаны common.reply_button_message:
    «⚙️ Настройки» → экран §7.16 (тот же ответ, что callback
    settings:show / команда /settings); «📊 История за неделю» —
    распознана как кнопка панели, но БЕЗ ответа и БЕЗ записи событий
    (обработчик history:week — задача 2.1, вне зоны 2.0; state-ui spec:
    нажатие не меняет дневник). Произвольный текст — молча."""
    from src import texts
    from src.repositories.sleep_events import SleepEventsRepo

    db = tmp_path / "bot.db"

    # «📊 История за неделю»: без записи и без ответа (маршрут 2.1)
    update = _FakeRouteUpdate(3001, BTN_HISTORY)
    asyncio.run(
        common.reply_button_message(update, _FakeRouteContext(db))  # type: ignore[arg-type]
    )
    assert update.effective_message.replies == []
    conn = init_db(db)
    assert SleepEventsRepo(conn).list_by_user(1) == []

    # «⚙️ Настройки»: экран §7.16, событий тоже нет
    update = _FakeRouteUpdate(3002, BTN_SETTINGS)
    asyncio.run(
        common.reply_button_message(update, _FakeRouteContext(db))  # type: ignore[arg-type]
    )
    replies = update.effective_message.replies
    assert len(replies) == 1
    assert texts.SETTINGS_SCREEN_HEADER in replies[0]
    assert SleepEventsRepo(conn).list_by_user(2) == []

    # Произвольный текст (не кнопка) — молча, как раньше
    update = _FakeRouteUpdate(3003, "привет")
    asyncio.run(
        common.reply_button_message(update, _FakeRouteContext(db))  # type: ignore[arg-type]
    )
    assert update.effective_message.replies == []
    conn.close()
