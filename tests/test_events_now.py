# Юнит-тесты событийных кнопок и «🕐 Сейчас» (change-пакет add-core-diary,
# задача 2.3; волна 6 — PB-3, задача 6.1).
#
# Критерии готовности 2.3 (FR-7, FR-8, FR-15; design D2, D5, D6):
#   - event:sleep/event:wake → меню §7.10 (time:now / time:manual);
#   - time:now создаёт событие с фактическим временем, is_backfilled=0,
#     меню переключается, ответ §7.5 / §7.2 со сводкой событий дня (PB-3);
#   - волна 6 (решение продакта 29.10): строка «Крайнее пробуждение» в
#     ответе НЕ выводится вовсе;
#   - задача 2.3 (FR-38, design D20): §7.2 получает строку окна дневного
#     сна от фактического подъёма (§7.2/§7.3/§7.4-стиль), §7.5 — без
#     окон; сводка PB-3 побайтно как в волне 6;
#   - guard устаревшей кнопки (design D5): записи нет, answerCallbackQuery,
#     клавиатура перерисована в актуальное состояние (F-3 review-001);
#   - observer (design D13): нажатие игнорируется с вежливым сообщением;
#   - повторный перевод состояния через меню корректен (sleep→wake→sleep).
#
# Правило rule 6: каждый тест несёт TC-EVNOW-NNN в первой строке докстринга.
# Время фиксируется в фикстуре frozen_now (monkeypatch events.datetime):
# 9:00 UTC = 12:00 МСК — ассерты без «живых» ЧЧ:ММ (правило волны 6).

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src import texts
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

# Фиксированный «сейчас»: 9:00 UTC = 12:00 МСК (Европа/Москва, UTC+3).
FIXED_NOW_UTC = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """datetime с замороженным now() — время теста фиксируется в фикстуре."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        if tz is not None:
            return FIXED_NOW_UTC.astimezone(tz)
        return FIXED_NOW_UTC.replace(tzinfo=None)


@pytest.fixture()
def frozen_now(monkeypatch):
    """Фиксировать «сейчас» обработчика events (правило волны 6)."""
    monkeypatch.setattr(events, "datetime", _FrozenDatetime)


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


def test_time_now_creates_sleep_event_and_answers_7_5(db_path, conn, frozen_now):
    """TC-EVNOW-002: «😴 Заснул» → «🕐 Сейчас» создаёт sleep-событие с
    фактическим временем (UTC, is_backfilled=0), меню переключается на
    «☀️ Проснулся», ответ — §7.5 с фактическим временем и сводкой событий
    дня (PB-3): пустой день — только «Бодрствование общее: 24 ч»; БЕЗ
    «Крайнее пробуждение» (решение 29.10) и БЕЗ строки окна — §7.5 окон
    не содержит (FR-7 в ред. волны 6, D6; design D20 — показ окон в §7.5
    отклонён). Время зафиксировано: 9:00 UTC = 12:00 МСК."""
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
    assert event["timestamp"] == "2026-10-10 09:00:00"  # фиксированное «сейчас»

    # Меню переключилось (redraw по свежему состоянию, FR-6)
    texts_ = _button_texts(update.callback_query.message.reply_markup)
    assert state.BTN_WAKE in texts_ and state.BTN_SLEEP not in texts_

    # Ответ §7.5: подтверждение + «Сон идёт.» + сводка событий дня (PB-3):
    # только что созданный незакрытый дневной отрезок 12:00 — строка
    # «Заснул» (без «Проснулся» — сон продолжается); строк «Крайнее
    # пробуждение» и «Окно» нет. Ред. BUG-004: база day_start (подъёма
    # нет), конец — момент расчёта 12:00 → 6 ч.
    assert len(update.effective_message.replies) == 1
    answer = update.effective_message.replies[0]
    assert answer.splitlines() == [
        "✅ Засыпание записано в 12:00.",
        "Сон идёт.",
        "Заснул: 12:00",
        "Бодрствование общее: 6 ч",
    ]
    assert answer == fell_asleep_text(
        "12:00", ["Заснул: 12:00", "Бодрствование общее: 6 ч"]
    )
    assert "Крайнее пробуждение" not in answer
    assert "Окно" not in answer


def test_time_now_creates_wake_event_and_answers_7_2_without_window(db_path, conn, frozen_now):
    """TC-EVNOW-003: «☀️ Проснулся» → «🕐 Сейчас» создаёт wake-событие,
    меню переключается на «😴 Заснул», ответ — §7.2 с фактическим временем
    и сводкой событий дня (PB-3): дневной сон 11:00→12:00 МСК как факты
    «Заснул/Проснулся/Дневной сон». Задача 2.3 (FR-38, D20): в сценарии
    НЕТ факта «Подъем» (ночной сон не отмечен) — окно от ненастоящего
    подъёма не строится, строка окна не добавляется (ответ как в волне 6,
    сводка PB-3 побайтно прежняя). Время зафиксировано: 9:00 UTC = 12:00 МСК."""
    user_id = _uid(conn)  # внутренний id пользователя telegram_id=1001
    # Сон, начатый час назад до ФИКСИРОВАННОГО «сейчас» (8:00 UTC = 11:00 МСК)
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-10 08:00:00")
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
    # Факты дня (PB-3): дневной отрезок — после склейки, из сырых событий.
    assert answer.splitlines() == [
        "✅ Пробуждение записано в 12:00.",
        "Заснул: 11:00",
        "Проснулся: 12:00",
        "Дневной сон: 1 ч",
        # Ред. BUG-004: (12:00 − 06:00) − 1 ч дневного сна = 5 ч.
        "Бодрствование общее: 5 ч",
    ]
    assert answer == woke_up_text(
        "12:00",
        ["Заснул: 11:00", "Проснулся: 12:00", "Дневной сон: 1 ч",
         "Бодрствование общее: 5 ч"],
    )
    assert "Окно" not in answer
    assert "Крайнее пробуждение" not in answer


# ----------------------------------------- Задача 2.3: окна §7.2–7.4 (FR-38)

def _frozen_at(moment_utc: datetime) -> type:
    """Класс datetime с замороженным now() на момент moment_utc
    (паттерн tests/test_hint.py — «Сейчас» кнопки = момент подъёма)."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is not None:
                return moment_utc.astimezone(tz)
            return moment_utc.replace(tzinfo=None)

    return _FrozenDatetime


def _wake_now(db_path, rise_utc: datetime):
    """«☀️ Проснулся» → «🕐 Сейчас» с «сейчас» = моменту подъёма rise_utc
    (ночь отмечена незакрытым sleep — подъём создаёт САМА кнопка).
    Подмена events.datetime — ТОЛЬКО на время нажатий (восстанавливается,
    чтобы не протекать в другие тесты процесса)."""
    saved = events.datetime
    try:
        events.datetime = _frozen_at(rise_utc)
        _press(db_path, "event:wake")
        return _press(db_path, CALLBACK_TIME_NOW, pending="wake")
    finally:
        events.datetime = saved


def test_time_now_wake_standard_rise_answers_7_2_with_window(db_path, conn):
    """TC-EVNOW-010 (FR-38, design D20, §7.2 в ред. PB-3): «☀️ Проснулся» →
    «🕐 Сейчас» при стандартном подъёме 08:00 МСК (ночь 22:00→08:00) —
    ответ содержит строку окна «Окно дневного сна: 13:20–14:00.»
    (08:00 + 5:20…6:00, из расчёта nap_window — не хардкод), сводку PB-3
    БЕЗ изменений («Подъем: 08:00» + «Бодрствование общее: 0 мин» по
    BUG-004: 08:00 − 08:00) и БЕЗ «Крайнее пробуждение». Время
    зафиксировано: 5:00 UTC = 08:00 МСК 10.10 (момент подъёма)."""
    user_id = _uid(conn)  # внутренний id пользователя telegram_id=1001
    # Ночь 09.10 22:00 МСК — незакрытый sleep; подъём отмечает кнопка.
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-09 19:00:00")

    update, _ = _wake_now(
        db_path, datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc)
    )

    answer = update.effective_message.replies[0]
    # Форма D20: факт + окно + сводка PB-3 (побайтно как в волне 6).
    assert answer.splitlines() == [
        "✅ Пробуждение записано в 08:00.",
        "Окно дневного сна: 13:20–14:00.",
        "Подъем: 08:00",
        "Бодрствование общее: 0 мин",
    ]
    assert answer == woke_up_text(
        "08:00",
        ["Подъем: 08:00", "Бодрствование общее: 0 мин"],
        "Окно дневного сна: 13:20–14:00.",
    )
    assert "Крайнее пробуждение" not in answer


def test_time_now_wake_early_rise_answers_7_3_with_anchors(db_path, conn):
    """TC-EVNOW-011 (FR-38, design D20, §7.3): ранний подъём 07:00 МСК —
    строка окна §7.3-стиль: совет «растянуть до 13:00» (лимит §3.6),
    окно 12:20–13:00 (07:00 + 5:20…6:00 ≤ лимита) и якоря ИЗ НАСТРОЕК
    («не позже 15:30», «около 22:00»); сводка PB-3 без изменений.
    Время зафиксировано: 4:00 UTC = 07:00 МСК 10.10."""
    user_id = _uid(conn)
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-09 19:00:00")

    update, _ = _wake_now(
        db_path, datetime(2026, 10, 10, 4, 0, tzinfo=timezone.utc)
    )

    answer = update.effective_message.replies[0]
    assert answer.splitlines() == [
        "✅ Пробуждение записано в 07:00.",
        (
            "Сегодня можно растянуть первое бодрствование до 13:00, "
            "чтобы не сдвигать режим. Окно дневного сна: 12:20–13:00. "
            "Пробуждение — не позже 15:30, уход в ночь — около 22:00."
        ),
        "Подъем: 07:00",
        "Бодрствование общее: 0 мин",
    ]


def test_time_now_wake_late_rise_answers_7_4_with_anchors(db_path, conn):
    """TC-EVNOW-012 (FR-38, design D20, §7.4): поздний подъём 09:00 МСК —
    строка окна §7.4-стиль: окно 14:20–15:00 (09:00 + 5:20…6:00),
    граница «разбудить не позже 15:30» (прогноз конца 15:50 позже
    крайнего) и якорь из настроек «около 22:30»; сводка PB-3 без
    изменений. Время: 6:00 UTC = 09:00 МСК 10.10."""
    user_id = _uid(conn)
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-09 19:30:00")

    update, _ = _wake_now(
        db_path, datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
    )

    answer = update.effective_message.replies[0]
    assert answer.splitlines() == [
        "✅ Пробуждение записано в 09:00.",
        (
            "Окно дневного сна: 14:20–15:00. Важно разбудить не позже "
            "15:30, чтобы уход в ночь был около 22:30."
        ),
        "Подъем: 09:00",
        "Бодрствование общее: 0 мин",
    ]


# ------------------------------- BUG-005: после дневного сна (Флоу 3)
# Решение продакта 01.11: пробуждение ПОСЛЕ дневного сна (последний
# завершённый интервал страницы — дневной отрезок) получает КОНКРЕТНОЕ
# время ухода «Рекомендованное время ухода: ЧЧ:ММ» (NightWindow.
# recommended, ОВ-4.2), а не прошедшее дневное окно; позже 22:30 —
# предупреждение. Утренний подъём (последний завершённый интервал —
# ночной отрезок) — дневное окно, как раньше (TC-EVNOW-010…012);
# сценарии без «Подъема» — без строки (TC-EVNOW-003).

def test_time_now_wake_after_day_sleep_recommends_bedtime(db_path, conn):
    """TC-EVNOW-013 (BUG-005, сценарий приемки Марии): подъём 08:03,
    дневной сон 13:40, «☀️ Проснулся» → «🕐 Сейчас» в 15:15 — ответ
    содержит «Рекомендованное время ухода: 22:00.» (NightWindow.
    recommended: бодрствование 5 ч 37 мин, 12 ч 10 мин набираются в
    21:48 — раньше 22:00 → ровно 22:00) и БЕЗ прошедшего окна «Окно
    дневного сна: 13:23–14:03». Время зафиксировано: 12:15 UTC =
    15:15 МСК 10.10."""
    user_id = _uid(conn)
    # Ночь 09.10 22:00 МСК → 08:03 МСК (завершённый ночной отрезок);
    # дневной сон 13:40 МСК — закрывает САМА кнопка «Сейчас» 15:15.
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-09 19:00:00")
    SleepEventsRepo(conn).insert(user_id, "wake", "2026-10-10 05:03:00")
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-10 10:40:00")

    update, _ = _wake_now(
        db_path, datetime(2026, 10, 10, 12, 15, tzinfo=timezone.utc)
    )

    answer = update.effective_message.replies[0]
    assert answer.splitlines() == [
        "✅ Пробуждение записано в 15:15.",
        "Рекомендованное время ухода: 22:00.",
        "Подъем: 08:03",
        "Заснул: 13:40",
        "Проснулся: 15:15",
        "Дневной сон: 1 ч 35 мин",
        "Бодрствование общее: 5 ч 37 мин",
    ]
    assert "Окно дневного сна" not in answer
    assert "13:23" not in answer
    assert "Крайнее пробуждение" not in answer


def test_time_now_wake_after_late_day_sleep_warns_past_deadline(db_path, conn):
    """TC-EVNOW-014 (BUG-005): поздний дневной сон 15:00–18:00 при
    подъёме 08:00 — recommended (23:10: бодрствование 7 ч, 12 ч 10 мин
    набираются в 23:10) СТРОГО позже 22:30 → та же строка рекомендации
    с предупреждением (§7.24-стиль, коротко и без жаргона). Время:
    15:00 UTC = 18:00 МСК 10.10."""
    user_id = _uid(conn)
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-09 19:00:00")
    SleepEventsRepo(conn).insert(user_id, "wake", "2026-10-10 05:00:00")
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-10-10 12:00:00")

    update, _ = _wake_now(
        db_path, datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
    )

    answer = update.effective_message.replies[0]
    lines = answer.splitlines()
    assert lines[0] == "✅ Пробуждение записано в 18:00."
    assert lines[1] == texts.recommended_bedtime_late_line("23:10")
    assert "Окно дневного сна" not in answer
    # Сводка PB-3 не меняется (подъём 08:00; (18:00 − 08:00) − 3:00 сна).
    assert lines[2:] == [
        "Подъем: 08:00",
        "Заснул: 15:00",
        "Проснулся: 18:00",
        "Дневной сон: 3 ч",
        "Бодрствование общее: 7 ч",
    ]


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


def test_latest_wake_line_removed_from_answer(db_path, conn, frozen_now):
    """TC-EVNOW-006 (волна 6, решение продакта 29.10, PB-3): строка
    «Крайнее пробуждение: …» в подтверждении засыпания НЕ выводится
    ВОВСЕ — даже при заданной настройке wake_deadline (16:00): норма
    постоянна, остаётся только в настройках (Пакет 3) и подсказках
    Пакета 2. Вместо неё — сводка событий дня с «Бодрствование общее».
    Время зафиксировано: 9:00 UTC = 12:00 МСК."""
    user_id = _uid(conn)  # внутренний id пользователя telegram_id=1001
    SettingsService(conn).seed_defaults(user_id)
    SettingsRepo(conn).upsert(user_id, "wake_deadline", "16:00")

    _press(db_path, "event:sleep")
    update, _ = _press(
        db_path, CALLBACK_TIME_NOW, pending="sleep"
    )

    answer = update.effective_message.replies[0]
    assert "Крайнее пробуждение" not in answer
    assert "16:00" not in answer
    assert "Бодрствование общее: " in answer
    # Структура §7.5: подтверждение + «Сон идёт.» + сводка (детерминированно,
    # review-001-2.3 major 1: время — из фикстуры, не из реального now).
    assert answer.splitlines()[0] == "✅ Засыпание записано в 12:00."
    assert answer.splitlines()[1] == "Сон идёт."
    assert answer.splitlines()[-1] == "Бодрствование общее: 6 ч"


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
