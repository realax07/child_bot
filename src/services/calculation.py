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

from collections.abc import Iterable, Mapping
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


def _page_rise(
    merged: list[SleepInterval],
    unclosed: SleepInterval | None,
    page_date: date,
    window_start: datetime,
    window_end: datetime,
    bedtime_from: time,
    rise_until: time,
) -> datetime | None:
    """Факт «Подъем» страницы дня по правилам PB-3 (review-001/002).

    Единая точка правила для `day_facts_lines` (PB-3) и расчёта окон
    (design D16, задача 1.1 add-recommendations) — «Подъем» окон = факт
    «Подъем» страницы, дублирование арифметики запрещено (D16).

    Подъем: конец ПОСЛЕДНЕГО ЗАВЕРШЁННОГО ночного отрезка, окончившегося
    в окне страницы до rise_until локального (ночь могла начаться ещё
    вечером предыдущего дня). Кандидат — только отрезок, после которого
    ТА ЖЕ ночь не продолжается: нет более позднего ночного отрезка,
    начавшегося в пределах НОЧНОГО ОКНА этого конца — от вечернего
    отбоя (bedtime_from) предыдущих суток до утренней границы
    (rise_until) суток конца (конец кандидата всегда в утренней
    половине: time < rise_until). Промежуточные ночные пробуждения
    одной ночи подавляют кандидата (ребенок «в ночи», minor-1
    review-001-6.1), а отрезки ЧУЖИХ ночей/страниц — ранний утренний
    сон следующего дня, ночные отрезки любых других дат — не подавляют
    и не подменяют: сравнение по ПОЛНОМУ datetime с привязкой к дате
    конца кандидата, а не по time-of-day (major-1 review-002-6.1).

    Returns:
        момент конца подъёма (naive datetime) или None — факта нет.
    """

    def closed(interval: SleepInterval) -> bool:
        return interval is not unclosed

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
        night_from = datetime.combine(
            interval.end_local.date() - timedelta(days=1), bedtime_from
        )
        night_to = datetime.combine(interval.end_local.date(), rise_until)
        night_continues = any(
            night_from <= later.start_local <= night_to
            for later in night_sorted[idx + 1 :]
        )
        if not night_continues:
            rise_candidates.append(interval)
    if not rise_candidates:
        return None
    # Приоритет — подъём утра самой страницы (конец в дату страницы):
    # ранний утренний сон СЛЕДУЮЩИХ суток, оканчивающийся до day_start
    # и потому попавший в окно страницы по D8, не подменяет её
    # «Подъем» (major-1 review-002-6.1, контр-кейс C5/N1).
    page_morning = [i for i in rise_candidates if i.end_local.date() == page_date]
    chosen = page_morning or rise_candidates
    return chosen[-1].end_local


def _unclosed_interval(
    ordered: list[tuple[str, datetime]],
    merged: list[SleepInterval],
) -> SleepInterval | None:
    """Незакрытый sleep: последняя запись — sleep без парного wake.

    Единая точка правила для `day_facts_lines` (PB-3), `nap_window` и
    `total_wake_at` (design D16; minor-3 review-001-1.1: правило не
    дублируется по функциям). Это ВСЕГДА последний построенный
    интервал — его конец НЕ факт (подъёма, пробуждения). Пустой или
    полностью закрытый день — None.
    """
    if ordered and ordered[-1][0] == "sleep" and merged:
        return merged[-1]
    return None


def _bug004_total_wake(
    merged: list[SleepInterval],
    unclosed: SleepInterval | None,
    rise_end: datetime | None,
    now_local: datetime | None,
    window_start: datetime,
    window_end: datetime,
) -> timedelta:
    """«Бодрствование общее» — ЕДИНАЯ формула BUG-004 (ред. 31.10).

    (момент расчёта − Подъем) − покрытие ДНЕВНЫМ сном в [база; конец]:
    база — факт «Подъем» (`_page_rise`; нет подъёма — day_start
    страницы); конец — момент расчёта now_local (при now_local=None —
    конец окна страницы, страница закрыта); идущий незакрытый НОЧНОЙ
    сон замораживает конец на своём начале (после отбоя ребёнок не
    бодрствует); незакрытый ДНЕВНОЙ сон вычитается до now_local (сон,
    не бодрствование; его конец уже now_local — build_sleep_intervals);
    ночное покрытие не вычитается (в базе от подъёма его нет);
    отрицательный результат → 0.

    Единственная точка арифметики бодрствования: факт PB-3
    (`day_facts_lines`) и расчёты подсказок (`total_wake_at`, задача
    1.2 add-recommendations) считают через НЕЁ — вторая реализация
    формулы запрещена design D16/BUG-004 (исходный инцидент «суммы не
    сходятся»).
    """
    wake_base = rise_end if rise_end is not None else window_start
    wake_end = now_local if now_local is not None else window_end
    if unclosed is not None and classify_sleep(
        unclosed.start_local, unclosed.end_local
    ).is_night:
        wake_end = unclosed.start_local
    day_covered = timedelta()
    for interval in merged:
        if classify_sleep(interval.start_local, interval.end_local).is_night:
            continue
        overlap_start = max(interval.start_local, wake_base)
        overlap_end = min(interval.end_local, wake_end)
        if overlap_end > overlap_start:
            day_covered += overlap_end - overlap_start
    total_wake = (wake_end - wake_base) - day_covered
    if total_wake < timedelta(0):
        total_wake = timedelta(0)
    return total_wake


def _bedtime_from(
    settings: Mapping[str, int | float | str] | None,
    default: time,
) -> time:
    """Вечерняя граница «Отбоя» из night_bounds «ЧЧ:ММ-ЧЧ:ММ» (начало)."""
    raw_bounds = _setting_value(settings, "night_bounds", default)
    if isinstance(raw_bounds, str) and "-" in raw_bounds:
        return _as_hhmm(raw_bounds.split("-")[0])
    return _as_hhmm(raw_bounds)


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
        ТА ЖЕ ночь не продолжается более поздним ночным отрезком —
        начавшимся в пределах ночного окна этого конца (от вечернего
        отбоя предыдущих суток до утренней границы суток конца,
        сравнение по ПОЛНОМУ datetime; отрезки чужих ночей/страниц —
        утренний сон следующего дня, ночные отрезки других дат —
        «Подъем» не подавляют и не подменяют, major-1 review-002-6.1;
        при выборе приоритет у конца в дату самой страницы). Подъём —
        утра этой страницы; ночной сон мог начаться вечером предыдущего
        дня; промежуточные ночные отрезки — короткие пробуждения внутри
        одной ночи — «Подъем» не создают;
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
      - «Бодрствование общее: H ч M мин» — (момент расчёта − Подъем) −
        покрытие ДНЕВНЫМ сном в интервале [Подъем; момент расчёта]
        (ред. BUG-004, решение продакта 31.10): база — факт «Подъем»
        (нет подъёма — day_start страницы); конец — момент расчёта
        now_local (при now_local=None — конец окна страницы, страница
        закрыта); идущий незакрытый НОЧНОЙ сон замораживает конец на
        своём начале (после отбоя ребёнок не бодрствует); незакрытый
        ДНЕВНОЙ сон вычитается до now_local (сон, не бодрствование);
        ночное покрытие не вычитается (в базе от подъёма его нет);
        отрицательный результат → 0. Выводится всегда.
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

    # Незакрытый sleep — единый хелпер (design D16, minor-3 review-001-1.1).
    unclosed = _unclosed_interval(ordered, merged)

    def closed(interval: SleepInterval) -> bool:
        return interval is not unclosed

    night_in_page: list[SleepInterval] = []
    day_in_page: list[SleepInterval] = []
    rise_end: datetime | None = None
    for interval in merged:
        if not (window_start <= interval.start_local < window_end):
            continue
        if classify_sleep(interval.start_local, interval.end_local).is_night:
            night_in_page.append(interval)
        else:
            day_in_page.append(interval)

    lines: list[str] = []

    # Подъем: единое правило PB-3 — `_page_rise` (та же функция, что и
    # в расчёте окон, design D16; без дублей правила).
    rise_end = _page_rise(
        merged,
        unclosed,
        page_date,
        window_start,
        window_end,
        bedtime_from,
        rise_until,
    )
    if rise_end is not None:
        lines.append(f"{texts.FACT_RISE}: {rise_end:%H:%M}")

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

    # «Бодрствование общее» — единая формула BUG-004 через хелпер
    # (та же арифметика, что total_wake_at для подсказок; design D16:
    # без дублей формулы). Правила базы/конца/заморозки/клампа —
    # в докстринге `_bug004_total_wake`.
    total_wake = _bug004_total_wake(
        merged, unclosed, rise_end, now_local, window_start, window_end,
    )
    lines.append(f"{texts.FACT_TOTAL_WAKE}: {texts.format_duration(total_wake)}")
    return lines


# ---------------------------------------------------------------------------
# Окна дневного сна (add-recommendations, задача 1.1; design D16;
# ТЗ §4.5 задача 1, §3.1, §3.6; FR-26, FR-27, FR-28, FR-35).
#
# Чистые функции: вход — сырые пары (event_type, timestamp) naive в TZ
# пользователя + настройки (dict, родные типы как в SettingsService.get_all)
# + момент расчёта; БД не касается. Якоря — из переданных настроек
# (design D14: смена настройки меняет окна без релиза); дефолты —
# аргументные дефолты функций ниже (значения ТЗ §5 = DEFAULT_KEYS
# пакета 1; прямой импорт DEFAULT_KEYS в calculation запрещён —
# чистота модуля без sqlite3, guard TC-SUM-001; соответствие дефолтов
# DEFAULT_KEYS фиксирует тест TC-REC-008).
# ---------------------------------------------------------------------------


def _setting_value(
    settings: Mapping[str, int | float | str] | None,
    key: str,
    default: int | float | str | time,
) -> int | float | str | time:
    """Значение настройки из переданного dict; нет ключа — дефолт (FR-24)."""
    if settings is None:
        return default
    value = settings.get(key)
    return default if value is None else value


def _as_minutes(value: int | float | str | time) -> int:
    """Настройка-длительность в минутах (значения int в DEFAULT_KEYS)."""
    return int(value)  # type: ignore[arg-type]


def _as_hours(value: int | float | str | time) -> float:
    """Настройка-длительность в часах (значения float в DEFAULT_KEYS)."""
    return float(value)  # type: ignore[arg-type]


def _as_hhmm(value: int | float | str | time) -> time:
    """Настройка «ЧЧ:ММ» (str в DEFAULT_KEYS) или уже time → time."""
    if isinstance(value, time):
        return value
    hours_s, minutes_s = str(value).split(":")
    return time(int(hours_s), int(minutes_s))


@dataclass(frozen=True)
class NapWindow:
    """Окно дневного сна с отклонениями эталона (FR-26…FR-28; D16).

    Attributes:
        rise: факт «Подъем» страницы дня (PB-3, `_page_rise`) — база окна.
        start: начало окна = подъём + минимум бодрствования
            (настройка sleep_min_before_nap, дефолт 5 ч 20 мин).
        end: конец окна = подъём + максимум бодрствования
            (настройка sleep_max_before_nap, дефолт 6 ч).
        wake_deadline: крайнее пробуждение от дневного сна (настройка
            wake_deadline, дефолт 15:30) в дату подъёма.
        forecast_end: прогноз конца сна при начале В НАЧАЛЕ окна —
            start + target_nap_duration (настройка, дефолт 90 мин).
            Для произвольного начала сна — `nap_end_forecast` (FR-35).
        after_deadline: True — прогноз конца при начале в начале окна
            ПОЗЖЕ wake_deadline (поздний подъём §3.6 — предупреждение
            «будить не позже 15:30» / §7.8 с цифрами).
        stretch_to_limit: True — ранний подъём §3.6: всё окно
            укладывается в лимит растяжения (stretch_limit, дефолт
            13:00) — совет «тянем бодрствование до конца окна».
    """

    rise: datetime
    start: datetime
    end: datetime
    wake_deadline: datetime
    forecast_end: datetime
    after_deadline: bool
    stretch_to_limit: bool


def nap_end_forecast(
    nap_start_local: datetime,
    settings: Mapping[str, int | float | str] | None = None,
    *,
    target_nap_duration_min: int = 90,
) -> datetime:
    """Прогноз конца дневного сна при начале nap_start_local (FR-35).

    База прогноза — настройка target_nap_duration (дефолт 90 мин,
    решение №1 от 26.10); прогноз уточняется при следующем событии
    (допущение design.md). Чистая функция, naive в TZ пользователя.

    Args:
        nap_start_local: фактическое/планируемое начало сна, naive.
        settings: настройки пользователя (dict) или None — дефолты.
        target_nap_duration_min: дефолт цели дневного сна, мин.

    Returns:
        прогноз момента пробуждения, naive datetime.
    """
    raw = _setting_value(settings, "target_nap_duration", target_nap_duration_min)
    return nap_start_local + timedelta(minutes=_as_minutes(raw))


def nap_end_after_deadline(
    nap_start_local: datetime,
    settings: Mapping[str, int | float | str] | None = None,
    *,
    target_nap_duration_min: int = 90,
    wake_deadline: time = time(15, 30),
) -> bool:
    """Проверка прогноза конца сна против крайнего пробуждения (FR-35).

    True — прогноз (начало + target_nap_duration) СТРОГО позже
    wake_deadline (настройка, дефолт 15:30); ровно в 15:30 — ещё нет
    («позже 15:30», ТЗ §7.8). Чистая функция, БД не касается.
    """
    deadline_dt = datetime.combine(nap_start_local.date(), _as_hhmm(
        _setting_value(settings, "wake_deadline", wake_deadline)
    ))
    return nap_end_forecast(
        nap_start_local, settings, target_nap_duration_min=target_nap_duration_min,
    ) > deadline_dt


def nap_window(
    events: Iterable[tuple[str, datetime]],
    now_local: datetime,
    settings: Mapping[str, int | float | str] | None = None,
    *,
    day_start: time = DEFAULT_DAY_START,
    merge_threshold_min: int = 20,
    bedtime_from: time = time(19, 0),
    rise_until: time = time(12, 0),
    stretch_limit: time = time(13, 0),
    sleep_min_before_nap_min: int = 320,
    sleep_max_before_nap_min: int = 360,
    target_nap_duration_min: int = 90,
    wake_deadline: time = time(15, 30),
) -> NapWindow | None:
    """Окно дневного сна от фактического подъёма страницы (FR-26…FR-28;
    ТЗ §4.5 задача 1, §3.1, §3.6; design D16, задача 1.1).

    Окно = [подъём + минимум бодрствования; подъём + максимум
    бодрствования] (§3.1: 5 ч 20 мин … 6 ч 00 мин). Подъём — факт
    «Подъем» страницы дня по правилам PB-3 (`_page_rise`, design D16:
    переиспользование, без дублей правила). Подстройка по отклонениям
    эталона §3.6 — флагами, детерминированно:
      - stretch_to_limit (ранний подъём, напр. 07:00 → 12:20–13:00):
        конец окна не позже лимита 13:00 — совет «тянем бодрствование
        до конца окна» (A6; тексты §7.3 — волна 2.3);
      - after_deadline (поздний подъём, напр. 09:00 → 14:20–15:00):
        прогноз конца при начале в начале окна позже 15:30 — граница
        «будить не позже 15:30» (A2/A7; тексты §7.4/§7.8 — волна 2.3).

    Окно считается ОТ ПОДЪЁМА, не от момента запроса: расчёт в любой
    момент дня даёт то же окно (FR-26, сценарий «Окно после дневного
    события»). Факта «Подъем» нет — None (неполные данные, FR-34:
    окна от ненастоящего подъёма не строим). Чистая функция: БД не
    касается, событий не создаёт.

    Args:
        events: сырые пары (event_type, timestamp) — 'sleep' / 'wake',
            naive datetime в TZ пользователя, любой порядок.
        now_local: момент расчёта (определяет страницу дня и закрывает
            незакрытый sleep), naive datetime в TZ пользователя.
        settings: настройки пользователя (dict, родные типы как в
            SettingsService.get_all) или None — дефолты ТЗ §5.
        day_start: граница страницы дня (настройка day_start, дефолт
            06:00).
        merge_threshold_min: порог склейки (настройка merge_threshold,
            дефолт 20 мин, design D9).
        bedtime_from: вечерняя граница факта «Отбой» (из night_bounds,
            дефолт 19:00).
        rise_until: утренняя граница факта «Подъем» (дефолт 12:00).
        stretch_limit: лимит растяжения первого бодрствования при
            раннем подъёме (ТЗ §3.6 A6 «тянем до 13:00»).
        sleep_min_before_nap_min: дефолт минимума бодрствования, мин
            (настройка sleep_min_before_nap — 5 ч 20 мин).
        sleep_max_before_nap_min: дефолт максимума бодрствования, мин
            (настройка sleep_max_before_nap — 6 ч 00 мин).
        target_nap_duration_min: дефолт цели дневного сна, мин
            (настройка target_nap_duration — решение №1).
        wake_deadline: дефолт крайнего пробуждения (настройка
            wake_deadline — 15:30).

    Returns:
        NapWindow с флагами отклонений §3.6 или None, если подъёма
        в странице дня нет.
    """
    day_start = _as_hhmm(_setting_value(settings, "day_start", day_start))
    page_date = day_page_of(now_local, day_start)
    merged = merge_intervals(
        build_sleep_intervals(events, now_local=now_local, day_start=day_start),
        _as_minutes(_setting_value(settings, "merge_threshold", merge_threshold_min)),
    )
    # Незакрытый sleep — единый хелпер (та же логика, что
    # day_facts_lines; design D16, minor-3 review-001-1.1): последняя
    # запись — sleep без парного wake; его конец — не факт подъёма.
    unclosed = _unclosed_interval(
        sorted(events, key=lambda item: item[1]), merged,
    )
    # Вечерняя граница «Отбоя» — из night_bounds «ЧЧ:ММ-ЧЧ:ММ» (начало).
    bedtime = _bedtime_from(settings, bedtime_from)
    rise = _page_rise(
        merged,
        unclosed,
        page_date,
        datetime.combine(page_date, day_start),
        _day_page_end(page_date, day_start),
        bedtime,
        rise_until,
    )
    if rise is None:
        return None

    min_wake = timedelta(minutes=_as_minutes(
        _setting_value(settings, "sleep_min_before_nap", sleep_min_before_nap_min)
    ))
    max_wake = timedelta(minutes=_as_minutes(
        _setting_value(settings, "sleep_max_before_nap", sleep_max_before_nap_min)
    ))
    window_start = rise + min_wake
    window_end = rise + max_wake
    deadline_time = _as_hhmm(_setting_value(settings, "wake_deadline", wake_deadline))
    deadline_dt = datetime.combine(rise.date(), deadline_time)
    target_min = _as_minutes(
        _setting_value(settings, "target_nap_duration", target_nap_duration_min)
    )

    return NapWindow(
        rise=rise,
        start=window_start,
        end=window_end,
        wake_deadline=deadline_dt,
        # Прогноз конца при начале в НАЧАЛЕ окна; для фактического
        # начала сна — nap_end_forecast/nap_end_after_deadline (FR-35).
        forecast_end=window_start + timedelta(minutes=target_min),
        after_deadline=window_start + timedelta(minutes=target_min) > deadline_dt,
        # Ранний подъём §3.6 (A6): всё окно не позже лимита 13:00.
        stretch_to_limit=window_end <= datetime.combine(rise.date(), stretch_limit),
    )


# ---------------------------------------------------------------------------
# Суммарное бодрствование и окно ночного сна (add-recommendations, задача
# 1.2 + доработка ОВ-4.2 от 31.10; design D16; ТЗ §4.5 задача 2, §3.3,
# §3.4; FR-29, FR-30 в ред. ОВ-4.2; FR-40; research A3/A4).
#
# Арифметика бодрствования — ТОЛЬКО через `_bug004_total_wake` (единая
# с фактом PB-3; вторая реализация формулы BUG-004 запрещена). Якоря
# ночи — из переданных настроек (design D14: смена настройки меняет
# окна без релиза); дефолты — аргументные (= значения в ред. ОВ-4.2;
# прямой импорт DEFAULT_KEYS в calculation запрещён — соответствие
# фиксирует тест TC-REC-016).
#
# Решение ОВ-4.2 (продакт, 31.10): рекомендованное время ухода =
# max(night_target, момент достижения нижней границы ночного окна),
# нижняя граница ночного окна = 12 ч 10 мин (не 12,0 ч) — бот
# рекомендует КОНКРЕТНОЕ время ухода в сон. Якорь нижней границы —
# настройка night_wake_min (минуты; в DEFAULT_KEYS пакета 1 ключа нет,
# поэтому засеянный в БД wake_total_min = 12.0 НЕ затеняет новый
# дефолт: SettingsService.get_all возвращает только ключи DEFAULT_KEYS,
# расчёт без явного переопределения получает 12 ч 10 мин). Достижение
# нижней границы СТРОГО позже night_deadline — флаг предупреждения.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NightWindow:
    """Окно ухода в ночь: конкретная рекомендация от бодрствования
    (FR-30 в ред. ОВ-4.2).

    Attributes:
        wake_total: суммарное бодрствование на момент расчёта (единая
            формула BUG-004 с фактом PB-3 — `total_wake_at`).
        reach_lower: расчётный момент достижения НИЖНЕЙ границы ночного
            окна (настройка night_wake_min, дефолт 12 ч 10 мин —
            ОВ-4.2); граница уже достигнута — момент расчёта
            («окно сейчас»).
        reach_upper: расчётный момент достижения ВЕРХНЕЙ границы
            (wake_total_max, дефолт 12,5 ч); аналогично.
        target: якорь целевого ухода в ночь (настройка night_target,
            дефолт 22:00) в дату страницы момента расчёта.
        deadline: якорь крайнего ухода в ночь (настройка
            night_deadline, дефолт 22:30) в ту же дату.
        recommended: КОНКРЕТНОЕ рекомендованное время ухода
            (ОВ-4.2) = max(target, reach_lower): граница 12 ч 10 мин
            достигается раньше 22:00 — рекомендуем ровно 22:00 (уход
            раньше ночного якоря при набранном бодрствовании не
            нужен); позже — сам момент достижения (уход, как только
            граница набрана). Границы 22:00–22:30 — обычный случай.
        later_than_deadline: True — достижение нижней границы СТРОГО
            позже крайнего ухода (предупреждение FR-30: «ночной сон
            может сдвинуться» — текст в волне 2); ровно в night_deadline
            — ещё нет (строгое «позже», ОВ-4.2).
    """

    wake_total: timedelta
    reach_lower: datetime
    reach_upper: datetime
    target: datetime
    deadline: datetime
    recommended: datetime
    later_than_deadline: bool


def total_wake_at(
    events: Iterable[tuple[str, datetime]],
    now_local: datetime,
    settings: Mapping[str, int | float | str] | None = None,
    *,
    day_start: time = DEFAULT_DAY_START,
    merge_threshold_min: int = 20,
    bedtime_from: time = time(19, 0),
    rise_until: time = time(12, 0),
) -> timedelta:
    """Суммарное бодрствование на момент по формуле BUG-004 (FR-29).

    ТА ЖЕ арифметика, что в факте «Бодрствование общее» PB-3:
    `day_facts_lines` и эта функция считают через один хелпер
    `_bug004_total_wake` (design D16: собственная арифметика подсказок
    запрещена — исходный инцидент «суммы не сходятся»). База — факт
    «Подъем» страницы (`_page_rise`; нет — day_start страницы); конец —
    now_local; незакрытый ночной сон замораживает конец; дневной сон
    вычитается; < 0 → 0 — см. докстринг хелпера. Факт PB-3 при тех же
    входных даёт ТО ЖЕ значение (сценарий «Единая арифметика с PB-3»).

    Args:
        events: сырые пары (event_type, timestamp) — 'sleep' / 'wake',
            naive datetime в TZ пользователя, любой порядок.
        now_local: момент расчёта (закрывает незакрытый sleep), naive.
        settings: настройки пользователя (dict, родные типы как в
            SettingsService.get_all) или None — дефолты ТЗ §5.
        day_start: граница страницы дня (настройка day_start, 06:00).
        merge_threshold_min: порог склейки (настройка merge_threshold,
            20 мин, design D9).
        bedtime_from: вечерняя граница факта «Отбой» (из night_bounds).
        rise_until: утренняя граница факта «Подъем».

    Returns:
        суммарное бодрствование на момент now_local (timedelta ≥ 0).
    """
    day_start = _as_hhmm(_setting_value(settings, "day_start", day_start))
    page_date = day_page_of(now_local, day_start)
    window_start = datetime.combine(page_date, day_start)
    window_end = _day_page_end(page_date, day_start)
    ordered = sorted(events, key=lambda item: item[1])
    merged = merge_intervals(
        build_sleep_intervals(events, now_local=now_local, day_start=day_start),
        _as_minutes(_setting_value(settings, "merge_threshold", merge_threshold_min)),
    )
    unclosed = _unclosed_interval(ordered, merged)
    rise = _page_rise(
        merged,
        unclosed,
        page_date,
        window_start,
        window_end,
        _bedtime_from(settings, bedtime_from),
        rise_until,
    )
    return _bug004_total_wake(
        merged, unclosed, rise, now_local, window_start, window_end,
    )


def night_window(
    events: Iterable[tuple[str, datetime]],
    now_local: datetime,
    settings: Mapping[str, int | float | str] | None = None,
    *,
    day_start: time = DEFAULT_DAY_START,
    merge_threshold_min: int = 20,
    bedtime_from: time = time(19, 0),
    rise_until: time = time(12, 0),
    night_wake_min_min: int = 730,
    wake_total_max_h: float = 12.5,
    night_target: time = time(22, 0),
    night_deadline: time = time(22, 30),
) -> NightWindow:
    """Конкретная рекомендация ухода в ночь от бодрствования (FR-30 в
    ред. ОВ-4.2; ТЗ §4.5 задача 2, §3.3, §3.4; design D16; research
    A3/A4).

    Сравнение `total_wake_at` (единая формула BUG-004) с границами
    ночного окна: нижняя — 12 ч 10 мин (ОВ-4.2, настройка
    night_wake_min), верхняя — 12,5 ч (wake_total_max). Расчётный
    момент достижения границы — проекция «бодрствование накапливается
    1:1, будущих событий нет» (как прогноз конца дневного сна —
    уточняется при следующем событии; корректна при бодрствовании на
    момент расчёта). РЕКОМЕНДОВАННОЕ время ухода (решение продакта
    ОВ-4.2, 31.10: «конкретное время ухода в сон… бодрствование
    не меньше 12 ч 10 мин») = max(night_target, reach_lower):
      - 12 ч 10 мин набираются РАНЬШЕ 22:00 → рекомендуем ровно
        night_target (22:00) — раньше уходить при набранном
        бодрствовании не нужно;
      - в границах 22:00–22:30 → сам момент достижения;
      - достижение СТРОГО позже night_deadline (22:30) → та же
        рекомендация + флаг later_than_deadline (предупреждение FR-30
        «ночной сон может сдвинуться»; текст — волна 2). Ровно в
        22:30 — рекомендации 22:30 БЕЗ флага (строгое «позже»).
    Позиция к диапазону (ниже/в/выше — §4.6) различается моментами
    reach_lower/reach_upper против now_local.

    Args:
        events: сырые пары (event_type, timestamp), naive, любой порядок.
        now_local: момент расчёта, naive datetime в TZ пользователя.
        settings: настройки пользователя (dict) или None — дефолты
            (в ред. ОВ-4.2; ключ night_wake_min можно переопределить
            в dict, в DEFAULT_KEYS его нет — засеянный wake_total_min
            = 12.0 нижнюю границу рекомендации НЕ меняет).
        day_start: граница страницы дня (настройка day_start, 06:00).
        merge_threshold_min: порог склейки (настройка merge_threshold).
        bedtime_from: вечерняя граница факта «Отбой» (из night_bounds).
        rise_until: утренняя граница факта «Подъем».
        night_wake_min_min: дефолт нижней границы ночного окна, мин
            (настройка night_wake_min — 12 ч 10 мин, ОВ-4.2).
        wake_total_max_h: дефолт верхней границы бодрствования, ч
            (настройка wake_total_max — 12,5).
        night_target: дефолт целевого ухода в ночь (настройка
            night_target — 22:00).
        night_deadline: дефолт крайнего ухода в ночь (настройка
            night_deadline — 22:30).

    Returns:
        NightWindow с рекомендацией `recommended` для страницы дня
        момента расчёта.
    """
    wake_total = total_wake_at(
        events,
        now_local,
        settings,
        day_start=day_start,
        merge_threshold_min=merge_threshold_min,
        bedtime_from=bedtime_from,
        rise_until=rise_until,
    )
    # Нижняя граница НОЧНОГО окна — 12 ч 10 мин (ОВ-4.2): якорь
    # night_wake_min в settings-dict, дефолт — аргументный (не литерал
    # в теле); ключ в DEFAULT_KEYS отсутствует, поэтому seeded
    # wake_total_min = 12.0 не затеняет решение продакта.
    min_wake = timedelta(minutes=_as_minutes(
        _setting_value(settings, "night_wake_min", night_wake_min_min)
    ))
    max_wake = timedelta(hours=_as_hours(
        _setting_value(settings, "wake_total_max", wake_total_max_h)
    ))
    # Граница уже достигнута — «окно сейчас»: проекция не уходит в
    # прошлое, моментом достижения считается момент расчёта.
    reach_lower = now_local + max(timedelta(0), min_wake - wake_total)
    reach_upper = now_local + max(timedelta(0), max_wake - wake_total)
    page_date = day_page_of(
        now_local, _as_hhmm(_setting_value(settings, "day_start", day_start))
    )
    target = datetime.combine(
        page_date, _as_hhmm(_setting_value(settings, "night_target", night_target))
    )
    deadline = datetime.combine(
        page_date,
        _as_hhmm(_setting_value(settings, "night_deadline", night_deadline)),
    )
    # ОВ-4.2: рекомендация = max(22:00, момент достижения 12 ч 10 мин);
    # строго позже 22:30 — предупреждение.
    recommended = max(target, reach_lower)
    return NightWindow(
        wake_total=wake_total,
        reach_lower=reach_lower,
        reach_upper=reach_upper,
        target=target,
        deadline=deadline,
        recommended=recommended,
        later_than_deadline=reach_lower > deadline,
    )


# ---------------------------------------------------------------------------
# Конфликты якорей — проверка реалистичности и честный компромисс
# (add-recommendations, задача 1.3; design D18; ТЗ §4.5 задача 3,
# §3.6; FR-31; research A5–A7).
#
# Проверки реалистичности (детерминированные, вход — те же сырые
# события + настройки + момент расчёта):
#   1) подъём + окно дневного сна → прогноз конца сна vs 15:30
#      (nap_window.after_deadline);
#   2) конец сна + остаток вечернего бодрствования → момент
#      достижения нижней границы ночного окна (12 ч 10 мин, ОВ-4.2)
#      vs крайний уход 22:30 (night_window.later_than_deadline).
# Правило компромисса ФИКСИРОВАНО (design D18): приоритет якорей
# НОЧЬ > 15:30 > дневное окно — ночной сон главный якорь режима,
# поэтому сдвигается всегда якорь НИЗШЕГО приоритета из конфликтующих
# (дневное окно). Одинаковый вход даёт одинаковый результат.
# ---------------------------------------------------------------------------


#: Имена якорей в отчёте о конфликте (стабильные, для текстов волны 2.3).
ANCHOR_NIGHT = "ночь"
ANCHOR_WAKE_DEADLINE = "15:30"
ANCHOR_NAP_WINDOW = "дневное окно"


@dataclass(frozen=True)
class AnchorConflict:
    """Один конфликт якорей с детерминированным компромиссом (FR-31).

    Attributes:
        conflicting: имена противоречащих якорей (первым — якорь
            более высокого приоритета, который остаётся).
        shifted: какой якорь сдвигается (низший приоритет из
            конфликтующих — по D18 всегда «дневное окно»).
        accepted: какое окно принято компромиссом (готовая
            формулировка с якорями-временами).
        latest_nap_end: предложение компромисса — крайнее пробуждение
            от дневного сна, при котором принятый якорь выполняется
            (проекция «будущих снов нет»); компромисс уже неосуществим
            (предложение в прошлом) — None.
    """

    conflicting: tuple[str, ...]
    shifted: str
    accepted: str
    latest_nap_end: datetime | None


def anchor_conflicts(
    events: Iterable[tuple[str, datetime]],
    now_local: datetime,
    settings: Mapping[str, int | float | str] | None = None,
    *,
    day_start: time = DEFAULT_DAY_START,
    merge_threshold_min: int = 20,
    bedtime_from: time = time(19, 0),
    rise_until: time = time(12, 0),
    stretch_limit: time = time(13, 0),
    sleep_min_before_nap_min: int = 320,
    sleep_max_before_nap_min: int = 360,
    target_nap_duration_min: int = 90,
    wake_deadline: time = time(15, 30),
    night_wake_min_min: int = 730,
    wake_total_max_h: float = 12.5,
    night_target: time = time(22, 0),
    night_deadline: time = time(22, 30),
) -> list[AnchorConflict]:
    """Проверка реалистичности якорей и детерминированный компромисс
    (FR-31; ТЗ §4.5 задача 3; design D18).

    Чистая функция НАД теми же расчётами окон (`nap_window`,
    `night_window` — без копий их арифметики). Возвращает СПИСОК
    конфликтов: какие якоря противоречат, какой сдвигается, какое
    окно принято. Пустой список — якоря совместимы, противоречивые
    окна не выводятся (FR-31). Компромисс детерминирован фиксированным
    приоритетом D18 — НОЧЬ > 15:30 > дневное окно: жертвой всегда
    становится якорь низшего приоритета («дневное окно»):
      - прогноз конца дневного сна позже 15:30 → сдвигается дневное
        окно: принято «будить не позже 15:30» (допустимое начало сна
        [окно.start; 15:30 − target]; окно целиком позже — несовместимо,
        дневной сон сокращается/пропускается);
      - момент достижения 12 ч 10 мин позже 22:30 (ОВ-4.2) → сдвигается
        дневное окно: ночь сохраняется — принят уход
        [night_target; night_deadline], предложение — будить от
        дневного сна не позже deadline − (12 ч 10 мин − бодрствование);
        предложение в прошлом (время упущено) — None с предупреждением
        о сдвиге ночи (тот же флаг, что FR-30).

    Args:
        events: сырые пары (event_type, timestamp), naive, любой порядок.
        now_local: момент расчёта, naive datetime в TZ пользователя.
        settings: настройки пользователя (dict) или None — дефолты
            (те же якоря, что у nap_window/night_window).
        Остальные keyword-аргументы — дефолты якорей, зеркалят
            nap_window / night_window (см. их докстринги).

    Returns:
        list[AnchorConflict] в фиксированном порядке: сначала конфликт
        с якорем 15:30, затем с якорем ночи; пустой список — конфликтов
        нет.
    """
    nap = nap_window(
        events, now_local, settings,
        day_start=day_start,
        merge_threshold_min=merge_threshold_min,
        bedtime_from=bedtime_from,
        rise_until=rise_until,
        stretch_limit=stretch_limit,
        sleep_min_before_nap_min=sleep_min_before_nap_min,
        sleep_max_before_nap_min=sleep_max_before_nap_min,
        target_nap_duration_min=target_nap_duration_min,
        wake_deadline=wake_deadline,
    )
    night = night_window(
        events, now_local, settings,
        day_start=day_start,
        merge_threshold_min=merge_threshold_min,
        bedtime_from=bedtime_from,
        rise_until=rise_until,
        night_wake_min_min=night_wake_min_min,
        wake_total_max_h=wake_total_max_h,
        night_target=night_target,
        night_deadline=night_deadline,
    )

    conflicts: list[AnchorConflict] = []

    # Конфликт 1: подъём + окно → конец сна позже 15:30 (приоритет
    # 15:30 выше дневного окна — окно сдвигается/жертвуется).
    if nap is not None and nap.after_deadline:
        target_nap = nap.forecast_end - nap.start
        latest_start = nap.wake_deadline - target_nap
        if latest_start >= nap.start:
            accepted = (
                f"уход в дневной сон {nap.start:%H:%M}–{latest_start:%H:%M}, "
                f"будить не позже {nap.wake_deadline:%H:%M}"
            )
        else:
            accepted = (
                f"дневное окно несовместимо с 15:30: сон начат позже "
                f"{latest_start:%H:%M} — будить не позже "
                f"{nap.wake_deadline:%H:%M}, дневной сон сокращается"
            )
        conflicts.append(AnchorConflict(
            conflicting=(ANCHOR_WAKE_DEADLINE, ANCHOR_NAP_WINDOW),
            shifted=ANCHOR_NAP_WINDOW,
            accepted=accepted,
            latest_nap_end=nap.wake_deadline,
        ))

    # Конфликт 2: конец сна + остаток вечернего бодрствования →
    # достижение 12 ч 10 мин позже 22:30 (приоритет ночи выше всех —
    # ночь сохраняется, дневное окно сдвигается).
    if night.later_than_deadline:
        night_min = timedelta(minutes=_as_minutes(
            _setting_value(settings, "night_wake_min", night_wake_min_min)
        ))
        latest_nap_end = night.deadline - (night_min - night.wake_total)
        feasible = latest_nap_end >= now_local
        accepted = (
            f"уход в ночь {night.target:%H:%M}–{night.deadline:%H:%M} "
            f"(ночь — главный якорь), "
            + (
                f"будить от дневного сна не позже {latest_nap_end:%H:%M}"
                if feasible
                else (
                    f"компенсация уже невозможна — предупреждение о сдвиге "
                    f"ночного сна, уход не позже {night.deadline:%H:%M}"
                )
            )
        )
        conflicts.append(AnchorConflict(
            conflicting=(ANCHOR_NIGHT, ANCHOR_NAP_WINDOW),
            shifted=ANCHOR_NAP_WINDOW,
            accepted=accepted,
            latest_nap_end=latest_nap_end if feasible else None,
        ))

    return conflicts
