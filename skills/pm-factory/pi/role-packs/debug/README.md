# Ролевой пак Pi: debug (эскалационный диагност, read-only)

Пак роли `debug` (`agents/debug_agent.md`) для исполнителя
[Pi](https://pi.dev) (`@earendil-works/pi-coding-agent`), модель
**glm-5.3-flash** (механический ярус, J59/J62). Оркестратор — всегда
Hermes/ПМ. Роль вне flow-графа: вызывается ПМ по эскалации `needs-debug:`.

## Состав

| Файл | Назначение |
|---|---|
| `role-agents.md` | Ролевая инструкция (выжимка фабричного промпта). При сборке — в корень worktree как `AGENTS.md` (или `AGENTS.pi.md`, если проектный уже есть). |
| `settings.example.json` | Дефолты сессии: `zai-glm`/`glm-5.3-flash`, tools `read/bash` (read-only диагностика — без edit/write), zone-guard. |

zone-guard НЕ входит в пак: используется установленный
`~/.pi/agent/extensions/zone-guard.ts` (общий для всех паков). Зоны — env
`PI_ZONE_WRITE` при вызове.

## Зона записи (PI_ZONE_WRITE)

```
манифест (glob-форма):       test-model/regress/**
env PI_ZONE_WRITE (префикс): test-model/regress
```

Обе строки задают ОДНУ зону: runner нормализует glob-хвосты зон манифеста в префикс-форму guard'а (`normalize_zone`, J61 — guard матчит зоны префиксом, не glob'ом). В env `PI_ZONE_WRITE` — префикс-форма (то, что прокидывает runner); в манифесте пакета (`stage.zones`) — glob-форма, как в гайде docs/guides/pipeline-manifest.md.

**Read-only диагностика**: записи в продукт нет (никаких коммитов/PR/правок
кода, спек, тестов — фикс текстом в отчете, применяет исходный dev).
Единственная зона записи — лог диагностики `test-model/regress/**`, и она
задается ЯВНО: без `PI_ZONE_WRITE` guard уйдет в дефолтные зоны, что для
read-only роли неверно. Прод — только чтение; временные стенды гасятся.

**Вердикт — один из трех классов: СРЕДА / КОД / ТЕСТ** (с доказательством:
минимальная репродукция + вывод), плюс предложение фикса текстом.

## Сборка на хосте

```bash
# 1. Ролевая инструкция → корень worktree
cp skills/pm-factory/pi/role-packs/debug/role-agents.md /tmp/wt-<change>/AGENTS.md

# 2. Settings → агент-директория (ключ zai — отдельно в ~/.pi/agent/models.json, chmod 600)
cp skills/pm-factory/pi/role-packs/debug/settings.example.json ~/.pi/agent/settings.json
```

## Запуск (print-режим, диагностика по эскалации ПМ)

```bash
cd /tmp/wt-<change> && \
PI_ZONE_WRITE='test-model/regress' \
pi --provider zai-glm --model glm-5.3-flash --no-session \
   -e ~/.pi/agent/extensions/zone-guard.ts \
   -p "<бриф: эскалация needs-debug от <роль>, симптом: <цитата отказа>, гипотеза автора: <...>. Изолированная репродукция → вердикт СРЕДА/КОД/ТЕСТ с доказательством + предложение фикса текстом. Лог диагностики: test-model/regress/>"
```

- Одна диагностика = одна сессия; итерационный бюджет отдельный (30).
- Подводные камни Pi (project trust, sessionDir, jiti) — общий список в
  `../regress-runner/README.md` («Подводные камни»).
- Изоляция параллельных прогонов — worktree
  (`scripts/session_worktree.sh create`) + запись в
  `~/.hermes/state/active_sessions.json`, как у всех паков.
