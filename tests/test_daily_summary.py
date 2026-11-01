"""Сводка за день на лету (FR-3, FR-12, FR-19; design D6, D8) и
сводка событий дня в подтверждениях (PB-3, волна 6; FR-7/FR-8/FR-19
в ред. волны 6; решение продакта 29.10).

Каждый тест помечен идентификатором TC-SUM-xxx в докстринге.
"""

import subprocess
import sys
from datetime import date, datetime, timedelta

from src.services.calculation import (
    build_sleep_intervals,
    daily_summary,
    day_facts_lines,
)


def _dt(day: int, h: int, m: int) -> datetime:
    """Naive-момент октября 2026 (день 10 = 10.10, день 11 = 11.10)."""
    return datetime(2026, 10, day, h, m)


def test_tc_sum_001_mixed_day_nap_and_overnight_sleep() -> None:
    """TC-SUM-001: смешанный день — дневной сон 13:30–15:05 и ночной
    22:10 → 07:20 (через полночь): суммы сходятся; функция чистая —
    БД не пишется (никаких импортов хранилища, вход не меняется).

    Ожидания: сон = 1:35 (дневной) + 9:10 (ночной целиком, D8) = 10:45.
    """
    events = [
        ("sleep", _dt(10, 13, 30)),
        ("wake", _dt(10, 15, 5)),
        ("sleep", _dt(10, 22, 10)),
        ("wake", _dt(11, 7, 20)),
    ]
    summary = daily_summary(events, page_date=date(2026, 10, 10))

    assert summary.total_sleep == timedelta(hours=10, minutes=45)
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
    assert len(previous.sleep_intervals) == 1

    current = daily_summary(events, page_date=date(2026, 10, 10))
    assert current.total_sleep == timedelta(0)
    assert current.sleep_intervals == ()


def test_tc_sum_003_empty_day_gives_zero_summary() -> None:
    """TC-SUM-003: день без событий → нулевой сон."""
    summary = daily_summary([], page_date=date(2026, 10, 10))
    assert summary.total_sleep == timedelta(0)
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


def test_tc_sum_005_unclosed_sleep_with_now_before_start_clamped() -> None:
    """TC-SUM-005 (CC-2, review-001-3.3): незакрытый sleep, now_local
    раньше момента засыпания — отрицательной длительности нет,
    интервал нулевой: сон не добавляется.
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


def test_tc_sum_006_merged_short_wake_not_counted_as_wake() -> None:
    """TC-SUM-006: пробуждение 15 мин при пороге 20 склеивается
    (FR-14); порог 10 — не склеивается, сон 1:45, два отрезка.
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
    assert len(merged.sleep_intervals) == 1

    split = daily_summary(
        events, page_date=date(2026, 10, 10), merge_threshold_min=10
    )
    assert split.total_sleep == timedelta(hours=1, minutes=45)
    assert len(split.sleep_intervals) == 2


# ------------------------------------- Сводка событий дня (PB-3, волна 6)


def test_tc_sum_007_facts_morning_rise_nap_and_new_sleep() -> None:
    """TC-SUM-007 (PB-3, сценарий спеки diary-events): за день Подъем
    07:41 (ночной отрезок, начатый накануне), дневной сон 13:30–15:00 и
    новое засыпание 15:40 → строки «Подъем: 07:41», «Заснул: 13:30»,
    «Проснулся: 15:00», «Заснул: 15:40», «Дневной сон: 1 ч 30 мин»,
    «Бодрствование общее: 6 ч 29 мин» (ред. BUG-004: (15:40 − 07:41)
    − 1:30 дневного сна); абстрактной суммы «Сон: …» нет."""
    events = [
        ("sleep", _dt(9, 22, 10)),
        ("wake", _dt(10, 7, 41)),
        ("sleep", _dt(10, 13, 30)),
        ("wake", _dt(10, 15, 0)),
        ("sleep", _dt(10, 15, 40)),
    ]
    lines = day_facts_lines(events, page_date=date(2026, 10, 10), now_local=_dt(10, 15, 40))

    assert lines == [
        "Подъем: 07:41",
        "Заснул: 13:30",
        "Проснулся: 15:00",
        "Заснул: 15:40",
        "Дневной сон: 1 ч 30 мин",
        "Бодрствование общее: 6 ч 29 мин",  # (15:40 − 07:41) − 1:30 дня
    ]
    joined = "\n".join(lines)
    assert "Сон:" not in joined  # суммарный сон за день НЕ выводится
    assert "Бодрствование:" not in joined  # старого формата «Бодрствование: …» нет


def test_tc_sum_008_facts_evening_bedtime_unclosed_night() -> None:
    """TC-SUM-008 (PB-3, вариант «вечер»): Подъем 07:41, дневной сон
    13:30–15:00 и Отбой 22:10 (незакрытый ночной отрезок) → «Ночной сон»
    и «Проснулся» у отбоя НЕ выводятся (строка только при наличии факта)."""
    events = [
        ("sleep", _dt(9, 22, 10)),
        ("wake", _dt(10, 7, 41)),
        ("sleep", _dt(10, 13, 30)),
        ("wake", _dt(10, 15, 0)),
        ("sleep", _dt(10, 22, 10)),
    ]
    lines = day_facts_lines(events, page_date=date(2026, 10, 10), now_local=_dt(10, 22, 40))

    assert lines == [
        "Подъем: 07:41",
        "Заснул: 13:30",
        "Проснулся: 15:00",
        "Дневной сон: 1 ч 30 мин",
        "Отбой: 22:10",
        # Ред. BUG-004: незакрытый ночной сон (идёт ночь) замораживает
        # цифру на отбое: (22:10 − 07:41) − 1:30 дневного сна = 12:59.
        "Бодрствование общее: 12 ч 59 мин",
    ]


def test_tc_sum_009_facts_night_page_completed_night_sleep() -> None:
    """TC-SUM-009 (PB-3, вариант «ночь, следующие сутки»): страница 10.10
    содержит ночной отрезок 22:10 → 07:20 (следующего дня, завершён) →
    «Отбой: 22:10» + «Ночной сон: 9 ч 10 мин»; «Подъем» на этой странице
    нет (подъём 07:20 за пределами окна страницы 10.10)."""
    events = [
        ("sleep", _dt(10, 22, 10)),
        ("wake", _dt(11, 7, 20)),
    ]
    lines = day_facts_lines(events, page_date=date(2026, 10, 10), now_local=_dt(11, 8, 0))

    assert lines == [
        "Отбой: 22:10",
        "Ночной сон: 9 ч 10 мин",
        # Ред. BUG-004: подъёма на странице нет → база day_start; конец —
        # момент расчёта: 26:00 − 0 (ночное покрытие не вычитается).
        "Бодрствование общее: 26 ч",
    ]


def test_tc_sum_010_facts_empty_day_total_wake_only() -> None:
    """TC-SUM-010 (PB-3): день без событий — строка факта только одна,
    «Бодрствование общее: 24 ч» (строки фактов без события не выводятся)."""
    lines = day_facts_lines([], page_date=date(2026, 10, 10))
    assert lines == ["Бодрствование общее: 24 ч"]


def test_tc_sum_011_facts_merges_short_wake_and_uses_threshold() -> None:
    """TC-SUM-011 (PB-3, D9): два дневных отрезка с 15-минутным
    пробуждением: при пороге 20 склеиваются → один интервал
    «Заснул: 13:00 / Проснулся: 15:00 / Дневной сон: 2 ч»; при пороге 10 —
    два отдельных отрезка и «Дневной сон: 1 ч 45 мин». Бодрствование
    общее учитывает склейку (склеенное пробуждение не бодрствование)."""
    events = [
        ("sleep", _dt(10, 13, 0)),
        ("wake", _dt(10, 14, 0)),
        ("sleep", _dt(10, 14, 15)),
        ("wake", _dt(10, 15, 0)),
    ]

    merged = day_facts_lines(
        events, page_date=date(2026, 10, 10), merge_threshold_min=20
    )
    assert merged == [
        "Заснул: 13:00",
        "Проснулся: 15:00",
        "Дневной сон: 2 ч",
        "Бодрствование общее: 22 ч",
    ]

    split = day_facts_lines(
        events, page_date=date(2026, 10, 10), merge_threshold_min=10
    )
    assert split == [
        "Заснул: 13:00",
        "Проснулся: 14:00",
        "Заснул: 14:15",
        "Проснулся: 15:00",
        "Дневной сон: 1 ч 45 мин",
        "Бодрствование общее: 22 ч 15 мин",
    ]


def test_tc_sum_012_facts_short_evening_sleep_no_rise() -> None:
    """TC-SUM-012 (minor-1 review-001-6.1): короткий вечерний сон
    19:00–19:30 — это «Отбой: 19:00» + «Ночной сон: 30 мин», но НЕ
    «Подъем: 19:30»: подъём — только конец ночного отрезка в утреннем
    окне (до 12:00); «Отбой» и «Подъем» в одной сводке не противоречат
    друг другу."""
    events = [("sleep", _dt(10, 19, 0)), ("wake", _dt(10, 19, 30))]
    lines = day_facts_lines(events, page_date=date(2026, 10, 10))

    assert lines == [
        "Отбой: 19:00",
        "Ночной сон: 30 мин",
        # Ред. BUG-004: ночное покрытие не вычитается (базы от подъёма
        # нет — 19:30 не в утреннем окне); 06:00 → 06:00 = 24 ч.
        "Бодрствование общее: 24 ч",
    ]
    joined = "\n".join(lines)
    assert "Подъем:" not in joined  # 19:30 — не подъём утра страницы


def test_tc_sum_013_facts_full_night_bedtime_and_rise() -> None:
    """TC-SUM-013 (PB-3, minor-1 review-001-6.1): полноценная ночь даёт
    ОБЕ строки в одной сводке страницы утра: ночной отрезок 21:00→07:10
    (начат накануне, окончен сегодня до 12:00) — «Подъем: 07:10»,
    вечернее засыпание 21:00 сегодня (незакрытое) — «Отбой: 21:00».
    Атрибуция по окну страницы — design D8 (как TC-SUM-007/009)."""
    events = [
        ("sleep", _dt(9, 21, 0)),
        ("wake", _dt(10, 7, 10)),
        ("sleep", _dt(10, 21, 0)),
    ]
    lines = day_facts_lines(events, page_date=date(2026, 10, 10), now_local=_dt(10, 21, 30))

    assert lines == [
        "Подъем: 07:10",
        "Отбой: 21:00",
        # Ред. BUG-004: незакрытый ночной сон (идёт ночь) замораживает
        # цифру на отбое: 21:00 − 07:10 = 13:50 (ночь не вычитается).
        "Бодрствование общее: 13 ч 50 мин",
    ]


def test_tc_sum_014_facts_night_awakening_single_bedtime() -> None:
    """TC-SUM-014 (minor-1 review-001-6.1): ночное пробуждение — отбой
    21:00, ночной отрезок до 01:20, сон 01:40–07:00 (гэп ровно 20 мин —
    без склейки при пороге 20). Страница отбоя 10.10: «Отбой: 21:00» —
    ПЕРВЫЙ ночной старт ≥ 19:00, а не последний отрезок ночи (01:40);
    «Подъем» НЕТ: конец 01:20 — промежуточное ночное пробуждение (ночь
    продолжается отрезком с началом 01:40 < 12:00), а конец 07:00 — за
    концом окна страницы (06:00 следующих суток). Страница утра 11.10:
    «Подъем: 07:00» — конец завершённого ночного отрезка до 12:00
    (design D8: атрибуция Подъема по концу в окне)."""
    events = [
        ("sleep", _dt(10, 21, 0)),
        ("wake", _dt(11, 1, 20)),
        ("sleep", _dt(11, 1, 40)),
        ("wake", _dt(11, 7, 0)),
    ]
    lines = day_facts_lines(
        events, page_date=date(2026, 10, 10), merge_threshold_min=20
    )
    assert lines == [
        "Отбой: 21:00",
        "Ночной сон: 9 ч 40 мин",  # 4:20 (21:00–01:20) + 5:20 (01:40–07:00)
        # Ред. BUG-004: подъёма нет → база day_start, ночное покрытие
        # не вычитается: 06:00 → 06:00 = 24 ч.
        "Бодрствование общее: 24 ч",
    ]
    joined = "\n".join(lines)
    assert joined.count("Отбой:") == 1
    assert "Подъем:" not in joined

    # Страница утра (11.10): та же ночь даёт «Подъем: 07:00», без «Отбой»
    # (её страница — 10.10) и без лишних ночных строк.
    morning = day_facts_lines(
        events, page_date=date(2026, 10, 11), merge_threshold_min=20
    )
    assert morning == [
        "Подъем: 07:00",
        # Ред. BUG-004: (конец окна 06:00 − подъем 07:00) = 23 ч.
        "Бодрствование общее: 23 ч",
    ]


def test_tc_sum_015_facts_rise_is_last_morning_end() -> None:
    """TC-SUM-015 (minor-1 review-001-6.1): ночь с ранним концом отрезка
    06:30 (ночь начата накануне в 21:00) и финальным сном 06:50–07:00 →
    «Подъем: 07:00» — ПОСЛЕДНИЙ конец ночного отрезка до 12:00, а не
    первый (06:30 — промежуточное ночное пробуждение, ребенок «в ночи»);
    «Отбой: 21:00» — сегодняшний вечер (гэп 06:30→06:50 ровно 20 мин —
    без склейки, порог 20)."""
    events = [
        ("sleep", _dt(9, 21, 0)),
        ("wake", _dt(10, 6, 30)),
        ("sleep", _dt(10, 6, 50)),
        ("wake", _dt(10, 7, 0)),
        ("sleep", _dt(10, 21, 0)),
    ]
    lines = day_facts_lines(events, page_date=date(2026, 10, 10), now_local=_dt(10, 21, 30))

    assert lines == [
        "Подъем: 07:00",
        "Отбой: 21:00",
        "Ночной сон: 10 мин",  # закрытый ночной отрезок 06:50–07:00
        # Ред. BUG-004: незакрытый ночной сон замораживает цифру на
        # отбое: 21:00 − 07:00 = 14 ч.
        "Бодрствование общее: 14 ч",
    ]
    joined = "\n".join(lines)
    assert joined.count("Подъем:") == 1
    assert "06:30" not in joined


def test_tc_sum_016_facts_morning_nap_no_bedtime() -> None:
    """TC-SUM-016 (minor-1 review-001-6.1, контр-кейс 06:30–07:00):
    утренний сон 06:30–07:00 (ночной по FR-9: конец ≤ 07:00) даёт
    «Подъем: 07:00», но НЕ «Отбой: 06:30» — «Отбой» только у отрезка,
    начавшегося в вечернем окне (≥ 19:00); противоречивая пара
    «Отбой»+«Подъем» из одного короткого утреннего отрезка исчезла."""
    events = [("sleep", _dt(10, 6, 30)), ("wake", _dt(10, 7, 0))]
    lines = day_facts_lines(events, page_date=date(2026, 10, 10))

    assert lines == [
        "Подъем: 07:00",
        "Ночной сон: 30 мин",
        # Ред. BUG-004: (конец окна 06:00 − подъем 07:00) = 23 ч; ночное
        # покрытие 06:30–07:00 не вычитается.
        "Бодрствование общее: 23 ч",
    ]
    joined = "\n".join(lines)
    assert "Отбой:" not in joined


def test_tc_sum_017_facts_day_lines_unchanged() -> None:
    """TC-SUM-017 (регресс minor-1 review-001-6.1): прежняя семантика
    дневных строк не сломана — дневной отрезок 13:30–15:00 даёт
    «Заснул: 13:30» / «Проснулся: 15:00» / «Дневной сон: 1 ч 30 мин»,
    ночные строки («Отбой»/«Ночной сон»/«Подъем») не выводятся."""
    events = [("sleep", _dt(10, 13, 30)), ("wake", _dt(10, 15, 0))]
    lines = day_facts_lines(events, page_date=date(2026, 10, 10))

    assert lines == [
        "Заснул: 13:30",
        "Проснулся: 15:00",
        "Дневной сон: 1 ч 30 мин",
        "Бодрствование общее: 22 ч 30 мин",
    ]
    joined = "\n".join(lines)
    assert "Отбой:" not in joined
    assert "Ночной сон:" not in joined
    assert "Подъем:" not in joined


def test_tc_sum_018_facts_next_day_morning_nap_no_rise_override() -> None:
    """TC-SUM-018 (major-1 review-002-6.1, контр-кейс C5): подъем 07:00
    10.10 (ночь 21:00 накануне) и ОТДЕЛЬНЫЙ утренний сон 05:30–05:45 на
    СЛЕДУЮЩИЙ день (11.10) — страница 10.10 показывает «Подъем: 07:00»:
    ранний утренний сон чужой страницы не подавляет и не подменяет
    «Подъем» (сравнение по полному datetime с привязкой к ночи конца
    кандидата, а не по time-of-day; приоритет конца в дату страницы)."""
    events = [
        ("sleep", _dt(9, 21, 0)),
        ("wake", _dt(10, 7, 0)),
        ("sleep", _dt(11, 5, 30)),
        ("wake", _dt(11, 5, 45)),
    ]
    lines = day_facts_lines(events, page_date=date(2026, 10, 10))

    assert lines == [
        "Подъем: 07:00",
        "Ночной сон: 15 мин",  # утренний сон 11.10 попал в окно по D8
        # Ред. BUG-004: (конец окна 06:00 11.10 − подъем 07:00 10.10) =
        # 23 ч; утренний сон 11.10 — ночной, не вычитается.
        "Бодрствование общее: 23 ч",
    ]
    joined = "\n".join(lines)
    assert "Подъем: 07:00" in joined
    assert "05:45" not in joined  # конец утреннего сна 11.10 НЕ «Подъем»


def test_tc_sum_019_facts_far_future_nap_no_rise_override() -> None:
    """TC-SUM-019 (major-1 review-002-6.1, контр-кейс C5b): тот же подъем
    07:00 10.10, но утренний сон 05:30–05:45 через 40 дней (20.11) —
    «Подъем: 07:00» на месте: ночной отрезок чужой даты НЕ подавляет
    «Подъем» вовсе (time-of-day сравнение до фикса исчезало)."""
    events = [
        ("sleep", _dt(9, 21, 0)),
        ("wake", _dt(10, 7, 0)),
        ("sleep", datetime(2026, 11, 20, 5, 30)),
        ("wake", datetime(2026, 11, 20, 5, 45)),
    ]
    lines = day_facts_lines(events, page_date=date(2026, 10, 10))

    assert lines == [
        "Подъем: 07:00",
        "Бодрствование общее: 23 ч",
    ]


def test_tc_sum_020_facts_night_awakening_suppressed_within_same_night() -> None:
    """TC-SUM-020 (регресс minor-1, major-1 review-002-6.1): отбой 21:00,
    ночное пробуждение 01:00–01:20, сон до 07:00 — семантика minor-1 не
    вернулась: конец 01:00 подавлен отрезком той же ночи (начало 01:20 в
    пределах ночного окна конца 01:00), страница отбоя 10.10 — один
    «Отбой: 21:00», «Подъем» НЕТ; страница утра 11.10 — ровно один
    «Подъем: 07:00», «Отбой» нет (attributions по design D8)."""
    events = [
        ("sleep", _dt(10, 21, 0)),
        ("wake", _dt(11, 1, 0)),
        ("sleep", _dt(11, 1, 20)),
        ("wake", _dt(11, 7, 0)),
    ]
    lines = day_facts_lines(
        events, page_date=date(2026, 10, 10), merge_threshold_min=20
    )
    assert lines == [
        "Отбой: 21:00",
        "Ночной сон: 9 ч 40 мин",  # 4:00 (21:00–01:00) + 5:40 (01:20–07:00)
        # Ред. BUG-004: подъёма нет → база day_start, ночное покрытие
        # не вычитается: 06:00 → 06:00 = 24 ч.
        "Бодрствование общее: 24 ч",
    ]
    joined = "\n".join(lines)
    assert joined.count("Отбой:") == 1
    assert "Подъем:" not in joined

    # Страница утра (11.10): та же ночь даёт ровно один «Подъем: 07:00».
    morning = day_facts_lines(
        events, page_date=date(2026, 10, 11), merge_threshold_min=20
    )
    assert morning == [
        "Подъем: 07:00",
        # Ред. BUG-004: (конец окна 06:00 − подъем 07:00) = 23 ч.
        "Бодрствование общее: 23 ч",
    ]
    assert "\n".join(morning).count("Подъем:") == 1
    assert "Отбой:" not in "\n".join(morning)


def test_tc_sum_021_facts_early_morning_rise_kept_on_bedtime_page() -> None:
    """TC-SUM-021 (регресс major-1 review-002-6.1): ночной сон 21:00→04:50
    (подъем до начала страницы утра) — «Подъем: 04:50» остается на
    странице отбоя 10.10 (конец в окне страницы по D8): подавление по
    ночному окну не убрало легитимный ранний подъем этой же ночи."""
    events = [
        ("sleep", _dt(10, 21, 0)),
        ("wake", _dt(11, 4, 50)),
    ]
    lines = day_facts_lines(events, page_date=date(2026, 10, 10))

    assert lines == [
        "Подъем: 04:50",
        "Отбой: 21:00",
        "Ночной сон: 7 ч 50 мин",
        # Ред. BUG-004: база — подъем 04:50 11.10, конец окна 06:00
        # 11.10 → 1 ч 10 мин (ночное покрытие не вычитается).
        "Бодрствование общее: 1 ч 10 мин",
    ]


# ------------------ «Бодрствование общее» от подъёма (BUG-004, ред. 31.10)


def test_tc_sum_022_facts_total_wake_acceptance_scenario() -> None:
    """TC-SUM-022 (BUG-004, сценарий приемки продакта 31.10): подъем
    08:00 (ночь 22:39 накануне), дневной сон 13:29–15:27, момент
    расчёта 20:33 → «Бодрствование общее: 10 ч 35 мин» =
    (20:33 − 08:00) − 1:58. Прежнее значение 20 ч 2 мин (окно страницы
    24 ч минус покрытие сном) — дефект BUG-004, не воспроизводится."""
    events = [
        ("sleep", _dt(30, 22, 39)),
        ("wake", _dt(31, 8, 0)),
        ("sleep", _dt(31, 13, 29)),
        ("wake", _dt(31, 15, 27)),
    ]
    lines = day_facts_lines(events, page_date=date(2026, 10, 31), now_local=_dt(31, 20, 33))

    assert lines == [
        "Подъем: 08:00",
        "Заснул: 13:29",
        "Проснулся: 15:27",
        "Дневной сон: 1 ч 58 мин",
        "Бодрствование общее: 10 ч 35 мин",  # (20:33 − 08:00) − 1:58
    ]


def test_tc_sum_023_facts_total_wake_no_day_sleep() -> None:
    """TC-SUM-023 (BUG-004): день без дневного сна — «Бодрствование
    общее» = момент расчёта − Подъем без вычетов: подъем 08:00,
    now 20:33 → 12 ч 33 мин; ночное покрытие 06:00–08:00 не вычитается."""
    events = [
        ("sleep", _dt(30, 22, 39)),
        ("wake", _dt(31, 8, 0)),
    ]
    lines = day_facts_lines(events, page_date=date(2026, 10, 31), now_local=_dt(31, 20, 33))

    assert lines == [
        "Подъем: 08:00",
        "Бодрствование общее: 12 ч 33 мин",  # 20:33 − 08:00
    ]


def test_tc_sum_024_facts_total_wake_no_rise_base_is_day_start() -> None:
    """TC-SUM-024 (BUG-004): подъёма на странице нет → база day_start
    страницы (06:00): дневной сон 13:29–15:27, now 20:33 →
    (20:33 − 06:00) − 1:58 = 12 ч 35 мин."""
    events = [
        ("sleep", _dt(31, 13, 29)),
        ("wake", _dt(31, 15, 27)),
    ]
    lines = day_facts_lines(events, page_date=date(2026, 10, 31), now_local=_dt(31, 20, 33))

    assert lines == [
        "Заснул: 13:29",
        "Проснулся: 15:27",
        "Дневной сон: 1 ч 58 мин",
        "Бодрствование общее: 12 ч 35 мин",  # (20:33 − 06:00) − 1:58
    ]


def test_tc_sum_025_facts_total_wake_unclosed_day_sleep_not_wake() -> None:
    """TC-SUM-025 (BUG-004): идёт незакрытый ДНЕВНОЙ сон (заснул 15:00,
    now 16:20) — он сон, а не бодрствование: (16:20 − 08:00) − 1:20
    (незакрытый отрезок 15:00→16:20) = 7 ч; вычетов сверх дневного
    покрытия нет."""
    events = [
        ("sleep", _dt(30, 22, 39)),
        ("wake", _dt(31, 8, 0)),
        ("sleep", _dt(31, 15, 0)),
    ]
    lines = day_facts_lines(events, page_date=date(2026, 10, 31), now_local=_dt(31, 16, 20))

    assert lines == [
        "Подъем: 08:00",
        "Заснул: 15:00",
        "Бодрствование общее: 7 ч",  # 16:20 − 08:00 − 1:20 идущего сна
    ]


def test_tc_sum_026_facts_total_wake_frozen_after_bedtime() -> None:
    """TC-SUM-026 (BUG-004): после отбоя (незакрытый НОЧНОЙ сон с 21:00,
    подъем был 08:00) цифра заморожена на отбое: 21:00 − 08:00 = 13 ч —
    независимо от момента расчёта (now 23:57 и позже не добавляют
    бодрствование: после отбоя ребенок не бодрствует)."""
    events = [
        ("sleep", _dt(30, 22, 39)),
        ("wake", _dt(31, 8, 0)),
        ("sleep", _dt(31, 21, 0)),
    ]
    lines = day_facts_lines(events, page_date=date(2026, 10, 31), now_local=_dt(31, 23, 57))

    assert lines == [
        "Подъем: 08:00",
        "Отбой: 21:00",
        "Бодрствование общее: 13 ч",  # заморожено на 21:00 − 08:00
    ]

    # Проверка «независимо от now»: момент расчёта на следующий день —
    # цифра та же (конец = начало ночного сна, а не now).
    later = day_facts_lines(
        events, page_date=date(2026, 10, 31), now_local=datetime(2026, 11, 1, 2, 0)
    )
    assert [l for l in later if l.startswith("Бодрствование общее:")] == [
        "Бодрствование общее: 13 ч"
    ]
