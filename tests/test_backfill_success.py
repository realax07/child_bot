# Юнит-тесты успешного ввода задним числом: §7.14 со сводкой без окон
# (change-пакет add-core-diary, задача 4.5).
#
# Критерии готовности 4.5 (FR-16, FR-19; design D6, D10; ТЗ §7.14; ОВ-ПО-2):
#   - ввод «вчера 22:30» → событие создано с is_backfilled=1;
#   - ответ §7.14: подтверждающая строка + обновлённая сводка за день
#     (суммарные сон/бодрствование), строки окон НЕ выводятся
#     (в тексте нет слова «Окно»);
#   - конфликтный путь «Изменить» (conflict:overwrite) тоже завершается §7.14;
#   - сводка считается по странице дня ВВОДИМОГО времени, не сегодняшней.
#
# Правило rule 6: каждый тест несёт TC-BFS-NNN в первой строке докстринга.

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src import texts
from src.db import init_db
from src.handlers import backfill, events
from src.handlers.events import PENDING_KIND_KEY, WAIT_EVENT_TIME_KEY
from src.repositories.sleep_events import SleepEventsRepo
from src.services.backfill_parser import resolve_tz
from src.services.event import get_user

TZ = ZoneInfo("Europe/Moscow")  # UTC+3, без перехода на летнее время

# callback_data кнопки «Изменить» §7.13 (контракт задачи 4.4, ТЗ §10.3).
CALLBACK_CONFLICT_OVERWRITE = "conflict:overwrite"


# ------------------------------------------------------------ Заглушки PTB
# (те же контракты, что в tests/test_backfill_dialog.py — задача 4.1)


class _FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
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


# --------------------------------------------------------------- Помощники


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "bot.db"


@pytest.fixture()
def conn(db_path):
    c = init_db(db_path)
    yield c
    c.close()


def _yesterday_at(hh: int, mm: int) -> datetime:
    """Вчера ЧЧ:ММ по локальному времени пользователя (TZ = Москва)."""
    day = (datetime.now(TZ) - timedelta(days=1)).date()
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def _utc_str(local_dt: datetime) -> str:
    """Локальный момент → строка UTC БД «YYYY-MM-DD HH:MM:SS» (design D4)."""
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _local_hhmm(local_dt: datetime) -> str:
    return local_dt.astimezone(resolve_tz("Europe/Moscow")).strftime("%H:%M")


def _insert_event(conn, user_id: int, kind: str, local_dt: datetime) -> int:
    return SleepEventsRepo(conn).insert(user_id, kind, _utc_str(local_dt))


def _send_text(db_path, text: str, user_data: dict, telegram_id: int = 5001):
    """Провести текстовое сообщение через реальный обработчик backfill."""
    update = _FakeTextUpdate(telegram_id, text)
    context = _FakeContext(db_path)
    context.user_data = user_data
    asyncio.run(backfill.backfill_time_message(update, context))  # type: ignore[arg-type]
    return update, context


def _press_overwrite(db_path, telegram_id: int, user_data: dict):
    """Провести нажатие «Изменить» §7.13 через реальный обработчик."""
    update = _FakeCallbackUpdate(telegram_id, CALLBACK_CONFLICT_OVERWRITE)
    context = _FakeContext(db_path)
    context.user_data = user_data
    asyncio.run(backfill.conflict_callback(update, context))  # type: ignore[arg-type]
    return update, context


# ------------------------------------------------------------------ Тесты


def test_tc_bfs_001_backfill_creates_event_and_answers_7_14(db_path, conn):
    """TC-BFS-001 (FR-16, FR-19, задача 4.5): ввод «вчера 22:30» → событие
    с is_backfilled=1 и ответ §7.14: подтверждающая строка + сводка
    сон/бодрствование, БЕЗ слова «Окно»; фазы диалога сняты."""
    when = _yesterday_at(22, 30)
    user_data = {
        WAIT_EVENT_TIME_KEY: True,
        PENDING_KIND_KEY: "sleep",
    }
    update, context = _send_text(db_path, "вчера 22:30", user_data)

    # Ответ §7.14: подтверждение с локальным временем + сводка, без окон.
    assert len(update.effective_message.replies) == 1
    answer = update.effective_message.replies[0]
    assert f"✅ Событие записано на {_local_hhmm(when)}." in answer
    assert "Обновлённая сводка за день:" in answer
    assert "Сон:" in answer
    assert "Бодрствование:" in answer
    assert "Окно" not in answer

    # РОВНО одно событие, создано задним числом (is_backfilled=1).
    user = get_user(conn, 5001)
    rows = SleepEventsRepo(conn).list_by_user(user["id"])
    assert len(rows) == 1
    assert rows[0]["event_type"] == "sleep"
    assert rows[0]["timestamp"] == _utc_str(when)
    assert rows[0]["is_backfilled"] is True

    # Обе фазы диалога сняты (design D10).
    assert WAIT_EVENT_TIME_KEY not in context.user_data
    assert PENDING_KIND_KEY not in context.user_data


def test_tc_bfs_002_conflict_overwrite_answers_7_14(db_path, conn):
    """TC-BFS-002 (FR-18 + FR-19, задача 4.5): конфликтный путь «Изменить»
    → уведомление о перезаписи (ОВ-ПО-1) И §7.14 со сводкой без «Окно»;
    перезапись без дублей; фазы очищены."""
    user = get_user(conn, 5002)
    wake_at = datetime.now(TZ) - timedelta(hours=2)
    wake_id = _insert_event(conn, user["id"], "wake", wake_at)

    user_data = {
        PENDING_KIND_KEY: "sleep",
        backfill.CONFIRM_OVERWRITE_KEY: {
            "event_id": wake_id,
            "when_utc": _utc_str(wake_at),
        },
    }
    update, context = _press_overwrite(db_path, 5002, user_data)

    # Уведомление ОВ-ПО-1, затем §7.14 со сводкой (без слова «Окно»).
    assert len(update.effective_message.replies) == 2
    assert update.effective_message.replies[0] == texts.BACKFILL_OVERWRITTEN.format(
        label=texts.KIND_LABELS["sleep"],
        local=_local_hhmm(wake_at),
    )
    summary = update.effective_message.replies[1]
    assert f"✅ Событие записано на {_local_hhmm(wake_at)}." in summary
    assert "Обновлённая сводка за день:" in summary
    assert "Сон:" in summary
    assert "Бодрствование:" in summary
    assert "Окно" not in summary

    # РОВНО одно событие — перезаписано видом/временем нового ввода.
    rows = SleepEventsRepo(conn).list_by_user(user["id"])
    assert len(rows) == 1
    assert rows[0]["id"] == wake_id
    assert rows[0]["event_type"] == "sleep"
    assert rows[0]["timestamp"] == _utc_str(wake_at)

    # Фазы диалога полностью очищены.
    for key in (backfill.CONFIRM_OVERWRITE_KEY, PENDING_KIND_KEY, WAIT_EVENT_TIME_KEY):
        assert key not in context.user_data


def test_tc_bfs_003_summary_uses_entered_time_day_page(db_path, conn):
    """TC-BFS-003 (FR-19, design D8, задача 4.5): сводка считается по
    странице дня ВВОДИМОГО времени (вчера), не сегодняшней: незакрытый сон
    с 21:00 вчера покрывает вчера 21:00→06:00 (9 ч окна), бодрствование
    страницы «вчера» = 24 ч − 9 ч = 15 ч; ввод «вчера 20:00» (wake ДО
    начала сна — без конфликта, D11: пересечения с интервалом нет)."""
    user = get_user(conn, 5003)
    _insert_event(conn, user["id"], "sleep", _yesterday_at(21, 0))

    user_data = {
        WAIT_EVENT_TIME_KEY: True,
        PENDING_KIND_KEY: "wake",
    }
    update, context = _send_text(
        db_path, "вчера 20:00", user_data, telegram_id=5003
    )

    # Ввод принят: ровно один ответ §7.14.
    assert len(update.effective_message.replies) == 1
    answer = update.effective_message.replies[0]
    assert f"✅ Событие записано на {_local_hhmm(_yesterday_at(20, 0))}." in answer
    assert "Окно" not in answer

    # Событие записано с is_backfilled=1.
    rows = SleepEventsRepo(conn).list_by_user(user["id"])
    assert len(rows) == 2
    backfilled = [r for r in rows if r["is_backfilled"]]
    assert len(backfilled) == 1
    assert backfilled[0]["event_type"] == "wake"
    assert backfilled[0]["timestamp"] == _utc_str(_yesterday_at(20, 0))

    # Числа сводки — страницы «вчера» (окно 06:00→06:00): бодрствование
    # 15 ч (24 ч окна минус 9 ч сна 21:00→06:00). Сегодняшняя страница
    # дала бы другое бодрствование; точное «Сон» зависит от момента
    # расчёта (незакрытый сон) — проверяется только наличие строки.
    assert "Бодрствование: 15 ч." in answer
    assert "Сон:" in answer
    # Фазы сняты.
    assert WAIT_EVENT_TIME_KEY not in context.user_data
    assert PENDING_KIND_KEY not in context.user_data
