# Регрессионные тесты BUG-001 (S2): backfill wake стирает запись о
# засыпании — перезапись вместо пары (S2-20261026-wake-overwrites-sleep).
#
# Root cause: detect_conflict считал ЛЮБОЙ wake при незакрытом sleep
# «пересечением» (незакрытый интервал тянется до момента расчёта) →
# §7.13 → «Изменить» перезаписывал sleep вейком. Требуемое поведение:
# wake ПОЗЖЕ начала незакрытого sleep — штатное закрытие пары (как
# кнопки «сейчас», §4.1), без §7.13; конфликты сохраняются: wake раньше
# начала незакрытого sleep, точное совпадение, пересечение с закрытой
# парой, двойной sleep.
#
# Правило rule 6: каждый тест несёт TC-BUG001-NNN в первой строке
# докстринга.

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import texts
from src.db import init_db
from src.handlers import backfill
from src.handlers.events import PENDING_KIND_KEY, WAIT_EVENT_TIME_KEY
from src.repositories.sleep_events import SleepEventsRepo
from src.services.event import get_user

TZ = ZoneInfo("Europe/Moscow")  # UTC+3, без перехода на летнее время


# ------------------------------------------------------------ Заглушки PTB
# (те же контракты, что в tests/test_conflicts.py — задача 4.4)


class _FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, reply, reply_markup=None):
        self.replies.append(reply)


class _FakeTextUpdate:
    def __init__(self, user_id: int, text: str) -> None:
        self.effective_user = type("U", (), {"id": user_id})()
        self.callback_query = None
        self.message = _FakeMessage(text)
        self.effective_message = self.message


class _FakeContext:
    def __init__(self, db_path) -> None:
        self.bot_data = {"db_path": db_path}
        self.user_data: dict = {}


# --------------------------------------------------------------- Помощники


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "bot.db"


@pytest.fixture()
def conn(db_path):
    c = init_db(db_path)
    yield c
    c.close()


def _past_local(hours_ago: float) -> datetime:
    """Момент в прошлом (локальное время пользователя, TZ = Москва)."""
    return (datetime.now(TZ) - timedelta(hours=hours_ago)).replace(
        second=0, microsecond=0
    )


def _input_text(local_dt: datetime) -> str:
    """Ввод парсеру в формате «DD.MM HH:MM»."""
    return f"{local_dt.day:02d}.{local_dt.month:02d} {local_dt:%H:%M}"


def _utc_str(local_dt: datetime) -> str:
    """Локальный момент → строка UTC БД «YYYY-MM-DD HH:MM:SS» (design D4)."""
    return local_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")


def _insert_event(conn, user_id: int, kind: str, local_dt: datetime) -> int:
    return SleepEventsRepo(conn).insert(user_id, kind, _utc_str(local_dt))


def _events(conn, user_id: int) -> list[dict]:
    return SleepEventsRepo(conn).list_by_user(user_id)


def _send_text(db_path, text: str, kind: str, telegram_id: int):
    user_data = {WAIT_EVENT_TIME_KEY: True, PENDING_KIND_KEY: kind}
    update = _FakeTextUpdate(telegram_id, text)
    context = _FakeContext(db_path)
    context.user_data = user_data
    asyncio.run(
        backfill.backfill_time_message(update, context)  # type: ignore[arg-type]
    )
    return update, context


# ------------------------------------------------------------------ Тесты


def test_tc_bug001_001_wake_closes_open_sleep_no_conflict(db_path, conn):
    """TC-BUG001-001 (BUG-001, сценарий Марии): sleep записан задним
    числом → ввод wake задним числом ПОЗЖЕ начала сна → §7.13 НЕТ,
    ОБА события в БД (wake с is_backfilled=1), сводка §7.14 в формате
    фактов продакта PB-3 (старого суммарного «Сон: …» нет; симптом
    BUG-001 — «Сон: 0 мин», «24 ч» — не воспроизводится)."""
    user = get_user(conn, 4101)
    sleep_at = _past_local(2)
    _insert_event(conn, user["id"], "sleep", sleep_at)
    wake_at = _past_local(1)

    update, context = _send_text(
        db_path, _input_text(wake_at), "wake", telegram_id=4101
    )

    # §7.13 НЕТ: один ответ — §7.14 (успех со сводкой), без конфликтного
    # текста и клавиатуры [Изменить]/[Отменить].
    assert len(update.effective_message.replies) == 1
    reply = update.effective_message.replies[0]
    assert "конфликтует" not in reply
    # Сводка §7.14 — факты продакта (PB-3): закрытая пара sleep+wake
    # видна строками «Заснул:/Проснулся:» (дневной интервал) либо
    # «Отбой:/Ночной сон:» (ночной, классификация зависит от времени
    # суток); суммарное бодрствование всегда выводится. Старого
    # формата «Сон: …» быть не должно.
    assert "Бодрствование общее: " in reply
    assert (
        "Заснул: " in reply
        and "Проснулся: " in reply
    ) or "Ночной сон: " in reply
    assert "Сон: " not in reply
    # Симптом BUG-001 не воспроизводится: сон не обнулён, бодрствование
    # не растянуто на сутки.
    assert "Сон: 0 мин" not in reply
    assert "24 ч" not in reply
    # ОБА события в БД: sleep + wake, wake — ввод задним числом.
    after = _events(conn, user["id"])
    assert [(ev["event_type"], ev["timestamp"]) for ev in after] == [
        ("sleep", _utc_str(sleep_at)),
        ("wake", _utc_str(wake_at)),
    ]
    assert after[1]["is_backfilled"] is True
    # Диалог завершён: фазы сняты, перезапись не назначалась.
    assert WAIT_EVENT_TIME_KEY not in context.user_data
    assert PENDING_KIND_KEY not in context.user_data
    assert backfill.CONFIRM_OVERWRITE_KEY not in context.user_data


def test_tc_bug001_002_wake_before_open_sleep_start_conflict(db_path, conn):
    """TC-BUG001-002 (BUG-001, конфликт сохранён): wake РАНЬШЕ начала
    незакрытого sleep с нарушением порядка (перед вводом — закрытая
    пара: sleep→wake→sleep, ввод wake даёт два wake подряд) → §7.13,
    цель — ПОСЛЕДНЕЕ нарушающее (D11); новое событие не создано.
    (wake раньше незакрытого sleep БЕЗ нарушения порядка — не конфликт:
    спека D11, TC-BFS-003.)"""
    user = get_user(conn, 4102)
    s1 = _past_local(5)
    w1 = _past_local(4)
    s2 = _past_local(1)  # незакрытый sleep
    _insert_event(conn, user["id"], "sleep", s1)
    w1_id = _insert_event(conn, user["id"], "wake", w1)
    _insert_event(conn, user["id"], "sleep", s2)
    wake_at = _past_local(3)  # раньше s2, после w1

    update, context = _send_text(
        db_path, _input_text(wake_at), "wake", telegram_id=4102
    )

    assert len(update.effective_message.replies) == 1
    assert texts.BACKFILL_CONFLICT.startswith("⚠️ Это время конфликтует")
    # Целевое событие — последнее нарушающее wake (D11).
    assert texts.KIND_LABELS["wake"] in update.effective_message.replies[0]
    assert context.user_data[backfill.CONFIRM_OVERWRITE_KEY] == {
        "event_id": w1_id,
        "when_utc": _utc_str(wake_at),
    }
    # Запись не создана: по-прежнему три события.
    assert len(_events(conn, user["id"])) == 3


def test_tc_bug001_003_double_sleep_still_conflict(db_path, conn):
    """TC-BUG001-003 (BUG-001, обратная сторона): ввод sleep при
    незакрытом sleep (двойное засыпание) → §7.13 как раньше; запись
    не создаётся."""
    user = get_user(conn, 4103)
    sleep_at = _past_local(2)
    sleep_id = _insert_event(conn, user["id"], "sleep", sleep_at)
    second_sleep_at = _past_local(1)

    update, context = _send_text(
        db_path, _input_text(second_sleep_at), "sleep", telegram_id=4103
    )

    assert len(update.effective_message.replies) == 1
    assert texts.BACKFILL_CONFLICT.startswith("⚠️ Это время конфликтует")
    assert context.user_data[backfill.CONFIRM_OVERWRITE_KEY] == {
        "event_id": sleep_id,
        "when_utc": _utc_str(second_sleep_at),
    }
    assert len(_events(conn, user["id"])) == 1


def test_tc_bug001_004_wake_inside_closed_pair_conflict(db_path, conn):
    """TC-BUG001-004 (BUG-001, конфликт сохранён): wake внутрь УЖЕ
    ЗАКРЫТОЙ пары (нарушение порядка, D11) → §7.13 как раньше; запись
    не создаётся."""
    user = get_user(conn, 4104)
    s1 = _past_local(5)
    w1 = _past_local(3)
    _insert_event(conn, user["id"], "sleep", s1)
    _insert_event(conn, user["id"], "wake", w1)
    inside = _past_local(4)  # между s1 и w1

    update, context = _send_text(
        db_path, _input_text(inside), "wake", telegram_id=4104
    )

    assert len(update.effective_message.replies) == 1
    assert texts.BACKFILL_CONFLICT.startswith("⚠️ Это время конфликтует")
    # D11: пересечение с закрытой парой — цель по прежним правилам.
    assert backfill.CONFIRM_OVERWRITE_KEY in context.user_data
    assert len(_events(conn, user["id"])) == 2
