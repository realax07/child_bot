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
#   - распознано (прошедшее время) → конфликт §7.13 (задача 4.4, design
#     D11) либо подтверждение; СОЗДАНИЕ СОБЫТИЯ при успехе и ответ §7.14 —
#     задача 4.5 (TODO), фаза снята;
#   - мусор (None) → §7.12 с повторным запросом, фаза сохраняется (FR-16);
#   - будущее (FUTURE) → предупреждение без записи, фаза сохраняется
#     (FR-20, решение №4: спорные времена — предупреждение).
#
# Задача 4.3: глубина ввода (FR-17, design D14) — глубина читается из
# settings (backfill_depth_days, через SettingsService, без кэша) и
# передаётся парсеру; слишком старое время (TOO_OLD) → §7.15 без записи,
# фаза снята (диалог завершён). Задача 4.4 (FR-18, design D11): конфликт
# (пересечение интервала / нарушение порядка) → §7.13 с кнопками
# [Изменить] (conflict:overwrite) / [Отменить] (conflict:cancel);
# «Изменить» → атомарная перезапись целевого события + уведомление
# (ОВ-ПО-1), «Отменить» → данные не меняются. Во ВСЕХ терминальных ветках
# фазы диалога сняты, включая PENDING_KIND_KEY после TOO_OLD
# (notes review-001-4.3).
#
# FR-15: выбор «Ввести время вручную» запрашивает время текстом §7.11.
#
# Слой handlers (design D1): единственное обращение к сервисам — get_user
# (пояс пользователя для парсера, design D4), SettingsService (глубина
# ввода, design D14), detect_conflict / overwrite_event (конфликты §7.13,
# задача 4.4); без repositories и SQL. Нормализация пояса — в парсере
# (resolve_tz), собственного fallback здесь больше нет (minors review-001-4.2).

from __future__ import annotations

from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src import texts
from src.handlers import common
from src.handlers.events import (
    OBSERVER_DENIED,
    PENDING_KIND_KEY,
    WAIT_EVENT_TIME_KEY,
)
from src.services.backfill_parser import FUTURE, TOO_OLD, parse_time_input, resolve_tz
from src.services.event import (
    ROLE_OBSERVER,
    detect_conflict,
    get_user,
    overwrite_event,
)
from src.services.settings import SettingsService
from src.services.state import KIND_SLEEP, KIND_WAKE

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

# Временное подтверждение задачи 4.2: время распознано и принято, конфликта
# нет. Запись события и успех §7.14 — задача 4.5 (TODO ниже).
TIME_ACCEPTED = "✅ Время принято: {local}."

# callback_data кнопок §7.13 (ТЗ §10.3; задача 4.4, design D11).
CALLBACK_CONFLICT_OVERWRITE = "conflict:overwrite"
CALLBACK_CONFLICT_CANCEL = "conflict:cancel"

# Фаза подтверждения перезаписи (design D10: фазы диалога — в user_data):
# целевое событие конфликта и момент нового ввода. Вид нового события —
# в PENDING_KIND_KEY (см. events.py).
CONFIRM_OVERWRITE_KEY = "confirm_overwrite"


def is_waiting_for_time(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Активна ли фаза ожидания текстового ввода времени (design D10)."""
    data = getattr(context, "user_data", None)
    return isinstance(data, dict) and bool(data.get(WAIT_EVENT_TIME_KEY))


def _clear_wait_phase(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Снять фазу ожидания времени (диалог разбора завершён)."""
    data = getattr(context, "user_data", None)
    if isinstance(data, dict):
        data.pop(WAIT_EVENT_TIME_KEY, None)


def _pending_kind(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Вид события из фазы диалога; None — фазы нет/вид невалиден."""
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return None
    kind = data.get(PENDING_KIND_KEY)
    return kind if kind in (KIND_SLEEP, KIND_WAKE) else None


def _clear_conflict_phases(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Снять ВСЕ фазы диалога: конфликт разрешён (задача 4.4)."""
    data = getattr(context, "user_data", None)
    if isinstance(data, dict):
        data.pop(CONFIRM_OVERWRITE_KEY, None)
        data.pop(PENDING_KIND_KEY, None)
        data.pop(WAIT_EVENT_TIME_KEY, None)


def _conflict_markup() -> InlineKeyboardMarkup:
    """Клавиатура §7.13: [Изменить] / [Отменить] (FR-18, design D11)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    texts.BTN_CONFLICT_OVERWRITE,
                    callback_data=CALLBACK_CONFLICT_OVERWRITE,
                )
            ],
            [
                InlineKeyboardButton(
                    texts.BTN_CONFLICT_CANCEL,
                    callback_data=CALLBACK_CONFLICT_CANCEL,
                )
            ],
        ]
    )


def _local_hhmm(utc_moment: str | datetime, tz_name: str | None) -> str:
    """UTC-момент (строка БД или datetime) → «ЧЧ:ММ» в поясе пользователя.

    Пояс нормализует resolve_tz (design D4, minors review-001-4.2) —
    собственного fallback здесь нет.
    """
    if isinstance(utc_moment, str):
        utc_moment = datetime.strptime(
            utc_moment, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    return utc_moment.astimezone(resolve_tz(tz_name or "")).strftime("%H:%M")


async def backfill_time_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Текстовый ввод в фазе WAIT_EVENT_TIME (задачи 4.1–4.2, design D10).

    Вне диалога (фаза не активна) текст игнорируется молча — bot не
    реагирует на произвольные сообщения пользователей. В фазе ввод
    разбирается парсером (задачи 4.2–4.3): прошедшее время в пределах
    глубины ввода → подтверждение (создание события — 4.4/4.5), мусор →
    §7.12, будущее → предупреждение, старше backfill_depth_days → §7.15
    (FR-17, фаза снята — диалог завершён).
    """
    if update.message is None or update.effective_user is None:
        return
    if not is_waiting_for_time(context):
        # Текст вне диалога — не мешает (критерий задачи 4.1).
        return

    conn = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
        # Глубина ввода — из settings при каждом использовании (FR-24,
        # design D14), без кэша и хардкода дефолта.
        depth = int(
            SettingsService(conn).get(user["id"], "backfill_depth_days")
        )
    finally:
        conn.close()

    parsed = parse_time_input(
        update.message.text or "",
        user["timezone"],
        datetime.now(timezone.utc),
        max_age_days=depth,
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
    if parsed is TOO_OLD:
        # FR-17 (design D14): старше backfill_depth_days — §7.15 дословно,
        # без записи; фаза снята — диалог завершён (решение ПМ, задача 4.3).
        # Терминальная ветка: PENDING_KIND_KEY тоже снят (notes
        # review-001-4.3 — фазы не переживают завершение диалога).
        kind = _pending_kind(context)
        _clear_wait_phase(context)
        data = getattr(context, "user_data", None)
        if isinstance(data, dict) and kind is not None:
            data.pop(PENDING_KIND_KEY, None)
        await update.message.reply_text(texts.BACKFILL_TOO_OLD)
        return

    # Распознанное прошедшее время в пределах глубины (FR-16, FR-17).
    if not isinstance(parsed, datetime):
        # Все исходы сентинелов перечислены выше; страховка от новых
        # исходов парсера вместо assert (minors review-001-4.2).
        _clear_wait_phase(context)
        return
    when_utc = parsed.strftime("%Y-%m-%d %H:%M:%S")
    kind = _pending_kind(context)

    # Задача 4.4 (FR-18, design D11): детект конфликта — пересечение с
    # существующими событиями или нарушение порядка sleep/wake.
    if kind is not None:
        conn = common.open_conn(context)
        try:
            conflict = detect_conflict(conn, user["id"], kind, when_utc)
        finally:
            conn.close()
        if conflict is not None:
            # §7.13 (ТЗ §7.13 / texts.BACKFILL_CONFLICT) с кнопками
            # [Изменить] / [Отменить]; фаза разбора снята, целевое событие
            # и вид нового — в user_data до решения пользователя.
            _clear_wait_phase(context)
            data = getattr(context, "user_data", None)
            if isinstance(data, dict):
                data[CONFIRM_OVERWRITE_KEY] = {
                    "event_id": conflict["id"],
                    "when_utc": when_utc,
                }
            await update.message.reply_text(
                texts.backfill_conflict_text(
                    texts.KIND_LABELS[conflict["event_type"]],
                    _local_hhmm(conflict["timestamp"], user["timezone"]),
                ),
                reply_markup=_conflict_markup(),
            )
            return

    # Конфликта нет (или фаза вида потеряна — перезапуск процесса): пока
    # только подтверждение и завершение фазы разбора. Запись события с
    # ответом §7.14 (is_backfilled=1) — задача 4.5 (TODO).
    _clear_wait_phase(context)
    local = parsed.astimezone(resolve_tz(user["timezone"])).strftime(
        "%d.%m %H:%M"
    )
    await update.message.reply_text(TIME_ACCEPTED.format(local=local))


async def conflict_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Кнопки §7.13: «Изменить» (conflict:overwrite) / «Отменить» (cancel).

    FR-18, design D11, ОВ-ПО-1: «Изменить» — атомарная перезапись целевого
    события видом и временем нового ввода (без дублей) + уведомление;
    «Отменить» — данные не меняются. В обоих случаях фазы диалога сняты.
    Observer — отказ без изменения данных (design D13). Потерянная фаза
    (перезапуск процесса / чужое окно) — безопасный дефолт: ничего не
    перезаписываем, клавиатура перерисована (design D5-стиль).
    """
    query = update.callback_query
    if query is None or update.effective_user is None:
        return
    if query.data not in (CALLBACK_CONFLICT_OVERWRITE, CALLBACK_CONFLICT_CANCEL):
        return

    conn = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
        data = getattr(context, "user_data", None)
        pending = data.get(CONFIRM_OVERWRITE_KEY) if isinstance(data, dict) else None
        kind = _pending_kind(context)

        # design D13: наблюдателю отметки недоступны — данные не меняются.
        if user["role"] == ROLE_OBSERVER:
            _clear_conflict_phases(context)
            await query.answer(text=OBSERVER_DENIED, show_alert=False)
            return

        # Фазы нет / неполны — перезаписывать нечем (безопасный дефолт).
        if (
            kind is None
            or not isinstance(pending, dict)
            or "event_id" not in pending
            or "when_utc" not in pending
        ):
            _clear_conflict_phases(context)
            await query.answer()
            await common.redraw_menu(update, conn, user["id"])
            return

        if query.data == CALLBACK_CONFLICT_CANCEL:
            # «Отменить» (FR-18): данные не меняются, фазы очищены.
            _clear_conflict_phases(context)
            await query.answer()
            if update.effective_message is not None:
                await update.effective_message.reply_text(
                    texts.BACKFILL_CONFLICT_CANCELLED
                )
            return

        # «Изменить» (FR-18, ОВ-ПО-1): атомарная перезапись целевого
        # события (тип и время из ввода) + уведомление; без дублей.
        overwritten = overwrite_event(
            conn, pending["event_id"], kind, pending["when_utc"]
        )
        _clear_conflict_phases(context)
        await query.answer()
        if update.effective_message is None:
            return
        if overwritten:
            await update.effective_message.reply_text(
                texts.BACKFILL_OVERWRITTEN.format(
                    label=texts.KIND_LABELS[kind],
                    local=_local_hhmm(pending["when_utc"], user["timezone"]),
                )
            )
        else:
            # Целевой записи уже нет (удалена параллельно) — данных не
            # меняем, меню перерисовано по актуальному состоянию.
            await common.redraw_menu(update, conn, user["id"])
    finally:
        conn.close()
