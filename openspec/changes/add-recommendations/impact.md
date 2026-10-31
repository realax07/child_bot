# Impact-карта — add-recommendations

> Заполнена СА при create_change (по образцу add-core-diary/impact.md). Дополняется ПМ при изменении дельт.

## Затронутые сервисы

Единственный процесс бота `src/main.py` (PTB v21, long polling) — без изменения топологии. В процесс добавляется:
- `src/services/recommendation.py` — новый (над CalculationService);
- `src/services/scheduler.py` — новый (APScheduler в процессе, единственный — design D19);
- `src/services/calculation.py` — РАСШИРЯЕТСЯ новыми функциями окон; контракт существующих `day_facts_lines` / `daily_summary` НЕ меняется (регресс PB-3/BUG-004 обязан остаться green);
- `src/handlers/hint.py`, `src/handlers/reminders.py` (или интеграция в `src/main.py`) — новые.

## Контракты

Telegram Bot API: новый callback `status:show` (ТЗ §10.3) + исходящие push (`bot.send_message` владельцу — §7.23/§7.24). Существующая callback-схема пакета 1 не меняется. Внутреннего HTTP API нет (IR-01 неприменим).

## Статика (static_v?)

Нет фронтенд-статики (IR-03 неприменим).

## Deploy-матрица и RUNBOOK

Миграций БД нет (remind_before/target_nap_duration засеяны пакетом 1). Deprecation-замечание: при рестарте процесса напоминания перестраиваются при старте (D19) — деплой волны 3 может отправить push повторно только если рестарт совпал с секундой отправки (принятый остаточный риск, design D19). RUNBOOK релизного цикла дополняет план деплоя пакета.

## Миграции

Нет. Схема пакета 1 (users/sleep_events/settings) не меняется; `sleep_type` CHECK — Пакет 3.

## Тесты (существующие — под изменением)

- `tests/test_events_now.py` — обновление: подтверждение пробуждения получает строку окна (FR-38, D20); снимается ассерт «без строки окна».
- `tests/test_backfill_success.py` — обновление: сводка §7.14-ред + строка окна.
- `tests/test_smoke_core.py` — обновление: тексты подтверждений с окнами.
- Регресс PB-3/BUG-004 (`test_daily_summary.py`, регресс-сверка фактов дня) — обязан остаться green без изменений (контракт задачи 1.1).
- Новые: `tests/test_recommendation_calc.py`, `tests/test_recommendation.py`, `tests/test_hint.py`, `tests/test_scheduler.py`, `tests/test_smoke_recommendation.py`. Автотесты — по approved TC-кейсам (`TC-<ДОМЕН>-NNN`), TC-трассировка в docstring (flow_check rule 6); test-model/case обновит qa_automation на своем этапе.
