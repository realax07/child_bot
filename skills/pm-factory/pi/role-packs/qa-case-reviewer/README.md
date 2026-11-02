# Ролевой пак Pi: qa-case-reviewer (ревьюер тест-кейсов)

Пак роли `qa_case_reviewer` (`agents/qa_case_reviewer_agent.md`) для
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
test-model/reviews/**
```

Review-файлы, **append-only партиии**: итерации нумеруются сквозно по пакету
(`test-model/reviews/<change-id>/review-NNN.md`), история не удаляется.
При вердикте «Одобрить» ревьюер переносит файлы партии в
`test-model/approved/<change-id>/` — для этого прогона вызывающий добавляет
зону:

```
PI_ZONE_WRITE='test-model/reviews/**:test-model/approved/**'
```

(без переноса — только `test-model/reviews/**`). Кейсы в `new/` ревьюер не
правит; `git push` блокируется. **Маркер «Одобрить» в вердикте обязателен**:
`## Вердикт: Одобрить` / `## Вердикт: Вернуть` — вердикт-ворота раннера
(`stage.verdict`) ищут «Одобрить» в файле, нет маркера → STOP
«ЖДУ ПМ: возврат ревью».

## Сборка на хосте

```bash
# 1. Ролевая инструкция → корень worktree
cp skills/pm-factory/pi/role-packs/qa-case-reviewer/role-agents.md /tmp/wt-<change>/AGENTS.md

# 2. Settings → агент-директория (ключ zai — отдельно в ~/.pi/agent/models.json, chmod 600)
cp skills/pm-factory/pi/role-packs/qa-case-reviewer/settings.example.json ~/.pi/agent/settings.json
```

## Запуск (print-режим, ревью партии из брифа ПМ)

```bash
cd /tmp/wt-<change> && \
PI_ZONE_WRITE='test-model/reviews/**:test-model/approved/**' \
pi --provider zai-glm --model glm-5.3-flash --no-session \
   -e ~/.pi/agent/extensions/zone-guard.ts \
   -p "<бриф: change C123, партия: список файлов test-model/new/C123/TC-*.md. Ревью файл-за-файлом, review-файл test-model/reviews/C123/review-NNN.md с маркером «Одобрить»/«Вернуть»; при «Одобрить» — перенос партии в test-model/approved/C123/>"
```

- Одна партия = одна сессия; ревью — файл за файлом, append в процессе.
- Подводные камни Pi (project trust, sessionDir, jiti) — общий список в
  `../regress-runner/README.md` («Подводные камни»).
- Изоляция параллельных прогонов — worktree
  (`scripts/session_worktree.sh create`) + запись в
  `~/.hermes/state/active_sessions.json`, как у всех паков.
