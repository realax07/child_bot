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
from datetime import date, datetime, time

from src.services.calculation import (
    day_facts_lines,
    nap_end_after_deadline,
    nap_end_forecast,
    nap_window,
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
