# Слой handlers: обработчики Telegram на python-telegram-bot v21+ (async).
# Правило слоёв (design D1, FR-1): handlers обращаются только к services,
# НЕ импортируют repositories и не обращаются к БД напрямую.
# Наполнение — задачи волн 2–5 change-пакета add-core-diary.
#
# Регистрация — через register_handlers(application) из src.main (задача 2.1).
# Импорт модулей обработчиков ЛЕНИВЫЙ (внутри функции): критерий готовности
# 1.1 требует, чтобы `import src.handlers` не тянул src.repositories, а
# start.py на уровне модуля импортирует repositories через сервис.

from __future__ import annotations


def register_handlers(application) -> None:
    """Зарегистрировать обработчики задач в Application (вызывает src.main).

    Сейчас: команда /start (задача 2.1); колбэки событий и выбора времени
    §7.10 (задача 2.3, design D2); отмена последней отметки (задача 2.4,
    design D12). Дальнейшие задачи добавляют обработчики сюда же.
    """
    from telegram.ext import CallbackQueryHandler, CommandHandler

    from src.handlers import events, start, undo

    application.add_handler(CommandHandler("start", start.start_command))
    application.add_handler(
        CallbackQueryHandler(
            events.event_button_callback, pattern=r"^event:(sleep|wake)$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            events.time_choose_callback, pattern=r"^time:(now|manual)$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(undo.undo_last_callback, pattern=r"^undo:last$")
    )
