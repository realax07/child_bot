# Роль-паки Pi (skills/pm-factory/pi/role-packs)

Роль-паки для исполнителя [Pi](https://pi.dev) (`@earendil-works/pi-coding-agent`):
каждый пак переносит фабричную роль (`agents/<роль>_agent.md`) на внешнюю
модель через Pi. Структура пака: `role-agents.md` (системный промпт; при сборке
копируется в корень worktree/клона как `AGENTS.md`), `settings.example.json`
(дефолты сессии; ключ — отдельно в `~/.pi/agent/models.json`, chmod 600),
`README.md` (сборка и вызов). Оркестратор — всегда Hermes/ПМ.

**Zone-guard — общий для всех паков**: `regress-runner/extensions/zone-guard.ts`,
устанавливается один раз в `~/.pi/agent/extensions/`, зоны записи на вызове —
env `PI_ZONE_WRITE="зона1:зона2"` (относительные globs от cwd) + флаг
`-e ~/.pi/agent/extensions/zone-guard.ts`. Механическая гарантия — только для
`write`/`edit` (fail-closed); bash-канал эвристический, `git push` блокируется
всем ролям. Общие подводные камни Pi — в `regress-runner/README.md`.

## Оглавление паков

| Пак | Роль (фабричный промпт) | Модель | Провайдер | Зоны записи (PI_ZONE_WRITE) |
|---|---|---|---|---|
| [`regress-runner/`](regress-runner/README.md) | regress_runner (`agents/regress_runner_agent.md`) | **glm-5.3-flash** | zai-glm | `test-model/regress/<id>/**` (+ `tests/**` по брифу) |
| [`auditor/`](auditor/README.md) | pi-auditor (ручной вызов ПМ, вне конвейера) | **kimi k3.1-pro** | kimi | read-only (auditor-readonly) |
| [`sa/`](sa/README.md) | sa (`agents/sa_agent.md`) | **glm-5.3** | zai-glm | `openspec/changes/<id>/**:sdd.md` |
| [`architect/`](architect/README.md) | architect (`agents/architect_agent.md`) | **glm-5.3** | zai-glm | `architecture/**:docs/**:test-model/reviews/**` |
| [`dev/`](dev/README.md) | dev (`agents/dev_agent.md`) | **glm-5.3** | zai-glm | `backend/**:frontend/**:services/**:src/**:tests/**:e2e/**` |
| [`ba/`](ba/README.md) | ba (`agents/ba_agent.md`) | **glm-5.3** | zai-glm | `requirements.md:docs/ba/**` |
| [`code-reviewer/`](code-reviewer/README.md) | code_reviewer (`agents/code_reviewer_agent.md`) | **glm-5.3-flash** | zai-glm | `test-model/reviews/**` (маркер «Одобрить» в вердикте обязателен) |
| [`qa-case-author/`](qa-case-author/README.md) | qa_case_author (`agents/qa_case_author_agent.md`) | **glm-5.3-flash** | zai-glm | `test-model/new/**` (1 кейс = 1 файл TC-*.md) |
| [`qa-case-reviewer/`](qa-case-reviewer/README.md) | qa_case_reviewer (`agents/qa_case_reviewer_agent.md`) | **glm-5.3-flash** | zai-glm | `test-model/reviews/**:test-model/approved/**` (append-only партиии) |
| [`qa-automation/`](qa-automation/README.md) | qa_automation (`agents/qa_automation_agent.md`) | **glm-5.3-flash** | zai-glm | `tests/**:test-model/**` |
| [`qa-impact-analyst/`](qa-impact-analyst/README.md) | qa_impact_analyst (`agents/qa_impact_analyst_agent.md`) | **glm-5.3-flash** | zai-glm | `test-model/impact/**` |
| [`ui-designer/`](ui-designer/README.md) | ui_designer (`agents/ui_designer_agent.md`) | **glm-5.3-flash** | zai-glm | `design/**` |
| [`design-validator/`](design-validator/README.md) | design_validator (`agents/design_validator_agent.md`) | **glm-5.3-flash** | zai-glm | `test-model/reviews/**` (review-NNN-design.md) |
| [`debug/`](debug/README.md) | debug (`agents/debug_agent.md`, вне flow-графа) | **glm-5.3-flash** | zai-glm | read-only диагностика; лог — `test-model/regress/**` (вердикт СРЕДА/КОД/ТЕСТ) |
| [`devops/`](devops/README.md) | devops (`agents/devops_agent.md`) | **glm-5.3-flash** | zai-glm | `deploy/**:docs/ops/**` (боевой деплой — только по утверждению ПМ, hermes-стоп; пак готовит релизную инфру в worktree) |

Распределение моделей — регруп J59 + досборка flash-паков J62: семантический
ярус (SA/архитектор/DEV/BA) — glm-5.3, дешевые механические роли (QA-квартет,
ревьюеры, дизайнер, диагностика, devops) — glm-5.3-flash, большой контекст —
Kimi K3.1 (аудитор, вручную: `settings.example.json` единый, kimi/ro-guard —
переопределение флагами CLI при вызове).

Зоны в таблице — glob-форма манифеста; env `PI_ZONE_WRITE` задается в
префикс-форме (без glob-хвостов `/**`) — runner нормализует зоны манифеста
перед прокидкой (`normalize_zone`, J61); обе формы задают одну зону, в
README каждого пака показаны обе. Структура `settings.example.json` — единая
во всех паках (J62): `zai-glm` + `glm-5.3` (sa/architect/dev/ba) или
`glm-5.3-flash` (остальные), thinking `off`, tools `read/bash/edit/write`
(у debug — `read/bash`), `sessionDir` роли, zone-guard, quietStartup,
compaction, retry.

## Шаблон вызова

```bash
cd <worktree-или-клон> && \
PI_ZONE_WRITE='<зоны-роли>' \
pi --provider <провайдер> --model <модель> --no-session \
   -e ~/.pi/agent/extensions/zone-guard.ts \
   -p "<бриф>"
```

Плейсхолдеры (`<id>` и т.п.) вызывающий подставляет под конкретный пакет
(или правит env). Длинные задачи дробите: `-p` = «команда → результат».

## Синхронизация с фабричными промптами (анти-расползание)

Каждый `role-agents.md` — выжимка фабричного промпта `agents/<роль>_agent.md`,
в шапке несет маркер `<!-- base: agents/<роль>_agent.md@<коммит> -->`. Правило:
изменил фабричный промпт роли → обнови выжимку пака и переклей маркер. Проверка
расхождения — сравнение коммита в маркере с актуальным `git log -1 -- agents/<файл>`.
