# Юнит-тесты конфликтов ввода задним числом: §7.13, «Изменить»/«Отменить»
# (change-пакет add-core-diary, задача 4.4).
#
# Критерии готовности 4.4 (FR-18; design D11; ТЗ §7.13; ОВ-ПО-1):
#   - wake 13:30 записан, ввод sleep на 13:30 → §7.13 с кнопками
#     [Изменить] (conflict:overwrite) / [Отменить] (conflict:cancel),
#     целевое событие в фазе CONFIRM_OVERWRITE (design D10);
#   - «Изменить» → атомарная перезапись: ровно одно событие с новым
#     типом/временем, без дублей + уведомление (ОВ-ПО-1);
#   - «Отменить» → данные идентичны до ввода, фазы очищены;
#   - два пересечения → перезаписано БЛИЖАЙШЕЕ конфликтующее (D11);
#   - нарушение порядка sleep/wake → перезаписано ПОСЛЕДНЕЕ нарушающее (D11);
#   - observer — отказ (design D13), данные не меняются.
#
# Правило rule 6: каждый тест несёт TC-CFL-NNN в первой строке докстринга.

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

# callback_data кнопок §7.13 (контракт задачи 4.4, ТЗ §10.3).
CALLBACK_CONFLICT_OVERWRITE = "conflict:overwrite"
CALLBACK_CONFLICT_CANCEL = "conflict:cancel"

# ------------------------------------------------------------ Заглушки PTB
# (те же контракты, что в tests/test_backfill_dialog.py — задача 4.1)


class _FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.reply_markup = None
        self.replies: list[str] = []

    async def reply_text(self, reply, reply_markup=None):
        self.replies.append(reply)
        if reply_markup is not None:
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


# --------------------------------------------------------------- Помощники


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "bot.db"


@pytest.fixture()
def conn(db_path):
    c = init_db(db_path)
    yield c
    c.close()


def _past_local(hours_ago: float) -> datetime:
    """Момент в прошлом (локальное время пользователя, TZ = Москва)."""
    return datetime.now(TZ) - timedelta(hours=hours_ago)


def _input_text(local_dt: datetime) -> str:
    """Ввод парсеру в формате «DD.MM HH:MM» (без привязки к «сегодня»)."""
    return f"{local_dt.day:02d}.{local_dt.month:02d} {local_dt:%H:%M}"


def _utc_str(local_dt: datetime) -> str:
    """Локальный момент → строка UTC БД «YYYY-MM-DD HH:MM:SS» (design D4)."""
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _insert_event(conn, user_id: int, kind: str, local_dt: datetime) -> int:
    return SleepEventsRepo(conn).insert(user_id, kind, _utc_str(local_dt))


def _events(conn, user_id: int) -> list[dict]:
    return SleepEventsRepo(conn).list_by_user(user_id)


def _send_text(db_path, text: str, user_data: dict, telegram_id: int = 4001):
    update = _FakeTextUpdate(telegram_id, text)
    context = _FakeContext(db_path)
    context.user_data = user_data
    asyncio.run(backfill.backfill_time_message(update, context))  # type: ignore[arg-type]
    return update, context


def _press_conflict(
    db_path, data: str, user_data: dict, telegram_id: int = 4001
):
    update = _FakeCallbackUpdate(telegram_id, data)
    context = _FakeContext(db_path)
    context.user_data = user_data
    asyncio.run(backfill.conflict_callback(update, context))  # type: ignore[arg-type]
    return update, context


def _phase(kind: str = "sleep") -> dict:
    """Фазы диалога на момент ввода времени (задача 4.1 + 4.2)."""
    return {WAIT_EVENT_TIME_KEY: True, PENDING_KIND_KEY: kind}


# ------------------------------------------------------------------ Тесты


def test_tc_cfl_001_conflict_shows_7_13_with_buttons(db_path, conn):
    """TC-CFL-001 (FR-18, сценарий §7.13): wake 13:30 записан, ввод sleep
    на 13:30 → §7.13 с кнопками [Изменить]/[Отменить]; целевое событие
    (wake) в фазе CONFIRM_OVERWRITE; записей не прибавилось."""
    user = get_user(conn, 4001)
    # Секунды обнулены: ввод «DD.MM HH:MM» парсится до целой минуты, а
    # сценарий §7.13 — точное совпадение «Проснулся в 13:30» / «Заснул 13:30».
    wake_at = _past_local(2).replace(second=0, microsecond=0)
    wake_id = _insert_event(conn, user["id"], "wake", wake_at)

    user_data = _phase("sleep")
    update, context = _send_text(db_path, _input_text(wake_at), user_data)

    # §7.13 дословно о целевом событии («Проснулся» в 13:30-стиль).
    assert update.effective_message.replies == [
        texts.backfill_conflict_text(
            texts.KIND_LABELS["wake"],
            wake_at.astimezone(resolve_tz(TZ)).strftime("%H:%M"),
        )
    ]
    assert texts.BACKFILL_CONFLICT.startswith("⚠️ Это время конфликтует")
    # Кнопки §7.13: [Изменить] / [Отменить] с callback_data задачи 4.4.
    markup = update.effective_message.reply_markup
    rows = [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]
    assert rows == [
        [(texts.BTN_CONFLICT_OVERWRITE, CALLBACK_CONFLICT_OVERWRITE)],
        [(texts.BTN_CONFLICT_CANCEL, CALLBACK_CONFLICT_CANCEL)],
    ]
    # Фаза разбора снята; целевое событие — в фазе подтверждения (D10).
    assert WAIT_EVENT_TIME_KEY not in context.user_data
    assert context.user_data[PENDING_KIND_KEY] == "sleep"
    assert context.user_data[backfill.CONFIRM_OVERWRITE_KEY] == {
        "event_id": wake_id,
        "when_utc": _utc_str(wake_at),
    }
    # Дубль не создан: запись по-прежнему одна.
    assert len(_events(conn, user["id"])) == 1


def test_tc_cfl_002_overwrite_single_event_new_kind_and_time(db_path, conn):
    """TC-CFL-002 (FR-18, «Изменить»): перезапись целевого события — ровно
    одно событие с новым типом/временем (без дублей) + уведомление
    (ОВ-ПО-1); фазы очищены."""
    user = get_user(conn, 4002)
    wake_at = _past_local(2)
    wake_id = _insert_event(conn, user["id"], "wake", wake_at)
    new_sleep = _past_local(2)  # тот же момент — сценарий §7.13

    user_data = {
        PENDING_KIND_KEY: "sleep",
        backfill.CONFIRM_OVERWRITE_KEY: {
            "event_id": wake_id,
            "when_utc": _utc_str(new_sleep),
        },
    }
    update, context = _press_conflict(
        db_path, CALLBACK_CONFLICT_OVERWRITE, user_data, telegram_id=4002
    )

    # Уведомление о перезаписи (ОВ-ПО-1).
    assert update.effective_message.replies == [
        texts.BACKFILL_OVERWRITTEN.format(
            label=texts.KIND_LABELS["sleep"],
            local=new_sleep.astimezone(resolve_tz(TZ)).strftime("%H:%M"),
        )
    ]
    # РОВНО одно событие, с типом и временем НОВОГО ввода (без дублей).
    after = _events(conn, user["id"])
    assert len(after) == 1
    assert after[0]["id"] == wake_id
    assert after[0]["event_type"] == "sleep"
    assert after[0]["timestamp"] == _utc_str(new_sleep)
    # Фазы диалога полностью очищены (включая PENDING_KIND_KEY).
    for key in (
        backfill.CONFIRM_OVERWRITE_KEY,
        PENDING_KIND_KEY,
        WAIT_EVENT_TIME_KEY,
    ):
        assert key not in context.user_data


def test_tc_cfl_003_cancel_keeps_data_identical(db_path, conn):
    """TC-CFL-003 (FR-18, «Отменить»): данные идентичны до ввода, новое
    событие не создано, фазы очищены."""
    user = get_user(conn, 4003)
    wake_at = _past_local(2)
    wake_id = _insert_event(conn, user["id"], "wake", wake_at)
    before = _events(conn, user["id"])

    user_data = {
        PENDING_KIND_KEY: "sleep",
        backfill.CONFIRM_OVERWRITE_KEY: {
            "event_id": wake_id,
            "when_utc": _utc_str(wake_at),
        },
    }
    update, context = _press_conflict(
        db_path, CALLBACK_CONFLICT_CANCEL, user_data, telegram_id=4003
    )

    # Подтверждение отмены; данные не изменились ни в чём.
    assert update.effective_message.replies == [texts.BACKFILL_CONFLICT_CANCELLED]
    assert _events(conn, user["id"]) == before
    for key in (
        backfill.CONFIRM_OVERWRITE_KEY,
        PENDING_KIND_KEY,
        WAIT_EVENT_TIME_KEY,
    ):
        assert key not in context.user_data


def test_tc_cfl_004_two_overlaps_overwrite_nearest(db_path, conn):
    """TC-CFL-004 (design D11): два пересечения → перезаписано БЛИЖАЙШЕЕ
    конфликтующее по времени событие (детерминированный выбор).

    Данные: sleep −5ч, sleep −3ч, wake −2ч → интервалы [−5ч; −2ч] и
    [−3ч; сейчас]. Ввод wake на −2ч50м пересекает оба; ближайшее —
    sleep −3ч (10 мин), он и перезаписан."""
    user = get_user(conn, 4004)
    s1 = _past_local(5)
    s2 = _past_local(3)
    w = _past_local(2)
    id1 = _insert_event(conn, user["id"], "sleep", s1)
    id2 = _insert_event(conn, user["id"], "sleep", s2)
    id3 = _insert_event(conn, user["id"], "wake", w)
    moment = _past_local(2 + 50 / 60)

    user_data = {
        PENDING_KIND_KEY: "wake",
        backfill.CONFIRM_OVERWRITE_KEY: {
            "event_id": id2,
            "when_utc": _utc_str(moment),
        },
    }
    update, context = _press_conflict(
        db_path, CALLBACK_CONFLICT_OVERWRITE, user_data, telegram_id=4004
    )
    after = {ev["id"]: ev for ev in _events(conn, user["id"])}
    assert set(after) == {id1, id2, id3}  # без дублей — ровно три записи
    # Ближайшее (sleep −3ч) перезаписано новым типом/временем…
    assert after[id2]["event_type"] == "wake"
    assert after[id2]["timestamp"] == _utc_str(moment)
    # …остальные события не тронуты.
    assert after[id1]["event_type"] == "sleep"
    assert after[id1]["timestamp"] == _utc_str(s1)
    assert after[id3]["event_type"] == "wake"
    assert after[id3]["timestamp"] == _utc_str(w)
    assert PENDING_KIND_KEY not in context.user_data
    assert backfill.CONFIRM_OVERWRITE_KEY not in context.user_data


def test_tc_cfl_005_order_violation_overwrite_last_violating(db_path, conn):
    """TC-CFL-005 (design D11): нарушение порядка sleep/wake → перезаписано
    ПОСЛЕДНЕЕ событие, приводящее к нарушению.

    Данные: sleep −5ч, wake −4ч, sleep −3ч. Ввод wake на −3.5ч: пересечений
    нет, но порядок wake → wake нарушен; нарушающее — wake −4ч (последнее),
    он и перезаписан.»"""
    user = get_user(conn, 4005)
    s1 = _past_local(5)
    w1 = _past_local(4)
    s2 = _past_local(3)
    id1 = _insert_event(conn, user["id"], "sleep", s1)
    id2 = _insert_event(conn, user["id"], "wake", w1)
    id3 = _insert_event(conn, user["id"], "sleep", s2)
    moment = _past_local(3.5)

    user_data = {
        PENDING_KIND_KEY: "wake",
        backfill.CONFIRM_OVERWRITE_KEY: {
            "event_id": id2,
            "when_utc": _utc_str(moment),
        },
    }
    update, context = _press_conflict(
        db_path, CALLBACK_CONFLICT_OVERWRITE, user_data, telegram_id=4005
    )

    assert update.effective_message.replies
    after = {ev["id"]: ev for ev in _events(conn, user["id"])}
    assert set(after) == {id1, id2, id3}
    # Последнее нарушающее (wake −4ч) перезаписано новым вводом…
    assert after[id2]["event_type"] == "wake"
    assert after[id2]["timestamp"] == _utc_str(moment)
    # …сны не тронуты; хронология sleep → wake → sleep корректна.
    assert after[id1]["timestamp"] == _utc_str(s1)
    assert after[id3]["timestamp"] == _utc_str(s2)
    kinds = [ev["event_type"] for ev in sorted(after.values(), key=lambda e: e["timestamp"])]
    assert kinds == ["sleep", "wake", "sleep"]
    assert PENDING_KIND_KEY not in context.user_data


def test_tc_cfl_006_observer_overwrite_denied(db_path, conn):
    """TC-CFL-006 (design D13): observer жмёт «Изменить» → отказ, данные
    не меняются, фазы очищены."""
    user = get_user(conn, 4006)
    conn.execute("UPDATE users SET role = 'observer' WHERE id = ?", (user["id"],))
    conn.commit()
    wake_at = _past_local(2)
    wake_id = _insert_event(conn, user["id"], "wake", wake_at)
    before = _events(conn, user["id"])

    user_data = {
        PENDING_KIND_KEY: "sleep",
        backfill.CONFIRM_OVERWRITE_KEY: {
            "event_id": wake_id,
            "when_utc": _utc_str(wake_at),
        },
    }
    update, context = _press_conflict(
        db_path, CALLBACK_CONFLICT_OVERWRITE, user_data, telegram_id=4006
    )

    # Отказ наблюдателю (тот же текст, что у событийных кнопок, design D13).
    assert update.callback_query.answers == [events.OBSERVER_DENIED]
    assert update.effective_message.replies == []
    assert _events(conn, user["id"]) == before
    for key in (
        backfill.CONFIRM_OVERWRITE_KEY,
        PENDING_KIND_KEY,
        WAIT_EVENT_TIME_KEY,
    ):
        assert key not in context.user_data


def test_tc_cfl_007_no_conflict_confirms_without_event(db_path, conn):
    """TC-CFL-007 (FR-18, негативный): без конфликта §7.13 не показывается;
    до задачи 4.5 — подтверждение 4.2, записи ещё нет (is_backfilled
    появится там)."""
    user = get_user(conn, 4007)
    moment = _past_local(2)  # событий нет — конфликта нет

    update, context = _send_text(db_path, _input_text(moment), _phase("sleep"))

    assert len(update.effective_message.replies) == 1
    assert "принято" in update.effective_message.replies[0]
    assert backfill.CONFIRM_OVERWRITE_KEY not in context.user_data
    assert _events(conn, user["id"]) == []
