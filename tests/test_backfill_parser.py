# Юнит-тесты чистого парсера ручного ввода времени
# (change-пакет add-core-diary, задача 4.2).
#
# Критерии готовности 4.2 (FR-16, FR-20; design D10; решение №4 —
# «вчера» = прошлый календарный день, не «24 часа назад»):
#   - «HH:MM» — сегодня в TZ пользователя;
#   - «вчера HH:MM» — прошлый календарный день (сейчас 08:00 10.10,
#     ввод «вчера 22:30» → 22:30 09.10);
#   - «DD.MM HH:MM» — конкретная дата текущего года;
#   - распознанное время в будущем → сентинел FUTURE (предупреждение),
#     мусор → None (§7.12) — исходы различаются;
#   - крайние значения 23:59 / 00:00.
#
# Правило rule 6: каждый тест несёт TC-BFP-NNN в первой строке докстринга.

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.services.backfill_parser import FUTURE, parse_time_input

TZ = ZoneInfo("Europe/Moscow")  # UTC+3, без перехода на летнее время


def _now_utc(local_year, local_month, local_day, local_h, local_m) -> datetime:
    """Момент ввода: локальное время Москвы → aware-UTC."""
    local = datetime(local_year, local_month, local_day, local_h, local_m)
    return local.replace(tzinfo=TZ).astimezone(timezone.utc)


# Сценарий FR-20: сейчас 08:00 10 октября (локальное время пользователя).
_NOW_08 = _now_utc(2026, 10, 10, 8, 0)


# ------------------------------------------------------------------ Форматы


def test_tc_bfp_001_today_hhmm_returns_utc_today():
    """TC-BFP-001: «10:50» → сегодня в TZ пользователя, результат в UTC."""
    # Сейчас 11:00 локального 10.10 → 10:50 сегодня в прошлом.
    parsed = parse_time_input("10:50", TZ, _now_utc(2026, 10, 10, 11, 0))
    expected = datetime(2026, 10, 10, 10, 50, tzinfo=TZ).astimezone(timezone.utc)
    assert isinstance(parsed, datetime)
    assert parsed == expected
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def test_tc_bfp_002_vverh_calendar_day_not_24h():
    """TC-BFP-002: «вчера 22:30» (сейчас 08:00 10.10) → 22:30 09.10 (FR-20)."""
    parsed = parse_time_input("вчера 22:30", TZ, _NOW_08)
    expected = datetime(2026, 10, 9, 22, 30, tzinfo=TZ).astimezone(timezone.utc)
    assert parsed == expected


def test_tc_bfp_003_date_format_current_year():
    """TC-BFP-003: «08.10 7:15» → 7:15 8 октября текущего года."""
    parsed = parse_time_input("08.10 7:15", TZ, _NOW_08)
    expected = datetime(2026, 10, 8, 7, 15, tzinfo=TZ).astimezone(timezone.utc)
    assert parsed == expected


def test_tc_bfp_004_single_digit_hour_and_date():
    """TC-BFP-004: ведущий ноль не обязателен — «8.10 7:15» и «7:15»."""
    assert parse_time_input("8.10 7:15", TZ, _NOW_08) == parse_time_input(
        "08.10 7:15", TZ, _NOW_08
    )
    # Сейчас 11:00 10.10 → «7:15» сегодня в прошлом.
    assert parse_time_input("7:15", TZ, _now_utc(2026, 10, 10, 11, 0)) == datetime(
        2026, 10, 10, 7, 15, tzinfo=TZ
    ).astimezone(timezone.utc)


# ----------------------------------------------------- FUTURE (предупреждение)


def test_tc_bfp_005_tomorrow_is_future():
    """TC-BFP-005: «завтра 10:00» — распознано, но в будущем → FUTURE."""
    assert parse_time_input("завтра 10:00", TZ, _NOW_08) is FUTURE


def test_tc_bfp_006_today_later_time_is_future():
    """TC-BFP-006: «HH:MM» позже момента ввода (08:00 → «10:50») → FUTURE."""
    assert parse_time_input("10:50", TZ, _NOW_08) is FUTURE


def test_tc_bfp_007_date_later_today_is_future():
    """TC-BFP-007: «10.10 23:59» сегодня позже 08:00 ввода → FUTURE (FR-20)."""
    assert parse_time_input("10.10 23:59", TZ, _NOW_08) is FUTURE


# ------------------------------------------------------- Мусор → None (§7.12)


@pytest.mark.parametrize(
    "text",
    [
        "привет",
        "",
        "   ",
        "25:00",  # невалидный час
        "10:70",  # невалидные минуты
        "10:50:30",  # секунды не допускаются
        "вчера",  # без времени
        "вчера22:30",  # без пробела
        "позавчера 10:00",  # формат не поддержан
        "12.13 10:00",  # невалидный месяц
        "32.10 10:00",  # невалидный день
        "31.02 10:00",  # несуществующая дата
        "10-50",  # неверный разделитель
        "10.10",  # дата без времени
    ],
)
def test_tc_bfp_008_garbage_returns_none(text):
    """TC-BFP-008: нераспознанный ввод → None (мусор, а не FUTURE)."""
    assert parse_time_input(text, TZ, _NOW_08) is None


def test_tc_bfp_009_none_is_distinct_from_future():
    """TC-BFP-009: исходы различаются — None не есть FUTURE и наоборот."""
    garbage = parse_time_input("мусор", TZ, _NOW_08)
    future = parse_time_input("завтра 10:00", TZ, _NOW_08)
    ok = parse_time_input("вчера 22:30", TZ, _NOW_08)
    assert garbage is None
    assert garbage is not FUTURE
    assert future is FUTURE
    assert future is not None
    assert isinstance(ok, datetime)


# ------------------------------------------------------------ Края 23:59/00:00


def test_tc_bfp_010_edge_yesterday_2359_just_after_midnight():
    """TC-BFP-010: сейчас 00:05 10.10, «вчера 23:59» → прошлое (23:59 09.10)."""
    now = _now_utc(2026, 10, 10, 0, 5)
    parsed = parse_time_input("вчера 23:59", TZ, now)
    assert parsed == datetime(2026, 10, 9, 23, 59, tzinfo=TZ).astimezone(
        timezone.utc
    )


def test_tc_bfp_011_edge_today_0000_just_after_midnight():
    """TC-BFP-011: сейчас 00:05, «00:00» → 00:00 сегодня — в прошлом."""
    now = _now_utc(2026, 10, 10, 0, 5)
    parsed = parse_time_input("00:00", TZ, now)
    assert parsed == datetime(2026, 10, 10, 0, 0, tzinfo=TZ).astimezone(
        timezone.utc
    )


def test_tc_bfp_012_edge_2359_before_now_is_future():
    """TC-BFP-012: «23:59» при вводе в 08:00 — момент ещё не наступил."""
    assert parse_time_input("23:59", TZ, _NOW_08) is FUTURE


def test_tc_bfp_013_edge_yesterday_0000():
    """TC-BFP-013: «вчера 00:00» — начало прошлого календарного дня."""
    parsed = parse_time_input("вчера 00:00", TZ, _NOW_08)
    assert parsed == datetime(2026, 10, 9, 0, 0, tzinfo=TZ).astimezone(
        timezone.utc
    )


# ------------------------------------------------------------------ Пояс и tz


def test_tc_bfp_014_timezone_name_string_and_fallback():
    """TC-BFP-014: tz строкой IANA, сдвиг локального момента, fallback."""
    parsed = parse_time_input("вчера 22:30", "Europe/Moscow", _NOW_08)
    assert parsed == datetime(2026, 10, 9, 22, 30, tzinfo=TZ).astimezone(
        timezone.utc
    )
    # Пояс применяется к вводу: в UTC−11 сейчас 18:00 09.10, «09.10 23:00»
    # локально = 10.10 10:00 UTC — позже 05:00 UTC → FUTURE (FR-20).
    other = parse_time_input("09.10 23:00", "Pacific/Pago_Pago", _NOW_08)
    assert other is FUTURE
    fallback = parse_time_input("вчера 22:30", "Not/AZone", _NOW_08)
    assert fallback == parsed  # fallback Europe/Moscow


def test_tc_bfp_015_case_and_whitespace_tolerant():
    """TC-BFP-015: регистр слова и пробелы по краям не ломают разбор."""
    assert parse_time_input("  Вчера 22:30 ", TZ, _NOW_08) == parse_time_input(
        "вчера 22:30", TZ, _NOW_08
    )


def test_tc_bfp_016_naive_now_treated_as_utc():
    """TC-BFP-016: naive-момент ввода трактуется как UTC (контракт парсера)."""
    naive = datetime(2026, 10, 10, 5, 0)  # = 08:00 MSK
    assert parse_time_input("вчера 22:30", TZ, naive) == datetime(
        2026, 10, 9, 22, 30, tzinfo=TZ
    ).astimezone(timezone.utc)
