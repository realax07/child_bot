"""Чистые расчёты разметки день/ночь (без Telegram и БД).

Разметка вычисляется на лету при чтении (design D6); границы ночи —
параметры из настроек пользователя. Все времена — локальные (naive)
в TZ пользователя; TZ применяется на границе страницы дня (design D4).

Трассировка: FR-9, FR-10, FR-11; design D7; ТЗ §4.2.
Отнесение сна к странице дня: FR-12; design D8; решение ПМ 26.10.
Интервалы сна из сырых событий: FR-13; design D6; ТЗ §10.5.
Склейка коротких пробуждений: FR-13, FR-14; design D9; ТЗ §4.3.
Сводка за день на лету: FR-3, FR-12, FR-19; design D6, D8.
Сводка событий дня в подтверждениях (PB-3, волна 6): FR-7/FR-8/FR-19
в ред. волны 6; решение продакта 29.10; design D7, D8.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from src import texts

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
        # Защитный кламп (CC-2, review-001-3.3): момент расчёта раньше
        # начала сна не должен давать интервал с отрицательной
        # длительностью — интервал схлопывается в нулевой.
        end = max(end, open_start)
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


@dataclass(frozen=True)
class DailySummary:
    """Сводка по странице дня (FR-19, design D6/D8).

    Attributes:
        page_date: календарная дата страницы дня.
        total_sleep: суммарный сон страницы — durations интервалов,
            отнесённых к странице по моменту начала (design D8),
            целиком, без обрезки по границе окна.
        total_wake: суммарное бодрствование внутри окна страницы
            (day_start → day_start следующего дня): окно минус
            фактическое покрытие сном (сквозные интервалы обрезаются
            по границе окна — бодрствование это фактическое время
            без сна внутри окна).
        sleep_intervals: интервалы сна, отнесённые к странице
            (после склейки), в хронологическом порядке.
    """

    page_date: date
    total_sleep: timedelta
    total_wake: timedelta
    sleep_intervals: tuple[SleepInterval, ...]


def daily_summary(
    events: Iterable[tuple[str, datetime]],
    page_date: date,
    day_start: time = DEFAULT_DAY_START,
    now_local: datetime | None = None,
    merge_threshold_min: int = 20,
) -> DailySummary:
    """Сводка за день на лету из сырых событий (FR-3, FR-12, FR-19;
    design D6, D8).

    Чистая функция: читает сырые события, ничего не пишет в БД и не
    создаёт хранимых агрегатов (FR-3, ТЗ §9.5). Сон после склейки
    относится к странице дня по моменту НАЧАЛА сна (design D8) и
    учитывается целиком — ночной сон через полночь целиком в странице
    дня начала. Бодрствование — фактическое время без сна внутри окна
    страницы (day_start → day_start следующего дня): короткое
    пробуждение, склеенное порогом, бодрствование не создаёт (FR-14).

    Args:
        events: сырые пары (event_type, timestamp) — 'sleep' / 'wake',
            naive datetime в TZ пользователя, любой порядок.
        page_date: календарная дата страницы дня.
        day_start: граница страницы дня (настройка, дефолт 06:00).
        now_local: момент расчёта (закрывает незакрытый sleep);
            naive datetime в TZ пользователя или None.
        merge_threshold_min: порог склейки в минутах (из settings
            пользователя, FR-24).

    Returns:
        DailySummary для страницы page_date.
    """
    window_start = datetime.combine(page_date, day_start)
    window_end = _day_page_end(page_date, day_start)

    merged = merge_intervals(
        build_sleep_intervals(events, now_local=now_local, day_start=day_start),
        merge_threshold_min,
    )

    attributed = tuple(
        interval
        for interval in merged
        if day_page_of(interval.start_local, day_start) == page_date
    )
    total_sleep = sum((interval.duration for interval in attributed), timedelta())

    covered = timedelta()
    for interval in merged:
        overlap_start = max(interval.start_local, window_start)
        overlap_end = min(interval.end_local, window_end)
        if overlap_end > overlap_start:
            covered += overlap_end - overlap_start
    total_wake = (window_end - window_start) - covered

    return DailySummary(
        page_date=page_date,
        total_sleep=total_sleep,
        total_wake=total_wake,
        sleep_intervals=attributed,
    )


def _window_sleep_coverage(
    merged: Iterable[SleepInterval],
    window_start: datetime,
    window_end: datetime,
) -> timedelta:
    """Фактическое покрытие окна страницы сном (сквозные интервалы
    обрезаются по границе окна) — база «Бодрствование общее» PB-3."""
    covered = timedelta()
    for interval in merged:
        overlap_start = max(interval.start_local, window_start)
        overlap_end = min(interval.end_local, window_end)
        if overlap_end > overlap_start:
            covered += overlap_end - overlap_start
    return covered


def day_facts_lines(
    events: Iterable[tuple[str, datetime]],
    page_date: date,
    day_start: time = DEFAULT_DAY_START,
    now_local: datetime | None = None,
    merge_threshold_min: int = 20,
    bedtime_from: time = time(19, 0),
    rise_until: time = time(12, 0),
) -> list[str]:
    """Строки фактов дня для подтверждений (PB-3, волна 6; FR-7/FR-8/FR-19
    в ред. волны 6; решение продакта 29.10).

    Формат продакта (backlog PB-3, утверждён Заказчиком 29.10): список
    ФАКТИЧЕСКИХ событий дня страницы + «Бодрствование общее». Единая
    точка сборки для всех трёх подтверждений (§7.5 засыпание, §7.2
    пробуждение, §7.14 ввод задним числом) — форматирование НЕ дублируется
    по хендлерам.

    Правила вывода (строка только при наличии факта):
      - «Подъем: HH:MM» — конец ПОСЛЕДНЕГО ЗАВЕРШЁННОГО ночного отрезка,
        окончившегося в окне страницы до 12:00 локального, после которого
        ночь не продолжается более поздним ночным отрезком (подъём утра
        этой страницы; ночной сон мог начаться вечером предыдущего дня;
        промежуточные ночные отрезки — короткие пробуждения внутри ночи —
        «Подъем» не создают);
      - «Заснул: HH:MM» / «Проснулся: HH:MM» — дневные отрезки (D7),
        начавшиеся в окне страницы, после склейки (D9); незакрытый
        отрезок — только «Заснул»;
      - «Дневной сон: H ч M мин» — суммарно по завершённым дневным
        отрезкам страницы (строка только если такой был);
      - «Отбой: HH:MM» — начало ПЕРВОГО ночного отрезка страницы,
        начавшегося в вечернем окне (не раньше 19:00 локального дня
        страницы, уход в ночь); ночные отрезки с началом раньше
        (утренние/дневные эпизоды) «Отбой» не создают;
      - «Ночной сон: H ч M мин» — суммарно по завершённым ночным
        отрезкам страницы (строка только если такой был);
      - «Бодрствование общее: H ч M мин» — окно страницы (day_start →
        day_start следующего дня) минус фактическое покрытие сном
        (склейка учтена, D9) — выводится всегда.
    Суммарный сон за день НЕ вычисляется и НЕ выводится (может
    превышать 24 ч из-за ночной страницы — design D8, «сумма сна смысла
    не имеет»). Отнесение фактов к странице — по окну страницы
    (day_start → day_start следующего дня, design D8): Подъем — по
    моменту конца в окне (подъём утра этой страницы), остальные
    отрезки — по моменту начала.

    Чистая функция: БД не касается, хранимых агрегатов не создаёт.

    Args:
        events: сырые пары (event_type, timestamp) — 'sleep' / 'wake',
            naive datetime в TZ пользователя, любой порядок.
        page_date: календарная дата страницы дня.
        day_start: граница страницы дня (настройка, дефолт 06:00).
        now_local: момент расчёта (закрывает незакрытый sleep);
            naive datetime в TZ пользователя или None.
        merge_threshold_min: порог склейки в минутах (из settings,
            FR-24).
        bedtime_from: граница вечернего окна факта «Отбой» — «Отбой»
            только у ночного отрезка, начавшегося не раньше (minor-1
            review-001-6.1).
        rise_until: граница утреннего окна факта «Подъем» — «Подъем»
            только у ночного отрезка, окончившегося до этого момента
            локального времени (minor-1 review-001-6.1).

    Returns:
        list[str] — строки фактов в порядке продакта; для пустого дня —
        только «Бодрствование общее».
    """
    ordered = sorted(events, key=lambda item: item[1])
    merged = merge_intervals(
        build_sleep_intervals(events, now_local=now_local, day_start=day_start),
        merge_threshold_min,
    )

    window_start = datetime.combine(page_date, day_start)
    window_end = _day_page_end(page_date, day_start)

    # Незакрытый sleep: последняя запись — sleep без парного wake; это
    # ВСЕГДА последний построенный интервал (его конец — не факт).
    unclosed: SleepInterval | None = None
    if ordered and ordered[-1][0] == "sleep" and merged:
        unclosed = merged[-1]

    def closed(interval: SleepInterval) -> bool:
        return interval is not unclosed

    night_in_page: list[SleepInterval] = []
    day_in_page: list[SleepInterval] = []
    for interval in merged:
        if not (window_start <= interval.start_local < window_end):
            continue
        if classify_sleep(interval.start_local, interval.end_local).is_night:
            night_in_page.append(interval)
        else:
            day_in_page.append(interval)

    lines: list[str] = []

    # Подъем: конец последнего завершённого ночного отрезка, окончившегося
    # в окне страницы до rise_until локального (ночь могла начаться ещё
    # вечером предыдущего дня). Кандидат — только отрезок, после которого
    # ночь НЕ продолжается: нет более позднего ночного отрезка,
    # начинающегося до rise_until (иначе конец — промежуточное ночное
    # пробуждение, ребенок «в ночи»; minor-1 review-001-6.1).
    night_sorted = [
        interval
        for interval in merged
        if classify_sleep(interval.start_local, interval.end_local).is_night
    ]
    rise_candidates = []
    for idx, interval in enumerate(night_sorted):
        if not (
            closed(interval)
            and window_start < interval.end_local <= window_end
            and interval.end_local.time() < rise_until
        ):
            continue
        night_continues = any(
            later.start_local.time() < rise_until
            for later in night_sorted[idx + 1 :]
        )
        if not night_continues:
            rise_candidates.append(interval)
    if rise_candidates:
        lines.append(f"{texts.FACT_RISE}: {rise_candidates[-1].end_local:%H:%M}")

    for interval in day_in_page:
        lines.append(f"{texts.FACT_FELL_ASLEEP}: {interval.start_local:%H:%M}")
        if closed(interval):
            lines.append(f"{texts.FACT_WOKE_UP}: {interval.end_local:%H:%M}")

    closed_day = [i for i in day_in_page if closed(i)]
    if closed_day:
        total = sum((i.duration for i in closed_day), timedelta())
        lines.append(f"{texts.FACT_DAY_SLEEP}: {texts.format_duration(total)}")

    if night_in_page:
        bedtime_candidates = [
            interval
            for interval in night_in_page
            if interval.start_local.time() >= bedtime_from
        ]
        if bedtime_candidates:
            lines.append(
                f"{texts.FACT_BEDTIME}: {bedtime_candidates[0].start_local:%H:%M}"
            )

    closed_night = [i for i in night_in_page if closed(i)]
    if closed_night:
        total = sum((i.duration for i in closed_night), timedelta())
        lines.append(f"{texts.FACT_NIGHT_SLEEP}: {texts.format_duration(total)}")

    total_wake = (window_end - window_start) - _window_sleep_coverage(
        merged, window_start, window_end
    )
    lines.append(f"{texts.FACT_TOTAL_WAKE}: {texts.format_duration(total_wake)}")
    return lines
