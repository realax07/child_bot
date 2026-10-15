#!/usr/bin/env python3
"""session_archive.py — архивация завершенных делегаций (E16, слой Б «фактический»).

Копирует транскрипты завершенных сессий из live-кэша в постоянное хранилище
с метаданными и детерминированным анализом паттернов неуспеха (счетчики,
повторы, трейсы). Запускается ПМ при приемке делегации (или пакетом).

Usage:
  python3 scripts/session_archive.py                 # архивировать все завершенные live-сессии
  python3 scripts/session_archive.py --status        # только показать, что подлежит архивации

Хранилище: ~/.hermes/state/session-archive/<deleg_id>/
  - task-0.log            — полный транскрипт (копия)
  - meta.json             — роль/длительность/размер/исход
  - analysis.json         — детерминированный разбор паттернов неуспеха
"""
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

LIVE_DIR = Path.home() / ".hermes/cache/delegation/live"
ARCHIVE_DIR = Path.home() / ".hermes/state/session-archive"

# Паттерны неуспеха (детерминированный анализ, ярус 2)
PATTERNS = {
    "traceback": re.compile(r"Traceback \(most recent call last\)"),
    "connection_refused": re.compile(r"ERR_CONNECTION_REFUSED|Connection refused"),
    "module_not_found": re.compile(r"ModuleNotFoundError: No module named '([^']+)'"),
    "file_not_found": re.compile(r"(?:FileNotFoundError|No such file or directory):?.*?'([^']+)'"),
    "exit_nonzero": re.compile(r"exit[_ ]code[=: ]+(\d+)"),
}


def analyze(log_text: str) -> dict:
    """Детерминированный разбор: счетчики паттернов + топ повторов."""
    out = {"patterns": {}, "repeated_blocks": []}
    for name, rx in PATTERNS.items():
        hits = rx.findall(log_text)
        if hits:
            out["patterns"][name] = {"count": len(hits), "samples": list(dict.fromkeys(hits))[:5]}
    # Повторяющиеся строки-команды (вырожденные петли): строка >40 симв., встречается >2 раз
    counts = {}
    for line in log_text.splitlines():
        s = line.strip()
        if len(s) > 40:
            counts[s] = counts.get(s, 0) + 1
    loops = [(s, c) for s, c in counts.items() if c > 2]
    loops.sort(key=lambda x: -x[1])
    out["repeated_blocks"] = [{"line": s[:200], "count": c} for s, c in loops[:5]]
    out["verdict"] = (
        "degraded" if loops or out["patterns"].get("traceback", {}).get("count", 0) > 5
        else "suspicious" if out["patterns"]
        else "clean"
    )
    return out


def archive_session(sess: Path, dry: bool = False) -> dict:
    log_file = sess / "task-0.log"
    if not log_file.is_file():
        return {"id": sess.name, "skipped": "нет task-0.log"}
    text = log_file.read_text(encoding="utf-8", errors="replace")
    final = re.search(r"status=(\w+)", text)
    meta = {
        "deleg_id": sess.name,
        "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "size_bytes": log_file.stat().st_size,
        "sha256": hashlib.sha256(log_file.read_bytes()).hexdigest()[:16],
        "status": final.group(1) if final else "unknown",
    }
    analysis = analyze(text)
    if not dry:
        dst = ARCHIVE_DIR / sess.name
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(log_file, dst / "task-0.log")
        (dst / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        (dst / "analysis.json").write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"id": sess.name, "status": meta["status"], "verdict": analysis["verdict"],
            "patterns": list(analysis["patterns"].keys()), "archived": not dry}


def main() -> int:
    dry = "--status" in sys.argv
    if not LIVE_DIR.is_dir():
        print(f"Нет live-каталога: {LIVE_DIR}")
        return 0
    results = []
    for sess in sorted(LIVE_DIR.iterdir()):
        if sess.is_dir():
            results.append(archive_session(sess, dry=dry))
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    n_degraded = sum(1 for r in results if r.get("verdict") == "degraded")
    print(f"Итого: {len(results)} сессий, деградированных: {n_degraded}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
