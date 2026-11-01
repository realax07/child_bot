# Экран настроек и диалог изменения значения (change-пакет
# add-settings-history, задача 1.2; ТЗ §7.16–7.19, §10.2–10.4; design D21).
#
# ТЗ §10.3: двойной вход — команда /settings и callback «settings:show»
# (кнопка «⚙️ Настройки»); ТЗ §6: кнопка есть в ОБОИХ состояниях меню,
# настройки НЕ создают событий (sleep_events не пишется, как у подсказки
# FR-32). Нажатие параметра (callback «settings:edit:<key>») → диалог
# §7.17 (имя, текущее, формат); текстовый ввод значения обрабатывается
# только в фазе FSM WAIT_SETTING_VALUE (ТЗ §10.4; design D10-стиль:
# фаза живёт в context.user_data PTB, БД-FSM нет): успех → §7.18
# «старое → новое» (SettingsService.update — валидация, запись settings +
# settings_history одной операцией), ошибка SettingsValueError → §7.19
# БЕЗ изменения настройки, фаза сохранена (повторный ввод, как §7.12).
# Отмена — кнопка «Отменить» (settings:cancel) под §7.17: фаза гасится,
# настройки не меняются.
#
# Тексты — дословные §7.16–7.19 из src/texts.py (D6); форматы не-длительных
# параметров в §7.17/§7.19 — служебные формулировки по типам валидации
# SettingsService. Классификация ключей — ЧЕРЕЗ множества сервиса
# (_DURATION_MINUTE_KEYS и др.): единый источник ключей DEFAULT_KEYS
# (design D14, D21 — копий не заводим; альтернатива «своя копия» уже
# приводила к инциденту — техдолг 2.3). Импорт приватных имён сознателен:
# src/services вне зоны задачи 1.2, публиковать новый API санкции не давали.
#
# Доступ (design D13, FR-47): экран и диалог — только со СВОИМ user_id
# (get_user по effective_user.id); наблюдатель — read-only: экран ВИДИТ,
# правка (edit/ввод значения) — вежливый отказ, настройки не меняются.
#
# Слой handlers (design D1): только сервисы (services.settings,
# services.access, services.event); без repositories и SQL.

from __future__ import annotations

import sqlite3
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src import texts
from src.handlers import common
from src.handlers.events import (
    CONFIRM_PAIR_KEY,
    PENDING_KIND_KEY,
    WAIT_EVENT_TIME_KEY,
    WAIT_PAIR_SLEEP_KEY,
    WAIT_PAIR_WAKE_KEY,
)
from src.handlers.backfill import CONFIRM_OVERWRITE_KEY
from src.services.access import is_observer
from src.services.event import get_user
from src.services.settings import (
    DEFAULT_KEYS,
    SettingsService,
    SettingsValueError,
    _DURATION_MINUTE_KEYS,
    _HOURS_KEYS,
    _PLAIN_INT_KEYS,
    _TIME_KEYS,
    _WINDOW_KEYS,
)

# Текст кнопки «⚙️ Настройки» (ТЗ §6, дословно) и callback-контракт
# settings:show (§10.3) — ЕДИНЫЙ источник src/services/state.py (design
# D14): add-settings-history 2.0 перенёс константы туда, где лежат все
# кнопочные константы меню, — кнопка теперь физически в
# REPLY_KEYBOARD/build_menu. Импорт (не копия): направление «handler →
# service» допустимо (D1), обратное — нет, поэтому определение живёт в
# state.py, а не наоборот. Имя BTN_SETTINGS оставлено экспортом модуля —
# на него ссылаются common.py и тесты.
from src.services.state import BTN_SETTINGS, CALLBACK_SETTINGS_SHOW
CALLBACK_SETTINGS_CANCEL = "settings:cancel"
CALLBACK_EDIT_PREFIX = "settings:edit:"

# CALLBACK_SETTINGS_SHOW + CALLBACK_SETTINGS_CANCEL — единая регистрация.
CALLBACK_PATTERN = r"^settings:(show|cancel|edit:.+)$"

# Фаза FSM §10.4: WAIT_SETTING_VALUE — бот ждёт новое значение настройки.
# В user_data хранится РЕДАКТИРУЕМЫЙ ключ (design D10-стиль: фазы диалога
# — в context.user_data, один процесс).
WAIT_SETTING_VALUE = "wait_setting_value"

# Фазы backfill-диалога заднего числа (группа 0) — ЧЕРЕЗ константы
# владельцев (events.py / backfill.py), без строковых копий (design
# D14-стиль: единый источник имён ключей user_data). Импорт из соседних
# модулей handlers сознателен: имена ключей — публичные константы своих
# модулей, дублировать их здесь значило бы завести вторую копию.
BACKFILL_PHASE_KEYS: tuple[str, ...] = (
    WAIT_EVENT_TIME_KEY,  # «ждём текстовый ввод времени» (events.py)
    PENDING_KIND_KEY,  # вид события диалога (events.py)
    CONFIRM_OVERWRITE_KEY,  # фаза подтверждения перезаписи §7.13 (backfill.py)
    # Задача 3.1 (FR-51, design D23): фазы ПАРНОГО ввода — в том же
    # взаимном исключении с настройками (обе стороны), что и остальные
    # фазы группы 0; имена — из событий.py владельца, без копий (D14).
    WAIT_PAIR_SLEEP_KEY,  # шаг 1 пары «Когда заснул?» (events.py)
    WAIT_PAIR_WAKE_KEY,  # шаг 2 пары «Когда проснулся?» (events.py)
    CONFIRM_PAIR_KEY,  # §7.13 пары: оба момента до решения (events.py)
)

# Состав экрана: группы §7.16 и их ключи (D14: имена — из DEFAULT_KEYS,
# копий значений нет). «Прочее» — 4 параметра §7.16 + backfill_depth_days
# (§5, 12-й параметр) + target_nap_duration (13-й, допущение D21).
SETTINGS_LAYOUT: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        texts.SETTINGS_GROUP_DAY,
        ("sleep_min_before_nap", "sleep_max_before_nap", "wake_deadline"),
    ),
    (texts.SETTINGS_GROUP_NIGHT, ("night_target", "night_deadline")),
    (texts.SETTINGS_GROUP_WAKE, ("wake_total_min", "wake_total_max")),
    (
        texts.SETTINGS_GROUP_MISC,
        (
            "remind_before",
            "merge_threshold",
            "night_bounds",
            "day_start",
            "backfill_depth_days",
            "target_nap_duration",
        ),
    ),
)


# ------------------------------------------------------- Формат значений


def value_line(key: str, value: int | float | str) -> str:
    """Значение параметра в формате ТЗ: «5 ч 20 мин», «12 ч 30 мин»,
    «15:30», «19:00–07:00», «3 дня» (форматирование §7 — texts.py)."""
    if key in _PLAIN_INT_KEYS:  # дни глубины ввода (§5)
        return texts.days_line(int(value))
    if key in _DURATION_MINUTE_KEYS:
        return texts.format_duration(timedelta(minutes=int(value)))
    if key in _HOURS_KEYS:  # границы бодрствования в часах
        return texts.format_duration(timedelta(minutes=round(float(value) * 60)))
    if key in _WINDOW_KEYS:
        # Хранится с дефисом («19:00-07:00»), ТЗ §7.16 печатает тире.
        return str(value).replace("-", "–")
    if key in _TIME_KEYS:
        return str(value)
    return str(value)  # pragma: no cover — все 13 ключей покрыты выше


def format_line(key: str) -> str:
    """Строка формата §7.17/§7.19 по типу ключа (валидация SettingsService)."""
    if key in _DURATION_MINUTE_KEYS:
        return texts.SETTINGS_FORMAT_DURATION
    if key in _TIME_KEYS:
        return texts.SETTINGS_FORMAT_TIME
    if key in _HOURS_KEYS:
        return texts.SETTINGS_FORMAT_HOURS
    if key in _WINDOW_KEYS:
        return texts.SETTINGS_FORMAT_WINDOW
    return texts.SETTINGS_FORMAT_DAYS  # _PLAIN_INT_KEYS


# ------------------------------------------------------------- Экран §7.16


def screen_text(settings_map: dict[str, int | float | str]) -> str:
    """Экран §7.16: шапка, группы с текущими значениями, подвал."""
    lines = [texts.SETTINGS_SCREEN_HEADER]
    for group, keys in SETTINGS_LAYOUT:
        lines.append("")
        lines.append(texts.settings_group_line(group))
        for key in keys:
            lines.append(
                texts.settings_param_line(
                    texts.SETTINGS_PARAM_LABELS[key],
                    value_line(key, settings_map[key]),
                )
            )
    lines.append("")
    lines.append(texts.SETTINGS_SCREEN_FOOTER)
    return "\n".join(lines)


# ----------------------------------------------------- Фазы FSM (§10.4)


def _pending_key(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Редактируемый ключ из фазы WAIT_SETTING_VALUE; None — фазы нет.

    Невалидный ключ в фазе (перезапуск процесса) — фаза гасится:
    безопасный дефолт без записи (design D10-стиль).
    """
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return None
    key = data.get(WAIT_SETTING_VALUE)
    if key not in DEFAULT_KEYS:
        data.pop(WAIT_SETTING_VALUE, None)
        return None
    return key


def _clear_phase(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Снять фазу WAIT_SETTING_VALUE; True — фаза была активна."""
    data = getattr(context, "user_data", None)
    if isinstance(data, dict):
        return data.pop(WAIT_SETTING_VALUE, None) is not None
    return False


def _clear_backfill_phases(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Снять ВСЕ фазы backfill-диалога заднего числа (группа 0);
    True — хотя бы одна была активна.

    Взаимное исключение фаз (major review-001-1.2): PTB доставляет текст
    в ОБЕ группы обработчиков, поэтому одновременные фазы настроек
    (группа 2) и backfill-диалога (группа 0) приводят к тому, что ввод
    значения настройки записывает событие сна. Вход в настройки —
    начало НОВОЙ фазы: предыдущая снимается целиком («фазы не
    переживают завершение диалога», notes review-001-4.3) — включая
    фазу подтверждения перезаписи §7.13.
    """
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return False
    had_phase = False
    for phase_key in BACKFILL_PHASE_KEYS:
        if data.pop(phase_key, None) is not None:
            had_phase = True
    return had_phase


def _cancel_markup() -> InlineKeyboardMarkup:
    """Кнопка отмены диалога значения (служебная, вне ТЗ §10.3-списка)."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(texts.SETTINGS_BTN_CANCEL, callback_data=CALLBACK_SETTINGS_CANCEL)]]
    )


# ------------------------------------------------------------ Обработчики


async def _reply_screen(
    message, conn: sqlite3.Connection, user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Ответ экраном §7.16 по свежим значениям пользователя (без кэша)."""
    _clear_phase(context)  # новый вход в настройки гасит незавершённый диалог
    # Взаимное исключение фаз (major review-001-1.2): вход в настройки
    # гасит и фазы backfill-диалога (WAIT_EVENT_TIME, PENDING_KIND,
    # confirm_overwrite) — иначе текст значения настройки дошёл бы и в
    # группу 0 и записал событие.
    _clear_backfill_phases(context)
    settings_map = SettingsService(conn).get_all(user_id)
    await message.reply_text(screen_text(settings_map))


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /settings (ТЗ §10.2) → экран §7.16.

    Событий не создаёт, состояние ребёнка не меняет; вход в настройки
    гасит незавершённый диалог значения (фаза WAIT_SETTING_VALUE).
    """
    if update.effective_message is None or update.effective_user is None:
        return
    conn: sqlite3.Connection = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
        await _reply_screen(update.effective_message, conn, user["id"], context)
    finally:
        conn.close()


async def reply_settings_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    user: dict,
) -> None:
    """Текст «⚙️ Настройки» с reply-панели (ТЗ §6; маршрут — common.py).

    Тот же экран, что /settings и callback settings:show (ТЗ §10.3 —
    двойной вход, одна логика). Соединение открыл вызывающий
    (reply_button_message) — сигнатура как у reply_hint_button.
    """
    if update.effective_message is None:
        return
    await _reply_screen(update.effective_message, conn, user["id"], context)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Колбэки настроек (ТЗ §10.3): settings:show / settings:edit:<key> /
    settings:cancel.

    show — экран §7.16 (наблюдателю тоже доступен, design D13 — чтение);
    edit — диалог §7.17 + фаза WAIT_SETTING_VALUE (только parent,
    наблюдателю — вежливый отказ); cancel — фаза гасится без изменений.
    Устаревший/неизвестный key в edit — безопасный дефолт: без фазы,
    без записи.
    """
    query = update.callback_query
    if query is None or update.effective_user is None or update.effective_message is None:
        return
    data = query.data or ""
    message = update.effective_message

    conn: sqlite3.Connection = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)

        if data == CALLBACK_SETTINGS_SHOW:
            await _reply_screen(message, conn, user["id"], context)
            await query.answer()
            return

        if data == CALLBACK_SETTINGS_CANCEL:
            had_phase = _clear_phase(context)
            await query.answer()
            if had_phase:
                await message.reply_text(texts.SETTINGS_CANCELLED)
            return

        if data.startswith(CALLBACK_EDIT_PREFIX):
            key = data[len(CALLBACK_EDIT_PREFIX) :]
            if key not in DEFAULT_KEYS:
                await query.answer()
                return
            if is_observer(conn, user["id"]):
                await query.answer()
                await message.reply_text(texts.OBSERVER_SETTINGS_DENIED)
                return
            current = SettingsService(conn).get(user["id"], key)
            # Взаимное исключение фаз (major review-001-1.2): вход в
            # диалог правки — начало новой фазы, фазы backfill-диалога
            # снимаются ДО постановки WAIT_SETTING_VALUE.
            _clear_backfill_phases(context)
            phase = getattr(context, "user_data", None)
            if isinstance(phase, dict):
                phase[WAIT_SETTING_VALUE] = key
            await query.answer()
            await message.reply_text(
                texts.settings_ask_value_text(
                    texts.SETTINGS_PARAM_LABELS[key],
                    value_line(key, current),
                    format_line(key),
                ),
                reply_markup=_cancel_markup(),
            )
    finally:
        conn.close()


async def setting_value_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Текстовый ввод в фазе WAIT_SETTING_VALUE (ТЗ §10.4).

    Вне фазы — молча (как backfill_time_message: не мешаем остальным).
    В фазе: наблюдатель — вежливый отказ, фаза гасится (read-only);
    SettingsService.update → успех §7.18 «старое → новое», фаза снята;
    SettingsValueError → §7.19 БЕЗ изменения настройки, фаза СОХРАНЕНА
    (повторный ввод, как §7.12 у заднего числа).
    """
    if update.message is None or update.effective_user is None:
        return
    key = _pending_key(context)
    if key is None:
        return

    conn: sqlite3.Connection = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
        if is_observer(conn, user["id"]):
            _clear_phase(context)
            await update.message.reply_text(texts.OBSERVER_SETTINGS_DENIED)
            return
        try:
            old, new = SettingsService(conn).update(
                user["id"], key, update.message.text or ""
            )
        except SettingsValueError:
            # §7.19 (FR-45): настройка и история не тронуты, фаза живёт.
            await update.message.reply_text(
                texts.settings_bad_value_text(format_line(key))
            )
            return
    finally:
        conn.close()

    _clear_phase(context)
    await update.message.reply_text(
        texts.settings_updated_text(
            texts.SETTINGS_PARAM_LABELS[key],
            value_line(key, old),
            value_line(key, new),
        )
    )
