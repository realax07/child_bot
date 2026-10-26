<!-- Reviewer-Delegation: deleg-28b2120c6a6547f4 -->

# Code Review: задача 1.3 (add-core-diary) — review-001

- **Ревьюер:** независимый код-ревьюер (делегация deleg-28b2120c6a6547f4)
- **Дата:** 2026-10-26
- **Ревьюемый код:** коммит `cae3ee4`, ветка `feature/add-core-diary-1.3` (worktree `/home/openclaw/child_bot-worktrees/deleg-28b2120c6a6547f4`); дифф `13aeae5..cae3ee4`
- **Артефакты:** tasks.md (1.3), design.md (D3, D5, D6, D11, D12, D14), requirements.md (FR-2, FR-3), src/db.py (готов задачей 1.2)

## Проверки

1. **Зона:** коммит `cae3ee4` затрагивает ровно 4 файла из зоны задачи: `src/repositories/users.py` (73+), `src/repositories/sleep_events.py` (130+), `src/repositories/settings.py` (44+), `tests/test_repositories.py` (280+). Посторонних изменений нет. ✅
2. **Сырые поля (FR-3, design D6, review-001 F-1):** `insert` пишет только `user_id, event_type, timestamp, created_at, is_backfilled`; `sleep_type`/`is_merged` заданы литеральными NULL в VALUES и параметрами не передаются. `overwrite` обновляет только `event_type`/`timestamp`, резервы схемы не трогает — после overwrite остаются NULL (проверено тестом TC-REPO-015). Методов записи `sleep_type`/`is_merged` в API репозиториев нет вообще. ✅
3. **Контракты:**
   - `UsersRepo.get_or_create` идемпотентен: SELECT→INSERT→SELECT, повторный вызов возвращает того же пользователя без дубля (TC-REPO-002, COUNT=1). Роль по умолчанию 'parent' — D3/TC-REPO-003. ✅
   - `SleepEventsRepo.last`/`delete_last` — `ORDER BY created_at DESC, id DESC` (D12): «последняя» по моменту записи, не по `timestamp`; тест TC-REPO-011 фиксирует выбор задним-числом события с ранним timestamp. ✅
   - `overwrite` — один UPDATE по id, без DELETE+INSERT; TC-REPO-015 подтверждает отсутствие дублей (COUNT=2) и нетронутость соседней записи (D11). Без переданных полей → False (TC-REPO-017). ✅
   - `SettingsRepo.get_all` (dict key→value, изоляция по user_id — TC-REPO-019), `upsert` — `ON CONFLICT (user_id, key) DO UPDATE` поверх UNIQUE схемы; обновление без дублей (TC-REPO-021), одинаковый ключ у разных пользователей раздельно (TC-REPO-022). ✅
4. **Тесты:** ровно 22 (`pytest --collect-only` → 22), докстринги TC-REPO-001…022 без пропусков, ассерты содержательные (прямые SELECT в БД, COUNT-проверки, негативные IntegrityError/False/None, изоляция пользователей). `time.sleep` отсутствует (grep по диффу — пусто). ✅
5. **Прогон в worktree:** `python3 -m pytest tests/ -q` → **34 passed in 0.74s** (22 новых + 12 задач 1.2 — регрессии нет); `python3 scripts/flow_check.py .` → `flow_check: OK — порядок артефактов соответствует флоу`. ✅
6. **Секреты/мёртвый код/инъекции:** grep по паролям/токенам/ключам/`eval`/`exec`/`os.system` — пусто. Все SQL-запросы параметризованы (`?`-плейсхолдеры); единственные f-string-вставки — `_COLUMNS` (константный кортеж имён колонок) и собранный из литеральных строк SET-список в `overwrite` (имена полей не приходят извне) — инъекция невозможна. Мёртвого кода нет; все публичные методы репозиториев покрыты тестами. Слои не нарушены: репозитории не импортируют handlers/services (D1). ✅

## Замечания

| # | Серьезность | Файл:место | Замечание | Рекомендация |
|---|---|---|---|---|
| 1 | note | src/repositories/users.py:41–54 | `get_or_create` не защищён от гонки двух параллельных INSERT по одному telegram_id (UNIQUE → IntegrityError у проигравшего). Для однопроцессного бота на общем соединении (design D1/D3) недостижимо | Оставить как есть; при появлении конкурентных соединений (WAL это допускает) — обернуть INSERT в try/IntegrityError с повторным SELECT |
| 2 | note | src/repositories/users.py:23 / sleep_events.py:26 | `_utcnow_iso()` дублируется в двух модулях; seconds-разрешение формата `%Y-%m-%d %H:%M:%S` означает возможные равные created_at у двух событий одной секунды — корректно разрешается tie-break по id в last/delete_last (D12), поведение зафиксировано тестами | Вынести в общий хелпер при следующей правке; менять формат не нужно |
| 3 | note | src/repositories/sleep_events.py:73 | `assert row_id is not None` в продовом пути: assert выпадает при `python -O`, а по факту это контракт «lastrowid задан после INSERT», а не инвариант для отладки | Заменить на явную проверку или убрать — cur.lastrowid после успешного INSERT в sqlite3 всегда int |
| 4 | note | src/repositories/sleep_events.py:100–103 | `overwrite` без переданных полей возвращает False (no-op), что документировано тестом TC-REPO-017, но из сигнатуры неочевидно — вызывающий сервис (задача 3.x, D11) обязан передать хотя бы одно поле | Одной строки в docstring достаточно (уже упомянуто «Меняются только переданные поля»); можно усилить при интеграции |
| 5 | note | tests/test_repositories.py:132 | TC-REPO-012 вызывает `events.last(1001)` с telegram_id вместо `user["id"]` — проходит, т.к. в фикстуре id=1≠1001 только внешне: фактически пользователь первый, id=1, а 1001 — чужой user_id; тест проверяет пустую выборку несуществующего пользователя, что совпадает с целью «пустая БД», но формально не по фикстуре | Поправить на `user["id"]` для точности формулировки «на пустой БД» |

Blocker/major не найдены.

## Проверенные Scenario

- FR-3 / «Событие фиксируется как есть» + «sleep_type при записи не вычисляется»: insert пишет только сырые поля, резервы NULL (TC-REPO-006/007), в т.ч. после overwrite (TC-REPO-015).
- FR-2 (контракт уровня CRUD): CHECK event_type отклоняет недопустимый тип (TC-REPO-008), FK-зависимость через user_id (изоляция TC-REPO-010).
- D12 / FR-21: last/delete_last по created_at→id (TC-REPO-011/013), пустой случай → False без изменений (TC-REPO-012/014).
- D11 / FR-18 (репозиторная часть): атомарный UPDATE по id без дублей, частичная перезапись (TC-REPO-015/016/017).
- D14 (репозиторная часть): upsert идемпотентен, UNIQUE(user_id,key), изоляция настроек (TC-REPO-018…022).
- Негативные: несуществующий пользователь (TC-REPO-005), несуществующий id overwrite (TC-REPO-016), чужие данные не видны (TC-REPO-010/019).

## Что НЕ проверено

- Интеграцию репозиториев с сервисами/handlers (задачи 2.x, волна 2) — модулей ещё нет.
- Поведение под конкурентными соединениями (WAL) — вне критерия готовности задачи.
- Корректность вычисления sleep_type/is_merged при чтении — логика пакетов 1.4+/2, колонки только резервируются (D6/D9).

## Вердикт: approve
