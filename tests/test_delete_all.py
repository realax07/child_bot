"""Юнит-тесты /delete_all с подтверждением (change-пакет
add-settings-history, задача 3.3).

ТЗ §7.26–7.27 (тексты дословно; состав кнопок §7.26 — решение продакта
01.11 по ОВ-5.3: подтверждение ОТДЕЛЬНОЙ кнопкой «Да, удалить всё»,
текст-пароль отклонён), ТЗ §10.5 (удаление всех данных пользователя),
§10.2 (команда); design D25; FR-54; спека help-and-delete.

Проверяются: отдельный шаг подтверждения (без нажатия кнопки удаления
НЕТ — любой другой ввод гасит фазу); удаление ВСЕХ 4 сущностей только
своего user_id (второй пользователь цел) в одной транзакции; §7.27
после удаления; отмена — меню без изменений без §7.27; /start заново
пересеивает 13 дефолтов (D14); задачи напоминаний
«remind:<user_id>:<kind>» сняты (точка EventService, FR-41/D19);
повторный /delete_all после удаления корректен; взаимное исключение
FSM-фаз в обе стороны; наблюдателю — отказ (D13).

Эталон текстов — ФАЙЛ ТЗ (паттерн TC-HINT-009 / TC-HELPDEL-006): сверка
не может «поехать» вместе с src/texts.py.

Правило rule 6: каждый тест несёт TC-HELPDEL-0xx в первой строке
докстринга (нумерация продолжается за задачей 3.2: там TC-HELPDEL-001…
006).
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import texts
from src.db import init_db
from src.handlers import (
    CONFIRM_DELETE_ALL_KEY,
    CALLBACK_DELETE_ALL_NO,
    CALLBACK_DELETE_ALL_YES,
    _discard_delete_confirm_update,
    delete_all_callback,
    delete_all_command,
)
from src.handlers.events import WAIT_PAIR_SLEEP_KEY
from src.handlers.settings_screen import WAIT_SETTING_VALUE
from src.handlers.start import start_command
from src.repositories.sleep_events import SleepEventsRepo
from src.repositories.users import UsersRepo
from src.services import scheduler as scheduler_mod
from src.services.scheduler import KIND_NAP, KIND_NIGHT, ReminderScheduler
from src.services.settings import DEFAULT_KEYS, SettingsService

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC_FILE = _REPO_ROOT / "ТЗ_Дневник_сна_ребёнка.md"

_TZ = "Europe/Moscow"  # UTC+3, без перехода на летнее время в 2026
_NOW = datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc)  # 12:30 МСК


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
        self.text: str | None = None
        self.reply_markup = None
        self.replies: list[str] = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)
        self.reply_markup = reply_markup

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup


class _FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.answers: list[str | None] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.message.reply_markup = reply_markup


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.language_code = "ru"


class _CommandUpdate:
    """Команда (/start, /delete_all)."""

    def __init__(self, user_id: int) -> None:
        self.effective_user = _FakeUser(user_id)
        self.callback_query = None
        self.message = _FakeMessage()
        self.effective_message = self.message


class _CallbackUpdate:
    """Нажатие inline-кнопки подтверждения §7.26."""

    def __init__(self, user_id: int, data: str) -> None:
        self.effective_user = _FakeUser(user_id)
        self.callback_query = _FakeCallbackQuery(data)
        self.effective_message = self.callback_query.message


class _TextUpdate:
    """Произвольный текстовый ввод (не кнопка)."""

    def __init__(self, user_id: int, text: str) -> None:
        self.effective_user = _FakeUser(user_id)
        self.callback_query = None
        self.message = _FakeMessage()
        self.message.text = text
        self.effective_message = self.message


class _FakeContext:
    def __init__(self, db_path, user_data=None) -> None:
        self.bot_data = {"db_path": db_path}
        self.user_data = user_data if user_data is not None else {}


# ------------------------------------------------------------------ драйвер


def _run(command, db_path, update, context):
    asyncio.run(command(update, context))  # type: ignore[arg-type]
    return update, context


def _run_start(db_path, telegram_id: int = 1001):
    """/start через реальный обработчик (пользователь + 13 настроек)."""
    return _run(start_command, db_path, _CommandUpdate(telegram_id), _FakeContext(db_path))


def _run_delete_command(db_path, user_data=None, telegram_id: int = 1001):
    """/delete_all через реальный обработчик (только вопрос §7.26)."""
    return _run(
        delete_all_command,
        db_path,
        _CommandUpdate(telegram_id),
        _FakeContext(db_path, user_data),
    )


def _press(db_path, data: str, user_data, telegram_id: int = 1001):
    """Нажатие кнопки §7.26 через реальный обработчик колбэка."""
    return _run(
        delete_all_callback,
        db_path,
        _CallbackUpdate(telegram_id, data),
        _FakeContext(db_path, user_data),
    )


def _other_input(update, user_data, db_path):
    """Любой другой ввод — через реальный guard-обработчик группы 3."""
    _run(
        _discard_delete_confirm_update,
        db_path,
        update,
        _FakeContext(db_path, user_data),
    )


def _seed_diary(conn, user_id: int) -> None:
    """Дневник с данными: события + изменённая настройка + строка истории."""
    events = SleepEventsRepo(conn)
    events.insert(user_id, "sleep", "2026-11-02 19:00:00")
    events.insert(user_id, "wake", "2026-11-03 05:00:00")
    SettingsService(conn).update(user_id, "night_target", "23:00")


def _counts(conn, user_id: int) -> dict:
    """Число строк всех 4 сущностей пользователя."""
    return {
        table: conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        for table in ("sleep_events", "settings", "settings_history")
    } | {
        "users": conn.execute(
            "SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)
        ).fetchone()[0]
    }


# ---------------------------------------------------------------- Фикстуры


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "bot.db"


@pytest.fixture()
def conn(db_path):
    c = init_db(db_path)
    yield c
    c.close()


# ------------------------------------------------------------------- Тесты


def test_tc_helpdel_007_texts_verbatim_and_ov53_buttons():
    """TC-HELPDEL-007 (AC-6, ОВ-5.3): вопрос §7.26 и §7.27 равны цитатам
    ФАЙЛА ТЗ посимвольно; кнопки — решение продакта 01.11: [Да, удалить
    всё]/[Отмена] (строка кнопок §7.26 в ТЗ «[Да, удалить]» заменена
    решением ОВ-5.3, вопрос — дословно)."""
    spec_726 = _spec_section("7.26")
    question, _, spec_buttons = spec_726.partition("\n\n")
    assert texts.DELETE_ALL_CONFIRM == question
    # Заменённая решением строка кнопок в исходнике существует (защита
    # от молчаливой перефразировки обеих сторон).
    assert spec_buttons == "[Да, удалить] [Отмена]"
    assert texts.BTN_DELETE_ALL_YES == "Да, удалить всё"
    assert texts.BTN_DELETE_ALL_CANCEL == "Отмена"
    assert texts.DELETE_ALL_DONE == _spec_section("7.27")


def test_tc_helpdel_008_no_confirmation_no_deletion(db_path, conn):
    """TC-HELPDEL-008 (ОВ-5.3, спека «MUST только после явного нажатия»):
    /delete_all ничего не удаляет (только вопрос §7.26); произвольный
    текст гасит фазу подтверждения; нажатие «Да, удалить всё» после
    гашения — данных нет изменений и §7.27 НЕ отправляется."""
    _run_start(db_path)
    user_id = UsersRepo(conn).get_or_create(1001)["id"]
    _seed_diary(conn, user_id)
    before = _counts(conn, user_id)

    cmd, context = _run_delete_command(db_path)
    assert cmd.effective_message.replies == [texts.DELETE_ALL_CONFIRM]
    assert context.user_data[CONFIRM_DELETE_ALL_KEY] is True
    assert _counts(conn, user_id) == before  # удалений на шаге вопроса нет

    # Любой другой ввод гасит состояние подтверждения БЕЗ удаления.
    _other_input(_TextUpdate(1001, "какой-то текст"), context.user_data, db_path)
    assert CONFIRM_DELETE_ALL_KEY not in context.user_data

    upd, context = _press(db_path, CALLBACK_DELETE_ALL_YES, context.user_data)
    assert upd.effective_message.replies == []  # §7.27 нет
    assert _counts(conn, user_id) == before  # данные целы


def test_tc_helpdel_009_confirm_deletes_all_entities_own_user_only(db_path, conn):
    """TC-HELPDEL-009 (FR-54, §10.5, D25): подтверждение удаляет ВСЕ 4
    сущности (sleep_events, settings, settings_history, users) только
    СВОЕГО user_id; данные второго пользователя целы; ответ — §7.27."""
    _run_start(db_path, 1001)
    _run_start(db_path, 2002)
    user_a = UsersRepo(conn).get_or_create(1001)["id"]
    user_b = UsersRepo(conn).get_or_create(2002)["id"]
    _seed_diary(conn, user_a)
    _seed_diary(conn, user_b)
    assert _counts(conn, user_a)["settings_history"] == 1  # история реально есть

    _, context = _run_delete_command(db_path, telegram_id=1001)
    upd, _ = _press(db_path, CALLBACK_DELETE_ALL_YES, context.user_data, telegram_id=1001)

    assert upd.effective_message.replies == [texts.DELETE_ALL_DONE]
    assert texts.DELETE_ALL_DONE == _spec_section("7.27")
    # Удалены все 4 сущности пользователя A…
    assert all(v == 0 for v in _counts(conn, user_a).values())
    assert conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (user_a,)).fetchone()[0] == 0
    # …а пользователь B не тронут (изоляция удаления, спека D13).
    counts_b = _counts(conn, user_b)
    assert counts_b["users"] == 1
    assert counts_b["sleep_events"] == 2
    assert counts_b["settings"] == 13
    assert counts_b["settings_history"] == 1


def test_tc_helpdel_010_cancel_no_deletion_no_done_text(db_path, conn):
    """TC-HELPDEL-010 (спека «Отмена»): «Отмена» — ничего не удалено,
    фаза снята, бот вернулся в меню (клавиатура перерисована); §7.27
    НЕ отправляется."""
    _run_start(db_path)
    user_id = UsersRepo(conn).get_or_create(1001)["id"]
    _seed_diary(conn, user_id)
    before = _counts(conn, user_id)

    _, context = _run_delete_command(db_path)
    upd, context = _press(db_path, CALLBACK_DELETE_ALL_NO, context.user_data)

    assert _counts(conn, user_id) == before  # ничего не удалено
    assert texts.DELETE_ALL_DONE not in upd.effective_message.replies
    assert CONFIRM_DELETE_ALL_KEY not in context.user_data
    # Возврат в меню: клавиатура сообщения перерисована (guard-стиль D5).
    assert upd.effective_message.reply_markup is not None


def test_tc_helpdel_011_start_reseeds_13_defaults(db_path, conn):
    """TC-HELPDEL-011 (D14, спека «повторный /start пересеивает 13
    дефолтов»): после удаления /start создаёт пользователя заново;
    настройки — ровно 13 дефолтов (правка до удаления не переживает),
    истории изменений нет."""
    _run_start(db_path)
    user_id = UsersRepo(conn).get_or_create(1001)["id"]
    _seed_diary(conn, user_id)  # night_target=23:00 + строка истории
    assert SettingsService(conn).get(user_id, "night_target") == "23:00"

    _, context = _run_delete_command(db_path)
    _press(db_path, CALLBACK_DELETE_ALL_YES, context.user_data)

    _run_start(db_path)
    new_id = UsersRepo(conn).get_or_create(1001)["id"]
    settings = SettingsService(conn).get_all(new_id)
    assert len(settings) == 13
    assert settings == DEFAULT_KEYS  # дефолты, а не прежние значения
    assert conn.execute(
        "SELECT COUNT(*) FROM settings_history WHERE user_id = ?", (new_id,)
    ).fetchone()[0] == 0
    assert SleepEventsRepo(conn).list_by_user(new_id) == []


def test_tc_helpdel_012_reminder_jobs_removed(db_path, conn):
    """TC-HELPDEL-012 (FR-41, D10/D19/D25): после удаления план
    напоминаний пустеет — задачи «remind:<user_id>:<kind>» сняты
    ре-шедулом из точки EventService.delete_all."""
    conn.execute("PRAGMA foreign_keys=ON")
    user = UsersRepo(conn).get_or_create(555000, timezone=_TZ)
    SettingsService(conn).seed_defaults(user["id"])
    SleepEventsRepo(conn).insert(user["id"], "sleep", "2026-11-02 19:00:00")
    SleepEventsRepo(conn).insert(user["id"], "wake", "2026-11-03 05:00:00")

    sched = ReminderScheduler(
        _FakeBot(), db_path, clock=lambda: _NOW
    )
    sched.reschedule()
    nap_job = f"remind:{user['id']}:{KIND_NAP}"
    night_job = f"remind:{user['id']}:{KIND_NIGHT}"
    times = sched.planned_fire_times()
    assert times.get(nap_job) is not None
    assert times.get(night_job) is not None

    scheduler_mod._set_active(sched)  # как в живом процессе (design D19)
    try:
        _, context = _run_delete_command(db_path, telegram_id=555000)
        _press(db_path, CALLBACK_DELETE_ALL_YES, context.user_data, telegram_id=555000)
    finally:
        scheduler_mod._set_active(None)

    times = sched.planned_fire_times()
    assert nap_job not in times
    assert night_job not in times
    assert [jid for jid in times if jid.startswith("remind:")] == []


def test_tc_helpdel_013_repeat_delete_all_after_deletion(db_path, conn):
    """TC-HELPDEL-013: повторный /delete_all после удаления корректен,
    хотя данных нет: снова §7.26 → §7.27, без ошибок, все 4 таблицы
    пусты для пользователя."""
    _run_start(db_path)
    _, context = _run_delete_command(db_path)
    _press(db_path, CALLBACK_DELETE_ALL_YES, context.user_data)
    user_id = UsersRepo(conn).get_or_create(1001)["id"]  # /delete_all пересоздаёт
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1

    cmd, context = _run_delete_command(db_path)
    assert cmd.effective_message.replies == [texts.DELETE_ALL_CONFIRM]
    upd, _ = _press(db_path, CALLBACK_DELETE_ALL_YES, context.user_data)
    assert upd.effective_message.replies == [texts.DELETE_ALL_DONE]
    assert all(v == 0 for v in _counts(conn, user_id).values())
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_tc_helpdel_014_fsm_phases_mutually_exclusive(db_path, conn):
    """TC-HELPDEL-014 (взаимное исключение фаз, образец major
    review-001-1.2): вход в /delete_all гасит фазы backfill-диалога и
    диалога настройки; наоборот — любой конкурирующий ввод (текст,
    команда, чужой колбэк settings:edit) гасит фазу подтверждения, и
    устаревшее «Да, удалить всё» удаление НЕ вызывает."""
    _run_start(db_path)
    user_id = UsersRepo(conn).get_or_create(1001)["id"]
    _seed_diary(conn, user_id)
    before = _counts(conn, user_id)

    # 1) /delete_all гасит конкурирующие диалоги (прямая сторона).
    phases = {WAIT_PAIR_SLEEP_KEY: True, WAIT_SETTING_VALUE: "night_target"}
    _, context = _run_delete_command(db_path, user_data=phases)
    assert WAIT_PAIR_SLEEP_KEY not in context.user_data
    assert WAIT_SETTING_VALUE not in context.user_data
    assert context.user_data[CONFIRM_DELETE_ALL_KEY] is True

    # 2) Конкурирующий ввод гасит подтверждение (обратная сторона):
    #    чужой колбэк settings:edit:<key> проходит через guard группы 3.
    _other_input(
        _CallbackUpdate(1001, "settings:edit:night_target"),
        context.user_data,
        db_path,
    )
    assert CONFIRM_DELETE_ALL_KEY not in context.user_data

    # 3) Команда (не /delete_all) тоже гасит; после этого устаревшее
    #    «Да, удалить всё» — без удаления и без §7.27.
    _, context = _run_delete_command(db_path)
    _other_input(_TextUpdate(1001, "/start"), context.user_data, db_path)
    assert CONFIRM_DELETE_ALL_KEY not in context.user_data
    upd, _ = _press(db_path, CALLBACK_DELETE_ALL_YES, context.user_data)
    assert upd.effective_message.replies == []
    assert _counts(conn, user_id) == before

    # 4) Своя команда /delete_all фазу НЕ гасит (guard исключает её) —
    #    повторный вход валиден и идемпотентен по фазе.
    _, context = _run_delete_command(db_path)
    _other_input(_TextUpdate(1001, "/delete_all"), context.user_data, db_path)
    assert context.user_data[CONFIRM_DELETE_ALL_KEY] is True
    _, context = _run_delete_command(db_path, user_data=context.user_data)
    assert context.user_data[CONFIRM_DELETE_ALL_KEY] is True


def test_tc_helpdel_015_observer_denied(db_path, conn):
    """TC-HELPDEL-015 (design D13: delete_all — только parent): наблюдатель
    получает отказ, фаза подтверждения не ставится, удаления нет."""
    _run_start(db_path)
    user_id = UsersRepo(conn).get_or_create(1001)["id"]
    _seed_diary(conn, user_id)
    conn.execute("UPDATE users SET role = 'observer' WHERE id = ?", (user_id,))
    conn.commit()
    before = _counts(conn, user_id)

    cmd, context = _run_delete_command(db_path)
    assert cmd.effective_message.replies == [texts.OBSERVER_DENIED]
    assert CONFIRM_DELETE_ALL_KEY not in context.user_data
    assert _counts(conn, user_id) == before


class _FakeBot:
    """Ловит send_message — пушей без реального Telegram (design D19)."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append((chat_id, text))


class _FailingConn:
    """Прокси-соединение: 3-й DELETE (settings_history) падает (инъекция
    сбоя между DELETE — образец пробы ревьюера review-001-3.3)."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql, params=()):
        if "settings_history" in sql:
            raise sqlite3.OperationalError("injected failure (test)")
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_tc_helpdel_016_delete_all_failure_explicit_rollback(db_path, conn):
    """TC-HELPDEL-016 (4.2, minor-1 review-001-3.3): сбой между DELETE —
    ЯВНЫЙ rollback в UsersRepo.delete_all_data: атомарность не зависит
    от дисциплины вызывающего — на ТОМ ЖЕ соединении после исключения
    все 4 сущности целы (не ждём close() и неявный откат)."""
    _run_start(db_path)
    user_id = UsersRepo(conn).get_or_create(1001)["id"]
    _seed_diary(conn, user_id)
    before = _counts(conn, user_id)

    repo = UsersRepo(_FailingConn(conn))
    with pytest.raises(sqlite3.OperationalError):
        repo.delete_all_data(user_id)

    # явный rollback: частичные изменения НЕ видны на том же соединении
    assert _counts(conn, user_id) == before


def test_tc_helpdel_017_guard_matches_first_token_only(db_path, conn):
    """TC-HELPDEL-017 (4.2, minor-2 review-001-3.3): guard сравнивает
    ПЕРВЫЙ токен: мусорная команда «/delete_allxyz» ГАСИТ фазу
    подтверждения (CommandHandler на неё не срабатывает), а ввод с
    первым токеном ровно «/delete_all» — нет."""
    _, context = _run_delete_command(db_path)
    assert context.user_data[CONFIRM_DELETE_ALL_KEY] is True

    _other_input(
        _TextUpdate(1001, "/delete_allxyz"), context.user_data, db_path
    )
    assert CONFIRM_DELETE_ALL_KEY not in context.user_data

    # первый токен ровно /delete_all — собственная команда, фаза цела
    _, context = _run_delete_command(db_path)
    _other_input(
        _TextUpdate(1001, "/delete_all"), context.user_data, db_path
    )
    assert context.user_data[CONFIRM_DELETE_ALL_KEY] is True
