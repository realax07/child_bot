"""Отнесение сна к странице дня (FR-12; design D8; решение ПМ 26.10).

Каждый тест помечен идентификатором TC-DAYP-xxx в докстринге.
"""

from datetime import date, datetime, time

from src.services.calculation import day_page_of, sleep_page


def _dt(day: int, h: int, m: int) -> datetime:
    """Naive-момент октября 2026 (день 10 = 10.10, день 11 = 11.10)."""
    return datetime(2026, 10, day, h, m)


def test_tc_dayp_001_overnight_night_belongs_to_start_day() -> None:
    """TC-DAYP-001: сон 22:10 (10.10) → 07:20 (11.10) — страница 10.10 (FR-12)."""
    page = sleep_page(_dt(10, 22, 10), _dt(11, 7, 20))
    assert page == date(2026, 10, 10)


def test_tc_dayp_002_start_before_day_start_previous_day() -> None:
    """TC-DAYP-002: сон 02:00 → 05:30 — страница ПРЕДЫЩЕГО календарного дня."""
    page = sleep_page(_dt(11, 2, 0), _dt(11, 5, 30))
    assert page == date(2026, 10, 10)


def test_tc_dayp_003_night_across_midnight_start_day() -> None:
    """TC-DAYP-003: сон 23:30 (10.10) → 02:00 (11.10) — страница дня начала (10.10)."""
    page = sleep_page(_dt(10, 23, 30), _dt(11, 2, 0))
    assert page == date(2026, 10, 10)


def test_tc_dayp_004_exact_day_start_is_current_day() -> None:
    """TC-DAYP-004: начало ровно 06:00 — страница текущего дня (граница включена)."""
    assert day_page_of(_dt(11, 6, 0)) == date(2026, 10, 11)
    assert sleep_page(_dt(11, 6, 0), _dt(11, 7, 30)) == date(2026, 10, 11)


def test_tc_dayp_005_minute_before_day_start_is_previous_day() -> None:
    """TC-DAYP-005: начало 05:59 — страница предыдущего дня (граница исключена)."""
    assert day_page_of(_dt(11, 5, 59)) == date(2026, 10, 10)
    assert sleep_page(_dt(11, 5, 59), _dt(11, 7, 30)) == date(2026, 10, 10)


def test_tc_dayp_006_day_sleep_within_page() -> None:
    """TC-DAYP-006: дневной сон 13:30 → 15:05 — страница того же календарного дня."""
    page = sleep_page(_dt(10, 13, 30), _dt(10, 15, 5))
    assert page == date(2026, 10, 10)


def test_tc_dayp_007_custom_day_start() -> None:
    """TC-DAYP-007: day_start из настроек (07:30): 07:00 → предыдущий день."""
    assert day_page_of(_dt(11, 7, 0), day_start=time(7, 30)) == date(2026, 10, 10)
    assert sleep_page(_dt(11, 7, 0), _dt(11, 8, 30), day_start=time(7, 30)) == date(
        2026, 10, 10
    )
