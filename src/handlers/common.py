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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from src.db import init_db
from src.services import state
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
]


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
    except Exception:
        # Разметка уже совпадает с актуальной (PTB поднимает BadRequest) —
        # перерисовка не требуется; состояние нажатия это не меняет.
        pass
