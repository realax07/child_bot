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

from src import texts


async def help_command(update, context) -> None:
    """Команда /help: дословный текст §7.28 (задача 3.2, design D26).

    Доступна в ЛЮБОМ состоянии (бодрствует/спит, внутри FSM-диалогов
    backfill/настройки): обработчик ТОЛЬКО отвечает текстом — не пишет
    в БД, не меняет настройки и не трогает фазы в context.user_data
    (открытые диалоги остаются активны — справка поверх).
    """
    message = update.effective_message
    if message is not None:
        await message.reply_text(texts.HELP)


# ------------------------------------ /delete_all (задача 3.3, FR-54, D25)

# Фаза FSM подтверждения удаления (design D10-стиль: фазы диалога — в
# context.user_data, один процесс). Подтверждение — ОТДЕЛЬНЫЙ шаг
# КНОПКОЙ (ОВ-5.3, решение продакта 01.11; текст-пароль отклонён).
CONFIRM_DELETE_ALL_KEY = "confirm_delete_all"

# callback-контракт кнопок §7.26 (§10.3-стиль).
CALLBACK_DELETE_ALL_YES = "delete_all:yes"
CALLBACK_DELETE_ALL_NO = "delete_all:no"
DELETE_ALL_CALLBACK_PATTERN = r"^delete_all:(yes|no)$"


def _delete_all_markup():
    """Кнопки §7.26 (ред. ОВ-5.3): [Да, удалить всё] / [Отмена]."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    texts.BTN_DELETE_ALL_YES, callback_data=CALLBACK_DELETE_ALL_YES
                )
            ],
            [
                InlineKeyboardButton(
                    texts.BTN_DELETE_ALL_CANCEL, callback_data=CALLBACK_DELETE_ALL_NO
                )
            ],
        ]
    )


def _pop_confirm_phase(context) -> bool:
    """Снять фазу подтверждения /delete_all; True — фаза была активна."""
    data = getattr(context, "user_data", None)
    if isinstance(data, dict):
        return data.pop(CONFIRM_DELETE_ALL_KEY, None) is not None
    return False


def _set_confirm_phase(context) -> None:
    """Поставить фазу подтверждения, ГАСЯ конкурирующие диалоги.

    Взаимное исключение фаз (по образцу settings_screen._clear_backfill_phases,
    major review-001-1.2): вход в подтверждение удаления — начало НОВОЙ
    фазы; фазы backfill-диалога (группа 0) и диалога значения настройки
    (группа 2) снимаются целиком. Ключи — импортированные константы
    владельцев, копий нет (design D14-стиль). Импорт settings_screen
    ленивый — критерий готовности 1.1: `import src.handlers` не тянет
    repositories (см. шапку модуля).
    """
    from src.handlers import settings_screen

    _pop_confirm_phase(context)
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return
    for phase_key in (
        *settings_screen.BACKFILL_PHASE_KEYS,
        settings_screen.WAIT_SETTING_VALUE,
    ):
        data.pop(phase_key, None)
    data[CONFIRM_DELETE_ALL_KEY] = True


def _clear_all_dialog_phases(context) -> None:
    """После удаления данных открытых диалогов быть не должно: снять ВСЕ
    фазы (подтверждение, backfill, значение настройки)."""
    from src.handlers import settings_screen

    _pop_confirm_phase(context)
    data = getattr(context, "user_data", None)
    if isinstance(data, dict):
        for phase_key in (
            *settings_screen.BACKFILL_PHASE_KEYS,
            settings_screen.WAIT_SETTING_VALUE,
        ):
            data.pop(phase_key, None)


async def delete_all_command(update, context) -> None:
    """Команда /delete_all: подтверждение §7.26 кнопкой (задача 3.3, D25).

    НИКАКИХ удалений здесь нет — только вопрос §7.26 с кнопками
    [Да, удалить всё]/[Отмена] и фаза CONFIRM_DELETE_ALL_KEY в user_data.
    Вход в подтверждение гасит конкурирующие FSM-диалоги (обе стороны
    взаимного исключения — см. guard-обработчики группы 3 ниже).
    Наблюдателю — отказ без фазы (design D13: delete_all — только parent).
    """
    message = update.effective_message
    if message is None or update.effective_user is None:
        return

    from src.handlers import common
    from src.services.access import deny_message, is_observer, get_user

    conn = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
        if is_observer(conn, user["id"]):
            await message.reply_text(deny_message())
            return
    finally:
        conn.close()

    _set_confirm_phase(context)
    await message.reply_text(
        texts.DELETE_ALL_CONFIRM, reply_markup=_delete_all_markup()
    )


async def delete_all_callback(update, context) -> None:
    """Колбэк кнопок §7.26 (ОВ-5.3): подтверждение КНОПКОЙ в отдельном шаге.

    «Да, удалить всё» — удаление ТОЛЬКО при активной фазе подтверждения:
    любой другой ввод фазу гасит (guard-обработчики группы 3), устаревшая
    кнопка из истории чата удаление НЕ вызывает (guard-стиль design D5) —
    вместо §7.27 уходит свежее меню. Удаление — EventService.delete_all
    (одна транзакция: sleep_events, settings, settings_history, users;
    только свои данные), после — §7.27 дословно; повторный /start
    пересеивает 13 дефолтов (D14). «Отмена» — ничего не удалено, фаза
    снята, бот возвращается в меню; §7.27 НЕ отправляется.
    """
    query = update.callback_query
    message = update.effective_message
    if query is None or message is None or update.effective_user is None:
        return

    from src.handlers import common
    from src.services.access import deny_message, get_user, is_observer
    from src.services.event import delete_all as delete_all_service

    data = query.data or ""
    was_pending = _pop_confirm_phase(context)

    conn = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
        if data == CALLBACK_DELETE_ALL_NO:
            # Отмена: меню без изменений, §7.27 не отправляется.
            await query.answer()
            await common.redraw_menu(update, conn, user["id"])
            return
        # «Да, удалить всё» (и любой прочий data по шаблону — нет):
        # без активной фазы подтверждения удаления НЕТ (устаревшая кнопка).
        if not was_pending:
            await query.answer()
            await common.redraw_menu(update, conn, user["id"])
            return
        if is_observer(conn, user["id"]):
            await query.answer()
            await message.reply_text(deny_message())
            return
        delete_all_service(conn, user["id"])
    finally:
        conn.close()

    # После удаления открытые диалоги не имеют смысла — все фазы сняты.
    _clear_all_dialog_phases(context)
    await query.answer()
    await message.reply_text(texts.DELETE_ALL_DONE)


async def _discard_delete_confirm_update(update, context) -> None:
    """Гашение фазы подтверждения ЛЮБЫМ другим вводом (§7.26, ОВ-5.3).

    Guard-обработчик ОТДЕЛЬНОЙ группы (group=3): PTB доставляет апдейт
    во ВСЕ группы, поэтому любой чужой текст/команда/колбэк гасит фазу
    БЕЗ удаления, не мешая собственным диалогам остальных групп (паттерн
    разделения групп BUG-002). Сам /delete_all не гасится — фазу ставит
    он. settings_screen.py/backfill.py вне зоны задачи 3.3, поэтому
    обратная сторона взаимного исключения (их входы гасят ЭТУ фазу)
    реализована пассивно здесь: их нажатия/ввода проходят через группу 3.
    """
    message = update.effective_message
    if message is not None:
        text = message.text or ""
        if text.startswith("/delete_all"):
            return  # собственная команда — фазу только что поставила группа 0
    _pop_confirm_phase(context)


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

    from src.handlers import backfill, common, events, hint, history, settings_screen, start, undo

    application.add_handler(CommandHandler("start", start.start_command))
    # Справка /help (add-settings-history 3.2, ТЗ §7.28/§10.2, design
    # D26): дословный текст §7.28 в любом состоянии — БД, состояние,
    # настройки и FSM-фазы не трогаются (справка поверх). Регистрация
    # рядом с /start (D26); хендлер — в этом же модуле, чтобы
    # `import src.handlers` оставался ленивым по repositories.
    application.add_handler(CommandHandler("help", help_command))
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
    # 📊 История за неделю (add-settings-history 2.1, ТЗ §10.2–10.3,
    # design D22): команда /history и колбэк history:week — единый
    # экран §7.20 + блок сегодня; событий не создаёт.
    application.add_handler(CommandHandler("history", history.history_command))
    application.add_handler(
        CallbackQueryHandler(
            history.history_callback, pattern=history.CALLBACK_PATTERN
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
    # 🗑 /delete_all (add-settings-history 3.3, ТЗ §7.26–7.27, §10.2,
    # design D25, ОВ-5.3): команда показывает подтверждение §7.26 и
    # ставит фазу CONFIRM_DELETE_ALL_KEY; колбэк кнопок
    # delete_all:yes|no удаляет данные ТОЛЬКО при активной фазе.
    application.add_handler(CommandHandler("delete_all", delete_all_command))
    application.add_handler(
        CallbackQueryHandler(
            delete_all_callback, pattern=DELETE_ALL_CALLBACK_PATTERN
        )
    )
    # Гашение фазы подтверждения ЛЮБЫМ другим вводом (ОВ-5.3: «любой
    # другой ввод гасит состояние подтверждения БЕЗ удаления») —
    # ОТДЕЛЬНАЯ группа 3: PTB доставит апдейт и сюда (текст — включая
    # команды, и любой колбэк), фаза снимается, удаления нет. Обратная
    # сторона взаимного исключения с диалогами настроек/backfill, чьи
    # модули вне зоны задачи 3.3 (см. _discard_delete_confirm_update).
    application.add_handler(
        MessageHandler(filters.TEXT, _discard_delete_confirm_update),
        group=3,
    )
    application.add_handler(
        CallbackQueryHandler(
            _discard_delete_confirm_update, pattern=r".*"
        ),
        group=3,
    )
