<!-- Reviewer-Delegation: deleg_671f57eea4bb420c -->

# Code Review: задача 1.1 (add-core-diary) — review-001

- **Ревьюер:** независимый код-ревьюер (делегация deleg_671f57eea4bb420c)
- **Дата:** 2026-10-26
- **Ревьюемый код:** коммит `fa81a55`, ветка `feature/add-core-diary-1.1` (worktree `/home/openclaw/child_bot-worktrees/deleg-671f57eea4bb420c`); дифф `2c3a89a..fa81a55`
- **Артефакты:** tasks.md (1.1), design.md (D1, D2, D4), requirements.md (FR-1), ТЗ §7.1–7.15

## Проверки

1. **Зона:** дифф затрагивает ровно 5 файлов — `src/main.py`, `src/handlers/__init__.py`, `src/services/__init__.py`, `src/repositories/__init__.py`, `src/texts.py` (все новые). Чужих файлов нет. ✅
2. **Слои (D1/FR-1):** handlers/services/repositories `__init__` — только комментарии, ноль импортов и логики; `src/main.py` и `src/texts.py` репозитории не импортируют (grep). Прогон: `import src.handlers; assert 'src.repositories' not in sys.modules` → OK. ✅
3. **Тексты §7.1–7.15:** программное построчное сравнение всех 15 разделов и 4 кнопок (§7.10, §7.13) `src/texts.py` против ТЗ — дословное совпадение (тривиальные различия — только артефакты парсинга blockquote перед строками кнопок, не дефекты). Готовность-критерий grep §7.10/§7.12 подтверждён. ✅
4. **main.py / PTB:** `ApplicationBuilder().token(token).build()` + `run_polling()` — корректный async-API PTB v21+ (в окружении 22.8); токен только из env `BOT_TOKEN`, отсутствие — `SystemExit`; `--check` возвращается до `run_polling`, в сеть не выходит. Прогон: `BOT_TOKEN=stub python3 -m src.main --check` → exit 0, «OK: Application построен…». ✅
5. **Качество:** типизация (`build_application() -> Application`, `main() -> None`, `from __future__ import annotations`), докстринги, мертвого кода нет, секретов нет (токен не хардкожен). ✅

## Findings

| # | Severity | Файл | Замечание | Рекомендация |
|---|----------|------|-----------|--------------|
| F-1 | **major** | `requirements.txt` (отсутствует) | Зависимость `python-telegram-bot` не объявлена нигде в репо (нет `requirements.txt`/`pyproject.toml`), при том что `requirements.txt` прямо указан в зоне задачи 1.1 и других задач с этим файлом в пакете нет. Критерий готовности «`python -m src.main` стартует без ошибок» выполняется только из-за предустановленного в окружении PTB 22.8 — на чистом окружении точка входа падает на `import telegram`. Каркас разворачиваемого бота (design D2) без фиксированной зависимости невоспроизводим | Создать `requirements.txt` (файл из зоны задачи) с зафиксированной зависимостью, например `python-telegram-bot>=21,<25` |
| F-2 | note | `src/main.py` | `--check` строит Application, но не вызывает `application.initialize()` — для каркаса приемлемо (initialize произойдет в `run_polling`); зафиксировано, чтобы не всплыть как вопрос на задачах волн 2–5 | Опционально: в `--check` после build вызвать initialize/shutdown, если потребуется проверка регистрации handlers |
| F-3 | note | `src/main.py:8` | Импорт `Application` используется только в аннотации; с `from __future__ import annotations` допустимо, замечания нет — отмечено как пройденная проверка типизации | — |

## Scenario, проверенные прогоном

- `BOT_TOKEN=stub python3 -m src.main --check` → exit 0, без сетевых вызовов (вывод: «OK: Application построен, слои handlers/services/repositories на месте.»).
- `python3 -c "import src.handlers; import sys; assert 'src.repositories' not in sys.modules"` → OK (правило слоёв D1).
- Пословное сверка текстов §7.1–7.15 + кнопок §7.10/§7.13 — скрипт сравнения, 0 расхождений.

## Что не проверено

- Реальный запуск long polling (`run_polling`) с боевым токеном — вне ревью (нужен реальный BOT_TOKEN, сеть).
- Поведение `run_polling` при невалидном токене — не входит в каркас 1.1.
- Контент будущих обработчиков (волны 2–5) — не в диффе.

## Вердикт: return
