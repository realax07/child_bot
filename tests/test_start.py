# Юнит-тесты команды /start (change-пакет add-core-diary, задача 2.1).
#
# Критерии готовности 2.1: первый /start создаёт пользователя и 13 ключей
# settings (FR-5, FR-23); повторный /start идемпотентен — настройки и события
# не сбрасываются; TZ записан в users.timezone (FR-4, design D4); роль
# parent (FR-22). Ответ — текст §7.1 и клавиатура «😴 Заснул».
#
# Правило rule 6: каждый тест несёт TC-START-NNN в первой строке докстринга.

from __future__ import annotations

import asyncio

import pytest

from src import texts
from src.db import init_db
from src.handlers.start import BTN_SLEEP, map_language_to_tz, start_command
from src.repositories.settings import SettingsRepo
from src.repositories.sleep_events import SleepEventsRepo
from src.repositories.users import UsersRepo
from src.services.settings import DEFAULT_KEYS, SettingsService


# ------------------------------------------------------------ Заглушки PTB

class _FakeMessage:
    def __init__(self) -> None:
        self.text: str | None = None
        self.reply_markup = None

    async def reply_text(self, text, reply_markup=None):
        self.text = text
        self.reply_markup = reply_markup


class _FakeUser:
    def __init__(self, user_id: int, language_code: str | None) -> None:
        self.id = user_id
        self.language_code = language_code


class _FakeUpdate:
    def __init__(self, user_id: int, language_code: str | None) -> None:
        self.effective_user = _FakeUser(user_id, language_code)
        self.effective_message = _FakeMessage()


class _FakeContext:
    def __init__(self, db_path) -> None:
        self.bot_data = {"db_path": db_path}


def _send_start(db_path, user_id: int = 1001, language_code: str | None = "ru"):
    """Провести /start через реальный обработчик с фейковым Update/Context."""
    update = _FakeUpdate(user_id, language_code)
    context = _FakeContext(db_path)
    asyncio.run(start_command(update, context))  # type: ignore[arg-type]
    return update.effective_message


# ---------------------------------------------------------------- Фикстуры

@pytest.fixture()
def db_path(tmp_path):
    """Путь к свежей БД; инициализируется самим обработчиком при /start."""
    return tmp_path / "bot.db"


@pytest.fixture()
def conn(db_path):
    """Отдельное соединение для проверок (инициализация схемы уже была)."""
    c = init_db(db_path)
    yield c
    c.close()


# ------------------------------------------------------------------- Тесты

def test_start_first_creates_user_and_13_settings(db_path, conn):
    """TC-START-001: первый /start создаёт пользователя и ровно 13 настроек
    со значениями по умолчанию (FR-5, FR-23, design D14)."""
    _send_start(db_path)

    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert users == 1

    user = UsersRepo(conn).get_or_create(1001)
    stored = SettingsRepo(conn).get_all(user["id"])
    assert set(stored) == set(DEFAULT_KEYS)
    assert len(stored) == 13
    for key, default in DEFAULT_KEYS.items():
        assert stored[key] == str(default), f"дефолт {key} не совпал"

    # Чтение через сервис — в родных типах
    service = SettingsService(conn)
    values = service.get_all(user["id"])
    assert values["merge_threshold"] == 20 and isinstance(values["merge_threshold"], int)
    assert values["wake_total_max"] == 12.5 and isinstance(values["wake_total_max"], float)
    assert values["night_bounds"] == "19:00-07:00"
    assert values["target_nap_duration"] == 90  # решение 26.10 №1


def test_start_repeat_is_idempotent(db_path, conn):
    """TC-START-002: повторный /start не сбрасывает настройки и события
    (FR-5, design D14)."""
    _send_start(db_path)
    _send_start(db_path)

    users_repo = UsersRepo(conn)
    user = users_repo.get_or_create(1001)

    # Пользователь один, настроек по-прежнему 13
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM settings WHERE user_id=?",
                        (user["id"],)).fetchone()[0] == 13

    # Правка пользователя и событие переживают повторный /start
    settings_repo = SettingsRepo(conn)
    settings_repo.upsert(user["id"], "merge_threshold", "30")
    events_repo = SleepEventsRepo(conn)
    events_repo.insert(user["id"], "sleep", "2026-10-26 13:30:00")

    _send_start(db_path)

    assert settings_repo.get(user["id"], "merge_threshold") == "30"
    assert conn.execute("SELECT COUNT(*) FROM settings WHERE user_id=?",
                        (user["id"],)).fetchone()[0] == 13
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert len(events_repo.list_by_user(user["id"])) == 1


def test_start_saves_timezone(db_path, conn):
    """TC-START-003: TZ записан в users.timezone по карте language_code →
    IANA, fallback Europe/Moscow (FR-4, design D4)."""
    _send_start(db_path, user_id=2001, language_code="ru")
    assert UsersRepo(conn).get_timezone(2001) == "Europe/Moscow"

    _send_start(db_path, user_id=2002, language_code="uk-UA")
    assert UsersRepo(conn).get_timezone(2002) == "Europe/Kyiv"

    _send_start(db_path, user_id=2003, language_code="de")
    assert UsersRepo(conn).get_timezone(2003) == "Europe/Moscow"  # fallback

    # Карта — артефакт кода, фиксируется юнит-тестом (design D4)
    assert map_language_to_tz("kk") == "Asia/Almaty"
    assert map_language_to_tz(None) == "Europe/Moscow"


def test_start_creates_parent_role(db_path, conn):
    """TC-START-004: созданный пользователь имеет роль parent (FR-22,
    решение 26.10 №5)."""
    _send_start(db_path, user_id=3001)
    row = conn.execute(
        "SELECT role FROM users WHERE telegram_id=3001"
    ).fetchone()
    assert row is not None and row[0] == "parent"


def test_start_replies_text_7_1_and_awake_keyboard(db_path):
    """TC-START-005: ответ — текст §7.1 дословно (texts.START) и клавиатура
    состояния «бодрствует» с кнопкой «😴 Заснул» (ТЗ §6; меню уточнит 2.2)."""
    message = _send_start(db_path)
    assert message.text == texts.START
    assert message.reply_markup is not None
    row = message.reply_markup.keyboard[0]
    assert len(row) == 1 and row[0].text == BTN_SLEEP


def test_seed_defaults_keeps_user_edits(conn):
    """TC-START-006: SettingsService.seed_defaults идемпотентен — существующие
    значения не перезаписываются, недостающие ключи досоздаются (FR-5,
    design D14)."""
    service = SettingsService(conn)
    user = UsersRepo(conn).get_or_create(4001)
    service.seed_defaults(user["id"])
    SettingsRepo(conn).upsert(user["id"], "day_start", "05:30")
    service.seed_defaults(user["id"])
    values = service.get_all(user["id"])
    assert values["day_start"] == "05:30"
    assert len(values) == 13

    # Удалённый ключ восстанавливается дефолтом при повторном seed
    conn.execute("DELETE FROM settings WHERE user_id=? AND key='day_start'", (user["id"],))
    conn.commit()
    service.seed_defaults(user["id"])
    assert service.get(user["id"], "day_start") == "06:00"
