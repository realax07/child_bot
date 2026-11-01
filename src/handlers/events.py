# Обработчики событийных кнопок «😴 Заснул» / «☀️ Проснулся»
# (change-пакет add-core-diary, задача 2.3).
#
# design D2: callback-схема ТЗ §10.3 — event:sleep / event:wake открывают
# выбор времени §7.10 (кнопки time:now / time:manual); time:now создаёт
# событие «сейчас», time:manual передаёт дальше диалогу заднего числа
# (полная ветка диалога — задача 4.1, design D10).
# design D5 (guard): inline-клавиатуры из истории чата остаются нажимаемыми,
# поэтому перед созданием события состояние перепроверяется по БД
# (state.is_stale_callback); устаревшее нажатие игнорируется — событие НЕ
# создаётся, answerCallbackQuery + перерисовка актуальной клавиатуры
# (common.redraw_menu). Состояние читается заново и на event:*, и на time:*
# (между нажатиями оно могло измениться).
# design D6: sleep_type при записи не вычисляется (create_event пишет NULL).
# design D13: роль observer — нажатие игнорируется с вежливым сообщением,
# событие не создаётся.
# FR-7 (в ред. волны 6, PB-3): sleep «сейчас» → §7.5: подтверждающая строка
# + сводка событий дня (calculation.day_facts_lines — единая точка сборки);
# строка «Крайнее пробуждение: …» НЕ выводится вовсе (решение продакта
# 29.10: норма постоянна; wake_deadline остаётся в настройках — Пакет 3 —
# и в подсказках Пакета 2, из расчётов не удалён).
# FR-8 (в ред. PB-3 + FR-38, задача 2.3; ред. BUG-005): wake «сейчас» →
# §7.2: подтверждающая строка + строка СЛЕДУЮЩЕГО сна по типу
# пробуждения (последний завершённый интервал страницы): после дневного
# сна — «Рекомендованное время ухода: ЧЧ:ММ» (calculation.night_window,
# ОВ-4.2; позже 22:30 — предупреждение); утренний подъём — окно
# дневного сна от фактического подъёма (calculation.nap_window, design
# D16/D20; §7.3/§7.4-стиль при раннем/позднем подъёме — якоря из
# настроек) + сводка событий дня; подавление окон волны 6 снято,
# сценарии без «Подъема» — без строки (как в волне 6).
# design D4: время хранится в UTC; в текстах ответов — локальное время
# пользователя (users.timezone через zoneinfo).
#
# Слой handlers (design D1): только сервисы (services.event, services.state,
# services.settings), без repositories и прямого SQL.

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, time, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src import texts
from src.handlers import common
from src.services import access
from src.services.backfill_parser import resolve_tz
from src.services.calculation import (
    DEFAULT_DAY_START,
    _unclosed_interval,
    build_sleep_intervals,
    classify_sleep,
    day_facts_lines,
    day_page_of,
    merge_intervals,
    nap_window,
    night_window,
)
from src.services.event import create_event, get_user, list_raw_events
from src.services.settings import DEFAULT_KEYS, SettingsService
from src.services.state import KIND_SLEEP, KIND_WAKE, is_stale_callback

# callback_data кнопок §7.10 (design D2, ТЗ §10.3).
CALLBACK_TIME_NOW = "time:now"
CALLBACK_TIME_MANUAL = "time:manual"

# Фазы диалога в context.user_data (design D10: фазы диалога — не в БД):
# какая событийная кнопка ожидает выбора времени.
PENDING_KIND_KEY = "pending_event_kind"
# Задача 4.1 (FR-15): ветка «⏪ Ввести время вручную» — бот ждёт текстовый
# ввод времени (§7.11); фаза читается обработчиком текста (backfill.py).
WAIT_EVENT_TIME_KEY = "wait_event_time"
#
# Задача 3.1 (add-settings-history, FR-51, design D23): ПАРНЫЙ ввод сна
# задним числом — ДВА ШАГА (решение продакта 01.11, ОВ-5.1). Вид «сон» +
# «⏪ Ввести время вручную» ставит WAIT_EVENT_TIME как в ветке 4.1 ПЛЮС
# маркер шага 1 (WAIT_PAIR_SLEEP_KEY) — по нему backfill.py ведёт ПАРНЫЙ
# разбор; легаси-пробы фаз (без маркера) идут одиночной веткой, behavior
# не меняется (санкция ПМ на 3.1). Разбор шага 1 ставит фазу шага 2
# (WAIT_PAIR_WAKE_KEY), конфликт пары — CONFIRM_PAIR_KEY. Имена ключей —
# единый источник здесь (design D14-стиль); settings_screen добавляет их
# в BACKFILL_PHASE_KEYS (взаимное исключение с настройками в обе стороны).
WAIT_PAIR_SLEEP_KEY = "wait_pair_sleep"  # шаг 1: ждём «Когда заснул?»
WAIT_PAIR_WAKE_KEY = "wait_pair_wake"  # шаг 2: ждём «Когда проснулся?»
CONFIRM_PAIR_KEY = "confirm_pair"  # §7.13 пары: оба момента до решения

# Формулировка шага 2 пары (design D23 — решение продакта 01.11; секции
# «Когда проснулся?» в ТЗ §7 нет — служебный вопрос, список форматов —
# §7.11 дословно). Шаг 1 («Когда заснул?») отвечает §7.11 ДОСЛОВНО —
# texts.BACKFILL_ASK_TIME: формулировка шага 1 закреплена TC-BFD-001
# (диалог заднего числа 4.1), новой секции ТЗ для неё нет.
PAIR_ASK_WAKE = f"Когда проснулся?\n\n{texts.BACKFILL_ASK_TIME}"

# Сообщение наблюдателю — единый текст texts.OBSERVER_DENIED через
# services.access.deny_message() (задача 5.1, design D13; review-001-2.4,
# note 1: дубль литерала убран). Имя оставлено как алиас для обратной
# совместимости импортов (handlers/backfill.py, тесты).
OBSERVER_DENIED = texts.OBSERVER_DENIED


# -------------------------------------------------------------- Тексты FR-7/8

def fell_asleep_text(local_hhmm: str, summary_lines: Iterable[str]) -> str:
    """§7.5 с фактическим временем засыпания (FR-7 в ред. волны 6, PB-3).

    Строка «Крайнее пробуждение: ЧЧ:ММ» НЕ выводится вовсе (решение
    продакта 29.10). К подтверждению добавляется сводка событий дня —
    строки строит calculation.day_facts_lines (единая точка, PB-3).
    """
    return (
        f"✅ Засыпание записано в {local_hhmm}.\n"
        "Сон идёт.\n"
        + "\n".join(summary_lines)
    )


def woke_up_text(
    local_hhmm: str,
    summary_lines: Iterable[str],
    window_line: str | None = None,
) -> str:
    """§7.2 (FR-8 в ред. PB-3 + FR-38, задача 2.3 / design D20):
    подтверждающая строка + строка окна дневного сна + сводка событий дня.

    Подавление окон волны 6 СНЯТО: окно считается ОТ ФАКТИЧЕСКОГО
    подъёма страницы (calculation.nap_window, design D16 — не хардкод),
    стандартный/ранний/поздний подъём — §7.2/§7.3/§7.4-стиль; якоря
    раннего/позднего текста — из настроек (design D14). Сводка PB-3 —
    БЕЗ изменений (факты дня + «Бодрствование общее» BUG-004, строки
    строит calculation.day_facts_lines — единая точка). Окна нет (факта
    «Подъем» нет — окно от ненастоящего подъёма не строим, FR-34-стиль)
    — ответ как в волне 6.
    """
    lines = [f"✅ Пробуждение записано в {local_hhmm}."]
    if window_line is not None:
        lines.append(window_line)
    lines.extend(summary_lines)
    return "\n".join(lines)


# ------------------------------------------------- Сводка событий дня (PB-3)

#: Лимит растяжения первого бодрствования при раннем подъёме (§3.6,
#: «тянем до 13:00») — ТОТ ЖЕ аргументный дефолт, что у
#: calculation.nap_window (ключа-настройки нет); передаётся в расчёт
#: явно и подставляется в текст §7.3 — единый источник значения.
NAP_STRETCH_LIMIT = time(13, 0)


def _local_naive(utc_moment: str | datetime, tz_name: str | None) -> datetime:
    """UTC-момент (строка БД «YYYY-MM-DD HH:MM:SS» или datetime) →
    naive datetime в поясе пользователя (design D4)."""
    if isinstance(utc_moment, str):
        utc_moment = datetime.strptime(
            utc_moment, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    return utc_moment.astimezone(resolve_tz(tz_name or "")).replace(tzinfo=None)


def _settings_day_start(raw: str) -> time:
    """«ЧЧ:ММ» настройки day_start → time; фолбэк — дефолт расчётов (D8)."""
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except (AttributeError, ValueError):
        return DEFAULT_DAY_START


def day_summary_lines(
    conn: sqlite3.Connection, user: dict, ref_utc: str | datetime
) -> list[str]:
    """Сводка событий дня (PB-3) по странице дня момента ref_utc.

    Единая точка сборки для всех трёх подтверждений (§7.5/§7.2 «Сейчас»
    и §7.14 задним числом): сырые события — через сервис (design D1),
    day_start / merge_threshold — из settings при каждом расчёте
    (FR-24, design D14), строки — чистая calculation.day_facts_lines
    (без дублирования форматирования по хендлерам).
    """
    user_id = user["id"]
    tz_name = user["timezone"]
    settings = SettingsService(conn)
    day_start = _settings_day_start(str(settings.get(user_id, "day_start")))
    threshold = int(settings.get(user_id, "merge_threshold"))

    raw_events = [
        (row["event_type"], _local_naive(row["timestamp"], tz_name))
        for row in list_raw_events(conn, user_id)
    ]
    ref_local = _local_naive(ref_utc, tz_name)
    page = day_page_of(ref_local, day_start)
    now_local = datetime.now(resolve_tz(tz_name or "")).replace(tzinfo=None)
    return day_facts_lines(
        raw_events,
        page,
        day_start=day_start,
        now_local=now_local,
        merge_threshold_min=threshold,
    )


def _woke_after_day_sleep(
    raw_events: list[tuple[str, datetime]],
    ref_local: datetime,
    settings: dict,
) -> bool:
    """Тип пробуждения (BUG-005): последний ЗАВЕРШЁННЫЙ интервал —
    дневной отрезок?

    Интервалы — calculation.build_sleep_intervals + merge_intervals (та
    же склейка, что в окнах); незакрытый сон — единый хелпер
    `_unclosed_interval` (design D16, как в recommendation/scheduler:
    правило не дублируется) — его конец НЕ пробуждение. Завершённых
    интервалов нет — False (строку следующего сна в этом случае не
    строит nap_window).
    """
    merged = merge_intervals(
        build_sleep_intervals(
            raw_events,
            now_local=ref_local,
            day_start=_settings_day_start(str(settings.get("day_start"))),
        ),
        int(settings.get("merge_threshold", 20)),
    )
    unclosed = _unclosed_interval(
        sorted(raw_events, key=lambda item: item[1]), merged
    )
    completed = [interval for interval in merged if interval is not unclosed]
    if not completed:
        return False
    last = completed[-1]
    return not classify_sleep(last.start_local, last.end_local).is_night


def next_sleep_line(
    conn: sqlite3.Connection, user: dict, ref_utc: str | datetime
) -> str | None:
    """Строка СЛЕДУЮЩЕГО сна для подтверждения пробуждения (FR-8/FR-38;
    ред. BUG-005, решение продакта 01.11) или None — факта «Подъем»
    страницы нет (неполные данные — без строки, как в волне 6).

    Тип пробуждения — по последнему ЗАВЕРШЁННОМУ интервалу
    (`_woke_after_day_sleep`):
      - ДНЕВНОЙ отрезок → пробуждение ПОСЛЕ дневного сна: строка
        «Рекомендованное время ухода: ЧЧ:ММ» — calculation.night_window
        (ОВ-4.2: max(22:00, момент набора 12 ч 10 мин)); рекомендация
        позже 22:30 (later_than_deadline) — с предупреждением (§7.24-
        стиль). Прошедшее дневное окно НЕ показывается;
      - НОЧНОЙ отрезок → утренний подъём: окно дневного сна, как раньше
        (calculation.nap_window, design D16/D20; §7.2/§7.3/§7.4-стиль
        по флагам отклонений §3.6).
    Расчётный момент — локальное время ref_utc (для «Сейчас» — момент
    записи, для заднего числа — момент вводимого события). События и
    настройки читаются при каждом вызове (FR-24, design D14); сборка
    текста — templates texts.*.
    """
    user_id = user["id"]
    tz_name = user["timezone"]
    settings = SettingsService(conn).get_all(user_id)

    raw_events = [
        (row["event_type"], _local_naive(row["timestamp"], tz_name))
        for row in list_raw_events(conn, user_id)
    ]
    ref_local = _local_naive(ref_utc, tz_name)
    nap = nap_window(
        raw_events,
        ref_local,
        settings,
        stretch_limit=NAP_STRETCH_LIMIT,
    )
    if nap is None:
        return None

    if _woke_after_day_sleep(raw_events, ref_local, settings):
        night = night_window(raw_events, ref_local, settings)
        recommended_line = f"{night.recommended:%H:%M}"
        if night.later_than_deadline:
            return texts.recommended_bedtime_late_line(recommended_line)
        return texts.recommended_bedtime_line(recommended_line)

    # Утренний подъём — окно дневного сна (§7.2–7.4), как до BUG-005.
    start_line = f"{nap.start:%H:%M}"
    end_line = f"{nap.end:%H:%M}"
    deadline_line = f"{nap.wake_deadline:%H:%M}"
    if nap.stretch_to_limit:
        return texts.nap_window_early_text(
            f"{NAP_STRETCH_LIMIT:%H:%M}",
            start_line,
            end_line,
            deadline_line,
            _settings_hhmm(settings, "night_target"),
        )
    if nap.after_deadline:
        return texts.nap_window_late_text(
            start_line,
            end_line,
            deadline_line,
            _settings_hhmm(settings, "night_deadline"),
        )
    return texts.nap_window_line(start_line, end_line)


def _settings_hhmm(settings: dict, key: str) -> str:
    """Якорь-настройка «ЧЧ:ММ» (design D14) для текстов окон §7.3/§7.4.

    Ключ не задан (SettingsService.get_all отдаёт только DEFAULT_KEYS —
    seed при /start; расчёты без него берут аргументные дефолты) —
    дефолт ТЗ §5 из DEFAULT_KEYS (единый источник ключей, design D21;
    техдолг пакета 2, п. 2.3 — локальный дубль дефолтов снят).
    """
    return str(settings.get(key, DEFAULT_KEYS[key]))


# ------------------------------------------------------------- Помощники

def _kind_from_callback_data(data: str | None) -> str | None:
    """callback_data кнопки → вид события (KIND_SLEEP/KIND_WAKE) или None."""
    if data == "event:sleep":
        return KIND_SLEEP
    if data == "event:wake":
        return KIND_WAKE
    return None


def time_choose_markup() -> InlineKeyboardMarkup:
    """Клавиатура §7.10: «🕐 Сейчас» / «⏪ Ввести время вручную» (FR-15).

    Публичная с версии BUG-002: используется и reply-обработчиком панели
    (reply_event_button), и inline-колбэком event_button_callback.
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(texts.BTN_TIME_NOW, callback_data=CALLBACK_TIME_NOW)],
            [
                InlineKeyboardButton(
                    texts.BTN_TIME_MANUAL, callback_data=CALLBACK_TIME_MANUAL
                )
            ],
        ]
    )


def _local_hhmm(
    utc_moment: str | datetime, tz_name: str | None
) -> str:
    """UTC-момент (строка БД «YYYY-MM-DD HH:MM:SS» или aware datetime) →
    «ЧЧ:ММ» в поясе пользователя (design D4).

    Единый хелпер handlers.events / handlers.undo (техдолг пакета 2,
    п. 2.1): некорректное/пустое имя пояса — fallback Europe/Moscow
    (resolve_tz — единственная точка нормализации пояса).
    """
    if isinstance(utc_moment, str):
        utc_moment = datetime.strptime(
            utc_moment, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    local = utc_moment.astimezone(resolve_tz(tz_name or "")).replace(
        tzinfo=None
    )
    return f"{local:%H:%M}"


def _pending_kind(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return None
    kind = data.get(PENDING_KIND_KEY)
    return kind if kind in (KIND_SLEEP, KIND_WAKE) else None


def _clear_pair_phases(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Снять ВСЕ фазы парного ввода (задача 3.1, design D23): шаг 1, шаг 2
    и §7.13-подтверждение пары.

    Зеркало _clear_setting_phase (взаимное исключение фаз): начало нового
    диалога event:*/time:manual — старые фазы пары не переживают (notes
    review-001-4.3: «фазы не переживают завершение диалога»).
    """
    data = getattr(context, "user_data", None)
    if isinstance(data, dict):
        data.pop(WAIT_PAIR_SLEEP_KEY, None)
        data.pop(WAIT_PAIR_WAKE_KEY, None)
        data.pop(CONFIRM_PAIR_KEY, None)


def _set_pending_kind(context: ContextTypes.DEFAULT_TYPE, kind: str | None) -> None:
    data = getattr(context, "user_data", None)
    if isinstance(data, dict):
        if kind is None:
            data.pop(PENDING_KIND_KEY, None)
            # Сброс любой фазы диалога заднего числа (задача 4.1, design D10):
            # диалог прерван (guard/observer/«Сейчас») — фазу нужно снять.
            data.pop(WAIT_EVENT_TIME_KEY, None)
        else:
            data[PENDING_KIND_KEY] = kind
            # Повторное нажатие event:* начинает НОВЫЙ диалог (review-001-4.1,
            # note 1): зависшая фаза ожидания текста снята — иначе текст,
            # набранный до выбора time:manual, был бы принят незапрошенно.
            data.pop(WAIT_EVENT_TIME_KEY, None)
            # Взаимное исключение фаз (1.2-sym, санкция ПМ): постановка
            # backfill-фазы — начало НОВОЙ фазы, фаза диалога настроек
            # снимается (зеркально к _clear_backfill_phases в
            # settings_screen, 3de6685) — иначе текст события (например
            # «13:20») дошёл бы и в группу 2 и обновил настройку.
            _clear_setting_phase(context)
        # Задача 3.1 (FR-51/D23): новый диалог event:* снимает и фазы
        # парного ввода — любая постановка PENDING_KIND начинает диалог
        # заново (та же симметрия, что у WAIT_EVENT_TIME выше).
        _clear_pair_phases(context)


def _set_wait_event_time(
    context: ContextTypes.DEFAULT_TYPE, active: bool
) -> None:
    """Фаза «ждём текстовый ввод времени» (задача 4.1, design D10).

    Только постановка фазы: все сбросы идут через _set_pending_kind(context,
    None) (guard/observer/«Сейчас») либо очисткой ключа в backfill.py
    (успешный разбор ввода — задача 4.2). Мёртвая ветка сброса убрана
    (review-001-4.1, note 3).
    """
    data = getattr(context, "user_data", None)
    if isinstance(data, dict) and active:
        data[WAIT_EVENT_TIME_KEY] = True
        # Взаимное исключение фаз (1.2-sym, санкция ПМ): вход в «⏪ Ввести
        # время вручную» гасит фазу настроек WAIT_SETTING_VALUE — зеркально
        # к _clear_backfill_phases в settings_screen (3de6685), см.
        # _clear_setting_phase.
        _clear_setting_phase(context)
        # Задача 3.1 (FR-51/D23): вход в одиночную ветку (wake) гасит и
        # фазы парного ввода — два диалога группы 0 одновременно запрещены.
        _clear_pair_phases(context)


def _clear_setting_phase(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Снять фазу настроек WAIT_SETTING_VALUE (группа 2).

    Симметрия взаимного исключения фаз (major review-001-1.2, санкция ПМ
    на events.py вне первичной зоны): PTB доставляет текст в ОБЕ группы,
    поэтому одновременные фазы настроек (группа 2) и backfill-диалога
    (группа 0) приводят к тому, что ввод времени события обновляет
    настройку (или наоборот, см. 3de6685). Импорт константы — внутри
    функции: settings_screen импортирует константы events на уровне
    модуля, верхнеуровневый импорт дал бы цикл; строковых копий нет
    (design D14-стиль: единый источник имён).
    """
    from src.handlers.settings_screen import WAIT_SETTING_VALUE

    data = getattr(context, "user_data", None)
    if isinstance(data, dict):
        data.pop(WAIT_SETTING_VALUE, None)


# --------------------------------------------------------- Обработчики

async def event_button_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Нажатие «😴 Заснул» / «☀️ Проснулся» → меню §7.10 (FR-7, FR-8, FR-15).

    Observer — вежливый отказ без события (D13); устаревшая кнопка —
    answerCallbackQuery + перерисовка, событие не создаётся (design D5).
    """
    query = update.callback_query
    if query is None:
        return
    kind = _kind_from_callback_data(query.data)
    if kind is None or update.effective_user is None:
        return

    conn: sqlite3.Connection = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)

        # design D13 (задача 5.1): наблюдателю отметки недоступны —
        # нажатие игнорируется (проверка роли — services.access).
        if access.is_observer(conn, user["id"]):
            _set_pending_kind(context, None)
            await query.answer(text=access.deny_message(), show_alert=False)
            return

        # Guard устаревшей кнопки (design D5): состояние — свежее из БД.
        if is_stale_callback(conn, user["id"], kind):
            _set_pending_kind(context, None)
            await query.answer()
            await common.redraw_menu(update, conn, user["id"])
            return

        # Валидная кнопка: запомнить вид события, показать §7.10 (FR-15).
        _set_pending_kind(context, kind)
        try:
            await query.edit_message_text(
                texts.BACKFILL_CHOOSE_TIME, reply_markup=time_choose_markup()
            )
        except BadRequest:
            # §7.10 уже показан (повторное нажатие) — перерисовка не нужна.
            pass
        await query.answer()
    finally:
        conn.close()


# ------------------------------------------------- Reply-панель (BUG-002)

async def reply_event_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    user: dict,
    kind: str,
) -> None:
    """Кнопка «😴 Заснул» / «☀️ Проснулся» постоянной reply-панели (BUG-002).

    Отличия от event_button_callback: нажатие — обычное текстовое
    сообщение (reply-клавиатура присылает текст), перерисовывать нечего.
    Guard design D5 тот же — состояние перечитывается из БД:
      - «не та» кнопка → дружелюбный ответ по факту (ALREADY_ASLEEP /
        STILL_AWAKE) + свежее inline-меню отдельным сообщением; записи НЕТ;
      - валидная кнопка → штатный диалог §7.10 (BACKFILL_CHOOSE_TIME с
        кнопками time:now / time:manual), как у inline event:*.
    """
    message = update.effective_message
    if message is None:
        return

    # design D13: наблюдателю отметки недоступны — вежливый отказ без события.
    if access.is_observer(conn, user["id"]):
        _set_pending_kind(context, None)
        await message.reply_text(access.deny_message())
        return

    # Guard устаревшей кнопки (design D5): состояние — свежее из БД.
    if is_stale_callback(conn, user["id"], kind):
        _set_pending_kind(context, None)
        await message.reply_text(
            common.ALREADY_ASLEEP if kind == KIND_SLEEP else common.STILL_AWAKE
        )
        await common.send_fresh_menu(update, conn, user["id"])
        return

    # Валидная кнопка: тот же диалог §7.10, что у inline event:* (FR-15).
    _set_pending_kind(context, kind)
    await message.reply_text(
        texts.BACKFILL_CHOOSE_TIME, reply_markup=time_choose_markup()
    )


async def time_choose_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Выбор «🕐 Сейчас» / «⏪ Ввести время вручную» в меню §7.10 (FR-7/8/15)."""
    query = update.callback_query
    if query is None:
        return
    action = query.data
    if action not in (CALLBACK_TIME_NOW, CALLBACK_TIME_MANUAL):
        return
    if update.effective_user is None:
        return

    conn: sqlite3.Connection = common.open_conn(context)
    try:
        user = get_user(conn, update.effective_user.id)
        kind = _pending_kind(context)

        # design D13 (задача 5.1): наблюдателю отметки недоступны и на
        # этом шаге (проверка роли — services.access).
        if access.is_observer(conn, user["id"]):
            _set_pending_kind(context, None)
            await query.answer(text=access.deny_message(), show_alert=False)
            return

        # Нет ожидаемой фазы диалога (перезапуск процесса / чужое сообщение):
        # трактуем как устаревшее нажатие (design D5 — безопасный дефолт).
        # Симметрично соседним guard-веткам снимаем обе фазы (review-001-4.1,
        # note 2) — зависший WAIT_EVENT_TIME здесь не выживает.
        if kind is None:
            _set_pending_kind(context, None)
            await query.answer()
            await common.redraw_menu(update, conn, user["id"])
            return

        # Guard повторно по свежему состоянию БД (design D5): пока показывалось
        # §7.10, состояние могло измениться (другое устройство/старое окно).
        if is_stale_callback(conn, user["id"], kind):
            _set_pending_kind(context, None)
            await query.answer()
            await common.redraw_menu(update, conn, user["id"])
            return

        if action == CALLBACK_TIME_MANUAL:
            if kind == KIND_SLEEP:
                # Задача 3.1 (FR-51, design D23, ОВ-5.1): парный ввод сна —
                # шаг 1 «Когда заснул?» (§7.11-стиль: текст §7.11 дословно).
                # WAIT_EVENT_TIME ставится КАК РАНЬШЕ (контракт ветки 4.1,
                # TC-BFD-001), плюс фаза-маркер шага 1 пары — по ней
                # backfill.py ведёт ПАРНЫЙ разбор вместо одиночного.
                # Легаси-пробы фаз (только WAIT_EVENT_TIME + PENDING_KIND)
                # идут одиночной веткой — behavior сохранён. Взаимное
                # исключение фаз — внутри _set_wait_event_time (настройки
                # и зависшие фазы пары сняты).
                _set_wait_event_time(context, True)
                data = getattr(context, "user_data", None)
                if isinstance(data, dict):
                    data[WAIT_PAIR_SLEEP_KEY] = True
                await query.answer()
                if update.effective_message is not None:
                    await update.effective_message.reply_text(
                        texts.BACKFILL_ASK_TIME
                    )
                return
            # FR-15: запрос времени текстом §7.11; фаза WAIT_EVENT_TIME
            # сохраняется в context.user_data (задача 4.1, design D10) —
            # текстовый ввод примет обработчик backfill.py. Разбор ввода
            # (парсер) — задача 4.2.
            _set_wait_event_time(context, True)
            await query.answer()
            if update.effective_message is not None:
                await update.effective_message.reply_text(texts.BACKFILL_ASK_TIME)
            return

        # «🕐 Сейчас» (FR-7/FR-8): событие с текущим временем UTC.
        _set_pending_kind(context, None)
        now_utc = datetime.now(timezone.utc)
        try:
            create_event(conn, user["id"], kind, now_utc.strftime(
                "%Y-%m-%d %H:%M:%S"
            ))
        except ValueError:
            # Состояние изменилось между §7.10 и «Сейчас» — guard (design D5).
            await query.answer()
            await common.redraw_menu(update, conn, user["id"])
            return

        # Переключение меню по свежему состоянию (FR-6) + ответ §7.5/§7.2
        # со сводкой событий дня (PB-3, волна 6). answerCallbackQuery до
        # redraw/reply (review-001-2.3, note 1): при падении edit/reply
        # пользователь всё равно не увидит «часики».
        await query.answer()
        await common.redraw_menu(update, conn, user["id"])
        local_hhmm = _local_hhmm(now_utc, user["timezone"])
        # Сводка — ЕДИНАЯ точка сборки (PB-3): события и настройки читаются
        # ПОСЛЕ записи события — факт уже в списке (расчётный момент — now).
        summary_lines = day_summary_lines(conn, user, now_utc)
        if kind == KIND_SLEEP:
            # §7.5: БЕЗ строки окна (design D20: показ окон в §7.5
            # отклонён) и БЕЗ «Крайнего пробуждения» (решение 29.10);
            # расчёт окна здесь и НЕ нужен (техдолг пакета 2, п. 2.3:
            # лишний вызов next_sleep_line в §7.5-ветке снят — окно
            # строится только для wake-ветки ниже).
            answer = fell_asleep_text(local_hhmm, summary_lines)
        else:
            # FR-8: подтверждающая строка + окно + сводка PB-3 без изменений.
            # Строка следующего сна — по типу пробуждения (BUG-005,
            # FR-38/D20): утренний подъём → дневное окно; после дневного
            # сна → конкретное время ухода; None — факта «Подъем» нет
            # (окна/рекомендации от неполных данных не строим).
            window_line = next_sleep_line(conn, user, now_utc)
            answer = woke_up_text(local_hhmm, summary_lines, window_line)
        if update.effective_message is not None:
            await update.effective_message.reply_text(answer)
    finally:
        conn.close()
