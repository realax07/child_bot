"""Экран настроек и диалог изменения — задача 1.2 пакета add-settings-history.

Handler settings_screen.py (design D21; ТЗ §7.16–7.19, §10.2–10.4):
экран §7.16 (группы, текущие значения пользователя) по команде /settings,
callback settings:show и тексту панели «⚙️ Настройки»; диалог §7.17
(callback settings:edit:<key>, фаза FSM WAIT_SETTING_VALUE); успех §7.18
«старое → новое» (SettingsService.update); ошибка §7.19 без изменения
настройки; отмена диалога без изменений; «⚙️ Настройки» не пишет в
sleep_events; наблюдатель — read-only (экран видит, править нельзя —
design D13); FR-24: правка wake_deadline меняет окно следующего расчёта.

Каждый тест помечен TC-SETT-NNN в первой строке докстринга (rule 6).
Время НЕ влияет на ответы экрана (значения настроек статичны) — «живых»
ЧЧ:ММ нет; события FR-24-теста — фиксированные даты 03.11.2026.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

from src import texts
from src.db import init_db
from src.handlers import settings_screen
from src.handlers.settings_screen import (
    CALLBACK_SETTINGS_CANCEL,
    CALLBACK_SETTINGS_SHOW,
    WAIT_SETTING_VALUE,
)
from src.repositories.settings import SettingsHistoryRepo
from src.services.calculation import nap_window
from src.services.event import get_user
from src.services.settings import DEFAULT_KEYS, SettingsService

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC_FILE = _REPO_ROOT / "ТЗ_Дневник_сна_ребёнка.md"


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


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeCallbackUpdate:
    """Нажатие inline-кнопки настроек."""

    def __init__(self, user_id: int, data: str) -> None:
        self.effective_user = _FakeUser(user_id)
        self.callback_query = _FakeCallbackQuery(data)
        self.effective_message = self.callback_query.message


class _FakeTextUpdate:
    """Текстовое сообщение: команда /settings или ввод значения."""

    def __init__(self, user_id: int, text: str) -> None:
        self.effective_user = _FakeUser(user_id)
        self.callback_query = None
        self.message = _FakeMessage()
        self.message.text = text
        self.effective_message = self.message


class _FakeContext:
    def __init__(self, db_path) -> None:
        self.bot_data = {"db_path": db_path}
        self.user_data: dict = {}


# --------------------------------------------------------------- Фикстуры


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "bot.db"


@pytest.fixture()
def tg_id():
    """Telegram-id владельца (произвольный; БД свежая в каждом тесте)."""
    return 4242


def _press(db_path, data: str, telegram_id: int = 4242, context=None):
    """Провести нажатие inline-кнопки настроек через реальный обработчик.

    context можно передать свой — чтобы нажатия делили одну фазу FSM.
    """
    update = _FakeCallbackUpdate(telegram_id, data)
    if context is None:
        context = _FakeContext(db_path)
    asyncio.run(settings_screen.settings_callback(update, context))  # type: ignore[arg-type]
    return update, context


def _command(db_path, text: str = "/settings", telegram_id: int = 4242):
    update = _FakeTextUpdate(telegram_id, text)
    context = _FakeContext(db_path)
    asyncio.run(settings_screen.settings_command(update, context))  # type: ignore[arg-type]
    return update, context


def _type_value(db_path, context, text: str, telegram_id: int = 4242):
    """Ввести значение настройки в фазе WAIT_SETTING_VALUE."""
    update = _FakeTextUpdate(telegram_id, text)
    asyncio.run(settings_screen.setting_value_message(update, context))  # type: ignore[arg-type]
    return update


def _panel_text(db_path, telegram_id: int = 4242):
    """Текст «⚙️ Настройки» с reply-панели через общий маршрутизатор."""
    from src.handlers import common

    update = _FakeTextUpdate(telegram_id, settings_screen.BTN_SETTINGS)
    context = _FakeContext(db_path)

    async def _run():
        conn = common.open_conn(context)
        try:
            user = get_user(conn, telegram_id)
            await settings_screen.reply_settings_button(
                update, context, conn, user  # type: ignore[arg-type]
            )
        finally:
            conn.close()

    asyncio.run(_run())
    return update


def _event_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM sleep_events").fetchone()[0]


def _history_rows(conn, user_id: int):
    return SettingsHistoryRepo(conn).list_for_user(user_id)


# --------------------------------------------------- §7.16: главный экран


def test_tc_sett_015_screen_shows_groups_and_current_values(db_path):
    """TC-SETT-015: экран §7.16 — все группы, все параметры с текущими
    значениями; при дефолтах значения совпадают с примерами ТЗ.

    Трассировка: Requirement «Экран настроек» (FR-42, FR-43), Scenario
    «Открытие экрана из меню»; design D21 (состав — 12 параметров §7.16
    + target_nap_duration в «Прочее»).
    """
    conn = init_db(db_path)
    user = get_user(conn, 4242)
    SettingsService(conn).seed_defaults(user["id"])
    update, _ = _press(db_path, CALLBACK_SETTINGS_SHOW)

    text = update.effective_message.replies[0]
    # Заголовок и подвал §7.16 дословно.
    assert texts.SETTINGS_SCREEN_HEADER in text
    assert texts.SETTINGS_SCREEN_FOOTER in text
    # Все 4 группы §7.16.
    for group in (
        texts.SETTINGS_GROUP_DAY,
        texts.SETTINGS_GROUP_NIGHT,
        texts.SETTINGS_GROUP_WAKE,
        texts.SETTINGS_GROUP_MISC,
    ):
        assert f"**{group}**" in text
    # Все 13 параметров (labels = DEFAULT_KEYS, без копий — design D14).
    assert set(texts.SETTINGS_PARAM_LABELS) == set(DEFAULT_KEYS)
    for label in texts.SETTINGS_PARAM_LABELS.values():
        assert label in text
    # Дефолтные значения — в форматах ТЗ §7.16.
    assert "- Минимум бодрствования до дневного сна: 5 ч 20 мин" in text
    assert "- Максимум бодрствования до дневного сна: 6 ч" in text
    assert "- Крайнее время пробуждения от дневного сна: 15:30" in text
    assert "- Целевое время ухода: 22:00" in text
    assert "- Крайнее время ухода: 22:30" in text
    assert "- Нижняя граница: 12 ч" in text
    assert "- Верхняя граница: 12 ч 30 мин" in text
    assert "- Напоминание заранее: 15 мин" in text
    assert "- Порог склейки снов: 20 мин" in text
    assert "- Границы ночного сна: 19:00–07:00" in text
    assert "- Начало дня: 06:00" in text
    assert "- Максимальная глубина ввода: 3 дня" in text
    assert "- Целевая длительность дневного сна: 1 ч 30 мин" in text
    conn.close()


def test_tc_sett_016_custom_value_shown_on_screen(db_path):
    """TC-SETT-016: экран показывает ТЕКУЩЕЕ значение пользователя, а не
    дефолт (правка через SettingsService.update видна на экране).

    Трассировка: Requirement «Экран настроек» (FR-42), FR-45; design D21.
    """
    conn = init_db(db_path)
    user = get_user(conn, 4242)
    SettingsService(conn).seed_defaults(user["id"])
    SettingsService(conn).update(user["id"], "remind_before", "25м")
    update, _ = _command(db_path)

    text = update.effective_message.replies[0]
    assert "- Напоминание заранее: 25 мин" in text
    assert "- Напоминание заранее: 15 мин" not in text
    conn.close()


def test_tc_sett_017_settings_command_same_screen_as_callback(db_path):
    """TC-SETT-017: /settings (ТЗ §10.2) даёт тот же экран §7.16, что и
    callback settings:show, и текст reply-панели «⚙️ Настройки» (ТЗ §10.3
    — двойной вход, одна логика).

    Трассировка: Requirement «Экран настроек» (FR-42); design D21.
    """
    by_command, _ = _command(db_path)
    by_callback, _ = _press(db_path, CALLBACK_SETTINGS_SHOW, telegram_id=777)
    by_panel = _panel_text(db_path, telegram_id=888)

    screen = by_command.effective_message.replies[0]
    assert by_callback.effective_message.replies[0] == screen
    assert by_panel.effective_message.replies[0] == screen


# -------------------------------------------------- §7.17: диалог значения


def test_tc_sett_018_edit_opens_dialog_with_name_current_format(db_path):
    """TC-SETT-018: нажатие параметра → §7.17 (имя, текущее, формат) и
    фаза WAIT_SETTING_VALUE установлена.

    Трассировка: Requirement «Экран настроек» (FR-43), Scenario «Запрос
    нового значения»; ТЗ §10.4; design D21.
    """
    conn = init_db(db_path)
    user = get_user(conn, 4242)
    SettingsService(conn).seed_defaults(user["id"])
    update, context = _press(db_path, "settings:edit:sleep_min_before_nap")

    assert context.user_data.get(WAIT_SETTING_VALUE) == "sleep_min_before_nap"
    dialog = update.effective_message.replies[0]
    expected = texts.settings_ask_value_text(
        "Минимум бодрствования до дневного сна",
        "5 ч 20 мин",
        texts.SETTINGS_FORMAT_DURATION,
    )
    assert dialog == expected
    # Дословная структура §7.17.
    assert dialog == (
        "Введите новое значение для «Минимум бодрствования до дневного сна».\n"
        "Текущее: 5 ч 20 мин.\n"
        "Формат: `5:20` или `5ч20м`."
    )
    # Кнопка отмены диалога — служебная, callback §10.4-стиля.
    assert update.effective_message.reply_markup is not None
    conn.close()


# ------------------------------------------------- §7.18: успешная правка


def test_tc_sett_019_successful_edit_updates_and_logs(db_path):
    """TC-SETT-019: ввод «5ч30м» → §7.18 «5 ч 20 мин → 5 ч 30 мин»;
    настройка обновлена, settings_history содержит old/new (FR-46), фаза
    снята.

    Трассировка: Requirement «Валидация значений и успех» (FR-45),
    Scenario «Успешная правка»; Requirement «Логирование изменений»
    (FR-46); design D21.
    """
    conn = init_db(db_path)
    user = get_user(conn, 4242)
    SettingsService(conn).seed_defaults(user["id"])
    _, context = _press(db_path, "settings:edit:sleep_min_before_nap")
    reply = _type_value(db_path, context, "5ч30м")

    assert reply.effective_message.replies == [
        texts.settings_updated_text(
            "Минимум бодрствования до дневного сна",
            "5 ч 20 мин",
            "5 ч 30 мин",
        )
    ]
    assert WAIT_SETTING_VALUE not in context.user_data
    assert SettingsService(conn).get(user["id"], "sleep_min_before_nap") == 330
    rows = _history_rows(conn, user["id"])
    assert len(rows) == 1
    assert rows[0]["key"] == "sleep_min_before_nap"
    assert rows[0]["old_value"] == "320"
    assert rows[0]["new_value"] == "330"
    conn.close()


def test_tc_sett_020_time_key_edit_round_trip(db_path):
    """TC-SETT-020: правка время-ключа wake_deadline — §7.18 с
    нормализованным значением, значение в settings обновлено.

    Трассировка: Requirement «Валидация значений и успех» (FR-44/FR-45);
    design D21; Scenario «Запись при смене» (спека settings-ui).
    """
    conn = init_db(db_path)
    user = get_user(conn, 4242)
    SettingsService(conn).seed_defaults(user["id"])
    _, context = _press(db_path, "settings:edit:wake_deadline")
    reply = _type_value(db_path, context, "15:40")

    assert reply.effective_message.replies == [
        texts.settings_updated_text(
            "Крайнее время пробуждения от дневного сна", "15:30", "15:40"
        )
    ]
    assert SettingsService(conn).get(user["id"], "wake_deadline") == "15:40"
    conn.close()


# --------------------------------------------------- §7.19: ошибка ввода


def test_tc_sett_021_garbage_input_keeps_setting_and_phase(db_path):
    """TC-SETT-021: «полтора часа» → §7.19, настройка и история НЕ
    изменены, фаза сохранена — повторный ввод успешен.

    Трассировка: Requirement «Валидация значений и успех» (FR-44/FR-45),
    Scenario «Неразбираемый ввод»; Scenario «Ошибка не логируется» (FR-46);
    design D21.
    """
    conn = init_db(db_path)
    user = get_user(conn, 4242)
    SettingsService(conn).seed_defaults(user["id"])
    _, context = _press(db_path, "settings:edit:sleep_min_before_nap")
    reply = _type_value(db_path, context, "полтора часа")

    assert reply.effective_message.replies == [
        texts.settings_bad_value_text(texts.SETTINGS_FORMAT_DURATION)
    ]
    assert reply.effective_message.replies[0] == (
        "Не могу разобрать значение. Попробуйте формат `5:20` или `5ч20м`."
    )
    # Без изменений: ни settings, ни история; фаза живёт.
    assert SettingsService(conn).get(user["id"], "sleep_min_before_nap") == 320
    assert _history_rows(conn, user["id"]) == []
    assert context.user_data.get(WAIT_SETTING_VALUE) == "sleep_min_before_nap"

    retry = _type_value(db_path, context, "5:45")
    assert "обновлён" in retry.effective_message.replies[0]
    assert SettingsService(conn).get(user["id"], "sleep_min_before_nap") == 345
    conn.close()


# --------------------------------------------------- Отмена диалога (§10.4)


def test_tc_sett_022_cancel_clears_phase_without_changes(db_path):
    """TC-SETT-022: «Отменить» в диалоге §7.17 — фаза снята, настройка
    и история не изменены; cancel без активной фазы — молча без текста.

    Трассировка: ТЗ §10.4 (WAIT_SETTING_VALUE с отменой); design D21.
    """
    conn = init_db(db_path)
    user = get_user(conn, 4242)
    SettingsService(conn).seed_defaults(user["id"])
    _, context = _press(db_path, "settings:edit:merge_threshold")
    cancel, _ = _press(db_path, CALLBACK_SETTINGS_CANCEL, context=context)

    assert cancel.effective_message.replies == [texts.SETTINGS_CANCELLED]
    assert WAIT_SETTING_VALUE not in context.user_data
    assert SettingsService(conn).get(user["id"], "merge_threshold") == 20
    assert _history_rows(conn, user["id"]) == []

    # Cancel вне фазы — без ответа (нечего отменять).
    idle, idle_context = _press(db_path, CALLBACK_SETTINGS_CANCEL)
    assert idle.effective_message.replies == []
    assert idle_context.user_data == {}
    conn.close()


# --------------------------------- События не создаются; изоляция (FR-47)


def test_tc_sett_023_settings_paths_never_write_events(db_path):
    """TC-SETT-023: весь сценарий настроек (экран callback'ом/командой/
    панелью, диалог, мусорный ввод, отмена) НЕ пишет в sleep_events.

    Трассировка: Requirement «Экран настроек», Scenario «Открытие экрана
    из меню» («в sleep_events ничего не добавлено»); FR-47; design D21.
    """
    conn = init_db(db_path)
    get_user(conn, 4242)
    assert _event_count(conn) == 0

    _press(db_path, CALLBACK_SETTINGS_SHOW)
    _command(db_path)
    _panel_text(db_path)
    _, context = _press(db_path, "settings:edit:remind_before")
    _type_value(db_path, context, "не время")
    _type_value(db_path, context, "20м")
    _press(db_path, CALLBACK_SETTINGS_CANCEL)

    assert _event_count(conn) == 0
    conn.close()


# ---------------------------------------- Наблюдатель: read-only (design D13)


def test_tc_sett_024_observer_sees_screen_but_cannot_edit(db_path):
    """TC-SETT-024: наблюдатель ВИДИТ экран §7.16 (чтение), но правка
    недоступна: settings:edit и ввод значения — вежливый отказ, настройка
    и история не изменены, фаза не ставится/гасится.

    Трассировка: Requirement «Экран настроек» («редактирование — только
    владелец (parent), наблюдатель — чтение»); FR-47; design D13/D21.
    """
    conn = init_db(db_path)
    observer_tg = 5555
    user = get_user(conn, observer_tg)
    SettingsService(conn).seed_defaults(user["id"])
    conn.execute("UPDATE users SET role = 'observer' WHERE id = ?", (user["id"],))
    conn.commit()

    # Экран — доступен.
    seen, _ = _press(db_path, CALLBACK_SETTINGS_SHOW, telegram_id=observer_tg)
    assert texts.SETTINGS_SCREEN_HEADER in seen.effective_message.replies[0]

    # edit — вежливый отказ, фазы нет.
    denied, context = _press(db_path, "settings:edit:night_target", telegram_id=observer_tg)
    assert denied.effective_message.replies == [texts.OBSERVER_SETTINGS_DENIED]
    assert WAIT_SETTING_VALUE not in context.user_data

    # Ввод значения при фазе, поставленной до смены роли, — отказ, фаза
    # гасится, настройка не меняется (read-only на всём пути).
    context.user_data[WAIT_SETTING_VALUE] = "night_target"
    reply = _type_value(db_path, context, "23:00", telegram_id=observer_tg)
    assert reply.effective_message.replies == [texts.OBSERVER_SETTINGS_DENIED]
    assert SettingsService(conn).get(user["id"], "night_target") == "22:00"
    assert _history_rows(conn, user["id"]) == []
    conn.close()


# ------------------------------------------ FR-24: расчёт читает новое значение


def test_tc_sett_025_wake_deadline_edit_changes_next_window(db_path):
    """TC-SETT-025: успешная правка wake_deadline меняет окно следующего
    расчёта (FR-24 — без перезапуска), прямо через сервисы.

    Подъём 09:00 МСК (поздний §3.6): окно 14:20–15:00, прогноз конца сна
    15:50. При дефолтном крайнем 15:30 nap_window.after_deadline = True;
    после update wake_deadline → 16:30 — тот же расчёт даёт False.

    Трассировка: Requirement «Валидация значений и успех» («последующие
    расчёты MUST использовать новое значение без перезапуска»), Scenario
    «Успешная правка»; FR-24; design D21/D16.
    """
    conn = init_db(db_path)
    user = get_user(conn, 4242)
    SettingsService(conn).seed_defaults(user["id"])

    # Фиксированные события (наивные локальные МСК): ночь 02.11 22:00,
    # поздний подъём 03.11 09:00 (§3.6/§7.4), расчёт в 09:30 — «сейчас»
    # в расчёт не входит.
    events = [
        ("sleep", datetime(2026, 11, 2, 22, 0)),
        ("wake", datetime(2026, 11, 3, 9, 0)),
    ]
    now_local = datetime(2026, 11, 3, 9, 30)
    settings_map = SettingsService(conn).get_all(user["id"])

    before = nap_window(events, now_local, settings_map)
    assert before is not None
    assert before.end == datetime(2026, 11, 3, 15, 0)
    assert before.forecast_end == datetime(2026, 11, 3, 15, 50)
    assert before.after_deadline is True  # 15:50 позже крайних 15:30

    # Правка настройки через сервис (как из экрана §7.18).
    old, new = SettingsService(conn).update(user["id"], "wake_deadline", "16:30")
    assert (old, new) == ("15:30", "16:30")

    after = nap_window(events, now_local, SettingsService(conn).get_all(user["id"]))
    assert after is not None
    assert after.wake_deadline == datetime(2026, 11, 3, 16, 30)
    assert after.forecast_end == before.forecast_end
    assert after.after_deadline is False  # 15:50 раньше новых крайних 16:30
    conn.close()


# -------------------------------------------- Дословность §7.16/§7.19 (D6)


def test_tc_sett_026_texts_match_tz_verbatim():
    """TC-SETT-026: формулировки экрана и ошибки совпадают с ТЗ дословно
    (эталон — файл ТЗ, не texts.py): заголовок/подвал §7.16, строки
    параметров §7.16, строка §7.19.

    Трассировка: design D6 (тексты дословно, без перефразировок);
    Requirement «Валидация значений и успех», Scenario «Неразбираемый
    ввод».
    """
    spec = _SPEC_FILE.read_text(encoding="utf-8")

    def _tz_quote(section: str) -> str:
        start = spec.index(f"### {section}")
        body = spec[start : spec.index("###", start + 1)]
        return "\n".join(
            line[2:] for line in body.splitlines() if line.startswith("> ")
        )

    section_716 = _tz_quote("7.16 Настройки — главный экран")
    assert texts.SETTINGS_SCREEN_HEADER == section_716.splitlines()[0]
    assert texts.SETTINGS_SCREEN_FOOTER == section_716.splitlines()[-1]
    for literal in (
        "**Режим дня**",
        "**Ночной сон**",
        "**Бодрствование**",
        "**Прочее**",
        "- Минимум бодрствования до дневного сна: 5 ч 20 мин",
        "- Крайнее время пробуждения от дневного сна: 15:30",
        "- Целевое время ухода: 22:00",
        "- Верхняя граница: 12 ч 30 мин",
        "- Границы ночного сна: 19:00–07:00",
        "- Начало дня: 06:00",
    ):
        assert literal in section_716
        assert literal in settings_screen.screen_text(dict(DEFAULT_KEYS))

    section_719 = _tz_quote("7.19 Настройки — ошибка")
    assert texts.settings_bad_value_text(texts.SETTINGS_FORMAT_DURATION) == section_719

    section_717 = _tz_quote("7.17 Настройки — запрос нового значения")
    assert texts.settings_ask_value_text(
        "Минимум бодрствования до дневного сна", "5 ч 20 мин", texts.SETTINGS_FORMAT_DURATION
    ) == section_717

    section_718 = _tz_quote("7.18 Настройки — успех")
    assert texts.settings_updated_text(
        "Минимум бодрствования до дневного сна", "5 ч 20 мин", "5 ч 30 мин"
    ) == section_718
