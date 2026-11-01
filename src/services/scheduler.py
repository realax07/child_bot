"""Планировщик напоминаний — единственный APScheduler в процессе бота
(change-пакет add-recommendations, задача 3.1; design D19).

Задачи (ТЗ §4.7, §8, §10.6 в объёме пакета 2; FR-39, FR-40, FR-41):
  - push §7.23 за remind_before (настройка, дефолт 15 мин) до ОТКРЫТИЯ
    окна дневного сна (NapWindow.start) — с окном и текущим бодрствованием;
  - push §7.24 за remind_before до РАСЧЁТНОГО рекомендованного времени
    ночного окна (NightWindow.recommended = max(22:00, момент достижения
    12 ч 10 мин), ОВ-4.2) — с бодрствованием и якорями 22:00/22:30;
  - push — bot.send_message в чат владельца (telegram_id из users).

Идемпотентный ре-шедул (FR-41): пересчёт при КАЖДОМ событии
(create/overwrite — точки services.event.create_event/overwrite_event;
delete — handlers.undo) и при старте процесса. Задача по id
«remind:<user_id>:<kind>» заменяется целиком — дубль не появляется;
момент в прошлом план НЕ включает — «в прошлое» не шлём, и отработавшие
за сегодня пуши после рестарта не повторяются (рестарт — тот же расчёт
на новом «сейчас»).

Ребёнок уже спит (незакрытый ночной сон) — ночных пушей нет до утра
(FR-40, решение ОВ-4.2 «повторных ночных пушей нет»); задачу снимает
тот же ре-шедул по событию засыпания.

Логика окон — ТОЛЬКО CalculationService (nap_window / night_window /
total_wake_at; ТЗ §4.6 «логика та же», design D16/D17): собственной
арифметики бодрствования здесь нет. Тексты пушей — src/texts.py
(§7.23/§7.24 дословно). Часы — инъекция clock (aware UTC): тесты идут
на фальшивых часах, без реального ожидания (design D19).

Реализация — APScheduler (AsyncIOScheduler) напрямую; push-задача
пересчитывает план на момент срабатывания и шлёт только актуальный push,
поэтому сдвинувшееся окно (событие между постановкой и запуском)
никогда не отправляет устаревший текст.

Слой services (design D1): репозитории — да, Telegram API — только
переданный bot.send_message (граница процесса — src/main.py).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from src import texts
from src.db import init_db
from src.repositories.sleep_events import SleepEventsRepo
from src.repositories.users import UsersRepo
from src.services.backfill_parser import resolve_tz
from src.services.calculation import (
    _as_hhmm,
    _as_minutes,
    _setting_value,
    _unclosed_interval,
    build_sleep_intervals,
    classify_sleep,
    merge_intervals,
    nap_window,
    night_window,
    total_wake_at,
)
from src.services.event import list_raw_events
from src.services.settings import SettingsService

__all__ = [
    "KIND_NAP",
    "KIND_NIGHT",
    "JOB_ID_TEMPLATE",
    "PushPlan",
    "ReminderScheduler",
    "compute_push_plan",
    "send_push_job",
    "notify_events_changed",
    "start_reminder_scheduler",
    "shutdown_reminder_scheduler",
]

# Виды push-задач (id задачи «remind:<user_id>:<kind>», job replace).
KIND_NAP = "nap"      # §7.23 — до окна дневного сна (FR-39)
KIND_NIGHT = "night"  # §7.24 — до ночного окна (FR-40)

JOB_ID_TEMPLATE = "remind:{user_id}:{kind}"

# Допуск задачи на момент срабатывания: план пересчитывается в момент
# запуска задачи, и push считается актуальным, пока его расчётный момент
# в пределах допуска от «сейчас» (дрейф цикла событий, не простой после
# рестарта — планировщик строится заново на новом «сейчас»).
FIRE_GRACE = timedelta(minutes=5)


@dataclass(frozen=True)
class PushPlan:
    """Один запланированный push (результат compute_push_plan).

    Attributes:
        kind: KIND_NAP / KIND_NIGHT — идентификатор задачи.
        user_id: id владельца дневника в users.
        chat_id: чат владельца (telegram_id из users, design D19).
        run_at_utc: момент отправки, aware UTC (за remind_before до окна).
        text: текст пуша §7.23/§7.24 с цифрами на момент расчёта; задача
            пересчитывает план при срабатывании — шлётся текст именно
            того момента (ТЗ §4.7: цифры актуальны на момент пуша).
    """

    kind: str
    user_id: int
    chat_id: int
    run_at_utc: datetime
    text: str


def _local_naive(utc_moment: str | datetime, tz_name: str | None) -> datetime:
    """UTC-момент (строка БД «YYYY-MM-DD HH:MM:SS» или datetime) →
    naive datetime в поясе пользователя (design D4) — как в handlers."""
    if isinstance(utc_moment, str):
        utc_moment = datetime.strptime(
            utc_moment, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    return utc_moment.astimezone(resolve_tz(tz_name or "")).replace(tzinfo=None)


def _to_utc(local_naive: datetime, tz_name: str | None) -> datetime:
    """Naive-момент в поясе пользователя → aware UTC (обратный _local_naive)."""
    return local_naive.replace(tzinfo=resolve_tz(tz_name or "")).astimezone(
        timezone.utc
    )


def compute_push_plan(
    conn: sqlite3.Connection,
    user: dict,
    now_utc: datetime,
    *,
    past_grace: timedelta = timedelta(0),
) -> list[PushPlan]:
    """Ближайшие push-планы владельца на момент now_utc (FR-39, FR-40).

    Чистая точка решения: события/настройки читаются при каждом вызове
    (FR-24, design D14), окна — ТОЛЬКО CalculationService (design D16,
    ТЗ §4.6). Правила включения:
      - окно дневного сна есть (факт «Подъем» страницы) и момент
        «окно.start − remind_before» ещё НЕ в прошлом (с допуском
        past_grace для пересчёта в момент срабатывания задачи);
      - ночное окно: рекомендованное время (ОВ-4.2) минус remind_before
        ещё не в прошлом; ребёнок УЖЕ спит (незакрытый ночной сон) —
        ночного пуша нет до утра (FR-40, ОВ-4.2);
      - план — НЕ ЛОГИ: отработавший (ушедший в прошлое) момент в план
        не попадает, поэтому после рестарта отработавшие пуши не
        повторяются (FR-41).

    Args:
        conn: открытое соединение БД.
        user: dict владельца (UsersRepo-поля: id, telegram_id, timezone).
        now_utc: момент расчёта, aware UTC.
        past_grace: допуск «в прошлое» (только для задачи push в момент
            срабатывания; планировщик при постановке задач использует 0).

    Returns:
        list[PushPlan] — до двух планов (дневной и ночной push).
    """
    user_id = user["id"]
    chat_id = user["telegram_id"]
    tz_name = user.get("timezone")
    settings = SettingsService(conn).get_all(user_id)
    remind_before = timedelta(minutes=_as_minutes(
        _setting_value(settings, "remind_before", 15)
    ))
    now_local = _local_naive(now_utc, tz_name)

    events: list[tuple[str, datetime]] = [
        (row["event_type"], _local_naive(row["timestamp"], tz_name))
        for row in list_raw_events(conn, user_id)
    ]

    # Незакрытый сон — единый хелпер расчётов (design D16, minor-3
    # review-001-1.1); «уже спит ночной сном» гасит ночной push (FR-40).
    day_start = _as_hhmm(_setting_value(settings, "day_start", "06:00"))
    ordered = sorted(events, key=lambda item: item[1])
    merged = merge_intervals(
        build_sleep_intervals(events, now_local=now_local, day_start=day_start),
        _as_minutes(_setting_value(settings, "merge_threshold", 20)),
    )
    unclosed = _unclosed_interval(ordered, merged)
    night_sleeping = unclosed is not None and classify_sleep(
        unclosed.start_local, unclosed.end_local
    ).is_night

    earliest = now_local - past_grace
    plan: list[PushPlan] = []

    # FR-39: push за remind_before до ОТКРЫТИЯ окна дневного сна (§7.23).
    nap = nap_window(events, now_local, settings)
    if nap is not None:
        run_at = nap.start - remind_before
        if run_at > earliest:
            total = total_wake_at(events, now_local, settings)
            plan.append(PushPlan(
                kind=KIND_NAP,
                user_id=user_id,
                chat_id=chat_id,
                run_at_utc=_to_utc(run_at, tz_name),
                text=texts.reminder_nap_text(
                    texts.reminder_minutes_line(int(remind_before.total_seconds() // 60)),
                    f"{nap.start:%H:%M}",
                    f"{nap.end:%H:%M}",
                    texts.reminder_duration_line(total),  # §7.23: «5 ч 05 мин»
                ),
            ))

    # FR-40: push за remind_before до РАСЧЁТНОГО рекомендованного времени
    # ночного окна (ОВ-4.2: max(night_target, достижение 12 ч 10 мин));
    # спящий (незакрытый ночной сон) — ночных пушей нет до утра.
    if not night_sleeping:
        night = night_window(events, now_local, settings)
        run_at = night.recommended - remind_before
        if run_at > earliest:
            plan.append(PushPlan(
                kind=KIND_NIGHT,
                user_id=user_id,
                chat_id=chat_id,
                run_at_utc=_to_utc(run_at, tz_name),
                text=texts.reminder_night_text(
                    texts.reminder_duration_line(night.wake_total),  # §7.24
                    f"{night.target:%H:%M}",
                    f"{night.deadline:%H:%M}",
                ),
            ))
    return plan


async def send_push_job(
    bot,
    db_path: str | Path,
    user_id: int,
    kind: str,
    *,
    now_utc: datetime | None = None,
) -> None:
    """Задача push (ТЗ §10.6 send_reminder): отправить АКТУАЛЬНЫЙ push.

    Пересчитывает план на момент срабатывания (§4.7 «пересчитываются при
    каждом событии… окна сдвигаются»): если события сдвинули окно, а
    ре-шедул ещё не заменил задачу, устаревший момент в свежем плане
    отсутствует — пуш не уходит вовсе (никогда не шлём «в прошлое» и
    старое окно). Текст строится по цифрам момента срабатывания.
    now_utc — инъекция фальшивых часов для тестов; None — реальные.
    """
    moment = now_utc if now_utc is not None else datetime.now(timezone.utc)
    conn = init_db(db_path)
    try:
        user = next(
            (u for u in UsersRepo(conn).list_all(role="parent")
             if u["id"] == user_id),
            None,
        )
        if user is None:
            return
        plan = compute_push_plan(conn, user, moment, past_grace=FIRE_GRACE)
    finally:
        conn.close()
    for item in plan:
        # Только СВОЙ вид и момент в допуске от срабатывания: план без
        # past_grace не включает прошлое, поэтому повторных пушей по
        # тому же окну нет (FR-41).
        if item.kind == kind and item.run_at_utc <= moment + FIRE_GRACE:
            await bot.send_message(chat_id=item.chat_id, text=item.text)


class ReminderScheduler:
    """Обёртка единственного APScheduler в процессе (design D19).

    Постановка задач — job replace по детерминированному id
    «remind:<user_id>:<kind>»: повторный reschedule заменяет момент,
    исчезнувшее окно снимает задачу. AsyncIOScheduler стартует в цикле
    бота (src/main.py, post_init); в тестах используется незапущенный —
    моменты задач читаются по триггеру, без реального ожидания.
    """

    def __init__(
        self,
        bot,
        db_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        scheduler: AsyncIOScheduler | None = None,
    ) -> None:
        self._bot = bot
        self._db_path = Path(db_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._aps = (
            scheduler
            if scheduler is not None
            else AsyncIOScheduler(timezone=timezone.utc)
        )

    # -------------------------------------------------- жизненный цикл

    def start(self) -> None:
        """Старт и первичный ре-шедул из событий (FR41, design D19/PB-7)."""
        if not self._aps.running:
            self._aps.start()
        _set_active(self)
        self.reschedule()

    def shutdown(self) -> None:
        """Останов (post_shutdown PTB): активный инстанс снимается."""
        if _get_active() is self:
            _set_active(None)
        if self._aps.running:
            self._aps.shutdown(wait=False)

    # ------------------------------------------------------- ре-шедул

    def reschedule(self) -> None:
        """Идемпотентный ре-шедул напоминаний (FR-41).

        План строится заново по актуальным событиям и настройкам; каждая
        задача ставится заменой по id (дубль не появляется), окна, которых
        больше нет (событие удалено/окно ушло в прошлое), снимаются.
        """
        now_utc = self._clock()
        conn = init_db(self._db_path)
        try:
            planned: dict[tuple[int, str], PushPlan] = {}
            for user in UsersRepo(conn).list_all(role="parent"):
                for item in compute_push_plan(conn, user, now_utc):
                    planned[(item.user_id, item.kind)] = item
        finally:
            conn.close()

        for (user_id, kind), item in planned.items():
            self._set_job(
                JOB_ID_TEMPLATE.format(user_id=user_id, kind=kind),
                item.run_at_utc,
                [user_id, kind],
            )
        # Задачи, чьи окна исчезли из плана (удаление события, спящая
        # ночь и т.п.), снимаются — иначе сработала бы устаревшая.
        for key in self._managed_keys() - set(planned):
            self._remove_job(JOB_ID_TEMPLATE.format(user_id=key[0], kind=key[1]))

    def _managed_keys(self) -> set[tuple[int, str]]:
        """Ключи (user_id, kind) задач, поставленных этим планировщиком."""
        keys: set[tuple[int, str]] = set()
        for job in self._aps.get_jobs():
            parts = job.id.split(":")
            if len(parts) == 3 and parts[0] == "remind":
                try:
                    keys.add((int(parts[1]), parts[2]))
                except ValueError:
                    continue
        return keys

    def _set_job(
        self, job_id: str, run_at_utc: datetime, args: list
    ) -> None:
        """Поставить/заменить задачу push (job replace, design D19)."""
        self._remove_job(job_id)
        self._aps.add_job(
            send_push_job,
            trigger=DateTrigger(run_date=run_at_utc),
            args=[self._bot, self._db_path, *args],
            id=job_id,
            name=f"push:{job_id}",
            replace_existing=True,
            misfire_grace_time=int(FIRE_GRACE.total_seconds()),
        )

    def _remove_job(self, job_id: str) -> None:
        """Снять задачу; нет задачи — данных нет изменений (идемпотентно)."""
        try:
            self._aps.remove_job(job_id)
        except Exception:
            # JobLookupError (и pending-вариант) — задачи нет, снимать
            # нечего; это нормальный путь идемпотентного ре-шедула.
            pass

    # -------------------------------------------------- тестовый доступ

    def planned_fire_times(self) -> dict[str, datetime | None]:
        """Моменты запуска задач по триггерам (фальшивые часы в тестах —
        без ожидания срабатывания; у DateTrigger момент детерминирован)."""
        now = self._clock()
        return {
            job.id: job.trigger.get_next_fire_time(None, now)
            for job in self._aps.get_jobs()
        }


# ------------------------------------------------- ре-шедул по событиям

_active: ReminderScheduler | None = None


def _get_active() -> ReminderScheduler | None:
    return _active


def _set_active(instance: ReminderScheduler | None) -> None:
    global _active
    _active = instance


def notify_events_changed() -> None:
    """Точка вызова из мутаций событий (FR-41; create/overwrite/delete).

    Вызывается из services.event.create_event / overwrite_event и
    handlers.undo после успешного изменения данных. Без активного
    планировщика (юнит-тесты сервисов, --check) — no-op: сервисы событий
    не зависят от планировщика. Сбой ре-шедула не ломает запись события —
    следующий ре-шедул (событие/старт) приведёт план к актуальному.
    """
    if _active is None:
        return
    try:
        _active.reschedule()
    except Exception:  # pragma: no cover — защитный перехват (см. выше)
        logging.getLogger(__name__).warning(
            "Ре-шедул напоминаний не выполнен", exc_info=True
        )


def start_reminder_scheduler(bot, db_path: str | Path) -> ReminderScheduler:
    """Создать, зарегистрировать и запустить планировщик (src/main.py)."""
    scheduler = ReminderScheduler(bot, db_path)
    scheduler.start()
    return scheduler


def shutdown_reminder_scheduler() -> None:
    """Остановить активный планировщик (если был запущен)."""
    if _active is not None:
        _active.shutdown()
