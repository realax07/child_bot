#!/usr/bin/env python3
"""Каркас change-пакета (openspec/changes/<change>/) по контракту фабрики.

Скилл: openspec-authoring. Создает proposal.md / design.md / tasks.md /
research.md (опционально) с обязательными шапками и заглушками разделов.
НЕ создает спеку домена и не заменяет работу СА — только скелет файлов.

Usage:
    python package_scaffold.py add-my-feature --flow 1
    python package_scaffold.py fix-tree-overflow --flow 2 --dir openspec/changes
"""
import argparse
import sys
from datetime import date
from pathlib import Path

HEADER = """# {title}

- **Change:** {change}
- **Флоу:** {flow}
- **Дата:** {today}
- **Автор:** {author}

> Каркас создан package_scaffold.py; разделы заполняет {owner} по контракту
> openspec-authoring. Пустые разделы НЕ удалять — пометить «N/A с основанием».
"""

PROPOSAL = """
## Зачем (проблема)

<пожелания Заказчика дословно или ссылка на источник>

## Что делаем (решение одной фразой)

<...>

## Альтернативы (минимум 2, с trade-off)

1. <...> — trade-off: <...>
2. <...> — trade-off: <...>

## Границы (anti-scope)

- <что сознательно НЕ делаем>
"""

DESIGN = """
## Решения

| # | Решение | Основание | Альтернатива |
|---|---------|-----------|--------------|
| D1 | | | |

## Затрагиваемое

- Модули/файлы: <...>
- Миграции: <есть/нет; обратимость — обязательное поле>
- Сквозные процессы: <затронуты/нет>

## Допущения

- <каждое явно: «проверено на проде» / «не проверено»>
"""

TASKS = """
## Задачи

| # | Задача | Зона (файлы/модуль) | Роль | Зависимости |
|---|--------|---------------------|------|-------------|
| T1 | | | dev | — |

## Порядок

<последовательность с учетом зависимостей; QA после dev по каждому шву>

## Критерии готовности

- [ ] <из спеки, трассируются к FR/NFR>
"""

RESEARCH = """
## Вопросы

| # | Вопрос | Ответ | Источник/проверка |
|---|--------|-------|-------------------|

## Trade-offs

<для каждого «сервис?», «библиотека?», «схема?» — критерии и решение>
"""

FILES = {
    "proposal.md": (PROPOSAL, "Заказчик+ПМ", "проблема/решение/альтернативы"),
    "design.md": (DESIGN, "СА", "решения/затрагиваемое/допущения"),
    "tasks.md": (TASKS, "СА+ПМ", "нарезка и порядок"),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("change", help="идентификатор change (add-my-feature)")
    ap.add_argument("--flow", type=int, default=1, choices=range(1, 6))
    ap.add_argument("--dir", default="openspec/changes")
    ap.add_argument("--research", action="store_true",
                    help="создать также research.md (когда было системное исследование)")
    ap.add_argument("--author", default="ПМ")
    args = ap.parse_args()

    target = Path(args.dir) / args.change
    if target.exists() and any(target.iterdir()):
        print(f"ОТКАЗ: {target} существует и не пуст — каркас не перезаписывает")
        return 1
    target.mkdir(parents=True, exist_ok=True)
    made = []
    for name, (body, owner, _) in FILES.items():
        if name == "design.md" and args.flow in (3, 5):
            continue  # хотфикс/экспресс: design не обязателен
        p = target / name
        p.write_text(  # type: ignore[union-attr]
            HEADER.format(title=name.replace(".md", "").capitalize(), change=args.change,
                          flow=args.flow, today=date.today().isoformat(),
                          author=args.author, owner=owner) + body,
            encoding="utf-8",
        )
        made.append(p.name)
    if args.research:
        p = target / "research.md"
        p.write_text(HEADER.format(title="Research", change=args.change, flow=args.flow,
                                   today=date.today().isoformat(), author=args.author,
                                   owner="СА/БА") + RESEARCH, encoding="utf-8")
        made.append("research.md")
    print(f"Создано в {target}: {', '.join(made)}")
    print("Дальше: заполнить разделы по контракту openspec-authoring; "
          "PLAN-<change>.md в корне — до старта флоу (J41).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
