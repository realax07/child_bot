<!-- Reviewer-Delegation: deleg_b6f943fbfc024b06 -->

# Code Review: задача 1.2 (add-core-diary) — review-001

- **Ревьюер:** независимый код-ревьюер (делегация deleg_b6f943fbfc024b06)
- **Дата:** 2026-10-26
- **Ревьюемый код:** коммиты `93cd831` + `b157971`, ветка `feature/add-core-diary-1.2` (worktree `/home/openclaw/child_bot-worktrees/deleg-b6f943fbfc024b06`); дифф `a35e5e0..b157971`
- **Артефакты:** tasks.md (1.2), design.md (D3, D4, D6), requirements.md (FR-2, FR-3, FR-22), ТЗ §9.1–9.3

## Проверки

1. **Зона:** коммиты dev `93cd831` (src/db.py 92+, tests/test_db.py 180+) и `b157971` (tests/test_db.py, только docstrings) затрагивают ровно `src/db.py` + `tests/test_db.py`. Полный дифф диапазона `a35e5e0..b157971` дополнительно содержит `docs/project-pitfalls.md` и чекбокс 1.1 в `tasks.md` — это админ-коммит ПМ `d842d66` (merge 1.1), не зона dev; нарушений зоны dev нет. ✅
2. **Схема vs ТЗ §9.1–9.3:** колонки/типы сверены программно (PRAGMA table_info в тестах TC-DB-005) и глазами по DDL. `users`: id/telegram_id/timezone/created_at + `role TEXT NOT NULL DEFAULT 'parent'` (D3, решение №5); `sleep_events`: все 8 колонок §9.2, CHECK `event_type IN ('sleep','wake')`, FK → users(id); `settings`: 4 колонки §9.3 + UNIQUE(user_id, key). `settings_history`/`daily_summary` НЕ создаются (D3) — тест TC-DB-002 фиксирует «ровно три таблицы». ✅
3. **PRAGMA (D3):** `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000` — включаются на каждом открытии в `connect()`; WAL как свойство файла проверен на втором соединении (TC-DB-003/004/011). Прогон: 11 passed, journal_mode=wal подтверждён. ✅
4. **Nullable-резервы (D6/FR-3):** `sleep_type`/`is_merged` объявлены без NOT NULL; тест TC-DB-008 вставляет событие без них и читает `(None, None)` — при записи NULL. ✅
5. **FK и дефолты:** негативные сценарии покрыты реальными вставками: FK-нарушение отклоняется (TC-DB-010), NULL в role отклоняется (TC-DB-007), дефолт role='parent' подтверждён вставкой (TC-DB-006). ✅
6. **Тесты:** 11 тестов, докстринги TC-DB-001…011 (трассировка для flow_check), ассерты содержательные (DDL-сверка типов/notnull, негативные IntegrityError, WAL через второе соединение). `time.sleep` отсутствует. Прогон: `python3 -m pytest tests/test_db.py -q` → **11 passed in 0.21s**. ✅
7. **flow_check:** `python3 scripts/flow_check.py .` → `flow_check: OK — порядок артефактов соответствует флоу`. ✅
8. **Секреты/мёртвый код:** grep по секретам/токенам/паролям — пусто; путь БД — параметр (env-обёртка — задача 5.3, design D15, заявлено в шапке модуля). Мёртвого кода нет; оба экспорта (`connect`, `init_db`) используются тестами. Docstrings на всех уровнях. ✅

## Замечания

| # | Серьезность | Файл:место | Замечание | Рекомендация |
|---|---|---|---|---|
| 1 | minor | src/db.py:37 (`sleep_type TEXT`) | Нет `CHECK (sleep_type IN ('day','night'))` — ТЗ §9.2 допускает day/night/null; сейчас колонка примет любую строку. Смягчается тем, что по D6 колонка — nullable-резерв, пакетом 1 не пишется | Добавить CHECK в additive-миграцию пакета 2/3, когда колонка начнёт заполняться |
| 2 | note | tests/test_db.py:70 | `PRAGMA table_info({table})` через f-string — паттерн SQL-инъекции в коде. Значение приходит из константного словаря теста, эксплойта нет | Оставить; при расширении таблицы параметризовать список допустимых имён |
| 3 | note | src/db.py:23 vs :45 | Асимметрия NOT NULL: `users.created_at` nullable, `sleep_events.created_at` NOT NULL. ТЗ NOT NULL нигде не задаёт — это хардеning dev, поведение не нарушено | Можно выровнять в future migration; не блокирует |
| 4 | note | src/db.py:76–78 | `is_backfilled BOOLEAN NOT NULL DEFAULT 0` и `UNIQUE(user_id,key)` в settings — добавления сверх ТЗ §9.2–9.3; осмысленные (булев флаг, идемпотентный seed D14), но формально вне дословного §9 | Зафиксировать как сознательное расширение в design (D3-аппендикс) при следующей правке design.md |
| 5 | note | tests/test_db.py:138–146 | TC-DB-009 ищет индекс подстрокой в `sqlite_master.sql` — сработает и на обратный порядок колонок; слабее точной сверки | Достаточно для критерия готовности; при споре — парс `sqlite_stat`/EXPLAIN |

## Проверенные Scenario

- FR-2 / «Инициализация БД»: создание с нуля, три таблицы, WAL (TC-DB-001/002/003/011).
- FR-3 / «Событие фиксируется как есть» и «sleep_type при записи не вычисляется» (схемная часть): nullable-резервы пишутся NULL (TC-DB-008).
- FR-22 (схемная часть): role NOT NULL DEFAULT 'parent', негативная вставка NULL (TC-DB-006/007).
- Негативные: FK-нарушение (TC-DB-010), foreign_keys=ON (TC-DB-004).

## Что НЕ проверено

- Репозитории и запись событий из бизнес-логики (задача 1.3) — вне диффа.
- Многопроцессная конкуренция записей (WAL + busy_timeout) — поведение не тестируется в пакете 1; проверен только факт установки PRAGMA.
- Совместимость с python-telegram-bot/прод-деплоем — модуль не связан с PTB.

## Вердикт: approve

Нарушений спеки (blocker/major) нет: схема дословно соответствует ТЗ §9.1–9.3 c добавками D3 (role) и D6 (nullable-резервы), PRAGMA включаются на каждом соединении, зона соблюдена, тесты осмысленные и проходят (11 passed), flow_check OK. Замечания minor/note не блокируют слияние.
