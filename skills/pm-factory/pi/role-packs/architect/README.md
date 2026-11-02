# Ролевой пак Pi: architect (архитектор)

Пак роли `architect` (архитектор конвейера AI Factory) для исполнителя
[Pi](https://pi.dev) (`@earendil-works/pi-coding-agent`). Роль-образец:
`agents/architect_agent.md` (Hermes-сабагент); этот пак переносит ту же роль на
модель **glm-5.3** (семантический ярус, J59) через Pi. Оркестратор — всегда
Hermes/ПМ.

## Состав

| Файл | Назначение |
|---|---|
| `role-agents.md` | Ролевая инструкция (выжимка фабричного промпта). При сборке копируется в **корень worktree как `AGENTS.md`**. Полная версия роли — `agents/architect_agent.md` в корне клона. |
| `settings.example.json` | Дефолты сессии: провайдер/модель `zai-glm`/`glm-5.3`, tools `read/bash/edit/write`, `sessionDir`, подключение zone-guard. |

Расширение **zone-guard — НЕ входит в пак**: используется уже установленный
`~/.pi/agent/extensions/zone-guard.ts` (общий для всех паков, см.
`regress-runner/extensions/zone-guard.ts` — тот же файл). Зоны записи задаются
переменной `PI_ZONE_WRITE` при вызове.

## Зона записи (PI_ZONE_WRITE)

```
architecture/**:docs/**
```

Карта, ADR и проектная документация. Если бриф требует положить review-файл
вне этих зон (например `test-model/reviews/<change-id>/review-NNN-architecture.md`),
вызывающий ДОБАВЛЯЕТ путь в env под конкретный пакет:
`PI_ZONE_WRITE='architecture/**:docs/**:test-model/reviews/C123/**'`. Все
остальное — отказ zone-guard (`{ block: true }` на `write`/`edit`); `git push`
блокируется.

## Сборка на хосте

Detach-worktree выделяет ПМ (`git worktree add -b <ветка> <путь> origin/main`).

```bash
# 1. Ролевая инструкция → корень worktree
cp skills/pm-factory/pi/role-packs/architect/role-agents.md /tmp/wt-<change>/AGENTS.md

# 2. Settings → агент-директория (ключ zai — отдельно в ~/.pi/agent/models.json, chmod 600)
cp skills/pm-factory/pi/role-packs/architect/settings.example.json ~/.pi/agent/settings.json

# 3. Zone-guard — уже в ~/.pi/agent/extensions/ (общий, не копировать повторно)
```

## Запуск (print-режим, разовые задачи из брифа ПМ)

```bash
cd /tmp/wt-<change> && \
PI_ZONE_WRITE='architecture/**:docs/**:test-model/reviews/C123/**' \
pi --provider zai-glm --model glm-5.3 --no-session \
   -e ~/.pi/agent/extensions/zone-guard.ts \
   -p "<бриф: change C123, пути proposal/specs/design/tasks. Вердикт approve/return — в review-файле, строка «Карта: …» обязательна>"
```

- `-e zone-guard.ts` — обязательный флаг (fail-closed, отказ вне зоны —
  окончателен).
- Длинные задачи дробите: `-p` = «команда → результат».
- Подводные камни Pi (project trust, sessionDir, jiti и т.д.) — общий список в
  `../regress-runner/README.md` («Подводные камни»), для всех паков одинаков.

## Изоляция параллельных прогонов (обязательно)

Pi-прогон — полноценная сессия на репо: перед запуском выдели отдельный worktree
(`scripts/session_worktree.sh create <repo> <session-id>`, AGENTS.md правило 11)
и внеси зону роли в `~/.hermes/state/active_sessions.json` — реестр не знает о
Pi-прогонах сам, пересечение с делегацией видно только через запись. После
приемки — worktree снять, строку из реестра удалить.
