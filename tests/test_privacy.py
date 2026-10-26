# Тесты приватности: локальное хранение данных (задача 5.3).
#
# Критерий готовности 5.3 / FR-25 / design D15 / решение 26.10 №6:
# - БД и бэкапы создаются только локально (в tmp-каталоге теста);
# - в src/ нет внешних HTTP-вызовов с пользовательскими данными
#   (0 импортов requests/httpx/urllib/aiohttp, 0 endpoint'ов-литералов);
# - логи в src/ не пишут содержимое сообщений (только user_id и тип события).
#
# Допущение (документировано): python-telegram-bot (библиотека `telegram`)
# сама общается с Telegram Bot API — это транспорт самого бота, а не вывод
# пользовательских данных во внешний сервис/аналитику/облако. В нашем коде
# (src/) сетевых импортов и внешних endpoint'ов нет — это и проверяется.

from __future__ import annotations

import ast
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from src.db import DEFAULT_DB_PATH, DB_PATH_ENV, db_path_from_env, init_db

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
BACKUP_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backup_db.sh"

# Сетевые библиотеки, запрещённые в src/ (FR-25: без внешних передач данных).
FORBIDDEN_NET_MODULES = {
    "requests",
    "httpx",
    "aiohttp",
    "urllib",
    "urllib.request",
    "urllib.parse",
    "urllib2",
    "urllib3",
    "httplib",
    "http.client",
    "ftplib",
    "smtplib",
    "socket",
}

LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
# Атрибуты update/message/callback_query, содержащие пользовательский ввод.
MESSAGE_CONTENT_ATTRS = {
    "text",
    "caption",
    "data",
    "message",
    "effective_message",
    "effective_user",
    "effective_chat",
    "username",
    "first_name",
    "last_name",
    "bio",
}
# Передача целого объекта в лог тоже раскрывает содержимое (repr).
WHOLE_OBJECT_NAMES = {"update", "message", "query", "msg", "callback_query"}


def _src_files() -> list[Path]:
    return sorted(SRC_DIR.rglob("*.py"))


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _iter_net_imports(tree: ast.AST) -> list[str]:
    """Все импортируемые модули, совпавшие с запрещённым списком."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if alias.name in FORBIDDEN_NET_MODULES or root in FORBIDDEN_NET_MODULES:
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if node.module in FORBIDDEN_NET_MODULES or root in FORBIDDEN_NET_MODULES:
                found.append(node.module)
    return found


def _logging_content_leaks(tree: ast.AST) -> list[str]:
    """Вызовы логирования, в аргументах которых есть содержимое сообщений."""
    leaks: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in LOG_METHODS):
            continue
        if not (isinstance(func.value, ast.Name) and "log" in func.value.id.lower()):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Attribute) and sub.attr in MESSAGE_CONTENT_ATTRS:
                    leaks.append(f"{func.value.id}.{func.attr}(...{sub.attr}...)")
                if isinstance(sub, ast.Name) and sub.id in WHOLE_OBJECT_NAMES:
                    leaks.append(f"{func.value.id}.{func.attr}(...{sub.id}...)")
    return leaks


# --- TC-PRV-001: БД создаётся локально по env BOT_DB_PATH -------------------


def test_tc_prv_001_db_created_locally_from_env(tmp_path, monkeypatch):
    """init_db() без аргумента читает env BOT_DB_PATH и создаёт локальный файл."""
    db_path = tmp_path / "data" / "bot.db"
    monkeypatch.setenv(DB_PATH_ENV, str(db_path))
    assert db_path_from_env() == db_path

    assert not db_path.exists()
    conn = init_db()
    try:
        assert db_path.exists(), "файл БД должен появиться по пути из env"
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        # Задача 5.3 ничего не меняет в схеме: те же три таблицы (design D3).
        assert {r[0] for r in rows} == {"users", "sleep_events", "settings"}
    finally:
        conn.close()


def test_tc_prv_001b_default_db_path_is_local_file(monkeypatch):
    """Дефолт при не заданном env — локальный файл рядом с ботом."""
    monkeypatch.delenv(DB_PATH_ENV, raising=False)
    assert db_path_from_env() == Path(DEFAULT_DB_PATH)
    # Согласованность с src/handlers/common.py::db_path (тот же env и дефолт).
    from src.handlers import common

    assert common.db_path.__doc__ is not None
    monkeypatch.delenv(DB_PATH_ENV, raising=False)
    assert common.db_path(type("Ctx", (), {"bot_data": None})()) == DEFAULT_DB_PATH


# --- TC-PRV-002: бэкап создаётся локально рядом с БД ------------------------


def test_tc_prv_002_backup_created_locally_next_to_db(tmp_path, monkeypatch):
    """scripts/backup_db.sh делает локальный бэкап (sqlite3 .backup) рядом с БД."""
    assert shutil.which("sqlite3"), "для локального бэкапа нужен sqlite3 CLI"

    db_path = tmp_path / "bot.db"
    monkeypatch.setenv(DB_PATH_ENV, str(db_path))
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO users (telegram_id, timezone, created_at) VALUES (?, ?, ?)",
        (4242, "Europe/Moscow", "2026-10-26 12:00:00"),
    )
    conn.commit()
    # Соединение остаётся открытым (WAL) — .backup обязан дать консистентный
    # снапшот, в отличие от копирования файла.
    try:
        result = subprocess.run(
            ["bash", str(BACKUP_SCRIPT)],
            capture_output=True,
            text=True,
            check=True,
            env={"BOT_DB_PATH": str(db_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
    finally:
        conn.close()

    backups = list(tmp_path.glob("bot.db.backup-*"))
    assert len(backups) == 1, f"ровно один бэкап рядом с БД: {backups}"
    backup = backups[0]
    assert backup.is_file() and backup.stat().st_size > 0
    assert "OK:" in result.stdout and str(backup) in result.stdout

    # Бэкап — валидная SQLite-БД с данными на момент снапшота.
    bk = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        tables = {
            r[0]
            for r in bk.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == {"users", "sleep_events", "settings"}
        assert bk.execute("SELECT telegram_id FROM users").fetchall() == [(4242,)]
    finally:
        bk.close()


def test_tc_prv_002b_backup_script_is_local_only():
    """Скрипт бэкапа не содержит внешних вызовов (FR-25: только наш сервер)."""
    content = BACKUP_SCRIPT.read_text(encoding="utf-8")
    forbidden = ("curl", "wget", "nc ", "ssh", "scp", "rsync", "ftp", "http://", "https://")
    for token in forbidden:
        assert token not in content, f"бэкап-скрипт не должен использовать {token!r}"


# --- TC-PRV-003: в src/ нет внешних HTTP-вызовов с пользовательскими данными --


def test_tc_prv_003_no_network_imports_in_src():
    """AST: в src/ ноль импортов сетевых/внешних библиотек."""
    offenders: list[str] = []
    for path in _src_files():
        offenders += [f"{path.name}: {mod}" for mod in _iter_net_imports(_tree(path))]
    assert offenders == [], (
        "в src/ запрещены сетевые импорты (FR-25, данные не покидают сервер): "
        f"{offenders}"
    )


def test_tc_prv_003b_no_external_endpoints_in_src():
    """В src/ нет литералов внешних endpoint'ов (http/https)."""
    offenders: list[str] = []
    for path in _src_files():
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "http://" in line or "https://" in line:
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == [], f"внешние endpoint'ы в src/: {offenders}"


# --- TC-PRV-004: логи не пишут содержимое сообщений --------------------------


def test_tc_prv_004_logs_do_not_contain_message_content():
    """AST: вызовов логирования с содержимым сообщений/пользователей нет.

    design D15: в логах — только user_id и тип события. На текущий момент
    src/ вообще не содержит логирования пользовательского ввода; тест
    фиксирует это состояние и упадёт при появлении утечки.
    """
    offenders: list[str] = []
    for path in _src_files():
        offenders += [f"{path.name}: {leak}" for leak in _logging_content_leaks(_tree(path))]
    assert offenders == [], (
        "логи не должны писать содержимое сообщений (design D15): "
        f"{offenders}"
    )


@pytest.mark.parametrize(
    "snippet, should_leak",
    [
        ("logger.info('sleep event recorded for user %s', user_id)", False),
        ("logger.debug('event_type=%s', event_type)", False),
        ("logger.info('текст: %s', update.message.text)", True),
        ("logging.warning('callback %s', query.data)", True),
        ("logger.error('сообщение целиком: %s', message)", True),
    ],
)
def test_tc_prv_004b_log_leak_detector(snippet, should_leak):
    """Самопроверка детектора утечек в логи (на мини-AST)."""
    tree = ast.parse(snippet)
    leaks = _logging_content_leaks(tree)
    assert bool(leaks) is should_leak, f"{snippet!r}: {leaks}"
