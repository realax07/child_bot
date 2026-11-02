# Ролевой пак Pi: qa-automation (автоматизатор тест-кейсов)

Пак роли `qa_automation` (`agents/qa_automation_agent.md`) для исполнителя
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
tests/**:test-model/**
```

`tests/**` — автотесты и `tests/README.md`; `test-model/**` — баг-репорты
в `test-model/bugs/` (approved-кейсы и review — read-only вход). Код продукта,
спеки, `requirements.md` — вне зон; `git push` блокируется. Вход — только
кейсы `test-model/approved/<change-id>/` с вердиктом «Одобрить»: кейсы из
`new/` — не вход, работа не начинается.

## Сборка на хосте

```bash
# 1. Ролевая инструкция → корень worktree
cp skills/pm-factory/pi/role-packs/qa-automation/role-agents.md /tmp/wt-<change>/AGENTS.md

# 2. Settings → агент-директория (ключ zai — отдельно в ~/.pi/agent/models.json, chmod 600)
cp skills/pm-factory/pi/role-packs/qa-automation/settings.example.json ~/.pi/agent/settings.json
```

## Запуск (print-режим, пакет тестов из брифа ПМ)

```bash
cd /tmp/wt-<change> && \
PI_ZONE_WRITE='tests/**:test-model/**' \
pi --provider zai-glm --model glm-5.3-flash --no-session \
   -e ~/.pi/agent/extensions/zone-guard.ts \
   -p "<бриф: change C123, кейсы test-model/approved/C123/, sdd.md. Автотесты в tests/api|tests/web, TC-ID в docstring, tests/README.md с матрицей кейс→тест. Прогон: зеленый или каждое красное = баг-репорт в test-model/bugs/>"
```

- Пакет дроби: `-p` = «команда → результат» (партия кейсов за прогон).
- Стенд для web-тестов можно поднять стадией `stand:` манифеста пакета
  (env стенда раннер прокинет в pi-стадии автоматически) — см.
  `docs/guides/pipeline-manifest.md`.
- Подводные камни Pi (project trust, sessionDir, jiti) — общий список в
  `../regress-runner/README.md` («Подводные камни»).
- Изоляция параллельных прогонов — worktree
  (`scripts/session_worktree.sh create`) + запись в
  `~/.hermes/state/active_sessions.json`, как у всех паков.
