<!-- Reviewer-Delegation: deleg-ecd7690377354e8c -->

# Code Review: задача 2.3 (add-core-diary) — review-001

- **Ревьюер:** независимый код-ревьюер (делегация deleg-ecd7690377354e8c)
- **Дата:** 2026-10-26
- **Ревьюемый код:** коммит `79d5066`, ветка `feature/add-core-diary-2.3` (worktree `/home/openclaw/child_bot-worktrees/deleg-ecd7690377354e8c`); дифф `b8441f2..79d5066` (base — слияние 2.2)
- **Артефакты:** tasks.md (2.3), design.md (D2, D5, D6, D10, D13), requirements.md (FR-7, FR-8, FR-15, FR-22); ТЗ §7.2/§7.5/§7.10/§7.11 (дословная сверка)
- **Санкционированные ПМ отклонения (не дефекты):** правки `src/handlers/__init__.py` (регистрация callback-хендлеров — согласованная точка); удаление `_CALLBACK_BY_KIND` + docstring `kind_of_button` (прямое исполнение minor-3/note-4 review-001-2.2); обновление ассерта клавиатуры в `tests/test_start.py` (вынуждено интеграцией inline-меню — иначе тест был бы красным); одна строка `register_handlers` в `src/main.py`. Все четыре присутствуют в диффе ровно в согласованном объёме, посторонней логики не несут.

## Замечания

| # | Серьезность | Место | Суть | Рекомендация |
|---|---|---|---|---|
| 1 | **major** | `tests/test_events_now.py:284` (TC-EVNOW-006) | Флейк-ассерт, зависящий от времени суток: `assert "15:30" not in answer` отвергает подстроку, которая совпадает с фактическим локальным (Europe/Moscow) ЧЧ:ММ момента записи. Воспроизведено дважды при ревью (2026-10-26, 15:30 МСК): ответ `'✅ Засыпание записано в 15:30.\nСон идёт. Крайнее пробуждение: 16:00.'` корректен по FR-7, но ассерт падает. Окно отказа — 1 минута в сутки; CI будет интермиттентно красным. Заявление «55 green» на момент ревью не подтверждено: **1 failed, 54 passed** в двух прогонах | Не исключать подстроку старого дефолта из всего текста: ассерть точное равенство второй строки — `answer.splitlines()[1] == "Сон идёт. Крайнее пробуждение: 16:00."` (суть теста — источник строки «Крайнее пробуждение»), либо заморозить время (freezegun/monkeypatch `events.datetime`). Прод-поведение корректно — дефект только в тесте |

**Notes (не влияют на вердикт):**

- `src/handlers/events.py`, ветка успеха `time:now`: `query.answer()` вызывается только после `redraw_menu` и `reply_text` — если edit/reply упадёт (не BadRequest), answerCallbackQuery не состоится и у пользователя останется «часики». В guard-ветках answer сделан до перерисовки — можно выровнять порядок и здесь.
- `src/handlers/events.py` (`event_button_callback`): `except BadRequest` вокруг `edit_message_text` глотает любой BadRequest, не только «Message is not modified» (например, «message can't be edited»). Паттерн согласован с review-001-2.2 (там сужение до BadRequest принято достаточным) — замечание не возвращается, фиксируется как известное поведение.

## Проверки

1. **Зона:** дифф `b8441f2..79d5066` — 9 файлов: `src/handlers/{__init__,common,events,start}.py`, `src/main.py`, `src/services/{event,state}.py`, `tests/{test_events_now,test_start}.py`. Новые файлы ровно из зоны задачи (`events.py`, `event.py`, `test_events_now.py`); отклонения — из списка санкционированных ПМ. Посторонней логики нет. ✅
2. **Guard на ОБОИХ шагах (D5):** `event_button_callback` — `is_stale_callback` по свежему чтению БД → `query.answer()` + `common.redraw_menu`, `_set_pending_kind(None)`, события нет; `time_choose_callback` — повторный guard: (а) нет фазы диалога в `user_data` → трактовка как устаревшее нажатие (безопасный дефолт), (б) `is_stale_callback` повторно, (в) третья линия защиты — `create_event` поднимает ValueError при противоречии состоянию → тот же guard-поведение. Покрыто TC-EVNOW-004 (устаревшая `event:sleep` при спящем: записи нет, `answers == [None]`, клавиатура → «☀️ Проснулся») и TC-EVNOW-005 (устаревший `time:now`). ✅
3. **time:now (FR-7/FR-8, D4, D6):** `datetime.now(timezone.utc)` → строка UTC «YYYY-MM-DD HH:MM:SS»; `create_event` пишет только сырые поля через `SleepEventsRepo.insert(is_backfilled=False)`; `sleep_type`/`is_merged` — NULL (TC-EVNOW-002/003 ассертят оба NULL и `is_backfilled is False`; TC-EVNOW-002 — погрешность времени < 300 c). ✅
4. **Тексты (AC-6):** §7.5 — `fell_asleep_text` дословно совпадает с ТЗ §7.5 при подстановке («✅ Засыпание записано в ЧЧ:ММ.\nСон идёт. Крайнее пробуждение: ЧЧ:ММ.»); «Крайнее пробуждение» — из настройки `wake_deadline` (TC-EVNOW-006: смена на 16:00 меняет текст — сам механизм покрыт, флейк-ассерт см. замечание 1). §7.2 — `woke_up_text` = только первая строка дословно, строки окна нет (TC-EVNOW-003: `"Окно" not in answer`, `"\n" not in answer`). §7.10 («Когда это произошло?» + `time:now`/`time:manual`) и §7.11 — сверены с ТЗ дословно. ✅
5. **Observer (D13, FR-22):** проверка роли в обоих обработчиках (`event_button_callback` и `time_choose_callback`); нажатие игнорируется, записи нет, вежливое сообщение `OBSERVER_DENIED` через `query.answer(text=…)`; фаза диалога сбрасывается. TC-EVNOW-007: `answers == [OBSERVER_DENIED]`, счётчик событий 0, §7.10 не показан. ✅
6. **D10 / D2:** фаза диалога — `context.user_data["pending_event_kind"]`, БД-FSM нет; callback-контракты дословно: `event:sleep`/`event:wake`, `time:now`/`time:manual` — и в коде, и в паттернах регистрации (`^event:(sleep|wake)$`, `^time:(now|manual)$`). `time:manual` отвечает §7.11 и сохраняет фазу (разбор ввода — зона 4.1, согласовано с tasks.md). ✅
7. **Minors review-001-2.2 исполнены:** (1) `start.py` переведён на `common.open_conn`, приватный `_db_path` удалён — один источник правды о пути к БД; (2) `common.redraw_menu` — `except BadRequest` вместо `except Exception`, прочие ошибки не глотаются; (3) `_CALLBACK_BY_KIND` удалён; (4) docstring `kind_of_button` дописан (undo роутится по callback_data). ✅
8. **Слои (D1) и сервис:** `events.py` импортирует только сервисы (`services.event/state/settings`) и `texts`; репозитории — только внутри `services/event.py`. `get_user` — временный мост до `access.py` (5.1), задокументирован в докстринге. `SettingsService.get` при отсутствии ключа возвращает дефолт — нажатие кнопки без предварительного /start не падает. ✅
9. **Тесты TC-EVNOW-001…009:** все 9 присутствуют, TC-id в докстринге, `time.sleep` нет; ассерты содержательные (состав меню, счётчики записей, answers callback, перерисовка, повторяемость sleep→wake→sleep в TC-008, контракт сервиса в TC-009). Замечание 1 — к качеству TC-006, не к покрытию. ✅
10. **Прогоны в worktree (свежий вывод):** `python3 -m pytest tests/ -q` → **1 failed, 54 passed** (замечание 1; повторный прогон — тот же результат: отказ стабилен в окне 15:30 МСК, не сетевой шум); `python3 scripts/flow_check.py .` → `flow_check: OK — порядок артефактов соответствует флоу`; `BOT_TOKEN=stub python3 -m src.main --check` → `OK: Application построен…`. ✅
11. **Безопасность:** SQL — только через репозитории, параметры плейсхолдерами (`INSERT INTO sleep_events (…) VALUES (…)` с bind-параметрами); секретов в диффе нет (BOT_TOKEN читается из env, --check на stub проходит); пользовательские данные во внешние сервисы не отправляются. ✅

## Что НЕ проверено

- Реальная сеть/Telegram (все проверки — на фейковом PTB-слое): доставка answerCallbackQuery, поведение inline-клавиатуры в реальном чате.
- Полный диалог заднего числа после `time:manual` (парсер ввода — задача 4.1): проверен только факт запроса §7.11 и сохранение фазы.
- Конкурентные нажатия из нескольких устройств в реальном времени (guard покрыт юнит-сценариями «состояние изменилось между шагами»).

## Вердикт: return

Единственное замечание — major (флейк-ассерт TC-EVNOW-006, ломающий критерий готовности «55 green»); прод-код замечаний не имеет. После правки теста — повторное ревью (review-002) по тому же коммиту/диффу: код менять не требуется.
