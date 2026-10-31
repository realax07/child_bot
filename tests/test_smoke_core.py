# СКВОЗНОЙ smoke пакета add-core-diary (change-пакет, задача 5.4;
# критерий приемки ПЛАНа п.3: бот стартует, /start, две кнопки, ввод
# задним числом, отмена).
#
# Сценарий в тестовой среде: фейковый PTB (контракты tests/test_events_now.py /
# tests/test_backfill_dialog.py), РЕАЛЬНАЯ временная БД (init_db в tmp_path),
# реальные обработчики start/events/backfill/undo. Фазы диалога живут в ОДНОМ
# context.user_data между нажатиями — как в реальном PTB у одного пользователя.
#
# Проверяется:
#   1. /start → §7.1, меню с «😴 Заснул» (FR-5, FR-6);
#   2. «😴 Заснул» → §7.10 («🕐 Сейчас» / «⏪ Ввести время вручную») (FR-15);
#   3. «🕐 Сейчас» → §7.5, меню с «☀️ Проснулся» (FR-7);
#   4. «☀️ Проснулся» → §7.10 → «⏪ Ввести время вручную» → §7.11 →
#      ввод «вчера 22:30» → §7.14 со сводкой, is_backfilled=1
#      (FR-15, FR-16, FR-19);
#   5. «↩️ Отменить последнее» → последнее событие удалено, меню вернулось
#      по актуальному состоянию (FR-21, design D12);
#   6. сверка ключевых текстов с ТЗ §7 ДОСЛОВНО — эталон читается из файла
#      ТЗ_Дневник_сна_ребёнка.md, а не из src/texts.py (AC-6);
#   7. бот стартует: `python -m src.main --check` (каркас задачи 1.1).
#
# Правило rule 6: каждый тест несёт TC-SMOKE-NNN в первой строке докстринга.

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import texts
from src.db import init_db
from src.handlers import backfill, common, events, start, undo
from src.handlers.events import CALLBACK_TIME_MANUAL, CALLBACK_TIME_NOW
from src.repositories.sleep_events import SleepEventsRepo
from src.services.event import get_user
from src.services.settings import SettingsService
from src.services.state import (
    BTN_SLEEP,
    BTN_UNDO,
    BTN_WAKE,
    CALLBACK_SLEEP,
    CALLBACK_UNDO,
    CALLBACK_WAKE,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC_FILE = _REPO_ROOT / "ТЗ_Дневник_сна_ребёнка.md"

TELEGRAM_ID = 9001

# Фиксированное «сейчас» сценария: 9:00 UTC = 12:00 МСК 10.10 (правило
# волны 6 — время фиксируется в фикстуре, ассерты без «живых» ЧЧ:ММ).
FIXED_NOW_UTC = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """datetime с замороженным now() — время сценария детерминировано."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        if tz is not None:
            return FIXED_NOW_UTC.astimezone(tz)
        return FIXED_NOW_UTC.replace(tzinfo=None)


@pytest.fixture()
def frozen_now(monkeypatch):
    """Фиксировать «сейчас» для сценария (правило волны 6).

    events.datetime — замороженный класс («Сейчас» и расчётный момент
    сводки); undo.datetime — тот же класс («сейчас» мини-состояния после
    отмены, PB-4); backfill.parse_time_input — обёртка с фиксированным
    моментом ввода (класс backfill.datetime НЕ подменяется: хендлер
    проверяет isinstance(parsed, datetime)).
    """
    monkeypatch.setattr(events, "datetime", _FrozenDatetime)
    monkeypatch.setattr(undo, "datetime", _FrozenDatetime)
    real_parse = backfill.parse_time_input
    monkeypatch.setattr(
        backfill,
        "parse_time_input",
        lambda text, tz, now, max_age_days=None: real_parse(
            text, tz, _FrozenDatetime.now(timezone.utc), max_age_days=max_age_days
        ),
    )


# ------------------------------------------------------------ Заглушки PTB
# (те же контракты, что в tests/test_events_now.py — задача 2.3)


class _FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.reply_markup = None
        self.replies: list[str] = []

    async def reply_text(self, reply, reply_markup=None):
        self.replies.append(reply)
        if reply_markup is not None:
            # Перерисованное меню (edit_reply_markup) не затирать: reply_text
            # без клавиатуры идёт отдельным сообщением.
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
    def __init__(self, user_id: int, language_code: str | None = None) -> None:
        self.id = user_id
        self.language_code = language_code


class _FakeCommandUpdate:
    """Update для команды /start."""

    def __init__(self, user_id: int, language_code: str | None = None) -> None:
        self.effective_user = _FakeUser(user_id, language_code)
        self.callback_query = None
        self.message = _FakeMessage()
        self.effective_message = self.message


class _FakeCallbackUpdate:
    """Update для CallbackQuery (эффективное сообщение — сообщение кнопок)."""

    def __init__(self, user_id: int, data: str) -> None:
        self.effective_user = _FakeUser(user_id)
        self.callback_query = _FakeCallbackQuery(data)
        self.effective_message = self.callback_query.message


class _FakeTextUpdate:
    """Update для текстового сообщения."""

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
    return [
        btn.callback_data for row in markup.inline_keyboard for btn in row
    ]


# ---------------------------------------------------------------- Эталон §7


def _spec_section(section: str) -> str:
    """Дословный текст раздела ТЗ §7.x (blockquote после заголовка, AC-6).

    Эталон читается из файла ТЗ, а не копируется в тест: сверка не может
    «поехать» вместе с texts.py.
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


class TestSmokeCore:
    """Сквозной сценарий пакета 1 одним пользователем (задача 5.4).

    Общий context.user_data и общая БД между нажатиями — как в реальном PTB.
    """

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
        update = _FakeCommandUpdate(TELEGRAM_ID, language_code="ru")
        context = _FakeContextFactory.new_context(db_path)
        asyncio.run(start.start_command(update, context))  # type: ignore[arg-type]
        return update, context

    def _press(self, db_path, data: str, user_data: dict):
        update = _FakeCallbackUpdate(TELEGRAM_ID, data)
        context = _FakeContextFactory.new_context(db_path, user_data)
        if data.startswith("event:"):
            handler = events.event_button_callback
        elif data.startswith("time:"):
            handler = events.time_choose_callback
        else:
            handler = undo.undo_last_callback
        asyncio.run(handler(update, context))  # type: ignore[arg-type]
        return update, context

    def _send_text(self, db_path, text: str, user_data: dict):
        update = _FakeTextUpdate(TELEGRAM_ID, text)
        context = _FakeContextFactory.new_context(db_path, user_data)
        asyncio.run(backfill.backfill_time_message(update, context))  # type: ignore[arg-type]
        return update, context

    # ------------------------------------------------------------- сценарий

    def test_tc_smoke_001_end_to_end_scenario(self, db_path, conn, frozen_now):
        """TC-SMOKE-001: /start → «😴 Заснул» → «🕐 Сейчас» → «☀️
        Проснулся» → вручную «вчера 22:30» (§7.14, is_backfilled=1) →
        «↩️ Отменить последнее»; меню и БД на каждом шаге (PLAN п.3).
        Время зафиксировано: 9:00 UTC = 12:00 МСК 10.10."""
        user_data: dict = {}  # ОДИН user_data на весь сценарий (реальный PTB)

        # 1. /start → §7.1, меню с «😴 Заснул» (FR-5, FR-6).
        # Хотфикс BUG-002: /start сначала шлёт сообщение-носитель постоянной
        # reply-панели, затем приветствие с inline-меню (PLAN-BUG-002 п. 3/4).
        start_update, _ = self._start(db_path)
        assert start_update.effective_message.replies == [
            common.REPLY_PANEL_INTRO,
            texts.START,
        ]
        start_menu = start_update.effective_message.reply_markup
        assert BTN_SLEEP in _button_texts(start_menu)
        assert BTN_WAKE not in _button_texts(start_menu)
        assert BTN_UNDO in _button_texts(start_menu)
        assert CALLBACK_SLEEP in _button_datas(start_menu)

        # 13 дефолтных настроек засеяны (FR-23)
        user = get_user(conn, TELEGRAM_ID)
        assert len(SettingsService(conn).get_all(user["id"])) == 13

        # 2. «😴 Заснул» → §7.10 с «🕐 Сейчас»/«⏪ Ввести время вручную»,
        #    событие ещё НЕ создано (FR-15)
        sleep_update, _ = self._press(db_path, CALLBACK_SLEEP, user_data)
        assert sleep_update.callback_query.message.text == texts.BACKFILL_CHOOSE_TIME
        assert texts.BTN_TIME_NOW in _button_texts(
            sleep_update.callback_query.message.reply_markup
        )
        assert texts.BTN_TIME_MANUAL in _button_texts(
            sleep_update.callback_query.message.reply_markup
        )
        assert SleepEventsRepo(conn).list_by_user(user["id"]) == []

        # 3. «🕐 Сейчас» → §7.5 с фактом дня, меню переключилось на
        #    «☀️ Проснулся» (FR-7 в ред. волны 6, PB-3): пустой день —
        #    только «Бодрствование общее: 24 ч»; «Крайнее пробуждение»
        #    НЕ выводится (решение продакта 29.10)
        now_update, _ = self._press(
            db_path, CALLBACK_TIME_NOW, user_data
        )
        events_menu = now_update.callback_query.message.reply_markup
        assert BTN_WAKE in _button_texts(events_menu)
        assert BTN_SLEEP not in _button_texts(events_menu)
        assert len(now_update.effective_message.replies) == 1
        answer_75 = now_update.effective_message.replies[0]
        assert answer_75.splitlines() == [
            "✅ Засыпание записано в 12:00.",
            "Сон идёт.",
            "Заснул: 12:00",
            "Бодрствование общее: 24 ч",
        ]
        assert "Крайнее пробуждение" not in answer_75

        rows = SleepEventsRepo(conn).list_by_user(user["id"])
        assert [e["event_type"] for e in rows] == ["sleep"]
        assert rows[0]["is_backfilled"] is False  # «Сейчас», не задним числом

        # 4. «☀️ Проснулся» → §7.10 → «⏪ Ввести время вручную» → §7.11 →
        #    «вчера 22:30» → §7.14 с фактами дня страницы «вчера»
        #    (FR-15, FR-16, FR-19 в ред. волны 6 / PB-3)
        wake_update, _ = self._press(db_path, CALLBACK_WAKE, user_data)
        assert wake_update.callback_query.message.text == texts.BACKFILL_CHOOSE_TIME

        manual_update, _ = self._press(
            db_path, CALLBACK_TIME_MANUAL, user_data
        )
        assert manual_update.effective_message.replies == [texts.BACKFILL_ASK_TIME]
        # Фаза диалога сохранена между нажатиями (design D10)
        assert user_data.get(events.WAIT_EVENT_TIME_KEY) is True
        assert user_data.get(events.PENDING_KIND_KEY) == "wake"

        text_update, _ = self._send_text(db_path, "вчера 22:30", user_data)
        assert len(text_update.effective_message.replies) == 1
        answer_714 = text_update.effective_message.replies[0]
        assert answer_714.startswith("✅ Событие записано на 22:30.")
        assert "Обновлённая сводка за день:" in answer_714
        # Формат продакта (PB-3): события дня + «Бодрствование общее»;
        # абстрактных сумм «Сон: …»/«Бодрствование: …» и окон нет.
        assert "Бодрствование общее: " in answer_714
        assert "Сон:" not in answer_714
        assert "Бодрствование:" not in answer_714
        assert "Окно" not in answer_714  # окна — Пакет 2 (решение ОВ-ПО-2)
        # Диалог завершён — фазы сняты (design D10)
        assert events.WAIT_EVENT_TIME_KEY not in user_data
        assert events.PENDING_KIND_KEY not in user_data

        rows = SleepEventsRepo(conn).list_by_user(user["id"])
        # list_by_user отсортирован по timestamp: wake «вчера» — раньше sleep
        # «сейчас», проверяем состав, а не порядок строк.
        assert sorted(e["event_type"] for e in rows) == ["sleep", "wake"]
        backfilled_wake = [e for e in rows if e["event_type"] == "wake"][0]
        assert backfilled_wake["is_backfilled"] is True  # задним числом (FR-16)

        # 5. «↩️ Отменить последнее» → удалено ПОСЛЕДНЕЕ событие (wake
        #    задним числом), меню вернулось по актуальному состоянию
        #    «спит» (design D12, FR-21); подтверждение PB-4: ЧТО удалено
        #    («Отменено: Проснулся 22:30» — wake «вчера 22:30» МСК) +
        #    мини-состояние за сегодня (остался sleep 12:00 МСК — факты
        #    дня PB-3, те же строки, что на шаге 3)
        undo_update, _ = self._press(db_path, CALLBACK_UNDO, user_data)
        assert undo_update.callback_query.answers == [None]
        assert undo_update.effective_message.replies == [
            "Отменено: Проснулся 22:30.\n\n"
            "Записей за сегодня: 1.\n"
            "Заснул: 12:00\n"
            "Бодрствование общее: 24 ч"
        ]
        rows = SleepEventsRepo(conn).list_by_user(user["id"])
        assert [e["event_type"] for e in rows] == ["sleep"]  # wake удалён
        restored_menu = undo_update.callback_query.message.reply_markup
        assert BTN_WAKE in _button_texts(restored_menu)
        assert BTN_SLEEP not in _button_texts(restored_menu)
        assert BTN_UNDO in _button_texts(restored_menu)

    def test_tc_smoke_002_key_texts_match_spec_verbatim(self, conn):
        """TC-SMOKE-002: ключевые тексты §7 сверены с ТЗ ДОСЛОВНО —
        эталон берётся из файла ТЗ, а не из src/texts.py (AC-6)."""
        # §7.1 — приветствие /start
        assert texts.START == _spec_section("7.1")
        # §7.10 — вопрос выбора времени
        assert texts.BACKFILL_CHOOSE_TIME == _spec_section("7.10").splitlines()[0]
        # §7.11 — запрос времени
        assert texts.BACKFILL_ASK_TIME == _spec_section("7.11")
        # §7.12 — ошибка формата
        assert texts.BACKFILL_BAD_FORMAT == _spec_section("7.12")
        # §7.14 — эталонная цитата (фактический ответ — backfill_success_text
        # без строки окна, решение ОВ-ПО-2)
        assert texts.BACKFILL_SUCCESS == _spec_section("7.14")
        # §7.15 — слишком давно
        assert texts.BACKFILL_TOO_OLD == _spec_section("7.15")
        # §7.5 — структура ответа о засыпании (время подстановкой)
        fell_template = _spec_section("7.5")
        assert fell_template.startswith("✅ Засыпание записано в ")
        assert "Сон идёт. Крайнее пробуждение: " in fell_template
        # §7.2 — подтверждающая строка о пробуждении
        assert _spec_section("7.2").startswith("✅ Пробуждение записано в ")
        # Кнопки §7.10 дословно
        assert texts.BTN_TIME_NOW == "🕐 Сейчас"
        assert texts.BTN_TIME_MANUAL == "⏪ Ввести время вручную"
        # Кнопки меню ТЗ §6 дословно
        assert BTN_SLEEP == "😴 Заснул"
        assert BTN_WAKE == "☀️ Проснулся"
        assert BTN_UNDO == "↩️ Отменить последнее"

    def test_tc_smoke_003_bot_application_boots(self):
        """TC-SMOKE-003: бот стартует — `python -m src.main --check`
        строит Application с зарегистрированными обработчиками и завершается
        без сети (критерий приемки ПЛАНа п.3 «бот стартует»)."""
        env = dict(os.environ, BOT_TOKEN="smoke-stub-token")
        proc = subprocess.run(
            [sys.executable, "-m", "src.main", "--check"],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "OK" in proc.stdout


class _FakeContextFactory:
    """Фейковый PTB-контекст (bot_data['db_path'] + общий user_data)."""

    @staticmethod
    def new_context(db_path, user_data: dict | None = None):
        class _Context:
            def __init__(self) -> None:
                self.bot_data = {"db_path": db_path}
                self.user_data: dict = {} if user_data is None else user_data

        return _Context()
