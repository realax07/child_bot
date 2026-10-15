#!/usr/bin/env python3
"""Каркас handoff-документа (docs/handoffs/handoff-<дата>-<тема>.md).

Скилл: handoff. Создает скелет передачи контекста по контракту скилла
(Состояние / В полете / Решения / Открытые вопросы / Следующий шаг).
Документ append-only: скрипт только создает НОВЫЙ файл.

Usage:
    python handoff_scaffold.py "передача p16 после волны 2" --dir docs/handoffs
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

TEMPLATE = """# Handoff: {topic}

- **Дата:** {today}
- **Автор:** {author}
- **Статус:** активная передача (заполняется, потом — append-only)

## Состояние

<2–5 предложений: где проект/фабрика сейчас, последний артефакт/релиз>

## В полете

| # | Что | Где (файл/ветка/PR) | Что осталось |
|---|-----|---------------------|--------------|

## Решения (принятые, с датой)

- {today} — <решение> — источник (чат/файл)

## Открытые вопросы

1. <вопрос — кому адресован>

## Следующий шаг

<одна конкретная задача, с которой продолжить>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("topic", help="тема передачи (в имя файла: slug)")
    ap.add_argument("--dir", default="docs/handoffs")
    ap.add_argument("--author", default="ПМ")
    args = ap.parse_args()

    slug = re.sub(r"[^a-zа-я0-9]+", "-", args.topic.lower()).strip("-")[:60]
    target = Path(args.dir) / f"handoff-{date.today().isoformat()}-{slug}.md"
    if target.exists():
        print(f"ОТКАЗ: {target} существует — handoff append-only, "
              "дополняй существующий, не создавай дубликат")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        TEMPLATE.format(topic=args.topic, today=date.today().isoformat(),
                        author=args.author),
        encoding="utf-8",
    )
    print(f"Создан {target}; заполни разделы и продолжай работу от него.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
