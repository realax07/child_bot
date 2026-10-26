"""Склейка смежных интервалов сна при коротком пробуждении
(FR-13, FR-14; design D9; ТЗ §4.3).

Каждый тест помечен идентификатором TC-MRG-xxx в докстринге.
"""

from datetime import datetime

from src.services.calculation import SleepInterval, build_sleep_intervals, merge_intervals


def _dt(day: int, h: int, m: int) -> datetime:
    """Naive-момент октября 2026."""
    return datetime(2026, 10, day, h, m)


def _interval(day: int, h1: int, m1: int, h2: int, m2: int) -> SleepInterval:
    return SleepInterval(start_local=_dt(day, h1, m1), end_local=_dt(day, h2, m2))


def test_tc_mrg_001_chain_merge_into_one_sleep() -> None:
    """TC-MRG-001 (FR-13): 13:00–14:00 + 14:00–14:15 + 14:15–15:00 при пороге 20 →
    один сон 13:00–15:00, бодрствование за период не учитывается."""
    intervals = [
        _interval(10, 13, 0, 14, 0),
        _interval(10, 14, 0, 14, 15),
        _interval(10, 14, 15, 15, 0),
    ]
    merged = merge_intervals(intervals, threshold_min=20)
    assert [(i.start_local, i.end_local) for i in merged] == [(_dt(10, 13, 0), _dt(10, 15, 0))]


def test_tc_mrg_002_wake_longer_than_threshold_two_sleeps() -> None:
    """TC-MRG-002 (FR-13): пробуждение 25 мин при пороге 20 → два отдельных сна."""
    intervals = [
        _interval(10, 13, 0, 14, 0),
        _interval(10, 14, 25, 15, 0),
    ]
    merged = merge_intervals(intervals, threshold_min=20)
    assert [(i.start_local, i.end_local) for i in merged] == [
        (_dt(10, 13, 0), _dt(10, 14, 0)),
        (_dt(10, 14, 25), _dt(10, 15, 0)),
    ]


def test_tc_mrg_003_threshold_30_merges_25_min_wake() -> None:
    """TC-MRG-003 (FR-14): порог 30 мин из settings → пробуждение 25 мин склеено."""
    intervals = [
        _interval(10, 13, 0, 14, 0),
        _interval(10, 14, 25, 15, 0),
    ]
    merged = merge_intervals(intervals, threshold_min=30)
    assert [(i.start_local, i.end_local) for i in merged] == [(_dt(10, 13, 0), _dt(10, 15, 0))]


def test_tc_mrg_004_wake_exactly_threshold_not_merged() -> None:
    """TC-MRG-004: бодрствование РОВНО 20 мин при пороге 20 → два сна (строго меньше)."""
    intervals = [
        _interval(10, 13, 0, 14, 0),
        _interval(10, 14, 20, 15, 0),
    ]
    merged = merge_intervals(intervals, threshold_min=20)
    assert [(i.start_local, i.end_local) for i in merged] == [
        (_dt(10, 13, 0), _dt(10, 14, 0)),
        (_dt(10, 14, 20), _dt(10, 15, 0)),
    ]


def test_tc_mrg_005_gap_one_minute_below_threshold_merged() -> None:
    """TC-MRG-005: бодрствование 19 мин при пороге 20 → склеено (граница строгости)."""
    intervals = [
        _interval(10, 13, 0, 14, 0),
        _interval(10, 14, 19, 15, 0),
    ]
    merged = merge_intervals(intervals, threshold_min=20)
    assert [(i.start_local, i.end_local) for i in merged] == [(_dt(10, 13, 0), _dt(10, 15, 0))]


def test_tc_mrg_006_merge_from_raw_events_end_to_end() -> None:
    """TC-MRG-006: сквозной путь сырые события → интервалы → склейка (design D6→D9)."""
    events = [
        ("sleep", _dt(10, 13, 0)),
        ("wake", _dt(10, 14, 0)),
        ("sleep", _dt(10, 14, 15)),
        ("wake", _dt(10, 15, 0)),
    ]
    merged = merge_intervals(build_sleep_intervals(events, now_local=_dt(10, 16, 0)), 20)
    assert [(i.start_local, i.end_local) for i in merged] == [(_dt(10, 13, 0), _dt(10, 15, 0))]


def test_tc_mrg_007_single_and_empty_inputs() -> None:
    """TC-MRG-007: пустой список и одиночный интервал проходят без изменений."""
    assert merge_intervals([], threshold_min=20) == []
    single = [_interval(10, 13, 0, 14, 0)]
    assert merge_intervals(single, threshold_min=20) == single


def test_tc_mrg_008_unsorted_input_sorted_before_merge() -> None:
    """TC-MRG-008: интервалы не по порядку — сортировка перед склейкой."""
    intervals = [
        _interval(10, 14, 15, 15, 0),
        _interval(10, 13, 0, 14, 0),
    ]
    merged = merge_intervals(intervals, threshold_min=20)
    assert [(i.start_local, i.end_local) for i in merged] == [(_dt(10, 13, 0), _dt(10, 15, 0))]
