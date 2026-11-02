# Ролевой пак Pi: regress-runner

Пак роли `regress_runner` (QA-регресс-раннер конвейера AI Factory) для
исполнителя [Pi](https://pi.dev) (`@earendil-works/pi-coding-agent`). Роль-образец:
`agents/regress_runner_agent.md` (Hermes-сабагент); этот пак переносит ту же
роль на дешевую flash-модель через Pi. Оркестратор — всегда Hermes/ПМ.

## Состав

| Файл | Назначение |
|---|---|
| `role-agents.md` | Ролевая инструкция (системный промпт). При сборке копируется в **корень worktree как `AGENTS.md`** — Pi подхватывает его безусловно как контекст. |
| `settings.example.json` | Дефолты сессии: провайдер/модель `zai-glm`/`glm-5.3-flash`, tools `read/bash/edit/write`, `sessionDir`, подключение zone-guard. |
| `extensions/zone-guard.ts` | Расширение — механическое исполнение зон (см. ниже). |

## Что охраняет zone-guard (fail-closed)

Перехват `tool_call` в событийной модели Pi (формат — `export default function (pi: ExtensionAPI)`, TS без компиляции, грузит jiti):

1. **`write`/`edit`** — запись только в зонах относительно cwd сессии:
   `test-model/regress/**` и `tests/**` (переопределяется `PI_ZONE_WRITE="зона1:зона2"`).
   Все остальное — отказ с вежливой причиной.
2. **`bash`** — запрет `git push` (пушит ПМ после приемки артефакта).
3. **`bash`** — docker только на чтение (`ps/inspect/logs/images/version/info/stats/top`);
   мутации (`run/exec/rm/stop/build/compose up…`, `system prune` и т.п.) — отказ.

Каждый отказ: `{ block: true, reason }` модели + строка в
`test-model/regress/zone-guard.log` + `ui.notify` при наличии UI.

**Честное ограничение**: bash-канал — эвристика по строке команды; `mv`/`tee`/редиректы мимо зон guard не парсит. Механическая гарантия — для `write`/`edit`; секреты и прод в этот контур не давать (см. «Подводные камни»).

## Сборка на хосте

Работаем в detach-worktree, выделенном ПМ:

```bash
git -C <repo> worktree add -b <ветка> /tmp/wt-<change> origin/main
```

### 1. Ролевая инструкция → корень worktree

```bash
cp skills/pm-factory/pi/role-packs/regress-runner/role-agents.md \
   /tmp/wt-<change>/AGENTS.md
```

Pi читает `AGENTS.md` из рабочей директории (и родителей) как контекст
безусловно — **project trust на это не влияет**. Рядом может лежать
`AGENTS.override.md` — он заменит наш файл в той же директории, не кладите его.

### 2. Settings → агент-директория

```bash
cp skills/pm-factory/pi/role-packs/regress-runner/settings.example.json \
   ~/.pi/agent/settings.json
```

Ключ zai — отдельно, `~/.pi/agent/models.json` (chmod 600, не в git); ключ
должен быть из аккаунта с активным GLM Coding Plan, иначе 429
«Insufficient balance». Альтернатива — project-level: `.pi/settings.json` в
корне worktree, но тогда Pi потребует project trust (см. ниже).

### 3. Расширение → `~/.pi/agent/extensions/`

```bash
mkdir -p ~/.pi/agent/extensions
cp skills/pm-factory/pi/role-packs/regress-runner/extensions/zone-guard.ts \
   ~/.pi/agent/extensions/
```

Пользовательские расширения из `~/.pi/agent/extensions/` грузятся всегда и не
зависят от project trust (в отличие от расширений из `.pi/` проекта). Разовые
прогоны можно грузить флагом: `pi -e <path>/zone-guard.ts`.

### 4. Доверие проекту (project trust)

Pi требует trust-решение, когда находит в рабочей папке `.pi/settings.json`,
`.pi/extensions`, `.pi/skills`, `.pi/SYSTEM.md` и т.п. Для этого пака:
- **Ничего не кладите в `.pi/` worktree** — тогда trust-запроса не будет вовсе.
- Если положили (project-настройки/расширения), при первом запуске подтвердите
  trust или заранее: `pi config` / `defaultProjectTrust` (только в
  агент-директории settings), либо отвечьте на промпт.
- `defaultProjectTrust: "ask"` — дефолт; для автоматизации допустимо
  `"always"` **только** если вы контролируете содержимое worktree (свой
  worktree от origin/main — да; чужой каталог — нет).

### 5. Запуск

RPC-демон (долгая сессия, управление из Hermes/скриптов):

```bash
cd /tmp/wt-<change> && pi --mode rpc
```

Print-режим — разовые задачи из брифа ПМ (так гоняет конвейер):

```bash
cd /tmp/wt-<change> && pi --no-session \
  -p "Бриф: change C123, ветка X, коммит Y. Гони flowctl regress --change C123 --final. Вердикт и артефакт — в test-model/regress/C123/."
```

Длинные задачи дробите: `-p` = «команда → результат».

## Подводные камни / несовместимости Pi (проверено по доке pi 1.4.0, docs/latest)

1. **Project trust — не граница безопасности**: он лишь решает, грузить ли
   проектные `.pi/**`-ресурсы. Инструменты после старта работают с правами
   процесса Pi; trust не ограничивает tool calls. Механическую зону дает
   именно zone-guard, а не trust.
2. **`sessionDir` читается ДО trust-решения** — единственный project-ключ,
   который применится, даже если trust отклонен. Не кладите чувствительное в
   project `sessionDir`.
3. **`AGENTS.md` грузится без trust** — контекстные файлы не защищены trust-моделью; treat instructions as untrusted input.
4. **Print/JSON-режимы не имеют UI**: `ctx.ui.confirm/select` недоступны;
   интерактивные подтверждения в `-p` зависнут/не сработают. Zone-guard
   поэтому не спрашивает — блокирует молча и логирует (`ctx.hasUI` guard).
5. **`tool_call`-gate fail-closed у самого Pi**: ошибка обработчика расширения
   блокирует tool call — guard намеренно не бросает исключения, возвращает
   `{ block, reason }` только по политике.
6. **Bash-канал не парсится Pi в AST** — защита пути для bash принципиально
   эвристическая; для строгой изоляции используйте контейнер
   (docs/containerization.md), а zone-guard — как второй рубеж.
7. **Расширения в jiti, без tsc**: типовые ошибки всплывут при загрузке; для
   проверки загрузки используйте фактический запуск (smoke ниже), а не
   `node --check` (он не понимает TS).
8. **Модели — из `<agent-dir>/models.json`** = `~/.pi/agent/models.json`
   (НЕ `~/.pi/models.json`); `settings.json` не хранит ключей.
9. **`defaultTools` с голыми именами ЗАМЕНЯЕТ набор** (не дополняет):`+name`/`-name` — для частичных правок. В паке задан полный список `read/bash/edit/write`.
10. Чужие `pi install` пакеты запрещены политикой фабрики — расширения/паки только собственные файлы.

## Smoke-проверка (как в конвейере)

```bash
# песочница
mkdir -p /tmp/smoke-pi && cd /tmp/smoke-pi && git init -q \
  && mkdir -p tests test-model/regress/C123 src && echo t > tests/t.py \
  && git add -A && git commit -qm init
# отказ вне зоны:
pi -e <pack>/extensions/zone-guard.ts --no-session -p "Создай файл src/evil.txt со словом x инструментом write."
#   → инструмент write блокируется, в ответе причина zone-guard, файла нет,
#     в test-model/regress/zone-guard.log строка BLOCK.
# успех в зоне:
pi -e <pack>/extensions/zone-guard.ts --no-session -p "Создай файл test-model/regress/C123/notes.md со словом ok."
#   → файл создан.
```
