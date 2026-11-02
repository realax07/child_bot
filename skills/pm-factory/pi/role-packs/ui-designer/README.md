# Ролевой пак Pi: ui-designer (дизайнер UI)

Пак роли `ui_designer` (`agents/ui_designer_agent.md`) для исполнителя
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
design/**
```

Мокапы (standalone HTML), `design/tokens.html` (единый источник токенов,
J47), README-секция design. Продукт-код (`backend/`, `frontend/`),
`openspec/**`, `test-model/`, `tests/` — вне зон; `git push` блокируется
(пушит оркестратор после приемки Заказчиком).

## Сборка на хосте

```bash
# 1. Ролевая инструкция → корень worktree
cp skills/pm-factory/pi/role-packs/ui-designer/role-agents.md /tmp/wt-<change>/AGENTS.md

# 2. Settings → агент-директория (ключ zai — отдельно в ~/.pi/agent/models.json, chmod 600)
cp skills/pm-factory/pi/role-packs/ui-designer/settings.example.json ~/.pi/agent/settings.json
```

## Запуск (print-режим, дизайн-задача из брифа ПМ)

```bash
cd /tmp/wt-<change> && \
PI_ZONE_WRITE='design/**' \
pi --provider zai-glm --model glm-5.3-flash --no-session \
   -e ~/.pi/agent/extensions/zone-guard.ts \
   -p "<бриф: change C123, спека (Requirements/Scenarios к визуализации), какие экраны/состояния, формат выбора (2–3 варианта). Мокапы в design/ + design/tokens.html. Отчет: пути, покрытые Scenario, ≤3 решений на внимание Заказчика>"
```

- Одна задача = одна сессия; версии вариантов — в одном файле табами или
  отдельными файлами.
- Подводные камни Pi (project trust, sessionDir, jiti) — общий список в
  `../regress-runner/README.md` («Подводные камни»).
- Изоляция параллельных прогонов — worktree
  (`scripts/session_worktree.sh create`) + запись в
  `~/.hermes/state/active_sessions.json`, как у всех паков.
