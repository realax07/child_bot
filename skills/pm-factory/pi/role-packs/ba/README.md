# Ролевой пак Pi: ba (бизнес-аналитик)

Пак роли `ba` (бизнес-аналитик конвейера AI Factory) для исполнителя
[Pi](https://pi.dev) (`@earendil-works/pi-coding-agent`). Роль-образец:
`agents/ba_agent.md` (Hermes-сабагент); этот пак переносит ту же роль на
модель **glm-5.3** (семантический ярус, J59) через Pi. Оркестратор — всегда
Hermes/ПМ.

## Состав

| Файл | Назначение |
|---|---|
| `role-agents.md` | Ролевая инструкция (выжимка фабричного промпта). При сборке копируется в **корень worktree как `AGENTS.md`**. Полная версия роли — `agents/ba_agent.md` в корне клона. |
| `settings.example.json` | Дефолты сессии: провайдер/модель `zai-glm`/`glm-5.3`, tools `read/bash/edit/write`, `sessionDir`, подключение zone-guard. |

Расширение **zone-guard — НЕ входит в пак**: используется уже установленный
`~/.pi/agent/extensions/zone-guard.ts` (общий для всех паков, см.
`regress-runner/extensions/zone-guard.ts` — тот же файл). Зоны записи задаются
переменной `PI_ZONE_WRITE` при вызове.

## Зона записи (PI_ZONE_WRITE)

```
манифест (glob-форма):       requirements.md:docs/ba/**
env PI_ZONE_WRITE (префикс): requirements.md:docs/ba
```

Обе строки задают ОДНУ зону: runner нормализует glob-хвосты зон манифеста в префикс-форму guard'а (`normalize_zone`, J61 — guard матчит зоны префиксом, не glob'ом). В env `PI_ZONE_WRITE` — префикс-форма (то, что прокидывает runner); в манифесте пакета (`stage.zones`) — glob-форма, как в гайде docs/guides/pipeline-manifest.md.

Только два артефакта роли. Черновики — в диалоге; запись в репозиторий —
только согласованные версии (черновик → подтверждение Заказчика → запись).
Все остальное — отказ zone-guard (`{ block: true }` на `write`/`edit`);
`git push` блокируется.

## Сборка на хосте

BA — диалоговая роль (plan-write): ей обычно хватает клона репозитория;
worktree по брифу ПМ, если работа идет в пакете.

```bash
# 1. Ролевая инструкция → корень клона/worktree
cp skills/pm-factory/pi/role-packs/ba/role-agents.md <клон>/AGENTS.md

# 2. Settings → агент-директория (ключ zai — отдельно в ~/.pi/agent/models.json, chmod 600)
cp skills/pm-factory/pi/role-packs/ba/settings.example.json ~/.pi/agent/settings.json

# 3. Zone-guard — уже в ~/.pi/agent/extensions/ (общий, не копировать повторно)
```

## Запуск (print-режим, разовые задачи из брифа ПМ)

```bash
cd <клон> && \
PI_ZONE_WRITE='requirements.md:docs/ba' \
pi --provider zai-glm --model glm-5.3 --no-session \
   -e ~/.pi/agent/extensions/zone-guard.ts \
   -p "<бриф: раунд вопросов N по requirements.md, ответы Заказчика дословно (дата, цитаты) → docs/ba/answers_roundN.md, изменения → requirements.md>"
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
