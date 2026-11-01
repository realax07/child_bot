# Юнит-тесты схемы БД v1 (задача 1.2).
#
# Критерий готовности задачи 1.2: БД создаётся с нуля, есть три таблицы,
# PRAGMA journal_mode = wal, дефолт role='parent', nullable-колонки
# sleep_events.sleep_type / is_merged. Плюс колонки/типы по ТЗ §9.1–9.3
# и индекс sleep_events(user_id, timestamp).

from __future__ import annotations

import sqlite3

import pytest

from src.db import init_db

EXPECTED_TABLES = {"users", "sleep_events", "settings", "settings_history"}

# Ожидаемые колонки: имя -> (declared type в DDL, notnull по PRAGMA)
EXPECTED_COLUMNS = {
    "users": {
        "id": ("INTEGER", 0),
        "telegram_id": ("INTEGER", 1),
        "timezone": ("TEXT", 0),
        "created_at": ("DATETIME", 0),
        "role": ("TEXT", 1),
    },
    "sleep_events": {
        "id": ("INTEGER", 0),
        "user_id": ("INTEGER", 1),
        "event_type": ("TEXT", 1),
        "timestamp": ("DATETIME", 1),
        "created_at": ("DATETIME", 1),
        "is_backfilled": ("BOOLEAN", 1),
        "sleep_type": ("TEXT", 0),  # nullable-резерв (design D6)
        "is_merged": ("BOOLEAN", 0),  # nullable-резерв (design D6)
    },
    "settings": {
        "id": ("INTEGER", 0),
        "user_id": ("INTEGER", 1),
        "key": ("TEXT", 1),
        "value": ("TEXT", 0),
    },
}


@pytest.fixture()
def db(tmp_path):
    """Свежая БД с нуля; соединение закрывается после теста."""
    conn = init_db(tmp_path / "bot.db")
    yield conn
    conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> dict:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {name: (ctype, notnull) for _, name, ctype, notnull, _, _ in rows}


def test_db_created_from_scratch(tmp_path):
    """TC-DB-001: init_db создаёт файл БД с нуля и возвращает рабочее соединение."""
    db_path = tmp_path / "bot.db"
    assert not db_path.exists()
    conn = init_db(db_path)
    try:
        assert db_path.exists()
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_three_tables_exist(db):
    """TC-DB-002: схема содержит ровно четыре таблицы (design D3 + settings_history
    задачи 1.1 пакета add-settings-history по ТЗ §9.4)."""
    rows = db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    assert {r[0] for r in rows} == EXPECTED_TABLES


def test_journal_mode_wal(db):
    """TC-DB-003: PRAGMA journal_mode = wal (FR-2, design D3)."""
    mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_foreign_keys_on(db):
    """TC-DB-004: PRAGMA foreign_keys = ON (design D3)."""
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_columns_and_types_per_spec(db):
    """TC-DB-005: колонки и типы строго по ТЗ §9.1–9.3 (+ role, design D3)."""
    for table, expected in EXPECTED_COLUMNS.items():
        actual = _columns(db, table)
        assert set(actual) == set(expected), f"колонки {table}"
        for column, (ctype, notnull) in expected.items():
            got_type, got_notnull = actual[column]
            assert got_type.upper() == ctype.upper(), f"{table}.{column} type"
            assert got_notnull == notnull, f"{table}.{column} notnull"


def test_role_default_parent(db):
    """TC-DB-006: users.role NOT NULL DEFAULT 'parent' (FR-22, решение 26.10 №5)."""
    info = _columns(db, "users")
    assert info["role"] == ("TEXT", 1)
    ddl = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()[0]
    assert "DEFAULT 'parent'" in ddl

    db.execute(
        "INSERT INTO users (telegram_id, timezone, created_at) VALUES (?, ?, ?)",
        (1001, "Europe/Moscow", "2026-10-26 12:00:00"),
    )
    role = db.execute("SELECT role FROM users WHERE telegram_id=1001").fetchone()[0]
    assert role == "parent"


def test_role_not_null_enforced(db):
    """TC-DB-007: явная попытка вставить NULL в role отклоняется."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO users (telegram_id, timezone, created_at, role) "
            "VALUES (?, ?, ?, NULL)",
            (1002, None, "2026-10-26 12:00:00"),
        )


def test_sleep_type_and_is_merged_nullable(db):
    """TC-DB-008: sleep_type/is_merged пишутся как NULL — резерв схемы (design D6)."""
    cur = db.execute(
        "INSERT INTO users (telegram_id, created_at) VALUES (?, ?)",
        (1003, "2026-10-26 12:00:00"),
    )
    user_id = cur.lastrowid
    cur = db.execute(
        "INSERT INTO sleep_events (user_id, event_type, timestamp, created_at, "
        "is_backfilled) VALUES (?, ?, ?, ?, ?)",
        (user_id, "sleep", "2026-10-26 13:30:00", "2026-10-26 13:30:05", 0),
    )
    row = db.execute(
        "SELECT sleep_type, is_merged FROM sleep_events WHERE id=?",
        (cur.lastrowid,),
    ).fetchone()
    assert row == (None, None)


def test_index_on_user_timestamp_exists(db):
    """TC-DB-009: индекс sleep_events(user_id, timestamp) создан."""
    rows = db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='sleep_events'"
    ).fetchall()
    indexes = {name: sql for name, sql in rows}
    target = [sql for sql in indexes.values() if sql]
    assert any(
        "user_id" in sql and "timestamp" in sql for sql in target
    ), f"индекс (user_id, timestamp) не найден: {sorted(indexes)}"


def test_foreign_key_enforced(db):
    """TC-DB-010: вставка события для несуществующего пользователя отклоняется."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO sleep_events (user_id, event_type, timestamp, "
            "created_at, is_backfilled) VALUES (?, ?, ?, ?, ?)",
            (9999, "sleep", "2026-10-26 13:30:00", "2026-10-26 13:30:05", 0),
        )


def test_wal_persists_across_connections(tmp_path):
    """TC-DB-011: WAL — свойство файла БД, виден и в новом соединении."""
    db_path = tmp_path / "wal.db"
    conn = init_db(db_path)
    conn.close()
    conn2 = sqlite3.connect(str(db_path))
    try:
        assert conn2.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn2.close()
