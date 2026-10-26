"""Сводка за день на лету (FR-3, FR-12, FR-19; design D6, D8).

Каждый тест помечен идентификатором TC-SUM-xxx в докстринге.
"""

import subprocess
import sys
from datetime import date, datetime, timedelta

from src.services.calculation import (
    build_sleep_intervals,
    daily_summary,
)


def _dt(day: int, h: int, m: int) -> datetime:
    """Naive-момент октября 2026 (день 10 = 10.10, день 11 = 11.10)."""
    return datetime(2026, 10, day, h, m)


def test_tc_sum_001_mixed_day_nap_and_overnight_sleep() -> None:
    """TC-SUM-001: смешанный день — дневной сон 13:30–15:05 и ночной
    22:10 → 07:20 (через полночь): суммы сходятся; функция чистая —
    БД не пишется (никаких импортов хранилища, вход не меняется).

    Ожидания: сон = 1:35 (дневной) + 9:10 (ночной целиком, D8) = 10:45;
    бодрствование = 24:00 окна − фактический сон внутри окна
    (1:35 + 7:50 до границы 06:00) = 14:35.
    """
    events = [
        ("sleep", _dt(10, 13, 30)),
        ("wake", _dt(10, 15, 5)),
        ("sleep", _dt(10, 22, 10)),
        ("wake", _dt(11, 7, 20)),
    ]
    summary = daily_summary(events, page_date=date(2026, 10, 10))

    assert summary.total_sleep == timedelta(hours=10, minutes=45)
    assert summary.total_wake == timedelta(hours=14, minutes=35)
    # Сон через полночь целиком в странице дня начала (design D8).
    assert [(i.start_local, i.end_local) for i in summary.sleep_intervals] == [
        (_dt(10, 13, 30), _dt(10, 15, 5)),
        (_dt(10, 22, 10), _dt(11, 7, 20)),
    ]

    # Чистая функция: расчёт не тянет ни БД, ни репозитории (FR-3) —
    # проверяем в свежем интерпретаторе, независимо от порядка тестов.
    probe = (
        "import sys; import src.services.calculation as c;"
        "from datetime import date, datetime;"
        "c.daily_summary([('sleep', datetime(2026,10,10,13,0))],"
        "page_date=date(2026,10,10), now_local=datetime(2026,10,10,14,0));"
        "assert 'src.repositories' not in sys.modules, 'repositories imported';"
        "assert 'src.db' not in sys.modules, 'db imported';"
        "assert 'sqlite3' not in sys.modules, 'sqlite3 imported'"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)

    # Входные сырые события не изменены.
    assert events == [
        ("sleep", _dt(10, 13, 30)),
        ("wake", _dt(10, 15, 5)),
        ("sleep", _dt(10, 22, 10)),
        ("wake", _dt(11, 7, 20)),
    ]


def test_tc_sum_002_early_morning_sleep_belongs_to_previous_page() -> None:
    """TC-SUM-002: сон с началом 02:00 → 05:30 попадает в сводку страницы
    ПРЕДЫДУЩЕГО календарного дня (design D8); в страницу дня 10.10 — нет.
    """
    events = [("sleep", _dt(10, 2, 0)), ("wake", _dt(10, 5, 30))]

    previous = daily_summary(events, page_date=date(2026, 10, 9))
    assert previous.total_sleep == timedelta(hours=3, minutes=30)
    # Окно страницы 09.10: 06:00 09.10 → 06:00 10.10, сон внутри 3:30.
    assert previous.total_wake == timedelta(hours=20, minutes=30)
    assert len(previous.sleep_intervals) == 1

    current = daily_summary(events, page_date=date(2026, 10, 10))
    assert current.total_sleep == timedelta(0)
    assert current.total_wake == timedelta(hours=24)
    assert current.sleep_intervals == ()


def test_tc_sum_003_empty_day_gives_zero_summary() -> None:
    """TC-SUM-003: день без событий → нулевые сон и бодрствование окна."""
    summary = daily_summary([], page_date=date(2026, 10, 10))
    assert summary.total_sleep == timedelta(0)
    assert summary.total_wake == timedelta(hours=24)
    assert summary.sleep_intervals == ()


def test_tc_sum_004_unclosed_sleep_counted_until_now_local() -> None:
    """TC-SUM-004: незакрытый сон учитывается до момента расчёта
    (now_local): sleep 13:00, расчёт в 14:30 → сон 1:30.
    """
    events = [("sleep", _dt(10, 13, 0))]
    summary = daily_summary(
        events, page_date=date(2026, 10, 10), now_local=_dt(10, 14, 30)
    )
    assert summary.total_sleep == timedelta(hours=1, minutes=30)
    assert summary.total_wake == timedelta(hours=22, minutes=30)


def test_tc_sum_005_unclosed_sleep_with_now_before_start_clamped() -> None:
    """TC-SUM-005 (CC-2, review-001-3.3): незакрытый sleep, now_local
    раньше момента засыпания — отрицательной длительности нет, интервал
    нулевой: сон не добавляется, бодрствование не искажается.
    """
    intervals = build_sleep_intervals(
        [("sleep", _dt(10, 13, 0))], now_local=_dt(10, 12, 0)
    )
    assert len(intervals) == 1
    assert intervals[0].duration == timedelta(0)

    summary = daily_summary(
        [("sleep", _dt(10, 13, 0))],
        page_date=date(2026, 10, 10),
        now_local=_dt(10, 12, 0),
    )
    assert summary.total_sleep == timedelta(0)
    assert summary.total_wake == timedelta(hours=24)


def test_tc_sum_006_merged_short_wake_not_counted_as_wake() -> None:
    """TC-SUM-006: пробуждение 15 мин при пороге 20 склеивается —
    бодрствование за период не учитывается (FR-14); порог 10 — не
    склеивается, сон 1:45, пробуждение 15 мин в бодрствовании.
    """
    events = [
        ("sleep", _dt(10, 13, 0)),
        ("wake", _dt(10, 14, 0)),
        ("sleep", _dt(10, 14, 15)),
        ("wake", _dt(10, 15, 0)),
    ]

    merged = daily_summary(
        events, page_date=date(2026, 10, 10), merge_threshold_min=20
    )
    assert merged.total_sleep == timedelta(hours=2)
    # Внутри окна: бодрствование 06:00–13:00 (7:00), 15-минутного
    # пробуждения нет (склейка), после 15:00 до 06:00 (15:00).
    assert merged.total_wake == timedelta(hours=22)
    assert len(merged.sleep_intervals) == 1

    split = daily_summary(
        events, page_date=date(2026, 10, 10), merge_threshold_min=10
    )
    assert split.total_sleep == timedelta(hours=1, minutes=45)
    assert split.total_wake == timedelta(hours=22, minutes=15)
    assert len(split.sleep_intervals) == 2
