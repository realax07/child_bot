"""RecommendationService — сборка статуса «⏰ Подсказка сейчас» (change-пакет
add-recommendations, задача 2.1; design D17).

Сервис НАД CalculationService: собственной арифметики бодрствования НЕТ —
суммарное бодрствование берётся через `total_wake_at` (единая формула
BUG-004 с фактом PB-3, design D16), окна — через `nap_window` /
`night_window`, конфликты якорей — через `anchor_conflicts`, прогноз
конца дневного сна — через `nap_end_forecast` (вторая реализация формулы
запрещена D16/D17). Незакрытый сон — единый хелпер `_unclosed_interval`
расчётов (minor-3 review-001-1.1).

Вход — сырые пары (event_type, timestamp) naive в TZ пользователя +
настройки (dict, родные типы как в SettingsService.get_all) + момент
расчёта now_local — как у функций расчёта. БД не трогается, событий не
создаёт (кнопка подсказки не пишет в sleep_events, FR-32); чистая.

Возврат — СТРУКТУРА (dict с полями и готовыми строками фактов):
финальные дословные тексты §7.6–7.9 — зона задачи 2.3 (`src/texts.py`),
здесь они НЕ хардкодятся. Время и состояние в текстовых полях не
дублируются лишний раз: поля содержат длительности, позиции к диапазону,
моменты окон и их строковые фрагменты («ЧЧ:ММ», формат durations из
`texts.format_duration`) — «голых» цифр без контекста нет (ТЗ §4.6),
но и готовых абзацев §7 тоже нет.

Состояния (design D5 по последнему событию, здесь — по timestamp,
как во всех чистых расчётах):
  - «спит» (последнее событие sleep — незакрытый сон): сколько спит,
    прогноз пробуждения (дневной сон — от target_nap_duration), прогноз
    бодрствования после; «пора укладывать» спящему не выдаётся (FR-33);
    незавершённый дневной сон дольше цели — мягкая отметка FR-37
    (push «пора будить» отклонён, ОВ-4.1 — поля push в структуре нет);
  - «бодрствует» (последнее wake + есть факт «Подъем» страницы):
    структура §4.6 из 5 пунктов — ключевая цифра (с последнего
    пробуждения), суммарно за день (BUG-004), позиция ниже/в/выше
    диапазона 12–12,5 ч, окно следующего сна с учётом времени суток,
    предупреждения (§7.8 поздний дневной сон с цифрами, §7.9 перегул,
    конфликты якорей D18) (FR-32, FR-35, FR-36);
  - «неполные данные» (факта «Подъем» нет): просьба отметить события,
    «0 ч» и окна не выводятся (FR-34) — полей длительностей в структуре
    нет вовсе.

Учёт времени суток (ТЗ §4.6 «утром — дневное окно, вечером — ночное»):
пока момент расчёта не прошёл конец дневного окна (nap_window.end),
следующий сон — дневной; после — ночной (NightWindow.recommended —
конкретное время ухода по ОВ-4.2). Подъём страницы всегда ≤ 12:00
(rise_until), поэтому конец дневного окна всегда ≤ 18:00 — правило
«утро/вечер» не требует отдельной границы времени суток.

Несовместимые предложения в прошлом (minor-1 review-001-1.3): у каждого
конфликта якорей сервис вычисляет осуществимость — предложение
latest_nap_end раньше момента расчёта обнуляется (None) с флагом
feasible=False, чтобы волна 2.3 дала честное сообщение вместо времени
из прошлого.

Трассировка: FR-32…FR-37; design D17, D18; ТЗ §4.6, §7.6–7.9.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from src import texts
from src.services.calculation import (
    _as_hhmm,
    _as_hours,
    _as_minutes,
    _setting_value,
    _unclosed_interval,
    anchor_conflicts,
    build_sleep_intervals,
    classify_sleep,
    merge_intervals,
    nap_end_forecast,
    nap_window,
    night_window,
    total_wake_at,
)

# Дефолты границ диапазона позиций (ТЗ §5): зеркало DEFAULT_KEYS пакета 1
# (нижняя 12,0 ч / верхняя 12,5 ч) — прямой импорт DEFAULT_KEYS сюда
# запрещён чистотой модуля (без sqlite3), соответствие фиксирует тест
# TC-REC-032 (как TC-REC-008/016 у расчётов).
DEFAULT_WAKE_TOTAL_MIN_H = 12.0
DEFAULT_WAKE_TOTAL_MAX_H = 12.5

# Состояния ответа (ТЗ §4.6; design D5).
STATE_AWAKE = "awake"
STATE_ASLEEP = "asleep"
STATE_INCOMPLETE = "incomplete"

# Позиция к диапазону границ (§4.6 «Пограничные состояния»).
POSITION_BELOW = "below"
POSITION_IN = "in"
POSITION_ABOVE = "above"

# Типы предупреждений (п. 5 структуры §4.6; FR-35, FR-36, FR-31/D18).
WARNING_LATE_NAP = "late_nap"            # §7.8 — поздний дневной сон
WARNING_OVERTIRED = "overtired"          # §7.9 — перегул
WARNING_ANCHOR_CONFLICT = "anchor_conflict"  # конфликты якорей (D18)


def _hhmm_line(moment: datetime) -> str:
    """Момент времени в строку «ЧЧ:ММ» (фрагмент факта для волны 2.3)."""
    return f"{moment:%H:%M}"


class RecommendationService:
    """Сборка статуса подсказки из расчётов окон (design D17).

    Экземпляр — один запрос статуса: события, момент расчёта и настройки
    фиксируются при создании, `status()` возвращает структурный dict.
    Сервис чистый: БД не касается, событий не создаёт.
    """

    def __init__(
        self,
        events: Iterable[tuple[str, datetime]],
        now_local: datetime,
        settings: Mapping[str, int | float | str] | None = None,
    ) -> None:
        self._events = list(events)
        self._now = now_local
        self._settings = settings

    # -- точки чтения настроек (FR-24: при каждом расчёте; те же ключи,
    #    что у расчётов; парсинг — хелперы calculation, без дублей) ----

    def _day_start(self):
        return _as_hhmm(_setting_value(self._settings, "day_start", "06:00"))

    def _merge_threshold(self) -> int:
        return _as_minutes(_setting_value(self._settings, "merge_threshold", 20))

    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Структурный ответ подсказки для текущего состояния ребёнка.

        Returns:
            dict с ключом "state":
              - STATE_ASLEEP — поля сна §7.7 (см. `_sleeping_status`);
              - STATE_AWAKE — структура §4.6 из 5 пунктов
                (см. `_awake_status`);
              - STATE_INCOMPLETE — неполные данные, просьба отметить
                события; полей «0 ч»/окон в структуре нет (FR-34).
        """
        ordered = sorted(self._events, key=lambda item: item[1])
        merged = merge_intervals(
            build_sleep_intervals(
                self._events, now_local=self._now, day_start=self._day_start(),
            ),
            self._merge_threshold(),
        )
        # Незакрытый сон — ЕДИНЫЙ хелпер расчётов (design D16, minor-3
        # review-001-1.1): правило не дублируется.
        unclosed = _unclosed_interval(ordered, merged)
        if unclosed is not None:
            return self._sleeping_status(unclosed)
        return self._awake_status()

    # -- «спит» (FR-33, FR-37; ТЗ §4.6, §7.7) --------------------------

    def _sleeping_status(self, unclosed) -> dict:
        is_night = classify_sleep(
            unclosed.start_local, unclosed.end_local
        ).is_night
        result: dict = {
            "state": STATE_ASLEEP,
            "sleep_start": unclosed.start_local,
            "sleep_start_line": _hhmm_line(unclosed.start_local),
            "duration": unclosed.duration,
            "duration_line": texts.format_duration(unclosed.duration),
            "is_night": is_night,
            # Прогнозы — только для дневного сна (FR-33: «для дневного
            # сна — от целевой длительности»); у ночного своей метрики
            # цели нет — честный None, арифметику изобретать запрещено.
            "forecast_wake": None,
            "forecast_wake_line": None,
            "forecast_total_wake_after": None,
            "forecast_total_wake_line": None,
            # Мягкая отметка FR-37 (дневной сон дольше цели); поля push
            # «пора будить» в структуре НЕТ (ОВ-4.1 отклонён).
            "over_target": None,
        }
        if is_night:
            return result

        target = timedelta(minutes=_as_minutes(_setting_value(
            self._settings, "target_nap_duration", 90,
        )))
        forecast = nap_end_forecast(unclosed.start_local, self._settings)
        if forecast > self._now:
            # Прогноз в будущем: момент пробуждения + суммарное
            # бодрствование ПОСЛЕ него — через единую формулу BUG-004
            # (total_wake_at на момент прогноза; проекция «будущих
            # событий нет», как у NightWindow — design D16).
            total_after = total_wake_at(
                self._events, forecast, self._settings,
            )
            result["forecast_wake"] = forecast
            result["forecast_wake_line"] = _hhmm_line(forecast)
            result["forecast_total_wake_after"] = total_after
            result["forecast_total_wake_line"] = texts.format_duration(total_after)
        # else: цель уже прошла, «ожидаемое пробуждение» в прошлом не
        # советуем (честность, minor-1-стиль) — мягкая отметка ниже.
        if unclosed.duration > target:
            result["over_target"] = {
                "duration": unclosed.duration,
                "duration_line": texts.format_duration(unclosed.duration),
                "target": target,
                "target_line": texts.format_duration(target),
            }
        return result

    # -- «бодрствует» / «неполные данные» (FR-32, FR-34…FR-36) ---------

    def _awake_status(self) -> dict:
        nap = nap_window(self._events, self._now, self._settings)
        if nap is None:
            # Факта «Подъем» страницы нет — окна от ненастоящего подъёма
            # не строим, «0 ч» не выводим (FR-34): полей длительностей в
            # структуре нет, волна 2.3 просит отметить события.
            return {"state": STATE_INCOMPLETE, "has_rise": False}

        night = night_window(self._events, self._now, self._settings)
        total = night.wake_total  # == total_wake_at (единая формула BUG-004)

        lower = timedelta(hours=_as_hours(_setting_value(
            self._settings, "wake_total_min", DEFAULT_WAKE_TOTAL_MIN_H,
        )))
        upper = timedelta(hours=_as_hours(_setting_value(
            self._settings, "wake_total_max", DEFAULT_WAKE_TOTAL_MAX_H,
        )))
        if total < lower:
            position = POSITION_BELOW
        elif total > upper:
            position = POSITION_ABOVE
        else:
            position = POSITION_IN

        return {
            "state": STATE_AWAKE,
            # 1. Ключевая цифра — бодрствование с последнего пробуждения
            # (последнее событие здесь всегда wake: незакрытого сна нет,
            # факта подъёма нет → неполные данные).
            # Контракт wake_since_last_wake (minor review-001-2.1): при
            # несогласованном входе (момент расчёта раньше последнего
            # wake) разница отрицательна — клампится к нулю, в минус
            # поле не уходит.
            "wake_since_last_wake": _clamp_zero(
                self._now - ordered_last_wake(self._events)
            ),
            # 2. Суммарно за день — BUG-004 через total_wake_at.
            "total_wake": total,
            # 3. Позиция относительно диапазона + остатки до границ
            # (§7.6: «До нижней/верхней границы осталось»).
            "position": position,
            "bounds": {"lower": lower, "upper": upper},
            "remaining_to_lower": _clamp_zero(lower - total),
            "remaining_to_upper": _clamp_zero(upper - total),
            # 4. Окно следующего сна с учётом времени суток.
            "next_window": self._next_window(nap, night),
            # 5. Предупреждения (§7.8, §7.9, конфликты якорей D18).
            "warnings": self._warnings(nap, night, total, upper),
        }

    def _next_window(self, nap, night) -> dict:
        if self._now <= nap.end:
            return {
                "kind": "nap",
                "rise": nap.rise,
                "rise_line": _hhmm_line(nap.rise),
                "start": nap.start,
                "start_line": _hhmm_line(nap.start),
                "end": nap.end,
                "end_line": _hhmm_line(nap.end),
                "wake_deadline": nap.wake_deadline,
                "wake_deadline_line": _hhmm_line(nap.wake_deadline),
                "stretch_to_limit": nap.stretch_to_limit,
                "after_deadline": nap.after_deadline,
            }
        return {
            "kind": "night",
            "recommended": night.recommended,
            "recommended_line": _hhmm_line(night.recommended),
            "target": night.target,
            "target_line": _hhmm_line(night.target),
            "deadline": night.deadline,
            "deadline_line": _hhmm_line(night.deadline),
            "later_than_deadline": night.later_than_deadline,
        }

    def _warnings(self, nap, night, total, upper) -> list[dict]:
        warnings: list[dict] = []

        # §7.8 (FR-35): начало сна в окне даёт конец позже 15:30 —
        # цифры из nap_window (start → forecast_end vs wake_deadline).
        if nap.after_deadline:
            warnings.append({
                "type": WARNING_LATE_NAP,
                "nap_start": nap.start,
                "nap_start_line": _hhmm_line(nap.start),
                "forecast_end": nap.forecast_end,
                "forecast_end_line": _hhmm_line(nap.forecast_end),
                "deadline": nap.wake_deadline,
                "deadline_line": _hhmm_line(nap.wake_deadline),
            })

        # §7.9 (FR-36): перегул — фактическая цифра и верхняя граница.
        if total > upper:
            warnings.append({
                "type": WARNING_OVERTIRED,
                "total_wake": total,
                "total_wake_line": texts.format_duration(total),
                "upper": upper,
                "upper_line": texts.format_duration(upper),
            })

        # Конфликты якорей (D18): предложение в прошлом (minor-1
        # review-001-1.3) — неосуществимо, время из прошлого не
        # советуем: latest_nap_end обнуляется, флаг feasible=False.
        # Обычная ветка вместо assert-сужения типа (minor review-001-2.1):
        # согласованность «feasible ↔ proposal is not None» проверяется
        # условием, а не инвариантом.
        for conflict in anchor_conflicts(self._events, self._now, self._settings):
            proposal = conflict.latest_nap_end
            if proposal is not None and proposal >= self._now:
                latest_end, latest_line = proposal, _hhmm_line(proposal)
                feasible = True
            else:
                latest_end, latest_line = None, None
                feasible = False
            warnings.append({
                "type": WARNING_ANCHOR_CONFLICT,
                "conflicting": conflict.conflicting,
                "shifted": conflict.shifted,
                "accepted": conflict.accepted,
                "latest_nap_end": latest_end,
                "latest_nap_end_line": latest_line,
                "feasible": feasible,
            })
        return warnings


def ordered_last_wake(events: list[tuple[str, datetime]]) -> datetime:
    """Момент последнего события wake по timestamp (хронология сырых событий).

    Вызывается только из статуса «бодрствует»: незакрытого сна нет и
    факт «Подъем» есть, значит wake-событие в странице дня было.
    """
    last_wake = max(
        (ts for event_type, ts in events if event_type == "wake"),
        default=None,
    )
    if last_wake is None:  # pragma: no cover — защитный клапан
        raise ValueError("нет события wake при наличии факта «Подъем»")
    return last_wake


def _clamp_zero(value: timedelta) -> timedelta:
    """Отрицательный остаток до границы → 0 (граница уже достигнута)."""
    return value if value > timedelta(0) else timedelta(0)
