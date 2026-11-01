"""Экран «История за неделю» — задача 2.1 пакета add-settings-history.

Handler history.py (design D22; ТЗ §4.8, §7.20–7.22, §10.2–10.3; spec
weekly-history FR-48…FR-50): ответ §7.20 — строки 7 завершённых страниц
дня (новые → старые) + блок сегодня; отметки ✓/⚠️ по границам
пользователя (FR-49); пустой сегодня → §7.21 без «0 ч», неполные/старые
данные → §7.22 со временем последнего события (FR-50); кнопка «📊
История за неделю» (callback history:week), команда /history и текст
reply-панели — один ответ; кнопка НЕ пишет в sleep_events; наблюдатель
видит только СВОИ данные (design D13, FR-47 — выборка по своему user_id).

Каждый тест помечен TC-HIST-NNN в docstring (flow_check rule 6).
Время фиксировано (frozen_now, monkeypatch history.datetime):
09:30 UTC = 12:30 МСК 03.11.2026 (сегодня — страница 03.11, вт);
данные недели — фиксированные события 31.10–03.11.2026 (UTC+3).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src import texts
from src.db import init_db
from src.handlers import common, history
from src.handlers.history import CALLBACK_PATTERN
from src.repositories.sleep_events import SleepEventsRepo
from src.services.event import get_user
from src.services.state import BTN_HISTORY, CALLBACK_HISTORY

# Фиксированное «сейчас» обработчика (Европа/Москва = UTC+3):
# 09:30 UTC 03.11.2026 = 12:30 МСК (бодрствующий, страница 03.11 — вт).
FIXED_NOW_UTC = datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc)

HEADER = texts.HISTORY_WEEK_HEADER

# Неделя с 2+ днями фактов (времена UTC; локальные = UTC+3):
#   31.10 (сб): ночной отрезок 22:40 → 01.11 09:00 (факт отбоя страницы);
#   01.11 (вс): подъём 09:00, сон 13:00–14:30, отбой 22:40 (⚠️ — позже 22:30);
#   02.11 (пн): подъём 08:00, сон 13:30–15:00, отбой 22:10 (✓);
#   03.11 (сегодня): пробуждение 08:00, дальше бодрствует (4 ч 30 мин к 12:30).
WEEK_EVENTS_UTC = (
    ("2026-10-31 19:40:00", "sleep"),  # 31.10 22:40 — отбой страницы 31.10
    ("2026-11-01 06:00:00", "wake"),  # 01.11 09:00 — подъём
    ("2026-11-01 10:00:00", "sleep"),  # 01.11 13:00
    ("2026-11-01 11:30:00", "wake"),  # 01.11 14:30
    ("2026-11-01 19:40:00", "sleep"),  # 01.11 22:40 — отбой (позже 22:30)
    ("2026-11-02 05:00:00", "wake"),  # 02.11 08:00 — подъём
    ("2026-11-02 10:30:00", "sleep"),  # 02.11 13:30
    ("2026-11-02 12:10:00", "wake"),  # 02.11 15:10
    ("2026-11-02 19:10:00", "sleep"),  # 02.11 22:10 — отбой
    ("2026-11-03 05:00:00", "wake"),  # 03.11 08:00 — подъём (сегодня)
)

# Полный ожидаемый ответ §7.20 + блок сегодня (TC-HIST-001).
WEEK_SCREEN = "\n".join(
    [
        HEADER,
        "",
        "- 02.11 (пн): бодрствование 12 ч 30 мин, сон 13:30–15:10, ночь 22:10 ✓",
        "- 01.11 (вс): бодрствование 12 ч 10 мин, сон 13:00–14:30, ночь 22:40 ⚠️",
        "- 31.10 (сб): бодрствование 16 ч 40 мин, ночь 22:40 ⚠️",
        "",
        "- 03.11 (вт): бодрствование 4 ч 30 мин",
    ]
)


# ------------------------------------------------------------ Заглушки PTB


class _FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)


class _FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.answers: list[str | None] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)


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


def _frozen(moment_utc: datetime) -> type:
    """Класс datetime с замороженным now() на момент moment_utc."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is not None:
                return moment_utc.astimezone(tz)
            return moment_utc.replace(tzinfo=None)

    return _FrozenDatetime


# --------------------------------------------------------------- Фикстуры


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "bot.db"


@pytest.fixture()
def conn(db_path):
    c = init_db(db_path)
    yield c
    c.close()


@pytest.fixture()
def frozen_now(monkeypatch):
    """Фиксировать «сейчас» обработчика history (правило фикс. времени)."""
    monkeypatch.setattr(history, "datetime", _frozen(FIXED_NOW_UTC))


def _uid(conn, telegram_id: int) -> int:
    return get_user(conn, telegram_id)["id"]


def _seed(conn, telegram_id: int, events=WEEK_EVENTS_UTC) -> int:
    user_id = _uid(conn, telegram_id)
    for ts, kind in events:
        SleepEventsRepo(conn).insert(user_id, kind, ts)
    return user_id


def _count(conn, user_id: int) -> int:
    return len(SleepEventsRepo(conn).list_by_user(user_id))


def _press(db_path, telegram_id: int = 1001, data: str = CALLBACK_HISTORY):
    update = _FakeCallbackUpdate(telegram_id, data)
    asyncio.run(history.history_callback(update, _FakeContext(db_path)))  # type: ignore[arg-type]
    return update


def _command(db_path, telegram_id: int = 1001):
    update = _FakeTextUpdate(telegram_id, "/history")
    asyncio.run(history.history_command(update, _FakeContext(db_path)))  # type: ignore[arg-type]
    return update


def _panel(db_path, telegram_id: int = 1001):
    """Текст «📊 История за неделю» с reply-панели через общий вход
    reply_button_message (группа 1, BUG-002) — тот же маршрут, что у
    кнопок событий (ТЗ §10.3: двойной вход)."""
    update = _FakeTextUpdate(telegram_id, BTN_HISTORY)
    asyncio.run(
        common.reply_button_message(update, _FakeContext(db_path))  # type: ignore[arg-type]
    )
    return update


# ------------------------------------------------------------------- Тесты


def test_tc_hist_001_week_screen_7_20_two_days_newest_first(
    db_path, conn, frozen_now
):
    """TC-HIST-001 (FR-48): экран §7.20 за неделю с 2+ днями фактов —
    строки дней новые → старые, формат §7.20 (бодрствование, сон, ночь,
    ✓/⚠️), плюс блок сегодня; ⚠️ на дне с уходом 22:40 позже крайнего
    22:30 при бодрствовании в границах (сценарий spec «Поздний уход»)."""
    _seed(conn, 1001)
    update = _press(db_path)
    assert update.callback_query.message.replies == [WEEK_SCREEN]


def test_tc_hist_002_empty_week_and_today_7_21_no_zero(db_path, conn, frozen_now):
    """TC-HIST-002 (FR-50): пустая неделя и пустой сегодня → §7.21
    дословно, БЕЗ «0 ч» и без строк дней (только фактические дни)."""
    update = _press(db_path)
    assert update.callback_query.message.replies == [
        f"{HEADER}\n\n{texts.HISTORY_EMPTY_TODAY}"
    ]
    assert "0 ч" not in update.callback_query.message.replies[0]


def test_tc_hist_003_observer_sees_only_own_data(db_path, conn, frozen_now):
    """TC-HIST-003 (FR-47, D13): наблюдателю история доступна (чтение),
    но события ДРУГОГО пользователя в его ответе не появляются — только
    свои (пустые) данные; владелец видит свою неделю."""
    _seed(conn, 1001)
    observer_id = _uid(conn, 2002)
    conn.execute("UPDATE users SET role='observer' WHERE id=?", (observer_id,))
    conn.commit()

    other_update = _press(db_path, telegram_id=2002)
    other_reply = other_update.callback_query.message.replies[0]
    assert texts.HISTORY_EMPTY_TODAY in other_reply
    assert "02.11 (пн)" not in other_reply

    owner_update = _press(db_path, telegram_id=1001)
    assert owner_update.callback_query.message.replies == [WEEK_SCREEN]


def test_tc_hist_004_button_and_command_write_no_events(
    db_path, conn, frozen_now
):
    """TC-HIST-004 (state-ui spec): callback history:week, команда /history
    и текст панели «📊 История за неделю» НЕ пишут в sleep_events — один
    ответ §7.20, число событий не меняется."""
    _seed(conn, 1001)
    before = _count(conn, _uid(conn, 1001))

    pressed = _press(db_path)
    commanded = _command(db_path)
    pannel = _panel(db_path)
    assert pressed.callback_query.message.replies == [WEEK_SCREEN]
    assert commanded.message.replies == [WEEK_SCREEN]
    assert pannel.message.replies == [WEEK_SCREEN]
    assert pressed.callback_query.answers == [None]  # колбэк отвечен

    assert _count(conn, _uid(conn, 1001)) == before


def test_tc_hist_005_stale_today_7_22_with_last_event_time(
    db_path, conn, monkeypatch
):
    """TC-HIST-005 (FR-50): подъём 07:15 и после — ничего до вечера
    (последнее событие — пробуждение дольше wake_total_max назад) →
    §7.22 дословно со временем последнего события 07:15 (сценарий spec
    «Устаревшие данные»); строка «сегодня» не рисуется."""
    monkeypatch.setattr(
        history, "datetime", _frozen(datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc))
    )  # 21:00 МСК
    _seed(
        conn,
        1001,
        events=(
            ("2026-11-02 19:00:00", "sleep"),  # 02.11 22:00 — отбой
            ("2026-11-03 04:15:00", "wake"),  # 03.11 07:15 — подъём
        ),
    )
    update = _press(db_path)
    assert update.callback_query.message.replies == [
        "\n".join(
            [
                HEADER,
                "",
                "- 02.11 (пн): бодрствование 16 ч, ночь 22:00 ⚠️",
                "",
                texts.history_stale_text("07:15"),
            ]
        )
    ]
    # Эталон §7.22 — дословная формулировка ТЗ (подставлено только время).
    assert texts.HISTORY_STALE_TODAY == texts.history_stale_text("07:15")


def test_tc_hist_006_day_without_bedtime_has_no_night_line(
    db_path, conn, frozen_now
):
    """TC-HIST-006 (FR-49, research §7.20): день без факта отбоя — строки
    «ночь …» нет («строка только при наличии факта»); нарушение верхней
    границы бодрствования без ухода — всё равно ⚠️ («хотя бы одна
    граница нарушена»)."""
    _seed(
        conn,
        1001,
        events=(
            ("2026-11-01 19:40:00", "sleep"),  # 01.11 22:40 — отбой 01.11
            ("2026-11-02 05:00:00", "wake"),  # 02.11 08:00 — подъём
            ("2026-11-02 10:30:00", "sleep"),  # 02.11 13:30
            ("2026-11-02 12:00:00", "wake"),  # 02.11 15:00
            # Отбоя 02.11 нет — ночных событий после 15:00 не отмечено.
        ),
    )
    update = _press(db_path)
    reply = update.callback_query.message.replies[0]
    day_line = "- 02.11 (пн): бодрствование 20 ч 30 мин, сон 13:30–15:00 ⚠️"
    assert day_line in reply
    assert "ночь" not in day_line
    # Блок сегодня: событий за 03.11 нет → §7.21.
    assert reply.endswith(texts.HISTORY_EMPTY_TODAY)


def test_tc_hist_007_out_of_upper_bound_day_7_20_shape(db_path, conn, frozen_now):
    """TC-HIST-007 (FR-49, сценарий spec «Строка дня недели»/«Выход за
    верхнюю границу»): бодрствование дня 12 ч 45 мин при верхней границе
    12,5 ч → строка §7.20 с ⚠️ (день 12 ч 45 мин / уход 22:40)."""
    _seed(
        conn,
        1001,
        events=(
            ("2026-11-01 19:40:00", "sleep"),  # 01.11 22:40 — отбой 01.11
            ("2026-11-02 05:25:00", "wake"),  # 02.11 08:25 — подъём
            ("2026-11-02 10:00:00", "sleep"),  # 02.11 13:00
            ("2026-11-02 11:30:00", "wake"),  # 02.11 14:30
            ("2026-11-02 19:40:00", "sleep"),  # 02.11 22:40 — отбой
            ("2026-11-03 05:00:00", "wake"),  # 03.11 08:00
        ),
    )
    update = _press(db_path)
    reply = update.callback_query.message.replies[0]
    # (22:40 − 08:25) − 1 ч 30 мин = 12 ч 45 мин > верхней границы 12,5 ч.
    assert (
        "- 02.11 (пн): бодрствование 12 ч 45 мин, сон 13:00–14:30, ночь 22:40 ⚠️"
        in reply
    )


def test_tc_hist_008_registration_history_week_pattern():
    """TC-HIST-008 (ТЗ §10.3): регистрация колбэка history:week — контракт
    callback_data из единого источника state.py; REGEXP покрывает ровно
    его (без чужих данных)."""
    import re

    assert CALLBACK_HISTORY == "history:week"
    assert re.match(CALLBACK_PATTERN, "history:week")
    assert not re.match(CALLBACK_PATTERN, "settings:show")
    assert not re.match(CALLBACK_PATTERN, "history:month")
