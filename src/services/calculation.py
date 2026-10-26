"""Чистые расчёты разметки день/ночь (без Telegram и БД).

Разметка вычисляется на лету при чтении (design D6); границы ночи —
параметры из настроек пользователя. Все времена — локальные (naive)
в TZ пользователя; TZ применяется на границе страницы дня (design D4).

Трассировка: FR-9, FR-10, FR-11; design D7; ТЗ §4.2.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

#: Текст предупреждения FR-10 (ТЗ §4.2, дословно).
LATE_DAY_SLEEP_WARNING = "Поздний дневной сон может сдвинуть ночной"

#: Зазор предупреждения перед ночной границей (design D7, warn_gap = 10 мин).
WARN_GAP = timedelta(minutes=10)

#: Порог короткого сна в ночном диапазоне (ТЗ §4.2): метка при < 40 мин.
SHORT_NIGHT_SLEEP_THRESHOLD = timedelta(minutes=40)


@dataclass(frozen=True)
class DayNightMark:
    """Результат разметки интервала сна.

    Attributes:
        is_night: True — ночной сон, False — дневной (FR-9).
        warning: текст предупреждения FR-10 либо None.
        requires_check: флаг «требует проверки» в данных (FR-11);
            текст по метке в этом пакете не выводится.
    """

    is_night: bool
    warning: str | None
    requires_check: bool


def classify_sleep(
    start_local: datetime,
    end_local: datetime,
    night_start: time = time(19, 0),
    night_end: time = time(7, 0),
) -> DayNightMark:
    """Разметить интервал сна как дневной или ночной (design D7).

    Правила:
      - ночной, если начало >= night_start ИЛИ конец <= night_end (FR-9);
      - начало в [night_start - warn_gap; night_start) — дневной
        с предупреждением LATE_DAY_SLEEP_WARNING (FR-10);
      - начало в ночном диапазоне и длительность < 40 мин —
        requires_check=True, флаг только в данных (FR-11).

    Args:
        start_local: начало сна, naive datetime в TZ пользователя.
        end_local: конец сна, naive datetime в TZ пользователя.
        night_start: граница начала ночи (настройка, дефолт 19:00).
        night_end: граница конца ночи (настройка, дефолт 07:00).

    Returns:
        DayNightMark с полями is_night, warning, requires_check.
    """
    start_t = start_local.time()
    end_t = end_local.time()

    # Сон через полночь (naive-времена): конец попадает на следующий день.
    if end_local < start_local:
        end_local = end_local + timedelta(days=1)

    is_night = start_t >= night_start or end_t <= night_end

    warning: str | None = None
    night_start_dt = datetime.combine(start_local.date(), night_start)
    warn_from = night_start_dt - WARN_GAP
    if not is_night and warn_from <= start_local < night_start_dt:
        warning = LATE_DAY_SLEEP_WARNING

    requires_check = False
    if is_night and (end_local - start_local) < SHORT_NIGHT_SLEEP_THRESHOLD:
        requires_check = True

    return DayNightMark(is_night=is_night, warning=warning, requires_check=requires_check)
