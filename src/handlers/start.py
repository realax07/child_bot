# Обработчик /start (change-пакет add-core-diary, задача 2.1; интеграция — 2.3).
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
# Интеграция 2.3 (review-001-2.2 minor 1): единый источник правды о пути к
# БД — common.open_conn (приватная копия _db_path удалена); ответ —
# inline-меню из common.current_markup (единый стиль с колбэками), а не
# reply-заглушка 2.1: inline-кнопки остаются нажимаемыми в истории чата,
# что и требует guard'а устаревших клавиатур (design D5).
#
# Примечание (структура, задача 2.1): по прямому указанию задачи 2.1
# обработчик использует UsersRepo и SettingsService напрямую; вынос
# start-логики в отдельный сервис — уточнение задачами волн 2–5.

from __future__ import annotations

import sqlite3

from telegram import Update
from telegram.ext import ContextTypes

from src import texts
from src.handlers import common
from src.handlers.common import current_markup, open_conn
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


def map_language_to_tz(language_code: str | None) -> str:
    """language_code TG-профиля → IANA-имя пояса (design D4)."""
    if not language_code:
        return DEFAULT_TZ
    base = language_code.split("-")[0].strip().lower()
    return LANGUAGE_TO_TZ.get(base, DEFAULT_TZ)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start: пользователь, TZ, seed 13 настроек, ответ §7.1 (FR-5)."""
    tg_user = update.effective_user
    if tg_user is None:
        return

    conn: sqlite3.Connection = open_conn(context)
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
        # Меню — inline-клавиатура по состоянию (единый стиль с колбэками 2.3).
        markup = current_markup(conn, user["id"])
    finally:
        conn.close()

    if update.effective_message is not None:
        # Хотфикс BUG-002: постоянная reply-панель (видна всегда внизу
        # экрана, не зависит от истории чата). Отдельным сообщением-носителем
        # ДО приветствия; приветствие (ниже) по-прежнему несёт inline-меню
        # по состоянию — оба механизма сосуществуют (п. 4 плана BUG-002):
        # reply-панель — всегда доступная, inline — для подтверждений/
        # выбора времени.
        await update.effective_message.reply_text(
            common.REPLY_PANEL_INTRO, reply_markup=common.build_reply_keyboard()
        )
        await update.effective_message.reply_text(
            texts.START, reply_markup=markup
        )
