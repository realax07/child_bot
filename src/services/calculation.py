"""Чистые расчёты разметки день/ночь (без Telegram и БД).

Разметка вычисляется на лету при чтении (design D6); границы ночи —
параметры из настроек пользователя. Все времена — локальные (naive)
в TZ пользователя; TZ применяется на границе страницы дня (design D4).

Трассировка: FR-9, FR-10, FR-11; design D7; ТЗ §4.2.
Отнесение сна к странице дня: FR-12; design D8; решение ПМ 26.10.
Интервалы сна из сырых событий: FR-13; design D6; ТЗ §10.5.
Склейка коротких пробуждений: FR-13, FR-14; design D9; ТЗ §4.3.
"""

from collections.abc import Iterable
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


@dataclass(frozen=True)
class SleepInterval:
    """Расчётный интервал сна (ТЗ §10.5, design D6/D9).

    Attributes:
        start_local: начало сна, naive datetime в TZ пользователя.
        end_local: конец сна, naive datetime в TZ пользователя.
    """

    start_local: datetime
    end_local: datetime

    @property
    def duration(self) -> timedelta:
        """Длительность интервала сна."""
        return self.end_local - self.start_local


def _day_page_end(day_page: date, day_start: time) -> datetime:
    """Момент конца страницы дня: day_start следующего календарного дня."""
    return datetime.combine(day_page + timedelta(days=1), day_start)


def build_sleep_intervals(
    events: Iterable[tuple[str, datetime]],
    now_local: datetime | None = None,
    day_start: time = DEFAULT_DAY_START,
) -> list[SleepInterval]:
    """Построить интервалы сна из сырой последовательности sleep/wake
    (design D6, ТЗ §10.5) — база всех расчётов волны 3.

    Правила:
      - события сортируются по timestamp перед парсингом — сырые записи
        вне хронологического порядка обрабатываются корректно;
      - пары sleep→wake образуют интервал сна;
      - незакрытый sleep → интервал до now_local (момент расчёта), а при
        отсутствии now_local — до конца страницы дня начала сна
        (day_start следующего календарного дня);
      - wake без парного sleep игнорируется;
      - повторный sleep при открытом интервале игнорируется (интервал
        открывает первое засыпание).

    Args:
        events: сырые пары (event_type, timestamp) — 'sleep' / 'wake',
            naive datetime в TZ пользователя, любой порядок.
        now_local: момент расчёта (закрывает незакрытый sleep);
            naive datetime в TZ пользователя или None.
        day_start: граница страницы дня (настройка, дефолт 06:00) —
            определяет конец страницы при незакрытом sleep без now_local.

    Returns:
        list[SleepInterval] в хронологическом порядке.
    """
    ordered = sorted(events, key=lambda item: item[1])

    intervals: list[SleepInterval] = []
    open_start: datetime | None = None
    for event_type, timestamp in ordered:
        if event_type == "sleep":
            if open_start is None:
                open_start = timestamp
        elif event_type == "wake" and open_start is not None:
            intervals.append(SleepInterval(start_local=open_start, end_local=timestamp))
            open_start = None
        # wake без парного sleep — игнорируется (FR-13, ТЗ §4.1).

    if open_start is not None:
        end = now_local if now_local is not None else _day_page_end(
            day_page_of(open_start, day_start), day_start
        )
        intervals.append(SleepInterval(start_local=open_start, end_local=end))

    return intervals


def merge_intervals(
    intervals: Iterable[SleepInterval],
    threshold_min: int,
) -> list[SleepInterval]:
    """Склеить смежные интервалы сна при коротком пробуждении
    (design D9, FR-13, FR-14; ТЗ §4.3).

    Смежные интервалы объединяются, если бодрствование между ними
    СТРОГО меньше порога (пробуждение ровно threshold_min минут
    интервалы НЕ склеивает). Порог передаётся параметром — читается
    из settings пользователя при каждом расчёте (FR-24), смена порога
    меняет результат без перезаписи событий. is_merged в БД не пишется
    — склейка только вычисление (D9).

    Args:
        intervals: интервалы сна (результат build_sleep_intervals).
        threshold_min: порог склейки в минутах (из settings пользователя).

    Returns:
        list[SleepInterval] после склейки, в хронологическом порядке.
    """
    threshold = timedelta(minutes=threshold_min)
    ordered = sorted(intervals, key=lambda i: i.start_local)

    merged: list[SleepInterval] = []
    for interval in ordered:
        if merged and (interval.start_local - merged[-1].end_local) < threshold:
            prev = merged[-1]
            merged[-1] = SleepInterval(
                start_local=prev.start_local,
                end_local=max(prev.end_local, interval.end_local),
            )
        else:
            merged.append(interval)
    return merged
