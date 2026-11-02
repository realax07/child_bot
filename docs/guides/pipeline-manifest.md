# Манифест pipeline в планах (слой 1 диспетчера пакетов)

Раннер v0: `python3 scripts/package_runner.py <PLAN-файл>` (слой 2).
Секция `pipeline:` объявляется в YAML-блоке PLAN-<id>.md — план остается
единым артефактом ПМ, раннер лишь исполняет объявленное.

## Пример

````markdown
# PLAN-my-package: …

**Флоу: 4**. …

```yaml
pipeline:
  package: my-package
  flow: 4
  stages:
    - id: impl
      run: pi:dev
      zones: [backend/**, tests/**]
      inputs: [openspec/changes/my-package/spec.md]
      task: реализовать FR-1..FR-3 по спеке, тесты обязательны
    - id: check
      run: gate:python3 scripts/flow_check.py
    - id: review-plan
      run: hermes:pm
```
````

## Правила

- `run:` — ровно один из трех видов:
  - `pi:<роль>` — Pi роль-пак `skills/pm-factory/pi/role-packs/<роль>/`.
    Раннер создает worktree (`scripts/session_worktree.sh`) **от текущего
    HEAD репо** (не origin/main — иначе в сцене нет ветки ПМ с планом и
    карточками бага), кладет `role-agents.md` пака в корень worktree как
    `AGENTS.md`; если в worktree уже есть `AGENTS.md` проекта, проектный
    файл **не перезаписывается** — роль кладется рядом как `AGENTS.pi.md`
    с пометкой «читай вместе с AGENTS.md проекта» (Pi читает оба), бриф
    упоминает нужный файл. Задает `PI_ZONE_WRITE` из `zones` и вызывает
    `pi --no-session -e ~/.pi/agent/extensions/zone-guard.ts -p <бриф>`.
    Для pi-шага `zones` **обязателен**. Провайдер/модель берутся из
    `settings.example.json` пака (дефолт `zai-glm`/`glm-5.3`); ключ pi берет
    сам из `~/.pi/agent/models.json` — раннер секретов не касается.
  - `gate:<команда>` — shell-команда в worktree; ненулевой exit → STOP
    (exit 2, диагностика; воротные отказы НЕ ретраятся). Env стенда (E6 —
    `EKOTOV_WIKI_BASE_URL`, `EKOTOV_WIKI_DB_PATH`) срезается у gate-команды
    (эквивалент `env -u`): с ним pytest коллекция цепляет `tests/web`
    (нужен стенд) и падает с collection error. **Gate-команда сама отвечает
    за скоуп тестов** — пример узкого gate::

        run: gate:python3 -m pytest tests/gallery -q -k gallery
        # НЕ gate:python3 -m pytest tests/ -q: без стенда tests/web
        # коллекционируется и роняет ворота.
  - `stand:<команда>` — **подъем стенда** (J62): shell-команда в worktree
    (типовой пример — `bash scripts/qa_stand_up.sh reset`). Ненулевой exit →
    STOP. Из stdout парсятся строки `export VAR=value` (формат
    `scripts/qa_stand_up.sh`: ведущие пробелы и кавычки допустимы); ни
    одного export → STOP («стенд не отчитался env» — без env последующие
    стадии гарантированно красные). Отчитавшиеся переменные сохраняются в
    runner-state (`stand_env`) и **прокидываются в env всех последующих
    pi/gate стадий пакета** (при resume — тоже, stand-стадия не
    переисполняется). **Гашение стенда НЕ выполняется автоматически**:
    сироту подберет reaper/gc (`scripts/stand_reaper.py`) или ПМ гасит
    вручную (`bash scripts/qa_stand_up.sh stop` в worktree); после
    «докатан» раннер печатает напоминание.
  - `hermes:<роль>` — **стоп-точка ПМ**: раннер печатает
    `ЖДУ ПМ: stage <id>, роль <роль>` + состояние и выходит с кодом 0.
    Дальше — ручной диспатч ПМ (делегация, приемка, push); повторный запуск
    раннера после ПМ-действий докатывает пакет: hermes-шаг помечен в state
    статусом `waited` и пропускается (раннер факт ПМ-работы не проверяет —
    запускай повтор, когда ПМ-шаг реально сделан).
- `optional: true` (опциональные стадии, «шаги могут быть опциональными,
  типа дизайнера») — только для pi/gate-стадий: отказ НЕ останавливает
  пакет. Gate fail / verdict fail / pi fail / таймаут → стадия помечается
  в state статусом **`skipped`**, в журнал плана и stderr пишется
  **WARNING**, раннер идет дальше (при pi-успехе — обычный проход, включая
  gate/verdict). Обязательные стадии (без `optional`) — STOP как раньше.
  На resume skipped-стадии не переисполняются (аналог done/waited —
  ретраев нет). У stand/hermes ключ не действует: молчаливый пропуск
  stand-стадии только переносит гарантированный отказ глубже (env стенда
  нужен следующим стадиям), hermes — ручная точка ПМ. Типовое применение —
  design/design_check (задачи без фронт-дельты) и deploy (боевой деплой —
  вне автомата).
- Последовательность v0 — строгая, без параллелей; `depends[]`
  игнорируется с WARNING, если нарушает порядок.
- `gate` (опциональное поле pi-шага) — команда-проверка в worktree после
  успешного pi; ненулевой exit → STOP.
- `verdict` (опциональное поле pi/gate-стадии, J62) — **вердикт-ворота**:
  отображение `{file: путь, contains: маркер}`. После успешной стадии раннер
  проверяет, что файл (в worktree) содержит маркер — иначе STOP exit 2 с
  сообщением **«ЖДУ ПМ: возврат ревью»** (стадия в state — `stopped`; при
  повторном запуске стадия переисполняется, как gate-отказ — ретраев нет).
  Типовое применение — review-стадии: маркер «Одобрить» в review-файле
  (машиночитаемый вердикт; прозаический вердикт «по памяти» = задача НЕ
  покрыта воротами). Валидация манифеста требует оба поля непустыми::

      - id: code_review
        run: pi:code-reviewer
        zones: [test-model/reviews/**]
        verdict:
          file: test-model/reviews/my-package/review-001.md
          contains: "Одобрить"
- Таймаут вызова: env `PIPELINE_TIMEOUT` (сек), дефолт 1800; таймаут и
  ненулевой exit pi/gate/stand → STOP: exit 2, хвост stdout/stderr ≤30 строк.
- Состояние: `<план>.runner-state.json` рядом с планом (путь печатается
  в начале прогона и в диагностике STOP — файл не «теряется» после
  прогона); resume пропускает done/waited-стадии, чужой state (другой
  package/plan) — RUNNER-STOP. Хвост stdout/stderr каждой стадии
  (≤50 строк) сохраняется в state (`stdout_tail`/`stderr_tail`) и в лог
  `<план>.runner.log` — вывод успешных стадий тоже доступен после
  прогона. После каждого stage — строка в секцию «## Журнал раннера»
  плана.

### Опциональные стадии (`optional: true`)

Комментарий Заказчика: «шаги могут быть опциональными, типа дизайнера».
Ключ стадии `optional: true` — семантика для pi/gate-стадий:

| Отказ | Обязательная (по умолчанию) | `optional: true` |
|---|---|---|
| gate fail / gate-таймаут | STOP exit 2 | `skipped` + WARNING, дальше |
| pi fail / pi-таймаут | STOP exit 2 | `skipped` + WARNING, дальше |
| verdict fail (нет маркера) | STOP «ЖДУ ПМ: возврат ревью» | `skipped` + WARNING, дальше |
| pi + gate ok, verdict ok | `done` | `done` (обычный проход) |

- `skipped` пишется в state; в «Журнал раннера» плана — строка с WARNING;
  в лог `<план>.runner.log` — статус SKIPPED с хвостами stdout/stderr.
- resume: skipped-стадии НЕ переисполняются (аналог done/waited, ретраев нет).
- stand/hermes ключ игнорируют: stand без env стенда роняет следующие стадии
  (молчаливый пропуск только переносит отказ глубже), hermes — ручная точка.

## Полный пример манифеста Флоу 1 (требования → релиз)

Скелет конвейера Флоу 1 для пакета `my-package`: требования → approve →
создание change → архитектурное ревью → design/design_check (опциональные)
→ dev → код-ревью → QA-кейсы → ревью кейсов → автоматизация →
impact-анализ → регресс → архивация → релиз → deploy (опциональная,
релизная инфра). Стадии `hermes:` — ручные стоп-точки ПМ (диспатч
делегаций, приемка, push, merge); раннер их «докатывает» при повторном
запуске после ПМ-действий.

````markdown
# PLAN-my-package: <название пакета>

**Флоу: 1**. Спека: openspec/changes/my-package/. Пакет едет раннером v0.

```yaml
pipeline:
  package: my-package
  flow: 1
  stages:
    - id: requirements
      run: pi:ba
      zones: [requirements.md, docs/ba/**]
      inputs: [docs/backlog/my-package.md]
      task: черновик требований по брифу Заказчика
    - id: approve
      run: hermes:pm          # СТОП-ТОЧКА: ПМ выносит черновик требований
                              # Заказчику и вносит правки; repeat раннера —
                              # когда требования утверждены
    - id: create_change
      run: pi:sa              # СА пишет proposal/spec/design/tasks (зоны
      zones: [openspec/changes/my-package/**, sdd.md]   # openspec + sdd)
      inputs: [requirements.md, docs/ba/answers_round1.md]
      gate: openspec validate --strict
    - id: arch_review
      run: pi:architect
      zones: [test-model/reviews/**, architecture/**, docs/**]
      inputs: [openspec/changes/my-package/proposal.md,
               openspec/changes/my-package/design.md]
      verdict:
        file: test-model/reviews/my-package/arch-review-001.md
        contains: "Одобрить"
    - id: design              # дизайн-макеты/токены ДО кода (J36); опциональна:
      run: pi:ui-designer     # задачи без фронт-дельты — падение → skipped,
      zones: [design/**]      # пакет едет дальше (не STOP)
      inputs: [openspec/changes/my-package/design.md]
      optional: true
    - id: design_check
      run: pi:design-validator
      zones: [test-model/reviews/**]
      inputs: [design/]
      verdict:
        file: test-model/reviews/my-package/design-check-001.md
        contains: "Одобрить"
      optional: true          # отказ вердикта → skipped, не STOP
    - id: dev
      run: pi:dev
      zones: [backend/**, frontend/**, services/**, src/**, tests/**]
      inputs: [openspec/changes/my-package/spec.md,
               openspec/changes/my-package/design.md]
      gate: python3 scripts/flow_check.py
    - id: code_review
      run: pi:code-reviewer
      zones: [test-model/reviews/**]
      verdict:                # вердикт-ворота: нет «Одобрить» в файле —
        file: test-model/reviews/my-package/review-001.md   # STOP «ЖДУ ПМ:
        contains: "Одобрить"                                # возврат ревью»,
                              # ПМ возвращает дифф dev, потом repeat
    - id: qa_cases
      run: pi:qa-case-author
      zones: [test-model/new/**]
      inputs: [openspec/changes/my-package/spec.md,
               openspec/changes/my-package/sdd.md]
    - id: case_review
      run: pi:qa-case-reviewer
      zones: [test-model/reviews/**, test-model/approved/**]
      inputs: [test-model/new/my-package/]
      verdict:
        file: test-model/reviews/my-package/review-cases-001.md
        contains: "Одобрить"
    - id: qa_stand             # подъем стенда (J62): stdout стенда
      run: stand:bash scripts/qa_stand_up.sh reset   # (export EKOTOV_WIKI_*)
                              # прокидывается в env qa_auto/design/regress;
                              # гашение НЕ автоматом — reaper/gc подберет
    - id: qa_auto
      run: pi:qa-automation
      zones: [tests/**, test-model/**]
      inputs: [test-model/approved/my-package/]
    - id: impact
      run: pi:qa-impact-analyst
      zones: [test-model/impact/**]
      inputs: [openspec/changes/my-package/specs/,
               test-model/regression/manifest.md]
    - id: regress
      run: pi:regress-runner
      zones: [test-model/regress/**, tests/**]
      inputs: [test-model/impact/my-package.md]
    - id: archive
      run: hermes:pm          # СТОП-ТОЧКА: ПМ приемлет пакет (задачи tasks.md,
                              # мокапы в design/archive/, кейсы approved/),
                              # push ветки и PR
    - id: release
      run: hermes:pm          # СТОП-ТОЧКА: ПМ merge PR после зеленого CI,
                              # тег релиза, архивация change-пакета
    - id: deploy              # релизная инфра (devops, J62-остаток): runbook,
      run: pi:devops          # деплой-скрипты (бэкап/EXPECTED_COMMIT/смоук/
      zones: [deploy/**, docs/ops/**]       # DRY_RUN), миграции — в worktree.
      optional: true          # БОЕВОЙ деплой прод-стека — только по
                              # утверждению ПМ (hermes-стоп), накатку
                              # выполняет Заказчик; падение стадии →
                              # skipped + WARNING, пакет едет дальше
```
````

### Пояснение hermes-стопов

- `approve` — единственная точка воли Заказчика: утверждение требований.
  Раннер останавливается со статусом `waited`; повторный запуск — когда ПМ
  зафиксировал утвержденную спеку.
- `create_change` — pi:sa пишет change-пакет (зоны openspec + sdd), strict-валидация
  как gate; arch_review — pi:architect с вердикт-воротами «Одобрить».
- `design`/`design_check` — мокапы ДО кода и их валидация (J36), обе стадии
  `optional: true`: падение/отказ вердикта → skipped + WARNING, пакет едет
  дальше; задачи без фронт-дельты — стадии можно удалить (или отметка
  design-not-required).
- `deploy` — pi:devops (J62-остаток), `optional: true`: подготовка релизной
  инфры в worktree (runbook, деплой-скрипты, миграции, чек-листы). БОЕВОЙ
  деплой прод-стека — только по утверждению ПМ (hermes-стоп); живой прод
  devops читает, реальные накатки выполняет Заказчик.
- Эскалация `pi:debug` (СРЕДА/КОД/ТЕСТ) — НЕ стадия: ПМ запускает при застревании
  dev/qa-стадии параллельно источнику (J35).
- `archive`/`release` — ручные: приемка артефактов, push, PR/merge, тег.
  Раннер факт ПМ-работы не проверяет — repeat запускается, когда шаг
  реально сделан (иначе — повторное «ЖДУ ПМ» без вреда: стадия уже `waited`).
- Останов по verdict/gate/stand (`ЖДУ ПМ: …` на stderr, exit 2) — тоже
  ручной контур: разбор ПМ, правка (доработка dev, возврат ревью, гашение
  чужого стенда) и repeat раннера; остановленная стадия переисполняется
  целиком (ретраев нет).
