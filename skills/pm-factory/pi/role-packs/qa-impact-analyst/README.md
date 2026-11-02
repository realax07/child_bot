# Ролевой пак Pi: qa-impact-analyst (impact-аналитик регресса)

Пак роли `qa_impact_analyst` (`agents/qa_impact_analyst_agent.md`) для
исполнителя [Pi](https://pi.dev) (`@earendil-works/pi-coding-agent`), модель
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
test-model/impact/**
```

Ровно один файл отчета: `test-model/impact/<change-id>.md` (таблица
тест → дельта → вердикт keep/revalidate/retire + новые тестовые требования +
средовые требования прогона). Тесты, кейсы, спеки, код — read-only;
`git push` блокируется.

## Сборка на хосте

```bash
# 1. Ролевая инструкция → корень worktree
cp skills/pm-factory/pi/role-packs/qa-impact-analyst/role-agents.md /tmp/wt-<change>/AGENTS.md

# 2. Settings → агент-директория (ключ zai — отдельно в ~/.pi/agent/models.json, chmod 600)
cp skills/pm-factory/pi/role-packs/qa-impact-analyst/settings.example.json ~/.pi/agent/settings.json
```

## Запуск (print-режим, анализ дельт из брифа ПМ)

```bash
cd /tmp/wt-<change> && \
PI_ZONE_WRITE='test-model/impact/**' \
pi --provider zai-glm --model glm-5.3-flash --no-session \
   -e ~/.pi/agent/extensions/zone-guard.ts \
   -p "<бриф: change C123, дельты openspec/changes/C123/specs/, манифест test-model/regression/manifest.md. Вердикты keep/revalidate/retire по каждому затронутому тесту + новые тестовые требования + средовые требования → test-model/impact/C123.md>"
```

- Один пакет = одна сессия.
- Подводные камни Pi (project trust, sessionDir, jiti) — общий список в
  `../regress-runner/README.md` («Подводные камни»).
- Изоляция параллельных прогонов — worktree
  (`scripts/session_worktree.sh create`) + запись в
  `~/.hermes/state/active_sessions.json`, как у всех паков.
