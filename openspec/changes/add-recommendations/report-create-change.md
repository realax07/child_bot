<!-- change: add-recommendations | project: child_bot -->

# Отчет SA: create_change add-recommendations

- **Дата:** 2026-10-31
- **Коммит:** один коммит на main — `[chore] change-пакет add-recommendations (Флоу 1): proposal/design/tasks/specs, волны 1-4` (SHA — в git log; в файле не фиксируется, чтобы не ломать требование «одним коммитом»)

## Сделано

- proposal.md — зачем/что меняется: бот-советник (окна сна, «⏰ Подсказка сейчас», автонапоминания); альтернативы 1–5; anti-scope (PB-1/PB-2/PB-5, UI настроек — Пакет 3; ОВ-4 — WAITING_PRODUCT); влияние на существующие подтверждения (возврат строк окон §7.2–7.4 в ред. PB-3)
- requirements.md — FR-26…FR-41 (нумерация после FR-25 add-core-diary), группы K–M–N; ОВ-4 раздел: ОВ-4.1 (push «пора будить» при перерастании цели дневного сна +10 мин), ОВ-4.2 (тихие часы ночного напоминания) — обе WAITING_PRODUCT
- research.md — конспект якорей ТЗ §3.1–3.6 (таблица A1–A7), логика §4.5–4.7; унаследованные решения: PB-3 (форма фактов), BUG-004 (формула бодрствования now − Подъем − Дневной сон), D6–D9/D14/D5 пакета 1; CalculationService — единый источник арифметики
- design.md — D16–D20 (продолжение нумерации D1–D15): D16 CalculationService (чистые функции, вход — пары (type, timestamp) naive в TZ пользователя + настройки, без БД; day_facts_lines/daily_summary не меняются); D17 RecommendationService (над CalculationService, формат §7.6/7.7, неполные данные без «0 ч»); D18 конфликты якорей (детерминированный компромисс, приоритет ночь > 15:30 > дневное окно); D19 планировщик APScheduler в процессе бота (единственный, пересчёт при событиях и старте, идемпотентный ре-шедул, push через bot.send_message владельцу); D20 возврат строк окон в подтверждения (факт + окно + сводка PB-3). Связи с D1/D2/D5/D6/D7/D8/D9/D10/D13/D14 пакета 1
- tasks.md — 4 волны, 10 задач: волна 1 расчёты (calculation.py расширяется, tests/test_recommendation_calc.py), волна 2 подсказки (recommendation.py, hint.py, регистрация в handlers/__init__.py, texts.py §7.6–7.9 + окна в подтверждения, обновления test_events_now/test_smoke_core), волна 3 напоминания (scheduler.py, reminders.py/интеграция в main.py, test_scheduler.py на фальшивых часах; задача 3.2 — WAITING_PRODUCT по ОВ-4), волна 4 smoke (test_smoke_recommendation.py). Зоны записи — точные пути; тестам — TC-<ДОМЕН>-NNN с TC-трассировкой в docstring (rule 6)
- specs/** — 4 дельты ADDED: recommendation-windows (окна дневного/ночного сна, отклонения эталона, конфликты якорей), hint-now (3 состояния, структура 5 пунктов, неполные данные, предупреждения, незавершённый сон), auto-reminders (push дневной/ночной, пересчёт, идемпотентность; ОВ-4.1/ОВ-4.2 — отдельные Requirements с пометкой WAITING_PRODUCT), diary-events (окна в подтверждениях §7.2–7.4 ред. PB-3 — дельта к домену пакета 1, добавлена сверх трёх названных в задании: возврат окон меняет подтверждённое поведение diary-events)

## Проверки

- `openspec validate add-recommendations --strict` → «Change 'add-recommendations' is valid» (exit 0, без ошибок и без warnings)
- `python3 scripts/flow_check.py .` → «flow_check: OK — порядок артефактов соответствует флоу» (exit 0; штатный FLOW-WARNING про устаревшие промпты ~/ai-factory — вне зоны пакета)
- Все файлы внутри зоны openspec/changes/add-recommendations/**

## Критерии готовности этапа

- [x] proposal/design/tasks/specs созданы по конвенции openspec (по образцу add-core-diary)
- [x] strict-валидация проходит без ошибок
- [x] трассировка задач на FR-26…FR-41 и решения D16–D20 полная
- [x] ОВ-4.1/ОВ-4.2 помечены WAITING_PRODUCT в requirements.md, specs/auto-reminders и tasks.md (задача 3.2)
