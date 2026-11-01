# СКВОЗНОЙ smoke «неделя с настройками» (change-пакет add-settings-history,
# задача 4.1; PLAN волна 4: сквозной сценарий пакета 3 на фальшивых часах).
# Образцы стиля — tests/test_smoke_core.py (пакет 1) и
# tests/test_smoke_recommendation.py (пакет 2).
#
# Сценарий в тестовой среде: фейковый PTB (контракты test_smoke_core.py),
# РЕАЛЬНАЯ временная БД (init_db в tmp_path), реальные обработчики
# start/common (reply-панель)/events/backfill/history/settings_screen/help/
# delete_all и реальные сервисы (SettingsService + settings_history,
# EventService.create_event). Один context.user_data и одна БД между
# нажатиями — как в реальном PTB у одного пользователя.
#
# «Неделя с настройками» (время зафиксировано, без sleep/«живых» ЧЧ:ММ;
# Europe/Moscow = UTC+3, «сегодня» — воскресенье 01.11.2026):
#   1. /start → §7.1, reply-панель BUG-002 с «📊 История за неделю»/
#      «⚙️ Настройки», 13 дефолтов (FR-5/FR-23, FR-42/FR-48);
#   2. события недели задним числом: страницы пн 26.10–пт 30.10
#      (подъём 08:00, дневной сон 13:30–15:00, отбой 22:00 — сервис
#      create_event) и ОДНА ПАРА через НОВЫЙ диалог §7.11×2 (задача 3.1,
#      FR-51): «😴 Заснул» → вручную «13:40» → «Когда проснулся?» «15:15»
#      → §7.14-стиль; сегодня — подъём 08:00 одиночным диалогом §7.11;
#   3. «📊 История за неделю» → §7.20: 7 строк недели (новые → старые,
#      ✓/⚠️) + блок сегодня; события кнопкой НЕ создаются;
#   4. «⚙️ Настройки» → §7.16 (все строки цитаты ТЗ в экране);
#   5. изменение настройки: settings:edit → §7.17 → ошибка «полтора
#      часа» §7.19 (значение и история не тронуты) → /help §7.28 ПОВЕРХ
#      диалога (фаза живёт) → «5:30» → §7.18 «5 ч 20 мин → 5 ч 30 мин»;
#   6. строка в settings_history: ровно одна, old=320/new=330 (FR-46);
#   7. расчёты от нового значения: подсказка до правки — окно 13:20–14:00,
#      после правки — 13:30–14:10 (подъём 08:00 + новая минимальная
#      граница 5 ч 30 мин; FR-24/D16 — пересчёт без перезаписи событий);
#   8. /delete_all → §7.26 с кнопками [Да, удалить всё]/[Отмена]
#      (ОВ-5.3) → подтверждение → §7.27; БД пуста: sleep_events,
#      settings, settings_history, users (FR-54, §10.5);
#   9. /start заново → дефолты пересеяны (13 ключей, 5 ч 20 мин);
#  10. создание события после пересева работает: «😴 Заснул» →
#      «🕐 Сейчас» → §7.5, запись в sleep_events.
#
# Тексты — сверка с файлом ТЗ §7.11/§7.16–7.20/§7.26–7.28 ДОСЛОВНО
# (AC-6: эталон читается из ТЗ_Дневник_сна_ребёнка.md, не из texts.py).
#
# Правило rule 6: каждый тест несёт TC-SMOKE3-NNN в докстринге.

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import texts
from src.db import init_db
from src.handlers import (
    CALLBACK_DELETE_ALL_NO,
    CALLBACK_DELETE_ALL_YES,
    CONFIRM_DELETE_ALL_KEY,
    backfill,
    common,
    delete_all_callback,
    delete_all_command,
    events,
    help_command,
    history,
    hint,
    settings_screen,
    start,
)
from src.handlers.events import CALLBACK_TIME_MANUAL, CALLBACK_TIME_NOW
from src.repositories.settings import SettingsHistoryRepo
from src.repositories.sleep_events import SleepEventsRepo
from src.services.event import create_event, get_user
from src.services.settings import DEFAULT_KEYS, SettingsService
from src.services.state import (
    BTN_HINT,
    BTN_HISTORY,
    BTN_SETTINGS,
    BTN_SLEEP,
    BTN_WAKE,
    CALLBACK_SETTINGS_SHOW,
    CALLBACK_SLEEP,
    CALLBACK_WAKE,
    REPLY_KEYBOARD,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC_FILE = _REPO_ROOT / "ТЗ_Дневник_сна_ребёнка.md"

TELEGRAM_ID = 5001

# --------------------------------------------------------------- Часы сценария
# Фиксированные моменты (aware UTC; МСК = UTC+3), без «живых» ЧЧ:ММ:
T_CLOCK_PAIR = datetime(2026, 10, 31, 12, 20, tzinfo=timezone.utc)  # 15:20 МСК 31.10
T_NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)            # 12:00 МСК 01.11


class _ScenarioClock:
    """Фальшивые часы сценария: текущий момент переставляется шагами."""

    def __init__(self) -> None:
        self.utc = T_CLOCK_PAIR


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
    сводки/§7.14); hint.datetime — момент подсказки; history.datetime —
    момент расчёта экрана §7.20; backfill.parse_time_input — обёртка с
    моментом часов (сам класс backfill.datetime НЕ подменяется: хендлер
    проверяет isinstance(parsed, datetime)).
    """
    frozen = _frozen_datetime_class(clock)
    monkeypatch.setattr(events, "datetime", frozen)
    monkeypatch.setattr(hint, "datetime", frozen)
    monkeypatch.setattr(history, "datetime", frozen)
    real_parse = backfill.parse_time_input
    monkeypatch.setattr(
        backfill,
        "parse_time_input",
        lambda text, tz, now, max_age_days=None: real_parse(
            text, tz, clock.utc, max_age_days=max_age_days
        ),
    )


# ------------------------------------------------------------ Заглушки PTB
# (те же контракты, что в tests/test_smoke_core.py / test_smoke_recommendation.py)


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


# ---------------------------------------------------------------- Эталон §7


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


# ------------------------------------------------------------------ Тесты


class TestSmokeSettingsHistory:
    """Сквозной «неделя с настройками» пакета 3 (задача 4.1, D21–D26)."""

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

    def _press_callback(self, db_path, data: str, user_data: dict, handler):
        """Произвольный inline-колбэк (настройки, §7.26, события)."""
        update = _FakeCallbackUpdate(TELEGRAM_ID, data)
        context = _FakeContextFactory.new_context(db_path, user_data)
        asyncio.run(handler(update, context))  # type: ignore[arg-type]
        return update, context

    def _send_text(self, db_path, text: str, user_data: dict, handler):
        """Текстовое сообщение конкретному обработчику (группа 0/2)."""
        update = _FakeTextUpdate(TELEGRAM_ID, text)
        context = _FakeContextFactory.new_context(db_path, user_data)
        asyncio.run(handler(update, context))  # type: ignore[arg-type]
        return update, context

    def _command(self, db_path, handler, user_data: dict):
        update = _FakeCommandUpdate(TELEGRAM_ID)
        context = _FakeContextFactory.new_context(db_path, user_data)
        asyncio.run(handler(update, context))  # type: ignore[arg-type]
        return update, context

    # ------------------------------------------------------------- сценарий

    def test_tc_smoke3_001_week_with_settings_end_to_end(
        self, db_path, conn, frozen_now, clock
    ):
        """TC-SMOKE3-001 (FR-42…FR-54, design D21–D26): «неделя с
        настройками» — /start → события недели задним числом (одна пара
        через новый диалог §7.11×2) → «📊 История за неделю» (§7.20 +
        сегодня) → «⚙️ Настройки» (§7.16) → изменение настройки
        (§7.17 → §7.18, ошибка §7.19) → строка в settings_history →
        расчёты от нового значения → /help (§7.28) → /delete_all
        (§7.26 → §7.27) → /start заново (дефолты пересеяны) → создание
        события работает. Время зафиксировано на часах сценария."""
        user_data: dict = {}  # ОДИН user_data на весь сценарий (реальный PTB)

        # 1. /start → §7.1 + reply-панель BUG-002 (FR-5/FR-6): носитель
        #    панели со ВСЕМИ шестью кнопками, включая «📊 История за
        #    неделю» и «⚙️ Настройки» (ТЗ §6, add-settings-history 2.0),
        #    и приветствие с inline-меню; 13 дефолтных настроек (FR-23).
        start_update, _ = self._start(db_path)
        assert start_update.effective_message.replies == [
            common.REPLY_PANEL_INTRO,
            texts.START,
        ]
        assert REPLY_KEYBOARD == [
            [BTN_SLEEP, BTN_WAKE, BTN_HINT],
            [BTN_HISTORY, BTN_SETTINGS],
            ["↩️ Отменить последнее"],
        ]
        start_menu = start_update.effective_message.reply_markup
        assert CALLBACK_SLEEP in _button_datas(start_menu)
        assert CALLBACK_SETTINGS_SHOW in _button_datas(start_menu)
        user = get_user(conn, TELEGRAM_ID)
        assert len(SettingsService(conn).get_all(user["id"])) == 13

        def events_count(user_id: int) -> int:
            return len(SleepEventsRepo(conn).list_by_user(user_id))

        assert events_count(user["id"]) == 0

        # 2а. События недели задним числом (сервис): ночь перед пн и
        #     страницы пн 26.10–пт 30.10 — подъём 08:00, дневной сон
        #     13:30–15:00, отбой 22:00 (МСК = UTC+3).
        create_event(conn, user["id"], "sleep", "2026-10-25 19:00:00",
                     is_backfilled=True)  # вс 25.10 22:00 МСК
        for day in range(26, 31):  # пн 26.10 … пт 30.10
            create_event(conn, user["id"], "wake", f"2026-10-{day} 05:00:00",
                         is_backfilled=True)   # подъём 08:00
            create_event(conn, user["id"], "sleep", f"2026-10-{day} 10:30:00",
                         is_backfilled=True)   # заснул 13:30
            create_event(conn, user["id"], "wake", f"2026-10-{day} 12:00:00",
                         is_backfilled=True)   # проснулся 15:00
            create_event(conn, user["id"], "sleep", f"2026-10-{day} 19:00:00",
                         is_backfilled=True)   # отбой 22:00
        # Подъём субботы 31.10 — до пары (состояние «бодрствует»).
        create_event(conn, user["id"], "wake", "2026-10-31 05:00:00",
                     is_backfilled=True)  # сб 31.10 08:00 МСК
        assert events_count(user["id"]) == 22

        # 2б. ОДНА пара через НОВЫЙ диалог §7.11×2 (задача 3.1, FR-51,
        #     ОВ-5.1): 15:20 МСК «😴 Заснул» → «⏪ Ввести время вручную»
        #     → «13:40» (шаг 1) → «Когда проснулся?» → «15:15» (шаг 2).
        clock.utc = T_CLOCK_PAIR
        sleep_update, _ = self._press_panel(db_path, BTN_SLEEP, user_data)
        assert sleep_update.effective_message.replies == [texts.BACKFILL_CHOOSE_TIME]
        manual_update, _ = self._press_time(
            db_path, CALLBACK_TIME_MANUAL, user_data
        )
        # Шаг 1 — §7.11 ДОСЛОВНО (эталон из файла ТЗ, AC-6).
        assert manual_update.effective_message.replies == [texts.BACKFILL_ASK_TIME]
        assert texts.BACKFILL_ASK_TIME == _spec_section("7.11")
        step1_update, _ = self._send_text(
            db_path, "13:40", user_data, backfill.backfill_time_message
        )
        # Шаг 2 — «Когда проснулся?» + список форматов §7.11 (D23).
        assert step1_update.effective_message.replies == [
            events.PAIR_ASK_WAKE,
        ]
        step2_update, _ = self._send_text(
            db_path, "15:15", user_data, backfill.backfill_time_message
        )
        # Успех — §7.14-стиль по странице 31.10: сводка PB-3 + строка
        # следующего сна (BUG-005: после дневного сна — время ухода,
        # ОВ-4.2: 12 ч 10 мин набираются в 21:45 → рекомендация 22:00).
        assert step2_update.effective_message.replies == [
            "\n".join(
                [
                    "✅ Событие записано на 15:15.",
                    "",
                    "Обновлённая сводка за день:",
                    "Подъем: 08:00",
                    "Заснул: 13:40",
                    "Проснулся: 15:15",
                    "Дневной сон: 1 ч 35 мин",
                    "Бодрствование общее: 5 ч 45 мин",
                    "Рекомендованное время ухода: 22:00.",
                ]
            )
        ]
        # Диалог пары завершён — ВСЕ фазы сняты (design D23).
        for phase_key in (
            events.WAIT_EVENT_TIME_KEY,
            events.PENDING_KIND_KEY,
            events.WAIT_PAIR_SLEEP_KEY,
            events.WAIT_PAIR_WAKE_KEY,
            events.CONFIRM_PAIR_KEY,
        ):
            assert phase_key not in user_data
        pair_rows = [
            e
            for e in SleepEventsRepo(conn).list_by_user(user["id"])
            if e["timestamp"] in ("2026-10-31 10:40:00", "2026-10-31 12:15:00")
        ]
        assert [(e["event_type"], e["is_backfilled"]) for e in pair_rows] == [
            ("sleep", True),
            ("wake", True),
        ]

        # 2в. Отбой субботы 22:00 (сервис) и сегодня: подъём 08:00
        #     одиночным диалогом §7.11 (wake — легаси-ветка 4.1).
        create_event(conn, user["id"], "sleep", "2026-10-31 19:00:00",
                     is_backfilled=True)  # сб 31.10 22:00 МСК
        clock.utc = T_NOW  # далее — воскресенье 01.11, 12:00 МСК
        wake_update, _ = self._press_panel(db_path, BTN_WAKE, user_data)
        assert wake_update.effective_message.replies == [texts.BACKFILL_CHOOSE_TIME]
        wake_manual, _ = self._press_time(db_path, CALLBACK_TIME_MANUAL, user_data)
        assert wake_manual.effective_message.replies == [texts.BACKFILL_ASK_TIME]
        today_rise, _ = self._send_text(
            db_path, "08:00", user_data, backfill.backfill_time_message
        )
        assert today_rise.effective_message.replies == [
            "\n".join(
                [
                    "✅ Событие записано на 08:00.",
                    "",
                    "Обновлённая сводка за день:",
                    "Подъем: 08:00",
                    "Бодрствование общее: 4 ч",
                    "Окно дневного сна: 13:20–14:00.",
                ]
            )
        ]
        assert events_count(user["id"]) == 26

        # 3. «📊 История за неделю» → §7.20: 7 завершённых страниц
        #    (новые → старые) + блок сегодня (FR-48…FR-50). Отметки:
        #    ✓ — бодрствование 12 ч 25/30 мин в границах и отбой 22:00;
        #    ⚠️ — у вс 25.10 только отбой без подъёма (база day_start
        #    06:00 → 16 ч — вне границ); сегодня — строка без отметки
        #    (день не завершён). Кнопка событий НЕ создаёт.
        history_update, _ = self._press_panel(db_path, BTN_HISTORY, user_data)
        assert len(history_update.effective_message.replies) == 1
        screen = history_update.effective_message.replies[0]
        assert screen == "\n".join(
            [
                "📊 **История за неделю**",
                "",
                "- 31.10 (сб): бодрствование 12 ч 25 мин, сон 13:40–15:15, ночь 22:00 ✓",
                "- 30.10 (пт): бодрствование 12 ч 30 мин, сон 13:30–15:00, ночь 22:00 ✓",
                "- 29.10 (чт): бодрствование 12 ч 30 мин, сон 13:30–15:00, ночь 22:00 ✓",
                "- 28.10 (ср): бодрствование 12 ч 30 мин, сон 13:30–15:00, ночь 22:00 ✓",
                "- 27.10 (вт): бодрствование 12 ч 30 мин, сон 13:30–15:00, ночь 22:00 ✓",
                "- 26.10 (пн): бодрствование 12 ч 30 мин, сон 13:30–15:00, ночь 22:00 ✓",
                "- 25.10 (вс): бодрствование 16 ч, ночь 22:00 ⚠️",
                "",
                "- 01.11 (вс): бодрствование 4 ч",
            ]
        )
        assert events_count(user["id"]) == 26  # история ничего не пишет

        # 4. Базовая подсказка ДО правки: окно от подъёма 08:00 и
        #    дефолтной минимальной границы 5 ч 20 мин → 13:20–14:00.
        hint_before, _ = self._press_panel(db_path, BTN_HINT, user_data)
        hint_before_text = hint_before.effective_message.replies[0]
        assert "Окно дневного сна: 13:20–14:00." in hint_before_text

        # 5. «⚙️ Настройки» → экран §7.16 (FR-42, D21): КАЖДАЯ строка
        #    цитаты ТЗ — в экране дословно (эталон из файла ТЗ, AC-6);
        #    плюс 13-й состав (D21): глубина ввода §5 и цель дневного сна.
        settings_update, _ = self._press_panel(db_path, BTN_SETTINGS, user_data)
        assert len(settings_update.effective_message.replies) == 1
        settings_screen_text = settings_update.effective_message.replies[0]
        assert settings_screen_text.splitlines()[0] == texts.SETTINGS_SCREEN_HEADER
        for spec_line in _spec_section("7.16").splitlines():
            if spec_line:
                assert spec_line in settings_screen_text, spec_line
        assert "- Максимальная глубина ввода: 3 дня" in settings_screen_text
        assert "- Целевая длительность дневного сна: 1 ч 30 мин" in settings_screen_text
        assert settings_screen_text.splitlines()[-1] == texts.SETTINGS_SCREEN_FOOTER
        assert events_count(user["id"]) == 26  # настройки событий не создают

        # 6а. Нажатие параметра → §7.17 ДОСЛОВНО (имя, текущее, формат).
        edit_update, _ = self._press_callback(
            db_path,
            f"{settings_screen.CALLBACK_EDIT_PREFIX}sleep_min_before_nap",
            user_data,
            settings_screen.settings_callback,
        )
        assert edit_update.effective_message.replies == [
            texts.settings_ask_value_text(
                "Минимум бодрствования до дневного сна",
                "5 ч 20 мин",
                "`5:20` или `5ч20м`",
            )
        ]
        assert (
            texts.settings_ask_value_text(
                "Минимум бодрствования до дневного сна",
                "5 ч 20 мин",
                "`5:20` или `5ч20м`",
            )
            == _spec_section("7.17")
        )
        assert user_data.get(settings_screen.WAIT_SETTING_VALUE) == (
            "sleep_min_before_nap"
        )

        # 6б. Ошибка формата §7.19 (FR-45): значение НЕ изменено, в
        #     settings_history записи НЕТ, фаза сохранена (повторный ввод).
        bad_update, _ = self._send_text(
            db_path, "полтора часа", user_data,
            settings_screen.setting_value_message,
        )
        assert bad_update.effective_message.replies == [
            texts.settings_bad_value_text("`5:20` или `5ч20м`")
        ]
        assert (
            texts.settings_bad_value_text("`5:20` или `5ч20м`")
            == _spec_section("7.19")
        )
        assert SettingsService(conn).get(user["id"], "sleep_min_before_nap") == 320
        assert SettingsHistoryRepo(conn).list_for_user(user["id"]) == []
        assert user_data.get(settings_screen.WAIT_SETTING_VALUE) == (
            "sleep_min_before_nap"
        )

        # 6в. /help ПОВЕРХ диалога (FR-53, D26): §7.28 дословно, фаза
        #     WAIT_SETTING_VALUE не погашена, БД не тронута.
        help_update, _ = self._command(db_path, help_command, user_data)
        assert help_update.effective_message.replies == [texts.HELP]
        assert texts.HELP == _spec_section("7.28")
        assert user_data.get(settings_screen.WAIT_SETTING_VALUE) == (
            "sleep_min_before_nap"
        )
        assert events_count(user["id"]) == 26

        # 6г. Успех §7.18 «старое → новое» (FR-44): 320 → 330 («5:30»).
        ok_update, _ = self._send_text(
            db_path, "5:30", user_data, settings_screen.setting_value_message,
        )
        assert ok_update.effective_message.replies == [
            texts.settings_updated_text(
                "Минимум бодрствования до дневного сна", "5 ч 20 мин", "5 ч 30 мин",
            )
        ]
        assert (
            texts.settings_updated_text(
                "Минимум бодрствования до дневного сна", "5 ч 20 мин", "5 ч 30 мин",
            )
            == _spec_section("7.18")
        )
        assert settings_screen.WAIT_SETTING_VALUE not in user_data

        # 7. Строка в settings_history (FR-46, §5.1/§8.3): РОВНО одна —
        #     old=320/new=330 одной операцией с обновлением settings.
        assert SettingsService(conn).get(user["id"], "sleep_min_before_nap") == 330
        history_rows = SettingsHistoryRepo(conn).list_for_user(user["id"])
        assert len(history_rows) == 1
        assert history_rows[0]["key"] == "sleep_min_before_nap"
        assert history_rows[0]["old_value"] == "320"
        assert history_rows[0]["new_value"] == "330"

        # 8. Расчёты от НОВОГО значения (FR-24/D16): окно дневного сна
        #    пересчитано от подъёма 08:00: начало = подъём + новая
        #    минимальная граница 5 ч 30 мин → 13:30 (было 13:20); конец —
        #    от максимума 6 ч, не менялся (события не перезаписывались).
        hint_after, _ = self._press_panel(db_path, BTN_HINT, user_data)
        hint_after_text = hint_after.effective_message.replies[0]
        assert hint_after_text == "\n".join(
            [
                "Бодрствование с последнего пробуждения: 4 ч. "
                "Суммарно за сегодня: 4 ч.",
                "До нижней границы (12 ч) осталось: 8 ч. "
                "До верхней границы (12 ч 30 мин) осталось: 8 ч 30 мин.",
                "",
                "Окно дневного сна: 13:30–14:00.",
            ]
        )
        assert "13:20" not in hint_after_text

        # 9. /delete_all → §7.26 с кнопками [Да, удалить всё]/[Отмена]
        #    (ОВ-5.3, D25) и фазой подтверждения; НИЧЕГО не удалено до
        #     подтверждения.
        confirm_update, _ = self._command(
            db_path, delete_all_command, user_data
        )
        assert confirm_update.effective_message.replies == [texts.DELETE_ALL_CONFIRM]
        assert texts.DELETE_ALL_CONFIRM == _spec_section("7.26").splitlines()[0]
        assert _button_texts(confirm_update.effective_message.reply_markup) == [
            "Да, удалить всё",
            "Отмена",
        ]
        assert _button_datas(confirm_update.effective_message.reply_markup) == [
            CALLBACK_DELETE_ALL_YES,
            CALLBACK_DELETE_ALL_NO,
        ]
        assert user_data.get(CONFIRM_DELETE_ALL_KEY) is True
        assert events_count(user["id"]) == 26  # без подтверждения удалений нет

        # 10. Подтверждение «Да, удалить всё» → §7.27 дословно; удалены
        #     ВСЕ 4 сущности своего user_id (FR-54, §10.5, одна транзакция).
        deleted_update, _ = self._press_callback(
            db_path, CALLBACK_DELETE_ALL_YES, user_data, delete_all_callback
        )
        assert deleted_update.effective_message.replies == [texts.DELETE_ALL_DONE]
        assert texts.DELETE_ALL_DONE == _spec_section("7.27")
        assert conn.execute(
            "SELECT COUNT(*) FROM sleep_events WHERE user_id = ?", (user["id"],)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM settings WHERE user_id = ?", (user["id"],)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM settings_history WHERE user_id = ?", (user["id"],)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM users WHERE id = ?", (user["id"],)
        ).fetchone()[0] == 0
        # Все фазы диалогов после удаления сняты.
        assert CONFIRM_DELETE_ALL_KEY not in user_data
        assert settings_screen.WAIT_SETTING_VALUE not in user_data

        # 11. /start заново → дефолты пересеяны (D14): 13 ключей,
        #     правка 330 откатилась к дефолту 320 (данные удалены).
        restart_update, _ = self._start(db_path)
        assert restart_update.effective_message.replies == [
            common.REPLY_PANEL_INTRO,
            texts.START,
        ]
        user2 = get_user(conn, TELEGRAM_ID)
        assert SettingsService(conn).get_all(user2["id"]) == dict(DEFAULT_KEYS)
        assert SettingsService(conn).get(user2["id"], "sleep_min_before_nap") == 320
        assert events_count(user2["id"]) == 0

        # 12. Создание события после пересева работает: «😴 Заснул» →
        #     «🕐 Сейчас» 12:00 МСК → §7.5 с фактами пустого дня
        #     (BUG-004: 12:00 − 06:00 day_start = 6 ч).
        sleep2_update, _ = self._press_panel(db_path, BTN_SLEEP, user_data)
        assert sleep2_update.effective_message.replies == [texts.BACKFILL_CHOOSE_TIME]
        now_update, _ = self._press_time(db_path, CALLBACK_TIME_NOW, user_data)
        assert now_update.effective_message.replies[0].splitlines() == [
            "✅ Засыпание записано в 12:00.",
            "Сон идёт.",
            "Заснул: 12:00",
            "Бодрствование общее: 6 ч",
        ]
        rows = SleepEventsRepo(conn).list_by_user(user2["id"])
        assert [e["event_type"] for e in rows] == ["sleep"]
        assert rows[0]["is_backfilled"] is False  # «Сейчас», не задним числом

    def test_tc_smoke3_002_key_texts_match_spec_verbatim(self):
        """TC-SMOKE3-002 (AC-6, ТЗ §7.16–7.22/§7.26–7.28): тексты
        настроек, истории, удаления и справки сверены с файлом ТЗ
        ДОСЛОВНО — эталон читается из ТЗ_Дневник_сна_ребёнка.md, а не
        копируется в тест (паттерн TC-SMOKE-002/TC-SMOKE2-002)."""
        # §7.16 — шапка, подвал, заголовки групп.
        spec_716 = _spec_section("7.16").splitlines()
        assert texts.SETTINGS_SCREEN_HEADER == spec_716[0]
        assert texts.SETTINGS_SCREEN_FOOTER == spec_716[-1]
        for group in (
            texts.SETTINGS_GROUP_DAY,
            texts.SETTINGS_GROUP_NIGHT,
            texts.SETTINGS_GROUP_WAKE,
            texts.SETTINGS_GROUP_MISC,
        ):
            assert f"**{group}**" in spec_716
        # §7.17–7.19 — диалог изменения (пример ТЗ: 5 ч 20 мин → 5 ч 30 мин).
        assert texts.settings_ask_value_text(
            "Минимум бодрствования до дневного сна", "5 ч 20 мин", "`5:20` или `5ч20м`"
        ) == _spec_section("7.17")
        assert texts.settings_updated_text(
            "Минимум бодрствования до дневного сна", "5 ч 20 мин", "5 ч 30 мин"
        ) == _spec_section("7.18")
        assert texts.settings_bad_value_text(
            "`5:20` или `5ч20м`"
        ) == _spec_section("7.19")
        # §7.20 — шапка и формат строки дня (цифры примера ТЗ).
        assert texts.HISTORY_WEEK_HEADER == _spec_section("7.20").splitlines()[0]
        assert texts.history_day_line(
            "08.10 (вт)",
            ["бодрствование 12 ч 20 мин", "сон 13:30–15:05", "ночь 22:10"],
            " ✓",
        ) == _spec_section("7.20").splitlines()[2]
        # §7.21/§7.22 — блоки «сегодня» (§7.22 — с подстановкой времени).
        assert texts.HISTORY_EMPTY_TODAY == _spec_section("7.21")
        assert texts.history_stale_text("07:15") == _spec_section("7.22")
        # §7.26/§7.27 — удаление: вопрос дословно; состав кнопок —
        # решение продакта ОВ-5.3 ([Да, удалить всё] вместо [Да, удалить]).
        assert texts.DELETE_ALL_CONFIRM == _spec_section("7.26").splitlines()[0]
        assert texts.BTN_DELETE_ALL_YES == "Да, удалить всё"
        assert texts.BTN_DELETE_ALL_CANCEL == "Отмена"
        assert texts.DELETE_ALL_DONE == _spec_section("7.27")
        # §7.28 — справка /help дословно.
        assert texts.HELP == _spec_section("7.28")
        # Кнопки ТЗ §6 дословно (входы сценария).
        assert BTN_SLEEP == "😴 Заснул"
        assert BTN_WAKE == "☀️ Проснулся"
        assert BTN_HINT == "⏰ Подсказка сейчас"
        assert BTN_HISTORY == "📊 История за неделю"
        assert BTN_SETTINGS == "⚙️ Настройки"
        assert "↩️ Отменить последнее" in REPLY_KEYBOARD[2]
