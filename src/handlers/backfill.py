# Диалог ввода события задним числом — текстовый ввод времени
# (change-пакет add-core-diary, задача 4.1).
#
# design D10: диалог без БД-FSM — фаза диалога живёт в context.user_data PTB
# (один процесс). Кнопка «⏪ Ввести время вручную» (events.py, time:manual)
# ставит фазу WAIT_EVENT_TIME и шлёт §7.11; этот модуль принимает текстовый
# ввод ТОЛЬКО пока фаза активна. Тексты вне диалога игнорируются без какой
# либо реакции — они не должны мешать остальным обработчикам и состоянию.
#
# Пока парсера нет (задача 4.2, services/backfill_parser.py) — заглушка:
# текст получен и фаза сохранена, никакого ответа пользователю не
# отправляется (§7.12/§7.13/§7.14 появятся в задачах 4.2–4.5).
#
# FR-15: выбор «Ввести время вручную» запрашивает время текстом §7.11.
#
# Слой handlers (design D1): доступ к БД в этой фазе не требуется —
# фаза диалога полностью в context.user_data.

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from src.handlers.events import WAIT_EVENT_TIME_KEY


def is_waiting_for_time(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Активна ли фаза ожидания текстового ввода времени (design D10)."""
    data = getattr(context, "user_data", None)
    return isinstance(data, dict) and bool(data.get(WAIT_EVENT_TIME_KEY))


async def backfill_time_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Текстовый ввод в фазе WAIT_EVENT_TIME (задача 4.1, design D10).

    Вне диалога (фаза не активна) текст игнорируется молча — bot не
    реагирует на произвольные сообщения пользователей. В фазе — заглушка
    до парсера задачи 4.2: ввод получен, фаза сохранена, ответа нет.
    """
    if update.message is None or update.effective_user is None:
        return
    if not is_waiting_for_time(context):
        # Текст вне диалога — не мешает (критерий задачи 4.1).
        return
    # Фаза активна: текст получен. Разбор форматов (HH:MM / «вчера ЧЧ:ММ» /
    # DD.MM ЧЧ:ММ) — задача 4.2; сейчас ничего не отвечаем и фазу сохраняем.
    return
