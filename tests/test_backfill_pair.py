# Юнит-тесты ПАРНОГО ввода задним числом — два шага диалога
# (change-пакет add-settings-history, задача 3.1; PB-1, ОВ-5.1).
#
# Критерии готовности 3.1 (FR-51, design D23; spec paired-backfill):
#   - пара 13:40/15:15 создаёт ДВА события (оба is_backfilled=1) и
#     ЕДИНЫЙ конфликт-чек вызывается РОВНО ОДИН раз на пару;
#   - пересечение пары с существующим интервалом → §7.13, событий НЕТ;
#   - wake <= sleep → отказ КАК КОНФЛИКТ (§7.13), событий нет;
#   - незакрытый sleep уже записан → спрашивается ТОЛЬКО пробуждение,
#     дубля засыпания нет (существующее поведение backfill);
#   - после пары план напоминаний пересчитан (точка EventService FR-41);
#   - «Отменить» в §7.13 пары гасит ВСЁ; «Изменить» переспрашивает пару
#     с шага 1;
#   - взаимное исключение фаз с настройками в ОБЕ стороны;
#   - глубина §7.15 — для ОБОИХ времён пары.
#
# 3.1-fix (review-001-3.1 major №1 + minor №2):
#   - атомарность: отказ второй вставки → rollback, в БД НЕТ полупары,
#     ответ пользователю НЕ пуст (TC-PBACK-011);
#   - будущее время (FR-20) на шаге 1 (TC-PBACK-012) и шаге 2
#     (TC-PBACK-013) — предупреждение, фаза шага сохранена.
#
# Правило rule 6: каждый тест несёт TC-PBACK-NNN в первой строке
# докстринга. Время зафиксировано в фикстуре frozen_now: 8:00 МСК 10.10
# (обёртка parse_time_input + events.datetime — как в test_backfill_success,
# правило волны 6: без «живых» ЧЧ:ММ).

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src import texts
from src.db import init_db
from src.handlers import backfill, events, settings_screen
from src.handlers.events import (
    PAIR_ASK_WAKE,
    PENDING_KIND_KEY,
    WAIT_EVENT_TIME_KEY,
    WAIT_PAIR_SLEEP_KEY,
    WAIT_PAIR_WAKE_KEY,
)
from src.handlers.settings_screen import WAIT_SETTING_VALUE
from src.repositories.sleep_events import SleepEventsRepo
from src.services import scheduler as scheduler_mod
from src.services.backfill_parser import resolve_tz
from src.services.event import detect_pair_conflict, get_user

TZ = ZoneInfo("Europe/Moscow")  # UTC+3, без перехода на летнее время

# Фиксированный «сейчас» диалога: 8:00 МСК 10.10 (05:00 UTC).
FIXED_NOW_LOCAL = datetime(2026, 10, 10, 8, 0, tzinfo=TZ)

# callback_data кнопок §7.13 (контракт задачи 4.4 / 3.1).
CALLBACK_CONFLICT_OVERWRITE = "conflict:overwrite"
CALLBACK_CONFLICT_CANCEL = "conflict:cancel"


class _FrozenDatetime(datetime):
    """datetime с замороженным now() — время теста фиксируется в фикстуре."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        if tz is not None:
            return FIXED_NOW_LOCAL.astimezone(tz)
        return FIXED_NOW_LOCAL.replace(tzinfo=None)


@pytest.fixture()
def frozen_now(monkeypatch):
    """Фиксировать «сейчас» для теста (правило волны 6).

    events.datetime — замороженный класс (расчётный момент сводки §7.14);
    parse_time_input в backfill — обёртка с фиксированным моментом ввода
    (сам класс backfill.datetime НЕ подменяется: isinstance-guard задачи
    4.2 — см. fixedNow-фикстуру test_backfill_success).
    """
    monkeypatch.setattr(events, "datetime", _FrozenDatetime)
    real_parse = backfill.parse_time_input
    monkeypatch.setattr(
        backfill,
        "parse_time_input",
        lambda text, tz, now, max_age_days=None: real_parse(
            text, tz, _FrozenDatetime.now(timezone.utc), max_age_days=max_age_days
        ),
    )


# ------------------------------------------------------------ Заглушки PTB


class _FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list[str] = []
        self.reply_markup = None

    async def reply_text(self, reply, reply_markup=None):
        self.replies.append(reply)
        self.reply_markup = reply_markup


class _FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.answers: list[str | None] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None):
        self.message.replies.append(text)
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


class _FakeScheduler:
    """Счётчик вызовов точки ре-шедула (FR-41) вместо живого APScheduler."""

    def __init__(self) -> None:
        self.reschedule_calls = 0

    def reschedule(self) -> None:
        self.reschedule_calls += 1


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
    """Вчера ЧЧ:ММ локально относительно фиксированного «сейчас»."""
    day = (FIXED_NOW_LOCAL - timedelta(days=1)).date()
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def _utc_str(local_dt: datetime) -> str:
    """Локальный момент → строка UTC БД «YYYY-MM-DD HH:MM:SS» (design D4)."""
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _local_hhmm(local_dt: datetime) -> str:
    return local_dt.astimezone(resolve_tz("Europe/Moscow")).strftime("%H:%M")


def _days_ago_text(days: int) -> str:
    """Ввод «DD.MM HH:MM» для момента days дней назад (детерминированно
    при фиксированном «сейчас» 8:00 МСК 10.10)."""
    target = FIXED_NOW_LOCAL - timedelta(days=days)
    return f"{target.day:02d}.{target.month:02d} 07:15"


def _press_callback(db_path, data: str, user_data: dict, telegram_id: int = 7001):
    """Провести нажатие event:*/time:*/conflict:* через реальный обработчик."""
    update = _FakeCallbackUpdate(telegram_id, data)
    context = _FakeContext(db_path)
    context.user_data = user_data
    if data.startswith("event:"):
        handler = events.event_button_callback
    elif data.startswith("conflict:"):
        handler = backfill.conflict_callback
    elif data.startswith("settings:"):
        handler = settings_screen.settings_callback
    else:
        handler = events.time_choose_callback
    asyncio.run(handler(update, context))  # type: ignore[arg-type]
    return update, context


def _send_text(db_path, text: str, user_data: dict, telegram_id: int = 7001):
    """Провести текстовое сообщение через реальный обработчик backfill."""
    update = _FakeTextUpdate(telegram_id, text)
    context = _FakeContext(db_path)
    context.user_data = user_data
    asyncio.run(backfill.backfill_time_message(update, context))  # type: ignore[arg-type]
    return update, context


def _run_pair_dialog(db_path, sleep_text: str, wake_text: str, telegram_id: int = 7001):
    """Полный UI-путь пары: «😴 Заснул» → «⏪ Ввести время вручную» →
    время засыпания → время пробуждения. Возвращает (update, context)."""
    _, context = _press_callback(db_path, "event:sleep", {}, telegram_id)
    _, context = _press_callback(
        db_path, "time:manual", context.user_data, telegram_id
    )
    _, context = _send_text(db_path, sleep_text, context.user_data, telegram_id)
    update, context = _send_text(
        db_path, wake_text, context.user_data, telegram_id
    )
    return update, context


def _event_rows(conn, telegram_id: int):
    user = get_user(conn, telegram_id)
    return SleepEventsRepo(conn).list_by_user(user["id"])


# ------------------------------------------------------------------ Тесты


def test_tc_pback_001_pair_creates_two_events_single_conflict_check(
    db_path, conn, frozen_now, monkeypatch
):
    """TC-PBACK-001 (FR-51, design D23; spec paired-backfill «Пара из двух
    шагов»): пара 13:40/15:15 → созданы ОБА события (is_backfilled=1),
    ЕДИНЫЙ конфликт-чек пары вызван РОВНО ОДИН раз, ответ §7.14-стиль
    (факт + сводка PB-3 по странице ввода); фазы диалога сняты.
    Время зафиксировано: 8:00 МСК 10.10."""
    calls = {"n": 0}
    real_check = detect_pair_conflict

    def _counting(conn_, user_id, sleep_utc, wake_utc):
        calls["n"] += 1
        return real_check(conn_, user_id, sleep_utc, wake_utc)

    monkeypatch.setattr(backfill, "detect_pair_conflict", _counting)

    # Шаг 1: «вчера 13:40» — событий ещё НЕТ, вопрос шага 2.
    _, context = _press_callback(db_path, "event:sleep", {})
    _, context = _press_callback(db_path, "time:manual", context.user_data)
    update, context = _send_text(db_path, "вчера 13:40", context.user_data)
    assert update.effective_message.replies == [PAIR_ASK_WAKE]
    assert _event_rows(conn, 7001) == []
    assert context.user_data.get(WAIT_PAIR_SLEEP_KEY) is None
    assert context.user_data.get(WAIT_PAIR_WAKE_KEY) == {
        "sleep_utc": _utc_str(_yesterday_at(13, 40))
    }

    # Шаг 2: «вчера 15:15» — ОБА события, §7.14-стиль.
    update, context = _send_text(db_path, "вчера 15:15", context.user_data)
    assert len(update.effective_message.replies) == 1
    answer = update.effective_message.replies[0]
    assert answer.startswith("✅ Событие записано на 15:15.")
    assert "Обновлённая сводка за день:" in answer
    assert answer.splitlines()[3:] == [
        "Заснул: 13:40",
        "Проснулся: 15:15",
        "Дневной сон: 1 ч 35 мин",
        # Ред. BUG-004: (08:00 10.10 − 06:00 09.10) − 1:35 дневного сна.
        "Бодрствование общее: 24 ч 25 мин",
    ]

    rows = _event_rows(conn, 7001)
    assert [(r["event_type"], r["timestamp"]) for r in rows] == [
        ("sleep", _utc_str(_yesterday_at(13, 40))),
        ("wake", _utc_str(_yesterday_at(15, 15))),
    ]
    assert all(r["is_backfilled"] for r in rows)

    # Единый конфликт-чек пары — ровно один вызов (D23).
    assert calls["n"] == 1
    # Фазы диалога сняты целиком.
    for key in (
        WAIT_EVENT_TIME_KEY,
        PENDING_KIND_KEY,
        WAIT_PAIR_SLEEP_KEY,
        WAIT_PAIR_WAKE_KEY,
    ):
        assert key not in context.user_data


def test_tc_pback_002_pair_overlap_conflict_no_events(db_path, conn, frozen_now):
    """TC-PBACK-002 (FR-51; spec paired-backfill «Конфликт пары»): пара
    13:40/15:15 пересекается с записанным сном 12:00–14:00 → §7.13 с
    [Изменить]/[Отмена] (цель — ближайшее событие «Проснулся 14:00»),
    НОВЫХ событий нет; фазы шагов сняты, пара ждёт решения."""
    user = get_user(conn, 7002)
    SleepEventsRepo(conn).insert(user["id"], "sleep", _utc_str(_yesterday_at(12, 0)))
    SleepEventsRepo(conn).insert(user["id"], "wake", _utc_str(_yesterday_at(14, 0)))

    _, context = _press_callback(db_path, "event:sleep", {}, 7002)
    _, context = _press_callback(db_path, "time:manual", context.user_data, 7002)
    _, context = _send_text(db_path, "вчера 13:40", context.user_data, 7002)
    update, context = _send_text(db_path, "вчера 15:15", context.user_data, 7002)

    # §7.13 с фактической целью (дословная формулировка ТЗ).
    assert len(update.effective_message.replies) == 1
    assert update.effective_message.replies[0] == texts.backfill_conflict_text(
        "Проснулся", "14:00"
    )
    assert update.callback_query is None  # ответ текстом, не колбэком
    assert update.effective_message.reply_markup is not None  # [Изменить]/[Отмена]

    # НОВЫХ событий нет (ровно два записанных ранее).
    rows = _event_rows(conn, 7002)
    assert len(rows) == 2
    # Фазы шагов сняты, пара — в фазе подтверждения.
    assert WAIT_PAIR_SLEEP_KEY not in context.user_data
    assert WAIT_PAIR_WAKE_KEY not in context.user_data
    assert context.user_data[backfill.CONFIRM_PAIR_KEY] == {
        "sleep_utc": _utc_str(_yesterday_at(13, 40)),
        "wake_utc": _utc_str(_yesterday_at(15, 15)),
    }


def test_tc_pback_003_wake_not_after_sleep_refused_as_conflict(
    db_path, conn, frozen_now
):
    """TC-PBACK-003 (FR-51, D23): wake == sleep (13:40 = 13:40) и
    wake < sleep (13:00 < 13:40) — отказ КАК КОНФЛИКТ: §7.13 с засыпанием
    пары как целью, событий нет."""
    for wake_hh, wake_mm in ((13, 40), (13, 0)):
        _, context = _press_callback(db_path, "event:sleep", {}, 7003)
        _, context = _press_callback(
            db_path, "time:manual", context.user_data, 7003
        )
        _, context = _send_text(db_path, "вчера 13:40", context.user_data, 7003)
        update, context = _send_text(
            db_path, f"вчера {wake_hh:02d}:{wake_mm:02d}", context.user_data, 7003
        )

        assert len(update.effective_message.replies) == 1
        assert update.effective_message.replies[0] == texts.backfill_conflict_text(
            "Заснул", "13:40"
        )
        assert _event_rows(conn, 7003) == []
        assert backfill.CONFIRM_PAIR_KEY in context.user_data
        # Чистим фазу подтверждения перед следующей итерацией.
        context.user_data.clear()


def test_tc_pback_004_open_sleep_asks_wake_only(db_path, conn, frozen_now):
    """TC-PBACK-004 (FR-51; spec paired-backfill «Засыпание уже записано»):
    незакрытый сон 13:40 уже записан → диалог спрашивает ТОЛЬКО время
    пробуждения (§7.11, без вопроса о засыпании),wake закрывает интервал
    штатно (BUG-001), дубля засыпания нет; парные фазы не заводятся."""
    user = get_user(conn, 7004)
    SleepEventsRepo(conn).insert(user["id"], "sleep", _utc_str(_yesterday_at(13, 40)))

    _, context = _press_callback(db_path, "event:wake", {}, 7004)
    update, context = _press_callback(
        db_path, "time:manual", context.user_data, 7004
    )
    # Только пробуждение: обычный запрос §7.11, БЕЗ «Когда заснул?»-пары.
    assert update.effective_message.replies == [texts.BACKFILL_ASK_TIME]
    assert context.user_data.get(WAIT_EVENT_TIME_KEY) is True
    assert WAIT_PAIR_SLEEP_KEY not in context.user_data

    update, context = _send_text(db_path, "вчера 15:15", context.user_data, 7004)
    assert len(update.effective_message.replies) == 1
    assert "✅ Событие записано на 15:15." in update.effective_message.replies[0]

    rows = _event_rows(conn, 7004)
    # Дубля засыпания нет: ровно один sleep + один wake.
    assert [r["event_type"] for r in rows] == ["sleep", "wake"]
    assert rows[1]["is_backfilled"] is True
    # Парные фазы не заводились.
    for key in (WAIT_PAIR_SLEEP_KEY, WAIT_PAIR_WAKE_KEY, backfill.CONFIRM_PAIR_KEY):
        assert key not in context.user_data


def test_tc_pback_005_pair_reschedules_reminders(db_path, conn, frozen_now):
    """TC-PBACK-005 (FR-41, design D10/D23; 3.1-fix): после записи пары
    план напоминаний пересчитан — точка EventService (scheduler.
    notify_events_changed → ReminderScheduler.reschedule) вызвана РОВНО
    ОДИН раз, ПОСЛЕ успешного коммита ПАРЫ (create_event_pair — атомарная
    транзакция; reschedule идемпотентен, design D19; при откате пары хук
    не срабатывает)."""
    fake = _FakeScheduler()
    scheduler_mod._set_active(fake)  # как в живом процессе (design D19)
    try:
        _, context = _press_callback(db_path, "event:sleep", {}, 7005)
        _, context = _press_callback(
            db_path, "time:manual", context.user_data, 7005
        )
        _, context = _send_text(db_path, "вчера 13:40", context.user_data, 7005)
        update, context = _send_text(
            db_path, "вчера 15:15", context.user_data, 7005
        )
    finally:
        scheduler_mod._set_active(None)

    assert len(_event_rows(conn, 7005)) == 2
    # 3.1-fix: ОДИН вызов на пару — после успешного коммита обеих вставок.
    assert fake.reschedule_calls == 1


def test_tc_pback_006_pair_conflict_cancel_clears_everything(
    db_path, conn, frozen_now
):
    """TC-PBACK-006 (FR-51, D11-семантика «Отменить»): конфликт пары на
    шаге 2 → «Отменить» гасит ВСЁ: подтверждение отмены, событий нет,
    ВСЕ фазы диалога сняты."""
    user = get_user(conn, 7006)
    SleepEventsRepo(conn).insert(user["id"], "sleep", _utc_str(_yesterday_at(12, 0)))
    SleepEventsRepo(conn).insert(user["id"], "wake", _utc_str(_yesterday_at(14, 0)))

    _, context = _press_callback(db_path, "event:sleep", {}, 7006)
    _, context = _press_callback(db_path, "time:manual", context.user_data, 7006)
    _, context = _send_text(db_path, "вчера 13:40", context.user_data, 7006)
    _, context = _send_text(db_path, "вчера 15:15", context.user_data, 7006)
    assert backfill.CONFIRM_PAIR_KEY in context.user_data  # §7.13 показан

    update, context = _press_callback(
        db_path, CALLBACK_CONFLICT_CANCEL, context.user_data, 7006
    )

    assert update.effective_message.replies == [texts.BACKFILL_CONFLICT_CANCELLED]
    assert len(_event_rows(conn, 7006)) == 2  # только записанные ранее
    for key in (
        WAIT_EVENT_TIME_KEY,
        PENDING_KIND_KEY,
        WAIT_PAIR_SLEEP_KEY,
        WAIT_PAIR_WAKE_KEY,
        backfill.CONFIRM_PAIR_KEY,
    ):
        assert key not in context.user_data


def test_tc_pback_007_pair_conflict_overwrite_restarts_step_one(
    db_path, conn, frozen_now
):
    """TC-PBACK-007 (FR-51, D23): «Изменить» в §7.13 ПАРЫ — пара
    переспрашивается с шага 1 (§7.11 дословно), фаза шага 1 + WAIT_EVENT_TIME
    снова активны, событий нет (перезапись одной записи пару не чинит)."""
    user = get_user(conn, 7007)
    SleepEventsRepo(conn).insert(user["id"], "sleep", _utc_str(_yesterday_at(12, 0)))
    SleepEventsRepo(conn).insert(user["id"], "wake", _utc_str(_yesterday_at(14, 0)))

    _, context = _press_callback(db_path, "event:sleep", {}, 7007)
    _, context = _press_callback(db_path, "time:manual", context.user_data, 7007)
    _, context = _send_text(db_path, "вчера 13:40", context.user_data, 7007)
    _, context = _send_text(db_path, "вчера 15:15", context.user_data, 7007)

    update, context = _press_callback(
        db_path, CALLBACK_CONFLICT_OVERWRITE, context.user_data, 7007
    )

    assert update.effective_message.replies == [texts.BACKFILL_ASK_TIME]
    assert context.user_data.get(WAIT_EVENT_TIME_KEY) is True
    assert context.user_data.get(WAIT_PAIR_SLEEP_KEY) is True
    assert backfill.CONFIRM_PAIR_KEY not in context.user_data
    assert len(_event_rows(conn, 7007)) == 2  # новых нет

    # Пару можно ввести заново без конфликта (сдвинули за записанный сон).
    _, context = _send_text(db_path, "вчера 09:00", context.user_data, 7007)
    update, context = _send_text(db_path, "вчера 11:00", context.user_data, 7007)
    assert "✅ Событие записано на 11:00." in update.effective_message.replies[0]
    assert len(_event_rows(conn, 7007)) == 4


def test_tc_pback_008_mutual_phase_exclusion_both_ways(db_path, conn):
    """TC-PBACK-008 (взаимное исключение фаз, review-001-1.2-симметрия,
    санкция ПМ на 3.1): (а) вход в парный диалог снимает WAIT_SETTING_VALUE;
    (б) вход в настройки снимает фазы ПАРЫ (BACKFILL_PHASE_KEYS в обе
    стороны, без копий хелперов)."""
    # (а) фаза настроек → event:sleep → time:manual: фаза настроек снята
    # УЖЕ на event:sleep (1.2-sym через _set_pending_kind) и остаётся
    # снятой после входа в шаг 1 пары.
    init_db(db_path)
    user_data: dict = {WAIT_SETTING_VALUE: "day_start"}
    _, context = _press_callback(db_path, "event:sleep", user_data, 7008)
    assert WAIT_SETTING_VALUE not in context.user_data
    _, context = _press_callback(
        db_path, "time:manual", context.user_data, 7008
    )
    assert WAIT_SETTING_VALUE not in context.user_data
    assert context.user_data.get(WAIT_PAIR_SLEEP_KEY) is True

    # (б) фазы пары → settings:show: все фазы пары погашены.
    init_db(db_path)
    user_data = {
        WAIT_EVENT_TIME_KEY: True,
        PENDING_KIND_KEY: "sleep",
        WAIT_PAIR_SLEEP_KEY: True,
        WAIT_PAIR_WAKE_KEY: {"sleep_utc": "2026-10-09 10:40:00"},
        backfill.CONFIRM_PAIR_KEY: {
            "sleep_utc": "2026-10-09 10:40:00",
            "wake_utc": "2026-10-09 12:15:00",
        },
    }
    update, context = _press_callback(
        db_path, "settings:show", user_data, 7008
    )
    for key in (
        WAIT_EVENT_TIME_KEY,
        PENDING_KIND_KEY,
        WAIT_PAIR_SLEEP_KEY,
        WAIT_PAIR_WAKE_KEY,
        backfill.CONFIRM_PAIR_KEY,
    ):
        assert key not in context.user_data
    # Экран настроек §7.16 показан.
    assert "⚙️ **Настройки**" in update.effective_message.replies[0]


def test_tc_pback_009_depth_too_old_for_both_pair_times(db_path, conn, frozen_now):
    """TC-PBACK-009 (FR-17, spec paired-backfill «Слишком давняя пара»):
    глубина §7.15 для ОБОИХ времён пары — (а) засыпание старше 3 дней →
    §7.15 сразу, 0 событий, фазы сняты; (б) засыпание в норме, пробуждение
    старше 3 дней → §7.15, 0 событий, ВСЕ фазы сняты."""
    # (а) шаг 1 — слишком давно.
    _, context = _press_callback(db_path, "event:sleep", {}, 7009)
    _, context = _press_callback(db_path, "time:manual", context.user_data, 7009)
    update, context = _send_text(
        db_path, _days_ago_text(4), context.user_data, 7009
    )
    assert update.effective_message.replies == [texts.BACKFILL_TOO_OLD]
    assert _event_rows(conn, 7009) == []
    for key in (WAIT_EVENT_TIME_KEY, WAIT_PAIR_SLEEP_KEY, PENDING_KIND_KEY):
        assert key not in context.user_data

    # (б) шаг 2 — пробуждение слишком давно.
    _, context = _press_callback(db_path, "event:sleep", {}, 7009)
    _, context = _press_callback(db_path, "time:manual", context.user_data, 7009)
    _, context = _send_text(db_path, "вчера 13:40", context.user_data, 7009)
    update, context = _send_text(
        db_path, _days_ago_text(4), context.user_data, 7009
    )
    assert update.effective_message.replies == [texts.BACKFILL_TOO_OLD]
    assert _event_rows(conn, 7009) == []
    for key in (
        WAIT_EVENT_TIME_KEY,
        PENDING_KIND_KEY,
        WAIT_PAIR_SLEEP_KEY,
        WAIT_PAIR_WAKE_KEY,
    ):
        assert key not in context.user_data


def test_tc_pback_010_bad_format_on_step_two_keeps_phase(db_path, conn, frozen_now):
    """TC-PBACK-010 (FR-16 в ред. 3.1): мусор на шаге 2 → §7.12 с повторным
    запросом, фаза шага 2 СОХРАНЕНА (sleep_utc не потерян), валидный ввод
    после — завершает пару (парсер §4.4 переиспользован)."""
    _, context = _press_callback(db_path, "event:sleep", {}, 7010)
    _, context = _press_callback(db_path, "time:manual", context.user_data, 7010)
    _, context = _send_text(db_path, "вчера 13:40", context.user_data, 7010)

    update, context = _send_text(db_path, "совсем не время", context.user_data, 7010)
    assert update.effective_message.replies == [texts.BACKFILL_BAD_FORMAT]
    assert context.user_data.get(WAIT_PAIR_WAKE_KEY) == {
        "sleep_utc": _utc_str(_yesterday_at(13, 40))
    }

    update, context = _send_text(db_path, "вчера 15:15", context.user_data, 7010)
    assert "✅ Событие записано на 15:15." in update.effective_message.replies[0]
    assert len(_event_rows(conn, 7010)) == 2


def test_tc_pback_011_pair_insert_failure_atomic_rollback(db_path, conn, frozen_now, monkeypatch):
    """TC-PBACK-011 (3.1-fix, review-001-3.1 major №1): отказ ВТОРОЙ
    вставки пары (инъекция ValueError в SleepEventsRepo.stage_insert на
    wake) через РЕАЛЬНЫЙ обработчик → транзакция пары откачена: в БД НЕТ
    полупары (0 событий), фазы сняты, пользователь получил честный
    отказ-ответ (молчаливого проглота нет)."""
    real_stage = SleepEventsRepo.stage_insert
    staged = {"kinds": []}

    def _flaky_stage(self, user_id, event_type, timestamp, **kwargs):
        staged["kinds"].append(event_type)
        if event_type == "wake":
            raise ValueError("инъекция отказа второй вставки (TC-PBACK-011)")
        return real_stage(self, user_id, event_type, timestamp, **kwargs)

    monkeypatch.setattr(SleepEventsRepo, "stage_insert", _flaky_stage)

    update, context = _run_pair_dialog(
        db_path, "вчера 13:40", "вчера 15:15", telegram_id=7011
    )

    # Дошли до второй вставки — отказ случился МЕЖДУ вставками пары.
    assert staged["kinds"] == ["sleep", "wake"]
    # Атомарность: staged sleep откачен — в БД НЕТ полупары (0 событий).
    assert _event_rows(conn, 7011) == []
    # Честный ответ пользователю (не пуст, не «успех»).
    assert update.effective_message.replies == [backfill.BACKFILL_PAIR_FAILED]
    assert update.effective_message.replies[0].startswith("⚠️")
    # Фазы диалога сняты.
    for key in (
        WAIT_EVENT_TIME_KEY,
        PENDING_KIND_KEY,
        WAIT_PAIR_SLEEP_KEY,
        WAIT_PAIR_WAKE_KEY,
    ):
        assert key not in context.user_data


def test_tc_pback_012_future_time_on_step_one_keeps_phase(db_path, conn, frozen_now):
    """TC-PBACK-012 (FR-20, minor №2 review-001-3.1): будущее время на
    ШАГЕ 1 пары («завтра 13:40») → предупреждение БЕЗ записи, фаза шага 1
    СОХРАНЕНА; после валидного ввода пара проходит оба шага и создаётся."""
    _, context = _press_callback(db_path, "event:sleep", {}, 7012)
    _, context = _press_callback(db_path, "time:manual", context.user_data, 7012)
    update, context = _send_text(db_path, "завтра 13:40", context.user_data, 7012)

    assert update.effective_message.replies == [texts.FUTURE_TIME_WARNING]
    assert _event_rows(conn, 7012) == []
    # Фаза шага 1 сохранена — пользователь вводит время засыпания заново.
    assert context.user_data.get(WAIT_PAIR_SLEEP_KEY) is True
    assert WAIT_PAIR_WAKE_KEY not in context.user_data

    update, context = _send_text(db_path, "вчера 13:40", context.user_data, 7012)
    assert update.effective_message.replies == [PAIR_ASK_WAKE]
    update, context = _send_text(db_path, "вчера 15:15", context.user_data, 7012)
    assert "✅ Событие записано на 15:15." in update.effective_message.replies[0]
    assert len(_event_rows(conn, 7012)) == 2


def test_tc_pback_013_future_time_on_step_two_keeps_sleep_utc(db_path, conn, frozen_now):
    """TC-PBACK-013 (FR-20, minor №2 review-001-3.1): будущее время на
    ШАГЕ 2 пары («завтра 15:15») → предупреждение БЕЗ записи, фаза шага 2
    СОХРАНЕНА вместе с sleep_utc (засыпание шага 1 не потеряно); валидный
    ввод после — пара завершена (2 события, оба is_backfilled=1)."""
    _, context = _press_callback(db_path, "event:sleep", {}, 7013)
    _, context = _press_callback(db_path, "time:manual", context.user_data, 7013)
    _, context = _send_text(db_path, "вчера 13:40", context.user_data, 7013)

    update, context = _send_text(db_path, "завтра 15:15", context.user_data, 7013)
    assert update.effective_message.replies == [texts.FUTURE_TIME_WARNING]
    assert _event_rows(conn, 7013) == []
    # Фаза шага 2 с sleep_utc сохранена (шаг 1 не потерян).
    assert context.user_data.get(WAIT_PAIR_WAKE_KEY) == {
        "sleep_utc": _utc_str(_yesterday_at(13, 40))
    }

    update, context = _send_text(db_path, "вчера 15:15", context.user_data, 7013)
    assert "✅ Событие записано на 15:15." in update.effective_message.replies[0]
    rows = _event_rows(conn, 7013)
    assert [(r["event_type"], r["timestamp"]) for r in rows] == [
        ("sleep", _utc_str(_yesterday_at(13, 40))),
        ("wake", _utc_str(_yesterday_at(15, 15))),
    ]
    assert all(r["is_backfilled"] for r in rows)
