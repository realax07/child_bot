---
name: factory-flow
description: Deterministic flow skeleton: stage maps, blockers, escalation rules.
version: 1.0.0
author: AI Factory
license: MIT
platforms: [hermes]
metadata:
  hermes:
    tags: [pipeline, flow-control, orchestration]
    related_skills: [implementation, openspec-authoring, case-review, test-automation, code-review]
---

# Детерминированный флоу: скелет и карта блокеров

Как пройти любую работу по конвейеру без «тыканья» в блокеры: знать карту стадий, заранее видеть проверки каждого шага и эскалировать по правилам, а не обходить. Скелет — общий для всех ролей; роль-специфика — в секции «Твой шаг в детерминированном флоу» твоего промпта. Источники истины: `docs/README-flow-control.md` (процесс), `agents/README.md` (таблица зон записи), `scripts/flow_transition.py` (STAGE_TABLE, коды причин), `scripts/session_check.py` (резервация).

## Общая рамка

```
Решение Заказчика (зафиксировано) → план с флоу → flowctl prepare
   → [ворота: ALLOW?] → reservation зоны → run (manual/delegate)
   → работа в worktree → finish → zone-check + gates (gate_runner)
   → provenance review → accepted / returned / blocked
```

## Карта стадий пяти флоу

**Флоу 1 — полный (фича):**
`approve_requirements` (заказчик/pm, требуется человеческое одобрение) → `create_change` (sa) → `architecture_review` (pm) → `dev_task` (dev) → `code_review` + `accept_review` (code_reviewer) → `merge_task` (dev_lead) → `qa_cases` (qa_case_author; спека → кейсы напрямую, без чеклиста — J23) → `qa_review` (qa_case_reviewer) → `qa_impact_analysis` (qa_impact_analyst) → `qa_automation` (qa_automation) → `archive_change` (sa/dev_lead) → `release` (pm, только после archive).

**Флоу 2 — багфикс:** `dev_task` по существующей спеке с BUG-NNN. Если фикс меняет ожидаемое поведение/API/схему (spec_delta) — обязательная эскалация: create_change (Флоу 1) или решение Заказчика. Молча расширить фикс = DENY.

**Флоу 3 — хотфикс:** `incident_ref` → `emergency_stabilize` (dev/pm, минимальный цикл) → ОБЯЗАТЕЛЬный последующий PR-цикл (dev_task → code_review → merge_task). Пока PR-цикл не закрыт — все post-emergency действия несут STALE_EVIDENCE (долг хотфикса). Deploy/rollback — только при существующем approval_ref; локальный PASS ≠ защита main (EXTERNAL_ENFORCEMENT_UNKNOWN).

**Флоу 4 — обслуживание:** `[chore]`-маркер → `chore_task` (dev) → code_review → merge_task. PR/review не обходится; изменение правил фабрики — отдельное решение Заказчика (rules_change), без него DENY.

**Флоу 5 — экспресс:** условия экспресс-режима из BACKLOG → минимальный цикл, но до закрытия обязательны ретроспективные артефакты: requirements + spec delta + кейсы. Без них закрытие = DENY.

## Роль → кто что делает

| Что | Кто | Как |
|---|---|---|
| Старт пакета, approve_requirements | Заказчик | решение фиксируется в задачах ПМ |
| Резервация зоны, flowctl prepare/run/finish, merge, release, provenance-запись | ПМ | до твоей работы: prepare + reserve; после: gates + приемка |
| create_change, archive_change | СА | после УТВЕРЖДЕН requirements; дельты слиты в master-spec |
| Реализация задачи, коммиты в свою ветку, PR | dev | только в резервированной зоне |
| Ревью кода, approve/return | code_reviewer | по SHA/диффу, независимая роль |
| Чеклист / кейсы / ревью кейсов / автотесты | QA-роли | по своей цепочке, вход — артефакт предыдущей стадии |

Ты исполняешь свою стадию; `prepare` до и `finish` с воротами после — работа ПМ. Не видишь резервации/зоны в задаче — эскалация ПМ, не самодеятельность.

## Карта блокеров (что встретишь и что делать)

| Проверка | Когда встретишь | Коды отказа | Твоя реакция |
|---|---|---|---|
| `flowctl prepare` / `flow_transition check` | старт любого действия | DENY / UNKNOWN; MISSING_INPUT, INVALID_GATE, HUMAN_APPROVAL_REQUIRED, WRONG_ROLE, STALE_EVIDENCE, STALE_SNAPSHOT | **Стоп и эскалация ПМ с кодом причины.** Не обходи, не повторяй «с другими аргументами», не считай UNKNOWN разрешением |
| `session_check reserve` | старт сессии | ZONE_CONFLICT (зону держит другая сессия), DUPLICATE_PAYLOAD | Не открывай свою зону «пор wider»: запроси у ПМ другую зону/разрешение конфликта |
| `session_check check` | приемка результата | OUT_OF_ZONE (diff вышел за резервированную зону, renames/symlink тоже считаются) | До finish проверь `git status`/changed paths сам; выход за зону = эскалация до report, а не после |
| `gate_runner run` (finish) | закрытие работы | FAIL/ERROR/SKIPPED (≠PASS) по gates: openspec strict, flow_check, pm_bounds, pr_validate | Ворота обязательны и не сокращаются: «запусти только часть» = нарушение. Красные ворота — эскалация с выводом команд |
| `gate_runner record-review/check` | provenance | STALE_EVIDENCE (SHA/diff_digest устарели), WRONG_ROLE (ревьюер = автор) | Ревью всегда по свежему SHA/диффу; сам себе ревьюер быть не можешь |
| PR в main | merge | ruleset отклоняет прямой push в main | Любая работа — через ветку + PR; merge делает ПМ/dev_lead после зеленых ворот |

## Железные правила

1. **Запрещено просить шире зоны.** Зона задана резервацией ПМ; нужно больше — запрос ПМ с обоснованием, не самовольный заход.
2. **Запрещено самому решать ворота.** DENY/UNKNOWN/FAIL не преодолеваются исполнителем — эскалация ПМ с кодом причины и фактическим выводом.
3. **UNKNOWN не разрешает исполнение** — это честное «не проверяемо», а не зеленый свет.
4. **Ничего не удаляется молча** (worktree, реестр, история ревью): падение процесса → ПМ через `flowctl reconcile`.
5. Повторный run без нового решения Заказчика откажет — не «повторяй до успеха».
