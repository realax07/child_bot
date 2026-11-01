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


async def _post_init(application: Application) -> None:
    """Старт единственного планировщика напоминаний (задача 3.1, design D19).

    Вызывается PTB после инициализации бота (bot готов к send_message).
    При старте процесса планировщик перестраивается (PB-7 крон-сторож):
    ре-шедул напоминаний из событий — отработавшие пуши не повторяются.
    """
    from src.db import db_path_from_env
    from src.services.scheduler import start_reminder_scheduler

    start_reminder_scheduler(application.bot, db_path_from_env())


async def _post_shutdown(application: Application) -> None:
    """Останов планировщика при завершении процесса (design D19)."""
    from src.services.scheduler import shutdown_reminder_scheduler

    shutdown_reminder_scheduler()


def build_application() -> Application:
    """Построить async-приложение PTB с токеном из env BOT_TOKEN.

    Обработчики (handlers) регистрируются задачами волн 2–5.
    """
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Переменная окружения BOT_TOKEN не задана.")

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

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
