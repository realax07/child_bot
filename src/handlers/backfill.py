# Диалог ввода события задним числом — текстовый ввод времени
# (change-пакет add-core-diary, задачи 4.1–4.2).
#
# design D10: диалог без БД-FSM — фаза диалога живёт в context.user_data PTB
# (один процесс). Кнопка «⏪ Ввести время вручную» (events.py, time:manual)
# ставит фазу WAIT_EVENT_TIME и шлёт §7.11; этот модуль принимает текстовый
# ввод ТОЛЬКО пока фаза активна. Тексты вне диалога игнорируются без какой
# либо реакции — они не должны мешать остальным обработчикам и состоянию.
#
# Задача 4.2: разбор ввода парсером services/backfill_parser.parse_time_input
# (FR-16, FR-20; design D10):
#   - распознано (прошедшее время) → подтверждение; СОЗДАНИЕ СОБЫТИЯ —
#     задачи 4.4/4.5 (конфликты §7.13 и успех §7.14) — TODO, фаза снята;
#   - мусор (None) → §7.12 с повторным запросом, фаза сохраняется (FR-16);
#   - будущее (FUTURE) → предупреждение без записи, фаза сохраняется
#     (FR-20, решение №4: спорные времена — предупреждение).
#
# FR-15: выбор «Ввести время вручную» запрашивает время текстом §7.11.
#
# Слой handlers (design D1): единственное обращение к сервисам — get_user
# (пояс пользователя для парсера, design D4); без repositories и SQL.

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from src import texts
from src.handlers import common
from src.handlers.events import WAIT_EVENT_TIME_KEY
from src.services.backfill_parser import FUTURE, parse_time_input
from src.services.event import get_user

# Fallback пояса — как в events.py (design D4, аудитория бота).
_FALLBACK_TZ = "Europe/Moscow"

# Предупреждение FR-20 («распознанное время в будущем») — служебный текст
# вне ТЗ §7 (в texts.py §7.11–7.15 его нет); формулировка повторяет список
# форматов §7.11 — «предупреждение §7.11-стиля».
FUTURE_TIME_WARNING = (
    "⚠️ Это время ещё не наступило. Введите прошедшее время:\n"
    "\n"
    "- `10:50` — сегодня\n"
    "- `вчера 22:30` — вчера\n"
    "- `08.10 7:15` — конкретная дата"
)

# Временное подтверждение задачи 4.2: время распознано и принято. Запись
# события, конфликты §7.13 и успех §7.14 — задачи 4.4/4.5 (TODO ниже).
TIME_ACCEPTED = "✅ Время принято: {local}."


def is_waiting_for_time(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Активна ли фаза ожидания текстового ввода времени (design D10)."""
    data = getattr(context, "user_data", None)
    return isinstance(data, dict) and bool(data.get(WAIT_EVENT_TIME_KEY))


def _user_tz(tz_name: str | None) -> ZoneInfo:
    """Пояс пользователя: имя из БД или fallback (design D4)."""
    try:
        return ZoneInfo(tz_name) if tz_name else ZoneInfo(_FALLBACK_TZ)
    except Exception:  # некорректное имя пояса в БД — аудитория бота
        return ZoneInfo(_FALLBACK_TZ)


def _clear_wait_phase(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Снять фазу ожидания времени (диалог разбора завершён)."""
    data = getattr(context, "user_data", None)
    if isinstance(data, dict):
        data.pop(WAIT_EVENT_TIME_KEY, None)


async def backfill_time_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Текстовый ввод в фазе WAIT_EVENT_TIME (задачи 4.1–4.2, design D10).

    Вне диалога (фаза не активна) текст игнорируется молча — bot не
    реагирует на произвольные сообщения пользователей. В фазе ввод
    разбирается парсером (задача 4.2): прошедшее время → подтверждение
    (создание события — 4.4/4.5), мусор → §7.12, будущее → предупреждение.
    """
    if update.message is None or update.effective_user is None:
        return
    if not is_waiting_for_time(context):
        # Текст вне диалога — не мешает (критерий задачи 4.1).
        return

    conn = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
    finally:
        conn.close()

    zone = _user_tz(user["timezone"])
    parsed = parse_time_input(
        update.message.text or "", zone, datetime.now(timezone.utc)
    )

    if parsed is None:
        # FR-16: мусор → §7.12 с повторным запросом; фаза сохраняется.
        await update.message.reply_text(texts.BACKFILL_BAD_FORMAT)
        return
    if parsed is FUTURE:
        # FR-20 (решение №4): время ещё не наступило — предупреждение без
        # записи; фаза сохраняется, пользователь вводит время заново.
        await update.message.reply_text(FUTURE_TIME_WARNING)
        return

    # Распознанное прошедшее время (FR-16). TODO(4.4/4.5): детект конфликтов
    # §7.13 (design D11) и запись события с ответом §7.14 (is_backfilled=1);
    # здесь пока только подтверждение и завершение фазы разбора. Вид события
    # (PENDING_KIND_KEY) сохранён в context.user_data для задач 4.4/4.5.
    _clear_wait_phase(context)
    assert isinstance(parsed, datetime)  # сузить после проверок None/FUTURE
    local = parsed.astimezone(zone).strftime("%d.%m %H:%M")
    await update.message.reply_text(TIME_ACCEPTED.format(local=local))
