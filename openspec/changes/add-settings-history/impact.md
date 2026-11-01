# Impact-карта — add-settings-history

> Заполнена СА при create_change (по образцу add-recommendations/impact.md). Дополняется ПМ при изменении дельт.

## Затронутые сервисы

Единственный процесс бота `src/main.py` (PTB v21, long polling) — без изменения топологии. В процесс добавляется:
- `src/services/settings.py` — РАСШИРЯЕТСЯ: `update(user_id, key, value)` + валидация по типам DEFAULT_KEYS + запись в settings_history одной операцией (design D21);
- `src/services/calculation.py` — РАСШИРЯЕТСЯ новой чистой функцией `weekly_history`; контракт существующих `day_facts_lines` / `daily_summary` НЕ меняется (регресс PB-3/BUG-004 остаётся green) (D22);
- `src/services/event.py` — РАСШИРЯЕТСЯ `delete_all(user_id)` (§10.5, D25);
- `src/services/state.py` + `src/handlers/common.py` — клавиатура и маршрутизация: кнопки «📊 История за неделю» и «⚙️ Настройки» в обоих состояниях (ТЗ §6);
- `src/handlers/settings_screen.py`, `src/handlers/history.py` — новые; `src/handlers/backfill.py` — парный ввод PB-1 (два шага);
- `src/repositories/settings.py` (SettingsHistoryRepo), `src/repositories/users.py` (удаление users-строки), `src/db.py` (CREATE TABLE settings_history), `src/texts.py` (§7.16–7.22, §7.26–7.28), `src/handlers/__init__.py`, `src/main.py`.
- Техдолг пакета 2 (волны 1/4): дубли хелперов undo/events, owner_users SQL в `scheduler.py` → UsersRepo, мёртвая `total_wake`, `nap_window_text` в §7.5-ветке `events.py`, локальный дубль дефолтов §5.

## Контракты

Telegram Bot API: новые callback `history:week`, `settings:show`, `settings:edit:<key>` (ТЗ §10.3), колбэк подтверждения delete_all; новые команды /settings, /history, /help, /delete_all (§10.2); FSM WAIT_SETTING_VALUE + подтверждение delete_all (§10.4-стиль). Callback-схема пакетов 1–2 не меняется — только расширение. Клавиатура (reply) меняется: +2 кнопки в обоих состояниях. Внутреннего HTTP API нет (IR-01 неприменим).

## Статика (static_v?)

Нет фронтенд-статики (IR-03 неприменим). Web App — anti-scope (отклонено продактом 01.11).

## Deploy-матрица и RUNBOOK

Миграций данных НЕТ: settings_history создаётся идемпотентным `CREATE TABLE IF NOT EXISTS` при init_db (ТЗ §9.4; фактически в схеме main таблицы нет — расхождение PLAN↔код зафиксировано в research/report). Пустая таблица — лифть нечего. Внимание деплоя: первое изменение настройки после релиза создаёт первую запись settings_history. RUNBOOK релизного цикла дополняет план деплоя пакета.

## Миграции

Нет. Схема дополняется только CREATE TABLE settings_history (идемпотентно); users/sleep_events/settings не меняются; sleep_type CHECK-миграция — вне пакета (не запланирована).

## Тесты (существующие — под изменением)

- `tests/test_state.py` — обновление: клавиатура +2 кнопки в обоих состояниях (тексты §6, маршрутизация) — задача 2.0.
- `tests/test_smoke_core.py`, `tests/test_smoke_recommendation.py` — обновление ассертов клавиатуры/меню (поведение существующих кнопок не меняется).
- `tests/test_settings_usage.py` — проверка после рефакторинга 1.3 (дубль дефолтов сведён на DEFAULT_KEYS).
- Регресс PB-3/BUG-004 (`test_daily_summary.py`, факты дня) — обязан остаться green без изменений (контракт 2.1).
- Регресс пакета 2 (окна/подсказки/напоминания) — green; ре-шедул после пары PB-1 и после delete_all — по точкам EventService (FR-41 пакета 2 сохраняется).
- Новые: `tests/test_settings_history.py`, `tests/test_settings_screen.py`, `tests/test_weekly_history.py`, `tests/test_history_handler.py`, `tests/test_backfill_pair.py`, `tests/test_help.py`, `tests/test_delete_all.py`, `tests/test_smoke_settings_history.py`. Автотесты — по approved TC-кейсам (`TC-SETT-0xx`, `TC-HIST-0xx`, `TC-PBACK-0xx`, `TC-HELPDEL-0xx`, TC-трассировка в docstring, flow_check rule 6); test-model/case обновит qa_automation на своем этапе. Пресеты (TC-PRESET) — отменены продактом 01.11, кейсов нет.
