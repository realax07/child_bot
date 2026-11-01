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
#     D11) либо запись события + ответ §7.14 (задача 4.5), фаза снята;
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
# Задача 4.5 (FR-19, design D6/D10; ТЗ §7.14; решение ОВ-ПО-2): успешный
# ввод задним числом — и прямой записью (create_event, is_backfilled=1),
# и через «Изменить» в конфликте — отвечает §7.14: подтверждающая строка
# + обновлённая сводка за день по СТРАНИЦЕ ДНЯ вводимого времени. С волны 6
# (PB-3, задача 6.1) вместо абстрактных сумм сон/бодрствование — список
# фактических событий дня (формат продакта): сборка — events.day_summary_lines
# (ЕДИНАЯ точка для всех трёх подтверждений, без дублей форматирования);
# суммарный сон за день НЕ вычисляется (может превышать 24 ч из-за ночной
# страницы — design D8). Задача 2.3 (FR-38, design D20): подавление окон
# волны 6 снято — строка окна дневного сна добавляется К сводке
# (events.next_sleep_line — строка следующего сна страницы ввода; ред.
# BUG-005: утренний подъём — окно дневного сна, после дневного сна —
# конкретное время ухода).
#
# FR-15: выбор «Ввести время вручную» запрашивает время текстом §7.11.
#
# Задача 3.1 (add-settings-history, FR-51, design D23; ОВ-5.1 — решение
# продакта 01.11): ПАРНЫЙ ввод сна задним числом — ДВА ШАГА. Вид «сон» +
# «⏪ Ввести время вручную» (events.py) ставит WAIT_EVENT_TIME как раньше
# (контракт ветки 4.1) ПЛЮС маркер шага 1 WAIT_PAIR_SLEEP_KEY, вопрос —
# §7.11 дословно («Введите время в формате…» — вопрос «Когда заснул?»);
# ответ ставит фазу шага 2 (WAIT_PAIR_WAKE_KEY) и спрашивает «Когда
# проснулся?» (PAIR_ASK_WAKE). После шага 2 — ЕДИНАЯ проверка ПАРЫ
# (event.detect_pair_conflict, один вызов): порядок wake > sleep и
# пересечение интервала пары с существующими событиями; конфликт → §7.13
# [Изменить]/[Отмена] (CONFIRM_PAIR_KEY; «Изменить» переспрашивает пару
# [Изменить] переспрашивает пару с шага 1, «Отменить» гасит всё). Без
# конфликта пара создаётся АТОМАРНО (3.1-fix review-001-3.1:
# create_event_pair — ОБЕ вставки одной транзакцией, is_backfilled=1;
# полупара в БД невозможна; ре-шедул FR-41 — после коммита пары) и
# ответ §7.14-стиль (сводка PB-3 + окно, сборщики
# существующие). Глубина §7.15 — для ОБОИХ времён пары. Разбор по фазам
# БЕЗ маркера пары (легаси-пробы WAIT_EVENT_TIME + PENDING_KIND) идёт
# одиночной веткой — behavior существующего backfill сохранён (FR-51:
# незакрытый sleep → одиночная ветка wake спрашивает только пробуждение).
#
# Слой handlers (design D1): единственное обращение к сервисам — get_user
# (пояс пользователя для парсера, design D4), SettingsService (глубина
# ввода, design D14), detect_conflict / overwrite_event (конфликты §7.13,
# задача 4.4); без repositories и SQL. Нормализация пояса — в парсере
# (resolve_tz), собственного fallback здесь больше нет (minors review-001-4.2).

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src import texts
from src.handlers import common
from src.handlers.events import (
    CONFIRM_PAIR_KEY,
    OBSERVER_DENIED,
    PAIR_ASK_WAKE,
    PENDING_KIND_KEY,
    WAIT_EVENT_TIME_KEY,
    WAIT_PAIR_SLEEP_KEY,
    WAIT_PAIR_WAKE_KEY,
    _clear_pair_phases,
    day_summary_lines,
    next_sleep_line,
)
from src.services.backfill_parser import FUTURE, TOO_OLD, parse_time_input, resolve_tz
from src.services.event import (
    ROLE_OBSERVER,
    create_event,
    create_event_pair,
    detect_conflict,
    detect_pair_conflict,
    get_user,
    overwrite_event,
)
from src.services.settings import SettingsService
from src.services.state import KIND_SLEEP, KIND_WAKE

# callback_data кнопок §7.13 (ТЗ §10.3; задача 4.4, design D11).
CALLBACK_CONFLICT_OVERWRITE = "conflict:overwrite"
CALLBACK_CONFLICT_CANCEL = "conflict:cancel"

# Фаза подтверждения перезаписи (design D10: фазы диалога — в user_data):
# целевое событие конфликта и момент нового ввода. Вид нового события —
# в PENDING_KIND_KEY (см. events.py).
CONFIRM_OVERWRITE_KEY = "confirm_overwrite"

# Формат строки UTC БД (design D4) — единая константа модуля.
_TS_DB_FORMAT = "%Y-%m-%d %H:%M:%S"

# Отказ-текст при неудачной записи ПАРЫ (3.1-fix, review-001-3.1 major №1):
# при откате транзакции пары пользователь получает честный ответ —
# молчаливый проглот запрещён (полупары в БД нет, но фазы сняты и ввод
# не записан). Текст служебный (вне ТЗ §7) — держится в модуле обработчика
# (жёсткая зона фикса), как до переноса текстов задачи 4.2.
BACKFILL_PAIR_FAILED = (
    "⚠️ Не удалось записать сон — состояние изменилось. "
    "Попробуйте ещё раз."
)


def is_waiting_for_time(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Активна ли фаза ожидания текстового ввода времени (design D10)."""
    data = getattr(context, "user_data", None)
    return isinstance(data, dict) and bool(data.get(WAIT_EVENT_TIME_KEY))


def _pair_sleep_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Активна ли фаза шага 1 пары «Когда заснул?» (задача 3.1, D23)."""
    data = getattr(context, "user_data", None)
    return isinstance(data, dict) and bool(data.get(WAIT_PAIR_SLEEP_KEY))


def _pair_wake_phase(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """Фаза шага 2 пары: {sleep_utc}; None — фазы нет/фаза неполна.

    Неполная фаза (перезапуск процесса) гасится — безопасный дефолт без
    записи (design D10-стиль).
    """
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return None
    phase = data.get(WAIT_PAIR_WAKE_KEY)
    if isinstance(phase, dict) and "sleep_utc" in phase:
        return phase
    data.pop(WAIT_PAIR_WAKE_KEY, None)
    return None


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
    """Снять ВСЕ фазы диалога: разбор завершён (конфликт разрешён — 4.4,
    либо событие записано — 4.5, либо пара завершена — 3.1)."""
    data = getattr(context, "user_data", None)
    if isinstance(data, dict):
        data.pop(CONFIRM_OVERWRITE_KEY, None)
        data.pop(PENDING_KIND_KEY, None)
        data.pop(WAIT_EVENT_TIME_KEY, None)
        # Задача 3.1 (FR-51/D23): фазы парного ввода гасятся тем же
        # терминальным сбросом (единый хелпер — без дублей очистки).
        _clear_pair_phases(context)


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


def _backfill_success_text(
    conn: sqlite3.Connection, user: dict, when_utc: str
) -> str:
    """Ответ §7.14 (FR-19 в ред. волны 6, PB-3 + FR-38, задача 2.3/D20;
    ред. BUG-005): подтверждение + строка следующего сна + сводка дня.

    Сводка — фактические события дня СТРАНИЦЫ вводимого времени (design
    D6/D8), не сегодняшней, в формате продакта (Подъем / Заснул /
    Проснулся / Дневной сон / Отбой / Ночной сон + Бодрствование общее):
    сборка — events.day_summary_lines (единая точка всех трёх
    подтверждений); суммарный сон за день НЕ вычисляется (может
    превышать 24 ч из-за ночной страницы — D8). Подавление окон волны 6
    СНЯТО (задача 2.3): строка следующего сна добавляется К сводке
    (events.next_sleep_line — ред. BUG-005: утренний подъём — окно
    дневного сна от подъёма страницы ввода; после дневного сна —
    «Рекомендованное время ухода»; None — факта «Подъем» нет, строка
    не добавляется); сама сводка PB-3 НЕ меняется.
    """
    lines = day_summary_lines(conn, user, when_utc)
    window_line = next_sleep_line(conn, user, when_utc)
    return texts.backfill_success_text(
        _local_hhmm(when_utc, user["timezone"]), lines, window_line
    )


async def _parse_pair_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[datetime, dict] | None:
    """Разбор текста ОДНОГО шага пары (задача 3.1, FR-51; D23).

    Общая для обоих шагов ветка исходов парсера: мусор → §7.12 (фаза
    шага сохранена), будущее → предупреждение (фаза сохранена), старше
    backfill_depth_days → §7.15 (глубина §7.15 — для ОБОИХ времён пары;
    терминальная ветка: ВСЕ фазы диалога сняты). Возвращает
    (момент UTC, пользователь) — либо None, если ответ уже отправлен.
    """
    if update.message is None or update.effective_user is None:
        return None
    conn = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
        # Глубина ввода — из settings при каждом использовании (FR-24,
        # design D14), без кэша.
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
        # FR-16: мусор → §7.12; фаза шага сохранена (повторный ввод).
        await update.message.reply_text(texts.BACKFILL_BAD_FORMAT)
        return None
    if parsed is FUTURE:
        # FR-20 (решение №4): предупреждение без записи, фаза сохранена.
        await update.message.reply_text(texts.FUTURE_TIME_WARNING)
        return None
    if parsed is TOO_OLD:
        # FR-17 (design D14): §7.15 дословно, без записи; глубина
        # применяется к обоим временям пары. Терминальная ветка — сняты
        # ВСЕ фазы, включая PENDING_KIND_KEY (notes review-001-4.3).
        _clear_conflict_phases(context)
        await update.message.reply_text(texts.BACKFILL_TOO_OLD)
        return None
    if not isinstance(parsed, datetime):
        # Страховка от новых исходов парсера (minors review-001-4.2).
        _clear_conflict_phases(context)
        return None
    return parsed, user


async def _pair_sleep_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Шаг 1 пары (задача 3.1, FR-51, design D23): «Когда заснул?».

    ЕДИНЫЙ конфликт-чек пары здесь НЕ вызывается (D23: проверка одна,
    после ОБОИХ времён) — распознанный момент запоминается в фазе
    WAIT_PAIR_WAKE_KEY, бот спрашивает «Когда проснулся?» (PAIR_ASK_WAKE).
    """
    step = await _parse_pair_step(update, context)
    if step is None:
        return
    parsed, _user = step
    data = getattr(context, "user_data", None)
    if isinstance(data, dict):
        data.pop(WAIT_PAIR_SLEEP_KEY, None)
        data[WAIT_PAIR_WAKE_KEY] = {
            "sleep_utc": parsed.strftime(_TS_DB_FORMAT),
        }
    if update.message is not None:
        await update.message.reply_text(PAIR_ASK_WAKE)


async def _pair_wake_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Шаг 2 пары (задача 3.1, FR-51, design D23): «Когда проснулся?».

    После разбора — ЕДИНАЯ проверка ПАРЫ (detect_pair_conflict, один
    вызов): порядок wake > sleep и пересечение интервала пары с
    существующими событиями. Конфликт → §7.13 [Изменить]/[Отмена] (оба
    момента — в фазе CONFIRM_PAIR_KEY). Без конфликта пара создаётся
    АТОМАРНО (create_event_pair — одна транзакция, 3.1-fix review-001-3.1;
    is_backfilled=1; ре-шедул FR-41 — один вызов после коммита пары) и
    ответ §7.14-стиль: факт + строка следующего сна + сводка PB-3
    (существующие сборщики переиспользованы). Отказ записи → rollback +
    честный отказ-ответ (полупара в БД невозможна).
    """
    if update.message is None or update.effective_user is None:
        return
    phase = _pair_wake_phase(context)
    if phase is None:
        return
    step = await _parse_pair_step(update, context)
    if step is None:
        return
    parsed, user = step
    when_utc = parsed.strftime(_TS_DB_FORMAT)
    sleep_utc = phase["sleep_utc"]

    conn = common.open_conn(context)
    try:
        # Единый конфликт-чек ПАРЫ — ровно один вызов на пару (D23).
        conflict = detect_pair_conflict(conn, user["id"], sleep_utc, when_utc)
        if conflict is not None:
            # §7.13 с кнопками [Изменить]/[Отмена]; фазы шагов сняты,
            # оба момента пары — в CONFIRM_PAIR_KEY до решения.
            data = getattr(context, "user_data", None)
            if isinstance(data, dict):
                data.pop(WAIT_PAIR_SLEEP_KEY, None)
                data.pop(WAIT_PAIR_WAKE_KEY, None)
                data[CONFIRM_PAIR_KEY] = {
                    "sleep_utc": sleep_utc,
                    "wake_utc": when_utc,
                }
            await update.message.reply_text(
                texts.backfill_conflict_text(
                    texts.KIND_LABELS[conflict["event_type"]],
                    _local_hhmm(conflict["timestamp"], user["timezone"]),
                ),
                reply_markup=_conflict_markup(),
            )
            return

        try:
            # Атомарная ПАРА (3.1-fix, review-001-3.1 major №1): обе
            # вставки — одна транзакция (create_event_pair: stage ×2 +
            # один commit); D5-проверки обоих шагов выполняет сервис.
            create_event_pair(conn, user["id"], sleep_utc, when_utc)
            # §7.14-стиль по моменту пробуждения пары (страница дня —
            # страница ввода; сборщики существующие, задача 3.1).
            answer = _backfill_success_text(conn, user, when_utc)
        except ValueError:
            # Состояние изменилось между проверкой и записью (design D5):
            # транзакция пары НЕ закоммичена — staged-строки откатываются,
            # в БД НЕТ ни полупары, ни дублей (3.1-fix: молчаливый проглот
            # запрещён — честный отказ-ответ пользователю).
            conn.rollback()
            _clear_conflict_phases(context)
            await update.message.reply_text(BACKFILL_PAIR_FAILED)
            return
    finally:
        conn.close()
    # Успех — диалог завершён целиком: сняты ВСЕ фазы.
    _clear_conflict_phases(context)
    await update.message.reply_text(answer)


async def backfill_time_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Текстовый ввод в фазах диалога заднего числа (задачи 4.1–4.2 и
    3.1 парного ввода, design D10/D23).

    Вне диалога (ни одной фазы) текст игнорируется молча — bot не
    реагирует на произвольные сообщения пользователей. Фазы пары
    (задача 3.1, FR-51) обслуживаются ПЕРВЫМИ — они не пересекаются с
    WAIT_EVENT_TIME (одиночная ветка — wake и легаси-разбор — не менялась).
    Одиночная ветка: прошедшее время в пределах глубины ввода → запись
    события (is_backfilled=1) и ответ §7.14 со сводкой за день (задача
    4.5) либо §7.13 при конфликте (4.4), мусор → §7.12, будущее →
    предупреждение, старше backfill_depth_days → §7.15 (FR-17, фаза
    снята — диалог завершён).
    """
    if update.message is None or update.effective_user is None:
        return
    # Задача 3.1 (FR-51/D23): парный диалог — два шага.
    if _pair_sleep_active(context):
        await _pair_sleep_message(update, context)
        return
    if _pair_wake_phase(context) is not None:
        await _pair_wake_message(update, context)
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
        await update.message.reply_text(texts.FUTURE_TIME_WARNING)
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

    # Конфликта нет: запись события задним числом и ответ §7.14
    # (задача 4.5, FR-16 + FR-19).
    if kind is None:
        # Фаза вида потеряна (перезапуск процесса) — записывать нечем:
        # безопасный дефолт без записи, фазы сняты (design D10).
        _clear_conflict_phases(context)
        return

    conn = common.open_conn(context)
    try:
        # FR-16: событие создаётся с признаком is_backfilled (задача 4.5).
        create_event(conn, user["id"], kind, when_utc, is_backfilled=True)
        answer = _backfill_success_text(conn, user, when_utc)
    except ValueError:
        # Состояние изменилось между детектом конфликта и записью
        # (design D5): событие НЕ создано, фазы сняты — без ответа «успех».
        _clear_conflict_phases(context)
        return
    finally:
        conn.close()
    # Успех — диалог завершён целиком: сняты ВСЕ фазы (включая вид).
    _clear_conflict_phases(context)
    await update.message.reply_text(answer)


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

        # Задача 3.1 (FR-51, design D23): конфликт ПАРЫ. «Отменить» —
        # ничего не создано, ВСЕ фазы сняты. «Изменить» — пара начинается
        # заново с шага 1 «Когда заснул?» (оба времени переспрашиваются:
        # перезапись одной существующей записи пару не восстанавливает).
        pending_pair = (
            data.get(CONFIRM_PAIR_KEY) if isinstance(data, dict) else None
        )
        if (
            isinstance(pending_pair, dict)
            and "sleep_utc" in pending_pair
            and "wake_utc" in pending_pair
        ):
            _clear_conflict_phases(context)
            await query.answer()
            if update.effective_message is None:
                return
            if query.data == CALLBACK_CONFLICT_CANCEL:
                await update.effective_message.reply_text(
                    texts.BACKFILL_CONFLICT_CANCELLED
                )
                return
            # «Изменить»: пара заново с шага 1 — WAIT_EVENT_TIME + маркер
            # шага 1, вопрос §7.11 дословно (как на входе в диалог).
            user_data = getattr(context, "user_data", None)
            if isinstance(user_data, dict):
                user_data[WAIT_EVENT_TIME_KEY] = True
                user_data[WAIT_PAIR_SLEEP_KEY] = True
            await update.effective_message.reply_text(texts.BACKFILL_ASK_TIME)
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
            # Задача 4.5 (FR-19, ОВ-ПО-2) + 2.3 (FR-38/D20): «Изменить» —
            # тоже успешный ввод задним числом → §7.14 со сводкой за день
            # и строкой окна.
            await update.effective_message.reply_text(
                _backfill_success_text(conn, user, pending["when_utc"])
            )
        else:
            # Целевой записи уже нет (удалена параллельно) — данных не
            # меняем, меню перерисовано по актуальному состоянию.
            await common.redraw_menu(update, conn, user["id"])
    finally:
        conn.close()
