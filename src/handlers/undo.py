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
# удаляются. В ред. волны 6 (PB-4, задача 6.2): после успешной отмены
# бот сообщает, ЧТО удалено (тип + локальное время, прочитанные ДО
# delete_last), и мини-состояние дневника — что осталось (кол-во записей
# за сегодня + факты дня в формате PB-3; строки строит единая точка
# handlers.events.day_summary_lines → calculation.day_facts_lines).
#
# Слой handlers (design D1): сервисы — да. Временный мост (как get_user в
# services/event.py до задачи 5.1, design D13): удаление идёт через
# SleepEventsRepo напрямую — отдельного undo-сервиса в пакете 1 нет,
# контракт уже покрыт юнит-тестами репозитория (задача 1.3).

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from src import texts
from src.handlers import common
from src.handlers.events import day_summary_lines
from src.repositories.sleep_events import SleepEventsRepo
from src.services import access
from src.services import scheduler as reminder_scheduler
from src.services.backfill_parser import resolve_tz
from src.services.calculation import DEFAULT_DAY_START, day_page_of
from src.services.event import get_user, list_raw_events
from src.services.settings import SettingsService

# callback_data кнопки (ТЗ §6, design D2/§10.3 — контракты задач 2.2/2.4).
CALLBACK_UNDO_LAST = "undo:last"

# Служебные тексты (вне texts.py §7): NOTHING_TO_UNDO — без изменений
# (PB-4). Подтверждение отмены (PB-4, задача 6.2) строит undo_done_text
# ниже: ЧТО удалено (тип + локальное время, прочитанные ДО delete) +
# мини-состояние дневника (что осталось) в формате фактов PB-3.
NOTHING_TO_UNDO = "Отменять нечего: записей пока нет."

# Сообщение наблюдателю — единый текст texts.OBSERVER_DENIED через
# services.access.deny_message() (задача 5.1, design D13, FR-22;
# review-001-2.4, note 1: дубль литерала убран). Имя оставлено как алиас
# для обратной совместимости импортов (тесты).
OBSERVER_DENIED = texts.OBSERVER_DENIED


def _notify_after_delete(deleted: bool) -> None:
    """Точка ре-шедула напоминаний после удаления записи (FR-41, design D19).

    add-recommendations 3.1: delete — третья мутация событий (ТЗ §8.1).
    Удаления не было (пустая БД) — план не менялся, ре-шедул не нужен.
    Без активного планировщика (тесты) — no-op.
    """
    if deleted:
        reminder_scheduler.notify_events_changed()


def undo_done_text(
    conn: sqlite3.Connection, user: dict, event: dict
) -> str:
    """Подтверждение успешной отмены (FR-21 в ред. волны 6, PB-4).

    «Отменено: {label} {ЧЧ:ММ}» — вид и ЛОКАЛЬНОЕ время удалённой записи
    (event прочитан ДО delete_last, design D12: last по created_at, затем
    id); ниже — мини-состояние дневника: что осталось после удаления
    (_leftover_state ниже, формат фактов PB-3). Для пустого дня — строка
    «Сегодня записей пока нет» (texts.UNDO_LEFTOVER_EMPTY).
    """
    label = texts.KIND_LABELS.get(
        event["event_type"], event["event_type"]
    )
    local_hhmm = _local_hhmm(event["timestamp"], user["timezone"])
    return texts.undo_done_text(label, local_hhmm, _leftover_state(conn, user))


def _local_hhmm(utc_moment: str | datetime, tz_name: str | None) -> str:
    """UTC-момент (строка БД «YYYY-MM-DD HH:MM:SS») → «ЧЧ:ММ» в поясе
    пользователя (design D4) — как backfill._local_hhmm / события дня."""
    if isinstance(utc_moment, str):
        utc_moment = datetime.strptime(
            utc_moment, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    local = utc_moment.astimezone(resolve_tz(tz_name or "")).replace(
        tzinfo=None
    )
    return f"{local:%H:%M}"


def _settings_day_start(raw: str) -> time:
    """«ЧЧ:ММ» настройки day_start → time; фолбэк — дефолт расчётов (D8)
    (тот же парсинг, что events._settings_day_start; строки сводки при
    этом строит day_summary_lines — единая точка сборки, PB-3)."""
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except (AttributeError, ValueError):
        return DEFAULT_DAY_START


def _leftover_state(conn: sqlite3.Connection, user: dict) -> str:
    """Мини-состояние дневника после отмены (PB-4, задача 6.2).

    Сколько записей осталось за сегодня (страница дня момента «сейчас»,
    design D8) + сводка фактов дня в формате PB-3 — строки строит
    day_summary_lines (единая точка сборки волны 6; никаких новых
    абстрактных сумм). Пустой день — texts.UNDO_LEFTOVER_EMPTY.
    """
    user_id = user["id"]
    tz_name = user["timezone"]
    settings = SettingsService(conn)
    day_start = _settings_day_start(str(settings.get(user_id, "day_start")))

    ref_utc = datetime.now(timezone.utc)
    # Полный локальный момент «сейчас» — как events._local_naive:
    # aware UTC → naive local в поясе пользователя (design D4).
    ref_local = ref_utc.astimezone(resolve_tz(tz_name or "")).replace(
        tzinfo=None
    )
    page = day_page_of(ref_local, day_start)
    window_start = datetime.combine(page, day_start)
    window_end = window_start + timedelta(days=1)

    count = 0
    for row in list_raw_events(conn, user_id):
        ts = row["timestamp"]
        moment = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        moment = moment.astimezone(resolve_tz(tz_name or "")).replace(
            tzinfo=None
        )
        if window_start <= moment < window_end:
            count += 1
    if count == 0:
        return texts.UNDO_LEFTOVER_EMPTY
    summary = day_summary_lines(conn, user, ref_utc)
    return f"Записей за сегодня: {count}.\n" + "\n".join(summary)


async def reply_undo_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    user: dict,
) -> None:
    """Кнопка «↩️ Отменить последнее» постоянной reply-панели (BUG-002).

    Та же логика, что undo_last_callback (callback undo:last, design
    D12): удаление последней записи по created_at, затем id; пустая БД —
    «отменять нечего», данные не меняются. Отличие: нажатие — текстовое
    сообщение, перерисовывать нечего, поэтому после подтверждения бот
    присылает свежее inline-меню отдельным сообщением (п. 3 плана
    BUG-002). Observer — вежливый отказ без удаления (design D13).
    """
    message = update.effective_message
    if message is None:
        return

    # design D13 (FR-22): наблюдателю отметки недоступны.
    if access.is_observer(conn, user["id"]):
        await message.reply_text(access.deny_message())
        return

    # PB-4 (FR-21): ЧТО удалено читается ДО delete_last (last по
    # created_at, затем id — design D12), сам контракт репозитория
    # (delete_last → bool) не меняется.
    last_event = SleepEventsRepo(conn).last(user["id"])
    deleted = SleepEventsRepo(conn).delete_last(user["id"])
    _notify_after_delete(deleted)
    confirmation = (
        undo_done_text(conn, user, last_event)
        if deleted and last_event is not None
        else NOTHING_TO_UNDO
    )
    await message.reply_text(confirmation)
    # Свежее inline-меню по состоянию после удаления (design D5, FR-6).
    await common.send_fresh_menu(update, conn, user["id"])


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

        # PB-4 (FR-21): тип и время удаляемой записи читаются ДО delete
        # (last по created_at, затем id — design D12); контракт
        # репозитория (delete_last → bool) не меняется.
        last_event = SleepEventsRepo(conn).last(user["id"])
        deleted = SleepEventsRepo(conn).delete_last(user["id"])
        _notify_after_delete(deleted)

        # answerCallbackQuery до redraw/reply (review-001-2.3, note 1):
        # при падении edit/reply пользователь не увидит «часики».
        await query.answer()
        # Меню/состояние пересчитываются заново из БД (design D12, FR-6).
        await common.redraw_menu(update, conn, user["id"])

        if update.effective_message is not None:
            await update.effective_message.reply_text(
                undo_done_text(conn, user, last_event)
                if deleted and last_event is not None
                else NOTHING_TO_UNDO
            )
    finally:
        conn.close()
