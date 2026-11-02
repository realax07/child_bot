# Ролевой пак Pi: qa-case-author (автор тест-кейсов)

Пак роли `qa_case_author` (`agents/qa_case_author_agent.md`) для исполнителя
[Pi](https://pi.dev) (`@earendil-works/pi-coding-agent`), модель
**glm-5.3-flash** (механический ярус, J59/J62). Оркестратор — всегда
Hermes/ПМ.

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
манифест (glob-форма):       test-model/new/**
env PI_ZONE_WRITE (префикс): test-model/new
```

Обе строки задают ОДНУ зону: runner нормализует glob-хвосты зон манифеста в префикс-форму guard'а (`normalize_zone`, J61 — guard матчит зоны префиксом, не glob'ом). В env `PI_ZONE_WRITE` — префикс-форма (то, что прокидывает runner); в манифесте пакета (`stage.zones`) — glob-форма, как в гайде docs/guides/pipeline-manifest.md.

Только новые кейсы: `test-model/new/<change-id>/TC-<domain>-NNN.md` —
**1 кейс = 1 файл** (ревьюер грузит кейсы по одному, доработки — точечным
дифом одного файла). Перенос в `approved/` запрещен — право ревьюера
(qa-case-reviewer); zone-guard отклоняет запись вне зоны, `git push`
блокируется.

## Сборка на хосте

```bash
# 1. Ролевая инструкция → корень worktree
cp skills/pm-factory/pi/role-packs/qa-case-author/role-agents.md /tmp/wt-<change>/AGENTS.md

# 2. Settings → агент-директория (ключ zai — отдельно в ~/.pi/agent/models.json, chmod 600)
cp skills/pm-factory/pi/role-packs/qa-case-author/settings.example.json ~/.pi/agent/settings.json
```

## Запуск (print-режим, разовая задача из брифа ПМ)

```bash
cd /tmp/wt-<change> && \
PI_ZONE_WRITE='test-model/new' \
pi --provider zai-glm --model glm-5.3-flash --no-session \
   -e ~/.pi/agent/extensions/zone-guard.ts \
   -p "<бриф: change C123, пути спеки (openspec/changes/C123/specs/) и sdd.md. Развернуть каждый Scenario в TC-*.md в test-model/new/C123/, трассировка обязательна. Отчет: список TC-ID → Scenario>"
```

- Один пакет кейсов = одна сессия; большой пакет дроби по доменам спеки.
- Подводные камни Pi (project trust, sessionDir, jiti) — общий список в
  `../regress-runner/README.md` («Подводные камни»).
- Изоляция параллельных прогонов — worktree
  (`scripts/session_worktree.sh create`) + запись в
  `~/.hermes/state/active_sessions.json`, как у всех паков.
