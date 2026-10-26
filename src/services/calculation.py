"""Чистые расчёты разметки день/ночь (без Telegram и БД).

Разметка вычисляется на лету при чтении (design D6); границы ночи —
параметры из настроек пользователя. Все времена — локальные (naive)
в TZ пользователя; TZ применяется на границе страницы дня (design D4).

Трассировка: FR-9, FR-10, FR-11; design D7; ТЗ §4.2.
Отнесение сна к странице дня: FR-12; design D8; решение ПМ 26.10.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

#: Текст предупреждения FR-10 (ТЗ §4.2, дословно).
LATE_DAY_SLEEP_WARNING = "Поздний дневной сон может сдвинуть ночной"

#: Зазор предупреждения перед ночной границей (design D7, warn_gap = 10 мин).
WARN_GAP = timedelta(minutes=10)

#: Порог короткого сна в ночном диапазоне (ТЗ §4.2): метка при < 40 мин.
SHORT_NIGHT_SLEEP_THRESHOLD = timedelta(minutes=40)

#: Начало страницы дня по умолчанию (ТЗ §5 «Начало дня 06:00»).
DEFAULT_DAY_START = time(6, 0)


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


def day_page_of(
    local_datetime: datetime,
    day_start: time = DEFAULT_DAY_START,
) -> date:
    """Страница дня для момента времени (design D8, ТЗ §5).

    Страница дня = day_start (по умолчанию 06:00) текущего дня →
    05:59 следующего. Момент до day_start относится к предыдущему
    календарному дню; момент >= day_start — к текущему дню.

    Args:
        local_datetime: момент, naive datetime в TZ пользователя.
        day_start: граница начала страницы дня (настройка, дефолт 06:00).

    Returns:
        calendar date страницы дня, к которой относится момент.
    """
    if local_datetime.time() < day_start:
        return local_datetime.date() - timedelta(days=1)
    return local_datetime.date()


def sleep_page(
    start_local: datetime,
    end_local: datetime,
    day_start: time = DEFAULT_DAY_START,
) -> date:
    """Страница дня, в которую попадает сон (design D8, FR-12).

    Единое правило (решение ПМ 26.10, решение продакта №2): сон целиком
    относится к странице дня момента НАЧАЛА сна. Для начала в
    [00:00; day_start) это страница предыдущего календарного дня;
    ночной сон через полночь вследствие этого целиком попадает в
    страницу дня начала сна. Нормализация сна через полночь (naive
    end < start) выполняется как в classify_sleep (3.1).

    Args:
        start_local: начало сна, naive datetime в TZ пользователя.
        end_local: конец сна, naive datetime в TZ пользователя
            (в отнесении не участвует — сон относится по началу).
        day_start: граница начала страницы дня (настройка, дефолт 06:00).

    Returns:
        calendar date страницы дня, к которой относится весь сон.
    """
    # Сон через полночь (naive-времена): конец попадает на следующий день.
    if end_local < start_local:
        end_local = end_local + timedelta(days=1)
    return day_page_of(start_local, day_start)
