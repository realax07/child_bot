#!/usr/bin/env python3
"""session_watchdog.py — вотчдог роутинга Telegram-чата Заказчика (J19).

Проблема: ключ роутинга agent:main:telegram:dm:<chat> может указывать на
ended-сессию в state.db (после /stop + session_reset делегации или фейловера) —
Заказчик «проваливается» в зомби-сабагента, heal срабатывает только на inbound.

Вотчдог (cron, --no-agent): каждую минуту читает state.db:
  - для каждого routing_key вида agent:main:telegram:dm:* сверяет entry.session_id
    с sessions.ended_at;
  - если сессия ended и это НЕ осознанный сброс — удаляет залипшую запись роутинга
    (и из gateway_routing, и из sessions.json): следующее сообщение Заказчика
    создаст свежую сессию, ответ придет от главной сессии (ассистента);
  - доставка отчетов делегаций при этом не страдает: результат delegation
    остается в записях делегации, ворота no_zombie_holds при живом ключе не срабатывают.

Exit 1 = было лечение (для крона с failure-deliver). Тихий выход 0 = чисто.

Запуск: hermes cron create '*/2 * * * *' --no-agent --script session_watchdog.py
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

STATE_DB = Path.home() / ".hermes/state.db"
SESSIONS_JSON = Path.home() / ".hermes/sessions/sessions.json"
DM_PREFIX = "agent:main:telegram:dm:"
DRY_RUN = "--dry-run" in sys.argv


def heal() -> list[str]:
    con = sqlite3.connect(
        f"file:{STATE_DB}?mode=rw", timeout=10, uri=True
    )
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    actions: list[str] = []

    # 1) залипшие ключи роутинга: routing entry -> ended-сессия
    stale_keys: list[tuple[str, str, str]] = []  # (routing_key, session_id, ended_at)
    for row in cur.execute(
        "SELECT session_key, entry_json FROM gateway_routing WHERE session_key LIKE ?",
        (DM_PREFIX + "%",),
    ):
        try:
            entry = json.loads(row["entry_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        sid = entry.get("session_id", "")
        if not sid:
            continue
        ended = cur.execute(
            "SELECT ended_at FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        if ended and ended["ended_at"]:
            stale_keys.append((row["session_key"], sid, str(ended["ended_at"])))

    for key, sid, ended_at in stale_keys:
        actions.append(
            f"heal: routing '{key}' указывал на ended-сессию {sid} "
            f"(ended_at={ended_at}) — запись удалена, следующий inbound создаст свежую сессию"
        )
        if not DRY_RUN:
            cur.execute(
                "DELETE FROM gateway_routing WHERE session_key = ?", (key,)
            )

    if not DRY_RUN and actions:
        con.commit()

    # 2) живые, но незавершенные holds делегаций (информативно, не лечим)
    try:
        rows = list(
            cur.execute(
                "SELECT delegation_id, routing_key, state, updated_at "
                "FROM async_delegations WHERE state NOT IN ('delivered','failed','cancelled')"
            )
        )
        for r in rows:
            age_min = (time.time() - float(r["updated_at"] or 0)) / 60
            if age_min > 120:
                actions.append(
                    f"warn: hold {r['delegation_id']} ({r['routing_key']}, state={r['state']}) "
                    f"висит {age_min:.0f} мин"
                )
    except sqlite3.OperationalError:
        pass  # таблица/колонка отсутствует в этой версии — не критично

    # 2.5) тайтл главной сессии: активная telegram-DM сессия всегда должна
    #      называться 'main' (Заказчик находит её через /resume main).
    #      Автотйтл перезаписывает только безымянные, но ротации/баги могли
    #      стереть имя; тихо возвращаем, если сняли не мы (пустое или другое).
    main_rows = list(
        cur.execute(
            """SELECT r.session_key key, s.id id FROM gateway_routing r
               JOIN sessions s ON s.id = json_extract(r.entry_json, '$.session_id')
               WHERE r.session_key LIKE ?""",
            (DM_PREFIX + "%",),
        )
    )
    # «main» уникален: держим его только у самой свежей активной DM-сессии,
    # прочим активным возвращаем тайтл-заглушку, если collisions.
    if main_rows:
        active = max(main_rows, key=lambda r: str(r["id"]))
        # title UNIQUE (частичный индекс): «main» может застрять у мёртвой
        # старой сессии — сперва освобождаем, иначе UPDATE падает по
        # UNIQUE constraint и вотчдог умирает со streak'ом ошибок.
        holders = [
            h[0]
            for h in cur.execute(
                "SELECT id FROM sessions WHERE title = 'main' AND id != ?",
                (active["id"],),
            )
        ]
        for hid in holders:
            actions.append(f"heal: тайтл 'main' освобождён у устаревшей сессии {hid}")
            if not DRY_RUN:
                cur.execute(
                    "UPDATE sessions SET title = NULL, title_source = NULL WHERE id = ?",
                    (hid,),
                )
        for r in main_rows:
            t_row = cur.execute("SELECT title FROM sessions WHERE id = ?", (r["id"],)).fetchone()
            title = (t_row["title"] or "") if t_row else ""
            want = "main" if r["key"] == active["key"] else None
            if want and title != want:
                actions.append(f"heal: тайтл активной DM-сессии {r['id']} = '{title or '<пусто>'}' → 'main'")
                if not DRY_RUN:
                    cur.execute("UPDATE sessions SET title = 'main' WHERE id = ?", (r["id"],))

    # 2.75) незакрытые делегации: result готов, но delivery_state='dropped'
    #       (провал на границе сессии) — информативно для Заказчика.
    #       Каждый id предупреждаем один раз (state-файл), чтобы не спамить каждые 2 мин.
    _SEEN = Path.home() / ".hermes/state/watchdog_dropped_seen.json"
    try:
        seen: set[str] = set(json.loads(_SEEN.read_text()) if _SEEN.exists() else [])
    except (OSError, json.JSONDecodeError):
        seen = set()
    try:
        dropped = list(
            cur.execute(
                """SELECT delegation_id, parent_session_id, datetime(completed_at,'unixepoch') c
                   FROM async_delegations
                   WHERE delivery_state='dropped' AND state='completed'
                     AND completed_at > ?""",
                (time.time() - 86400,),
            )
        )
        fresh = [r for r in dropped if r["delegation_id"] not in seen]
        for r in fresh:
            actions.append(
                f"warn: результат делегации {r['delegation_id']} НЕ доставлен "
                f"(dropped, завершена {r['c']} UTC, родитель {r['parent_session_id']}) — "
                "читать: sqlite3 ~/.hermes/state.db \"SELECT result_json FROM async_delegations WHERE delegation_id='...'"
            )
        if fresh and not DRY_RUN:
            seen |= {r["delegation_id"] for r in fresh}
            _SEEN.parent.mkdir(parents=True, exist_ok=True)
            _SEEN.write_text(json.dumps(sorted(seen)))
    except sqlite3.OperationalError:
        pass

    # 2.8) субагент-контаминация main-DM (upstream #92859): delegate_task,
    #      диспатченный ровно на границе ротации сессии, не создает
    #      изолированный сабагент — роль сабагента (_delegate_from в
    #      model_config) въедается в НОВУЮ chat-сессию следующего звена
    #      main-цепочки. Сессия легально жива и роутится (старые проверки
    #      1/2.5 её не видят), Заказчик «проваливается в сабагента».
    #      Лечение — только пересоздание сессии; детектор дает ранний сигнал.
    try:
        contaminated = list(
            cur.execute(
                """SELECT r.session_key key, s.id id,
                          json_extract(s.model_config, '$._delegate_from') df
                   FROM gateway_routing r
                   JOIN sessions s ON s.id = json_extract(r.entry_json, '$.session_id')
                   WHERE r.session_key LIKE ? AND s.ended_at IS NULL
                     AND json_extract(s.model_config, '$._delegate_from') IS NOT NULL""",
                (DM_PREFIX + "%",),
            )
        )
        for r in contaminated:
            actions.append(
                f"heal-warn: main-DM сессия {r['id']} ({r['key']}) создана делегацией "
                f"(из {r['df']}) — субагент-контаминация (upstream #92859); "
                "пересоздать сессию (/reset или новая), работу в ней не продолжать"
            )
    except sqlite3.OperationalError:
        pass  # колонка model_config отсутствует в старых версиях — не критично

    if not DRY_RUN and actions:
        con.commit()

    # 3) sessions.json: сессии telegram, отсутствующие в state.db
    try:
        data = json.loads(SESSIONS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    ghost = [
        k
        for k, v in data.items()
        if k.startswith(DM_PREFIX)
        and not cur.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (v.get("session_id", ""),)
        ).fetchone()
    ]
    if ghost:
        actions.append(f"heal: ghost-записи sessions.json (нет в state.db): {ghost}")
        if not DRY_RUN:
            for k in ghost:
                data.pop(k, None)
            SESSIONS_JSON.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    con.close()
    return actions


def main() -> int:
    if not STATE_DB.exists():
        print(f"no state.db at {STATE_DB}")
        return 0
    actions = heal()
    if actions:
        print("\n".join(actions))
        return 1 if not DRY_RUN else 0
    print("ok: роутинг чист, зомби-holds нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
