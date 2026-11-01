# Сервис настроек (ТЗ §5; change-пакет add-core-diary, задача 2.1).
#
# design D14: при первом /start — seed 13 дефолтов (12 параметров ТЗ §5 +
# «целевая длительность дневного сна» = 90 мин, решение 26.10 №1) по
# фиксированному словарю ключей в коде. Повторный /start идемпотентен:
# существующие значения НЕ перезаписываются (пользовательские правки
# сохраняются). Значения хранятся в settings.value как TEXT (ТЗ §9.3);
# чтение возвращает приведённый к типу дефолта значение (FR-24: расчёты
# читают настройки при каждом использовании, без кэша — кэширование
# вне этого сервиса, на стороне вызывающих расчётов).
#
# Вызывается слоем handlers; доступ к хранилищу только через SettingsRepo
# (design D1, FR-1).

from __future__ import annotations

import re
import sqlite3

from src.repositories.settings import SettingsHistoryRepo, SettingsRepo

# 13 настроек: ключ -> значение по умолчанию. Тип значения по умолчанию
# определяет тип приведения при чтении (TEXT в БД). Порядок — ТЗ §5,
# последняя — новая настройка (решение 26.10 №1).
DEFAULT_KEYS: dict[str, int | float | str] = {
    "sleep_min_before_nap": 320,    # мин. бодрствования до дневного сна, мин (5 ч 20 мин)
    "sleep_max_before_nap": 360,    # макс. бодрствования до дневного сна, мин (6 ч 00 мин)
    "wake_deadline": "15:30",       # крайнее время пробуждения от дневного сна
    "night_target": "22:00",        # целевое время ухода в ночь
    "night_deadline": "22:30",      # крайнее время ухода в ночь
    "wake_total_min": 12.0,         # нижняя граница суммарного бодрствования, ч
    "wake_total_max": 12.5,         # верхняя граница суммарного бодрствования, ч
    "remind_before": 15,            # напоминание заранее, мин
    "day_start": "06:00",           # начало дня (страница дня 06:00–05:59, design D8)
    "merge_threshold": 20,          # порог склейки снов, мин (design D9)
    "night_bounds": "19:00-07:00",  # границы ночного сна, начало-конец (design D7)
    "backfill_depth_days": 3,       # максимальная глубина ввода задним числом, дней
    "target_nap_duration": 90,      # целевая длительность дневного сна, мин (решение №1)
}


def _to_text(value: int | float | str) -> str:
    """Сериализация дефолта в TEXT для settings.value (ТЗ §9.3)."""
    return str(value)


def _cast(raw: str, default: int | float | str) -> int | float | str:
    """Привести TEXT-значение из БД к типу дефолта этого ключа."""
    if isinstance(default, bool):  # pragma: no cover — bool-ключей в DEFAULT_KEYS нет
        return bool(int(raw))
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    return raw


# --------------------------------------------------------------- валидация
# Задача 1.1 add-settings-history (FR-44/45, design D21): формат ввода по
# типу ключа из DEFAULT_KEYS — единый источник ключей (D14), копий нет:
# классификация формата ссылается на имена ключей, дефолты — только здесь.

# Длительность в минутах: «5:20» (ч:мм), «5ч20м», «20м» или просто минуты
# («320») — ТЗ §7.17, design D21.
_DURATION_MINUTE_KEYS = frozenset({
    "sleep_min_before_nap",
    "sleep_max_before_nap",
    "remind_before",
    "merge_threshold",
    "target_nap_duration",
})
# Целое число без единиц (дни глубины ввода задним числом).
_PLAIN_INT_KEYS = frozenset({"backfill_depth_days"})
# Число часов (может быть дробным): нижняя/верхняя границы бодрствования.
_HOURS_KEYS = frozenset({"wake_total_min", "wake_total_max"})
# Время «ЧЧ:ММ» (хранится нормализованной строкой «ЧЧ:ММ»).
_TIME_KEYS = frozenset({"wake_deadline", "night_target", "night_deadline", "day_start"})
# Окно «ЧЧ:ММ-ЧЧ:ММ» (границы ночи; начало может быть больше конца —
# интервал через полночь, поэтому проверка min ≤ max НЕ применяется).
_WINDOW_KEYS = frozenset({"night_bounds"})

# Парные границы: ключ -> противоположная граница (проверка min ≤ max,
# в обе стороны, design D21 «физический смысл»). _PAIR_MIN_SIDE — какая
# из сторон пары является минимумом.
_PAIR_KEY = {
    "sleep_min_before_nap": "sleep_max_before_nap",
    "sleep_max_before_nap": "sleep_min_before_nap",
    "wake_total_min": "wake_total_max",
    "wake_total_max": "wake_total_min",
}
_PAIR_MIN_SIDE = frozenset({"sleep_min_before_nap", "wake_total_min"})

_RE_HHMM = re.compile(r"^\s*(\d{1,2})\s*:\s*(\d{1,2})\s*$")
_RE_HOURS = re.compile(r"^\s*\d+(?:\.\d+)?\s*$")
_RE_HM = re.compile(r"^\s*(\d+)\s*ч\s*(?:(\d{1,2})\s*м?)?\s*$")
_RE_M = re.compile(r"^\s*(\d{1,2})\s*м\s*$")
_RE_INT = re.compile(r"^\s*\d+\s*$")


class SettingsValueError(ValueError):
    """Введённое значение не прошло валидацию (§7.19; настройка не менялась)."""


def _parse_hhmm(text: str) -> str:
    """«ЧЧ:ММ» -> нормализованная строка «ЧЧ:ММ»; иначе SettingsValueError."""
    m = _RE_HHMM.match(text)
    if not m:
        raise SettingsValueError(f"ожидалось время ЧЧ:ММ, получено: {text!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        raise SettingsValueError(f"время вне диапазона 00:00–23:59: {text!r}")
    return f"{hour:02d}:{minute:02d}"


def _parse_duration_minutes(text: str) -> int:
    """Длительность «5:20»/«5ч20м»/«20м»/минуты -> минуты; иначе ошибка."""
    m = _RE_HHMM.match(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if minute > 59:
            raise SettingsValueError(f"минуты больше 59: {text!r}")
        return hour * 60 + minute
    m = _RE_HM.match(text)
    if m:
        return int(m.group(1)) * 60 + (int(m.group(2)) if m.group(2) else 0)
    m = _RE_M.match(text)
    if m:
        return int(m.group(1))
    if _RE_INT.match(text):
        return int(text)
    raise SettingsValueError(
        f"ожидалась длительность «5:20», «5ч20м» или минуты, получено: {text!r}"
    )


def _validate_value(key: str, text: str) -> int | float | str:
    """Разобрать и проверить ввод по типу ключа (FR-44/45, design D21).

    Возвращает значение в родном типе ключа; ошибку формата или
    физического смысла — SettingsValueError (настройка не менялась).
    """
    if key in _DURATION_MINUTE_KEYS:
        return _parse_duration_minutes(text)
    if key in _PLAIN_INT_KEYS:
        if not _RE_INT.match(text):
            raise SettingsValueError(f"ожидалось целое число, получено: {text!r}")
        return int(text)
    if key in _HOURS_KEYS:
        if not _RE_HOURS.match(text):
            raise SettingsValueError(f"ожидалось число часов, получено: {text!r}")
        return float(text)
    if key in _TIME_KEYS:
        return _parse_hhmm(text)
    if key in _WINDOW_KEYS:
        parts = text.split("-")
        if len(parts) != 2:
            raise SettingsValueError(f"ожидалось окно ЧЧ:ММ-ЧЧ:ММ, получено: {text!r}")
        return f"{_parse_hhmm(parts[0])}-{_parse_hhmm(parts[1])}"
    raise SettingsValueError(f"неизвестный ключ настройки: {key!r}")  # pragma: no cover


class SettingsService:
    """Чтение, первичный seed и изменение настроек (FR-5, FR-23, FR-24, FR-44)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._repo = SettingsRepo(conn)
        self._history = SettingsHistoryRepo(conn)

    def seed_defaults(self, user_id: int) -> None:
        """Создать недостающие дефолтные настройки пользователя.

        Идемпотентно (FR-5, design D14): существующие ключи не
        перезаписываются — повторный вызов (повторный /start) ничего
        не меняет и не сбрасывает правки пользователя.
        """
        for key, value in DEFAULT_KEYS.items():
            if self._repo.get(user_id, key) is None:
                self._repo.upsert(user_id, key, _to_text(value))

    def get(self, user_id: int, key: str) -> int | float | str:
        """Значение настройки в родном типе; при отсутствии — дефолт."""
        default = DEFAULT_KEYS[key]
        raw = self._repo.get(user_id, key)
        if raw is None:
            return default
        return _cast(raw, default)

    def get_all(self, user_id: int) -> dict[str, int | float | str]:
        """Все 13 настроек в родных типах (недостающие — дефолты)."""
        stored = self._repo.get_all(user_id)
        result: dict[str, int | float | str] = {}
        for key, default in DEFAULT_KEYS.items():
            raw = stored.get(key)
            result[key] = default if raw is None else _cast(raw, default)
        return result

    def update(self, user_id: int, key: str, value: str) -> tuple[int | float | str, int | float | str]:
        """Проверить и изменить настройку с записью в историю (FR-44–46).

        Валидация (design D21): формат по типу ключа из DEFAULT_KEYS —
        длительность «5:20»/«5ч20м»/минуты, время «ЧЧ:ММ», число
        (минуты/часы/дни); физический смысл — не-отрицательность и
        min ≤ max для парных границ (sleep_*_before_nap,
        wake_total_min/max; night_bounds — интервал через полночь,
        проверка min ≤ max к нему не применяется).

        Ошибка формата/смысла или неизвестный ключ -> SettingsValueError,
        хранилище не меняется. Успех -> обновление settings + INSERT в
        settings_history ОДНОЙ транзакцией (§8.3: «обновили без записи» и
        «записали без обновления» запрещены; при ошибке записи истории
        откатывается и обновление). Возвращает (old, new) в родных типах.

        Равное прежнему значение НЕ special-case'им: это легитимное
        успешное изменение с записью old == new (D21 не вводит no-op).
        """
        if key not in DEFAULT_KEYS:
            raise SettingsValueError(f"неизвестный ключ настройки: {key!r}")

        new = _validate_value(key, value)

        # Физический смысл (design D21): не-отрицательность обеспечена
        # самими парсерами — все числовые регекспы беззнаковые (минус
        # не разбирается); здесь проверяется только min ≤ max пары.
        other_key = _PAIR_KEY.get(key)
        if other_key is not None:
            # Парные ключи — числовые (минуты/часы). Нарушение — когда
            # min-сторона пары оказывается больше max-стороны (в какую
            # бы из двух мы ни писали).
            other = float(self.get(user_id, other_key))
            lo, hi = (float(new), other) if key in _PAIR_MIN_SIDE else (other, float(new))
            if lo > hi:
                raise SettingsValueError(
                    f"значение {new} нарушает границу {other_key}={other:g} (min ≤ max)"
                )

        old = self.get(user_id, key)
        try:
            self._repo.stage_upsert(user_id, key, _to_text(new))
            self._history.insert(user_id, key, _to_text(old), _to_text(new))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return old, new
