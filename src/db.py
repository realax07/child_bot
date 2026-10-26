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
# Путь БД берётся из env BOT_DB_PATH (задача 5.3, design D15, FR-25):
# дефолт — локальный файл рядом с ботом. Файл БД и бэкапы живут только на
# нашем сервере (решение 26.10 №6): никакой внешний тултип/облако не
# участвует. Схему v1 задача 5.3 не меняет.
#
# Согласованность с src/handlers/common.py::db_path — тот же env BOT_DB_PATH
# и тот же дефолт (common.py не в зоне задачи 5.3; проверка эквивалентности
# дефолтов — tests/test_privacy.py).

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Дефолтный путь БД — локальный файл в рабочем каталоге бота.
# Должен совпадать с дефолтом в src/handlers/common.py::db_path.
DEFAULT_DB_PATH = "child_bot.db"

# Переменная окружения с путём к БД (design D15).
DB_PATH_ENV = "BOT_DB_PATH"


def db_path_from_env(environ: dict | None = None) -> Path:
    """Локальный путь к БД: env BOT_DB_PATH, иначе DEFAULT_DB_PATH.

    FR-25/design D15: БД — только локальный файл на нашем сервере.
    """
    env = os.environ if environ is None else environ
    return Path(env.get(DB_PATH_ENV, DEFAULT_DB_PATH))

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


def init_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Создать БД с нуля (или открыть существующую) со схемой v1.

    db_path не задан (или None) — путь берётся из env BOT_DB_PATH
    (дефолт DEFAULT_DB_PATH, design D15/FR-25). Возвращает соединение
    с включёнными PRAGMA (WAL, foreign_keys).
    """
    path = Path(db_path) if db_path is not None else db_path_from_env()
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
