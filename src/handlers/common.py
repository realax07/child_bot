# Общие помощники обработчиков: клавиатура по состоянию (задача 2.2).
#
# design D5: меню строится по состоянию из БД при каждом построении;
# перерисовка актуальной клавиатуры — реакция на нажатие устаревшей кнопки
# из истории чата (FR-6, сценарий «Устаревшая кнопка из истории чата»,
# основа guard'а задач 2.3/2.4). Событийное меню — inline-клавиатура:
# только inline-кнопки в старых сообщениях остаются нажимаемыми, что и
# требует перечитывания состояния по БД (CallbackQueryHandler).
#
# Слой handlers (design D1, FR-1): сервисы — да, repositories/БД напрямую —
# нет. Здесь нет прямых запросов: состояние читает src/services/state.py.

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src.db import init_db
from src.services import state
from src.services.event import get_user
from src.services.state import (
    BTN_SLEEP,
    BTN_UNDO,
    BTN_WAKE,
    CALLBACK_SLEEP,
    CALLBACK_UNDO,
    CALLBACK_WAKE,
    KIND_SLEEP,
    KIND_UNDO,
    KIND_WAKE,
)

__all__ = [
    "BTN_SLEEP",
    "BTN_WAKE",
    "BTN_UNDO",
    "CALLBACK_SLEEP",
    "CALLBACK_WAKE",
    "CALLBACK_UNDO",
    "KIND_SLEEP",
    "KIND_WAKE",
    "KIND_UNDO",
    "db_path",
    "open_conn",
    "build_markup",
    "current_markup",
    "redraw_menu",
    "build_reply_keyboard",
    "send_fresh_menu",
    "reply_button_message",
    "REPLY_PANEL_INTRO",
    "ALREADY_ASLEEP",
    "STILL_AWAKE",
    "FRESH_MENU_NOTE",
]


# ----------------------------------------------------------------------------
# Постоянная reply-панель (хотфикс BUG-002, S2; PLAN-BUG-002, решение по
# test-model/bugs/BUG-002-no-persistent-keyboard.md).
#
# Root cause BUG-002: меню — inline-кнопки внутри конкретного сообщения;
# после диалога панель остаётся в истории чата, ввести новое событие нечем.
# Фикс: постоянная reply-клавиатура внизу экрана + обработчик её текстовых
# кнопок с guard-перечитыванием состояния (design D5): «не та» кнопка —
# дружелюбный ответ по факту без записи + свежее inline-меню в чат.
# Оба механизма сосуществуют (п. 4 плана): reply-панель — всегда доступная,
# inline — для подтверждений/выбора времени (§7.10, §7.13).

# Текст сообщения-носителя reply-панели (/start): сама панель — свойство
# чата, но Telegram требует отправить ReplyKeyboardMarkup с сообщением.
REPLY_PANEL_INTRO = "👇 Панель событий закреплена внизу экрана — доступна в любой момент."

# Служебные тексты guard-отказов (вне texts.py §7 — как OBSERVER_DENIED
# в events.py): «не та» кнопка панели не ошибка — ответ по фактическому
# состоянию, записи НЕТ (BUG-002, решение «нажатие не той кнопки»).
ALREADY_ASLEEP = "😴 Ребёнок уже спит — повторно засыпание отмечать не нужно. Свежее меню ниже."
STILL_AWAKE = "☀️ Ребёнок бодрствует — пробуждение отмечать не нужно. Свежее меню ниже."

# Текст сообщения со свежим inline-меню после события/отмены с панели
# (п. 3 плана BUG-002: «диалоги были под рукой»).
FRESH_MENU_NOTE = "Актуальное меню:"


def build_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная reply-панель BUG-002: статичный список state.REPLY_KEYBOARD.

    resize_keyboard=True — компактная панель; one_time_keyboard=False —
    панель НЕ исчезает после нажатия (смысл хотфикса).
    """
    return ReplyKeyboardMarkup(
        state.REPLY_KEYBOARD, resize_keyboard=True, one_time_keyboard=False
    )


async def send_fresh_menu(
    update: Update, conn: sqlite3.Connection, user_id: int
) -> None:
    """Прислать свежее inline-меню отдельным сообщением (BUG-002, п. 3).

    После события/отмены с reply-панели перерисовывать нечего (нажатие —
    не колбэк), поэтому актуальное меню по свежему чтению БД уходит новым
    сообщением. Используется и reply-обработчиками панели.
    """
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(FRESH_MENU_NOTE, reply_markup=current_markup(conn, user_id))


async def reply_button_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Нажатие кнопки постоянной reply-панели (BUG-002, п. 2 плана).

    ReplyKeyboard присылает ТЕКСТ кнопки — вид события определяется по
    тексту (state.kind_of_button / BTN_UNDO). Состояние перечитывается
    из БД (guard D5, services.state). Произвольный текст (не кнопка) —
    молча без реакции: диалоги заднего числа ведёт backfill.py (задача
    4.1 — не ломаем). Регистрируется в ОТДЕЛЬНОЙ группе PTB (group=1):
    MessageHandler backfill.py (группа 0) вне фазы диалога молчит, и
    обновление доходит и до него, и до этого обработчика.
    """
    from src.handlers import events, undo  # лениво: events/undo импортируют common

    message = update.effective_message
    text = message.text if message is not None else None
    if text is None or text not in state.REPLY_BUTTONS:
        return
    if update.effective_user is None:
        return

    conn: sqlite3.Connection = open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
        if text == state.BTN_UNDO:
            await undo.reply_undo_button(update, context, conn, user)
            return
        kind = state.kind_of_button(text)
        if kind is not None:
            await events.reply_event_button(update, context, conn, user, kind)
    finally:
        conn.close()


def db_path(context) -> str | Path:
    """Путь к БД: bot_data['db_path'] (тесты/stand) или env BOT_DB_PATH."""
    path = context.bot_data.get("db_path") if context.bot_data else None
    return path or os.environ.get("BOT_DB_PATH", "child_bot.db")


def open_conn(context) -> sqlite3.Connection:
    """Открыть (при необходимости — инициализировать) соединение с БД бота."""
    return init_db(db_path(context))


def build_markup(current_state: str) -> InlineKeyboardMarkup:
    """Разметка PTB по состоянию из чистой build_menu (design D5)."""
    rows = [
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in state.build_menu(current_state)
    ]
    return InlineKeyboardMarkup(rows)


def current_markup(conn: sqlite3.Connection, user_id: int) -> InlineKeyboardMarkup:
    """Актуальная клавиатура пользователя: состояние перечитано из БД.

    Используется при переключении меню после события и при перерисовке
    в guard'е (FR-6). Кэширования нет — каждый вызов читает БД.
    """
    return build_markup(state.get_state(conn, user_id))


async def redraw_menu(update: Update, conn: sqlite3.Connection, user_id: int) -> None:
    """Перерисовать клавиатуру сообщения с нажатой кнопкой (guard, FR-6).

    Ставит reply_markup сообщения колбэка в актуальное состояние; сама
    клавиатура строится по свежему чтению БД. Вызывается обработчиком
    колбэка при устаревшем нажатии (задачи 2.3/2.4, design D5).
    """
    query = update.callback_query
    if query is None or query.message is None:
        return
    try:
        await query.edit_message_reply_markup(
            reply_markup=current_markup(conn, user_id)
        )
    except BadRequest:
        # «Message is not modified»: разметка уже совпадает с актуальной —
        # перерисовка не требуется. Прочие ошибки не глотаются (review-001-2.2
        # minor 2): сетевые/прочие сбои видны в логе PTB.
        pass
