# Юнит-тесты CRUD-контрактов репозиториев (задача 1.3).
#
# Критерий готовности 1.3: green на CRUD-контракты UsersRepo /
# SleepEventsRepo / SettingsRepo; в sleep_events пишутся только сырые поля
# (event_type, timestamp, created_at, is_backfilled), sleep_type и is_merged
# остаются NULL (FR-3, design D6, review-001 F-1).
#
# Правило rule 6: каждый тест несёт TC-REPO-NNN в первой строке докстринга.

from __future__ import annotations

import sqlite3

import pytest

from src.db import init_db
from src.repositories.settings import SettingsRepo
from src.repositories.sleep_events import SleepEventsRepo
from src.repositories.users import UsersRepo


@pytest.fixture()
def db(tmp_path):
    """Свежая БД с нуля; соединение закрывается после теста."""
    conn = init_db(tmp_path / "bot.db")
    yield conn
    conn.close()


@pytest.fixture()
def users(db):
    return UsersRepo(db)


@pytest.fixture()
def events(db):
    return SleepEventsRepo(db)


@pytest.fixture()
def settings(db):
    return SettingsRepo(db)


# ---------------------------------------------------------------- UsersRepo


def test_users_get_or_create_new(db, users):
    """TC-REPO-001: get_or_create создаёт пользователя и возвращает id, role, timezone."""
    user = users.get_or_create(1001, timezone="Europe/Moscow")
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert user["telegram_id"] == 1001
    assert user["timezone"] == "Europe/Moscow"
    assert user["role"] == "parent"
    row = db.execute(
        "SELECT created_at FROM users WHERE telegram_id=1001"
    ).fetchone()
    assert row[0] is not None


def test_users_get_or_create_idempotent(db, users):
    """TC-REPO-002: повторный get_or_create возвращает того же пользователя, дублей нет."""
    first = users.get_or_create(1001)
    second = users.get_or_create(1001)
    assert first["id"] == second["id"]
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_users_role_default_parent(db, users):
    """TC-REPO-003: созданный без явной роли пользователь имеет role='parent'."""
    user = users.get_or_create(1002)
    assert user["role"] == "parent"
    row = db.execute("SELECT role FROM users WHERE id=?", (user["id"],)).fetchone()
    assert row[0] == "parent"


def test_users_timezone_read_and_update(db, users):
    """TC-REPO-004: get_timezone читает TZ, set_timezone обновляет (FR-4)."""
    users.get_or_create(1003)
    assert users.get_timezone(1003) is None
    assert users.set_timezone(1003, "Europe/Kaliningrad") is True
    assert users.get_timezone(1003) == "Europe/Kaliningrad"
    assert users.set_timezone(1003, "Europe/Moscow") is True
    assert users.get_timezone(1003) == "Europe/Moscow"


def test_users_timezone_unknown_user(db, users):
    """TC-REPO-005: для отсутствующего пользователя get_timezone -> None, set -> False."""
    assert users.get_timezone(9999) is None
    assert users.set_timezone(9999, "Europe/Moscow") is False


# ----------------------------------------------------------- SleepEventsRepo


def test_events_insert_writes_only_raw_fields(db, users, events):
    """TC-REPO-006: insert пишет только сырые поля; sleep_type/is_merged остаются NULL."""
    user = users.get_or_create(1001)
    event_id = events.insert(
        user["id"], "sleep", "2026-10-26 22:10:00", is_backfilled=False
    )
    row = db.execute(
        "SELECT user_id, event_type, timestamp, created_at, is_backfilled, "
        "sleep_type, is_merged FROM sleep_events WHERE id=?",
        (event_id,),
    ).fetchone()
    assert row[0] == user["id"]
    assert row[1] == "sleep"
    assert row[2] == "2026-10-26 22:10:00"
    assert row[3] is not None  # created_at проставлен
    assert row[4] == 0
    assert row[5] is None and row[6] is None  # design D6: резервы схемы


def test_events_insert_backfilled_flag(db, users, events):
    """TC-REPO-007: is_backfilled=True сохраняется как 1 (FR-16, design D10)."""
    user = users.get_or_create(1001)
    event_id = events.insert(
        user["id"], "wake", "2026-10-25 22:30:00", is_backfilled=True
    )
    row = db.execute(
        "SELECT is_backfilled FROM sleep_events WHERE id=?", (event_id,)
    ).fetchone()
    assert row[0] == 1


def test_events_insert_rejects_bad_event_type(db, users, events):
    """TC-REPO-008: событие с недопустимым event_type отклоняется CHECK-ом схемы."""
    user = users.get_or_create(1001)
    with pytest.raises(sqlite3.IntegrityError):
        events.insert(user["id"], "nap", "2026-10-26 13:00:00")


def test_events_list_by_user_chronological(db, users, events):
    """TC-REPO-009: list_by_user возвращает события пользователя по timestamp, затем id."""
    user = users.get_or_create(1001)
    a = events.insert(user["id"], "sleep", "2026-10-26 22:10:00")
    b = events.insert(user["id"], "wake", "2026-10-27 07:20:00")
    c = events.insert(user["id"], "sleep", "2026-10-26 13:30:00")  # задним числом
    rows = events.list_by_user(user["id"])
    assert [r["id"] for r in rows] == [c, a, b]
    assert all(r["user_id"] == user["id"] for r in rows)


def test_events_list_by_user_isolated(db, users, events):
    """TC-REPO-010: list_by_user не показывает события других пользователей."""
    u1 = users.get_or_create(1001)
    u2 = users.get_or_create(1002)
    events.insert(u1["id"], "sleep", "2026-10-26 22:10:00")
    events.insert(u2["id"], "wake", "2026-10-27 07:20:00")
    ids_u1 = {r["id"] for r in events.list_by_user(u1["id"])}
    ids_u2 = {r["id"] for r in events.list_by_user(u2["id"])}
    assert ids_u1 and ids_u2 and not (ids_u1 & ids_u2)


def test_events_last_by_created_at(db, users, events):
    """TC-REPO-011: last — запись с максимальным created_at/id, даже если timestamp ранний (design D12)."""
    user = users.get_or_create(1001)
    events.insert(user["id"], "sleep", "2026-10-26 22:10:00", created_at="2026-10-26 22:10:05")
    events.insert(
        user["id"], "wake", "2026-10-25 20:00:00", created_at="2026-10-26 23:00:00"
    )
    last = events.last(user["id"])
    assert last["event_type"] == "wake"
    assert last["timestamp"] == "2026-10-25 20:00:00"  # задним числом, timestamp ранний


def test_events_last_empty(db, users, events):
    """TC-REPO-012: last на пустой БД возвращает None, не падает."""
    users.get_or_create(1001)
    assert events.last(1001) is None


def test_events_delete_last(db, users, events):
    """TC-REPO-013: delete_last удаляет последнюю по created_at/id запись (FR-21, design D12)."""
    user = users.get_or_create(1001)
    keep_id = events.insert(user["id"], "sleep", "2026-10-26 22:10:00", created_at="2026-10-26 22:10:05")
    drop_id = events.insert(user["id"], "wake", "2026-10-26 06:40:00", created_at="2026-10-26 22:15:00")
    assert events.delete_last(user["id"]) is True
    remaining = [r["id"] for r in events.list_by_user(user["id"])]
    assert remaining == [keep_id]
    assert db.execute("SELECT COUNT(*) FROM sleep_events WHERE id=?", (drop_id,)).fetchone()[0] == 0


def test_events_delete_last_empty(db, users, events):
    """TC-REPO-014: delete_last при отсутствии событий -> False, данные не меняются (design D12)."""
    user = users.get_or_create(1001)
    assert events.delete_last(user["id"]) is False
    assert events.list_by_user(user["id"]) == []


def test_events_overwrite_target_only(db, users, events):
    """TC-REPO-015: overwrite атомарно обновляет ровно целевую запись, остальные не тронуты (design D11)."""
    user = users.get_or_create(1001)
    target = events.insert(user["id"], "wake", "2026-10-26 13:30:00", created_at="2026-10-26 13:30:05")
    other = events.insert(user["id"], "sleep", "2026-10-26 22:10:00", created_at="2026-10-26 22:10:05")
    assert events.overwrite(target, event_type="sleep", timestamp="2026-10-26 13:25:00") is True
    updated = db.execute(
        "SELECT event_type, timestamp, sleep_type, is_merged FROM sleep_events WHERE id=?",
        (target,),
    ).fetchone()
    assert updated[0] == "sleep" and updated[1] == "2026-10-26 13:25:00"
    assert updated[2] is None and updated[3] is None  # design D6 не нарушен
    untouched = db.execute(
        "SELECT event_type, timestamp FROM sleep_events WHERE id=?", (other,)
    ).fetchone()
    assert untouched == ("sleep", "2026-10-26 22:10:00")
    assert db.execute("SELECT COUNT(*) FROM sleep_events").fetchone()[0] == 2  # без дублей


def test_events_overwrite_missing_id(db, users, events):
    """TC-REPO-016: overwrite несуществующей записи -> False, ничего не пишет."""
    users.get_or_create(1001)
    assert events.overwrite(424242, event_type="sleep") is False
    assert events.list_by_user(1001) == []


def test_events_overwrite_partial_fields(db, users, events):
    """TC-REPO-017: overwrite меняет только переданные поля; без полей -> False."""
    user = users.get_or_create(1001)
    event_id = events.insert(user["id"], "sleep", "2026-10-26 22:10:00")
    assert events.overwrite(event_id, timestamp="2026-10-26 22:15:00") is True
    row = db.execute(
        "SELECT event_type, timestamp FROM sleep_events WHERE id=?", (event_id,)
    ).fetchone()
    assert row == ("sleep", "2026-10-26 22:15:00")
    assert events.overwrite(event_id) is False


# -------------------------------------------------------------- SettingsRepo


def test_settings_get_all(db, users, settings):
    """TC-REPO-018: get_all возвращает dict key->value по всем настройкам пользователя."""
    user = users.get_or_create(1001)
    settings.upsert(user["id"], "merge_threshold", "20")
    settings.upsert(user["id"], "day_start", "06:00")
    result = settings.get_all(user["id"])
    assert result == {"merge_threshold": "20", "day_start": "06:00"}


def test_settings_get_all_empty_and_isolated(db, users, settings):
    """TC-REPO-019: get_all без настроек -> {}; настройки других пользователей не видны."""
    u1 = users.get_or_create(1001)
    u2 = users.get_or_create(1002)
    assert settings.get_all(u1["id"]) == {}
    settings.upsert(u2["id"], "backfill_depth_days", "3")
    assert settings.get_all(u1["id"]) == {}
    assert settings.get_all(u2["id"]) == {"backfill_depth_days": "3"}


def test_settings_get(db, users, settings):
    """TC-REPO-020: get возвращает значение ключа или None при отсутствии."""
    user = users.get_or_create(1001)
    settings.upsert(user["id"], "merge_threshold", "20")
    assert settings.get(user["id"], "merge_threshold") == "20"
    assert settings.get(user["id"], "night_start") is None


def test_settings_upsert_insert_then_update(db, users, settings):
    """TC-REPO-021: upsert создаёт ключ, затем обновляет без дублей (UNIQUE user_id+key)."""
    user = users.get_or_create(1001)
    settings.upsert(user["id"], "merge_threshold", "20")
    settings.upsert(user["id"], "merge_threshold", "30")
    assert settings.get(user["id"], "merge_threshold") == "30"
    count = db.execute(
        "SELECT COUNT(*) FROM settings WHERE user_id=? AND key='merge_threshold'",
        (user["id"],),
    ).fetchone()[0]
    assert count == 1


def test_settings_upsert_allows_same_key_for_different_users(db, users, settings):
    """TC-REPO-022: одинаковый ключ у разных пользователей хранится раздельно."""
    u1 = users.get_or_create(1001)
    u2 = users.get_or_create(1002)
    settings.upsert(u1["id"], "day_start", "06:00")
    settings.upsert(u2["id"], "day_start", "08:00")
    assert settings.get(u1["id"], "day_start") == "06:00"
    assert settings.get(u2["id"], "day_start") == "08:00"
