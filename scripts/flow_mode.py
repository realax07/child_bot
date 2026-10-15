#!/usr/bin/env python3
"""flow_mode.py — режим детерминированного флоу (shadow / enforcing).

Решение Заказчика 2026-10-02: после APPROVE review-007 режим переключается
в enforcing — flowctl run / session_check reserve блокируют действие при
DENY/UNKNOWN. До этого весь пакет add-deterministic-flow работал в shadow.

Механика: файл ~/.hermes/state/flow_mode.json (или путь из FLOW_MODE_FILE)
вида {"mode": "shadow"|"enforcing", "since": iso, "by": "..."}.
Отсутствие файла = shadow (fail-soft: переключение — осознанное действие).

CLI:
  flow_mode.py show
  flow_mode.py set enforcing|shadow [--by <кто>]
Exit: 0 ок; 2 ошибка аргументов.

Компоненты читают через get_mode() — единую точку (без рассинхрона).
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MODE_FILE = Path.home() / ".hermes/state/flow_mode.json"
VALID_MODES = ("shadow", "enforcing")
# Статусы решений flow_transition, которые enforcing останавливает
# (shadow — только вычисляет, не исполняет).
BLOCKING_STATUSES = ("DENY", "UNKNOWN")


def mode_file() -> Path:
    return Path(os.environ.get("FLOW_MODE_FILE", str(DEFAULT_MODE_FILE)))


def get_mode() -> str:
    """Текущий режим; отсутствие/битость файла = shadow (fail-soft)."""
    try:
        data = json.loads(mode_file().read_text(encoding="utf-8"))
        mode = str(data.get("mode", "")).strip().lower()
        if mode in VALID_MODES:
            return mode
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return "shadow"


def set_mode(mode: str, by: str = "customer") -> dict:
    """Установить режим (атомарно, с записью кто/когда)."""
    mode = mode.strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"недопустимый режим: {mode!r} (разрешены: {', '.join(VALID_MODES)})")
    payload = {
        "mode": mode,
        "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "by": by,
    }
    target = mode_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return payload


def blocks_on(deny_or_unknown: bool) -> bool:
    """Должен ли компонент блокировать действие при DENY/UNKNOWN.

    shadow: никогда (решение вычисляется, но не исполняется).
    enforcing: да — DENY/UNKNOWN останавливают действие.
    """
    return get_mode() == "enforcing" and deny_or_unknown


def blocks_on_status(status: str) -> bool:
    """blocks_on для статуса решения flow_transition (ALLOW/DENY/UNKNOWN)."""
    return blocks_on(status in BLOCKING_STATUSES)


def mode_payload() -> dict:
    """Режим для вывода компонентов (прозрачность: видно, что активно)."""
    return {"mode": get_mode(), "mode_file": str(mode_file())}


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("show", "status"):
        mf = mode_file()
        exists = mf.exists()
        print(f"mode: {get_mode()}"
              + (f" (файл: {mf})" if exists else " (файла нет — дефолт shadow)"))
        return 0
    if args[0] == "set" and len(args) >= 2:
        by = "customer"
        if "--by" in args:
            i = args.index("--by")
            if i + 1 < len(args):
                by = args[i + 1]
        try:
            payload = set_mode(args[1], by)
        except ValueError as exc:
            print(f"FLOW-MODE-ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"mode: {payload['mode']} (с {payload['since']} UTC, by={payload['by']})")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
