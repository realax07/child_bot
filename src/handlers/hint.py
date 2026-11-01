# Callback-обработчик «⏰ Подсказка сейчас» (change-пакет add-recommendations,
# задача 2.2; ТЗ §10.3 callback_data «status:show», design D17).
#
# ТЗ §10.3/§4.6: нажатие даёт МГНОВЕННЫЙ ответ из RecommendationService
# (design D17) и НЕ создаёт событие (sleep_events не пишется, FR-32).
# Время «сейчас» и слово состояния ребёнка в тексте НЕ выводятся —
# только длительности, позиции к диапазону, окна и предупреждения.
# Ответ доступен в обоих состояниях меню (awake/asleep), «неполные
# данные» — просьба отметить события без «0 ч» и окон (FR-34).
#
# design D5 (guard по образцу событий): состояние перечитывается из БД
# при КАЖДОМ нажатии (свежие события + настройки, без кэша) — ответ
# всегда по актуальному состоянию, «устаревшее» нажатие из старого
# сообщения ничего не пишет (писать нечего вообще) и не переключает
# меню: подсказка валидна в обоих состояниях.
#
# design D13: observer — подсказка ДОСТУПНА (чтение, без ограничений);
# проверки роли нет, denying-ветки нет.
#
# Тексты — СТРУКТУРНЫЕ фрагменты сервиса (длительности texts.format_duration,
# моменты окон «ЧЧ:ММ», доклад конфликтов из anchor_conflicts): дословные
# §7.6–7.9 добавит задача 2.3 в src/texts.py — здесь их НЕТ.
#
# 2.2-fix (ТЗ §6): кнопка есть и в постоянной reply-панели — там она
# присылает ТЕКСТ «⏰ Подсказка сейчас»; маршрут текста reply_button_message
# → reply_hint_button: та же логика (_current_status), событие не создаётся.
#
# design D4: события хранятся в UTC; расчёт — в локальном наивном времени
# пользователя (users.timezone через zoneinfo, resolve_tz).
#
# Слой handlers (design D1): только сервисы (services.event,
# services.settings, services.recommendation), без repositories и SQL.

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from src import texts
from src.handlers import common
from src.services.backfill_parser import resolve_tz
from src.services.event import get_user, list_raw_events
from src.services.recommendation import (
    POSITION_ABOVE,
    POSITION_BELOW,
    STATE_ASLEEP,
    STATE_AWAKE,
    WARNING_ANCHOR_CONFLICT,
    WARNING_LATE_NAP,
    WARNING_OVERTIRED,
    RecommendationService,
)
from src.services.settings import SettingsService

# callback_data кнопки (ТЗ §10.3, design D17).
CALLBACK_STATUS_SHOW = "status:show"

# Fallback пояса — как в events.py (design D4, аудитория бота).
_FALLBACK_TZ = "Europe/Moscow"


def _local_naive(utc_moment: str | datetime, tz_name: str | None) -> datetime:
    """UTC-момент (строка БД «YYYY-MM-DD HH:MM:SS» или datetime) →
    naive datetime в поясе пользователя (design D4) — как в events.py."""
    if isinstance(utc_moment, str):
        utc_moment = datetime.strptime(
            utc_moment, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    return utc_moment.astimezone(resolve_tz(tz_name or "")).replace(tzinfo=None)


# ------------------------------------------------------- Текст ответа


def _awake_fragment(status: dict) -> str:
    """Структура §4.6 из 5 пунктов для статуса «бодрствует» (FR-32).

    Время «сейчас» и слово состояния не выводятся (ТЗ §4.6): только
    длительности, остатки до границ, окно следующего сна и предупреждения.
    Дословные формулировки §7.6 — задача 2.3.
    """
    bounds = status["bounds"]
    lower_line = texts.format_duration(bounds["lower"])
    upper_line = texts.format_duration(bounds["upper"])

    position = status["position"]
    if position == POSITION_BELOW:
        position_line = (
            f"До нижней границы ({lower_line}) осталось: "
            f"{texts.format_duration(status['remaining_to_lower'])}. "
            f"До верхней границы ({upper_line}) осталось: "
            f"{texts.format_duration(status['remaining_to_upper'])}."
        )
    elif position == POSITION_ABOVE:
        position_line = (
            f"Суммарное бодрствование превысило верхнюю границу ({upper_line})."
        )
    else:
        position_line = (
            f"Бодрствование в границах {lower_line}–{upper_line}. "
            f"До верхней границы осталось: "
            f"{texts.format_duration(status['remaining_to_upper'])}."
        )

    window = status["next_window"]
    if window["kind"] == "nap":
        window_line = f"Окно дневного сна: {window['start_line']}–{window['end_line']}."
        if window["stretch_to_limit"]:
            # Ранний подъём (§3.6): окно уже до лимита — совет тянуть до конца.
            window_line += " Сегодня можно растянуть до конца окна."
    else:
        window_line = (
            f"Окно ухода в ночь: около {window['recommended_line']} "
            f"(крайнее {window['deadline_line']})."
        )

    lines = [
        "Бодрствование с последнего пробуждения: "
        f"{texts.format_duration(status['wake_since_last_wake'])}. "
        f"Суммарно за сегодня: {texts.format_duration(status['total_wake'])}.",
        position_line,
        "",
        window_line,
    ]
    lines.extend(_warning_lines(status["warnings"]))
    return "\n".join(lines)


def _warning_lines(warnings: list[dict]) -> list[str]:
    """Предупреждения (п. 5 структуры §4.6) — строками с цифрами.

    Дословные §7.8/§7.9 — задача 2.3; здесь структурные формулировки
    из полей сервиса (цифры — из расчётов, не хардкод).
    """
    lines: list[str] = []
    for warning in warnings:
        kind = warning["type"]
        if kind == WARNING_LATE_NAP:
            lines.append(
                f"⚠️ Если сон начнётся в {warning['nap_start_line']}, он "
                f"закончится около {warning['forecast_end_line']} — позже "
                f"крайнего времени {warning['deadline_line']}. "
                "Ночной сон может сдвинуться."
            )
        elif kind == WARNING_OVERTIRED:
            lines.append(
                f"⚠️ Бодрствование {warning['total_wake_line']} — превышена "
                f"верхняя граница ({warning['upper_line']}). "
                "Постарайтесь уложить ребёнка как можно скорее."
            )
        elif kind == WARNING_ANCHOR_CONFLICT:
            anchors = " против ".join(warning["conflicting"])
            lines.append(f"⚠️ Якоря в противоречии: {anchors}. {warning['accepted']}.")
    return lines


def _asleep_fragment(status: dict) -> str:
    """Статус спящему §7.7 (FR-33): сколько спит, прогноз пробуждения
    (дневной сон — от цели из настроек), прогноз бодрствования после.

    «Пора укладывать» спящему НЕ выдаётся (FR-33). Незавершённый дневной
    сон дольше цели — мягкая отметка (FR-37), без пуша (ОВ-4.1). У ночного
    сна прогнозов нет — только факт «сколько спит» (D17, честный None).
    """
    lines = [
        f"Сон идёт с {status['sleep_start_line']} (уже {status['duration_line']})."
    ]
    if status["forecast_wake_line"] is not None:
        lines.append(
            f"Ожидаемое пробуждение: около {status['forecast_wake_line']}."
        )
    if status["forecast_total_wake_line"] is not None:
        lines.append(
            "После пробуждения суммарное бодрствование составит около "
            f"{status['forecast_total_wake_line']}."
        )
    over = status["over_target"]
    if over is not None:
        lines.append(
            f"Сон идёт дольше обычного: {over['duration_line']} "
            f"при обычной цели {over['target_line']}."
        )
    return "\n".join(lines)


def _incomplete_fragment() -> str:
    """Неполные данные (FR-34): факта «Подъем» страницы нет — «0 ч» и
    окна не выводятся, просьба отметить события (ТЗ §7.14-стиль)."""
    return (
        "Пока нет факта подъёма за сегодня — данных для подсказки мало.\n"
        "Отметьте «☀️ Проснулся» или введите время задним числом, "
        "и я посчитаю окна."
    )


def hint_text(status: dict) -> str:
    """Структурный ответ подсказки по состоянию из RecommendationService.

    Единая точка рендера для задачи 2.2: дословные тексты §7.6–7.9
    заменит задача 2.3 (src/texts.py).
    """
    state = status["state"]
    if state == STATE_AWAKE:
        return _awake_fragment(status)
    if state == STATE_ASLEEP:
        return _asleep_fragment(status)
    return _incomplete_fragment()


# --------------------------------------------------------- Обработчик


def _current_status(conn: sqlite3.Connection, user: dict) -> dict:
    """Свежий статус подсказки из БД (design D5) — общий вход обоих маршрутов.

    События и настройки перечитываются при КАЖДОМ вызове (без кэша) —
    callback status:show и текст reply-панели отвечают по одному и тому
    же свежему состоянию (ТЗ §10.3: двойной вход, одна логика).
    """
    tz_name = user["timezone"]
    now_local = _local_naive(datetime.now(timezone.utc), tz_name)
    raw_events = [
        (row["event_type"], _local_naive(row["timestamp"], tz_name))
        for row in list_raw_events(conn, user["id"])
    ]
    settings_map = SettingsService(conn).get_all(user["id"])
    return RecommendationService(raw_events, now_local, settings_map).status()


async def hint_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатие «⏰ Подсказка сейчас» → мгновенный ответ (FR-32…FR-34).

    Событие НЕ создаётся (FR-32); состояние перечитывается из БД при
    каждом нажатии (design D5 — свежие события и настройки); observer'у
    подсказка доступна (design D13 — чтение без ограничений).
    """
    query = update.callback_query
    if query is None or query.data != CALLBACK_STATUS_SHOW:
        return
    if update.effective_user is None:
        return

    conn: sqlite3.Connection = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
        status = _current_status(conn, user)

        await query.answer()
        if update.effective_message is not None:
            await update.effective_message.reply_text(hint_text(status))
    finally:
        conn.close()


async def reply_hint_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    user: dict,
) -> None:
    """Текст «⏰ Подсказка сейчас» с reply-панели (ТЗ §6, 2.2-fix).

    Reply-кнопка присылает ТЕКСТ — общий вход reply_button_message
    (группа 1, BUG-002) роутит его сюда; логика ТА ЖЕ, что у callback
    status:show (_current_status, ТЗ §10.3 — двойной вход). Событие НЕ
    создаётся (FR-32); состояние перечитывается из БД (design D5 —
    отдельный guard не нужен: подсказка валидна в обоих состояниях);
    observer'у доступна (design D13). Сигнатура — как reply_undo_button.
    """
    if update.effective_message is None:
        return
    status = _current_status(conn, user)
    await update.effective_message.reply_text(hint_text(status))
