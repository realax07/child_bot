# Юнит-тесты успешного ввода задним числом: §7.14 со сводкой событий дня
# (change-пакет add-core-diary, задачи 4.5 + 6.1/PB-3).
#
# Критерии готовности 4.5 (FR-16, FR-19 в ред. волны 6; design D6, D10):
#   - ввод «вчера 22:30» → событие создано с is_backfilled=1;
#   - ответ §7.14: подтверждающая строка + сводка событий дня в формате
#     продакта (PB-3: Подъем/Заснул/Проснулся/Дневной сон/Отбой/Ночной сон
#     + Бодрствование общее), БЕЗ абстрактных сумм «Сон: …»/
#     «Бодрствование: …»; задача 2.3 (FR-38, design D20): строка окна
#     дневного сна добавляется К сводке — в сценариях ниже факта
#     «Подъем» на странице ввода нет, строка окна не добавляется
#     (положительный сценарий с окном — TC-BFS-004);
#   - конфликтный путь «Изменить» (conflict:overwrite) тоже завершается §7.14;
#   - сводка считается по странице дня ВВОДИМОГО времени, не сегодняшней.
#
# Правило rule 6: каждый тест несёт TC-BFS-NNN в первой строке докстринга.
# Время фиксируется в фикстуре frozen_now (monkeypatch backfill.datetime):
# 8:00 МСК 10.10 — ассерты без «живых» ЧЧ:ММ (правило волны 6).

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

# Фиксированный «сейчас» обработчика backfill: 8:00 МСК 10.10 (05:00 UTC).
FIXED_NOW_LOCAL = datetime(2026, 10, 10, 8, 0, tzinfo=TZ)


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

    events.datetime — замороженный класс (расчётный момент сводки и
    «Сейчас»); backfill.parse_time_input — обёртка с фиксированным
    моментом ввода (сам класс backfill.datetime НЕ подменяется: хендлер
    проверяет isinstance(parsed, datetime), подмена класса сломала бы
    guard задачи 4.2).
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
    """Вчера ЧЧ:ММ по локальному времени относительно ФИКСИРОВАННОГО
    «сейчас» (8:00 МСК 10.10) — время теста зафиксировано в фикстуре."""
    day = (FIXED_NOW_LOCAL - timedelta(days=1)).date()
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


def test_tc_bfs_001_backfill_creates_event_and_answers_7_14(db_path, conn, frozen_now):
    """TC-BFS-001 (FR-16, FR-19 в ред. волны 6, PB-3): ввод «вчера 22:30» →
    событие с is_backfilled=1 и ответ §7.14: подтверждающая строка +
    сводка событий дня в формате продакта («Отбой: 22:30» — незакрытый
    ночной отрезок страницы «вчера», «Бодрствование общее: 16 ч 30 мин»);
    БЕЗ абстрактных сумм «Сон: …»/«Бодрствование: …» и без слова «Окно»;
    фазы диалога сняты. Время зафиксировано: 8:00 МСК 10.10."""
    when = _yesterday_at(22, 30)
    user_data = {
        WAIT_EVENT_TIME_KEY: True,
        PENDING_KIND_KEY: "sleep",
    }
    update, context = _send_text(db_path, "вчера 22:30", user_data)

    # Ответ §7.14: подтверждение с локальным временем + факты дня (PB-3).
    assert len(update.effective_message.replies) == 1
    answer = update.effective_message.replies[0]
    assert f"✅ Событие записано на {_local_hhmm(when)}." in answer
    assert "Обновлённая сводка за день:" in answer
    assert answer.splitlines()[3:] == [
        "Отбой: 22:30",
        "Бодрствование общее: 16 ч 30 мин",
    ]
    # Старый абстрактный формат сумм («Сон: …» / «Бодрствование: …»)
    # отсутствует; «Бодрствование общее» — не старый формат.
    assert "Сон:" not in answer
    assert "Бодрствование:" not in answer
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


def test_tc_bfs_002_conflict_overwrite_answers_7_14(db_path, conn, frozen_now):
    """TC-BFS-002 (FR-18 + FR-19 в ред. волны 6, PB-3): конфликтный путь
    «Изменить» → уведомление о перезаписи (ОВ-ПО-1) И §7.14 со сводкой
    событий дня в формате продакта («Заснул: 06:00» — незакрытый дневной
    отрезок, «Бодрствование общее: 0 мин» — ред. BUG-004: (08:00 − 06:00)
    − 2:00 идущего сна); строка окна не добавляется (факта «Подъем» на
    странице нет, FR-38/D20); перезапись без дублей; фазы очищены.
    Время зафиксировано: 8:00 МСК 10.10."""
    user = get_user(conn, 5002)
    wake_at = FIXED_NOW_LOCAL - timedelta(hours=2)  # 06:00 МСК 10.10
    wake_id = _insert_event(conn, user["id"], "wake", wake_at)

    user_data = {
        PENDING_KIND_KEY: "sleep",
        backfill.CONFIRM_OVERWRITE_KEY: {
            "event_id": wake_id,
            "when_utc": _utc_str(wake_at),
        },
    }
    update, context = _press_overwrite(db_path, 5002, user_data)

    # Уведомление ОВ-ПО-1, затем §7.14 с фактами дня (без слова «Окно»).
    assert len(update.effective_message.replies) == 2
    assert update.effective_message.replies[0] == texts.BACKFILL_OVERWRITTEN.format(
        label=texts.KIND_LABELS["sleep"],
        local=_local_hhmm(wake_at),
    )
    summary = update.effective_message.replies[1]
    assert f"✅ Событие записано на {_local_hhmm(wake_at)}." in summary
    assert "Обновлённая сводка за день:" in summary
    assert summary.splitlines()[3:] == [
        "Заснул: 06:00",
        # Ред. BUG-004: (08:00 − 06:00) − 2:00 незакрытого дневного сна.
        "Бодрствование общее: 0 мин",
    ]
    assert "Сон:" not in summary  # абстрактных сумм старого формата нет
    assert "Бодрствование:" not in summary
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


def test_tc_bfs_003_summary_uses_entered_time_day_page(db_path, conn, frozen_now):
    """TC-BFS-003 (FR-19 в ред. волны 6, design D8, PB-3): сводка считается
    по странице дня ВВОДИМОГО времени (вчера 09.10), не сегодняшней:
    незакрытый сон с 21:00 вчера (ночной отрезок страницы «вчера») даёт
    факты «Отбой: 21:00» и «Бодрствование общее: 15 ч» (окно 06:00→06:00,
    покрыто 9 ч); ввод «вчера 20:00» (wake ДО начала сна — без конфликта,
    D11: пересечения с интервалом нет). Время зафиксировано:
    8:00 МСК 10.10 — расчёт детерминирован (раньше ассерт зависел от
    реального now на границе суток)."""
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

    # Факты страницы «вчера» (окно 06:00 09.10 → 06:00 10.10): незакрытый
    # ночной отрезок 21:00 → «Отбой: 21:00», без «Ночной сон»/«Подъем»;
    # бодрствование 15 ч (24 ч окна минус 9 ч покрытия до 06:00).
    # bare wake 20:00 (без парного sleep) строку факта не даёт.
    assert answer.splitlines()[3:] == [
        "Отбой: 21:00",
        "Бодрствование общее: 15 ч",
    ]
    assert "Сон:" not in answer  # абстрактных сумм старого формата нет
    assert "Бодрствование:" not in answer
    # Фазы сняты.
    assert WAIT_EVENT_TIME_KEY not in context.user_data
    assert PENDING_KIND_KEY not in context.user_data


def test_tc_bfs_004_backfill_appends_window_line_to_summary(db_path, conn, frozen_now):
    """TC-BFS-004 (FR-38, design D20, §7.14 в ред. PB-3; задача 2.3):
    ввод задним числом «вчера 09:40» (засыпание) при ночи 08.10 22:00 →
    09.10 08:00 — к сводке PB-3 страницы «вчера» добавлена строка окна
    «Окно дневного сна: 13:20–14:00.» (08:00 + 5:20…6:00 — из расчёта
    nap_window страницы ввода, не хардкод); сами строки сводки ПОБАЙТНО
    равны волне 6 (day_facts_lines без изменений). Время зафиксировано:
    8:00 МСК 10.10."""
    user = get_user(conn, 5004)
    # Ночь 08.10 22:00 МСК → подъём 09.10 08:00 МСК (завершённый отрезок).
    _insert_event(conn, user["id"], "sleep", datetime(2026, 10, 8, 22, 0, tzinfo=TZ))
    _insert_event(conn, user["id"], "wake", datetime(2026, 10, 9, 8, 0, tzinfo=TZ))

    user_data = {
        WAIT_EVENT_TIME_KEY: True,
        PENDING_KIND_KEY: "sleep",
    }
    update, context = _send_text(db_path, "вчера 09:40", user_data, telegram_id=5004)

    assert len(update.effective_message.replies) == 1
    answer = update.effective_message.replies[0]
    # Строки сводки — побайтно как в волне 6; строка окна — ПОСЛЕ них.
    assert answer.splitlines() == [
        "✅ Событие записано на 09:40.",
        "",
        "Обновлённая сводка за день:",
        "Подъем: 08:00",
        "Заснул: 09:40",
        "Бодрствование общее: 1 ч 40 мин",
        "Окно дневного сна: 13:20–14:00.",
    ]
    # Окно — тот же расчёт, что у хендлера (nap_window страницы ввода).
    assert answer.endswith(texts.nap_window_line("13:20", "14:00"))
    # Регресс: сводка без строки окна идентична волне 6.
    assert answer.splitlines()[3:6] == [
        "Подъем: 08:00",
        "Заснул: 09:40",
        "Бодрствование общее: 1 ч 40 мин",
    ]


def test_tc_bfs_005_backfill_wake_after_day_sleep_recommends_bedtime(db_path, conn, frozen_now):
    """TC-BFS-005 (BUG-005, §7.14; сценарий приемки Марии задним числом):
    подъём вчера 08:03, дневной сон 13:40, ввод «вчера 15:15» (wake) —
    последний завершённый интервал страницы ввода — дневной отрезок →
    к сводке добавлена «Рекомендованное время ухода: 22:00.» (12 ч
    10 мин набираются в 21:48 — раньше 22:00 → ровно 22:00) и БЕЗ
    прошедшего окна дневного сна; сами строки сводки PB-3 не меняются.
    Время зафиксировано: 8:00 МСК 10.10 (страница ввода «вчера»
    закрыта — расчёт детерминирован)."""
    user = get_user(conn, 5005)
    # Ночь 08.10 22:00 МСК → подъём 09.10 08:03 МСК; дневной сон 13:40 МСК
    # — закрывается вводимым wake 15:15 МСК.
    _insert_event(conn, user["id"], "sleep", datetime(2026, 10, 8, 22, 0, tzinfo=TZ))
    _insert_event(conn, user["id"], "wake", datetime(2026, 10, 9, 8, 3, tzinfo=TZ))
    _insert_event(conn, user["id"], "sleep", datetime(2026, 10, 9, 13, 40, tzinfo=TZ))

    user_data = {
        WAIT_EVENT_TIME_KEY: True,
        PENDING_KIND_KEY: "wake",
    }
    update, context = _send_text(db_path, "вчера 15:15", user_data, telegram_id=5005)

    assert len(update.effective_message.replies) == 1
    answer = update.effective_message.replies[0]
    # Строки сводки PB-3 страницы «вчера» — без изменений; строка
    # следующего сна — ПОСЛЕ них (ред. BUG-005).
    assert answer.splitlines() == [
        "✅ Событие записано на 15:15.",
        "",
        "Обновлённая сводка за день:",
        "Подъем: 08:03",
        "Заснул: 13:40",
        "Проснулся: 15:15",
        "Дневной сон: 1 ч 35 мин",
        # Ред. BUG-004: конец — момент расчёта «сейчас» (08:00 10.10):
        # (08:00 − 08:03 вчера) − 1:35 дневного сна = 22 ч 22 мин.
        "Бодрствование общее: 22 ч 22 мин",
        texts.recommended_bedtime_line("22:00"),
    ]
    assert "Окно дневного сна" not in answer
    # Фазы сняты.
    assert WAIT_EVENT_TIME_KEY not in context.user_data
    assert PENDING_KIND_KEY not in context.user_data
