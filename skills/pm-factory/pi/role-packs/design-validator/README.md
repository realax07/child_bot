# Ролевой пак Pi: design-validator (валидатор дизайна)

Пак роли `design_validator` (`agents/design_validator_agent.md`) для
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
манифест (glob-форма):       test-model/reviews/**
env PI_ZONE_WRITE (префикс): test-model/reviews
```

Обе строки задают ОДНУ зону: runner нормализует glob-хвосты зон манифеста в префикс-форму guard'а (`normalize_zone`, J61 — guard матчит зоны префиксом, не glob'ом). В env `PI_ZONE_WRITE` — префикс-форма (то, что прокидывает runner); в манифесте пакета (`stage.zones`) — glob-форма, как в гайде docs/guides/pipeline-manifest.md.

Ровно один review-файл: `test-model/reviews/<change-id>/review-NNN-design.md`
(вердикт approve/return + таблица расхождений). Код/стили не правятся;
`git push` блокируется. Вердикт-строка `## Вердикт: Одобрить` /
`## Вердикт: Вернуть` — вердикт-ворота раннера (`stage.verdict`) ищут
«Одобрить» в файле, нет маркера → STOP «ЖДУ ПМ: возврат ревью».

## Сборка на хосте

```bash
# 1. Ролевая инструкция → корень worktree
cp skills/pm-factory/pi/role-packs/design-validator/role-agents.md /tmp/wt-<change>/AGENTS.md

# 2. Settings → агент-директория (ключ zai — отдельно в ~/.pi/agent/models.json, chmod 600)
cp skills/pm-factory/pi/role-packs/design-validator/settings.example.json ~/.pi/agent/settings.json
```

## Запуск (print-режим, валидация UI-дельты из брифа ПМ)

```bash
cd /tmp/wt-<change> && \
PI_ZONE_WRITE='test-model/reviews' \
pi --provider zai-glm --model glm-5.3-flash --no-session \
   -e ~/.pi/agent/extensions/zone-guard.ts \
   -p "<бриф: change C123, затронутые страницы/компоненты, пути эталона (design/README.md, design/tokens.html, утвержденный мокап), стенд по tests/README.md. Аудит покрытия стилей (computed styles), токены, состояния → review-файл test-model/reviews/C123/review-NNN-design.md с маркером «Одобрить»/«Вернуть»>"
```

- Одна валидация = одна сессия; стенд можно поднять стадией `stand:`
  манифеста пакета (env прокинется в pi-стадии автоматически) — см.
  `docs/guides/pipeline-manifest.md`.
- Подводные камни Pi (project trust, sessionDir, jiti) — общий список в
  `../regress-runner/README.md` («Подводные камни»).
- Изоляция параллельных прогонов — worktree
  (`scripts/session_worktree.sh create`) + запись в
  `~/.hermes/state/active_sessions.json`, как у всех паков.
