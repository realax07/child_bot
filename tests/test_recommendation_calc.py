"""Окно дневного сна — чистые функции расчёта (add-recommendations 1.1).

nap_window / nap_end_forecast / nap_end_after_deadline: окно от факта
«Подъем» страницы (PB-3) + 5:20…6:00 с подстройкой по отклонениям
эталона §3.6 (FR-26, FR-27, FR-28) и прогноз конца сна против крайнего
пробуждения 15:30 (FR-35). Якоря читаются из переданных настроек
(FR-24, design D14), БД не касается (design D16).

Каждый тест помечен идентификатором TC-REC-xxx в докстринге; времена
фиксированные, «живых» ЧЧ:ММ нет.
"""

import subprocess
import sys
from datetime import date, datetime, time, timedelta

from src.services.calculation import (
    day_facts_lines,
    nap_end_after_deadline,
    nap_end_forecast,
    nap_window,
    night_window,
    total_wake_at,
)
from src.services.settings import DEFAULT_KEYS


def _dt(day: int, h: int, m: int) -> datetime:
    """Naive-момент ноября 2026 (день 2 = 02.11, день 3 = 03.11)."""
    return datetime(2026, 11, day, h, m)


def _night_rise_at(rise_h: int, rise_m: int) -> list[tuple[str, datetime]]:
    """Ночь 22:00 → подъём утром 03.11 — база сценариев §3.5/§3.6."""
    return [
        ("sleep", _dt(2, 22, 0)),
        ("wake", _dt(3, rise_h, rise_m)),
    ]


def test_tc_rec_001_window_from_actual_rise_not_from_request_moment() -> None:
    """TC-REC-001 (FR-26): окно дневного сна от фактического подъёма.

    Подъём 08:00 (ночь 22:00 → 08:00), дневного сна ещё не было:
    окно 13:20–14:00 (08:00 + 5:20 … + 6:00). Сценарий «Окно после
    дневного события»: расчёт в 10:00 и в 12:00 даёт ОДНО И ТО ЖЕ
    окно — расчёт от подъёма, не от момента запроса.
    """
    events = _night_rise_at(8, 0)

    for now in (_dt(3, 10, 0), _dt(3, 12, 0)):
        window = nap_window(events, now)
        assert window is not None
        assert window.rise == _dt(3, 8, 0)
        assert window.start == _dt(3, 13, 20)
        assert window.end == _dt(3, 14, 0)
        assert window.stretch_to_limit is False
        assert window.after_deadline is False


def test_tc_rec_002_early_rise_window_stretch_advice() -> None:
    """TC-REC-002 (FR-27, §3.6 A6): ранний подъём 07:00 → окно
    12:20–13:00 с советом «тянем бодрствование до 13:00»
    (stretch_to_limit=True); 15:30 при таком окне не под угрозой
    (after_deadline=False), якоря §7.3 — волна 2.3.
    """
    window = nap_window(_night_rise_at(7, 0), _dt(3, 10, 0))

    assert window is not None
    assert window.rise == _dt(3, 7, 0)
    assert window.start == _dt(3, 12, 20)
    assert window.end == _dt(3, 13, 0)
    assert window.stretch_to_limit is True
    assert window.after_deadline is False


def test_tc_rec_003_late_rise_window_deadline_boundary() -> None:
    """TC-REC-003 (FR-28, §3.6 A7/A2): поздний подъём 09:00 → окно
    14:20–15:00; прогноз конца при начале окна 14:20 = 15:50 —
    ПОЗЖЕ крайнего 15:30 (after_deadline=True, граница будить
    ≤ 15:30; текст §7.4 — волна 2.3).
    """
    window = nap_window(_night_rise_at(9, 0), _dt(3, 12, 0))

    assert window is not None
    assert window.rise == _dt(3, 9, 0)
    assert window.start == _dt(3, 14, 20)
    assert window.end == _dt(3, 15, 0)
    assert window.wake_deadline == _dt(3, 15, 30)
    assert window.forecast_end == _dt(3, 15, 50)
    assert window.after_deadline is True
    assert window.stretch_to_limit is False


def test_tc_rec_004_forecast_vs_deadline_at_actual_sleep_start() -> None:
    """TC-REC-004 (FR-35, §7.8): прогноз конца при ФАКТИЧЕСКОМ начале
    сна внутри окна. Окно 13:20–14:00 (подъём 08:00), цель 90 мин:
    начало 14:20 → конец около 15:50 — позже 15:30 (True);
    начало 14:00 → ровно 15:30 — ещё не «позже» (False, строгая
    граница); начало 13:20 → 14:50 (False).
    """
    assert nap_end_forecast(_dt(3, 14, 20)) == _dt(3, 15, 50)
    assert nap_end_after_deadline(_dt(3, 14, 20)) is True
    assert nap_end_after_deadline(_dt(3, 14, 0)) is False
    assert nap_end_after_deadline(_dt(3, 13, 20)) is False


def test_tc_rec_005_rise_fact_matches_pb3_line() -> None:
    """TC-REC-005 (design D16, PB-3): «Подъем» для окон — тот же факт
    «Подъем» страницы, что в day_facts_lines: ночь с промежуточным
    пробуждением (22:10 → 02:00 → 02:20 → 06:50) даёт подъём 06:50 в
    ОБЕИХ функциях (единое правило, без дублей), окно 12:10–12:50.
    """
    events = [
        ("sleep", _dt(2, 22, 10)),
        ("wake", _dt(3, 2, 0)),
        ("sleep", _dt(3, 2, 20)),
        ("wake", _dt(3, 6, 50)),
    ]

    window = nap_window(events, _dt(3, 11, 0))
    assert window is not None
    assert window.rise == _dt(3, 6, 50)
    assert window.start == _dt(3, 12, 10)
    assert window.end == _dt(3, 12, 50)

    facts = day_facts_lines(events, page_date=date(2026, 11, 3), now_local=_dt(3, 11, 0))
    assert f"Подъем: 06:50" in facts


def test_tc_rec_006_no_rise_fact_returns_none_and_purity() -> None:
    """TC-REC-006 (FR-34, design D16): нет факта «Подъем» страницы —
    окна нет (None), неполные данные решает волна 2: пустые события;
    ночь ещё идёт (незакрытый sleep — его конец не факт). Функция
    чистая: в свежем интерпретаторе не тянет sqlite3/репозитории,
    вход не меняет.
    """
    assert nap_window([], _dt(3, 12, 0)) is None
    # Ночь в разгаре: sleep 22:00 без wake, расчёт в 03:00 ночи.
    assert nap_window([("sleep", _dt(2, 22, 0))], _dt(3, 3, 0)) is None

    events = _night_rise_at(8, 0)
    window = nap_window(events, _dt(3, 12, 0))
    assert window is not None  # вход не изменился функцией:
    assert events == [("sleep", _dt(2, 22, 0)), ("wake", _dt(3, 8, 0))]

    probe = (
        "import sys; import src.services.calculation as c;"
        "from datetime import datetime;"
        "c.nap_window([('sleep', datetime(2026,11,2,22,0)),"
        "('wake', datetime(2026,11,3,8,0))], datetime(2026,11,3,12,0));"
        "assert 'src.repositories' not in sys.modules, 'repositories imported';"
        "assert 'src.db' not in sys.modules, 'db imported';"
        "assert 'sqlite3' not in sys.modules, 'sqlite3 imported'"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def test_tc_rec_007_anchors_read_from_settings_dict() -> None:
    """TC-REC-007 (FR-24, design D14): якоря читаются из переданных
    настроек при каждом расчёте — смена target_nap_duration (90 → 60)
    или wake_deadline («15:30» → «16:00») меняет прогноз/флаг без
    изменения событий; поздний подъём 09:00 перестаёт быть «после
    15:30». БД не трогается (settings — dict на входе).
    """
    events = _night_rise_at(9, 0)
    now = _dt(3, 12, 0)

    short_nap = nap_window(events, now, {"target_nap_duration": 60})
    assert short_nap is not None
    assert short_nap.forecast_end == _dt(3, 15, 20)
    assert short_nap.after_deadline is False

    later_deadline = nap_window(events, now, {"wake_deadline": "16:00"})
    assert later_deadline is not None
    assert later_deadline.wake_deadline == _dt(3, 16, 0)
    assert later_deadline.after_deadline is False

    shifted = nap_window(events, now, {"sleep_min_before_nap": 300})
    assert shifted is not None
    assert shifted.start == _dt(3, 14, 0)


def test_tc_rec_008_defaults_mirror_settings_default_keys() -> None:
    """TC-REC-008 (design D14/D16): аргументные дефолты функций окна —
    зеркало DEFAULT_KEYS пакета 1 (единый источник значений; прямой
    импорт DEFAULT_KEYS в calculation.py запрещён чистотой модуля —
    соответствие фиксирует ЭТОТ тест).
    """
    nap_params = nap_window.__kwdefaults__
    assert nap_params is not None, "keyword-only дефолты nap_window ожидаемы"
    assert nap_params["sleep_min_before_nap_min"] == DEFAULT_KEYS["sleep_min_before_nap"]
    assert nap_params["sleep_max_before_nap_min"] == DEFAULT_KEYS["sleep_max_before_nap"]
    assert nap_params["target_nap_duration_min"] == DEFAULT_KEYS["target_nap_duration"]
    assert nap_params["wake_deadline"] == time(15, 30)
    assert str(DEFAULT_KEYS["wake_deadline"]) == "15:30"
    assert nap_params["merge_threshold_min"] == DEFAULT_KEYS["merge_threshold"]
    assert nap_params["day_start"] == time(6, 0)
    assert str(DEFAULT_KEYS["day_start"]) == "06:00"
    assert str(DEFAULT_KEYS["night_bounds"]).split("-")[0] == "19:00"

    forecast_params = nap_end_forecast.__kwdefaults__
    assert forecast_params is not None, "keyword-only дефолты nap_end_forecast ожидаемы"
    assert (
        forecast_params["target_nap_duration_min"]
        == DEFAULT_KEYS["target_nap_duration"]
    )


def test_tc_rec_009_total_wake_matches_pb3_fact_unified_arithmetic() -> None:
    """TC-REC-009 (FR-29, BUG-004): суммарное бодрствование на момент —
    ЕДИНАЯ арифметика с фактом PB-3. Сценарий «Единая арифметика с
    PB-3»: Подъем 08:00, дневной сон 13:29–15:27, момент 20:33 →
    10 ч 35 мин в total_wake_at И в day_facts_lines — один расчёт
    (формула BUG-004: (20:33 − 08:00) − 1:58), не дубль.
    """
    events = _night_rise_at(8, 0) + [
        ("sleep", _dt(3, 13, 29)),
        ("wake", _dt(3, 15, 27)),
    ]

    from_hint = total_wake_at(events, _dt(3, 20, 33))
    assert from_hint == timedelta(hours=10, minutes=35)

    facts = day_facts_lines(
        events, page_date=date(2026, 11, 3), now_local=_dt(3, 20, 33)
    )
    assert "Подъем: 08:00" in facts
    assert "Бодрствование общее: 10 ч 35 мин" in facts


def test_tc_rec_010_night_window_reaches_lower_bound_at_2215() -> None:
    """TC-REC-010 (FR-30, §4.5 з2): окно ухода при достижении 12 ч
    в 22:15. Подъем 08:00, дневной сон 13:00–15:15 (2:15), момент
    20:15 → бодрствование 10 ч, нижняя граница 12,0 ч достигается в
    22:15 — внутри якорей: окно ухода 22:00–22:30 (целевое 22:00),
    без флага «позже 22:30».
    """
    events = _night_rise_at(8, 0) + [
        ("sleep", _dt(3, 13, 0)),
        ("wake", _dt(3, 15, 15)),
    ]

    window = night_window(events, _dt(3, 20, 15))
    assert window.wake_total == timedelta(hours=10)
    assert window.reach_lower == _dt(3, 22, 15)
    assert window.reach_upper == _dt(3, 22, 45)
    assert window.target == _dt(3, 22, 0)
    assert window.deadline == _dt(3, 22, 30)
    assert window.later_than_deadline is False


def test_tc_rec_011_night_window_later_than_deadline_flags_warning() -> None:
    """TC-REC-011 (FR-30): расчётное достижение нижней границы ПОЗЖЕ
    крайнего ухода — флаг предупреждения «ночной сон может сдвинуться»
    (текст §7.24 — волна 2). Длинный дневной сон 13:00–16:00 (3:00),
    момент 22:00 → бодрствование 11 ч, граница 12,0 ч достигается в
    23:00 — позже 22:30.
    """
    events = _night_rise_at(8, 0) + [
        ("sleep", _dt(3, 13, 0)),
        ("wake", _dt(3, 16, 0)),
    ]

    window = night_window(events, _dt(3, 22, 0))
    assert window.wake_total == timedelta(hours=11)
    assert window.reach_lower == _dt(3, 23, 0)
    assert window.deadline == _dt(3, 22, 30)
    assert window.later_than_deadline is True


def test_tc_rec_012_night_window_below_range_far_from_window() -> None:
    """TC-REC-012 (FR-30): суммарное бодрствование НИЖЕ диапазона —
    до окна далеко, флага нет. Подъем 08:00 без дневного сна, момент
    12:00 → 4 ч бодрствования; 12,0 ч достигается в 20:00 (до 22:30),
    12,5 ч — в 20:30.
    """
    window = night_window(_night_rise_at(8, 0), _dt(3, 12, 0))

    assert window.wake_total == timedelta(hours=4)
    assert window.reach_lower == _dt(3, 20, 0)
    assert window.reach_upper == _dt(3, 20, 30)
    assert window.later_than_deadline is False


def test_tc_rec_013_night_window_above_range_window_is_now() -> None:
    """TC-REC-013 (FR-30/FR-36): суммарное бодрствование ВЫШЕ верхней
    границы — окно уже прошло/сейчас. Подъем 08:00, дневной сон
    13:00–15:00 (2:00), момент 23:00 → 13 ч (> 12,5): моменты
    достижения = момент расчёта (проекция не уходит в прошлое),
    уход просрочен — флаг «позже 22:30» (перегул §7.9 — волна 2).
    """
    events = _night_rise_at(8, 0) + [
        ("sleep", _dt(3, 13, 0)),
        ("wake", _dt(3, 15, 0)),
    ]

    window = night_window(events, _dt(3, 23, 0))
    assert window.wake_total == timedelta(hours=13)
    assert window.reach_lower == _dt(3, 23, 0)
    assert window.reach_upper == _dt(3, 23, 0)
    assert window.later_than_deadline is True


def test_tc_rec_014_unclosed_night_sleep_freezes_wake_end() -> None:
    """TC-REC-014 (FR-29, BUG-004): незакрытый ночной сон замораживает
    конец (после отбоя ребёнок не бодрствует) — одинаково в
    total_wake_at и day_facts_lines. Ночь 22:00 → 08:00, новый сон
    с 22:00 не закрыт, момент 03:00 следующей ночи → бодрствование
    заморожено на (22:00 − 08:00) = 14 ч, не 19 ч. Идущий дневной сон
    вычитается до момента: сон с 13:00 при моменте 15:00 →
    (7:00 − 2:00) = 5 ч.
    """
    night_now = _night_rise_at(8, 0) + [("sleep", _dt(3, 22, 0))]
    assert total_wake_at(night_now, _dt(4, 3, 0)) == timedelta(hours=14)
    facts = day_facts_lines(
        night_now, page_date=date(2026, 11, 3), now_local=_dt(4, 3, 0)
    )
    assert "Бодрствование общее: 14 ч" in facts

    day_sleep_open = _night_rise_at(8, 0) + [("sleep", _dt(3, 13, 0))]
    assert total_wake_at(day_sleep_open, _dt(3, 15, 0)) == timedelta(hours=5)


def test_tc_rec_015_night_window_anchors_from_settings_dict() -> None:
    """TC-REC-015 (FR-24, design D14): границы диапазона и якоря ночи
    читаются из переданных настроек при каждом расчёте — смена
    wake_total_min (12,0 → 11,5) сдвигает момент достижения,
    night_deadline («22:30» → «23:00») снимает флаг TC-REC-011,
    night_target («22:00» → «21:30») меняет целевой якорь.
    """
    events = _night_rise_at(8, 0) + [
        ("sleep", _dt(3, 13, 0)),
        ("wake", _dt(3, 16, 0)),
    ]

    lower_bound = night_window(
        events, _dt(3, 22, 0), {"wake_total_min": 11.5}
    )
    assert lower_bound.wake_total == timedelta(hours=11)
    assert lower_bound.reach_lower == _dt(3, 22, 30)
    assert lower_bound.later_than_deadline is False

    later_limit = night_window(events, _dt(3, 22, 0), {"night_deadline": "23:00"})
    assert later_limit.deadline == _dt(3, 23, 0)
    assert later_limit.later_than_deadline is False

    earlier_target = night_window(events, _dt(3, 22, 0), {"night_target": "21:30"})
    assert earlier_target.target == _dt(3, 21, 30)


def test_tc_rec_016_night_defaults_mirror_settings_default_keys() -> None:
    """TC-REC-016 (design D14/D16): аргументные дефолты total_wake_at /
    night_window — зеркало DEFAULT_KEYS пакета 1 (единый источник;
    прямой импорт DEFAULT_KEYS в calculation запрещён — соответствие
    фиксирует ЭТОТ тест).
    """
    wake_params = total_wake_at.__kwdefaults__
    assert wake_params is not None, "keyword-only дефолты total_wake_at ожидаемы"
    assert wake_params["merge_threshold_min"] == DEFAULT_KEYS["merge_threshold"]
    assert wake_params["day_start"] == time(6, 0)
    assert str(DEFAULT_KEYS["day_start"]) == "06:00"

    night_params = night_window.__kwdefaults__
    assert night_params is not None, "keyword-only дефолты night_window ожидаемы"
    assert night_params["wake_total_min_h"] == DEFAULT_KEYS["wake_total_min"]
    assert night_params["wake_total_max_h"] == DEFAULT_KEYS["wake_total_max"]
    assert night_params["wake_total_min_h"] == 12.0
    assert night_params["wake_total_max_h"] == 12.5
    assert night_params["night_target"] == time(22, 0)
    assert night_params["night_deadline"] == time(22, 30)
    assert str(DEFAULT_KEYS["night_target"]) == "22:00"
    assert str(DEFAULT_KEYS["night_deadline"]) == "22:30"
    assert night_params["merge_threshold_min"] == DEFAULT_KEYS["merge_threshold"]
    assert night_params["day_start"] == time(6, 0)
