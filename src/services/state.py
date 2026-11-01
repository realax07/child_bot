# Состояние ребёнка и динамическое меню (change-пакет add-core-diary, задача 2.2).
#
# design D5: состояние ребёнка НЕ хранится отдельно — выводится из последнего
# события: нет событий или последнее wake → «бодрствует» (awake), последнее
# sleep → «спит» (asleep). Таблица состояний отклонена (рассинхронизация
# с событиями). Состояние перечитывается из БД при каждом вызове (никакого
# кэша между нажатиями) — основа guard'а устаревших клавиатур (FR-6):
# inline-клавиатуры из истории чата остаются нажимаемыми, поэтому обработчик
# колбэка перед созданием события перепроверяет состояние по БД.
#
# design D12: «последнее» событие — по created_at, затем id (задним числом
# введённое событие с ранним timestamp всё равно последнее по факту записи).
#
# Чистые функции без Telegram-объектов: клавиатура — данные (текст кнопки +
# callback_data), разметку PTB строит слой handlers (src/handlers/common.py).
# Вызывается только слоем handlers (design D1, FR-1).

from __future__ import annotations

import sqlite3

from src.repositories.sleep_events import SleepEventsRepo

# Состояния (design D5)
STATE_AWAKE = "awake"
STATE_ASLEEP = "asleep"

# Тексты кнопок меню (ТЗ §6): события + «⏰ Подсказка сейчас» (FR-32,
# 2.2-fix) + «Отменить последнее» + «📊 История за неделю»/«⚙️ Настройки»
# (add-settings-history 2.0, FR-48/FR-42; тексты дословно §6).
BTN_SLEEP = "😴 Заснул"
BTN_WAKE = "☀️ Проснулся"
BTN_HINT = "⏰ Подсказка сейчас"
BTN_UNDO = "↩️ Отменить последнее"
BTN_HISTORY = "📊 История за неделю"
BTN_SETTINGS = "⚙️ Настройки"

# callback_data кнопок (контракты задач 2.3/2.4; §10.3 — history:week и
# settings:show, add-settings-history 2.0/D21–D22: обработчик history —
# задача 2.1, settings:show зарегистрирован задачей 1.2).
CALLBACK_SLEEP = "event:sleep"
CALLBACK_WAKE = "event:wake"
CALLBACK_UNDO = "undo:last"
CALLBACK_HISTORY = "history:week"
CALLBACK_SETTINGS_SHOW = "settings:show"

# Виды (kind) событийных кнопок для guard'а (is_stale_callback).
KIND_SLEEP = "sleep"
KIND_WAKE = "wake"
KIND_UNDO = "undo"

# Постоянная reply-клавиатура (хотфикс BUG-002, S2): панель внизу экрана
# видна всегда и не зависит от прокрутки истории чата — root cause BUG-002
# в том, что inline-меню живёт в конкретном сообщении. Статичный список
# безопасен: guard D5 (is_stale_callback) перечитывает состояние из БД на
# «не та» кнопка не создаёт запись (см. handlers/common.py). Подсказка —
# текстовая кнопка без события (FR-32): маршрут текста → hint.py (2.2-fix).
# Чистые данные (без PTB-объектов): ReplyKeyboardMarkup строит слой handlers.
# ТЗ §6: события и подсказка в одной строке; клавиатура ОБЩАЯ для обоих
# состояний — «⏰ Подсказка сейчас» валидна и бодрствующему, и спящему.
# Порядок §6 (после строки события, до «↩️ Отменить последнее»): история и
# настройки (add-settings-history 2.0) — обе валидны в обоих состояниях и
# событий не создают (state-ui spec); «↩️ Отменить последнее» — отдельной
# строкой, как в inline-меню build_menu.
REPLY_KEYBOARD: list[list[str]] = [
    [BTN_SLEEP, BTN_WAKE, BTN_HINT],
    [BTN_HISTORY, BTN_SETTINGS],
    [BTN_UNDO],
]
REPLY_BUTTONS: tuple[str, ...] = (
    BTN_SLEEP,
    BTN_WAKE,
    BTN_HINT,
    BTN_HISTORY,
    BTN_SETTINGS,
    BTN_UNDO,
)

_KIND_BY_STATE_BUTTON = {
    BTN_SLEEP: KIND_SLEEP,
    BTN_WAKE: KIND_WAKE,
}


def get_state(conn: sqlite3.Connection, user_id: int) -> str:
    """Состояние ребёнка из последнего события (design D5, FR-6).

    Нет событий или последнее wake → STATE_AWAKE; последнее sleep →
    STATE_ASLEEP. Всегда читает БД заново (без кэша) — каждое нажатие
    кнопки проверяется по актуальным данным.
    """
    last = SleepEventsRepo(conn).last(user_id)
    if last is None or last["event_type"] == "wake":
        return STATE_AWAKE
    return STATE_ASLEEP


def build_menu(state: str) -> list[list[tuple[str, str]]]:
    """Клавиатура по состоянию: [(текст, callback_data), ...] по строкам (FR-6).

    Ровно одна событийная кнопка (awake → «😴 Заснул», asleep →
    «☀️ Проснулся») и «↩️ Отменить последнее» (design D12) отдельной строкой;
    между ними — «📊 История за неделю» и «⚙️ Настройки» (ТЗ §6,
    add-settings-history 2.0): валидны в ОБОИХ состояниях, событий не
    создают (callback-контракты §10.3: history:week — обработчик в задаче
    2.1, settings:show — settings_screen, задача 1.2).
    """
    if state == STATE_ASLEEP:
        event_row = [(BTN_WAKE, CALLBACK_WAKE)]
    else:
        event_row = [(BTN_SLEEP, CALLBACK_SLEEP)]
    return [
        event_row,
        [(BTN_HISTORY, CALLBACK_HISTORY), (BTN_SETTINGS, CALLBACK_SETTINGS_SHOW)],
        [(BTN_UNDO, CALLBACK_UNDO)],
    ]


def kind_of_button(button_text: str) -> str | None:
    """Вид событийной кнопки по её тексту; None — кнопка не событийная.

    Кнопка «↩️ Отменить последнее» сюда не относится: обработчик 2.4
    роутит её по callback_data «undo:last» напрямую, не по тексту.
    «📊 История за неделю» и «⚙️ Настройки» (2.0) — тоже None: это НЕ
    событийные кнопки, они не проходят guard is_stale_callback, а
    маршрутизируются текстово в common.py (событий не создают).
    """
    return _KIND_BY_STATE_BUTTON.get(button_text)


def is_stale_callback(
    conn: sqlite3.Connection, user_id: int, button_kind: str
) -> bool:
    """Guard устаревших кнопок (design D5, FR-6) — основа задач 2.3/2.4.

    True — кнопка противоречит актуальному состоянию (нажатие из старого
    сообщения игнорируется): «Заснул» при asleep, «Проснулся» при awake.
    «Отменить последнее» валидна в обоих состояниях.
    Состояние перечитывается из БД при каждом вызове.
    """
    state = get_state(conn, user_id)
    if button_kind == KIND_SLEEP:
        return state != STATE_AWAKE
    if button_kind == KIND_WAKE:
        return state != STATE_ASLEEP
    if button_kind == KIND_UNDO:
        return False
    # Неизвестный вид — трактуем как устаревший (безопасный дефолт).
    return True
