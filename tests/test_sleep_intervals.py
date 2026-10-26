"""Интервалы сна из сырой последовательности sleep/wake (FR-13; design D6; ТЗ §10.5).

Каждый тест помечен идентификатором TC-INT-xxx в докстринге.
"""

from datetime import datetime

from src.services.calculation import build_sleep_intervals


def _dt(day: int, h: int, m: int) -> datetime:
    """Naive-момент октября 2026 (день 10 = 10.10, день 11 = 11.10)."""
    return datetime(2026, 10, day, h, m)


def test_tc_int_001_unclosed_sleep_closed_by_now_local() -> None:
    """TC-INT-001: незакрытый sleep → интервал до момента расчёта (now_local)."""
    events = [("sleep", _dt(10, 13, 0)), ("wake", _dt(10, 14, 0)), ("sleep", _dt(10, 15, 0))]
    intervals = build_sleep_intervals(events, now_local=_dt(10, 16, 30))
    assert [(i.start_local, i.end_local) for i in intervals] == [
        (_dt(10, 13, 0), _dt(10, 14, 0)),
        (_dt(10, 15, 0), _dt(10, 16, 30)),
    ]


def test_tc_int_002_unclosed_sleep_without_now_ends_at_day_page_end() -> None:
    """TC-INT-002: незакрытый sleep без now_local → до конца страницы дня (06:00 след. дня)."""
    events = [("sleep", _dt(10, 13, 0))]
    intervals = build_sleep_intervals(events, now_local=None)
    assert [(i.start_local, i.end_local) for i in intervals] == [
        (_dt(10, 13, 0), _dt(11, 6, 0))
    ]


def test_tc_int_003_wake_without_sleep_ignored() -> None:
    """TC-INT-003: wake без парного sleep — игнорируется, интервалов нет."""
    events = [("wake", _dt(10, 9, 0)), ("wake", _dt(10, 12, 0))]
    assert build_sleep_intervals(events, now_local=_dt(10, 13, 0)) == []


def test_tc_int_004_wake_before_first_sleep_ignored() -> None:
    """TC-INT-004: wake ПЕРЕД первым sleep не открывает интервал (после сортировки)."""
    events = [
        ("sleep", _dt(10, 13, 0)),
        ("wake", _dt(10, 14, 0)),
        ("wake", _dt(10, 12, 0)),
    ]
    intervals = build_sleep_intervals(events, now_local=_dt(10, 15, 0))
    assert [(i.start_local, i.end_local) for i in intervals] == [
        (_dt(10, 13, 0), _dt(10, 14, 0))
    ]


def test_tc_int_005_events_out_of_order_sorted_before_pairing() -> None:
    """TC-INT-005: события вне хронологического порядка — сортировка по timestamp."""
    events = [
        ("wake", _dt(10, 15, 5)),   # идёт ПЕРЕД sleep в списке
        ("sleep", _dt(10, 13, 30)),
    ]
    intervals = build_sleep_intervals(events, now_local=_dt(10, 16, 0))
    assert [(i.start_local, i.end_local) for i in intervals] == [
        (_dt(10, 13, 30), _dt(10, 15, 5))
    ]


def test_tc_int_006_normal_sequence_single_interval() -> None:
    """TC-INT-006: нормальная последовательность sleep→wake — один интервал."""
    events = [("sleep", _dt(10, 22, 10)), ("wake", _dt(11, 7, 20))]
    intervals = build_sleep_intervals(events, now_local=_dt(11, 9, 0))
    assert [(i.start_local, i.end_local) for i in intervals] == [
        (_dt(10, 22, 10), _dt(11, 7, 20))
    ]


def test_tc_int_007_empty_events_no_intervals() -> None:
    """TC-INT-007: пустая последовательность → пустой список интервалов."""
    assert build_sleep_intervals([], now_local=_dt(10, 12, 0)) == []


def test_tc_int_008_repeated_sleep_opens_from_first() -> None:
    """TC-INT-008: повторный sleep при открытом интервале игнорируется (первое засыпание)."""
    events = [
        ("sleep", _dt(10, 13, 0)),
        ("sleep", _dt(10, 13, 10)),
        ("wake", _dt(10, 14, 0)),
    ]
    intervals = build_sleep_intervals(events, now_local=_dt(10, 15, 0))
    assert [(i.start_local, i.end_local) for i in intervals] == [
        (_dt(10, 13, 0), _dt(10, 14, 0))
    ]
