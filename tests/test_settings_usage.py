"""Сквозное чтение настроек расчётами — аудит-фиксация (задача 5.2).

Трассировка: FR-24 (сценарий «Настройка влияет на расчёт»); design D14.

Аудит-фиксация, НЕ переделка: по задачам 3.1–3.4 и 4.3 расчёты уже
параметризованы. Эти тесты фиксируют контракт (design D14: настройки
читаются при каждом расчёте, кэша нет):

  - порог склейки — из settings через SettingsService при каждом расчёте;
    смена порога меняет результат daily_summary БЕЗ перезаписи событий;
  - глубина ввода backfill_depth_days — из settings; смена меняет вердикт
    backfill-обработчика (§7.15 ↔ §7.14);
  - границы ночи night_bounds — из settings; смена меняет разметку
    classify_sleep;
  - день старта страницы day_start — из settings; смена меняет отнесение
    сна к странице (sleep_page);
  - grep-аудит (AST): в calculation.py / backfill_parser.py нет хардкодов
    порогов ТЗ §5 — числовых литералов 20/3/19/7 вне дефолтов аргументов.

Документированные design-допущения (D7) — не настройки и не хардкоды
порогов: WARN_GAP = 10 мин и SHORT_NIGHT_SLEEP_THRESHOLD = 40 мин
(«требует проверки»), их AST-тест не запрещает.

Правило rule 6: каждый тест несёт TC-SETU-NNN в первой строке докстринга.
"""

from __future__ import annotations

import ast
import asyncio
from collections import Counter
from dataclasses import fields as dataclass_fields
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src import texts
from src.db import init_db
from src.handlers import backfill
from src.handlers.events import PENDING_KIND_KEY, WAIT_EVENT_TIME_KEY
from src.repositories.settings import SettingsRepo
from src.repositories.sleep_events import SleepEventsRepo
from src.services.calculation import (
    DailySummary,
    classify_sleep,
    daily_summary,
    sleep_page,
)
from src.services.event import get_user
from src.services.settings import SettingsService

TZ = ZoneInfo("Europe/Moscow")  # UTC+3, без перехода на летнее время

_SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "services"


# --------------------------------------------------------------- helpers


def _to_utc_str(local_naive: datetime) -> str:
    """Локальный naive-момент (Moscow) → строка UTC «YYYY-MM-DD HH:MM:SS»."""
    aware = local_naive.replace(tzinfo=TZ).astimezone(timezone.utc)
    return aware.strftime("%Y-%m-%d %H:%M:%S")


def _from_utc_str(raw: str) -> datetime:
    """Строка UTC из БД → локальный naive-момент (Moscow)."""
    aware = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    return aware.astimezone(TZ).replace(tzinfo=None)


def _parse_hhmm(raw: str) -> time:
    """«ЧЧ:ММ» значения настройки → time (контракт чтения из settings)."""
    hour_s, minute_s = raw.split(":")
    return time(int(hour_s), int(minute_s))


def _parse_bounds(raw: str) -> tuple[time, time]:
    """«ЧЧ:ММ-ЧЧ:ММ» настройки night_bounds → (night_start, night_end)."""
    start_s, end_s = raw.split("-")
    return _parse_hhmm(start_s), _parse_hhmm(end_s)


def _events_from_db(rows: list[dict]) -> list[tuple[str, datetime]]:
    """Сырые строки БД → пары (event_type, локальный naive-момент)."""
    return [
        (row["event_type"], _from_utc_str(row["timestamp"])) for row in rows
    ]


def _raw_snapshot(rows: list[dict]) -> list[tuple]:
    """Сырые поля записей (design D6) — для проверки «перезаписи нет»."""
    return [
        (r["id"], r["event_type"], r["timestamp"], r["created_at"],
         r["is_backfilled"], r["sleep_type"], r["is_merged"])
        for r in rows
    ]


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / "bot.db")
    yield c
    c.close()


def _seeded_user(conn, telegram_id: int) -> dict:
    """Пользователь с 13 дефолтными настройками (как после /start, FR-23)."""
    user = get_user(conn, telegram_id)
    SettingsService(conn).seed_defaults(user["id"])
    return user


# --------------------------------------- TC-SETU-001: порог склейки, E2E


def test_tc_setu_001_merge_threshold_from_settings_changes_summary_not_events(
    conn,
):
    """TC-SETU-001 (FR-24, D14): смена merge_threshold в settings меняет
    результат daily_summary при ТЕХ ЖЕ сырых событиях; записи в
    sleep_events не перезаписываются (склейка — только вычисление, D9).

    Сквозной сценарий: события читаются из БД, порог — из SettingsService;
    после upsert нового порога повторный расчёт даёт другой вердикт.
    """
    user = _seeded_user(conn, 5101)
    repo = SleepEventsRepo(conn)
    # Дневной сон, разбитый 15-минутным пробуждением: 13:00–14:00, 14:15–15:00.
    for kind, local in [
        ("sleep", datetime(2026, 10, 10, 13, 0)),
        ("wake", datetime(2026, 10, 10, 14, 0)),
        ("sleep", datetime(2026, 10, 10, 14, 15)),
        ("wake", datetime(2026, 10, 10, 15, 0)),
    ]:
        repo.insert(user["id"], kind, _to_utc_str(local), is_backfilled=True)

    rows = repo.list_by_user(user["id"])
    snapshot = _raw_snapshot(rows)

    settings = SettingsService(conn)
    # Расчёт читает порог из settings при каждом вызове (D14, без кэша).
    threshold = int(settings.get(user["id"], "merge_threshold"))
    assert threshold == 20  # дефолт ТЗ §5

    events = _events_from_db(repo.list_by_user(user["id"]))
    before = daily_summary(
        events, page_date=datetime(2026, 10, 10).date(),
        merge_threshold_min=threshold,
    )
    assert len(before.sleep_intervals) == 1  # 15-минутное пробуждение
    # СКЛЕИВАЕТСЯ при дефолтном пороге 20 (строгое «меньше», design D9).
    assert before.total_sleep == timedelta(hours=2)

    # Пользователь меняет настройку данными (UI настроек вне пакета, FR-24).
    SettingsRepo(conn).upsert(user["id"], "merge_threshold", "5")

    # Сырые события НЕ изменились (склейка ничего не перезаписывает, D9).
    assert _raw_snapshot(repo.list_by_user(user["id"])) == snapshot

    # Повторный расчёт с тем же кодом чтения даёт ДРУГОЙ результат:
    # 15-минутное пробуждение больше порога 5 — два отдельных сна (FR-14).
    after = daily_summary(
        _events_from_db(repo.list_by_user(user["id"])),
        page_date=datetime(2026, 10, 10).date(),
        merge_threshold_min=int(settings.get(user["id"], "merge_threshold")),
    )
    assert len(after.sleep_intervals) == 2
    assert after.total_sleep == timedelta(hours=1, minutes=45)
    # Наследие TC-SETU-001: DailySummary.total_wake удалён (п.в техдолга
    # 1.3, решение D8 «сумма сна/бодрствования за день смысла не имеет» —
    # по ТЗ показывается только текущее бодрствование). Поле больше
    # НЕ существует в DailySummary.
    assert not any(
        field.name == "total_wake" for field in dataclass_fields(DailySummary)
    )

    # И после смены порога записи по-прежнему идентичны.
    assert _raw_snapshot(repo.list_by_user(user["id"])) == snapshot


# --------------------------------- TC-SETU-002: глубина ввода, handler E2E


class _FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, reply, reply_markup=None):
        self.replies.append(reply)


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeTextUpdate:
    def __init__(self, user_id: int, text: str) -> None:
        self.effective_user = _FakeUser(user_id)
        self.callback_query = None
        self.message = _FakeMessage(text)
        self.effective_message = self.message


class _FakeContext:
    def __init__(self, db_path) -> None:
        self.bot_data = {"db_path": db_path}
        self.user_data: dict = {}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "bot.db"


def _send_backfill_text(db_path, text: str, user_data: dict, telegram_id: int):
    """Провести текстовое сообщение через реальный обработчик backfill."""
    update = _FakeTextUpdate(telegram_id, text)
    context = _FakeContext(db_path)
    context.user_data = user_data
    asyncio.run(backfill.backfill_time_message(update, context))  # type: ignore[arg-type]
    return update, context


def _phase_data() -> dict:
    return {WAIT_EVENT_TIME_KEY: True, PENDING_KIND_KEY: "sleep"}


def test_tc_setu_002_backfill_depth_from_settings_changes_verdict(
    db_path, conn
):
    """TC-SETU-002 (FR-17, FR-24, D14): смена backfill_depth_days в settings
    меняет вердикт backfill-обработчика (§7.15 ↔ §7.14) — обработчик читает
    глубину из SettingsService при каждом вводе, дефолт не хардкодится."""
    user = _seeded_user(conn, 5202)
    now_local = datetime.now(TZ)
    two_days_ago = now_local - timedelta(days=2)
    text = f"{two_days_ago:%d.%m} {two_days_ago:%H:%M}"

    # Глубина 1: 2 дня назад — старше → §7.15 дословно, записи нет.
    SettingsRepo(conn).upsert(user["id"], "backfill_depth_days", "1")
    update, context = _send_backfill_text(
        db_path, text, _phase_data(), user["telegram_id"]
    )
    assert update.effective_message.replies == [texts.BACKFILL_TOO_OLD]
    assert WAIT_EVENT_TIME_KEY not in context.user_data
    assert conn.execute(
        "SELECT COUNT(*) FROM sleep_events WHERE user_id = ?", (user["id"],)
    ).fetchone()[0] == 0

    # Дефолтная глубина 3 (смена настройки данными): тот же ввод проходит —
    # §7.14 со сводкой, запись с is_backfilled=1.
    SettingsRepo(conn).upsert(user["id"], "backfill_depth_days", "3")
    update, context = _send_backfill_text(
        db_path, text, _phase_data(), user["telegram_id"]
    )
    assert len(update.effective_message.replies) == 1
    assert "Событие записано" in update.effective_message.replies[0]
    assert "Окно" not in update.effective_message.replies[0]
    assert WAIT_EVENT_TIME_KEY not in context.user_data

    rows = SleepEventsRepo(conn).list_by_user(user["id"])
    assert len(rows) == 1
    assert rows[0]["is_backfilled"] is True
    assert rows[0]["sleep_type"] is None and rows[0]["is_merged"] is None

    # Обработчик читал именно настройку: сервис возвращает int (контракт D14).
    assert SettingsService(conn).get(user["id"], "backfill_depth_days") == 3


# --------------------------- TC-SETU-003: границы ночи → classify_sleep


def test_tc_setu_003_night_bounds_from_settings_change_marking(conn):
    """TC-SETU-003 (FR-9, FR-24, D7/D14): границы ночи читаются из settings
    (night_bounds) и передаются в classify_sleep; смена настройки меняет
    разметку сна — день/ночь и предупреждение переворачиваются без
    каких-либо записей (разметка на лету, design D6)."""
    user = _seeded_user(conn, 5103)
    settings = SettingsService(conn)

    # Дефолт ТЗ §5: ночь 19:00–07:00.
    night_start, night_end = _parse_bounds(
        str(settings.get(user["id"], "night_bounds"))
    )
    assert (night_start, night_end) == (time(19, 0), time(7, 0))

    # Засыпание 19:30 → ночной (начало >= 19:00).
    mark = classify_sleep(
        datetime(2026, 10, 10, 19, 30), datetime(2026, 10, 10, 22, 0),
        night_start=night_start, night_end=night_end,
    )
    assert mark.is_night is True and mark.warning is None

    # Смена настройки данными: ночь начинается в 20:30 → тот же сон
    # становится ДНЕВНЫМ (19:30 вне окна и вне [20:20; 20:30) предупреждения).
    SettingsRepo(conn).upsert(user["id"], "night_bounds", "20:30-07:00")
    night_start, night_end = _parse_bounds(
        str(settings.get(user["id"], "night_bounds"))
    )
    mark = classify_sleep(
        datetime(2026, 10, 10, 19, 30), datetime(2026, 10, 10, 22, 0),
        night_start=night_start, night_end=night_end,
    )
    assert mark.is_night is False and mark.warning is None

    # Смена КОНЦА ночи тоже читается из настройки: сон 06:30→09:00 при
    # night_end=06:00 — дневной (конец 09:00 > 06:00, начало 06:30 < 20:30).
    SettingsRepo(conn).upsert(user["id"], "night_bounds", "20:30-06:00")
    night_start, night_end = _parse_bounds(
        str(settings.get(user["id"], "night_bounds"))
    )
    mark = classify_sleep(
        datetime(2026, 10, 10, 6, 30), datetime(2026, 10, 10, 9, 0),
        night_start=night_start, night_end=night_end,
    )
    assert mark.is_night is False

    # Предупреждение FR-10 тоже следует за настройкой: начало 20:20 при
    # night_start=20:30 — дневной с текстом ТЗ §4.2 (окно warn_gap = 10 мин).
    mark = classify_sleep(
        datetime(2026, 10, 10, 20, 20), datetime(2026, 10, 10, 21, 0),
        night_start=night_start, night_end=night_end,
    )
    assert mark.is_night is False
    assert mark.warning == "Поздний дневной сон может сдвинуть ночной"


# ----------------------------- TC-SETU-004: день старта → sleep_page


def test_tc_setu_004_day_start_from_settings_changes_page_attribution(conn):
    """TC-SETU-004 (FR-12, FR-24, D8/D14): day_start читается из settings;
    смена границы меняет отнесение сна к странице дня (сон 05:00 при
    day_start=06:00 — страница предыдущего дня, при 04:00 — текущего)."""
    user = _seeded_user(conn, 5104)
    settings = SettingsService(conn)

    day_start = _parse_hhmm(str(settings.get(user["id"], "day_start")))
    assert day_start == time(6, 0)

    night_start_local = datetime(2026, 10, 10, 5, 0)
    page_before = sleep_page(
        night_start_local, datetime(2026, 10, 10, 5, 30), day_start=day_start
    )
    assert page_before == datetime(2026, 10, 9).date()  # 05:00 < 06:00 → назад

    # Смена настройки данными: день начинается в 04:00 → та же страница
    # дня 10.10 (05:00 >= 04:00).
    SettingsRepo(conn).upsert(user["id"], "day_start", "04:00")
    day_start = _parse_hhmm(str(settings.get(user["id"], "day_start")))
    page_after = sleep_page(
        night_start_local, datetime(2026, 10, 10, 5, 30), day_start=day_start
    )
    assert page_after == datetime(2026, 10, 10).date()
    assert page_after != page_before

    # Сводка следует за отнесением (та же настройка, тот же расчёт):
    # весь сон 05:00–05:30 теперь в странице 10.10.
    summary = daily_summary(
        [("sleep", night_start_local), ("wake", datetime(2026, 10, 10, 5, 30))],
        page_date=datetime(2026, 10, 10).date(),
        day_start=day_start,
    )
    assert summary.total_sleep == timedelta(minutes=30)


# --------------------------------------- TC-SETU-005: AST-аудит хардкодов


def test_tc_setu_005_no_threshold_hardcodes_outside_argument_defaults():
    """TC-SETU-005 (FR-24, D14): в calculation.py и backfill_parser.py нет
    хардкодов порогов ТЗ §5 — числовых литералов 20 (порог склейки),
    3 (глубина ввода), 19 и 7 (границы ночи) вне дефолтов аргументов.

    Проверка по AST, а не по тексту: значение литерала должно находиться
    в поддереве дефолта (позиционного или keyword-only) какой-либо
    функции. Дополнительно фиксируем, что параметризация НЕ потеряна:
    дефолты содержат 19/7 (classify_sleep) и 20 (daily_summary), а
    литерал 3 в расчётах не встречается вовсе — глубину ввода парсер
    получает аргументом max_age_days (дефолт 3 живёт в settings).
    """
    forbidden = {3, 7, 19, 20}

    def numeric_constants(node: ast.AST) -> list[int | float]:
        found: list[int | float] = []
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Constant)
                and isinstance(sub.value, (int, float))
                and not isinstance(sub.value, bool)
            ):
                found.append(sub.value)
        return found

    for module_name in ("calculation", "backfill_parser"):
        tree = ast.parse(
            (_SRC_DIR / f"{module_name}.py").read_text(encoding="utf-8")
        )

        # Разрешено: литералы внутри дефолтов аргументов функций.
        allowed: list[int | float] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults = list(node.args.defaults) + [
                    d for d in node.args.kw_defaults if d is not None
                ]
                for default in defaults:
                    allowed.extend(numeric_constants(default))

        everywhere = [
            v for v in numeric_constants(tree) if v in forbidden
        ]
        hardcodes = Counter(everywhere) - Counter(
            [v for v in allowed if v in forbidden]
        )
        assert not hardcodes, (
            f"{module_name}.py: хардкоды порогов вне дефолтов аргументов: "
            f"{dict(hardcodes)} (FR-24, design D14)"
        )

        # Параметризация не потеряна (иначе тест проходил бы «в ноль»).
        allowed_forbidden = Counter(v for v in allowed if v in forbidden)
        if module_name == "calculation":
            assert allowed_forbidden[19] >= 1  # night_start=19:00
            assert allowed_forbidden[7] >= 1   # night_end=07:00
            assert allowed_forbidden[20] >= 1  # merge_threshold_min=20
        else:
            # Глубина ввода (3) — только аргумент max_age_days, без дефолта.
            all_values = Counter(
                v for v in numeric_constants(tree)
            )
            assert all_values[3] == 0, (
                "backfill_parser.py: литерал 3 (глубина ввода) найден — "
                "глубина должна приходить из settings через аргумент"
            )
