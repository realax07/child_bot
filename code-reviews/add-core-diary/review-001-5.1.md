<!-- Reviewer-Delegation: deleg_7e637371199042f7 -->

# Code Review: задача 5.1 (add-core-diary) — review-001

- **Ревьюер:** независимый код-ревьюер (делегация deleg_7e637371199042f7)
- **Дата:** 2026-10-26
- **Ревьюемый код:** коммит `94965d1`, ветка `feature/add-core-diary-5.1` (worktree `/home/openclaw/child_bot-worktrees/deleg-7e637371199042f7`); дифф `be9b9d2..94965d1` (base — коммит слияния 4.5)
- **Зона диффа:** `src/services/access.py` (+67, новый — зона задачи), `src/handlers/events.py` (+24/−15 — зона), `src/handlers/undo.py` (+18/−12 — зона), `tests/test_observer.py` (+245, новый — зона), `src/texts.py` (+6 — санкционировано ПМ: единый OBSERVER_DENIED, требование задачи/note 1 review-001-2.4). Чужих файлов нет; tasks.md/спеки/test-model не тронуты.
- **Артефакты:** `tasks.md` (5.1 + критерий готовности), `design.md` (D13, D14, D1), `requirements.md` (FR-22, сценарии), решение 26.10 №5; note 1 review-001-2.4

## Замечания

Blocker — нет. Major — нет. Minor — нет.

**Note (3):**

- `src/handlers/backfill.py` (строки 55, 66, 351) — НЕ переведён на `services.access`: импортирует алиас `OBSERVER_DENIED` из `handlers.events` и проверяет `user["role"] == ROLE_OBSERVER` по-старому. Вне зоны задачи, санкционировано ПМ (перенос — пакет 2/3 или финальная уборка). Проверено: поведение не изменилось — алиас `events.OBSERVER_DENIED` теперь ссылка на тот же объект `texts.OBSERVER_DENIED` (`is`-проверка в TC-OBS-001), литерал в кодовой зоне остался один (`grep 'OBSERVER_DENIED = "'` → только `src/texts.py:131`). Замечание-напоминание: в пакете 2/3 перевести `backfill.conflict_callback` на `access.is_observer`/`access.deny_message` и убрать алиасы из `events.py`/`undo.py`, оставив их только пока есть внешние импортёры (тесты `test_conflicts.py`, `test_events_now.py`, `test_undo.py`).
- `src/services/access.py:39-47` — `access.get_user` в этом диффе никем не используется (обработчики по-прежнему берут `get_user` из `src.services.event`), это дословный дубликат обёртки `UsersRepo.get_or_create`. Не дефект (задел под D1-перевод, поведение корректно), но docstring в `src/services/event.py:38-45` остался в прошлом времени: «Временный мост… до выделения src/services/access.py (задача 5.1)» — 5.1 уже здесь, а мост не переключён. Рекомендация: в пакете 2/3 переключить обработчики на `access.get_user` и удалить мост из `event.py` (или обновить docstring, если мост остаётся сознательно).
- `src/handlers/events.py:181-183`, `src/handlers/undo.py:73-75` — проверка роли выполняется вторым SELECT (`access.is_observer`) сразу после `get_user`, т.е. два запроса к `users` на одно нажатие. Это осознанное следствие D13 (свежее чтение без кэша), издержка ничтожна (SQLite, локальный файл) — фиксирую как факт для истории, действия не требуется.

## Проверено

- **Зона:** дифф ограничен 5 файлами зоны (+ санкционированный `texts.py`); чужих правок нет.
- **Единая точка (D13, D1):** `access.get_role` — свежий SELECT `users.role` по `users.id` без кэша; неизвестный id → `ROLE_PARENT` (безопасный дефолт схемы NOT NULL DEFAULT 'parent', задача 1.2); `is_observer` — сравнение с `ROLE_OBSERVER`; `deny_message` → `texts.OBSERVER_DENIED`. Обработчики (`events.event_button_callback`, `events.time_choose_callback`, `undo.undo_last_callback`) зовут только `access.is_observer`/`access.deny_message`, SQL в handlers отсутствует.
- **Дубли убраны:** литерал OBSERVER_DENIED остался только в `texts.py:131`; в `events.py`/`undo.py` — алиасы-присваивания для обратной совместимости импортов (backfill.py, 3 тест-файла). Поведение не изменилось: observer → `query.answer(text=…, show_alert=False)`, событие не создаётся / запись не удаляется, фаза диалога снимается — подтверждено тестами и чтением диффа (ветки отказа сохранены дословно, изменился только источник текста и способ проверки роли).
- **FR-22 сценарии:** «Наблюдатель жмёт кнопку события» — TC-OBS-002/005 (записи нет, сообщение есть, меню не переключилось, фаза снята); «Наблюдатель запрашивает данные» — вне пакета 1 (сводки — пакеты 2–3, D13). Негативные/смежные: parent без ограничений (TC-OBS-003), отказ undo без удаления и без изменения state (TC-OBS-004), смена роли данными без перезапуска в обе стороны (TC-OBS-006). Все 6 TC-OBS-001…006 покрывают критерий готовности задачи («observer жмёт "😴 Заснул" → записи нет, есть сообщение; parent — запись есть») и расширение из делегации.
- **Прогон (worktree, свежий вывод):** `python3 -m pytest tests/ -q` → **161 passed** ×2 (2.70s / 2.82s; было 155 + 6 новых); `python3 -m pytest tests/test_observer.py -q` → **6 passed**; `python3 scripts/flow_check.py .` → **OK**. Docstring'и всех 6 тестов содержат TC-OBS-xxx; `time.sleep` в тест-файле нет. Регресс-набор вместо смоуков — других скриптов не писал.
- **Best practices:** SQL параметризован; секретов нет; тесты ходят через реальные обработчики с фейковым PTB (не заглушки сервисов); имена по домену; комментарии трассируют FR-22/D13/решение №5.

## Не проверено

- Реальное поведение в Telegram (обновление inline-меню, `query.answer` на живом клиенте) — только через фейковый PTB; покрытие теми же средствами, что и весь регресс-набор.
- Связь observer → владелец данных (linkage) — по D13 сознательно не создаётся в пакете 1, проверять нечего.
- Перевод `backfill.py` на `services.access` — вне зоны (санкция ПМ), оценено как приемлемое для пакета 1 (note 1).

## Вердикт: approve
