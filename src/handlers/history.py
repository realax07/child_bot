# Экран «История за неделю» (change-пакет add-settings-history, задача 2.1;
# ТЗ §4.8, §7.20–7.22, §10.2–10.3; design D22).
#
# ТЗ §10.2/§10.3: двойной вход — команда /history и callback «history:week»
# (кнопка «📊 История за неделю», ТЗ §6 — в обоих состояниях меню и в
# постоянной reply-панели). Ответ — §7.20: строка на каждый из 7 ЗАВЕРШЁННЫХ
# страниц дня (граница day_start, design D8; новые → старые) —
# «бодрствование …, сон …, ночь … ✓/⚠️» — плюс блок за сегодня.
#
# Арифметика — ТОЛЬКО существующие расчёты src/services/calculation (design
# D22: «своей арифметики в истории не»; spec weekly-history FR-48 — та же
# арифметика, что факты PB-3 и подсказки): факт «Подъем» — `_page_rise`,
# бодрствование — `_bug004_total_wake` (единая формула BUG-004, design D16),
# страницы/склейка — day_page_of/build_sleep_intervals/merge_intervals.
# Импорт приватных имён сознателен (прецедент settings_screen.py, задача
# 1.2): src/services вне зоны задачи 2.1, публиковать новый API санкции не
# давали; САНКЦИЯ ПМ (2.1): данные дневных событий читаются напрямую через
# существующие сервисы/репозитории, НОВЫХ SQL в хендлере нет — чистая
# агрегация `weekly_history` из calculation.py (D22/tasks.md) здесь НЕ
# заводится, т.к. calculation.py вне ЖЁСТКОЙ зоны задачи 2.1.
#
# Правила экрана (ТЗ §7.20–7.22, spec weekly-history FR-48…FR-50):
#   - строка дня: бодрствование (BUG-004, база «Подъем»), дневной сон
#     (интервалы после склейки D9), ночной уход («ночь HH:MM» = отбой —
#     первый ночной отрезок страницы, начавшийся не раньше вечерней
#     границы night_bounds; строки нет, если факта нет — принцип PB-3);
#     пустой день — строки нет (FR-50: «только фактические дни»);
#   - отметка (FR-49): ✓ — бодрствование в [wake_total_min; wake_total_max]
#     И уход ≤ night_deadline; ⚠️ — хотя бы одна граница нарушена; дня без
#     бодрствования/ухода отметка не касается (без вымышленных цифр);
#   - бодрствование завершённой страницы считается ДО отбоя (после ухода
#     в ночь ребёнок не бодрствует — то же правило заморозки, что у
#     незакрытого ночного сна в `_bug004_total_wake`; для страницы без
#     отбоя — до конца окна страницы, как в фактах PB-3 при
#     now_local=None), для сегодня — до момента расчёта;
#   - сегодня: событий нет → §7.21 (без «0 ч»); события есть, но подъёма
#     нет или последнее событие — пробуждение дольше wake_total_max назад
#     (дневные отметки пропущены — данные неполные/устаревшие) → §7.22 со
#     временем последнего события; иначе — строка дня за сегодня.
#
# Кнопка/команда/колбэк НЕ пишут в sleep_events (события не создают,
# state-ui spec) и не меняют состояние ребёнка; наблюдателю история
# доступна (design D13 — чтение; видит только СВОИ данные — выборка по
# своему user_id, FR-47).
#
# design D4: события хранятся в UTC; расчёт — в локальном наивном времени
# пользователя (users.timezone через zoneinfo, resolve_tz).
#
# Слой handlers (design D1): только сервисы (services.event,
# services.settings, services.calculation), без repositories и SQL.

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple

from telegram import Update
from telegram.ext import ContextTypes

from src import texts
from src.handlers import common
from src.services.backfill_parser import resolve_tz
from src.services.calculation import (
    _as_hhmm,
    _as_hours,
    _as_minutes,
    _bedtime_from,
    _bug004_total_wake,
    _day_page_end,
    _page_rise,
    _unclosed_interval,
    build_sleep_intervals,
    classify_sleep,
    day_page_of,
    merge_intervals,
)
from src.services.event import get_user, list_raw_events
from src.services.settings import SettingsService
from src.services.state import CALLBACK_HISTORY

# callback_data кнопки (ТЗ §10.3) — единый источник src/services/state.py
# (design D14); REGEXP регистрации — оттуда же.
CALLBACK_PATTERN = r"^history:week$"

# Fallback пояса — как в events.py/hint.py (design D4, аудитория бота).
_FALLBACK_TZ = "Europe/Moscow"

# Утренняя граница факта «Подъем» (та же, что в фактах PB-3 day_facts_lines).
_RISE_UNTIL = time(12, 0)

# Короткие имена дней недели в строках §7.20 («08.10 (вт)»).
_WEEKDAYS_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _local_naive(utc_moment: str | datetime, tz_name: str | None) -> datetime:
    """UTC-момент (строка БД «YYYY-MM-DD HH:MM:SS» или datetime) →
    naive datetime в поясе пользователя (design D4) — как в events.py/hint.py."""
    if isinstance(utc_moment, str):
        utc_moment = datetime.strptime(
            utc_moment, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    return utc_moment.astimezone(resolve_tz(tz_name or "")).replace(tzinfo=None)


def _date_line(page: date) -> str:
    """«08.10 (вт)» — дата страницы и день недели для строки §7.20."""
    return f"{page:%d.%m} ({_WEEKDAYS_RU[page.weekday()]})"


# ----------------------------------------------------- Факты страницы дня


class _PageFacts(NamedTuple):
    """Факты страницы дня той же арифметикой, что PB-3 (design D22).

    rise — факт «Подъем» (`_page_rise`); total_wake — бодрствование
    («Бодрствование общее» BUG-004); naps — дневные отрезки страницы
    после склейки; bedtime — отбой (первый ночной отрезок страницы,
    начавшийся не позже вечерней границы night_bounds) или None.
    """

    rise: datetime | None
    total_wake: timedelta
    naps: tuple  # tuple[SleepInterval, ...]
    bedtime: datetime | None
    unclosed: object | None  # незакрытый SleepInterval (его конец — не факт)


def _page_facts(
    events: list[tuple[str, datetime]],
    page_date: date,
    day_start: time,
    merge_threshold_min: int,
    bedtime_from: time,
    now_local: datetime | None,
) -> _PageFacts:
    """Факты страницы дня — переиспользование расчётов calculation (D22).

    ЕДИНСТВЕННОЕ дополнение к формуле BUG-004 (сам formula-хелпер не
    тронут, calculation.py вне зоны 2.1): для страницы с фактом отбоя
    конец бодрствования замораживается на начале ночного отрезка —
    после ухода в ночь ребёнок не бодрствует (то же правило, что у
    незакрытого ночного сна в `_bug004_total_wake`; у фактов PB-3 оно
    не срабатывает, потому что «сегодняшняя» ночь ещё не началась).
    Без отбоя — конец по now_local (сегодня) или по концу окна страницы
    (завершённый день), ровно как в day_facts_lines.
    """
    ordered = sorted(events, key=lambda item: item[1])
    merged = merge_intervals(
        build_sleep_intervals(events, now_local=now_local, day_start=day_start),
        merge_threshold_min,
    )
    unclosed = _unclosed_interval(ordered, merged)
    window_start = datetime.combine(page_date, day_start)
    window_end = _day_page_end(page_date, day_start)

    rise = _page_rise(
        merged, unclosed, page_date, window_start, window_end,
        bedtime_from, _RISE_UNTIL,
    )

    in_page = [
        interval
        for interval in merged
        if window_start <= interval.start_local < window_end
    ]
    night_in_page = [
        interval
        for interval in in_page
        if classify_sleep(interval.start_local, interval.end_local).is_night
    ]
    day_naps = tuple(
        interval
        for interval in in_page
        if not classify_sleep(interval.start_local, interval.end_local).is_night
    )
    # Отбой — как в day_facts_lines: ПЕРВЫЙ ночной отрезок страницы,
    # начавшийся в вечернем окне (не раньше bedtime_from); строки нет,
    # если факта нет (принцип PB-3).
    bedtime = next(
        (
            interval.start_local
            for interval in night_in_page
            if interval.start_local.time() >= bedtime_from
        ),
        None,
    )

    # Конец бодрствования: отбор замораживает (см. докстринг), иначе —
    # единая формула BUG-004 с тем же концом, что у фактов дня.
    wake_end = bedtime if bedtime is not None else now_local
    total_wake = _bug004_total_wake(
        merged, unclosed, rise, wake_end, window_start, window_end,
    )
    return _PageFacts(
        rise=rise, total_wake=total_wake, naps=day_naps, bedtime=bedtime,
        unclosed=unclosed,
    )


def _day_mark(
    facts: _PageFacts,
    wake_min: timedelta,
    wake_max: timedelta,
    night_deadline: time,
    in_progress: bool = False,
) -> str:
    """Отметка ✓/⚠️ по границам пользователя (FR-49); '' — данных мало.

    ✓ — бодрствование в [нижняя; верхняя] И уход ≤ крайнего; ⚠️ — хотя
    бы одна граница нарушена (значение бодрствования — то же, что
    «Бодрствование общее» фактов PB-3, включая базу day_start при
    отсутствии «Подъема»); без факта ухода при бодрствовании в
    границах отметка не ставится (проверять нечего). Сегодняшняя
    страница (in_progress) БЕЗ факта ухода не оценивается: день не
    завершён, «итога» ещё нет (нижняя граница суток к середине дня —
    не нарушение).
    """
    if in_progress and facts.bedtime is None:
        return ""
    wake_bad = not (wake_min <= facts.total_wake <= wake_max)
    bed_bad = facts.bedtime is not None and facts.bedtime.time() > night_deadline
    if facts.bedtime is not None:
        return " ⚠️" if (wake_bad or bed_bad) else " ✓"
    return " ⚠️" if wake_bad else ""


def _day_line_text(
    facts: _PageFacts,
    page_date: date,
    wake_min: timedelta,
    wake_max: timedelta,
    night_deadline: time,
    in_progress: bool = False,
) -> str | None:
    """Строка дня §7.20; None — день без фактов (строки нет, FR-50)."""
    if facts.rise is None and not facts.naps and facts.bedtime is None:
        return None  # пустой день — без цифр и без отметки (FR-49)
    parts = [f"бодрствование {texts.format_duration(facts.total_wake)}"]
    # «сон» — по ЗАВЕРШЁННЫМ дневным отрезкам (как «Заснул/Проснулся» в
    # фактах PB-3: конец незакрытого отрезка — не факт).
    closed_naps = [
        interval for interval in facts.naps if interval is not facts.unclosed
    ]
    if closed_naps:
        parts.append(
            "сон "
            + ", ".join(
                f"{interval.start_local:%H:%M}–{interval.end_local:%H:%M}"
                for interval in closed_naps
            )
        )
    if facts.bedtime is not None:
        parts.append(f"ночь {facts.bedtime:%H:%M}")
    return texts.history_day_line(
        _date_line(page_date),
        parts,
        _day_mark(facts, wake_min, wake_max, night_deadline, in_progress),
    )


# -------------------------------------------------------------- Экран §7.20


def week_screen_text(
    events: list[tuple[str, datetime]],
    settings_map: Mapping[str, int | float | str],
    now_local: datetime,
) -> str:
    """Ответ «📊 История за неделю» (§7.20 + блок сегодня, FR-48…FR-50).

    Чистая функция над сырыми событиями (локальные naive), настройками
    (dict SettingsService.get_all) и моментом расчёта: 7 завершённых
    страниц дня (новые → старые) + блок за сегодня (§7.21/§7.22/строка
    дня). Арифметика — только calculation (design D22).
    """
    day_start = _as_hhmm(settings_map["day_start"])
    merge_threshold_min = _as_minutes(settings_map["merge_threshold"])
    bedtime_from = _bedtime_from(settings_map, time(19, 0))
    night_deadline = _as_hhmm(settings_map["night_deadline"])
    wake_min = timedelta(hours=_as_hours(settings_map["wake_total_min"]))
    wake_max = timedelta(hours=_as_hours(settings_map["wake_total_max"]))

    today_page = day_page_of(now_local, day_start)

    week_lines: list[str] = []
    for offset in range(1, 8):  # новые → старые: вчера, позавчера, …
        page = today_page - timedelta(days=offset)
        facts = _page_facts(
            events, page, day_start, merge_threshold_min, bedtime_from,
            now_local=None,  # страница завершена — конец окна страницы
        )
        line = _day_line_text(facts, page, wake_min, wake_max, night_deadline)
        if line is not None:
            week_lines.append(line)

    today_block = _today_block(
        events, today_page, day_start, merge_threshold_min, bedtime_from,
        now_local, wake_min, wake_max, night_deadline,
    )

    parts = [texts.HISTORY_WEEK_HEADER]
    if week_lines:
        parts.append("")
        parts.extend(week_lines)
    parts.append("")
    parts.append(today_block)
    return "\n".join(parts)


def _today_block(
    events: list[tuple[str, datetime]],
    today_page: date,
    day_start: time,
    merge_threshold_min: int,
    bedtime_from: time,
    now_local: datetime,
    wake_min: timedelta,
    wake_max: timedelta,
    night_deadline: time,
) -> str:
    """Блок «сегодня» (FR-50): §7.21 — событий нет; §7.22 — данные
    неполные/устаревшие; иначе строка дня за сегодня."""
    window_start = datetime.combine(today_page, day_start)
    window_end = _day_page_end(today_page, day_start)
    today_events = [
        (kind, moment)
        for kind, moment in events
        if window_start <= moment < window_end
    ]
    if not today_events:
        return texts.HISTORY_EMPTY_TODAY  # §7.21, без «0 ч»

    facts = _page_facts(
        events, today_page, day_start, merge_threshold_min, bedtime_from,
        now_local=now_local,
    )

    last_moment = max(moment for _, moment in today_events)
    rise_missing = facts.rise is None
    stale_wake = (
        not rise_missing
        and facts.bedtime is None
        and now_local - last_moment > wake_max
    )
    if rise_missing or stale_wake:
        # §7.22 со временем последнего события (сценарий spec: «07:15
        # и после — ничего»).
        return texts.history_stale_text(f"{last_moment:%H:%M}")

    return _day_line_text(
        facts, today_page, wake_min, wake_max, night_deadline,
        in_progress=True,
    ) or texts.HISTORY_EMPTY_TODAY


# --------------------------------------------------------------- Обработчики


def _screen_text(conn: sqlite3.Connection, user: dict) -> str:
    """Свежий экран §7.20 из БД — общий вход всех трёх маршрутов (§10.3).

    События и настройки перечитываются при каждом вызове (без кэша,
    FR-24/D5); выборка — только СВОИ события (user_id владельца, D13).
    """
    tz_name = user["timezone"]
    now_local = _local_naive(datetime.now(timezone.utc), tz_name)
    events = [
        (row["event_type"], _local_naive(row["timestamp"], tz_name))
        for row in list_raw_events(conn, user["id"])
    ]
    settings_map = SettingsService(conn).get_all(user["id"])
    return week_screen_text(events, settings_map, now_local)


async def _reply_screen(message, conn: sqlite3.Connection, user: dict) -> None:
    """Ответ экраном §7.20; ничего не пишет и состояние не меняет."""
    await message.reply_text(_screen_text(conn, user))


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /history (ТЗ §10.2) → экран §7.20.

    Событий не создаёт, состояние ребёнка не меняет, FSM-фаз не трогает
    (история — чтение, ввод текста не ждёт).
    """
    if update.effective_message is None or update.effective_user is None:
        return
    conn: sqlite3.Connection = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
        await _reply_screen(update.effective_message, conn, user)
    finally:
        conn.close()


async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Колбэк history:week (ТЗ §10.3, кнопка «📊 История за неделю»).

    Тот же экран, что /history и текст reply-панели (двойной вход,
    одна логика). Событие НЕ создаётся; состояние перечитывается из БД
    (design D5); observer'у доступна (design D13 — чтение).
    """
    query = update.callback_query
    if query is None or (query.data or "") != CALLBACK_HISTORY:
        return
    if update.effective_user is None:
        return

    conn: sqlite3.Connection = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
        await query.answer()
        if update.effective_message is not None:
            await _reply_screen(update.effective_message, conn, user)
    finally:
        conn.close()


async def reply_history_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    user: dict,
) -> None:
    """Текст «📊 История за неделю» с reply-панели (ТЗ §6; маршрут —
    common.py). Тот же экран, что /history и callback history:week
    (ТЗ §10.3 — двойной вход). Соединение открыл вызывающий
    (reply_button_message) — сигнатура как у reply_settings_button.
    """
    if update.effective_message is None:
        return
    await _reply_screen(update.effective_message, conn, user)
