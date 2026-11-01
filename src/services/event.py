# Сервис событий сна (change-пакет add-core-diary, задача 2.3).
#
# design D5: перед записью состояние ребёнка перепроверяется по БД (выводится
# из последнего события, не хранится) — событие, противоречащее состоянию,
# не создаётся.
# design D6: при записи пишутся ТОЛЬКО сырые поля (event_type, timestamp,
# created_at, is_backfilled); sleep_type и is_merged — NULL (резерв схемы,
# разметка/склейка вычисляются на лету при чтении).
# design D4: timestamp хранится в UTC (строка «YYYY-MM-DD HH:MM:SS»).
#
# Гарантии create_event (FR-7, FR-8):
#   1) состояние соответствует кнопке (sleep — из «бодрствует», wake — из «спит»);
#   2) нет незакрытого такого же события (повторный sleep при спящем /
#      повторный wake при бодрствующем отвергается).
# Нарушение — ValueError; слой handlers переводит это в guard-поведение
# (answerCallbackQuery + перерисовка меню, design D5).
#
# Вызывается только слоем handlers (design D1, FR-1); репозитории — только
# отсюда.

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.repositories.sleep_events import SleepEventsRepo
from src.repositories.users import UsersRepo
from src.services import state
from src.services.state import KIND_SLEEP, KIND_WAKE

# Виды событий, создаваемые событийными кнопками (ТЗ §9.2 CHECK).
VALID_KINDS = (KIND_SLEEP, KIND_WAKE)

# Роли пользователей (ТЗ §9.1, решение 26.10 №5); observer — см. D13.
ROLE_PARENT = "parent"
ROLE_OBSERVER = "observer"


def get_user(conn: sqlite3.Connection, telegram_id: int) -> dict:
    """Пользователь по telegram_id: {id, telegram_id, role, timezone}.

    Создаётся при первом обращении (идемпотентно, как в /start — FR-5).
    Временный мост для обработчиков событий до выделения
    src/services/access.py (задача 5.1, design D13): handlers обращаются
    к сервису, не к репозиторию (design D1).
    """
    return UsersRepo(conn).get_or_create(telegram_id)


def _notify_scheduler() -> None:
    """Точка ре-шедула напоминаний (FR-41, design D19; add-recommendations 3.1).

    Ленивый импорт: services.scheduler читает этот модуль
    (list_raw_events) — прямой импорт дал бы цикл. Без активного
    планировщика (тесты сервисов, --check) — no-op (см.
    scheduler.notify_events_changed).
    """
    from src.services import scheduler  # лениво: цикл импорта

    scheduler.notify_events_changed()


def create_event(
    conn: sqlite3.Connection,
    user_id: int,
    kind: str,
    when_utc: str,
    *,
    is_backfilled: bool = False,
) -> dict:
    """Создать событие sleep/wake с сырыми полями (FR-7, FR-8, design D6).

    when_utc — момент события строкой UTC «YYYY-MM-DD HH:MM:SS» (design D4).
    is_backfilled=True — ввод задним числом (FR-16, задача 4.5); кнопки
    «сейчас» пишут дефолт False. Проверки перед записью (design D5):
      - kind допустим (sleep/wake);
      - состояние ребёнка соответствует (перечитано из БД);
      - нет незакрытого такого же события (последняя запись не того же вида).
    Возвращает созданную запись (dict из SleepEventsRepo-полей).
    ValueError — любая проверка не пройдена: событие НЕ создаётся.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"недопустимый вид события: {kind!r}")

    # Состояние всегда перечитывается из БД (design D5, без кэша).
    current_state = state.get_state(conn, user_id)
    expected = state.STATE_AWAKE if kind == KIND_SLEEP else state.STATE_ASLEEP
    if current_state != expected:
        raise ValueError(
            f"состояние {current_state!r} не соответствует кнопке {kind!r}"
        )

    # Незакрытое событие того же вида (последнее — по created_at, затем id,
    # design D12): повторное sleep при спящем / wake при бодрствующем.
    last = SleepEventsRepo(conn).last(user_id)
    if last is not None and last["event_type"] == kind:
        raise ValueError(f"незакрытое событие {kind!r} уже существует")

    event_id = SleepEventsRepo(conn).insert(
        user_id, kind, when_utc, is_backfilled=is_backfilled
    )
    _notify_scheduler()
    return {
        "id": event_id,
        "user_id": user_id,
        "event_type": kind,
        "timestamp": when_utc,
        "is_backfilled": is_backfilled,
        "sleep_type": None,  # design D6: разметка — расчёт при чтении
        "is_merged": None,   # design D9: склейка — расчёт при чтении
    }


# ------------------------- Атомарная ПАРА задним числом (3.1, FR-51)

def _ensure_pair_step(
    conn: sqlite3.Connection, user_id: int, kind: str
) -> None:
    """D5-проверки ОДНОГО шага пары перед stage-вставкой (3.1-fix).

    Те же гарантии, что у create_event (FR-7, FR-8): вид допустим;
    состояние соответствует кнопке (перечитано из БД); нет незакрытого
    такого же события. БЕЗ записи — вставку и commit делает
    create_event_pair. Проверка шага 2 (wake) выполняется ПОСЛЕ
    stage-вставки засыпания и видит её: тот же conn, та же транзакция
    (повторяет последовательность двух create_event).
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"недопустимый вид события: {kind!r}")

    # Состояние всегда перечитывается из БД (design D5, без кэша).
    current_state = state.get_state(conn, user_id)
    expected = state.STATE_AWAKE if kind == KIND_SLEEP else state.STATE_ASLEEP
    if current_state != expected:
        raise ValueError(
            f"состояние {current_state!r} не соответствует кнопке {kind!r}"
        )

    # Незакрытое событие того же вида (последнее — по created_at, затем id,
    # design D12): повторное sleep при спящем / wake при бодрствующем.
    last = SleepEventsRepo(conn).last(user_id)
    if last is not None and last["event_type"] == kind:
        raise ValueError(f"незакрытое событие {kind!r} уже существует")


def create_event_pair(
    conn: sqlite3.Connection,
    user_id: int,
    sleep_utc: str,
    wake_utc: str,
) -> tuple[dict, dict]:
    """Атомарно создать ПАРУ sleep+wake задним числом (3.1-fix, FR-51).

    Задача 3.1-fix (review-001-3.1 major №1): обе вставки — ОДНА
    транзакция (SleepEventsRepo.stage_insert ×2 + один commit, паттерн
    stage_upsert репозитория settings). При отказе ЛЮБОГО шага (ValueError
    от D5-проверок или вставки) commit НЕ выполняется — вызывающий
    обработчик делает rollback, полупара (незакрытый сон без
    пробуждения) в БД невозможна. Существующий create_event НЕ менялся —
    одиночные ветки пишут как раньше (каждая вставка — свой commit).

    Порядок шагов и проверки — как у последовательных create_event:
    D5-проверка засыпания → stage sleep → D5-проверка пробуждения
    (видит staged sleep: тот же conn/транзакция) → stage wake → commit.

    Ре-шедул напоминаний (FR-41) — ОДИН вызов ПОСЛЕ успешного коммита
    ПАРЫ (хук не срабатывает при откате; ReminderScheduler.reschedule
    идемпотентен, design D19). when_utc/sleep_utc — строки UTC
    «YYYY-MM-DD HH:MM:SS» (design D4). Возвращает (sleep, wake) —
    созданные записи (dict из SleepEventsRepo-полей).
    """
    repo = SleepEventsRepo(conn)
    _ensure_pair_step(conn, user_id, KIND_SLEEP)
    sleep_id = repo.stage_insert(
        user_id, KIND_SLEEP, sleep_utc, is_backfilled=True
    )
    _ensure_pair_step(conn, user_id, KIND_WAKE)
    wake_id = repo.stage_insert(
        user_id, KIND_WAKE, wake_utc, is_backfilled=True
    )
    conn.commit()  # единственный commit: атомарность пары (3.1-fix)
    _notify_scheduler()
    sleep_record = {
        "id": sleep_id,
        "user_id": user_id,
        "event_type": KIND_SLEEP,
        "timestamp": sleep_utc,
        "is_backfilled": True,
        "sleep_type": None,  # design D6: разметка — расчёт при чтении
        "is_merged": None,   # design D9: склейка — расчёт при чтении
    }
    wake_record = {
        "id": wake_id,
        "user_id": user_id,
        "event_type": KIND_WAKE,
        "timestamp": wake_utc,
        "is_backfilled": True,
        "sleep_type": None,
        "is_merged": None,
    }
    return sleep_record, wake_record


def list_raw_events(
    conn: sqlite3.Connection, user_id: int
) -> list[dict]:
    """Сырые события пользователя в хронологическом порядке (design D6).

    Мост для расчётов на лету (задача 4.5, FR-19): handlers обращаются
    к сервису, не к репозиторию (design D1). Поля — как в БД; timestamp —
    строка UTC (design D4), перевод в локальное время — у вызывающего.
    """
    return SleepEventsRepo(conn).list_by_user(user_id)


def overwrite_event(
    conn: sqlite3.Connection,
    event_id: int,
    kind: str,
    when_utc: str,
) -> bool:
    """Атомарная перезапись целевого события (FR-18, ОВ-ПО-1, design D11).

    «Изменить» в §7.13: существующей записи по id присваиваются вид и
    время НОВОГО ввода (один UPDATE — без дублей, design D11). Дублирующей
    записи не создаётся; прежнее значение не сохраняется (последующий
    «↩️ Отменить последнее» удаляет запись целиком, design D12).
    False — записи с таким id нет (данных нет изменений).
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"недопустимый вид события: {kind!r}")
    _parse_utc(when_utc)  # невалидный формат момента — ValueError, записи нет
    updated = SleepEventsRepo(conn).overwrite(
        event_id, event_type=kind, timestamp=when_utc
    )
    if updated:
        # FR-41: перезапись — точка ре-шедула напоминаний (design D19).
        _notify_scheduler()
    return updated


# ------------------------------------------------------- Конфликты (4.4)

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"  # design D4: строка UTC в БД


def _parse_utc(ts: str) -> datetime:
    """Строка UTC БД «YYYY-MM-DD HH:MM:SS» → aware-datetime (design D4)."""
    dt = datetime.strptime(ts, _TS_FORMAT)
    return dt.replace(tzinfo=timezone.utc)


def _interval_events(
    events: list[dict], start_index: int, now_utc: datetime
) -> tuple[datetime, datetime, list[dict]]:
    """Интервал сна [начало, конец] и его события-концы (design D11).

    Начало — sleep-событие на позиции start_index; конец — ближайшее
    следующее wake (хронология list_by_user); незакрытый sleep тянется
    до момента расчёта (как в build_sleep_intervals, design D9).
    """
    start_ev = events[start_index]
    end_ev = next(
        (
            ev
            for ev in events[start_index + 1:]
            if ev["event_type"] == KIND_WAKE
        ),
        None,
    )
    start = _parse_utc(start_ev["timestamp"])
    end = (
        _parse_utc(end_ev["timestamp"]) if end_ev is not None else now_utc
    )
    members = [start_ev] if end_ev is None else [start_ev, end_ev]
    return start, end, members


def detect_conflict(
    conn: sqlite3.Connection,
    user_id: int,
    kind: str,
    when_utc: str,
) -> dict | None:
    """Целевое событие конфликта для ввода задним числом (FR-18, D11).

    kind/when_utc — вид и момент НОВОГО ввода (when_utc — строка UTC
    «YYYY-MM-DD HH:MM:SS», design D4). Конфликт — пересечение момента с
    существующим интервалом сна (границы включены: «Проснулся в 13:30» +
    ввод «Заснул 13:30» — конфликт, сценарий §7.13) ИЛИ нарушение порядка
    sleep/wake (два одинаковых вида подряд после вставки).

    Детерминированный выбор цели (design D11):
      - при пересечениях — БЛИЖАЙШЕЕ конфликтующее по времени событие
        (минимум |t − ts|; ничья — более раннее, затем id);
      - при нарушении порядка — ПОСЛЕДНЕЕ (по времени, затем id)
        существующее событие, приводящее к нарушению.
    Исключение (BUG-001): незакрытый хвостовой sleep (последнее событие —
    sleep без последующего wake) и ввод wake ПОЗЖЕ его начала — это
    штатное закрытие пары (как кнопки «сейчас», §4.1), а не конфликт:
    возвращается None, событие записывается как есть. Все прежние
    конфликты сохраняются: пересечение с закрытой парой, точное
    совпадение, нарушение порядка (D11 — для настоящих конфликтов);
    wake РАНЬШЕ начала незакрытого sleep без пересечений и нарушения
    порядка записывается как есть (спека D11, TC-BFS-003).
    Пересечение проверяется первым; нарушения порядка — когда пересечения
    нет. None — конфликта нет, ввод можно записывать как есть.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"недопустимый вид события: {kind!r}")
    t = _parse_utc(when_utc)
    events = SleepEventsRepo(conn).list_by_user(user_id)
    if not events:
        return None
    now_utc = datetime.now(timezone.utc)

    # Незакрытый хвостовой sleep (BUG-001): последнее событие — sleep,
    # парного wake после него в хронологии нет. Ввод wake ПОЗЖЕ его
    # начала закрывает интервал штатно (§4.1) — такой интервал НЕ
    # пересечение ниже; РАНЬШЕ — конфликт (проверка после п. 2).
    open_sleep_start: datetime | None = None
    if events[-1]["event_type"] == KIND_SLEEP:
        open_sleep_start = _parse_utc(events[-1]["timestamp"])

    # 1) Пересечения интервалов сна (границы включены — сценарий §7.13).
    candidates: list[tuple[dict, float]] = []
    seen_ids: set[int] = set()
    for i, ev in enumerate(events):
        if ev["event_type"] != KIND_SLEEP:
            continue
        start, end, members = _interval_events(events, i, now_utc)
        if (
            kind == KIND_WAKE
            and open_sleep_start is not None
            and start == open_sleep_start
            and t > open_sleep_start
        ):
            # BUG-001: wake позже начала незакрытого сна — штатное
            # закрытие пары, не конфликт (создаётся без §7.13).
            continue
        if not (start <= t <= end):
            continue
        for member in members:
            if member["id"] in seen_ids:
                continue
            seen_ids.add(member["id"])
            distance = abs(
                (t - _parse_utc(member["timestamp"])).total_seconds()
            )
            candidates.append((member, distance))
    # Точное совпадение момента с существующим событием — тоже пересечение
    # (сценарий §7.13: «Проснулся в 13:30» записан, ввод «Заснул 13:30» —
    # нулевой зазор недопустим, даже если интервал сна перед wake не записан).
    for ev in events:
        if ev["id"] in seen_ids:
            continue
        if _parse_utc(ev["timestamp"]) == t:
            seen_ids.add(ev["id"])
            candidates.append((ev, 0.0))
    if candidates:
        best = min(
            candidates,
            key=lambda c: (c[1], _parse_utc(c[0]["timestamp"]), c[0]["id"]),
        )
        return best[0]

    # 2) Нарушение порядка sleep/wake: соседи нового момента в хронологии.
    prev_ev: dict | None = None
    next_ev: dict | None = None
    for ev in events:
        if _parse_utc(ev["timestamp"]) <= t:
            prev_ev = ev
        elif next_ev is None:
            next_ev = ev
            break
    violators = [
        ev
        for ev in (prev_ev, next_ev)
        if ev is not None and ev["event_type"] == kind
    ]
    if violators:
        # Последнее событие, приводящее к нарушению (D11): максимум по
        # времени, затем id.
        return max(
            violators,
            key=lambda ev: (_parse_utc(ev["timestamp"]), ev["id"]),
        )
    return None


# --------------------------------- Единый конфликт-чек пары (3.1, FR-51)

def detect_pair_conflict(
    conn: sqlite3.Connection,
    user_id: int,
    sleep_utc: str,
    wake_utc: str,
) -> dict | None:
    """ЕДИНЫЙ конфликт-чек ПАРЫ задним числом (задача 3.1, FR-51, D23).

    Пара — интервал [sleep, wake]; проверяется ЦЕЛИКОМ, одним вызовом
    (D23: «один конфликт-чек на пару»):
      1) порядок внутри пары: wake <= sleep — отказ как конфликт (D23);
         целью показывается засыпание ПАРЫ (id None — существующего
         события нет, текст §7.13 тот же);
      2) пересечение [sleep, wake] (границы включены — D11: «Проснулся
         в 13:30» + «Заснул 13:40» на границе) с интервалами сна
         пользователя (незакрытый тянется до момента расчёта, D9) —
         и/или существующее событие ВНУТРИ пары (вставка пары вокруг
         чужой записи разорвала бы хронологию sleep/wake).

    Выбор цели — детерминированный D11-стиль: минимум расстояния момента
    события до пары (0 — внутри), ничья — более раннее событие, затем id.
    None — конфликта нет, ОБА события можно записывать как есть.

    Существующие проверки (detect_conflict) НЕ меняются: парный ввод —
    отдельная функция (санкция ПМ на 3.1: behavior существующих проверок
    сохранён, event.py только дополняется).
    """
    s = _parse_utc(sleep_utc)
    w = _parse_utc(wake_utc)
    if w <= s:
        # Порядок пары: пробуждение не позже засыпания — отказ как
        # конфликт (D23); цель — засыпание самой пары.
        return {"id": None, "event_type": KIND_SLEEP, "timestamp": sleep_utc}

    events = SleepEventsRepo(conn).list_by_user(user_id)
    if not events:
        return None
    now_utc = datetime.now(timezone.utc)

    def _distance(ts: datetime) -> float:
        """Расстояние момента до пары: 0 — внутри, иначе до границы."""
        if s <= ts <= w:
            return 0.0
        return min(abs((ts - s).total_seconds()), abs((ts - w).total_seconds()))

    candidates: list[tuple[dict, float]] = []
    seen_ids: set[int] = set()

    def _consider(ev: dict) -> None:
        if ev["id"] in seen_ids:
            return
        seen_ids.add(ev["id"])
        candidates.append((ev, _distance(_parse_utc(ev["timestamp"]))))

    # 1) Пересечение интервала пары с интервалами сна (границы включены).
    for i, ev in enumerate(events):
        if ev["event_type"] != KIND_SLEEP:
            continue
        start, end, members = _interval_events(events, i, now_utc)
        if start <= w and s <= end:
            for member in members:
                _consider(member)

    # 2) Существующее событие внутри [sleep, wake] (в т.ч. на границе) —
    # хронологию sleep/wake вставкой пары не меняем.
    for ev in events:
        ts = _parse_utc(ev["timestamp"])
        if s <= ts <= w:
            _consider(ev)

    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda c: (c[1], _parse_utc(c[0]["timestamp"]), c[0]["id"]),
    )
    return best[0]
