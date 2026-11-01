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
    §7.10 (задача 2.3, design D2); подсказка «⏰ Подсказка сейчас»
    (add-recommendations 2.2, callback status:show, design D17); отмена
    последней отметки (задача 2.4, design D12); текстовый ввод задним
    числом в фазе WAIT_EVENT_TIME (задача 4.1, design D10); кнопки
    §7.13 «Изменить»/«Отменить» (задача 4.4, design D11). Дальнейшие
    задачи добавляют обработчики сюда же.
    """
    from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

    from src.handlers import backfill, common, events, hint, settings_screen, start, undo

    application.add_handler(CommandHandler("start", start.start_command))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND, backfill.backfill_time_message
        )
    )
    # Хотфикс BUG-002: кнопки постоянной reply-панели — в ОТДЕЛЬНОЙ группе
    # (group=1), иначе (в группе 0) этот MessageHandler перехватил бы ВСЕ
    # текстовые апдейты у себя и диалог ввода времени backfill.py (группа 0)
    # перестал бы получать текст. В разных группах апдейт получают оба;
    # reply_button_message реагирует ТОЛЬКО на тексты кнопок панели,
    # прочий текст — молча.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND, common.reply_button_message
        ),
        group=1,
    )
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
    # «⏰ Подсказка сейчас» (add-recommendations 2.2, ТЗ §10.3, design D17).
    application.add_handler(
        CallbackQueryHandler(hint.hint_callback, pattern=r"^status:show$")
    )
    application.add_handler(
        CallbackQueryHandler(
            backfill.conflict_callback,
            pattern=r"^conflict:(overwrite|cancel)$",
        )
    )
    # ⚙️ Настройки (add-settings-history 1.2, ТЗ §10.2–10.4, design D21):
    # команда /settings, колбэки settings:show / settings:edit:<key> /
    # settings:cancel — единая точка settings_callback.
    application.add_handler(
        CommandHandler("settings", settings_screen.settings_command)
    )
    application.add_handler(
        CallbackQueryHandler(
            settings_screen.settings_callback,
            pattern=settings_screen.CALLBACK_PATTERN,
        )
    )
    # Ввод значения настройки — в ОТДЕЛЬНОЙ группе (group=2): группа 0
    # занята диалогом backfill (матчит весь текст), группа 1 — reply-панель.
    # Вне фазы WAIT_SETTING_VALUE обработчик молча пропускает текст.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            settings_screen.setting_value_message,
        ),
        group=2,
    )
