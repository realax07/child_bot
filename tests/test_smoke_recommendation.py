# СКВОЗНОЙ smoke пакета add-recommendations (change-пакет, задача 4.1;
# PLAN «Критерии готовности пакета»: «день с подсказками» на фальшивых
# часах). Образец стиля — tests/test_smoke_core.py (пакет 1).
#
# Сценарий в тестовой среде: фейковый PTB (контракты test_smoke_core.py),
# РЕАЛЬНАЯ временная БД (init_db в tmp_path), реальные обработчики
# start/common (reply-панель)/events/backfill/hint и РЕАЛЬНЫЙ
# ReminderScheduler (APScheduler без старта, моменты по триггеру).
# Один context.user_data и одна БД между нажатиями — как в реальном PTB.
#
# «День с подсказками» (время фиксируется в сценарии, без «живых» ЧЧ:ММ
# и time.sleep; Europe/Moscow = UTC+3):
#   1. /start вечером 29.10 → §7.1, reply-панель BUG-002 с «⏰ Подсказка
#      сейчас», 13 настроек (FR-5/FR-23);
#   2. «😴 Заснул» → «🕐 Сейчас» 22:00 → §7.5 с «Отбой» (PB-3);
#   3. утром 30.10 «☀️ Проснулся» → «🕐 Сейчас» 08:00 → §7.2-стиль:
#      факт + строка окна (FR-38, D20) + сводка PB-3;
#   4. «⏰ Подсказка сейчас» бодрствующему в 12:30 → структура §7.6
#      дословно, sleep_events НЕ пишутся (FR-32);
#   5. «😴 Заснул» → «🕐 Сейчас» 13:30 → §7.5;
#   6. «⏰ Подсказка сейчас» спящему в 14:15 → структура §7.7, без
#      «пора укладывать», записи НЕТ (FR-33);
#   7. «☀️ Проснулся» → «⏪ Ввести время вручную» → «14:30» → §7.14
#      со строкой окна к сводке (FR-38/D20, is_backfilled=1);
#   8. планировщик пересчитал push-план на хуках событий (FR-41):
#      утром стоял nap-push 13:05; после дневного сна задача «в прошлое»
#      снята, остался ночной push 21:45 (§7.24, ОВ-4.2); ре-шедул
#      идемпотентен; nap-push в момент срабатывания «в прошлом» не шлётся.
#
# Тексты — сверка с файлом ТЗ §7.2/§7.6–7.9/§7.23/§7.24 ДОСЛОВНО
# (TC-SMOKE2-002, AC-6: эталон из ТЗ, не из texts.py).
#
# Правило rule 6: каждый тест несёт TC-SMOKE2-NNN в докстринге.

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import texts
from src.db import init_db
from src.handlers import backfill, common, events, hint, start
from src.handlers.events import CALLBACK_TIME_MANUAL, CALLBACK_TIME_NOW
from src.repositories.sleep_events import SleepEventsRepo
from src.services import scheduler as scheduler_mod
from src.services.event import get_user
from src.services.scheduler import (
    KIND_NAP,
    KIND_NIGHT,
    ReminderScheduler,
    compute_push_plan,
    send_push_job,
)
from src.services.settings import SettingsService
from src.services import state as state_mod
from src.services.state import (
    BTN_HINT,
    BTN_HISTORY,
    BTN_SETTINGS,
    BTN_SLEEP,
    BTN_UNDO,
    BTN_WAKE,
    CALLBACK_SLEEP,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC_FILE = _REPO_ROOT / "ТЗ_Дневник_сна_ребёнка.md"

TELEGRAM_ID = 7001

# --------------------------------------------------------------- Часы сценария
# Фиксированные моменты (aware UTC; МСК = UTC+3), без «живых» ЧЧ:ММ:
T_BEDTIME = datetime(2026, 10, 29, 19, 0, tzinfo=timezone.utc)    # 22:00 МСК 29.10
T_RISE = datetime(2026, 10, 30, 5, 0, tzinfo=timezone.utc)        # 08:00 МСК 30.10
T_HINT_AWAKE = datetime(2026, 10, 30, 9, 30, tzinfo=timezone.utc)  # 12:30 МСК
T_NAP = datetime(2026, 10, 30, 10, 30, tzinfo=timezone.utc)       # 13:30 МСК
T_HINT_ASLEEP = datetime(2026, 10, 30, 11, 15, tzinfo=timezone.utc)  # 14:15 МСК
T_BACKFILL = datetime(2026, 10, 30, 11, 35, tzinfo=timezone.utc)  # 14:35 МСК

# Расчётные моменты push-плана (UTC) на фальшивых часах:
NAP_FIRE_UTC = datetime(2026, 10, 30, 10, 5, tzinfo=timezone.utc)   # 13:05 МСК
NIGHT_FIRE_UTC = datetime(2026, 10, 30, 18, 45, tzinfo=timezone.utc)  # 21:45 МСК


class _ScenarioClock:
    """Фальшивые часы сценария: текущий момент переставляется шагами."""

    def __init__(self) -> None:
        self.utc = T_BEDTIME


def _frozen_datetime_class(clock: _ScenarioClock) -> type:
    """Класс datetime с замороженным now() на моменте часов сценария."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            moment = clock.utc
            if tz is not None:
                return moment.astimezone(tz)
            return moment.replace(tzinfo=None)

    return _FrozenDatetime


@pytest.fixture()
def clock() -> _ScenarioClock:
    return _ScenarioClock()


@pytest.fixture()
def frozen_now(monkeypatch, clock):
    """Фиксировать «сейчас» обработчиков на часах сценария.

    events.datetime — замороженный класс («Сейчас» и расчётный момент
    сводки); hint.datetime — тот же класс (момент подсказки, design D5);
    backfill.parse_time_input — обёртка с моментом часов (сам класс
    backfill.datetime НЕ подменяется: хендлер проверяет isinstance).
    """
    frozen = _frozen_datetime_class(clock)
    monkeypatch.setattr(events, "datetime", frozen)
    monkeypatch.setattr(hint, "datetime", frozen)
    real_parse = backfill.parse_time_input
    monkeypatch.setattr(
        backfill,
        "parse_time_input",
        lambda text, tz, now, max_age_days=None: real_parse(
            text, tz, clock.utc, max_age_days=max_age_days
        ),
    )


# ------------------------------------------------------------ Заглушки PTB
# (те же контракты, что в tests/test_smoke_core.py — пакет 1)


class _FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.reply_markup = None
        self.replies: list[str] = []

    async def reply_text(self, reply, reply_markup=None):
        self.replies.append(reply)
        if reply_markup is not None:
            self.reply_markup = reply_markup

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
        self.language_code = "ru"


class _FakeCommandUpdate:
    def __init__(self, user_id: int) -> None:
        self.effective_user = _FakeUser(user_id)
        self.callback_query = None
        self.message = _FakeMessage()
        self.effective_message = self.message


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


class _FakeBot:
    """Ловит send_message — пуши без реального Telegram (design D19)."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append((chat_id, text))


def _button_texts(markup) -> list[str]:
    if markup is None:
        return []
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _button_datas(markup) -> list[str]:
    if markup is None:
        return []
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


class _FakeContextFactory:
    """Фейковый PTB-контекст (bot_data['db_path'] + общий user_data)."""

    @staticmethod
    def new_context(db_path, user_data: dict | None = None):
        class _Context:
            def __init__(self) -> None:
                self.bot_data = {"db_path": db_path}
                self.user_data: dict = {} if user_data is None else user_data

        return _Context()


# ------------------------------------------------------------------ Тесты


class TestSmokeRecommendation:
    """Сквозной «день с подсказками» пакета 2 (задача 4.1, D16–D20)."""

    @pytest.fixture()
    def db_path(self, tmp_path):
        return tmp_path / "bot.db"

    @pytest.fixture()
    def conn(self, db_path):
        c = init_db(db_path)
        yield c
        c.close()

    # ------------------------------------------------------ помощь нажатий

    def _start(self, db_path):
        update = _FakeCommandUpdate(TELEGRAM_ID)
        context = _FakeContextFactory.new_context(db_path)
        asyncio.run(start.start_command(update, context))  # type: ignore[arg-type]
        return update, context

    def _press_panel(self, db_path, text: str, user_data: dict):
        """Кнопка постоянной reply-панели (BUG-002): текст → группа 1."""
        update = _FakeTextUpdate(TELEGRAM_ID, text)
        context = _FakeContextFactory.new_context(db_path, user_data)
        asyncio.run(common.reply_button_message(update, context))  # type: ignore[arg-type]
        return update, context

    def _press_time(self, db_path, data: str, user_data: dict):
        """«🕐 Сейчас» / «⏪ Ввести время вручную» (колбэк §7.10)."""
        update = _FakeCallbackUpdate(TELEGRAM_ID, data)
        context = _FakeContextFactory.new_context(db_path, user_data)
        asyncio.run(events.time_choose_callback(update, context))  # type: ignore[arg-type]
        return update, context

    def _send_text(self, db_path, text: str, user_data: dict):
        update = _FakeTextUpdate(TELEGRAM_ID, text)
        context = _FakeContextFactory.new_context(db_path, user_data)
        asyncio.run(backfill.backfill_time_message(update, context))  # type: ignore[arg-type]
        return update, context

    # ------------------------------------------------------------- сценарий

    def test_tc_smoke2_001_full_day_with_hints_and_push_plan(
        self, db_path, conn, frozen_now, clock
    ):
        """TC-SMOKE2-001 (FR-26…FR-41, design D16–D20): «день с
        подсказками» — /start → засыпание 22:00 → подъём 08:00 (§7.2 с
        окном и сводкой PB-3) → подсказка бодрствующему §7.6 дословно →
        дневной сон 13:30 → подсказка спящему §7.7 → пробуждение задним
        числом 14:30 (§7.14 со строкой окна) → планировщик пересчитал
        push-план: nap-push снят («в прошлое» не шлётся), ночной push
        21:45 на месте, ре-шедул идемпотентен. Подсказки НЕ пишут в
        sleep_events; время зафиксировано на часах сценария."""
        user_data: dict = {}  # ОДИН user_data на весь сценарий (реальный PTB)

        # Планировщик как в живом процессе: хуки событий ре-шедулят
        # (design D19, точка вызова services.event.create_event);
        # APScheduler НЕ стартует — моменты читаются по триггеру.
        sched = ReminderScheduler(_FakeBot(), db_path, clock=lambda: clock.utc)
        scheduler_mod._set_active(sched)
        try:
            self._run_scenario(db_path, conn, user_data, clock, sched)
        finally:
            scheduler_mod._set_active(None)  # изоляция глобального реестра

    # ------------------------------------------------- шаги сценария

    def _run_scenario(self, db_path, conn, user_data, clock, sched) -> None:
        def events_count(user_id: int) -> int:
            return len(SleepEventsRepo(conn).list_by_user(user_id))

        # 1. /start вечером 29.10 → §7.1 + reply-панель BUG-002 (FR-5/FR-6):
        #    носитель панели с «⏰ Подсказка сейчас» и приветствие с
        #    inline-меню (PLAN-BUG-002 п. 3/4); 13 дефолтных настроек (FR-23).
        start_update, _ = self._start(db_path)
        assert start_update.effective_message.replies == [
            common.REPLY_PANEL_INTRO,
            texts.START,
        ]
        # reply-панель статична (BUG-002): все шесть кнопок, включая
        # «⏰ Подсказка сейчас» и новые «История»/«Настройки»
        # (ТЗ §6, add-settings-history 2.0, текстовый вход подсказки).
        assert state_mod.REPLY_KEYBOARD == [
            [BTN_SLEEP, BTN_WAKE, BTN_HINT],
            [BTN_HISTORY, BTN_SETTINGS],
            [BTN_UNDO],
        ]
        start_menu = start_update.effective_message.reply_markup
        assert CALLBACK_SLEEP in _button_datas(start_menu)
        assert BTN_WAKE not in _button_texts(start_menu)
        user = get_user(conn, TELEGRAM_ID)
        assert len(SettingsService(conn).get_all(user["id"])) == 13
        assert events_count(user["id"]) == 0

        # 2. «😴 Заснул» (reply-панель) → §7.10 с «🕐 Сейчас» (FR-15);
        #    событие ещё НЕ создано.
        sleep_update, _ = self._press_panel(db_path, BTN_SLEEP, user_data)
        assert sleep_update.effective_message.replies == [texts.BACKFILL_CHOOSE_TIME]
        assert texts.BTN_TIME_NOW in _button_texts(
            sleep_update.effective_message.reply_markup
        )
        assert texts.BTN_TIME_MANUAL in _button_texts(
            sleep_update.effective_message.reply_markup
        )
        assert events_count(user["id"]) == 0

        # 3. «🕐 Сейчас» 22:00 МСК → §7.5 c фактом «Отбой» (PB-3,
        #    заморозка бодрствования на начале ночного сна — BUG-004:
        #    22:00 − 06:00 day_start = 16 ч); меню переключилось.
        now_update, _ = self._press_time(db_path, CALLBACK_TIME_NOW, user_data)
        events_menu = now_update.callback_query.message.reply_markup
        assert BTN_WAKE in _button_texts(events_menu)
        assert BTN_SLEEP not in _button_texts(events_menu)
        assert now_update.effective_message.replies[0].splitlines() == [
            "✅ Засыпание записано в 22:00.",
            "Сон идёт.",
            "Отбой: 22:00",
            "Бодрствование общее: 16 ч",
        ]
        assert len(now_update.effective_message.replies) == 1
        rows = SleepEventsRepo(conn).list_by_user(user["id"])
        assert [e["event_type"] for e in rows] == ["sleep"]
        assert rows[0]["is_backfilled"] is False

        # 4. Утро 30.10, 08:00: «☀️ Проснулся» → «🕐 Сейчас» → §7.2-стиль
        #    (FR-8 в ред. PB-3 + FR-38/D20): факт + строка окна дневного
        #    сна (стандартный подъём 08:00 → 08:00 + 5:20…6:00) + сводка
        #    PB-3 с «Подъем» и «Бодрствование общее» (BUG-004: 0 мин).
        clock.utc = T_RISE
        wake_update, _ = self._press_panel(db_path, BTN_WAKE, user_data)
        assert wake_update.effective_message.replies == [texts.BACKFILL_CHOOSE_TIME]
        rise_update, _ = self._press_time(db_path, CALLBACK_TIME_NOW, user_data)
        assert BTN_SLEEP in _button_texts(
            rise_update.callback_query.message.reply_markup
        )
        assert rise_update.effective_message.replies[0].splitlines() == [
            "✅ Пробуждение записано в 08:00.",
            "Окно дневного сна: 13:20–14:00.",
            "Подъем: 08:00",
            "Бодрствование общее: 0 мин",
        ]
        assert len(rise_update.effective_message.replies) == 1
        rows = SleepEventsRepo(conn).list_by_user(user["id"])
        assert [e["event_type"] for e in rows] == ["sleep", "wake"]

        # 5. Планировщик на хуках событий пересчитал push-план (FR-41):
        #    nap-push за 15 мин до окна (§7.23, 13:05 МСК) и ночной push
        #    (§7.24, ОВ-4.2: recommended 22:00 → 21:45 МСК) стоят, по
        #    одной задаче каждого вида (job replace, дублей нет).
        times = sched.planned_fire_times()
        nap_job_id = f"remind:{user['id']}:{KIND_NAP}"
        night_job_id = f"remind:{user['id']}:{KIND_NIGHT}"
        assert times[nap_job_id] == NAP_FIRE_UTC
        assert times[night_job_id] == NIGHT_FIRE_UTC
        assert [jid for jid in times if jid.endswith(f":{KIND_NAP}")] == [nap_job_id]

        # 6. 12:30: «⏰ Подсказка сейчас» (текст reply-панели, 2.2-fix)
        #    бодрствующему → §7.6 ДОСЛОВНО (4 ч 30 мин с подъёма 08:00,
        #    остатки до 12/12,5 ч, окно 13:20–14:00); sleep_events НЕ
        #    пишутся (FR-32); времени «сейчас» и слова состояния нет.
        clock.utc = T_HINT_AWAKE
        hint_awake_update, _ = self._press_panel(db_path, BTN_HINT, user_data)
        assert len(hint_awake_update.effective_message.replies) == 1
        hint_awake = hint_awake_update.effective_message.replies[0]
        assert hint_awake == texts.HINT_AWAKE
        assert hint_awake.splitlines()[0] == (
            "Бодрствование с последнего пробуждения: 4 ч 30 мин. "
            "Суммарно за сегодня: 4 ч 30 мин."
        )
        assert "Окно дневного сна: 13:20–14:00." in hint_awake
        assert "12:30" not in hint_awake
        assert "бодрствует" not in hint_awake
        assert events_count(user["id"]) == 2  # подсказка ничего не записала

        # 7. 13:30: «😴 Заснул» → «🕐 Сейчас» → §7.5 (дневной отрезок
        #    «Заснул», BUG-004: 13:30 − 08:00 = 5 ч 30 мин).
        clock.utc = T_NAP
        nap_update, _ = self._press_panel(db_path, BTN_SLEEP, user_data)
        assert nap_update.effective_message.replies == [texts.BACKFILL_CHOOSE_TIME]
        nap_now_update, _ = self._press_time(db_path, CALLBACK_TIME_NOW, user_data)
        assert nap_now_update.effective_message.replies[0].splitlines() == [
            "✅ Засыпание записано в 13:30.",
            "Сон идёт.",
            "Подъем: 08:00",
            "Заснул: 13:30",
            "Бодрствование общее: 5 ч 30 мин",
        ]
        assert len(nap_now_update.effective_message.replies) == 1

        # 8. Ре-шедул по событию засыпания: nap-push 13:05 ушёл в прошлое —
        #    задача СНЯТА, «в прошлое» план не включает (FR-41); ночной
        #    push на месте (ребёнок спит ДНЁМ — не ночной сон, FR-40).
        times = sched.planned_fire_times()
        assert nap_job_id not in times
        assert times[night_job_id] == NIGHT_FIRE_UTC

        # 9. 14:15: «⏰ Подсказка сейчас» спящему → §7.7 (45 мин сна,
        #    прогноз пробуждения ~15:00 по цели 90 мин, прогноз
        #    бодрствования после); «пора укладывать» нет (FR-33),
        #    записи НЕТ.
        clock.utc = T_HINT_ASLEEP
        hint_asleep_update, _ = self._press_panel(db_path, BTN_HINT, user_data)
        assert len(hint_asleep_update.effective_message.replies) == 1
        hint_asleep = hint_asleep_update.effective_message.replies[0]
        assert hint_asleep.splitlines()[0] == (
            "Сон идёт с 13:30 (уже 45 мин). Ожидаемое пробуждение: около 15:00."
        )
        assert (
            "После пробуждения суммарное бодрствование составит около 5 ч 30 мин."
        ) in hint_asleep
        assert "укладывать" not in hint_asleep
        assert events_count(user["id"]) == 3  # подсказка ничего не записала

        # 10. 14:35: «☀️ Проснулся» → «⏪ Ввести время вручную» → «14:30»
        #     → §7.14 (FR-16/FR-19, is_backfilled=1) со строкой окна
        #     К сводке PB-3 (FR-38/D20): Подъем/Заснул/Проснулся/
        #     «Дневной сон» + окно от подъёма 08:00.
        clock.utc = T_BACKFILL
        bf_wake_update, _ = self._press_panel(db_path, BTN_WAKE, user_data)
        assert bf_wake_update.effective_message.replies == [texts.BACKFILL_CHOOSE_TIME]
        manual_update, _ = self._press_time(
            db_path, CALLBACK_TIME_MANUAL, user_data
        )
        assert manual_update.effective_message.replies == [texts.BACKFILL_ASK_TIME]
        assert user_data.get(events.WAIT_EVENT_TIME_KEY) is True
        assert user_data.get(events.PENDING_KIND_KEY) == "wake"

        text_update, _ = self._send_text(db_path, "14:30", user_data)
        assert len(text_update.effective_message.replies) == 1
        answer_714 = text_update.effective_message.replies[0]
        assert answer_714.splitlines() == [
            "✅ Событие записано на 14:30.",
            "",
            "Обновлённая сводка за день:",
            "Подъем: 08:00",
            "Заснул: 13:30",
            "Проснулся: 14:30",
            "Дневной сон: 1 ч",
            # Расчётный момент сводки — «сейчас» ввода (14:35, BUG-004):
            # (14:35 − 08:00) − 1:00 дневного сна.
            "Бодрствование общее: 5 ч 35 мин",
            # BUG-005: после дневного сна — конкретное время ухода, не окно.
            # Формула ОВ-4.2: max(22:00, момент достижения 12 ч 10 мин).
            # Подъем 08:00, бодрствование на 14:30 = 5 ч 30 мин;
            # 12 ч 10 мин набираются в 21:10 → рекомендация ровно 22:00.
            texts.recommended_bedtime_line("22:00"),
        ]
        assert events.WAIT_EVENT_TIME_KEY not in user_data
        assert events.PENDING_KIND_KEY not in user_data
        rows = SleepEventsRepo(conn).list_by_user(user["id"])
        assert [e["event_type"] for e in rows] == ["sleep", "wake", "sleep", "wake"]
        backfilled = [
            e for e in rows
            if e["event_type"] == "wake" and e["timestamp"].startswith("2026-10-30 11:30")
        ]
        assert len(backfilled) == 1
        assert backfilled[0]["is_backfilled"] is True

        # 11. Итоговый push-план (ре-шедул на хуке backfill-ввода):
        #     только ночной push 21:45 МСК; nap-push в прошлом — нет;
        #     повторный ре-шедул идемпотентен (FR-41); nap-задача в момент
        #     «срабатывания» после окна ничего не шлёт («не в прошлое»).
        times = sched.planned_fire_times()
        assert times == {night_job_id: NIGHT_FIRE_UTC}
        sched.reschedule()
        assert sched.planned_fire_times() == {night_job_id: NIGHT_FIRE_UTC}

        bot = _FakeBot()
        asyncio.run(
            send_push_job(bot, db_path, user["id"], KIND_NAP, now_utc=T_BACKFILL)
        )
        assert bot.sent == []

        # Текст ночного пуша §7.24 дословно (цифры на момент расчёта
        # 14:35 МСК: 5 ч 35 мин бодрствования; якоря 22:00/22:30).
        plan = compute_push_plan(conn, user, T_BACKFILL)
        assert [p.kind for p in plan] == [KIND_NIGHT]
        assert plan[0].chat_id == TELEGRAM_ID
        assert plan[0].run_at_utc == NIGHT_FIRE_UTC
        assert plan[0].text == (
            "⏰ Пора готовиться ко сну. Суммарное бодрствование: "
            "5 ч 35 мин. Целевое время ухода: 22:00, крайнее — 22:30."
        )

    def test_tc_smoke2_002_key_texts_match_spec_verbatim(self):
        """TC-SMOKE2-002 (AC-6, ТЗ §7.2/§7.6–7.9/§7.23/§7.24): тексты
        ответов сценария сверены с файлом ТЗ ДОСЛОВНО — эталон читается
        из ТЗ_Дневник_сна_ребёнка.md, а не копируется в тест."""
        # §7.6–7.9 — подсказки: константы texts.py == цитатам файла ТЗ.
        assert texts.HINT_AWAKE == _spec_section("7.6")
        assert texts.HINT_SLEEPING == _spec_section("7.7")
        assert texts.LATE_NAP_WARNING == _spec_section("7.8")
        assert texts.OVERTIRED_WARNING == _spec_section("7.9")
        # §7.2 — факт + строка окна (шаблон подтверждения пробуждения).
        assert _spec_section("7.2").startswith("✅ Пробуждение записано в ")
        assert texts.nap_window_line("13:20", "14:00") == (
            _spec_section("7.2").splitlines()[1]
        )
        # §7.23/§7.24 — пуши планировщика с числами примера ТЗ.
        assert texts.reminder_nap_text(
            texts.reminder_minutes_line(15), "13:20", "14:00", "5 ч 05 мин"
        ) == _spec_section("7.23")
        assert texts.reminder_night_text(
            "12 ч 05 мин", "22:00", "22:30"
        ) == _spec_section("7.24")
        # Кнопки ТЗ §6 дословно (входы сценария).
        assert BTN_SLEEP == "😴 Заснул"
        assert BTN_WAKE == "☀️ Проснулся"
        assert BTN_HINT == "⏰ Подсказка сейчас"
        assert BTN_UNDO == "↩️ Отменить последнее"


# ------------------------------------------------------------- Эталон §7


def _spec_section(section: str) -> str:
    """Дословный текст раздела ТЗ §7.x (blockquote после заголовка, AC-6).

    Эталон читается из файла ТЗ, а не копируется в тест (паттерн
    TC-SMOKE-002 пакета 1).
    """
    lines = _SPEC_FILE.read_text(encoding="utf-8").splitlines()
    start_idx = next(
        i for i, line in enumerate(lines) if line.startswith(f"### {section}")
    )
    body: list[str] = []
    for line in lines[start_idx + 1:]:
        if not line.startswith(">"):
            if body:
                break  # blockquote раздела закончился
            continue  # пустые строки до цитаты
        body.append(line[1:].lstrip(" "))
    return "\n".join(body).strip("\n")
