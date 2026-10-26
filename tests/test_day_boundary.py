"""Пограничные тесты разметки день/ночь (FR-9, FR-10, FR-11; design D7; ТЗ §4.2).

Каждый тест помечен идентификатором TC-DAYB-xxx в докстринге.
"""

from datetime import datetime

from src.services.calculation import (
    LATE_DAY_SLEEP_WARNING,
    classify_sleep,
)


def _dt(h: int, m: int) -> datetime:
    """Naive-время одного дня (дата не влияет на разметку границ времени)."""
    return datetime(2026, 10, 26, h, m)


def test_tc_dayb_001_night_overnight() -> None:
    """TC-DAYB-001: 22:10 → 07:20 — ночной сон (FR-9, сценарий «Ночной сон»)."""
    mark = classify_sleep(_dt(22, 10), _dt(7, 20))
    assert mark.is_night is True
    assert mark.warning is None
    assert mark.requires_check is False


def test_tc_dayb_002_day_sleep() -> None:
    """TC-DAYB-002: 13:30 → 15:05 — дневной сон (FR-9, сценарий «Дневной сон»)."""
    mark = classify_sleep(_dt(13, 30), _dt(15, 5))
    assert mark.is_night is False
    assert mark.warning is None
    assert mark.requires_check is False


def test_tc_dayb_003_late_day_with_warning() -> None:
    """TC-DAYB-003: 18:50 → 19:40 — дневной + предупреждение (FR-10)."""
    mark = classify_sleep(_dt(18, 50), _dt(19, 40))
    assert mark.is_night is False
    assert mark.warning == LATE_DAY_SLEEP_WARNING
    assert mark.requires_check is False


def test_tc_dayb_004_short_night_requires_check() -> None:
    """TC-DAYB-004: 19:30 → 20:00 — ночной + requires_check (FR-11)."""
    mark = classify_sleep(_dt(19, 30), _dt(20, 0))
    assert mark.is_night is True
    assert mark.warning is None  # текст по метке не выводится в пакете
    assert mark.requires_check is True


def test_tc_dayb_005_start_exactly_night_start() -> None:
    """TC-DAYB-005: начало ровно 19:00 — ночной (граница включена, FR-9)."""
    mark = classify_sleep(_dt(19, 0), _dt(20, 30))
    assert mark.is_night is True
    assert mark.requires_check is False  # 90 мин — не короткий сон


def test_tc_dayb_006_end_exactly_night_end() -> None:
    """TC-DAYB-006: конец ровно 07:00 — ночной (граница включена, FR-9)."""
    mark = classify_sleep(_dt(23, 30), _dt(7, 0))
    assert mark.is_night is True


def test_tc_dayb_007_end_one_minute_before_night_end() -> None:
    """TC-DAYB-007: конец 06:59 — ночной (≤ 07:00, FR-9)."""
    mark = classify_sleep(_dt(6, 30), _dt(6, 59))
    assert mark.is_night is True


def test_tc_dayb_008_end_one_minute_after_night_end() -> None:
    """TC-DAYB-008: начало 06:30, конец 07:01 — дневной (FR-9)."""
    mark = classify_sleep(_dt(6, 30), _dt(7, 1))
    assert mark.is_night is False


def test_tc_dayb_009_short_night_exactly_40_min_no_flag() -> None:
    """TC-DAYB-009: ночной сон ровно 40 мин — без requires_check (< 40, FR-11)."""
    mark = classify_sleep(_dt(19, 30), _dt(20, 10))
    assert mark.is_night is True
    assert mark.requires_check is False


def test_tc_dayb_010_short_night_39_min_flag() -> None:
    """TC-DAYB-010: ночной сон 39 мин — requires_check=True (FR-11)."""
    mark = classify_sleep(_dt(19, 30), _dt(20, 9))
    assert mark.is_night is True
    assert mark.requires_check is True


def test_tc_dayb_011_warning_window_lower_bound() -> None:
    """TC-DAYB-011: начало 18:50 (night_start − 10 мин) — дневной + warning (FR-10)."""
    mark = classify_sleep(_dt(18, 50), _dt(19, 30))
    assert mark.is_night is False
    assert mark.warning == LATE_DAY_SLEEP_WARNING


def test_tc_dayb_012_no_warning_outside_window() -> None:
    """TC-DAYB-012: начало 18:49 (раньше warn-окна) — дневной без warning (FR-10)."""
    mark = classify_sleep(_dt(18, 49), _dt(19, 30))
    assert mark.is_night is False
    assert mark.warning is None
