"""Юнит-тесты команды /help (change-пакет add-settings-history, задача 3.2).

ТЗ §7.28 (текст дословно), §10.2 (/help — команда); design D26: /help
доступна в любом состоянии, не пишет в БД, не меняет состояние и
настройки, не гасит открытые FSM-диалоги (справка поверх); FR-53.

Эталон текста — ФАЙЛ ТЗ (паттерн TC-HINT-009 / TC-SMOKE-002): сверка
не может «поехать» вместе с src/texts.py.

Правило rule 6: каждый тест несёт TC-HELPDEL-0xx в первой строке
докстринга.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src import texts
from src.db import init_db
from src.handlers import help_command
from src.handlers.events import (
    WAIT_PAIR_SLEEP_KEY,
    WAIT_PAIR_WAKE_KEY,
)
from src.handlers.settings_screen import WAIT_SETTING_VALUE
from src.handlers.start import start_command
from src.repositories.settings import SettingsRepo
from src.repositories.sleep_events import SleepEventsRepo
from src.repositories.users import UsersRepo
from src.services.settings import SettingsService

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC_FILE = _REPO_ROOT / "ТЗ_Дневник_сна_ребёнка.md"


# ------------------------------------------------------------ Эталон ТЗ


def _spec_section(section: str) -> str:
    """Дословный текст раздела ТЗ §7.x (blockquote после заголовка, AC-6)."""
    lines = _SPEC_FILE.read_text(encoding="utf-8").splitlines()
    start_idx = next(
        i for i, line in enumerate(lines) if line.startswith(f"### {section}")
    )
    body: list[str] = []
    for line in lines[start_idx + 1:]:
        if not line.startswith(">"):
            if body:
                break  # blockquote раздела закончился
            continue  # пустые строки до цитаты
        body.append(line[1:].lstrip(" "))
    return "\n".join(body).strip("\n")


# ------------------------------------------------------------ Заглушки PTB


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self) -> None:
        self.effective_user = None
        self.effective_message = _FakeMessage()


class _FakeContext:
    def __init__(self, db_path, user_data=None) -> None:
        self.bot_data = {"db_path": db_path}
        self.user_data = user_data if user_data is not None else {}


class _StartUpdate:
    def __init__(self) -> None:
        self.effective_user = _StartUser()
        self.effective_message = _FakeMessage()


class _StartUser:
    id = 1001
    language_code = "ru"


class _StartContext:
    def __init__(self, db_path) -> None:
        self.bot_data = {"db_path": db_path}


def _run_start(db_path) -> None:
    """Провести /start через реальный обработчик (пользователь + 13 настроек)."""
    update = _StartUpdate()
    context = _StartContext(db_path)
    asyncio.run(start_command(update, context))  # type: ignore[arg-type]


def _send_help(db_path, user_data=None) -> list[str]:
    """Провести /help через реальный обработчик; вернуть список ответов."""
    update = _FakeUpdate()
    context = _FakeContext(db_path, user_data)
    asyncio.run(help_command(update, context))  # type: ignore[arg-type]
    return update.effective_message.replies


# ---------------------------------------------------------------- Фикстуры


@pytest.fixture()
def db_path(tmp_path):
    """Путь к свежей БД; схема инициализируется /start (init_db)."""
    return tmp_path / "bot.db"


@pytest.fixture()
def conn(db_path):
    """Отдельное соединение для проверок (схема уже инициализирована)."""
    c = init_db(db_path)
    yield c
    c.close()


# ------------------------------------------------------------------- Тесты


def test_tc_helpdel_006_help_text_matches_spec_verbatim():
    """TC-HELPDEL-006 (задача 3.2, AC-6): texts.HELP равен цитате §7.28
    ФАЙЛА ТЗ посимвольно — дословность не «поедет» при правках texts.py."""
    assert texts.HELP == _spec_section("7.28")


def test_tc_helpdel_002_help_while_awake(db_path, conn):
    """TC-HELPDEL-002 (FR-53, D26): /help при бодрствовании (после /start,
    событий нет) отвечает §7.28 дословно; БД не меняется — ни событий,
    ни настроек, ни строк settings_history."""
    _run_start(db_path)
    user_id = UsersRepo(conn).get_or_create(1001)["id"]

    before_settings = SettingsRepo(conn).get_all(user_id)
    before_events = SleepEventsRepo(conn).list_by_user(user_id)

    replies = _send_help(db_path)

    assert replies == [_spec_section("7.28")]
    # Состояние БД нетронуто
    assert SettingsRepo(conn).get_all(user_id) == before_settings
    assert SleepEventsRepo(conn).list_by_user(user_id) == before_events
    assert conn.execute(
        "SELECT COUNT(*) FROM settings_history WHERE user_id = ?", (user_id,)
    ).fetchone()[0] == 0


def test_tc_helpdel_001_help_while_asleep(db_path, conn):
    """TC-HELPDEL-001 (FR-53, D26): /help, когда ребёнок спит (последнее
    событие — sleep), отвечает §7.28 дословно; новое событие НЕ создаётся,
    состояние «спит» не меняется."""
    _run_start(db_path)
    user_id = UsersRepo(conn).get_or_create(1001)["id"]
    events = SleepEventsRepo(conn)
    events.insert(user_id, "sleep", "2026-10-31T13:30:00+00:00")
    before_events = events.list_by_user(user_id)

    replies = _send_help(db_path)

    assert replies == [_spec_section("7.28")]
    assert events.list_by_user(user_id) == before_events  # ничего не добавилось


def test_tc_helpdel_003_help_inside_settings_dialog(db_path, conn):
    """TC-HELPDEL-003 (D26): /help внутри диалога настройки (фаза FSM
    WAIT_SETTING_VALUE в user_data) отвечает §7.28; фаза СОХРАНЕНА —
    диалог не погашен; значение настройки и settings_history не тронуты."""
    _run_start(db_path)
    user_id = UsersRepo(conn).get_or_create(1001)["id"]
    service = SettingsService(conn)
    before = service.get_all(user_id)

    phase = {WAIT_SETTING_VALUE: "night_target"}
    replies = _send_help(db_path, user_data=phase)

    assert replies == [_spec_section("7.28")]
    # Диалог пережил справку
    assert phase.get(WAIT_SETTING_VALUE) == "night_target"
    # Настройки не изменились, записей истории настроек нет
    assert service.get_all(user_id) == before
    assert conn.execute(
        "SELECT COUNT(*) FROM settings_history WHERE user_id = ?", (user_id,)
    ).fetchone()[0] == 0


def test_tc_helpdel_004_help_inside_pair_dialog(db_path, conn):
    """TC-HELPDEL-004 (D26): /help внутри парного ввода задним числом
    (фазы WAIT_PAIR_SLEEP_KEY / WAIT_PAIR_WAKE_KEY) отвечает §7.28;
    обе фазы сохранены — диалог не погашен; событий нет."""
    _run_start(db_path)
    user_id = UsersRepo(conn).get_or_create(1001)["id"]

    phase = {WAIT_PAIR_SLEEP_KEY: True, WAIT_PAIR_WAKE_KEY: None}
    replies = _send_help(db_path, user_data=phase)

    assert replies == [_spec_section("7.28")]
    assert WAIT_PAIR_SLEEP_KEY in phase
    assert WAIT_PAIR_WAKE_KEY in phase
    assert SleepEventsRepo(conn).list_by_user(user_id) == []


def test_tc_helpdel_005_help_repeat_is_idempotent(db_path, conn):
    """TC-HELPDEL-005 (D26): повторный /help даёт тот же §7.28 и не
    меняет БД — идемпотентен (пользователь, настройки, события, история)."""
    _run_start(db_path)
    user_id = UsersRepo(conn).get_or_create(1001)["id"]

    first = _send_help(db_path)
    second = _send_help(db_path)

    assert first == second == [_spec_section("7.28")]
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM settings WHERE user_id = ?", (user_id,)
    ).fetchone()[0] == 13
    assert conn.execute(
        "SELECT COUNT(*) FROM sleep_events WHERE user_id = ?", (user_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM settings_history WHERE user_id = ?", (user_id,)
    ).fetchone()[0] == 0
