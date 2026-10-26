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
# FR-7: sleep «сейчас» → §7.5, строка «Крайнее пробуждение» — из настройки
# wake_deadline («Крайнее время пробуждения от дневного сна», ТЗ §5).
# FR-8: wake «сейчас» → подтверждающая строка §7.2; строка окна дневного сна
# в этом пакете НЕ выводится (окна — Пакет 2).
# design D4: время хранится в UTC; в текстах ответов — локальное время
# пользователя (users.timezone через zoneinfo).
#
# Слой handlers (design D1): только сервисы (services.event, services.state,
# services.settings), без repositories и прямого SQL.

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src import texts
from src.handlers import common
from src.services.event import ROLE_OBSERVER, create_event, get_user
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

# Сообщение наблюдателю (design D13): вне texts.py §7 — служебный текст.
OBSERVER_DENIED = "Отметки сна недоступны в режиме наблюдателя."

# Fallback пояса — как в /start (design D4, аудитория бота).
_FALLBACK_TZ = "Europe/Moscow"


# -------------------------------------------------------------- Тексты FR-7/8

def fell_asleep_text(local_hhmm: str, latest_wake: str) -> str:
    """§7.5 с фактическим временем засыпания (FR-7).

    «Крайнее пробуждение: ЧЧ:ММ» — из настройки wake_deadline (ТЗ §5);
    формулировка дословно по §7.5 (AC-6).
    """
    return (
        f"✅ Засыпание записано в {local_hhmm}.\n"
        f"Сон идёт. Крайнее пробуждение: {latest_wake}."
    )


def woke_up_text(local_hhmm: str) -> str:
    """§7.2, только подтверждающая строка (FR-8).

    Строка «Окно дневного сна: …» не выводится — окна расчётов Пакет 2.
    """
    return f"✅ Пробуждение записано в {local_hhmm}."


# ------------------------------------------------------------- Помощники

def _kind_from_callback_data(data: str | None) -> str | None:
    """callback_data кнопки → вид события (KIND_SLEEP/KIND_WAKE) или None."""
    if data == "event:sleep":
        return KIND_SLEEP
    if data == "event:wake":
        return KIND_WAKE
    return None


def _time_choose_markup() -> InlineKeyboardMarkup:
    """Клавиатура §7.10: «🕐 Сейчас» / «⏪ Ввести время вручную» (FR-15)."""
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

        # design D13: наблюдателю отметки недоступны — нажатие игнорируется.
        if user["role"] == ROLE_OBSERVER:
            _set_pending_kind(context, None)
            await query.answer(text=OBSERVER_DENIED, show_alert=False)
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
                texts.BACKFILL_CHOOSE_TIME, reply_markup=_time_choose_markup()
            )
        except BadRequest:
            # §7.10 уже показан (повторное нажатие) — перерисовка не нужна.
            pass
        await query.answer()
    finally:
        conn.close()


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

        # design D13: наблюдателю отметки недоступны и на этом шаге.
        if user["role"] == ROLE_OBSERVER:
            _set_pending_kind(context, None)
            await query.answer(text=OBSERVER_DENIED, show_alert=False)
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

        # Переключение меню по свежему состоянию (FR-6) + ответ §7.5/§7.2.
        # answerCallbackQuery до redraw/reply (review-001-2.3, note 1):
        # при падении edit/reply пользователь всё равно не увидит «часики».
        await query.answer()
        await common.redraw_menu(update, conn, user["id"])
        local_hhmm = _local_hhmm(now_utc, user["timezone"])
        if kind == KIND_SLEEP:
            latest_wake = str(
                SettingsService(conn).get(user["id"], "wake_deadline")
            )
            answer = fell_asleep_text(local_hhmm, latest_wake)
        else:
            # FR-8: только подтверждающая строка, без строки окна (Пакет 2).
            answer = woke_up_text(local_hhmm)
        if update.effective_message is not None:
            await update.effective_message.reply_text(answer)
    finally:
        conn.close()
