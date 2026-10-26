"""Чистый парсер ручного ввода времени (диалог заднего числа).

Форматы (FR-16, ТЗ §4.4; design D10):
  - «HH:MM» — сегодня в TZ пользователя (ведущий ноль часа не обязателен);
  - «вчера HH:MM» — прошлый КАЛЕНДАРНЫЙ день 00:00–23:59 локального времени
    (решение ПМ 26.10 №4: «вчера» — календарная дата, не «24 часа назад»);
  - «завтра HH:MM» — распознаётся, но всегда в будущем (см. FUTURE);
  - «DD.MM HH:MM» — конкретная дата ТЕКУЩЕГО года.

Исходы:
  - распознано → timezone-aware datetime в UTC;
  - распознано, но момент в будущем относительно now_utc → сентинел
    FUTURE (FR-20: отказ с предупреждением; фаза диалога сохраняется);
  - распознано, но старше max_age_days назад → сентинел TOO_OLD
    (FR-17, design D14: глубина ввода backfill_depth_days из settings,
    ответ §7.15 без записи; «старше» — СТРОГО больше глубины, момент
    ровно на границе проходит);
  - нераспознано (мусор, невалидные часы/минуты/дата) → None
    (FR-16: ответ §7.12 с повторным запросом).

None (мусор), FUTURE (будущее) и TOO_OLD (слишком давно) — РАЗНЫЕ
исходы с разными ответами бота. Глубина приходит аргументом (handler
читает её из SettingsService — design D14, без хардкода дефолта).

Чистая функция: без Telegram, БД и глобального времени — момент ввода
передаётся аргументом now_utc, пояс — аргументом tz.

Трассировка: FR-16, FR-17, FR-20; design D10, D14; решение №4
(decisions/2026-10-26-pm-questions-v1-3.md).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

__all__ = ["FUTURE", "TOO_OLD", "parse_time_input", "resolve_tz"]


class _FutureMoment:
    """Сентинел исхода «распознано, но время ещё не наступило» (FR-20)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return "FUTURE"


#: Единственный экземпляр сентинела; проверка исхода — ``parsed is FUTURE``.
FUTURE = _FutureMoment()


class _TooOldMoment:
    """Сентинел исхода «распознано, но старше глубины ввода» (FR-17)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return "TOO_OLD"


#: Единственный экземпляр сентинела; проверка исхода — ``parsed is TOO_OLD``.
TOO_OLD = _TooOldMoment()

# ЧЧ:ММ — ведущий ноль часа не обязателен («7:15»), секунды не допускаются.
_TIME = r"(?P<hour>\d{1,2}):(?P<minute>\d{2})"
_RE_TODAY = re.compile(rf"^{_TIME}$")
_RE_YESTERDAY = re.compile(rf"^вчера\s+{_TIME}$", re.IGNORECASE)
_RE_TOMORROW = re.compile(rf"^завтра\s+{_TIME}$", re.IGNORECASE)
# «08.10 7:15» — точка-разделитель даты, день/месяц без обязательного нуля.
_RE_DATE = re.compile(rf"^(?P<day>\d{{1,2}})\.(?P<month>\d{{1,2}})\s+{_TIME}$")

_FALLBACK_TZ = "Europe/Moscow"  # тот же fallback, что в events.py (design D4)

# Порядок разбора: явная дата первой, затем смещения от локального «сегодня».
# (pattern, day_offset): смещение применяется только к форматам без даты.
_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (_RE_DATE, 0),
    (_RE_TODAY, 0),
    (_RE_YESTERDAY, -1),
    (_RE_TOMORROW, 1),
)


def resolve_tz(tz: str | ZoneInfo) -> ZoneInfo:
    """Имя пояса или ZoneInfo → ZoneInfo; некорректное имя — fallback.

    Единственная точка нормализации пояса пользователя (design D4):
    handler передаёт имя из БД как есть, без собственного fallback.
    """
    if isinstance(tz, ZoneInfo):
        return tz
    try:
        return ZoneInfo(tz) if tz else ZoneInfo(_FALLBACK_TZ)
    except Exception:  # некорректное имя пояса в БД — аудитория бота
        return ZoneInfo(_FALLBACK_TZ)


def _as_utc(now_utc: datetime) -> datetime:
    """Момент ввода в UTC; naive-аргумент трактуется как UTC."""
    if now_utc.tzinfo is None:
        return now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(timezone.utc)


def parse_time_input(
    text: str,
    tz: str | ZoneInfo,
    now_utc: datetime,
    max_age_days: int | None = None,
) -> datetime | _FutureMoment | _TooOldMoment | None:
    """Разобрать ручной ввод времени диалога заднего числа (FR-16..FR-20).

    Args:
        text: текст сообщения пользователя.
        tz: пояс пользователя (имена IANA или ZoneInfo) — все форматы
            интерпретируются в локальном времени пользователя (design D4).
        now_utc: момент ввода (aware-UTC; naive трактуется как UTC) —
            точка отсчёта «сегодня/вчера» и проверок «в будущем»/«давно».
        max_age_days: максимальная глубина ввода задним числом, дней
            (backfill_depth_days из settings, design D14); None — без
            проверки глубины. «Старше» — строго больше: момент ровно
            max_age_days назад проходит (граница включена, FR-17).

    Returns:
        timezone-aware datetime в UTC — распознанное прошедшее время;
        ``FUTURE`` — распознано, но момент позже now_utc (предупреждение);
        ``TOO_OLD`` — распознано, но старше max_age_days (§7.15);
        ``None`` — ввод не распознан (§7.12 с повторным запросом).
    """
    zone = resolve_tz(tz)
    now = _as_utc(now_utc)
    now_local = now.astimezone(zone)

    stripped = (text or "").strip()
    for pattern, day_offset in _PATTERNS:
        match = pattern.match(stripped)
        if match is not None:
            local_dt = _compose_local(match, day_offset, now_local)
            if local_dt is None:
                return None
            return _to_utc_or_future(local_dt, zone, now, max_age_days)
    return None


def _compose_local(
    match: re.Match[str], day_offset: int, now_local: datetime
) -> datetime | None:
    """Собрать naive-локальный момент из совпадения одного из форматов.

    Даты формата «DD.MM» относятся к текущему году; невалидные
    часы/минуты/дата → None (мусор, FR-16).
    """
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    if "day" in match.groupdict():  # формат «DD.MM HH:MM»
        day = int(match.group("day"))
        month = int(match.group("month"))
        try:
            return datetime(now_local.year, month, day, hour, minute)
        except ValueError:  # 32.13, 31.02 и т.п. — не дата
            return None

    # «HH:MM», «вчера HH:MM», «завтра HH:MM» — от локального «сегодня».
    return (now_local + timedelta(days=day_offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )



def _to_utc_or_future(
    local_dt: datetime, zone: ZoneInfo, now: datetime, max_age_days: int | None
) -> datetime | _FutureMoment | _TooOldMoment:
    """Локальный naive-момент → aware-UTC; исходы FUTURE/TOO_OLD (FR-17/20)."""
    parsed_utc = local_dt.replace(tzinfo=zone).astimezone(timezone.utc)
    if parsed_utc > now:
        return FUTURE
    if max_age_days is not None and (now - parsed_utc) > timedelta(
        days=max_age_days
    ):
        return TOO_OLD
    return parsed_utc
