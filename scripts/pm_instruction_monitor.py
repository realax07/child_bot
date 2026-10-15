#!/usr/bin/env python3
"""pm_instruction_monitor.py — change-detector для крона pm-instruction-relay.

Печатает содержимое /home/openclaw/.hermes/state/pm_instruction.txt ТОЛЬКО
когда файл изменился с прошлого прогона (self-dedup по хэшу в соседнем
state-файле). Пустой вывод (clean) → крон-агент не запускается вовсе.
Детерминирован: никаких временных меток в выводе.
"""
import hashlib
import sys
from pathlib import Path

INSTRUCTION = Path.home() / ".hermes" / "state" / "pm_instruction.txt"
SEEN = Path.home() / ".hermes" / "state" / "pm_instruction_monitor_seen.json"


def main() -> int:
    try:
        text = INSTRUCTION.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        text = ""
    digest = hashlib.sha256(text.encode()).hexdigest() if text else ""
    try:
        seen = SEEN.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        seen = ""
    if not text or digest == seen:
        print("", end="")  # clean — крон-агент не будится
        return 0
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(digest, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
