"""Callback «⏰ Подсказка сейчас» (status:show) — add-recommendations 2.2.

Handler hint.py (design D17, ТЗ §10.3/§4.6): мгновенный ответ из
RecommendationService без записи в sleep_events (FR-32), структура §7.6
бодрствующему без времени «сейчас» и слова состояния (FR-32), статус
спящему §7.7 без «пора укладывать» (FR-33), неполные данные без «0 ч»
(FR-34), observer — подсказка доступна (чтение, design D13), guard D5 —
состояние перечитывается из БД на каждое нажатие, «устаревшая» кнопка
ничего не пишет.

Каждый тест помечен TC-HINT-xxx в первой строке докстринга (rule 6).
Время фиксировано (frozen_now, monkeypatch hint.datetime): 09:30 UTC =
12:30 МСК — ассерты без «живых» ЧЧ:ММ. Тексты ответа — дословные
§7.6–7.9 из src/texts.py (задача 2.3): рендер подставляет цифры сервиса
в формулировки ТЗ; дословность эталону ТЗ фиксирует TC-HINT-009.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import texts
from src.db import init_db
from src.handlers import common, hint
from src.handlers.hint import CALLBACK_STATUS_SHOW
from src.repositories.sleep_events import SleepEventsRepo
from src.repositories.users import UsersRepo
from src.services.state import BTN_HINT

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

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup


class _FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.answers: list[str | None] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text=None, reply_markup=None):
        self.message.text = text
        self.message.reply_markup = reply_markup

    async def edit_message_reply_markup(self, reply_markup=None):
        await self.message.edit_reply_markup(reply_markup=reply_markup)


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeUpdate:
    def __init__(self, user_id: int, data: str) -> None:
        self.effective_user = _FakeUser(user_id)
        self.callback_query = _FakeCallbackQuery(data)
        self.effective_message = self.callback_query.message


class _FakeTextUpdate:
    """Текст с reply-панели (2.2-fix): callback_query нет, есть message.text."""

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

# Фиксированные «сейчас» обработчика (Европа/Москва = UTC+3):
# 09:30 UTC = 12:30 МСК (бодрствующий), 11:15 UTC = 14:15 МСК (спящий),
# 12:10 UTC = 15:10 МСК (сон перерос цель).
FIXED_NOW_UTC = datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc)


def _frozen(moment_utc: datetime) -> type:
    """Класс datetime с замороженным now() на момент moment_utc."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is not None:
                return moment_utc.astimezone(tz)
            return moment_utc.replace(tzinfo=None)

    return _FrozenDatetime


@pytest.fixture()
def frozen_now(monkeypatch):
    """Фиксировать «сейчас» обработчика hint (правило фиксированного времени)."""
    monkeypatch.setattr(hint, "datetime", _frozen(FIXED_NOW_UTC))


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "bot.db"


@pytest.fixture()
def conn(db_path):
    c = init_db(db_path)
    yield c
    c.close()


# Ночь 02.11 22:00 МСК → подъём 03.11 08:00 МСК — стандартный подъём §3.5.
RISE_EVENTS_UTC = (
    ("2026-11-02 19:00:00", "sleep"),
    ("2026-11-03 05:00:00", "wake"),
)


def _press(db_path, data: str = CALLBACK_STATUS_SHOW, telegram_id: int = 1001):
    """Провести нажатие «⏰ Подсказка сейчас» через реальный обработчик."""
    update = _FakeUpdate(telegram_id, data)
    context = _FakeContext(db_path)
    asyncio.run(hint.hint_callback(update, context))  # type: ignore[arg-type]
    return update, context


def _press_text(db_path, telegram_id: int = 1001):
    """Провести ТЕКСТ «⏰ Подсказка сейчас» с reply-панели через общий вход
    reply_button_message (2.2-fix): тот же маршрут, что у кнопок событий."""
    update = _FakeTextUpdate(telegram_id, BTN_HINT)
    context = _FakeContext(db_path)
    asyncio.run(
        common.reply_button_message(update, context)  # type: ignore[arg-type]
    )
    return update, context


def _uid(conn, telegram_id: int = 1001) -> int:
    return UsersRepo(conn).get_or_create(telegram_id)["id"]


def _seed_rise(conn, user_id: int) -> None:
    for ts, kind in RISE_EVENTS_UTC:
        SleepEventsRepo(conn).insert(user_id, kind, ts)


def _sleep_count(conn, user_id: int) -> int:
    return len(SleepEventsRepo(conn).list_by_user(user_id))


# ------------------------------------------------------------------- Тесты

def test_tc_hint_001_awake_answer_7_6_structure_no_time_no_state(
    db_path, conn, frozen_now
):
    """TC-HINT-001 (FR-32, ТЗ §10.3/§4.6): нажатие при бодрствующем —
    мгновенный ответ по структуре §7.6 (бодрствование 4 ч 30 мин,
    суммарно за сегодня, остатки до границ 12/12,5 ч, окно дневного сна
    13:20–14:00) и НОЛЬ записей в sleep_events (событие не создаётся).
    Времени «сейчас» (12:30) и слова состояния («бодрствует») в тексте
    нет; предупреждений нет (совместимые якоря)."""
    user_id = _uid(conn)
    _seed_rise(conn, user_id)

    update, _ = _press(db_path)

    assert _sleep_count(conn, user_id) == 2  # запись НЕ создана
    assert update.callback_query.answers == [None]  # мгновенный ответ
    assert len(update.effective_message.replies) == 1
    answer = update.effective_message.replies[0]
    # Структура §7.6: ключевая цифра + за день, позиция к диапазону, окно.
    assert answer.splitlines()[0] == (
        "Бодрствование с последнего пробуждения: 4 ч 30 мин. "
        "Суммарно за сегодня: 4 ч 30 мин."
    )
    assert (
        "До нижней границы (12 ч) осталось: 7 ч 30 мин. "
        "До верхней границы (12 ч 30 мин) осталось: 8 ч."
    ) in answer
    assert "Окно дневного сна: 13:20–14:00." in answer
    # ДОСЛОВНО §7.6: при стандартном подъёме 08:00 (запрос 12:30, позиция
    # «ниже», предупреждений нет) ответ совпадает с эталонной цитатой ТЗ
    # побайтно (задача 2.3, AC-6; числа ТЗ совпали с фактическими).
    assert answer == texts.HINT_AWAKE
    # Время «сейчас» и слово состояния не выводятся (ТЗ §4.6).
    assert "12:30" not in answer
    assert "бодрствует" not in answer
    assert "⚠️" not in answer


def test_tc_hint_002_asleep_answer_7_7_no_put_to_sleep_advice(
    db_path, conn, monkeypatch
):
    """TC-HINT-002 (FR-33, §7.7): подсказка спящему — сон с 13:30 МСК
    (45 мин), ожидаемое пробуждение около 15:00 (цель 90 мин из
    настроек), прогноз бодрствования после 5 ч 30 мин; совета «пора
    укладывать» НЕТ, события не пишутся, времени «сейчас» (14:15) нет."""
    monkeypatch.setattr(hint, "datetime", _frozen(
        datetime(2026, 11, 3, 11, 15, tzinfo=timezone.utc)  # 14:15 МСК
    ))
    user_id = _uid(conn)
    _seed_rise(conn, user_id)
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-11-03 10:30:00")  # 13:30 МСК

    update, _ = _press(db_path)

    assert _sleep_count(conn, user_id) == 3  # запись НЕ создана
    assert len(update.effective_message.replies) == 1
    answer = update.effective_message.replies[0]
    # §7.7 дословно (задача 2.3): факт и прогноз — ОДНОЙ строкой, как в ТЗ.
    assert answer.splitlines()[0] == (
        "Сон идёт с 13:30 (уже 45 мин). Ожидаемое пробуждение: около 15:00."
    )
    assert (
        "После пробуждения суммарное бодрствование составит около 5 ч 30 мин."
    ) in answer
    assert "укладывать" not in answer
    assert "14:15" not in answer


def test_tc_hint_003_empty_day_asks_for_events_no_zero_hours(
    db_path, conn, frozen_now
):
    """TC-HINT-003 (FR-34, §4.6 «Неполные данные»): пустая страница дня —
    просьба отметить события («☀️ Проснулся» / задним числом); «0 ч» и
    окна НЕ выводятся, событий в БД не появляется."""
    user_id = _uid(conn)

    update, _ = _press(db_path)

    assert _sleep_count(conn, user_id) == 0
    assert len(update.effective_message.replies) == 1
    answer = update.effective_message.replies[0]
    assert "Проснулся" in answer
    assert "задним числом" in answer
    assert "0 ч" not in answer
    assert "Окно" not in answer


def test_tc_hint_004_observer_gets_read_only_answer(db_path, conn, frozen_now):
    """TC-HINT-004 (design D13, пакет 1): наблюдателю подсказка ДОСТУПНА
    (чтение без ограничений) — тот же структурный ответ, denying-сообщения
    нет, записи в sleep_events не появляются."""
    user_id = _uid(conn)
    _seed_rise(conn, user_id)
    conn.execute("UPDATE users SET role='observer' WHERE telegram_id=1001")
    conn.commit()

    update, _ = _press(db_path)

    assert _sleep_count(conn, user_id) == 2
    assert len(update.effective_message.replies) == 1
    answer = update.effective_message.replies[0]
    assert "Окно дневного сна: 13:20–14:00." in answer
    assert "недоступны" not in answer  # OBSERVER_DENIED не выдаётся


def test_tc_hint_005_stale_button_rereads_state_writes_nothing(
    db_path, conn, monkeypatch
):
    """TC-HINT-005 (design D5, guard по образцу событий): кнопка из
    старого сообщения при бодрствующем; пока сообщение висело в чате,
    ребёнок уснул (событие записано другим путём) — нажатие перечитывает
    состояние из БД и отвечает по СВЕЖЕМУ статусу спящего; ничего
    лишнего не пишется (записей ровно 3 — только внешние отметки)."""
    monkeypatch.setattr(hint, "datetime", _frozen(FIXED_NOW_UTC))  # 12:30 МСК
    user_id = _uid(conn)
    _seed_rise(conn, user_id)

    first, _ = _press(db_path)
    assert "Окно дневного сна" in first.effective_message.replies[0]

    # Состояние изменилось после отрисовки кнопки: сон 13:30 МСК не закрыт.
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-11-03 10:30:00")

    monkeypatch.setattr(hint, "datetime", _frozen(
        datetime(2026, 11, 3, 11, 15, tzinfo=timezone.utc)  # 14:15 МСК
    ))
    second, _ = _press(db_path)
    answer = second.effective_message.replies[0]
    assert answer.splitlines()[0] == (
        "Сон идёт с 13:30 (уже 45 мин). Ожидаемое пробуждение: около 15:00."
    )
    assert _sleep_count(conn, user_id) == 3  # нажатия ничего не добавили


def test_tc_hint_006_over_target_soft_mark_no_forecast_from_past(
    db_path, conn, monkeypatch
):
    """TC-HINT-006 (FR-37, ОВ-4.1): незавершённый дневной сон 1 ч 40 мин
    при цели 90 мин — мягкая отметка «дольше обычного» с цифрами; прогноз
    пробуждения (15:00) уже в прошлом — строки «Ожидаемое пробуждение»
    нет; пуша нет (обработчик ничего не шлёт, кроме ответа на нажатие)."""
    user_id = _uid(conn)
    _seed_rise(conn, user_id)
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-11-03 10:30:00")

    monkeypatch.setattr(hint, "datetime", _frozen(
        datetime(2026, 11, 3, 12, 10, tzinfo=timezone.utc)  # 15:10 МСК
    ))
    update, _ = _press(db_path)

    answer = update.effective_message.replies[0]
    assert (
        "Сон идёт дольше обычного: 1 ч 40 мин при обычной цели 1 ч 30 мин."
    ) in answer
    assert "Ожидаемое пробуждение" not in answer
    assert _sleep_count(conn, user_id) == 3


def test_tc_hint_007_text_button_same_answer_as_callback_no_event(
    db_path, conn, frozen_now
):
    """TC-HINT-007 (ТЗ §6, §10.3 двойной вход, FR-32): текст «⏰ Подсказка
    сейчас» с reply-панели (2.2-fix) — ТОТ ЖЕ ответ, что у callback
    status:show (структура §7.6); событие НЕ создаётся (0 новых записей);
    отдельного текста-ошибки нет (кнопка валидна бодрствующему)."""
    user_id = _uid(conn)
    _seed_rise(conn, user_id)

    text_update, _ = _press_text(db_path)
    callback_update, _ = _press(db_path)

    assert len(text_update.effective_message.replies) == 1
    assert (
        text_update.effective_message.replies[0]
        == callback_update.effective_message.replies[0]
    )
    assert "Окно дневного сна: 13:20–14:00." in text_update.effective_message.replies[0]
    assert _sleep_count(conn, user_id) == 2  # событие НЕ создано


def test_tc_hint_008_text_button_asleep_rereads_state_no_event(
    db_path, conn, monkeypatch
):
    """TC-HINT-008 (ТЗ §6 «в ОБОИХ состояниях», FR-33, design D5): текст
    «⏰ Подсказка сейчас» с панели спящему — ответ §7.7, идентичный
    callback; состояние перечитывается из БД (не по фазе клавиатуры);
    записей в sleep_events не добавляется."""
    monkeypatch.setattr(hint, "datetime", _frozen(
        datetime(2026, 11, 3, 11, 15, tzinfo=timezone.utc)  # 14:15 МСК
    ))
    user_id = _uid(conn)
    _seed_rise(conn, user_id)
    SleepEventsRepo(conn).insert(user_id, "sleep", "2026-11-03 10:30:00")  # 13:30 МСК

    text_update, _ = _press_text(db_path)
    callback_update, _ = _press(db_path)

    answer = text_update.effective_message.replies[0]
    assert answer.splitlines()[0] == (
        "Сон идёт с 13:30 (уже 45 мин). Ожидаемое пробуждение: около 15:00."
    )
    assert text_update.effective_message.replies == (
        callback_update.effective_message.replies
    )
    assert _sleep_count(conn, user_id) == 3  # событие НЕ создано


# ------------------------------------------------- Задача 2.3: §7.6–7.9 дословно


def _spec_section(section: str) -> str:
    """Дословный текст раздела ТЗ §7.x (blockquote после заголовка, AC-6).

    Эталон читается из файла ТЗ, а не копируется в тест: сверка не может
    «поехать» вместе с texts.py (паттерн TC-SMOKE-002).
    """
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


def test_tc_hint_009_texts_7_6_7_9_match_spec_verbatim():
    """TC-HINT-009 (задача 2.3, AC-6): дословные тексты §7.6–7.9 —
    эталонные константы texts.py равны цитатам ФАЙЛА ТЗ, а параметризо-
    ванные шаблоны рендера с числами примера ТЗ воспроизводят те же
    цитаты посимвольно (формулировки не «поехали» при параметризации)."""
    assert texts.HINT_AWAKE == _spec_section("7.6")
    assert texts.HINT_SLEEPING == _spec_section("7.7")
    assert texts.LATE_NAP_WARNING == _spec_section("7.8")
    assert texts.OVERTIRED_WARNING == _spec_section("7.9")

    # Рендер §7.6: числа примера ТЗ → дословная цитата (позиция «ниже»,
    # дневное окно, без предупреждений).
    assert texts.hint_awake_text(
        "4 ч 30 мин",
        "4 ч 30 мин",
        texts.hint_position_below_line(
            "12 ч", "7 ч 30 мин", "12 ч 30 мин", "8 ч"
        ),
        texts.hint_nap_window_line("13:20", "14:00"),
    ) == _spec_section("7.6")

    # Рендер §7.7: факт + прогноз одной строкой, прогноз бодрствования —
    # второй (числа примера ТЗ).
    assert texts.hint_sleeping_text(
        "13:30", "45 мин", "15:00", "10 ч 30 мин"
    ) == _spec_section("7.7")

    # Рендер §7.8 / §7.9 с числами примера ТЗ.
    assert texts.late_nap_warning_text(
        "14:20", "15:50", "15:30"
    ) == _spec_section("7.8")
    assert texts.overtired_warning_text("12 ч 40 мин") == _spec_section("7.9")


def test_tc_hint_010_late_nap_warning_7_8_verbatim(db_path, conn, monkeypatch):
    """TC-HINT-010 (FR-35, §7.8 дословно, задача 2.3): поздний подъём 09:00
    МСК (окно 14:20–15:00, прогноз конца 15:50 позже 15:30) — ответ
    бодрствующему содержит предупреждение §7.8 ПОСИМВОЛЬНО как в ТЗ, с
    фактическими цифрами расчёта (не хардкод). Запрос в 12:30 МСК."""
    monkeypatch.setattr(hint, "datetime", _frozen(FIXED_NOW_UTC))  # 12:30 МСК
    user_id = _uid(conn)
    # Ночь 02.11 22:30 МСК → подъём 03.11 09:00 МСК — поздний подъём §3.6.
    for ts, kind in (
        ("2026-11-02 19:30:00", "sleep"),
        ("2026-11-03 06:00:00", "wake"),
    ):
        SleepEventsRepo(conn).insert(user_id, kind, ts)

    update, _ = _press(db_path)

    answer = update.effective_message.replies[0]
    # Дословная формулировка §7.8 с фактическими цифрами расчёта
    # (шаблон = эталонная цитата ТЗ, TC-HINT-009; числа — из расчёта).
    assert texts.late_nap_warning_text("14:20", "15:50", "15:30") in answer
    assert (
        "⚠️ Если сон начнётся в 14:20, он закончится около 15:50 — "
        "позже крайнего времени 15:30. Ночной сон может сдвинуться."
    ) in answer
    assert "Окно дневного сна: 14:20–15:00." in answer


def test_tc_hint_011_overtired_warning_7_9_verbatim(db_path, conn, monkeypatch):
    """TC-HINT-011 (FR-36, §7.9 дословно, задача 2.3): суммарное
    бодрствование 13 ч (подъём 08:00 МСК, запрос в 21:00 МСК) превысило
    верхнюю границу 12,5 ч — ответ содержит предупреждение §7.9
    ПОСИМВОЛЬНО как в ТЗ с фактической цифрой; окно следующего сна —
    ночное (запрос после конца дневного окна)."""
    monkeypatch.setattr(hint, "datetime", _frozen(
        datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)  # 21:00 МСК
    ))
    user_id = _uid(conn)
    _seed_rise(conn, user_id)  # ночь 02.11 22:00 → подъём 03.11 08:00 МСК

    update, _ = _press(db_path)

    answer = update.effective_message.replies[0]
    # Дословная формулировка §7.9 с фактической цифрой (13 ч > 12,5 ч).
    assert texts.overtired_warning_text("13 ч") in answer
    assert (
        "⚠️ Бодрствование 13 ч — превышена верхняя граница. "
        "Постарайтесь уложить ребёнка как можно скорее."
    ) in answer
    assert "Окно ухода в ночь: около 22:00 (крайнее 22:30)." in answer
