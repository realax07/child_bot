# Обработчики событийных кнопок «😴 Заснул» / «☀️ Проснулся»
# (change-пакет add-core-diary, задача 2.3).
#
# design D2: callback-схема ТЗ §10.3 — event:sleep / event:wake открывают
# выбор времени §7.10 (кнопки time:now / time:manual); time:now создаёт
# событие «сейчас», time:manual передаёт дальше диалогу заднего числа
# (полная ветка диалога — задача 4.1, design D10).
# design D5 (guard): inline-клавиатуры из истории чата остаются нажимаемыми,
# поэтому перед созданием события состояние перепроверяется по БД
# (state.is_stale_callback); устаревшее нажатие игнорируется — событие НЕ
# создаётся, answerCallbackQuery + перерисовка актуальной клавиатуры
# (common.redraw_menu). Состояние читается заново и на event:*, и на time:*
# (между нажатиями оно могло измениться).
# design D6: sleep_type при записи не вычисляется (create_event пишет NULL).
# design D13: роль observer — нажатие игнорируется с вежливым сообщением,
# событие не создаётся.
# FR-7 (в ред. волны 6, PB-3): sleep «сейчас» → §7.5: подтверждающая строка
# + сводка событий дня (calculation.day_facts_lines — единая точка сборки);
# строка «Крайнее пробуждение: …» НЕ выводится вовсе (решение продакта
# 29.10: норма постоянна; wake_deadline остаётся в настройках — Пакет 3 —
# и в подсказках Пакета 2, из расчётов не удалён).
# FR-8 (в ред. PB-3 + FR-38, задача 2.3): wake «сейчас» → §7.2:
# подтверждающая строка + строка окна дневного сна от фактического
# подъёма (calculation.nap_window, design D16/D20; §7.3/§7.4-стиль при
# раннем/позднем подъёме — якоря из настроек) + сводка событий дня;
# подавление окон волны 6 снято.
# design D4: время хранится в UTC; в текстах ответов — локальное время
# пользователя (users.timezone через zoneinfo).
#
# Слой handlers (design D1): только сервисы (services.event, services.state,
# services.settings), без repositories и прямого SQL.

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src import texts
from src.handlers import common
from src.services import access
from src.services.backfill_parser import resolve_tz
from src.services.calculation import (
    DEFAULT_DAY_START,
    day_facts_lines,
    day_page_of,
    nap_window,
)
from src.services.event import create_event, get_user, list_raw_events
from src.services.settings import SettingsService
from src.services.state import KIND_SLEEP, KIND_WAKE, is_stale_callback

# callback_data кнопок §7.10 (design D2, ТЗ §10.3).
CALLBACK_TIME_NOW = "time:now"
CALLBACK_TIME_MANUAL = "time:manual"

# Фазы диалога в context.user_data (design D10: фазы диалога — не в БД):
# какая событийная кнопка ожидает выбора времени.
PENDING_KIND_KEY = "pending_event_kind"
# Задача 4.1 (FR-15): ветка «⏪ Ввести время вручную» — бот ждёт текстовый
# ввод времени (§7.11); фаза читается обработчиком текста (backfill.py).
WAIT_EVENT_TIME_KEY = "wait_event_time"

# Сообщение наблюдателю — единый текст texts.OBSERVER_DENIED через
# services.access.deny_message() (задача 5.1, design D13; review-001-2.4,
# note 1: дубль литерала убран). Имя оставлено как алиас для обратной
# совместимости импортов (handlers/backfill.py, тесты).
OBSERVER_DENIED = texts.OBSERVER_DENIED

# Fallback пояса — как в /start (design D4, аудитория бота).
_FALLBACK_TZ = "Europe/Moscow"


# -------------------------------------------------------------- Тексты FR-7/8

def fell_asleep_text(local_hhmm: str, summary_lines: Iterable[str]) -> str:
    """§7.5 с фактическим временем засыпания (FR-7 в ред. волны 6, PB-3).

    Строка «Крайнее пробуждение: ЧЧ:ММ» НЕ выводится вовсе (решение
    продакта 29.10). К подтверждению добавляется сводка событий дня —
    строки строит calculation.day_facts_lines (единая точка, PB-3).
    """
    return (
        f"✅ Засыпание записано в {local_hhmm}.\n"
        "Сон идёт.\n"
        + "\n".join(summary_lines)
    )


def woke_up_text(
    local_hhmm: str,
    summary_lines: Iterable[str],
    window_line: str | None = None,
) -> str:
    """§7.2 (FR-8 в ред. PB-3 + FR-38, задача 2.3 / design D20):
    подтверждающая строка + строка окна дневного сна + сводка событий дня.

    Подавление окон волны 6 СНЯТО: окно считается ОТ ФАКТИЧЕСКОГО
    подъёма страницы (calculation.nap_window, design D16 — не хардкод),
    стандартный/ранний/поздний подъём — §7.2/§7.3/§7.4-стиль; якоря
    раннего/позднего текста — из настроек (design D14). Сводка PB-3 —
    БЕЗ изменений (факты дня + «Бодрствование общее» BUG-004, строки
    строит calculation.day_facts_lines — единая точка). Окна нет (факта
    «Подъем» нет — окно от ненастоящего подъёма не строим, FR-34-стиль)
    — ответ как в волне 6.
    """
    lines = [f"✅ Пробуждение записано в {local_hhmm}."]
    if window_line is not None:
        lines.append(window_line)
    lines.extend(summary_lines)
    return "\n".join(lines)


# ------------------------------------------------- Сводка событий дня (PB-3)

#: Лимит растяжения первого бодрствования при раннем подъёме (§3.6,
#: «тянем до 13:00») — ТОТ ЖЕ аргументный дефолт, что у
#: calculation.nap_window (ключа-настройки нет); передаётся в расчёт
#: явно и подставляется в текст §7.3 — единый источник значения.
NAP_STRETCH_LIMIT = time(13, 0)


def _local_naive(utc_moment: str | datetime, tz_name: str | None) -> datetime:
    """UTC-момент (строка БД «YYYY-MM-DD HH:MM:SS» или datetime) →
    naive datetime в поясе пользователя (design D4)."""
    if isinstance(utc_moment, str):
        utc_moment = datetime.strptime(
            utc_moment, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    return utc_moment.astimezone(resolve_tz(tz_name or "")).replace(tzinfo=None)


def _settings_day_start(raw: str) -> time:
    """«ЧЧ:ММ» настройки day_start → time; фолбэк — дефолт расчётов (D8)."""
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except (AttributeError, ValueError):
        return DEFAULT_DAY_START


def day_summary_lines(
    conn: sqlite3.Connection, user: dict, ref_utc: str | datetime
) -> list[str]:
    """Сводка событий дня (PB-3) по странице дня момента ref_utc.

    Единая точка сборки для всех трёх подтверждений (§7.5/§7.2 «Сейчас»
    и §7.14 задним числом): сырые события — через сервис (design D1),
    day_start / merge_threshold — из settings при каждом расчёте
    (FR-24, design D14), строки — чистая calculation.day_facts_lines
    (без дублирования форматирования по хендлерам).
    """
    user_id = user["id"]
    tz_name = user["timezone"]
    settings = SettingsService(conn)
    day_start = _settings_day_start(str(settings.get(user_id, "day_start")))
    threshold = int(settings.get(user_id, "merge_threshold"))

    raw_events = [
        (row["event_type"], _local_naive(row["timestamp"], tz_name))
        for row in list_raw_events(conn, user_id)
    ]
    ref_local = _local_naive(ref_utc, tz_name)
    page = day_page_of(ref_local, day_start)
    now_local = datetime.now(resolve_tz(tz_name or "")).replace(tzinfo=None)
    return day_facts_lines(
        raw_events,
        page,
        day_start=day_start,
        now_local=now_local,
        merge_threshold_min=threshold,
    )


def nap_window_text(
    conn: sqlite3.Connection, user: dict, ref_utc: str | datetime
) -> str | None:
    """Строка окна дневного сна для подтверждений (FR-38, задача 2.3,
    design D20) или None — факта «Подъем» страницы нет.

    Окно — calculation.nap_window (ЕДИНСТВЕННАЯ точка расчёта, design
    D16: окно от фактического подъёма страницы, не хардкод); расчётный
    момент — локальное время ref_utc (для «Сейчас» — момент записи, для
    заднего числа — момент вводимого события: окно страницы ввода).
    Вариант текста — по флагам отклонений §3.6 из NapWindow:
      - stretch_to_limit → §7.3 (ранний подъём): совет «до лимита» +
        окно + якоря wake_deadline (из расчёта) и night_target
        (настройка);
      - after_deadline → §7.4 (поздний подъём): окно + якоря
        wake_deadline и night_deadline (настройка);
      - иначе §7.2 — просто строка окна.
    События и настройки читаются при каждом вызове (FR-24, design D14);
    сборка текста — templates texts.nap_window_*.
    """
    user_id = user["id"]
    tz_name = user["timezone"]
    settings = SettingsService(conn).get_all(user_id)

    raw_events = [
        (row["event_type"], _local_naive(row["timestamp"], tz_name))
        for row in list_raw_events(conn, user_id)
    ]
    ref_local = _local_naive(ref_utc, tz_name)
    nap = nap_window(
        raw_events,
        ref_local,
        settings,
        stretch_limit=NAP_STRETCH_LIMIT,
    )
    if nap is None:
        return None

    start_line = f"{nap.start:%H:%M}"
    end_line = f"{nap.end:%H:%M}"
    deadline_line = f"{nap.wake_deadline:%H:%M}"
    if nap.stretch_to_limit:
        return texts.nap_window_early_text(
            f"{NAP_STRETCH_LIMIT:%H:%M}",
            start_line,
            end_line,
            deadline_line,
            _settings_hhmm(settings, "night_target"),
        )
    if nap.after_deadline:
        return texts.nap_window_late_text(
            start_line,
            end_line,
            deadline_line,
            _settings_hhmm(settings, "night_deadline"),
        )
    return texts.nap_window_line(start_line, end_line)


def _settings_hhmm(settings: dict, key: str) -> str:
    """Якорь-настройка «ЧЧ:ММ» (design D14) для текстов окон §7.3/§7.4.

    Ключ не задан (SettingsService.get_all отдаёт только DEFAULT_KEYS —
    seed при /start; расчёты без него берут аргументные дефолты) —
    дефолт ТЗ §5: night_target 22:00, night_deadline 22:30.
    """
    defaults = {"night_target": "22:00", "night_deadline": "22:30"}
    return str(settings.get(key, defaults[key]))


# ------------------------------------------------------------- Помощники

def _kind_from_callback_data(data: str | None) -> str | None:
    """callback_data кнопки → вид события (KIND_SLEEP/KIND_WAKE) или None."""
    if data == "event:sleep":
        return KIND_SLEEP
    if data == "event:wake":
        return KIND_WAKE
    return None


def time_choose_markup() -> InlineKeyboardMarkup:
    """Клавиатура §7.10: «🕐 Сейчас» / «⏪ Ввести время вручную» (FR-15).

    Публичная с версии BUG-002: используется и reply-обработчиком панели
    (reply_event_button), и inline-колбэком event_button_callback.
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(texts.BTN_TIME_NOW, callback_data=CALLBACK_TIME_NOW)],
            [
                InlineKeyboardButton(
                    texts.BTN_TIME_MANUAL, callback_data=CALLBACK_TIME_MANUAL
                )
            ],
        ]
    )


def _local_hhmm(utc_dt: datetime, tz_name: str | None) -> str:
    """UTC-момент → «ЧЧ:ММ» в поясе пользователя (design D4)."""
    try:
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo(_FALLBACK_TZ)
    except Exception:  # некорректное имя пояса в БД — аудитория бота
        tz = ZoneInfo(_FALLBACK_TZ)
    return utc_dt.astimezone(tz).strftime("%H:%M")


def _pending_kind(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return None
    kind = data.get(PENDING_KIND_KEY)
    return kind if kind in (KIND_SLEEP, KIND_WAKE) else None


def _set_pending_kind(context: ContextTypes.DEFAULT_TYPE, kind: str | None) -> None:
    data = getattr(context, "user_data", None)
    if isinstance(data, dict):
        if kind is None:
            data.pop(PENDING_KIND_KEY, None)
            # Сброс любой фазы диалога заднего числа (задача 4.1, design D10):
            # диалог прерван (guard/observer/«Сейчас») — фазу нужно снять.
            data.pop(WAIT_EVENT_TIME_KEY, None)
        else:
            data[PENDING_KIND_KEY] = kind
            # Повторное нажатие event:* начинает НОВЫЙ диалог (review-001-4.1,
            # note 1): зависшая фаза ожидания текста снята — иначе текст,
            # набранный до выбора time:manual, был бы принят незапрошенно.
            data.pop(WAIT_EVENT_TIME_KEY, None)


def _set_wait_event_time(
    context: ContextTypes.DEFAULT_TYPE, active: bool
) -> None:
    """Фаза «ждём текстовый ввод времени» (задача 4.1, design D10).

    Только постановка фазы: все сбросы идут через _set_pending_kind(context,
    None) (guard/observer/«Сейчас») либо очисткой ключа в backfill.py
    (успешный разбор ввода — задача 4.2). Мёртвая ветка сброса убрана
    (review-001-4.1, note 3).
    """
    data = getattr(context, "user_data", None)
    if isinstance(data, dict) and active:
        data[WAIT_EVENT_TIME_KEY] = True


# --------------------------------------------------------- Обработчики

async def event_button_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Нажатие «😴 Заснул» / «☀️ Проснулся» → меню §7.10 (FR-7, FR-8, FR-15).

    Observer — вежливый отказ без события (D13); устаревшая кнопка —
    answerCallbackQuery + перерисовка, событие не создаётся (design D5).
    """
    query = update.callback_query
    if query is None:
        return
    kind = _kind_from_callback_data(query.data)
    if kind is None or update.effective_user is None:
        return

    conn: sqlite3.Connection = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)

        # design D13 (задача 5.1): наблюдателю отметки недоступны —
        # нажатие игнорируется (проверка роли — services.access).
        if access.is_observer(conn, user["id"]):
            _set_pending_kind(context, None)
            await query.answer(text=access.deny_message(), show_alert=False)
            return

        # Guard устаревшей кнопки (design D5): состояние — свежее из БД.
        if is_stale_callback(conn, user["id"], kind):
            _set_pending_kind(context, None)
            await query.answer()
            await common.redraw_menu(update, conn, user["id"])
            return

        # Валидная кнопка: запомнить вид события, показать §7.10 (FR-15).
        _set_pending_kind(context, kind)
        try:
            await query.edit_message_text(
                texts.BACKFILL_CHOOSE_TIME, reply_markup=time_choose_markup()
            )
        except BadRequest:
            # §7.10 уже показан (повторное нажатие) — перерисовка не нужна.
            pass
        await query.answer()
    finally:
        conn.close()


# ------------------------------------------------- Reply-панель (BUG-002)

async def reply_event_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    user: dict,
    kind: str,
) -> None:
    """Кнопка «😴 Заснул» / «☀️ Проснулся» постоянной reply-панели (BUG-002).

    Отличия от event_button_callback: нажатие — обычное текстовое
    сообщение (reply-клавиатура присылает текст), перерисовывать нечего.
    Guard design D5 тот же — состояние перечитывается из БД:
      - «не та» кнопка → дружелюбный ответ по факту (ALREADY_ASLEEP /
        STILL_AWAKE) + свежее inline-меню отдельным сообщением; записи НЕТ;
      - валидная кнопка → штатный диалог §7.10 (BACKFILL_CHOOSE_TIME с
        кнопками time:now / time:manual), как у inline event:*.
    """
    message = update.effective_message
    if message is None:
        return

    # design D13: наблюдателю отметки недоступны — вежливый отказ без события.
    if access.is_observer(conn, user["id"]):
        _set_pending_kind(context, None)
        await message.reply_text(access.deny_message())
        return

    # Guard устаревшей кнопки (design D5): состояние — свежее из БД.
    if is_stale_callback(conn, user["id"], kind):
        _set_pending_kind(context, None)
        await message.reply_text(
            common.ALREADY_ASLEEP if kind == KIND_SLEEP else common.STILL_AWAKE
        )
        await common.send_fresh_menu(update, conn, user["id"])
        return

    # Валидная кнопка: тот же диалог §7.10, что у inline event:* (FR-15).
    _set_pending_kind(context, kind)
    await message.reply_text(
        texts.BACKFILL_CHOOSE_TIME, reply_markup=time_choose_markup()
    )


async def time_choose_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Выбор «🕐 Сейчас» / «⏪ Ввести время вручную» в меню §7.10 (FR-7/8/15)."""
    query = update.callback_query
    if query is None:
        return
    action = query.data
    if action not in (CALLBACK_TIME_NOW, CALLBACK_TIME_MANUAL):
        return
    if update.effective_user is None:
        return

    conn: sqlite3.Connection = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
        kind = _pending_kind(context)

        # design D13 (задача 5.1): наблюдателю отметки недоступны и на
        # этом шаге (проверка роли — services.access).
        if access.is_observer(conn, user["id"]):
            _set_pending_kind(context, None)
            await query.answer(text=access.deny_message(), show_alert=False)
            return

        # Нет ожидаемой фазы диалога (перезапуск процесса / чужое сообщение):
        # трактуем как устаревшее нажатие (design D5 — безопасный дефолт).
        # Симметрично соседним guard-веткам снимаем обе фазы (review-001-4.1,
        # note 2) — зависший WAIT_EVENT_TIME здесь не выживает.
        if kind is None:
            _set_pending_kind(context, None)
            await query.answer()
            await common.redraw_menu(update, conn, user["id"])
            return

        # Guard повторно по свежему состоянию БД (design D5): пока показывалось
        # §7.10, состояние могло измениться (другое устройство/старое окно).
        if is_stale_callback(conn, user["id"], kind):
            _set_pending_kind(context, None)
            await query.answer()
            await common.redraw_menu(update, conn, user["id"])
            return

        if action == CALLBACK_TIME_MANUAL:
            # FR-15: запрос времени текстом §7.11; фаза WAIT_EVENT_TIME
            # сохраняется в context.user_data (задача 4.1, design D10) —
            # текстовый ввод примет обработчик backfill.py. Разбор ввода
            # (парсер) — задача 4.2.
            _set_wait_event_time(context, True)
            await query.answer()
            if update.effective_message is not None:
                await update.effective_message.reply_text(texts.BACKFILL_ASK_TIME)
            return

        # «🕐 Сейчас» (FR-7/FR-8): событие с текущим временем UTC.
        _set_pending_kind(context, None)
        now_utc = datetime.now(timezone.utc)
        try:
            create_event(conn, user["id"], kind, now_utc.strftime(
                "%Y-%m-%d %H:%M:%S"
            ))
        except ValueError:
            # Состояние изменилось между §7.10 и «Сейчас» — guard (design D5).
            await query.answer()
            await common.redraw_menu(update, conn, user["id"])
            return

        # Переключение меню по свежему состоянию (FR-6) + ответ §7.5/§7.2
        # со сводкой событий дня (PB-3, волна 6). answerCallbackQuery до
        # redraw/reply (review-001-2.3, note 1): при падении edit/reply
        # пользователь всё равно не увидит «часики».
        await query.answer()
        await common.redraw_menu(update, conn, user["id"])
        local_hhmm = _local_hhmm(now_utc, user["timezone"])
        # Сводка — ЕДИНАЯ точка сборки (PB-3): события и настройки читаются
        # ПОСЛЕ записи события — факт уже в списке (расчётный момент — now).
        summary_lines = day_summary_lines(conn, user, now_utc)
        # Строка окна — от фактического подъёма страницы (FR-38, D20):
        # тот же расчётный момент, что у сводки; None — факта «Подъем»
        # нет (окно от ненастоящего подъёма не строим).
        window_line = nap_window_text(conn, user, now_utc)
        if kind == KIND_SLEEP:
            # §7.5: БЕЗ строки окна (design D20: показ окон в §7.5
            # отклонён) и БЕЗ «Крайнего пробуждения» (решение 29.10).
            answer = fell_asleep_text(local_hhmm, summary_lines)
        else:
            # FR-8: подтверждающая строка + окно + сводка PB-3 без изменений.
            answer = woke_up_text(local_hhmm, summary_lines, window_line)
        if update.effective_message is not None:
            await update.effective_message.reply_text(answer)
    finally:
        conn.close()
