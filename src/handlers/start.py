# Обработчик /start (change-пакет add-core-diary, задача 2.1).
#
# design D4: TZ пользователя — IANA-имя из карты language_code → пояс,
# fallback Europe/Moscow (аудитория бота); хранится в users.timezone,
# применяется на границе ввода/вывода.
# design D5: состояние «бодрствует» отдельно НЕ хранится — выводится из
# последнего события (после /start событий нет → бодрствует); здесь
# никакого состояния не пишется.
# design D14: seed 13 дефолтных настроек идемпотентен (SettingsService).
# FR-5: первый /start — создать пользователя, TZ, 13 настроек, ответ §7.1;
# повторный /start — ничего не сбрасывает.
#
# Примечание (структура, задача 2.1): по прямому указанию задачи 2.1
# обработчик использует UsersRepo и SettingsService напрямую; вынос
# start-логики в отдельный сервис — уточнение задачами волн 2–5.

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src import texts
from src.db import init_db
from src.repositories.users import UsersRepo
from src.services.settings import SettingsService

# design D4: fallback для неизвестных language_code — пояс аудитории бота.
DEFAULT_TZ = "Europe/Moscow"

# Карта language_code → IANA (первое приближение, ТЗ §8.2 / design D4).
# Артефакт кода, фиксируется юнит-тестом (TC-START-003). Код вида «ru-RU»
# обрезается до «ru».
LANGUAGE_TO_TZ = {
    "ru": "Europe/Moscow",
    "uk": "Europe/Kyiv",
    "be": "Europe/Minsk",
    "kk": "Asia/Almaty",
    "ky": "Asia/Bishkek",
    "uz": "Asia/Tashkent",
    "hy": "Asia/Yerevan",
    "ka": "Asia/Tbilisi",
    "az": "Asia/Baku",
    "md": "Europe/Chisinau",
}

# Клавиатура для 2.1: одна событийная кнопка состояния «бодрствует»
# (ТЗ §6). Динамическое меню (вторая кнопка, «Отменить последнее»)
# уточняет задача 2.2 (design D5).
BTN_SLEEP = "😴 Заснул"


def map_language_to_tz(language_code: str | None) -> str:
    """language_code TG-профиля → IANA-имя пояса (design D4)."""
    if not language_code:
        return DEFAULT_TZ
    base = language_code.split("-")[0].strip().lower()
    return LANGUAGE_TO_TZ.get(base, DEFAULT_TZ)


def _db_path(context: ContextTypes.DEFAULT_TYPE) -> str | Path:
    """Путь к БД: bot_data['db_path'] (тесты/stand) или env BOT_DB_PATH."""
    path = context.bot_data.get("db_path") if context.bot_data else None
    return path or os.environ.get("BOT_DB_PATH", "child_bot.db")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start: пользователь, TZ, seed 13 настроек, ответ §7.1 (FR-5)."""
    tg_user = update.effective_user
    if tg_user is None:
        return

    conn: sqlite3.Connection = init_db(_db_path(context))
    try:
        tz = map_language_to_tz(tg_user.language_code)
        users = UsersRepo(conn)
        user = users.get_or_create(tg_user.id, timezone=tz)
        if user["timezone"] != tz:
            # FR-4: TZ сохраняется при каждом /start (профиль мог измениться).
            users.set_timezone(tg_user.id, tz)
            user["timezone"] = tz

        # Идемпотентный seed 13 дефолтов (FR-5, FR-23, design D14):
        # существующие значения не перезаписываются.
        SettingsService(conn).seed_defaults(user["id"])

        # Состояние «бодрствует» отдельно не хранится (design D5):
        # после /start у пользователя нет событий → выводится как бодрствует.
    finally:
        conn.close()

    keyboard = ReplyKeyboardMarkup(
        [[BTN_SLEEP]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )
    if update.effective_message is not None:
        await update.effective_message.reply_text(texts.START, reply_markup=keyboard)
