# Регрессионные тесты BUG-002 (S2): нет постоянной клавиатуры — после
# диалога нечем нажимать (S2-20261026-no-persistent-keyboard).
#
# Root cause: меню унифицировано на inline-клавиатуре внутри конкретного
# сообщения; после диалога панель остаётся в истории чата. Фикс (PLAN-BUG-002):
# постоянная reply-клавиатура [😴 Заснул] [☀️ Проснулся] [↩️ Отменить последнее]
# + обработчик текстовых кнопок с guard-перечитыванием состояния (design D5):
# «не та» кнопка — дружелюбный ответ по факту БЕЗ записи + свежее inline-меню;
# после отмены — подтверждение + свежее меню; произвольный текст вне диалога —
# молча (не ломает задачу 4.1).
#
# Правило rule 6: каждый тест несёт TC-BUG002-NNN в первой строке докстринга.

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup

from src import texts
from src.db import init_db
from src.handlers import common, events, undo
from src.handlers.events import PENDING_KIND_KEY
from src.repositories.sleep_events import SleepEventsRepo
from src.services.event import get_user
from src.services.state import (
    BTN_SLEEP,
    BTN_UNDO,
    BTN_WAKE,
    REPLY_BUTTONS,
    REPLY_KEYBOARD,
)

TZ = ZoneInfo("Europe/Moscow")  # UTC+3, без перехода на летнее время


# ------------------------------------------------------------ Заглушки PTB
# (контракты как в tests/test_bug_001.py; reply_text копит ВСЕ ответы —
# свежее меню идёт отдельным сообщением после подтверждения)


class _FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list[str] = []
        self.markups: list = []

    async def reply_text(self, reply, reply_markup=None):
        self.replies.append(reply)
        self.markups.append(reply_markup)


class _FakeTextUpdate:
    def __init__(self, user_id: int, text: str) -> None:
        self.effective_user = type("U", (), {"id": user_id})()
        self.callback_query = None
        self.message = _FakeMessage(text)
        self.effective_message = self.message


class _FakeContext:
    def __init__(self, db_path) -> None:
        self.bot_data = {"db_path": db_path}
        self.user_data: dict = {}


# --------------------------------------------------------------- Помощники


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "bot.db"


@pytest.fixture()
def conn(db_path):
    c = init_db(db_path)
    yield c
    c.close()


def _utc_str(local_dt: datetime) -> str:
    """Локальный момент → строка UTC БД «YYYY-MM-DD HH:MM:SS» (design D4)."""
    return local_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")


def _insert_sleep(conn, user_id: int) -> None:
    """Ввести sleep «5 минут назад» → состояние asleep (design D5)."""
    local_dt = datetime.now(TZ).replace(second=0, microsecond=0)
    SleepEventsRepo(conn).insert(user_id, "sleep", _utc_str(local_dt))


def _press(db_path, text: str, telegram_id: int):
    update = _FakeTextUpdate(telegram_id, text)
    context = _FakeContext(db_path)
    asyncio.run(
        common.reply_button_message(update, context)  # type: ignore[arg-type]
    )
    return update, context


def _events(conn, user_id: int) -> list[dict]:
    return SleepEventsRepo(conn).list_by_user(user_id)


# ------------------------------------------------------------------ Тесты


def test_tc_bug002_001_reply_keyboard_static_three_buttons():
    """TC-BUG002-001: панель постоянная — статичный список из ровно трёх
    кнопок [😴 Заснул] [☀️ Проснулся] [↩️ Отменить последнее]; разметка PTB
    с resize_keyboard=True и БЕЗ one_time_keyboard (панель не исчезает
    после нажатия — суть хотфикса BUG-002)."""
    assert REPLY_KEYBOARD == [[BTN_SLEEP, BTN_WAKE, BTN_UNDO]]
    assert len(REPLY_BUTTONS) == 3
    markup = common.build_reply_keyboard()
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert markup.resize_keyboard is True
    assert markup.one_time_keyboard is not True
    texts_ = [btn.text for row in markup.keyboard for btn in row]
    assert texts_ == [BTN_SLEEP, BTN_WAKE, BTN_UNDO]


def test_tc_bug002_002_sleep_button_when_asleep_refusal_no_record(db_path, conn):
    """TC-BUG002-002: «😴 Заснул» с панели при asleep → дружелюбный отказ
    («уже спит»), записи НЕТ, присылается свежее inline-меню — в нём
    «☀️ Проснулся» (состояние asleep, меню перечитано из БД, design D5)."""
    user = get_user(conn, 4201)
    _insert_sleep(conn, user["id"])
    before = _events(conn, user["id"])

    update, context = _press(db_path, BTN_SLEEP, telegram_id=4201)

    # Ровно два ответа: отказ + свежее меню; отказ дружелюбный, без §7.10.
    assert len(update.effective_message.replies) == 2
    assert "уже спит" in update.effective_message.replies[0]
    assert texts.BACKFILL_CHOOSE_TIME not in update.effective_message.replies[0]
    # Свежее меню — inline-клавиатура актуального состояния (asleep → wake).
    menu = update.effective_message.markups[1]
    assert isinstance(menu, InlineKeyboardMarkup)
    menu_texts = [btn.text for row in menu.inline_keyboard for btn in row]
    assert BTN_WAKE in menu_texts and BTN_SLEEP not in menu_texts
    # Записи НЕТ: событий столько же, сколько до нажатия.
    assert _events(conn, user["id"]) == before
    # Диалог не начат.
    assert PENDING_KIND_KEY not in context.user_data


def test_tc_bug002_003_wake_button_when_asleep_starts_7_10_dialog(db_path, conn):
    """TC-BUG002-003: «☀️ Проснулся» с панели при asleep → штатный диалог
    §7.10 («Когда это произошло?» + кнопки time:now / time:manual), фаза
    pending=wake; запись ещё НЕ создаётся (создаёт выбор времени)."""
    user = get_user(conn, 4202)
    _insert_sleep(conn, user["id"])

    update, context = _press(db_path, BTN_WAKE, telegram_id=4202)

    assert len(update.effective_message.replies) == 1
    assert update.effective_message.replies[0] == texts.BACKFILL_CHOOSE_TIME
    markup = update.effective_message.markups[0]
    assert isinstance(markup, InlineKeyboardMarkup)
    datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert datas == [events.CALLBACK_TIME_NOW, events.CALLBACK_TIME_MANUAL]
    assert context.user_data[PENDING_KIND_KEY] == "wake"
    # Событие ещё не записано: только sleep из подготовки.
    assert [(ev["event_type"]) for ev in _events(conn, user["id"])] == ["sleep"]


def test_tc_bug002_004_wake_button_when_awake_refusal_no_record(db_path, conn):
    """TC-BUG002-004: «☀️ Проснулся» с панели при awake (записей нет) →
    отказ («бодрствует»), записи НЕТ + свежее inline-меню с «😴 Заснул»."""
    get_user(conn, 4203)

    update, context = _press(db_path, BTN_WAKE, telegram_id=4203)

    assert len(update.effective_message.replies) == 2
    assert "бодрствует" in update.effective_message.replies[0]
    menu = update.effective_message.markups[1]
    menu_texts = [btn.text for row in menu.inline_keyboard for btn in row]
    assert BTN_SLEEP in menu_texts and BTN_WAKE not in menu_texts
    assert _events(conn, 4203) == []
    assert PENDING_KIND_KEY not in context.user_data


def test_tc_bug002_005_sleep_button_when_awake_starts_7_10_dialog(db_path, conn):
    """TC-BUG002-005 (симптом Марии, happy path): завершила диалог, открыла
    чат заново, нажала «😴 Заснул» с панели при awake → работает без
    прокрутки истории: диалог §7.10, фаза pending=sleep."""
    get_user(conn, 4204)

    update, context = _press(db_path, BTN_SLEEP, telegram_id=4204)

    assert update.effective_message.replies == [texts.BACKFILL_CHOOSE_TIME]
    assert context.user_data[PENDING_KIND_KEY] == "sleep"


def test_tc_bug002_006_undo_button_deletes_last_and_sends_fresh_menu(db_path, conn):
    """TC-BUG002-006: «↩️ Отменить последнее» с панели — та же логика
    undo:last: последняя запись удалена, подтверждение «Отменено: …»
    (PB-4: ЧТО удалено + мини-состояние; без «живого» ЧЧ:ММ — правило
    волны 6) + свежее inline-меню по состоянию ПОСЛЕ удаления (awake →
    «😴 Заснул»)."""
    user = get_user(conn, 4205)
    _insert_sleep(conn, user["id"])
    assert len(_events(conn, user["id"])) == 1

    update, _context = _press(db_path, BTN_UNDO, telegram_id=4205)

    assert len(update.effective_message.replies) == 2
    # PB-4: подтверждение начинается с «Отменено: Заснул» (локальное
    # время записи — реальное «5 минут назад», поэтому без фиксации
    # ЧЧ:ММ в ассерте).
    assert update.effective_message.replies[0].startswith("Отменено: Заснул ")
    assert _events(conn, user["id"]) == []
    menu = update.effective_message.markups[1]
    menu_texts = [btn.text for row in menu.inline_keyboard for btn in row]
    assert BTN_SLEEP in menu_texts


def test_tc_bug002_007_undo_button_empty_db_nothing_to_undo(db_path, conn):
    """TC-BUG002-007: «↩️ Отменить последнее» при пустой БД → «отменять
    нечего», данные не меняются; свежее меню всё равно присылается."""
    user = get_user(conn, 4206)

    update, _context = _press(db_path, BTN_UNDO, telegram_id=4206)

    assert len(update.effective_message.replies) == 2
    assert update.effective_message.replies[0] == undo.NOTHING_TO_UNDO
    assert _events(conn, user["id"]) == []


def test_tc_bug002_008_non_button_text_silently_ignored(db_path, conn):
    """TC-BUG002-008: произвольный текст (не кнопка панели) вне диалога —
    молча, ни одного ответа, записи нет (не ломает 4.1: диалог заднего
    числа ведёт backfill.py)."""
    user = get_user(conn, 4207)

    update, _context = _press(db_path, "просто текст", telegram_id=4207)

    assert update.effective_message.replies == []
    assert update.effective_message.markups == []
    assert _events(conn, user["id"]) == []
