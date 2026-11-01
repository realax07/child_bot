"""RecommendationService — сборка статуса «⏰ Подсказка сейчас»
(add-recommendations 2.1).

Сервис НАД CalculationService (design D17): структура §4.6 из 5 пунктов
бодрствующему (FR-32), статус спящего §7.7 (FR-33), неполные данные без
«0 ч» (FR-34), предупреждения §7.8/§7.9 с фактическими цифрами
(FR-35, FR-36), мягкая отметка незавершённого дневного сна дольше цели
без пуша (FR-37, ОВ-4.1). Арифметика бодрствования — только через
total_wake_at (BUG-004/D16); неосуществимые предложения конфликтов в
прошлом обнуляются (minor-1 review-001-1.3).

Каждый тест помечен идентификатором TC-REC-xxx в докстринге; времена
фиксированные, «живых» ЧЧ:ММ нет. Полные тексты §7 — зона 2.3: здесь
проверяется СТРУКТУРА и фрагменты фактов, не дословные абзацы.
"""

import json
from datetime import date, datetime, time, timedelta

from src.services.calculation import day_facts_lines
from src.services.recommendation import (
    POSITION_ABOVE,
    POSITION_BELOW,
    POSITION_IN,
    STATE_ASLEEP,
    STATE_AWAKE,
    STATE_INCOMPLETE,
    WARNING_ANCHOR_CONFLICT,
    WARNING_LATE_NAP,
    WARNING_OVERTIRED,
    RecommendationService,
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


def test_tc_rec_022_awake_morning_structure_5_items() -> None:
    """TC-REC-022 (FR-32, §4.6/§7.6): структура бодрствующему из 5
    пунктов. Подъём 08:00, момент 12:30: ключевая цифра 4 ч 30 мин,
    суммарно за день 4 ч 30 мин (совпадает с фактом PB-3 — единая
    формула BUG-004), позиция «ниже», остатки до границ 7 ч 30 мин /
    8 ч, окно дневного сна 13:20–14:00 (утро → дневное окно),
    предупреждений нет. Времени «сейчас» (12:30) и слова состояния в
    структуре нет; «голых» цифр нет — каждый факт с контекстом-полем.
    """
    events = _night_rise_at(8, 0)
    status = RecommendationService(events, _dt(3, 12, 30)).status()

    assert status["state"] == STATE_AWAKE
    # 1. Ключевая цифра — с последнего пробуждения (08:00).
    assert status["wake_since_last_wake"] == timedelta(hours=4, minutes=30)
    # 2. Суммарно за день — та же арифметика, что факт PB-3 (не дубль).
    assert status["total_wake"] == timedelta(hours=4, minutes=30)
    facts = day_facts_lines(events, page_date=date(2026, 11, 3), now_local=_dt(3, 12, 30))
    assert "Бодрствование общее: 4 ч 30 мин" in facts
    # 3. Позиция ниже диапазона 12–12,5 ч + остатки до границ.
    assert status["position"] == POSITION_BELOW
    assert status["remaining_to_lower"] == timedelta(hours=7, minutes=30)
    assert status["remaining_to_upper"] == timedelta(hours=8)
    assert status["bounds"]["lower"] == timedelta(hours=12)
    assert status["bounds"]["upper"] == timedelta(hours=12, minutes=30)
    # 4. Окно следующего сна: утро → дневное окно 13:20–14:00.
    window = status["next_window"]
    assert window["kind"] == "nap"
    assert window["start"] == _dt(3, 13, 20)
    assert window["end"] == _dt(3, 14, 0)
    assert window["stretch_to_limit"] is False
    # 5. Предупреждений нет — якоря совместимы.
    assert status["warnings"] == []

    # Момент «сейчас» и слово состояния не протекают в ТЕКСТОВЫЕ поля
    # (сырые timedelta/datetime поля — данные для волны 2.3, не текст).
    text_fragments = [
        value
        for key, value in status.items()
        if isinstance(value, str) or key.endswith("_line")
    ] + [
        fragment
        for warning in status["warnings"]
        for fragment in warning.values()
        if isinstance(fragment, str)
    ]
    assert not any("12:30" in fragment for fragment in text_fragments)
    assert not any("бодрствует" in fragment for fragment in text_fragments)


def test_tc_rec_023_position_below_in_above_three_states() -> None:
    """TC-REC-023 (FR-32, §4.6 «Пограничные состояния»): позиция к
    диапазону 12–12,5 ч — ниже / в / выше. Ниже: подъём 08:00 без
    дневного сна, 12:00 → 4 ч. В: дневной сон 13:00–13:45, 20:45 →
    ровно 12 ч (нижняя граница включительно), вечер → ночное окно
    (рекомендация 22:00 по ОВ-4.2). Выше: сон 13:00–14:20, 22:00 →
    12 ч 40 мин.
    """
    below = RecommendationService(_night_rise_at(8, 0), _dt(3, 12, 0)).status()
    assert below["position"] == POSITION_BELOW

    in_range = RecommendationService(
        _night_rise_at(8, 0) + [("sleep", _dt(3, 13, 0)), ("wake", _dt(3, 13, 45))],
        _dt(3, 20, 45),
    ).status()
    assert in_range["position"] == POSITION_IN
    assert in_range["total_wake"] == timedelta(hours=12)
    assert in_range["remaining_to_lower"] == timedelta(0)
    assert in_range["remaining_to_upper"] == timedelta(minutes=30)
    # Вечер (дневное окно 14:00 уже прошло) → ночное окно по ОВ-4.2.
    assert in_range["next_window"]["kind"] == "night"
    assert in_range["next_window"]["recommended"] == _dt(3, 22, 0)
    assert in_range["next_window"]["target"] == _dt(3, 22, 0)
    assert in_range["next_window"]["deadline"] == _dt(3, 22, 30)

    above = RecommendationService(
        _night_rise_at(8, 0) + [("sleep", _dt(3, 13, 0)), ("wake", _dt(3, 14, 20))],
        _dt(3, 22, 0),
    ).status()
    assert above["position"] == POSITION_ABOVE
    assert above["total_wake"] == timedelta(hours=12, minutes=40)


def test_tc_rec_024_late_nap_warning_7_8_with_figures() -> None:
    """TC-REC-024 (FR-35, §7.8): поздний подъём 09:00 — предупреждение
    о позднем дневном сне С ФАКТИЧЕСКИМИ цифрами: если сон начнётся в
    14:20 (начало окна), закончится около 15:50 — позже крайнего
    15:30. Цифры — из расчётов (nap_window), не хардкод.
    """
    status = RecommendationService(_night_rise_at(9, 0), _dt(3, 12, 0)).status()

    late = [
        warning
        for warning in status["warnings"]
        if warning["type"] == WARNING_LATE_NAP
    ]
    assert len(late) == 1
    warning = late[0]
    assert warning["nap_start"] == _dt(3, 14, 20)
    assert warning["nap_start_line"] == "14:20"
    assert warning["forecast_end"] == _dt(3, 15, 50)
    assert warning["forecast_end_line"] == "15:50"
    assert warning["deadline"] == _dt(3, 15, 30)
    assert warning["deadline_line"] == "15:30"
    # Совместный элемент — конфликт якорей «15:30 vs дневное окно» (D18),
    # предложение ещё осуществимо (15:30 позже момента 12:00).
    conflicts = [
        warning
        for warning in status["warnings"]
        if warning["type"] == WARNING_ANCHOR_CONFLICT
    ]
    assert len(conflicts) == 1
    assert conflicts[0]["feasible"] is True
    assert conflicts[0]["latest_nap_end"] == _dt(3, 15, 30)
    assert conflicts[0]["latest_nap_end_line"] == "15:30"


def test_tc_rec_025_overtired_warning_7_9_with_figures() -> None:
    """TC-REC-025 (FR-36, §7.9): перегул — предупреждение с фактической
    цифрой. Подъём 08:00, дневной сон 13:00–14:20 (1:20), момент
    22:00 → бодрствование 12 ч 40 мин — превышена верхняя граница
    12 ч 30 мин.
    """
    events = _night_rise_at(8, 0) + [
        ("sleep", _dt(3, 13, 0)),
        ("wake", _dt(3, 14, 20)),
    ]
    status = RecommendationService(events, _dt(3, 22, 0)).status()

    overtired = [
        warning
        for warning in status["warnings"]
        if warning["type"] == WARNING_OVERTIRED
    ]
    assert len(overtired) == 1
    warning = overtired[0]
    assert warning["total_wake"] == timedelta(hours=12, minutes=40)
    assert warning["total_wake_line"] == "12 ч 40 мин"
    assert warning["upper"] == timedelta(hours=12, minutes=30)
    assert warning["upper_line"] == "12 ч 30 мин"


def test_tc_rec_026_incomplete_no_zero_hours_no_window() -> None:
    """TC-REC-026 (FR-34, §4.6 «Неполные данные»): факта «Подъем» нет —
    состояние «неполные данные»: «0 ч» НЕТ в структуре, окон нет.
    Пустая страница дня (событий нет вовсе) и день только с дневным
    сном без ночи (подъёма нет) — оба случая.
    """
    empty = RecommendationService([], _dt(3, 12, 0)).status()
    assert empty["state"] == STATE_INCOMPLETE
    assert empty["has_rise"] is False
    assert "next_window" not in empty
    assert "total_wake" not in empty

    day_nap_only = RecommendationService(
        [("sleep", _dt(3, 13, 0)), ("wake", _dt(3, 14, 30))],
        _dt(3, 15, 0),
    ).status()
    assert day_nap_only["state"] == STATE_INCOMPLETE

    for status in (empty, day_nap_only):
        dumped = json.dumps(status, default=str, ensure_ascii=False)
        assert "0 ч" not in dumped, "«0 ч» в неполных данных"
        assert "13:20" not in dumped, "окно от ненастоящего подъёма"


def test_tc_rec_027_asleep_day_nap_forecast_7_7() -> None:
    """TC-REC-027 (FR-33, §7.7): статус спящему. Подъём 08:00, дневной
    сон с 13:30 не закрыт, момент 14:15, цель 90 мин: спит 45 мин,
    ожидаемое пробуждение около 15:00, бодрствование после — 5 ч 30 мин
    ((15:00 − 08:00) − 1:30 — ЕДИНАЯ формула BUG-004 через
    total_wake_at на момент прогноза). «Пора укладывать» в структуре
    нет; сон ещё не перерос цель — мягкой отметки нет.
    """
    events = _night_rise_at(8, 0) + [("sleep", _dt(3, 13, 30))]
    status = RecommendationService(events, _dt(3, 14, 15)).status()

    assert status["state"] == STATE_ASLEEP
    assert status["is_night"] is False
    assert status["sleep_start"] == _dt(3, 13, 30)
    assert status["sleep_start_line"] == "13:30"
    assert status["duration"] == timedelta(minutes=45)
    assert status["duration_line"] == "45 мин"
    assert status["forecast_wake"] == _dt(3, 15, 0)
    assert status["forecast_wake_line"] == "15:00"
    assert status["forecast_total_wake_after"] == timedelta(hours=5, minutes=30)
    assert status["forecast_total_wake_line"] == "5 ч 30 мин"
    assert status["over_target"] is None
    dumped = json.dumps(status, default=str, ensure_ascii=False)
    assert "укладывать" not in dumped, "совет «пора укладывать» спящему"


def test_tc_rec_028_over_target_soft_mark_no_push() -> None:
    """TC-REC-028 (FR-37, ОВ-4.1): незавершённый дневной сон 1 ч 40 мин
    при цели 90 мин → мягкая отметка (поля с длительностью и целью);
    прогноз пробуждения (15:00) уже в прошлом — честный None вместо
    времени из прошлого. Поля push «пора будить» в структуре НЕТ
    (ОВ-4.1 отклонён). Смена настройки target_nap_duration (90 → 120)
    снимает отметку (FR-24: настройки читаются при каждом расчёте).
    """
    events = _night_rise_at(8, 0) + [("sleep", _dt(3, 13, 30))]
    status = RecommendationService(events, _dt(3, 15, 10)).status()

    assert status["state"] == STATE_ASLEEP
    assert status["duration"] == timedelta(hours=1, minutes=40)
    assert status["forecast_wake"] is None, "прогноз из прошлого"
    assert status["forecast_wake_line"] is None
    over = status["over_target"]
    assert over is not None
    assert over["duration"] == timedelta(hours=1, minutes=40)
    assert over["duration_line"] == "1 ч 40 мин"
    assert over["target"] == timedelta(minutes=90)
    assert over["target_line"] == "1 ч 30 мин"
    assert "push" not in json.dumps(status, default=str, ensure_ascii=False)

    relaxed = RecommendationService(
        events, _dt(3, 15, 10), {"target_nap_duration": 120},
    ).status()
    assert relaxed["over_target"] is None
    assert relaxed["forecast_wake"] == _dt(3, 15, 30)


def test_tc_rec_029_asleep_night_no_forecast() -> None:
    """TC-REC-029 (FR-33): ночной сон — прогноза пробуждения и мягкой
    отметки нет (метрики цели у ночного сна нет; своя арифметика
    запрещена D17), факт «сколько спит» есть. Сон с 22:00, моменты
    23:00 и 03:00 следующей ночи — оба ночные.
    """
    for now in (_dt(2, 23, 0), _dt(3, 3, 0)):
        status = RecommendationService([("sleep", _dt(2, 22, 0))], now).status()
        assert status["state"] == STATE_ASLEEP
        assert status["is_night"] is True
        assert status["forecast_wake"] is None
        assert status["forecast_total_wake_after"] is None
        assert status["over_target"] is None


def test_tc_rec_030_conflict_proposal_in_past_not_advised() -> None:
    """TC-REC-030 (FR-31/D18 + minor-1 review-001-1.3): неосуществимое
    предложение конфликта (время из прошлого) не советуется.
    Осуществимо: подъём 10:00, момент 15:20 → предложение 15:30 ещё в
    будущем (feasible=True, время отдано). Неосуществимо: подъём 10:00,
    дневной сон 15:20–17:00, момент 17:00 → оба конфликта имеют
    предложения в прошлом — latest_nap_end обнулен (None) с флагом
    feasible=False, время из прошлого в структуре нет.
    """
    feasible = RecommendationService(_night_rise_at(10, 0), _dt(3, 15, 20)).status()
    conflicts = [
        warning
        for warning in feasible["warnings"]
        if warning["type"] == WARNING_ANCHOR_CONFLICT
    ]
    assert len(conflicts) == 1
    assert conflicts[0]["feasible"] is True
    assert conflicts[0]["latest_nap_end"] == _dt(3, 15, 30)

    past = RecommendationService(
        _night_rise_at(10, 0)
        + [("sleep", _dt(3, 15, 20)), ("wake", _dt(3, 17, 0))],
        _dt(3, 17, 0),
    ).status()
    conflicts = [
        warning
        for warning in past["warnings"]
        if warning["type"] == WARNING_ANCHOR_CONFLICT
    ]
    assert len(conflicts) == 2
    for warning in conflicts:
        assert warning["feasible"] is False
        assert warning["latest_nap_end"] is None, "время из прошлого в ответе"
        assert warning["latest_nap_end_line"] is None
    # Сам доклад конфликта остаётся (честность D18): якоря и принятое
    # окно присутствуют, сдвигается «дневное окно».
    assert {warning["shifted"] for warning in conflicts} == {"дневное окно"}
    assert all(warning["accepted"] for warning in conflicts)


def test_tc_rec_031_same_input_same_status_deterministic() -> None:
    """TC-REC-031 (design D17/D18, детерминированность): один и тот же
    вход (события + настройки + момент) даёт идентичную структуру
    статуса при повторном расчёте; сервис не меняет вход.
    """
    events = _night_rise_at(10, 0) + [
        ("sleep", _dt(3, 15, 20)),
        ("wake", _dt(3, 17, 0)),
    ]
    snapshot = list(events)

    first = RecommendationService(events, _dt(3, 17, 0)).status()
    second = RecommendationService(events, _dt(3, 17, 0)).status()
    assert first == second
    assert events == snapshot, "сервис изменил входные события"


def test_tc_rec_032_defaults_mirror_settings_default_keys() -> None:
    """TC-REC-032 (design D14/D17): дефолты границ диапазона позиций —
    зеркало DEFAULT_KEYS пакета 1 (нижняя 12,0 ч / верхняя 12,5 ч);
    прямой импорт DEFAULT_KEYS в сервис запрещён чистотой модуля —
    соответствие фиксирует ЭТОТ тест (как TC-REC-008/016 у расчётов).
    """
    from src.services import recommendation as rec

    assert rec.DEFAULT_WAKE_TOTAL_MIN_H == DEFAULT_KEYS["wake_total_min"]
    assert rec.DEFAULT_WAKE_TOTAL_MAX_H == DEFAULT_KEYS["wake_total_max"]
    # Порог склейки и границы дня читаются теми же хелперами расчётов.
    service = RecommendationService([], _dt(3, 12, 0))
    assert service._merge_threshold() == DEFAULT_KEYS["merge_threshold"]
    assert service._day_start() == time(6, 0)
    assert str(DEFAULT_KEYS["day_start"]) == "06:00"
