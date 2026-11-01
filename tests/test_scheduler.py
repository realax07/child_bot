"""Планировщик напоминаний — add-recommendations 3.1 (design D19).

TC-SCHED-001 (FR-39): push §7.23 за 15 мин до окна дневного сна 13:20
запланирован на 13:05; текст — окно + текущее бодрствование.
TC-SCHED-002 (FR-41): событие до срабатывания сдвинуло окно (перезапись
подъёма) — по хуку services.event.overwrite_event задача ЗАМЕНЕНА:
новый момент верен, задачи с прежним моментом/дублем нет.
TC-SCHED-003 (FR-41): рестарт процесса — свежий планировщик пересчитал
план из событий; отработавший (ушедший в прошлое) push не повторяется.
TC-SCHED-004 (FR-39): N из settings (remind_before=30) сдвигает момент.
TC-SCHED-005 (FR-40): спящий ребёнок (незакрытый ночной сон) — ночных
задач нет; у бодрствующего в тот же момент ночная задача стоит.
TC-SCHED-006 (FR-40): push §7.24 — расчётное рекомендованное время
(ОВ-4.2), текст с бодрствованием и якорями 22:00/22:30; задача push в
момент срабатывания шлёт актуальный текст, устаревшее окно — не шлёт.
TC-SCHED-007 (FR-41): ре-шедул-хук без активного планировщика — no-op
(сервисы событий не зависят от планировщика).

Фальшивые часы: фиксированные now_utc (Европа/Москва = UTC+3), никакого
реального ожидания и time.sleep — задачи ставятся в незапущенный
APScheduler, моменты читаются по триггеру (DateTrigger детерминирован).
Без «живых» ЧЧ:ММ — только фиксированные даты/времена.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import texts
from src.db import init_db
from src.repositories.settings import SettingsRepo
from src.repositories.sleep_events import SleepEventsRepo
from src.repositories.users import UsersRepo
from src.services import scheduler as scheduler_mod
from src.services.calculation import nap_window, night_window
from src.services.event import overwrite_event
from src.services.scheduler import (
    KIND_NAP,
    KIND_NIGHT,
    PushPlan,
    ReminderScheduler,
    compute_push_plan,
    notify_events_changed,
    send_push_job,
)

_TZ = "Europe/Moscow"  # UTC+3, без перехода на летнее время в 2026

# Фиксированные «сейчас» (UTC) и соответствующие моменты МСК:
NOW_1230_MSK = datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc)   # 12:30
NOW_1310_MSK = datetime(2026, 11, 3, 10, 10, tzinfo=timezone.utc)  # 13:10
NOW_2055_MSK = datetime(2026, 11, 3, 17, 55, tzinfo=timezone.utc)  # 20:55
NAP_FIRE_UTC = datetime(2026, 11, 3, 10, 5, tzinfo=timezone.utc)   # 13:05 МСК
NIGHT_FIRE_UTC = datetime(2026, 11, 3, 18, 55, tzinfo=timezone.utc)  # 21:55 МСК

CHAT_ID = 555000


class _FakeBot:
    """Ловит send_message — пушей без реального Telegram (design D19)."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append((chat_id, text))


# ------------------------------------------------------------------ фикстуры


@pytest.fixture()
def env(tmp_path):
    """БД с владельцем (role=parent) и засеянными настройками (дефолты §5)."""
    db_path = tmp_path / "bot.db"
    conn = init_db(db_path)
    user = UsersRepo(conn).get_or_create(CHAT_ID, timezone=_TZ)
    SettingsService = _settings_service()
    SettingsService(conn).seed_defaults(user["id"])
    box = SimpleNamespace(conn=conn, db_path=db_path, user=user)
    yield box
    conn.close()
    scheduler_mod._set_active(None)  # изоляция глобального реестра


def _settings_service():
    from src.services.settings import SettingsService

    return SettingsService


def _add(env, kind: str, ts_utc: str) -> int:
    return SleepEventsRepo(env.conn).insert(env.user["id"], kind, ts_utc)


def _nap_job_id(env) -> str:
    return f"remind:{env.user['id']}:{KIND_NAP}"


def _night_job_id(env) -> str:
    return f"remind:{env.user['id']}:{KIND_NIGHT}"


def _scheduler(env, now_utc: datetime, bot: _FakeBot | None = None):
    return ReminderScheduler(
        bot if bot is not None else _FakeBot(),
        env.db_path,
        clock=lambda: now_utc,
    )


def _seed_standard_day(env) -> int:
    """Ночь 02–03.11: отбой 22:00 МСК → подъём 08:00 МСК; возвращает id wake."""
    _add(env, "sleep", "2026-11-02 19:00:00")  # 22:00 МСК 02.11
    return _add(env, "wake", "2026-11-03 05:00:00")  # 08:00 МСК 03.11


def _seed_late_day(env) -> None:
    """Подъём 09:00 МСК + дневной сон 11:00–12:00 МСК (сценарий спящего)."""
    _add(env, "sleep", "2026-11-02 19:30:00")  # 22:30 МСК 02.11
    _add(env, "wake", "2026-11-03 06:00:00")   # 09:00 МСК
    _add(env, "sleep", "2026-11-03 08:00:00")  # 11:00 МСК
    _add(env, "wake", "2026-11-03 09:00:00")   # 12:00 МСК


# ------------------------------------------------------------------- тесты


def test_tc_sched_001_nap_push_15min_before_window(env):
    """TC-SCHED-001: окно 13:20–14:00, N=15 → push запланирован на 13:05;
    текст §7.23 — окно и бодрствование на момент расчёта."""
    _seed_standard_day(env)
    sched = _scheduler(env, NOW_1230_MSK)
    sched.reschedule()

    times = sched.planned_fire_times()
    # 13:05 МСК = 10:05 UTC — ровно за remind_before до открытия окна.
    assert times[_nap_job_id(env)] == NAP_FIRE_UTC
    # Дублей нет: ровно одна задача nap.
    assert [jid for jid in times if jid.endswith(f":{KIND_NAP}")] == [
        _nap_job_id(env)
    ]

    plan = compute_push_plan(env.conn, env.user, NOW_1230_MSK)
    nap_push = next(p for p in plan if p.kind == KIND_NAP)
    assert nap_push.chat_id == CHAT_ID
    assert nap_push.text == texts.reminder_nap_text(
        texts.reminder_minutes_line(15), "13:20", "14:00",
        texts.format_duration(timedelta(hours=4, minutes=30)),
    )
    assert nap_push.text.startswith(
        "⏰ Через 15 минут открывается окно дневного сна: 13:20–14:00."
    )


def test_tc_sched_002_event_shifts_window_job_replaced(env):
    """TC-SCHED-002: перезапись подъёма 08:00→09:00 сдвинула окно; хук
    overwrite_event заменил задачу — момент новый, дубль на 13:05 не уходит."""
    _seed_standard_day(env)
    sched = _scheduler(env, NOW_1230_MSK)
    sched.reschedule()
    assert sched.planned_fire_times()[_nap_job_id(env)] == NAP_FIRE_UTC

    scheduler_mod._set_active(sched)  # как в живом процессе (design D19)
    try:
        #wake 08:00 МСК → 09:00 МСК (перезапись, FR-18) — окно 14:20–15:00
        wake_id = SleepEventsRepo(env.conn).list_by_user(env.user["id"])[1]["id"]
        overwrite_event(env.conn, wake_id, "wake", "2026-11-03 06:00:00")
    finally:
        scheduler_mod._set_active(None)

    times = sched.planned_fire_times()
    # 14:05 МСК = 11:05 UTC; задачи на 13:05 больше нет, дубля нет.
    assert times[_nap_job_id(env)] == datetime(
        2026, 11, 3, 11, 5, tzinfo=timezone.utc
    )
    assert [jid for jid in times if jid.endswith(f":{KIND_NAP}")] == [
        _nap_job_id(env)
    ]


def test_tc_sched_003_restart_no_replay_of_fired_push(env):
    """TC-SCHED-003: рестарт в 13:10 — nap-push 13:05 в плане не восстанавливается;
    будущий ночной push при этом стоит."""
    _seed_standard_day(env)
    _add(env, "sleep", "2026-11-03 07:30:00")  # дневной сон 10:30 МСК
    _add(env, "wake", "2026-11-03 08:30:00")   # пробуждение 11:30 МСК

    fresh = _scheduler(env, NOW_1310_MSK)  # «новый процесс»
    fresh.reschedule()

    times = fresh.planned_fire_times()
    assert _nap_job_id(env) not in times  # отработавший push не повторяется
    # Ночь ещё впереди: recommend 22:00 МСК → push 21:45 МСК = 18:45 UTC.
    assert times[_night_job_id(env)] == datetime(
        2026, 11, 3, 18, 45, tzinfo=timezone.utc
    )


def test_tc_sched_004_remind_before_changes_moment(env):
    """TC-SCHED-004: remind_before=30 → push за 30 мин (12:50 МСК = 09:50 UTC)."""
    _seed_standard_day(env)
    SettingsRepo(env.conn).upsert(env.user["id"], "remind_before", "30")

    sched = _scheduler(env, NOW_1230_MSK)
    sched.reschedule()
    assert sched.planned_fire_times()[_nap_job_id(env)] == datetime(
        2026, 11, 3, 9, 50, tzinfo=timezone.utc
    )
    plan = compute_push_plan(env.conn, env.user, NOW_1230_MSK)
    nap_push = next(p for p in plan if p.kind == KIND_NAP)
    assert nap_push.text.startswith("⏰ Через 30 минут открывается окно")


def test_tc_sched_005_sleeping_child_no_night_jobs(env):
    """TC-SCHED-005: незакрытый ночной сон — ночных задач нет; у бодрствующего
    в тот же момент ночная задача стоит (разница — только факт засыпания)."""
    _seed_late_day(env)

    awake = _scheduler(env, NOW_2055_MSK)
    awake.reschedule()
    # Подъём 09:00, сон 11:00–12:00 → в 20:55 бодрствование 10 ч 55 мин,
    # 12 ч 10 мин достигаются в 22:10 МСК → recommend 22:10 → push 21:55.
    assert awake.planned_fire_times()[_night_job_id(env)] == NIGHT_FIRE_UTC

    # Ребёнок уснул в 20:30 МСК (незакрытый ночной сон) — ночных пушей нет.
    _add(env, "sleep", "2026-11-03 17:30:00")
    awake.reschedule()
    assert _night_job_id(env) not in awake.planned_fire_times()
    plan = compute_push_plan(env.conn, env.user, NOW_2055_MSK)
    assert [p.kind for p in plan] == []


def test_tc_sched_006_night_push_recommended_time_and_text(env):
    """TC-SCHED-006: ночной push по расчётному рекомендованному времени
    (ОВ-4.2); в момент срабатывания уходит §7.24 с актуальным бодрствованием,
    сдвинувшееся (устаревшее) окно не шлётся."""
    _seed_late_day(env)
    sched = _scheduler(env, NOW_2055_MSK)
    sched.reschedule()
    assert sched.planned_fire_times()[_night_job_id(env)] == NIGHT_FIRE_UTC

    # Текст §7.24: бодрствование на момент срабатывания (21:55 МСК:
    # 12 ч 55 мин − 1 ч сна = 11 ч 55 мин), якоря 22:00/22:30.
    bot = _FakeBot()
    asyncio.run(
        send_push_job(
            bot, env.db_path, env.user["id"], KIND_NIGHT,
            now_utc=NIGHT_FIRE_UTC,
        )
    )
    assert bot.sent == [(
        CHAT_ID,
        "⏰ Пора готовиться ко сну. Суммарное бодрствование: "
        "11 ч 55 мин. Целевое время ухода: 22:00, крайнее — 22:30.",
    )]

    # Окно сдвинулось ДО срабатывания (перезапись подъёма → recommend 23:05):
    # в момент прежнего запуска свежий план устаревший момент не содержит —
    # пуш «в прошлое»/по старому окну не уходит (FR-41).
    wake_id = SleepEventsRepo(env.conn).list_by_user(env.user["id"])[1]["id"]
    overwrite_event(env.conn, wake_id, "wake", "2026-11-03 06:30:00")  # 09:30
    silent_bot = _FakeBot()
    asyncio.run(
        send_push_job(
            silent_bot, env.db_path, env.user["id"], KIND_NIGHT,
            now_utc=NIGHT_FIRE_UTC,
        )
    )
    assert silent_bot.sent == []


def test_tc_sched_006b_nap_push_sends_at_fire_moment(env):
    """TC-SCHED-006 (дневная ветвь): в 13:05 задача шлёт §7.23 в чат владельца;
    расчётные окна взяты только из CalculationService."""
    _seed_standard_day(env)
    bot = _FakeBot()
    asyncio.run(
        send_push_job(
            bot, env.db_path, env.user["id"], KIND_NAP, now_utc=NAP_FIRE_UTC
        )
    )
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == CHAT_ID
    assert text == texts.reminder_nap_text(
        texts.reminder_minutes_line(15), "13:20", "14:00",
        texts.format_duration(timedelta(hours=5, minutes=5)),
    )

    # Единая арифметика: окно плана == nap_window расчётов (D16/D17).
    plan = compute_push_plan(env.conn, env.user, NAP_FIRE_UTC - timedelta(seconds=1))
    nap = nap_window(
        [
            (row["event_type"], _local(row["timestamp"]))
            for row in SleepEventsRepo(env.conn).list_by_user(env.user["id"])
        ],
        NAP_FIRE_UTC.astimezone(_moscow()).replace(tzinfo=None),
    )
    nap_push = next(p for p in plan if p.kind == KIND_NAP)
    assert f"{nap.start:%H:%M}–{nap.end:%H:%M}" in nap_push.text
    assert nap is not None  # подъём есть — окно расчётов не может быть None


def test_tc_sched_007_notify_without_scheduler_is_noop(env):
    """TC-SCHED-007: без активного планировщика хук мутации — no-op."""
    _seed_standard_day(env)
    assert scheduler_mod._get_active() is None
    notify_events_changed()  # не бросает и ничего не планирует


# ----------------------------------------------------------------- хелперы


def _moscow():
    from src.services.backfill_parser import resolve_tz

    return resolve_tz(_TZ)


def _local(ts_utc: str) -> datetime:
    return (
        datetime.strptime(ts_utc, "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=timezone.utc)
        .astimezone(_moscow())
        .replace(tzinfo=None)
    )
