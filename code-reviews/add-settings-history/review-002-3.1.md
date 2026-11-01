Reviewer-Delegation: deleg_2d3831591a534c6a

# Code Review 3.1 (повторный, review-002) — проверка фикса major review-001-3.1

- **Исполнитель:** dev-агент (фикс-коммит `2a2e5d3` на `963c68b`, worktree `deleg-2d3831591a534c6a`)
- **Диф:** `git diff 963c68b..2a2e5d3` — 4 файла (+253/−23): `src/repositories/sleep_events.py` (insert → stage_insert без commit + новый insert-обёртка с commit), `src/services/event.py` (новые `_ensure_pair_step` + `create_event_pair`, диф чисто аддитивный), `src/handlers/backfill.py` (парная ветка → `create_event_pair`; `except ValueError` → rollback + честный отказ-ответ `BACKFILL_PAIR_FAILED`), `tests/test_backfill_pair.py` (TC-PBACK-011 атомарность; 012/013 FUTURE — закрытие minor №2 review-001).
- **Спека-арбитр:** те же, что review-001 (ТЗ §7.11–7.15; design D23, D5, D4, D19, D6, D9, D12; FR-51, FR-41, FR-20).
- **Санкции ПМ, учтённые как не-дефекты:** (1) `stage_insert` в `repositories/sleep_events.py` — вне первичной зоны 3.1, файл санкционирован ПМ; (2) `BACKFILL_PAIR_FAILED` — константа в `backfill.py`, не в `texts.py` (texts вне зоны фикса, санкция ПМ; перенос — задача 4.2).
- **Предмет ревью:** ТОЛЬКО фикс major №1 (атомарность пары) + закрытие minor №2 (покрытие FUTURE). Основная функциональность 3.1 верифицирована в review-001 и здесь не переревьюивалась.

## Замечания

| # | Severity | Файл:строка | Суть | Рекомендация |
|---|----------|-------------|------|--------------|
| 1 | note | `src/handlers/backfill.py` `_pair_wake_message`, ветка `except ValueError` | **Отказ вне ValueError не даёт пользователю ответа.** `conn.commit()`/`_notify_scheduler()` внутри `create_event_pair` бросают не-ValueError (sqlite3.Error / RuntimeError): исключение уходит в PTB error handler — пользователь без ответа, фазы не сняты. Полупара при этом НЕВОЗМОЖНА в обоих порядках (проба ревьюера, сцены 3–4 ниже), поведение — паритет с одиночной веткой `create_event` (хук тоже после записи). Вне рамок major №1, не регрессия. | Принять как есть (parity с существующим кодом); при желании — единая обработка не-ValueError в 4.2 вместе с переносом текстов. |
| 2 | note | `src/handlers/backfill.py` `_pair_wake_message` (try-блок) | `_backfill_success_text` стоит ВНУТРИ try перед `except ValueError`: гипотетический ValueError от читателя сводки дал бы отказ-ответ ПОСЛЕ уже закоммиченной пары (ответ «не записано» при записанной паре). На текущем коде недостижимо — читатель ValueError не бросает (проверено чтением: `_backfill_success_text` → сборщики чтения). | Опционально: вынести `_backfill_success_text` из try (после except-ветки) при следующем касании файла. |
| 3 | note | `src/services/event.py` `_ensure_pair_step` vs `create_event` | D5-блок (kind/состояние/последнее событие) продублирован: `create_event` держится побайтно прежним (контракт фикса — обоснованно), `_ensure_pair_step` повторяет его проверки. Копия маленькая, с комментарием-отсылкой. | Объединить общий D5-блок в рефактор-окно (4.2 или долг-карта), пока `create_event` не трогается внешними причинами. |
| 4 | note | `scripts/flow_check.py` вывод | `FLOW-WARNING: промпты устарели относительно ~/ai-factory (pm_agent.md)` — то же инфраструктурное наблюдение, что в review-001-2.0/2.1/3.1; вне зоны задачи, `flow_check: OK`. | Передать ПМ как фоновое по конвейеру. |

Blocker: нет. Major: нет. Minor: нет (minor №2 review-001 — покрытие FUTURE — закрыт TC-PBACK-012/013).

## Вердикт: approve

Major №1 review-001 устранён корректно: пара пишется одной транзакцией (stage×2 → один commit), откат не оставляет полупару ни при одном порядке отказа, молчаливый проглот заменён честным отказ-ответом, хук ре-шедула — ровно один после коммита. Одиночные ветки не тронуты (`create_event` побайтно прежний — 0 удалённых строк в дифе event.py; легаси wake-ветка и репозиторные вызовы кнопок «сейчас» работают как раньше). Регресс 328×2 green, flow_check OK.

## Проверено (с командами и выводом)

1. **Сцена review-001 воспроизведена на фиксе — СВОЙ скрипт** (`/home/openclaw/.hermes/cache/scratch/review002_probe.py`, не дев-тест; реальный обработчик `backfill.backfill_time_message`, инъекция ValueError в `SleepEventsRepo.stage_insert` на wake). Вывод пробы: `staged == ['sleep','wake']` (дошли до второй вставки) → **БД = [] (0 событий, полупары нет)**; `replies == [BACKFILL_PAIR_FAILED]` («⚠️ Не удалось записать сон…»); все 4 фазы диалога сняты; `_notify_scheduler` при откате — 0 вызовов. 5/5 PASS.
2. **Транзакционность (трасса sqlite `set_trace_callback` + счётчик хука):** порядок на успешной паре — `insert, insert, COMMIT, notify`: ровно ОДИН COMMIT на пару, хук ровно ОДИН и СТРОГО после коммита. D5 шага-2 видит staged sleep: последний вызов `last()` внутри `create_event_pair` — `in_transaction=True`, в коннекте 1 строка, `last=event_type='sleep'` (та же транзакция; конфликт-чек шага 2 идёт ДО пары и на staged не влияет — код). 5/5 PASS.
3. **Полупара при любом порядке отказа (сцены 3–4 пробы):** (а) отказ `commit()` (прокси-коннект, OperationalError) — исключение наружу (не глотается), БД = [] (0 событий); (б) отказ `_notify_scheduler` после commit (RuntimeError) — исключение наружу, в БД ПОЛНАЯ пара (sleep+wake), не полупара. 4/4 PASS. Замечание note №1 — про отсутствие ответа пользователю в этих путях (паритет с одиночной веткой).
4. **Одиночные ветки не тронуты:** `git diff 963c68b..2a2e5d3 -- src/services/event.py | grep '^-[^-]' | wc -l` → **0** (`create_event` побайтно прежний, диф аддитивный); `calculation.py`, `events.py`, `settings_screen.py` — нулевой диф (`--stat` пуст). Легаси wake-ветка без дубля: проба через реальный обработчик при незакрытом sleep 10:40 → «вчера 15:15» → `[sleep, wake]`, дубля засыпания нет, §7.14-ответ; TC-PBACK-004 в сьюте green. Репозиторий: новый `insert` = `stage_insert`+commit — единственные вызовчики `insert` вне пары: `create_event` (event.py:99), настройки истории не тронуты.
5. **Регресс:** `python3 -m pytest tests/ -q` ДВАЖДЫ в worktree → `328 passed in 7.91s` и `328 passed in 8.28s` (325 review-001 + 3 новых). `python3 scripts/flow_check.py .` → `flow_check: OK` (exit 0).
6. **Гигиена новых тестов:** TC-PBACK-011/012/013 — TC-id в докстрингах (файл целиком: 18 вхождений TC-PBACK); `time.sleep`/`datetime.now(` в тест-файле — 0 вхождений; время зафиксировано frozen_now (8:00 МСК 10.10); 012 проверяет сохранение фазы шага 1, 013 — сохранение sleep_utc в фазе шага 2 + оба `is_backfilled=1`. Minor №2 review-001 (пробел покрытия FUTURE) закрыт.
7. **BACKFILL_PAIR_FAILED не глотает диагностику:** rollback выполняется явно до ответа (сцена 1), отказ-ответ — константа модуля, текст честный («состояние изменилось»), success-ответ при отказе отсутствует.

## Что НЕ проверено

- Живой PTB/long polling и реальный конкурентный доступ двух апдейтов (проверялось на уровне обработчика + sqlite-трассы; конкурентная сцена review-001 остаётся теоретической — оценка «major» давалась из-за достижимости при конкурентной записи, сейчас исключена атомарностью).
- `ReminderScheduler.reschedule` в живом процессе (идемпотентность принята по design D19 и review-001 п.5).
- Основная функциональность 3.1 (конфликты, §7.13/7.15, FSM) — верифицирована в review-001, здесь не дублировалась; регресс-сьютом покрыта.
