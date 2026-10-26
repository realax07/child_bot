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

import sqlite3

from src.repositories.settings import SettingsRepo

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


class SettingsService:
    """Чтение и первичный seed настроек пользователя (FR-5, FR-23, FR-24)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._repo = SettingsRepo(conn)

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
