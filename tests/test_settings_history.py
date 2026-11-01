"""SettingsService.update + settings_history — задача 1.1 пакета
add-settings-history.

Трассировка: FR-44 (валидация формата), FR-45 (физический смысл),
FR-46 (запись settings_history одной операцией); design D21;
specs/settings-ui «Валидация значений и успех», «Логирование изменений»;
ТЗ §5.1, §8.3, §9.4; research DEFAULT_KEYS, §9.4.

Критерии готовности задачи 1.1 (tasks.md):
  - wake_deadline 15:30 → 15:40 даёт строку истории old=15:30/new=15:40;
  - «полтора часа» → отказ без записи;
  - min > max → отказ;
  - settings_history создаётся init_db по ТЗ §9.4.

Время фиксированное: changed_at подменой _utcnow_iso (design D4 — UTC);
в ожиданиях нет «живых» ЧЧ:ММ. Правило rule 6: каждый тест несёт
TC-SETT-NNN в первой строке докстринга.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db import init_db
from src.repositories import settings as settings_repo_module
from src.repositories.settings import SettingsHistoryRepo
from src.services.settings import DEFAULT_KEYS, SettingsService, SettingsValueError

FIXED_NOW = "2026-11-01 12:00:00"  # фиксированный UTC-момент (design D4)


@pytest.fixture()
def db(tmp_path):
    """Свежая БД с нуля; соединение закрывается после теста."""
    conn = init_db(tmp_path / "bot.db")
    yield conn
    conn.close()


@pytest.fixture()
def user_id(db):
    """Пользователь с засеянными 13 дефолтами настроек."""
    cur = db.execute(
        "INSERT INTO users (telegram_id, created_at) VALUES (?, ?)",
        (7001, FIXED_NOW),
    )
    db.commit()
    uid = cur.lastrowid
    SettingsService(db).seed_defaults(uid)
    return uid


@pytest.fixture()
def fixed_now(monkeypatch):
    """changed_at == FIXED_NOW для всех записей истории в тесте."""
    monkeypatch.setattr(settings_repo_module, "_utcnow_iso", lambda: FIXED_NOW)


def _history_count(db: sqlite3.Connection, user_id: int) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM settings_history WHERE user_id = ?", (user_id,)
    ).fetchone()[0]


# ------------------------------------------------- ТЗ §9.4: схема таблицы


def test_tc_sett_004_init_db_creates_settings_history(db):
    """TC-SETT-004: settings_history создаётся init_db по ТЗ §9.4.

    Трассировка: Requirement «Логирование изменений в settings_history»;
    research §9.4 (id, user_id FK, key, old_value, new_value, changed_at).
    """
    cols = {
        name: (ctype, notnull)
        for _, name, ctype, notnull, _, _ in db.execute(
            "PRAGMA table_info(settings_history)"
        ).fetchall()
    }
    assert cols == {
        "id": ("INTEGER", 0),
        "user_id": ("INTEGER", 1),
        "key": ("TEXT", 1),
        "old_value": ("TEXT", 0),
        "new_value": ("TEXT", 0),
        "changed_at": ("DATETIME", 1),
    }
    # FK на users работает (PRAGMA foreign_keys=ON, design D3).
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO settings_history (user_id, key, old_value, "
            "new_value, changed_at) VALUES (?, ?, ?, ?, ?)",
            (9999, "wake_deadline", "15:30", "15:40", FIXED_NOW),
        )


def test_tc_sett_005_init_db_idempotent_keeps_history(tmp_path):
    """TC-SETT-005: повторный init_db идемпотентен — схема и данные целы.

    Трассировка: tasks.md 1.1 «идемпотентно, данных не лифтуем».
    """
    conn = init_db(tmp_path / "bot.db")
    cur = conn.execute(
        "INSERT INTO users (telegram_id, created_at) VALUES (?, ?)",
        (7002, FIXED_NOW),
    )
    conn.execute(
        "INSERT INTO settings_history (user_id, key, old_value, new_value, "
        "changed_at) VALUES (?, ?, ?, ?, ?)",
        (cur.lastrowid, "wake_deadline", "15:30", "15:40", FIXED_NOW),
    )
    conn.commit()
    conn.close()

    conn2 = init_db(tmp_path / "bot.db")  # повторный запуск — без ошибок
    try:
        rows = conn2.execute("SELECT key, old_value, new_value FROM settings_history").fetchall()
        assert rows == [("wake_deadline", "15:30", "15:40")]
    finally:
        conn2.close()


# --------------------------------------------------- FR-46: запись истории


def test_tc_sett_001_wake_deadline_update_writes_history(db, user_id, fixed_now):
    """TC-SETT-001: wake_deadline 15:30 → 15:40 — строка old=15:30/new=15:40.

    Трассировка: Scenario «Запись при смены» (specs/settings-ui); FR-46.
    """
    svc = SettingsService(db)
    old, new = svc.update(user_id, "wake_deadline", "15:40")

    assert (old, new) == ("15:30", "15:40")
    # settings обновлён, последующий расчёт читает новое (FR-45).
    assert svc.get(user_id, "wake_deadline") == "15:40"
    rows = SettingsHistoryRepo(db).list_for_user(user_id)
    assert len(rows) == 1
    row = rows[0]
    assert (row["key"], row["old_value"], row["new_value"]) == (
        "wake_deadline", "15:30", "15:40",
    )
    assert row["changed_at"] == FIXED_NOW


def test_tc_sett_002_duration_formats_accepted(db, user_id):
    """TC-SETT-002: форматы длительности «5:20»/«5ч20м»/минуты → минуты.

    Трассировка: FR-44; design D21 (длительность три формы).
    """
    svc = SettingsService(db)
    # дефолт sleep_min_before_nap = 320 («5:20»)
    assert svc.update(user_id, "sleep_min_before_nap", "5ч30м")[1] == 330
    assert svc.get(user_id, "sleep_min_before_nap") == 330
    assert svc.update(user_id, "sleep_min_before_nap", "5:10")[1] == 310
    assert svc.update(user_id, "sleep_min_before_nap", "320")[1] == 320
    assert svc.update(user_id, "remind_before", "20м")[1] == 20
    # в settings — TEXT-сериализация (ТЗ §9.3)
    raw = db.execute(
        "SELECT value FROM settings WHERE user_id=? AND key='sleep_min_before_nap'",
        (user_id,),
    ).fetchone()[0]
    assert raw == "320"


def test_tc_sett_006_history_newest_first_and_isolated(db, user_id, fixed_now):
    """TC-SETT-006: история — новые первыми; чужой user_id не видит.

    Трассировка: ТЗ §5.1; Scenario «Изоляция пользователей» (settings-ui).
    """
    svc = SettingsService(db)
    svc.update(user_id, "wake_deadline", "15:40")
    svc.update(user_id, "night_deadline", "22:40")

    # другой пользователь — своя (пустая) история
    cur = db.execute(
        "INSERT INTO users (telegram_id, created_at) VALUES (?, ?)",
        (7003, FIXED_NOW),
    )
    other_id = cur.lastrowid
    db.commit()

    repo = SettingsHistoryRepo(db)
    rows = repo.list_for_user(user_id)
    assert [r["key"] for r in rows] == ["night_deadline", "wake_deadline"]
    assert repo.list_for_user(other_id) == []


# ------------------------------------------------- FR-44/45: отказы без записи


def test_tc_sett_003_unparseable_input_rejected_without_writes(db, user_id):
    """TC-SETT-003: «полтора часа» → отказ, настройки и история не тронуты.

    Трассировка: Scenario «Неразбираемый ввод» (settings-ui); FR-44/46.
    """
    svc = SettingsService(db)
    with pytest.raises(SettingsValueError):
        svc.update(user_id, "sleep_min_before_nap", "полтора часа")

    assert svc.get(user_id, "sleep_min_before_nap") == 320  # дефолт цел
    assert _history_count(db, user_id) == 0  # ошибка не логируется
    # текст в settings тоже не изменился
    raw = db.execute(
        "SELECT value FROM settings WHERE user_id=? AND key='sleep_min_before_nap'",
        (user_id,),
    ).fetchone()[0]
    assert raw == "320"


def test_tc_sett_007_min_greater_than_max_rejected(db, user_id):
    """TC-SETT-007: min > max → отказ без записи (пара before_nap).

    Трассировка: FR-45 (min ≤ max); design D21 «физический смысл».
    """
    svc = SettingsService(db)
    # sleep_max_before_nap = 360; 420 («7:00») > 360 — отказ в обе стороны
    with pytest.raises(SettingsValueError):
        svc.update(user_id, "sleep_min_before_nap", "7:00")
    with pytest.raises(SettingsValueError):
        svc.update(user_id, "sleep_min_before_nap", "6:30")  # 390 > 360
    assert _history_count(db, user_id) == 0
    # поднять max можно, после этого min 390 валиден
    svc.update(user_id, "sleep_max_before_nap", "400")
    old, new = svc.update(user_id, "sleep_min_before_nap", "6:30")
    assert (old, new) == (320, 390)


def test_tc_sett_008_wake_total_pair_rejected(db, user_id):
    """TC-SETT-008: пара wake_total_min/max — min > max → отказ.

    Трассировка: FR-45 (min ≤ max, часы).
    """
    svc = SettingsService(db)
    with pytest.raises(SettingsValueError):
        svc.update(user_id, "wake_total_min", "13")  # 13 ч > 12.5 ч max
    assert svc.get(user_id, "wake_total_min") == 12.0
    assert _history_count(db, user_id) == 0
    # дробное число часов принимается
    assert svc.update(user_id, "wake_total_max", "13.5")[1] == 13.5
    assert svc.update(user_id, "wake_total_min", "13.0")[1] == 13.0


def test_tc_sett_009_negative_input_rejected(db, user_id):
    """TC-SETT-009: отрицательные числа не разбираются — отказ без записи.

    Трассировка: FR-45 (не-отрицательность); design D21.
    """
    svc = SettingsService(db)
    for key, text in (
        ("sleep_min_before_nap", "-10"),
        ("wake_total_min", "-1.5"),
        ("remind_before", "-5"),
        ("backfill_depth_days", "-1"),
    ):
        with pytest.raises(SettingsValueError):
            svc.update(user_id, key, text)
    assert _history_count(db, user_id) == 0


def test_tc_sett_010_bad_time_rejected(db, user_id):
    """TC-SETT-010: некорректное время (25:00 / мусор) → отказ без записи.

    Трассировка: FR-44 (корректное «ЧЧ:ММ»); design D21.
    """
    svc = SettingsService(db)
    for text in ("25:00", "15:70", "полдень", "15"):
        with pytest.raises(SettingsValueError):
            svc.update(user_id, "wake_deadline", text)
    assert svc.get(user_id, "wake_deadline") == "15:30"
    assert _history_count(db, user_id) == 0
    # «9:30» нормализуется к «09:30» (формат хранения дефолтов)
    assert svc.update(user_id, "wake_deadline", "9:30")[1] == "09:30"


def test_tc_sett_011_unknown_key_rejected(db, user_id):
    """TC-SETT-011: ключ вне DEFAULT_KEYS → SettingsValueError, записи нет.

    Трассировка: design D14 (единый источник ключей); FR-44.
    """
    svc = SettingsService(db)
    with pytest.raises(SettingsValueError):
        svc.update(user_id, "no_such_key", "1")
    assert _history_count(db, user_id) == 0


def test_tc_sett_012_window_format_and_night_bounds(db, user_id):
    """TC-SETT-012: night_bounds «ЧЧ:ММ-ЧЧ:ММ»; 19:00-07:00 валидно.

    Трассировка: FR-44; research DEFAULT_KEYS (окно-строка; интервал
    через полночь — min ≤ max не применяется, design D21).
    """
    svc = SettingsService(db)
    old, new = svc.update(user_id, "night_bounds", "20:00-07:30")
    assert (old, new) == ("19:00-07:00", "20:00-07:30")
    # некорректная часть окна → отказ
    with pytest.raises(SettingsValueError):
        svc.update(user_id, "night_bounds", "20:00-7x:30")
    assert svc.get(user_id, "night_bounds") == "20:00-07:30"


def test_tc_sett_013_error_after_success_keeps_single_row(db, user_id, fixed_now):
    """TC-SETT-013: успешная правка пишет ровно одну строку; следом
    отвергнутая попытка строку НЕ добавляет.

    Трассировка: FR-46 «Неудачные попытки и отмены MUST НЕ записываться».
    """
    svc = SettingsService(db)
    svc.update(user_id, "night_target", "21:50")
    with pytest.raises(SettingsValueError):
        svc.update(user_id, "night_target", "когда-нибудь")
    rows = SettingsHistoryRepo(db).list_for_user(user_id)
    assert len(rows) == 1
    assert (rows[0]["old_value"], rows[0]["new_value"]) == ("22:00", "21:50")


def test_tc_sett_014_all_default_keys_have_format(db, user_id):
    """TC-SETT-014: каждый из 13 ключей DEFAULT_KEYS валиден и обновляем.

    Трассировка: design D21/D14 — валидация по типам всех ключей
    DEFAULT_KEYS; защита от «забытого» ключа без формата.
    """
    svc = SettingsService(db)
    new_values = {
        "sleep_min_before_nap": "5:30",
        "sleep_max_before_nap": "6:10",
        "wake_deadline": "16:00",
        "night_target": "22:10",
        "night_deadline": "22:40",
        "wake_total_min": "12.5",
        "wake_total_max": "13.0",
        "remind_before": "10",
        "day_start": "06:30",
        "merge_threshold": "25",
        "night_bounds": "19:30-07:00",
        "backfill_depth_days": "5",
        "target_nap_duration": "1:30",
    }
    assert set(new_values) == set(DEFAULT_KEYS)  # 13/13 без дублей
    for key, text in new_values.items():
        old, new = svc.update(user_id, key, text)
        assert old == DEFAULT_KEYS[key]
        assert svc.get(user_id, key) == new
    assert _history_count(db, user_id) == len(DEFAULT_KEYS)


def test_tc_sett_031_hm_form_minutes_capped_and_m_suffix_required(db, user_id):
    """TC-SETT-031: грамматика «ЧчММ» едина (4.2, minor-1 review-001-1.1):
    «5ч»→360; «5ч20м»→320; «5ч75м» — минуты > 59 → отказ (как «5:75»);
    «5ч20» без суффикса «м» — двусмысленно → отказ; настройка и история
    не пишутся при отказе.
    """
    svc = SettingsService(db)
    assert svc.update(user_id, "sleep_min_before_nap", "5ч")[1] == 300
    assert svc.update(user_id, "sleep_min_before_nap", "5ч20м")[1] == 320
    for bad in ("5ч75м", "5ч20"):
        with pytest.raises(SettingsValueError):
            svc.update(user_id, "sleep_min_before_nap", bad)
    assert svc.get(user_id, "sleep_min_before_nap") == 320  # отказ не писал
    assert _history_count(db, user_id) == 2  # только два успешных


def test_tc_sett_032_minutes_form_three_digits_accepted(db, user_id):
    """TC-SETT-032: «Nм» — до 3 цифр (4.2, санкция ПМ): «120м»→120
    (= 2 ч; прежний кап 2 цифр отбрасывал легитимное значение);
    «999м»→999; «1000м» (4 цифры) → отказ без записи.
    """
    svc = SettingsService(db)
    assert svc.update(user_id, "target_nap_duration", "120м")[1] == 120
    assert svc.update(user_id, "merge_threshold", "999м")[1] == 999
    with pytest.raises(SettingsValueError):
        svc.update(user_id, "remind_before", "1000м")
    assert svc.get(user_id, "remind_before") == DEFAULT_KEYS["remind_before"]
    assert _history_count(db, user_id) == 2  # только два успешных
