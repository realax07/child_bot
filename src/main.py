# Точка входа бота «Дневник сна ребёнка».
# Change-пакет add-core-diary, задача 1.1 (каркас слоёв и точки входа).
#
# design D2: python-telegram-bot v21+ (async), Application.builder(), long polling.
# Токен берётся из переменной окружения BOT_TOKEN.
#
# Запуск:            BOT_TOKEN=<token> python3 -m src.main
# Проверка каркаса:  BOT_TOKEN=<stub>  python3 -m src.main --check
# (--check строит Application и завершается, не выходя в сеть)

from __future__ import annotations

import os
import sys

from telegram.ext import Application, ApplicationBuilder


def build_application() -> Application:
    """Построить async-приложение PTB с токеном из env BOT_TOKEN.

    Обработчики (handlers) регистрируются задачами волн 2–5.
    """
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Переменная окружения BOT_TOKEN не задана.")

    application = ApplicationBuilder().token(token).build()

    # Задача 2.3: регистрация обработчиков (команда /start, колбэки событий).
    from src.handlers import register_handlers

    register_handlers(application)
    return application


def main() -> None:
    application = build_application()

    if "--check" in sys.argv[1:]:
        # Проверка каркаса: импорт и построение Application прошли — выходим без сети.
        print("OK: Application построен, слои handlers/services/repositories на месте.")
        return

    # Основной режим: long polling (design D2).
    application.run_polling()


if __name__ == "__main__":
    main()
