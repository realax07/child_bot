# Инициализация БД и схема v1.
# Change-пакет add-core-diary, задача 1.2 (SQLite, WAL, три таблицы).
#
# design D3: SQLite, journal_mode=WAL, foreign_keys=ON; таблицы users,
# sleep_events, settings строго по ТЗ §9.1–9.3, плюс users.role
# TEXT NOT NULL DEFAULT 'parent' (решение 26.10 №5). settings_history и
# daily_summary НЕ создаются (пакеты 2–3).
# design D4: время храним в UTC (DATETIME); TZ пользователя применяется
# через zoneinfo на границе ввода/вывода/разметки, а не в схеме.
# design D6: sleep_type и is_merged — nullable-резервы схемы (ТЗ §9.2);
# при записи события заполняются NULL, разметка/склейка вычисляются на лету.
#
# Путь БД берётся из аргумента (env-обёртка появится в задаче 5.3, design D15).

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_STATEMENTS = (
    # ТЗ §9.1 + users.role (FR-22, design D3)
    """
    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY,
        telegram_id INTEGER NOT NULL UNIQUE,
        timezone    TEXT,
        created_at  DATETIME,
        role        TEXT NOT NULL DEFAULT 'parent'
    )
    """,
    # ТЗ §9.2; sleep_type/is_merged — NULL при записи (design D6)
    """
    CREATE TABLE IF NOT EXISTS sleep_events (
        id            INTEGER PRIMARY KEY,
        user_id       INTEGER NOT NULL REFERENCES users(id),
        event_type    TEXT NOT NULL CHECK (event_type IN ('sleep', 'wake')),
        timestamp     DATETIME NOT NULL,
        created_at    DATETIME NOT NULL,
        is_backfilled BOOLEAN NOT NULL DEFAULT 0,
        sleep_type    TEXT,
        is_merged     BOOLEAN
    )
    """,
    # ТЗ §9.3
    """
    CREATE TABLE IF NOT EXISTS settings (
        id     INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        key    TEXT NOT NULL,
        value  TEXT,
        UNIQUE (user_id, key)
    )
    """,
    # Индекс для выборок событий пользователя по времени (задача 1.2)
    """
    CREATE INDEX IF NOT EXISTS idx_sleep_events_user_timestamp
        ON sleep_events (user_id, timestamp)
    """,
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Открыть соединение с SQLite и включить требуемые PRAGMA (design D3).

    journal_mode=WAL персистентен в файле БД; foreign_keys привязан к
    соединению и включается на каждом открытии.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Создать БД с нуля (или открыть существующую) со схемой v1.

    Возвращает соединение с включёнными PRAGMA (WAL, foreign_keys).
    """
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = connect(path)
    try:
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn
