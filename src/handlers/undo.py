# Обработчик кнопки «↩️ Отменить последнее» (change-пакет add-core-diary,
# задача 2.4).
#
# design D12: callback undo:last удаляет ПОСЛЕДНЮЮ запись sleep_events —
# по created_at, затем id (задним числом введённое событие с ранним
# timestamp всё равно последнее по факту записи); после удаления состояние
# и меню пересчитываются (design D5: common.redraw_menu читает БД заново).
# Нет событий → сообщение «отменять нечего», данные НЕ меняются
# (delete_last возвращает False, записи не было). Отмена после перезаписи
# (D11 «Изменить») удаляет перезаписанную запись целиком — прежнее значение
# не восстанавливается (следствие delete_last по id).
# Guard (design D5): «Отменить последнее» валидна в обоих состояниях
# (state.is_stale_callback с KIND_UNDO всегда False), поэтому отдельной
# проверки устаревания здесь не требуется — пустые данные отсекает сам
# delete_last. Состояние всё равно перечитывается из БД при перерисовке.
# design D13 (FR-22): наблюдателю отметки недоступны — нажатие игнорируется
# с вежливым сообщением, удаления нет.
# FR-21: отменяется только последняя отметка; более ранние события не
# удаляются.
#
# Слой handlers (design D1): сервисы — да. Временный мост (как get_user в
# services/event.py до задачи 5.1, design D13): удаление идёт через
# SleepEventsRepo напрямую — отдельного undo-сервиса в пакете 1 нет,
# контракт уже покрыт юнит-тестами репозитория (задача 1.3).

from __future__ import annotations

import sqlite3

from telegram import Update
from telegram.ext import ContextTypes

from src import texts
from src.handlers import common
from src.repositories.sleep_events import SleepEventsRepo
from src.services import access
from src.services.event import get_user

# callback_data кнопки (ТЗ §6, design D2/§10.3 — контракты задач 2.2/2.4).
CALLBACK_UNDO_LAST = "undo:last"

# Служебные тексты (вне texts.py §7 — как OBSERVER_DENIED в events.py).
UNDO_DONE = "↩️ Последняя отметка отменена."
NOTHING_TO_UNDO = "Отменять нечего: записей пока нет."

# Сообщение наблюдателю — единый текст texts.OBSERVER_DENIED через
# services.access.deny_message() (задача 5.1, design D13, FR-22;
# review-001-2.4, note 1: дубль литерала убран). Имя оставлено как алиас
# для обратной совместимости импортов (тесты).
OBSERVER_DENIED = texts.OBSERVER_DENIED


async def undo_last_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Нажатие «↩️ Отменить последнее» — удалить последнюю запись (FR-21).

    Успех: удаление + перерисовка меню по свежему состоянию (ребёнок
    возвращается к состоянию до отметки) + подтверждение. Пустая БД:
    данные не меняются, сообщение «отменять нечего». Observer: вежливый
    отказ без удаления (design D13).
    """
    query = update.callback_query
    if query is None:
        return
    if query.data != CALLBACK_UNDO_LAST or update.effective_user is None:
        return

    conn: sqlite3.Connection = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)

        # design D13 (задача 5.1, FR-22): наблюдателю отметки недоступны —
        # нажатие игнорируется (проверка роли — services.access).
        if access.is_observer(conn, user["id"]):
            await query.answer(text=access.deny_message(), show_alert=False)
            return

        deleted = SleepEventsRepo(conn).delete_last(user["id"])

        # answerCallbackQuery до redraw/reply (review-001-2.3, note 1):
        # при падении edit/reply пользователь не увидит «часики».
        await query.answer()
        # Меню/состояние пересчитываются заново из БД (design D12, FR-6).
        await common.redraw_menu(update, conn, user["id"])

        if update.effective_message is not None:
            await update.effective_message.reply_text(
                UNDO_DONE if deleted else NOTHING_TO_UNDO
            )
    finally:
        conn.close()
