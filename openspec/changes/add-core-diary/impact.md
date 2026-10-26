# Impact-карта — add-core-diary

> Заполнена СА при create_change (J58). Дополняется ПМ при изменении дельт.

## Затронутые сервисы

Новый проект: единственный процесс бота `src/main.py` (PTB v21, long polling).
Слои handlers/services/repositories — создаются с нуля (см. design.md «Затрагиваемое»).
Смежных сервисов нет (микросервисной матрицы wiki проект не использует).

## Контракты

Внешний контракт — Telegram Bot API (callback-схема ТЗ §10.3: `event:sleep`,
`event:wake`, `event:sleep:manual`, `event:wake:manual`, `undo:last`,
`time:now`, `time:manual`, `conflict:overwrite`, `conflict:cancel`).
Внутреннего HTTP API нет; contracts/openapi.json не заводится (IR-01 неприменим).

## Статика (static_v?)

Нет фронтенд-статики (IR-03 неприменим).

## Deploy-матрица и RUNBOOK

Деплой — вне Пакета 1 (план: docker compose на нашем сервере рядом с wiki,
решение №7). Перед релизом пакета: compose-юнит + RUNBOOK (порядок выкатки/
отката, бэкап БД) — задача релизного цикла, не dev-задач. Миграций отката не
требуется (БД создается с нуля, схема additive).

## Миграции

Первичная схема v1 (`src/db.py`): users/sleep_events/settings + индекс
sleep_events(user_id, timestamp). Обратимость на этапе каркаса не требуется.

## Тесты

Новые домены: test_day_boundary, test_day_page, test_sleep_merge,
test_backfill, test_undo, test_events_now, test_start, test_state,
test_repositories (зоны задач см. tasks.md). Регресс-домены —
test-model/regress-domains.json (файл обновит qa_automation на своем этапе).
