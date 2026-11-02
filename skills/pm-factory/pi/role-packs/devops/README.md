# Ролевой пак Pi: devops (релизная инфраструктура)

Пак роли `devops` (`agents/devops_agent.md`) для исполнителя
[Pi](https://pi.dev) (`@earendil-works/pi-coding-agent`), модель
**glm-5.3-flash** (механический ярус, J59/J62). Оркестратор — всегда
Hermes/ПМ.

**Оговорка о проде (важно)**: БОЕВОЙ деплой прод-стека — только по
утверждению ПМ (hermes-стоп: раннер встанет на `hermes:pm`, ПМ согласует с
Заказчиком, накатку выполняет Заказчик). Пак работает в worktree и готовит
релизную инфраструктуру: актуализация runbook, деплой-скрипты с бэкапом/
EXPECTED_COMMIT/смоуком/DRY_RUN, миграции, чек-листы проверок — то, что
Заказчик затем выполнит на проде. Живой прод — только чтение.

## Состав

| Файл | Назначение |
|---|---|
| `role-agents.md` | Ролевая инструкция (выжимка фабричного промпта). При сборке — в корень worktree как `AGENTS.md` (или `AGENTS.pi.md`, если проектный уже есть). |
| `settings.example.json` | Дефолты сессии: `zai-glm`/`glm-5.3-flash`, tools `read/bash/edit/write`, zone-guard. |

zone-guard НЕ входит в пак: используется установленный
`~/.pi/agent/extensions/zone-guard.ts` (общий для всех паков). Зоны — env
`PI_ZONE_WRITE` при вызове.

## Зона записи (PI_ZONE_WRITE)

```
манифест (glob-форма):       deploy/**:docs/ops/**
env PI_ZONE_WRITE (префикс): deploy:docs/ops
```

Обе строки задают ОДНУ зону: runner нормализует glob-хвосты зон манифеста в
префикс-форму guard'а (`normalize_zone`, J61 — guard матчит зоны префиксом,
не glob'ом). В env `PI_ZONE_WRITE` — префикс-форма (то, что прокидывает
runner); в манифесте пакета (`stage.zones`) — glob-форма, как в гайде
docs/guides/pipeline-manifest.md.

`deploy/**` — runbook и деплой-скрипты проекта; `docs/ops/**` — ops-доки
фабрики. Код продукта, тесты, спеки — вне зон, zone-guard отклоняет запись
(`{block: true}`); `git push` блокируется.

## Сборка на хосте

```bash
# 1. Ролевая инструкция → корень worktree
cp skills/pm-factory/pi/role-packs/devops/role-agents.md /tmp/wt-<change>/AGENTS.md

# 2. Settings → агент-директория (ключ zai — отдельно в ~/.pi/agent/models.json, chmod 600)
cp skills/pm-factory/pi/role-packs/devops/settings.example.json ~/.pi/agent/settings.json
```

## Запуск (print-режим, подготовка релизной инфры из брифа ПМ)

```bash
cd /tmp/wt-<change> && \
PI_ZONE_WRITE='deploy:docs/ops' \
pi --provider zai-glm --model glm-5.3-flash --no-session \
   -e ~/.pi/agent/extensions/zone-guard.ts \
   -p "<бриф: что деплоим/меняем, целевой коммит, границы. Факты инфры командами ДО скрипта; выход: deploy/RUNBOOK.md + deploy/deploy.sh (бэкап, EXPECTED_COMMIT, смоук, DRY_RUN); верификация bash -n/dry-run/git diff --stat>"
```

- Стадия манифеста — `deploy` c `optional: true` (см. гайд
  `docs/guides/pipeline-manifest.md`): падение/отказ — пакет едет дальше,
  разбор ПМ.
- Одна задача = одна сессия; прод-изменения — НЕ исполнять (только
  готовить); боевой деплой — после hermes-стопа по утверждению ПМ.
- Подводные камни Pi (project trust, sessionDir, jiti) — общий список в
  `../regress-runner/README.md` («Подводные камни»).
- Изоляция параллельных прогонов — worktree
  (`scripts/session_worktree.sh create`) + запись в
  `~/.hermes/state/active_sessions.json`, как у всех паков.
