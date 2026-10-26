# Юнит-тесты глубины ввода задним числом — 3 дня
# (change-пакет add-core-diary, задача 4.3).
#
# Критерии готовности 4.3 (FR-17; design D14; ТЗ §7.15):
#   - время 4 дня назад → TOO_OLD (парсер) / §7.15 дословно (handler),
#     записи нет, фаза диалога снята;
#   - 2 дня назад — проходит;
#   - ровно 3 дня назад — проходит: «старше» = СТРОГО больше 3 дней
#     (граница включена);
#   - кастомная глубина читается из settings (backfill_depth_days),
#     дефолт не хардкодится (design D14).
#
# Правило rule 6: каждый тест несёт TC-BFD-DEP-NNN в первой строке
# докстринга.

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src import texts
from src.db import init_db
from src.handlers import backfill
from src.handlers.events import PENDING_KIND_KEY, WAIT_EVENT_TIME_KEY
from src.repositories.settings import SettingsRepo
from src.services.backfill_parser import FUTURE, TOO_OLD, parse_time_input
from src.services.event import get_user
from src.services.settings import SettingsService

TZ = ZoneInfo("Europe/Moscow")  # UTC+3, без перехода на летнее время


def _now_utc(local_year, local_month, local_day, local_h, local_m) -> datetime:
    """Момент ввода: локальное время Москвы → aware-UTC."""
    local = datetime(local_year, local_month, local_day, local_h, local_m)
    return local.replace(tzinfo=TZ).astimezone(timezone.utc)


# Сценарий FR-17: сейчас 08:00 10 октября (локальное время пользователя).
_NOW_08 = _now_utc(2026, 10, 10, 8, 0)


def _ddmm(d: datetime) -> str:
    """Формат «DD.MM» ввода парсеру (день/месяц без обязательного нуля)."""
    return f"{d.day:02d}.{d.month:02d}"


# ------------------------------------------------- Парсер: исходы FR-17


def test_tc_bfd_dep_001_four_days_ago_is_too_old():
    """TC-BFD-DEP-001: 4 дня назад при глубине 3 → TOO_OLD (не None/FUTURE)."""
    four_days_ago = _NOW_08 - timedelta(days=4)
    text = f"{_ddmm(four_days_ago.astimezone(TZ))} 08:00"
    parsed = parse_time_input(text, TZ, _NOW_08, max_age_days=3)
    assert parsed is TOO_OLD
    # Исходы различаются: TOO_OLD не есть None и не есть FUTURE.
    assert parsed is not None
    assert parsed is not FUTURE


def test_tc_bfd_dep_002_two_days_ago_passes():
    """TC-BFD-DEP-002: 2 дня назад при глубине 3 — проходит (datetime)."""
    two_days_ago = _NOW_08 - timedelta(days=2)
    text = f"{_ddmm(two_days_ago.astimezone(TZ))} 08:00"
    parsed = parse_time_input(text, TZ, _NOW_08, max_age_days=3)
    assert isinstance(parsed, datetime)
    assert parsed == two_days_ago


def test_tc_bfd_dep_003_exactly_three_days_ago_passes_boundary():
    """TC-BFD-DEP-003: ровно 3 дня назад — проходит (строгое «старше»).

    Граница: now - parsed == 3 дня включено; 3 дня + 1 минута — TOO_OLD.
    """
    exact = _NOW_08 - timedelta(days=3)
    text = f"{_ddmm(exact.astimezone(TZ))} 08:00"
    parsed = parse_time_input(text, TZ, _NOW_08, max_age_days=3)
    assert isinstance(parsed, datetime)
    assert parsed == exact

    minute_beyond = _NOW_08 - timedelta(days=3, minutes=1)
    text2 = f"{_ddmm(minute_beyond.astimezone(TZ))} 07:59"
    assert parse_time_input(text2, TZ, _NOW_08, max_age_days=3) is TOO_OLD


def test_tc_bfd_dep_004_custom_depth_from_argument():
    """TC-BFD-DEP-004: глубина берётся из аргумента (design D14 — без
    хардкода дефолта): при глубине 7 четыре дня назад проходят, при
    глубине 1 — два дня назад уже TOO_OLD; None — проверки нет."""
    four_days_ago = _NOW_08 - timedelta(days=4)
    text4 = f"{_ddmm(four_days_ago.astimezone(TZ))} 08:00"
    assert isinstance(
        parse_time_input(text4, TZ, _NOW_08, max_age_days=7), datetime
    )
    two_days_ago = _NOW_08 - timedelta(days=2)
    text2 = f"{_ddmm(two_days_ago.astimezone(TZ))} 08:00"
    assert parse_time_input(text2, TZ, _NOW_08, max_age_days=1) is TOO_OLD
    # None — проверка глубины не выполняется (обратная совместимость 4.2).
    assert isinstance(
        parse_time_input(text4, TZ, _NOW_08, max_age_days=None), datetime
    )


# ------------------------------------------- Handler: диалог и §7.15
# (заглушки PTB — те же контракты, что в tests/test_backfill_dialog.py)


class _FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, reply, reply_markup=None):
        self.replies.append(reply)


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


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


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "bot.db"


@pytest.fixture()
def conn(db_path):
    c = init_db(db_path)
    yield c
    c.close()


def _send_text(db_path, text: str, user_data: dict, telegram_id: int = 3001):
    """Провести текстовое сообщение через реальный обработчик backfill."""
    update = _FakeTextUpdate(telegram_id, text)
    context = _FakeContext(db_path)
    context.user_data = user_data
    asyncio.run(backfill.backfill_time_message(update, context))  # type: ignore[arg-type]
    return update, context


def _phase_data() -> dict:
    return {WAIT_EVENT_TIME_KEY: True, PENDING_KIND_KEY: "sleep"}


def test_tc_bfd_dep_005_handler_too_old_7_15_no_record_phase_cleared(
    db_path, conn
):
    """TC-BFD-DEP-005 (FR-17): 4 дня назад в диалоге → §7.15 дословно,
    записи нет, фаза снята (диалог завершён)."""
    now_local = datetime.now(TZ)
    target = now_local - timedelta(days=4)
    text = f"{_ddmm(target)} {target:%H:%M}"

    update, context = _send_text(db_path, text, _phase_data())

    # §7.15 дословно (ТЗ §7.15 / texts.BACKFILL_TOO_OLD).
    assert update.effective_message.replies == [texts.BACKFILL_TOO_OLD]
    assert texts.BACKFILL_TOO_OLD == "Нельзя вводить события старше 3 дней."
    # Записи нет.
    user = get_user(conn, 3001)
    assert conn.execute(
        "SELECT COUNT(*) FROM sleep_events WHERE user_id = ?", (user["id"],)
    ).fetchone()[0] == 0
    # Фаза снята — диалог завершён (решение ПМ, задача 4.3).
    assert WAIT_EVENT_TIME_KEY not in context.user_data


def test_tc_bfd_dep_006_handler_two_days_ago_passes(db_path, conn):
    """TC-BFD-DEP-006: 2 дня назад в диалоге — проходит (подтверждение,
    дефолтная глубина 3 из settings)."""
    now_local = datetime.now(TZ)
    target = now_local - timedelta(days=2)
    text = f"{_ddmm(target)} {target:%H:%M}"

    update, context = _send_text(db_path, text, _phase_data())

    assert len(update.effective_message.replies) == 1
    assert "принято" in update.effective_message.replies[0]
    assert WAIT_EVENT_TIME_KEY not in context.user_data


def test_tc_bfd_dep_007_handler_custom_depth_from_settings(db_path, conn):
    """TC-BFD-DEP-007 (design D14): глубина читается из settings через
    SettingsService — backfill_depth_days=7 пропускает 4 дня назад,
    backfill_depth_days=1 отклоняет 2 дня назад."""
    user = get_user(conn, 3002)
    now_local = datetime.now(TZ)

    # Кастомная глубина 7: 4 дня назад проходят.
    SettingsRepo(conn).upsert(user["id"], "backfill_depth_days", "7")
    target = now_local - timedelta(days=4)
    update, _ = _send_text(
        db_path, f"{_ddmm(target)} {target:%H:%M}", _phase_data(), 3002
    )
    assert "принято" in update.effective_message.replies[0]

    # Кастомная глубина 1: 2 дня назад → §7.15, фаза снята.
    SettingsRepo(conn).upsert(user["id"], "backfill_depth_days", "1")
    target = now_local - timedelta(days=2)
    update, context = _send_text(
        db_path, f"{_ddmm(target)} {target:%H:%M}", _phase_data(), 3002
    )
    assert update.effective_message.replies == [texts.BACKFILL_TOO_OLD]
    assert WAIT_EVENT_TIME_KEY not in context.user_data

    # Сервис возвращает приведённое к int значение (контракт D14/FR-24).
    assert SettingsService(conn).get(user["id"], "backfill_depth_days") == 1
