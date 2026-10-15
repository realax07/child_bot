# Контракт компонентов Flow Control (детерминированный слой конвейера)

> Поставка 01 change-пакета `add-deterministic-flow` (ТЗ `docs/chatgpt-deterministic-flow/01-contract-and-boundaries.md`).
> Нормативная спека: `openspec/changes/add-deterministic-flow/specs/deterministic-flow/spec.md`.
> Этот документ — контракт компонентов: интерфейсы, данные, коды причин, границы ответственности. Он **не** реализует и не запускает агентов.

## 1. Назначение и границы

Flow Control — исполняемая модель состояния и переходов. По репозиторию, Git и реестру сессий компоненты отвечают на вопросы: что уже доказано (evidence), какие действия разрешены, какой агент может их выполнить и почему действие заблокировано. Запуск агента — отдельный слой (Orchestrator) НАД этими решениями, не внутри них.

**Что контракт НЕ делает:**

1. Не заменяет `flow_check.py`, `pm_bounds_check.py`, `pr_validate.py`, OpenSpec и тесты — они остаются независимыми deterministic gates (раздел 12).
2. Не делает автоматический push, merge, release, deploy и не стартует новую крупную фазу: этапные ворота Заказчика из `AGENTS.md` обязательны и выражаются кодом `HUMAN_APPROVAL_REQUIRED`.
3. Не объявляет декларативные правила `AGENTS.md`/`artifact_contract.md` «автоматически обеспеченными»: пока проверка не реализована, соответствующее решение возвращает `UNKNOWN`/`DENY`, а не PASS.
4. Не хранит токены, секреты и полные промпты; execution log содержит ссылки и digest.
5. Первый MVP: Python stdlib и существующие команды. Без LangGraph/CrewAI/Temporal/БД событий/YAML-языка состояний.

## 2. Приоритет источников (нормативная лестница)

При конфликте источников действует порядок; конфликт фактов между собой = блокировка с объяснением (`AMBIGUOUS_STATE`), не выбор «по preference».

| Уровень | Источник | Статус | Примеры |
|---|---|---|---|
| 1 (нормативные правила) | Решения Заказчика (зафиксированные), спеки `openspec/specs/` + дельты | Норма | «погнали/запускай» в зафиксированном виде, УТВЕРЖДЕН в requirements.md, Requirement/Scenario |
| 2 (наблюдаемые факты) | Git, файлы, реестр сессий (`~/.hermes/state/active_sessions.json`), результаты gates/CI | Evidence | коммиты, наличие артефактов, approve-вердикты, PASS/FAIL |
| 3 (навигация) | Сообщения агентов | Только указатель на evidence уровня 2 | «отчет готов, артефакты в …» |

Правило: самоотчет агента не является доказательством. Сообщение агента валидно только как путь/ссылка на факт уровня 2, который компонент проверяет сам. Невозможность прочитать источник, неоднозначность, отсутствующее evidence и ошибка gate дают `DENY`/`UNKNOWN`, никогда — разрешение по умолчанию.

## 3. Scope

Состояние вычисляется **по области**, а не одним линейным статусом репозитория. Параллельные задачи и QA-ветки допустимы (правило 10–11 `AGENTS.md`).

```
Scope {
  repo_path: str          # канонический путь репозитория (Path.resolve())
  project: str            # ID проекта (как в реестре active_sessions.json)
  flow: int ∈ {1,2,3,4,5} # выбранный флоу; фиксируется в плане задачи (AGENTS.md, распоряжение 2026-09-20)
  change_id: str | None   # change-id (kebab-case) | BUG-NNN | chore-идентификатор
  task_id: str | None     # номер задачи tasks.md (N[.N...]), когда действие про конкретную задачу
}
```

Правила:

- Неизвестный `flow` (вне 1–5) и неоднозначный scope (например, scope указывает на change, которого нет ни в `openspec/changes/`, ни в `archive/`, либо на задачу вне `tasks.md` своего change) **блокируют действие**: `AMBIGUOUS_STATE` / `MISSING_INPUT` соответственно.
- Флоу обязан быть зафиксирован в плане задачи до старта работ; отсутствие фиксации = `MISSING_INPUT` на любом действии уровня фазы.

## 4. Общий интерфейс

```
inspect(repo, scope) -> FlowSnapshot          # чистая операция
check_action(snapshot, action) -> Decision    # чистая операция
admit_session(decision, request) -> Reservation   # меняет состояние (реестр сессий)
run_gates(scope, phase) -> GateReport         # меняет состояние (запускает проверки)
```

**Side effects (документируются отдельно и честно):**

| Операция | Чистота | Читает | Пишет |
|---|---|---|---|
| `inspect` | Чистая: без записей, fetch, checkout, запуска агентов; повторный вызов на неизменном вводе дает эквивалентную выдачу (временные поля исключены из digest) | Git (read-only), файлы, реестр | ничего |
| `check_action` | Чистая: детерминированная функция снимка | только `snapshot` + `action` | ничего |
| `admit_session` | Нечистая: добавляет/обновляет запись в реестре сессий (вне git), резервирует зону записи | decision, реестр | `~/.hermes/state/active_sessions.json` |
| `run_gates` | Нечистая: исполняет gates, результат фиксируется | репо, конфигурация gates | gate-отчет / audit-log |

## 5. FlowSnapshot

```
FlowSnapshot {
  schema_version: str
  scope: Scope
  repo_head: str                 # git rev-parse HEAD
  dirty_paths: list[str]         # git status --porcelain
  registry_digest: str           # digest active_sessions.json (или unknown-маркер)
  facts: list[Fact]              # key, value, source, observed_at, fingerprint,
                                 # confidence ∈ {verified, unknown}
  problems: list[Problem]        # что не удалось прочитать/однозначно определить
  snapshot_digest: str           # digest снимка без временных полей
}
```

- Каждый факт несет `source` (путь/команда) и `confidence`. Факт, который не удалось прочитать, — `unknown` с записью в `problems`, а НЕ «отсутствует» и НЕ «пусто».
- Отсутствующий/битый `active_sessions.json` → факт `unknown` + проблема; система не трактует это как «активных сессий нет».
- `snapshot_digest` — ожидаемая версия входа для `check_action` (см. `STALE_SNAPSHOT`).

## 6. ActionRequest и Decision

```
ActionRequest (входит в action) {
  actor_role: str          # роль из agents/README.md (dev, code_reviewer, qa_case_reviewer, pm, …)
  project: str
  scope: Scope
  requested_action: str    # имя из графа действий (раздел 7)
  approval_ref: str | None # ссылка на решение Заказчика, если оно обязательно для действия
  expected_snapshot_digest: str | None  # ожидаемая версия входного снимка
}

Decision {
  allowed: bool
  status: ALLOW | DENY | UNKNOWN      # UNKNOWN никогда не разрешает исполнение
  action, scope, actor_role
  snapshot_digest: str                # снимок, на котором вынесено решение
  blocking_reasons: list[ReasonCode]  # стабильные коды (раздел 9)
  details: list[str]                  # человекочитаемо, с путями к evidence
  required_gates: list[str]
  evidence_refs: list[str]            # пути/команды, где лежит evidence
  next_candidates: list[ActionCandidate]  # допустимые следующие действия
}
```

Правила валидации запроса:

- `actor_role`, `project`, `scope`, `requested_action` обязательны; нехватка → `MISSING_INPUT`.
- `expected_snapshot_digest` ≠ `snapshot.digest` → `DENY` c `STALE_SNAPSHOT` (снимок устарел — вызвать `inspect` заново).
- Решение Заказчика (approval_ref): принимается только **явно зафиксированное** подтверждение, относящееся к данному scope/фазе (формат раздела 10). Текст «ПМ считает согласованным» решением не является → `HUMAN_APPROVAL_REQUIRED`. Разрешение ограничено указанной фазой и не переносится после её закрытия (решение фазы A не покрывает действие фазы B → `HUMAN_APPROVAL_REQUIRED`).

## 7. Граф действий (по флоу, не один State(Enum) на репозиторий)

Граф описан как действия с предусловиями; для bug/hotfix/chore/express — собственные условия и исключения из действующих контрактов. Сокращения ролей — по `agents/README.md`.

### Флоу 1 (полный: requirements → change → dev → QA → archive → release)

| Действие | Роль | Предусловия (evidence) | Обязательное решение Заказчика | Gates |
|---|---|---|---|---|
| approve_requirements | Заказчик (через ПМ) | requirements.md ГОТОВ К УТВЕРЖДЕНИЮ rN; answers_round*.md всех раундов (контракт 1) | да (дословное «подтверждаю») | flow_check C1/E3 |
| create_change | sa | requirements.md УТВЕРЖДЕН rN | да (старт нового change-пакета) | openspec validate --strict |
| architecture_review | ПМ + валидация | change-пакет: proposal/design/tasks/specs-дельты, sdd.md, research.md (контракт 2) | нет (внутри запущенного change) | flow_check контракт 2 |
| dev_task | dev | architecture review пройден; задача tasks.md; [P]/зависимости; зона свободна | нет | flow_check; pm_bounds (J9/J10) |
| code_review | code_reviewer (≠ автор) | diff задачи; delegation автора | нет | review-файл с вердиктом (формат code_reviewer_agent.md) |
| merge_task | интегратор/dev-lead (не ПМ: J9) | approve-вердикт + provenance-sidecar (task/change/SHA, независимая роль); легаси-review — compatibility UNKNOWN, не новый auto-merge | нет | pm_bounds_check --require-review; pr_validate; branch protection (внеш.) |
| qa_cases → qa_review → qa_automation | qa_* | цепочка контрактов 4–6: спека → new → approved → tests (remove-qa-checklist: этап qa_checklist упразднен, кейсы пишутся по спеке напрямую) | нет | flow_check (порядок new/approved/tests, трассировка TC → Requirement/Scenario) |
| ops_task (боевая/операционная задача: деплой-действия, переключение прода, миграция данных в бою) | ПМ + devops, **Заказчик обязателен** | задача tasks.md с маркером `[ops]`; план отката; приемка Заказчика зафиксирована (протокол: дата, что проверено, дословное «ок») | да (приемка) | протокол приемки в PR/коммите; J10 НЕ применяется (диффа кода нет — ревьюеру нечего смотреть) |
| docs_task (документация по факту: map/sdd/RUNBOOK) | sa/ПМ | задача tasks.md с маркером `[docs]`; факты свергнуты с боем/логами | нет | J10 НЕ применяется; факты верифицируются источником (логи, прогоны), ссылка в коммите |
| archive_change | sa | все задачи [x]; QA-контур завершен; явное разрешение ПМ (контракт 7); дельты слиты | нет (фаза еще открыта) | flow_check контракт 7; openspec validate |
| release / новая фаза релиза | ПМ | archive завершен | **да** (старт релиза/фазы) | — |

Решение Заказчика 3.3-А (2026-10-02, shadow-R6 S7; долг m10): **архивация — функция
`sa`** (автор спек сливает дельты), а не dev_lead/integrator; последние остаются
допустимыми, где это соответствует роли исполнителя. Практика Р6 (sa заархивировал
f925a57 с validate 13/13 strict и слитыми дельтами) признана корректной; граф и
`STAGE_TABLE` (flow_transition.py) приведены в соответствие. Порядок Флоу 1 — строго
`archive_change` → `release` (решение 3.2-Б: релиз строго после архивации).

Пример DENY: `dev_task` при утвержденных требованиях, но без architecture review → `INVALID_GATE` + деталь «пропущен этап architecture review, evidence: openspec/changes/<id>/ …». Параллельные задачи: обе с `[P]`, зависимости выполнены, зоны не пересекаются → обе в `next_candidates`; пересечение зон → `ZONE_CONFLICT`.

### Флоу 2 (баг-фикс)

- Вход: `BUG-NNN` репорт в `test-model/bugs/` + **существующая спека**, описывающая ожидаемое поведение.
- Исключения из Флоу 1: без БА/СА-цикла; QA-контур по объему бага (решение ПМ в границах уже запущенной фазы).
- Условие эскалации: если фикс вводит **новое ожидаемое поведение/API** (дельта спеки) → действие по Флоу 2 запрещено, `DENY` с указанием правила эскалации (переход на Флоу 1 / решение Заказчика). Эскалация — не «запрет и тишина»: decision содержит `next_candidates = [create_change, escalate_to_customer]`.

### Флоу 3 (хотфикс/аварийная стабилизация)

- Разрешает прямую стабилизацию на продукте при инциденте; **обязательный последующий PR-цикл** (review + pr_validate) — ретроспективно закрывает обход.
- Пока PR-цикл не закрыт, все последующие действия этого scope получают `STALE_EVIDENCE`-пометку незакрытого долга (хотфикс без PR не считается завершенным).

### Флоу 4 (обслуживание)

- Обслуживание фабрики/проекта **без обхода PR/review**; изменение правил конвейера — change-пакетом по решению Заказчика.
- `chore`-PR не имеет права менять `openspec/` (изменение as is только через change-пакет, контракт 7; проверяет `pr_validate` check_chore).
- Коммиты, трогающие защищенные пути (`openspec/specs/`, `contracts/`, `AGENTS.md`, `agents/README.md`), требуют `[pipeline]`-маркер в subject (pm_bounds_check, граница J3).

### Флоу 5 (экспресс)

- Разрешен только по условиям BACKLOG/README (мелкие изменения, ограниченный объем) и **требует ретроспективные артефакты до закрытия** (мини-объяснение, трассировка). До их появления действие закрытия пакета → `DENY`/`STALE_EVIDENCE`. Урок R1.3: фича по Флоу 2 с проскоченным QA закрыта пост-фактум Флоу 5 — это исключение, не норма.

### Параллель и зоны

- `[P]`-задачи: параллельно при выполненных зависимостях и **непересекающихся зонах записи** (таблица зон в `agents/README.md`); при >1 активной сессии — свой git worktree на сессию (`scripts/session_worktree.sh`, правило 11 AGENTS.md).
- `admit_session` проверяет пересечение зоны новой сессии с активными записями реестра: пересечение → `ZONE_CONFLICT` (и тогда — строго последовательно).

## 8. Session Admission: admit_session(decision, request) -> Reservation

```
Reservation {
  reservation_id, scope, actor_role, zone_paths: list[str],
  worktree: str | None, expires_at, decision_digest
}
```

- Вызывается **только** при `decision.status == ALLOW` (ALLOW от UNKNOWN/DENY отличает поле `allowed`); решение не из текущего снимка (`digest` не совпал) → отказ `STALE_SNAPSHOT`.
- Резервирует зону записи (добавляет запись в реестр: delegation_id, role, project, owner_pm, status — обязательные поля pm_bounds_check J2).
- Приемка/завершение сессии — удалить запись (снятие reservation). Реестр — вне git; его отсутствие/битость не считается «сессий нет» (см. раздел 5).

## 9. Стабильные коды причин

| Код | Значение | Типовой источник | Человекочитаемая деталь обязана содержать |
|---|---|---|---|
| `MISSING_INPUT` | Отсутствует обязательный вход/факт/evidence | inspect, check_action | путь к ожидаемому evidence (например `test-model/checklists/<id>.md: отсутствует, контракт 4`) |
| `INVALID_GATE` | Пропущен обязательный этап/ворота порядка | check_action | какой этап пропущен и где это видно |
| `HUMAN_APPROVAL_REQUIRED` | Нет зафиксированного решения Заказчика для этого scope/фазы | check_action | какое действие, какую фазу, где искать формат решения (раздел 10) |
| `WRONG_ROLE` | Действие зарезервировано другой ролью (включая ревью собственной работы) | check_action | ожидаемая роль, фактическая, где зафиксирована |
| `SELF_REVIEW` | Review-файл на собственную работу (в т.ч. созданный ПМ/диспетчером под давлением красного CI) | check_action, аудит review-файлов | задача, автор кода, автор review-файла |
| `OPS_TASK_AS_DEV` | ops-задача проведена как dev (review-файл вместо протокола приемки Заказчика) | разбор тикета | задача, что должно было быть |
| `ZONE_CONFLICT` | Пересечение зон записи активных сессий | admit_session | конфликтующие пути и delegation_id |
| `STALE_EVIDENCE` | Evidence устарел: approve до нового коммита, незакрытый долг хотфикса | check_action | что устарело, старый/новый SHA или даты |
| `AMBIGUOUS_STATE` | Противоречивые/неоднозначные факты, неизвестный flow, неоднозначный scope | inspect, check_action | оба противоречащих факта с путями |
| `STALE_SNAPSHOT` | Входной снимок не соответствует текущему состоянию | check_action, admit_session | ожидаемый и фактический digest |
| `EXTERNAL_ENFORCEMENT_UNKNOWN` | Внешняя защита (branch protection) не подтверждена | check_action (merge/release) | что именно не подтверждено; пометка не повышает статус разрешения |

`DENY` перечисляет ВСЕ блокирующие причины (не первую). `UNKNOWN` обязателен, когда невозможна проверка (нечитаемый источник, strict-mode без provenance-блока — спека deterministic-flow, Requirement «Provenance review»).

## 10. Формат решения Заказчика (decision format, версия 1)

```
customer_decision v1 {
  decision_id: str            # уникальный, цитируемый
  scope_ref: { project, phase, change_id? }   # к какому scope/фазе относится
  verbatim_quote: str         # дословная цитата Заказчика
  source: str                 # где зафиксировано (PLAN.md / answers_roundN.md / чат-лог)
  fixed_at: date
  grants: list[Action]        # какие действия разрешает
  valid_until_phase_close: true   # не переносится после закрытия фазы
}
```

- Версия формата: `decision_format_version = 1`; нераспознаваемая/устаревшая версия → `MISSING_INPUT` (не интерпретировать «на глаз»).
- Правила приемки: только дословная фиксация уровня 1 (раздел 2); пересказ ПМ и «считаю согласованным» не проходят. Отнесение к scope/фазе проверяется по `scope_ref`: решение фазы A не покрывает действие фазы B (`HUMAN_APPROVAL_REQUIRED`).
- **Журнал решений (P0.4, пересмотр плана):** канонический носитель решения — запись `decisions/<YYYY-MM-DD>-<slug>.md` в репозитории с машиночитаемым блоком ` ```decision-record ` (schema `decision-record/1`): `decision_id`, `date`, `scope {project, change_id?, phase?}`, `action` (какое действие разрешает), `commit` (SHA на момент решения), `source` (канал/дословная цитата), опционально `expiration`. Запись создается `gate_runner.py record-decision` (валидация полей; append-only — перезапись запрещена). Строковый `approval_ref` ОБЯЗАН быть `decision_id` записи журнала; произвольная строка решением не является. Проверка соответствия (`flow_transition._approval_finding`): `action` записи покрывает запрошенное действие; `scope` записи покрывает scope запроса (заполненные поля); `commit` записи — предок текущего HEAD или равен ему (принцип свежести — «SHA в истории», НЕ TTL ≤24ч: решение — норма уровня 1, а не наблюдаемый факт уровня 2); `expiration` не истек. Словарь `customer_decision v1` остается допустимым для программных вызовов. Журнал решений — НЕ защищенная подпись и НЕ независимое одобрение личности Заказчика (общий пользователь ОС способен его изменить — известное допущение): в отчетах «решение зафиксировано», не «личность подтверждена».

## 11. Gate Runner: run_gates(scope, phase) -> GateReport

```
GateReport {
  scope, phase, gates: list[{name, status: PASS|FAIL|NOT_RUN|UNKNOWN, detail}],
  overall: PASS|FAIL|UNKNOWN, report_ref: str
}
```

- Исполняет применимые gates фазы; ошибка запуска gate — `UNKNOWN`/FAIL, не пропуск.
- Не является заменой существующих скриптов: вызывает их как adapter'ы (раздел 12), exit codes сохранены.

## 12. Ответственность слоев и адаптеры к существующим воротам

**Матрица ответственности (честная граница enforcement):**

| Слой | Что может | Чего НЕ доказывает |
|---|---|---|
| Runtime (Flow Control) | запретить запуск/приёмку действия в своей сессии | ничего о других машинах/сессиях |
| CI | запретить PR (валидаторы на каждый push) | настройки GitHub вне workflow |
| Branch protection на main | физически запретить прямой push/merge без PR | локальные обходы вне GitHub |
| Локальный CLI | показать состояние, отвергнуть действие локально | сам по себе НЕ доказывает настройки GitHub |

Локальный PASS gates не доказывает защиту main: при недоступности подтверждения branch protection решения на merge/release получают пометку `EXTERNAL_ENFORCEMENT_UNKNOWN` и не повышают статус разрешения.

**Существующие исполняемые ворота и их место в новой модели (факты по коду на HEAD d3304c3):**

| Скрипт | Что реально проверяет сегодня | Адаптер в Flow Control |
|---|---|---|
| `scripts/flow_check.py` | Детерминированная state machine ПО ФАЙЛАМ уже созданных артефактов: change без requirements.md / proposal+design+tasks / specs-дельт (контракты 1–2), sdd.md при активном change, Requirement без Scenario, кейсы в new/ без трассировки к спеке Requirement/Scenario, формат «1 кейс = 1 файл» (H1), approved/ без review «одобрить» (5), автотесты без approved и TC-трассировки (6), архивированный change без слияния дельт (7), закрытые dev-задачи без code-review approve (J10), I3-покрытие API-маршрутов. remove-qa-checklist: чеклисты не требуются и не валидируются (append-only история). Чистый, без LLM и агентов; exit 0/1/2 | Adapter `flow_check_gate`: `run_gates` запускает как subprocess; ошибки FLOW-ERROR мапятся в `blocking_reasons` (`INVALID_GATE`/`MISSING_INPUT` по разделам). Общие чистые функции (parse_verdict, approved_review_tasks) переиспользуются импортом, не дублируются |
| `scripts/pm_bounds_check.py` | Границы ПМ: (J3) защищенные пути конвейера без `[pipeline]`-маркера; (J2) обязательные поля реестра, один ПМ = один проект; (J9) коммиты ПМ-сессии в продуктовых репо без маркера; (J10) merge без review-файла с approve-вердиктом и датой ≤ коммита. Режимы CLI: --commits, --sessions, --product-commits, --all-projects, --require-review; exit 0/1 | Adapter `pm_bounds_gate`. НЕ превращается в единую state machine — остается независимым gate; его пары `(project, owner_pm)` и protected paths питают `admit_session` (`ZONE_CONFLICT`, `WRONG_ROLE` для ПМ-исполнителя) |
| `scripts/pr_validate.py` | PR по трассировочному маркеру в title/body (`[BUG-NNN]`/`[change-id]`/`[chore]`): наличие обязательных артефактов по контракту (баг-репорт; change-пакет+approved+review+тесты; chore без openspec-диффа), J10-покрытие задач диффа review. Работает локально и как GitHub Action; exit 0/1 | Adapter `pr_gate` для действий `merge_task`; маркер ID = источник `scope.change_id` при PR |

Дублируемые проверки (вердикты review, J10 встречается в обоих скриптах) унифицировать адаптерами либо вынести общие чистые функции **после** тестов старого поведения; exit codes и режимы скриптов не менять без отдельного теста совместимости (00-README, огр. 2). `pm_bounds_check.py` не становится частью state machine.

## 13. Восстановление после сбоя

- Состояние всегда вычисляется заново из фактов (Git, файлы, реестр): ни один компонент не хранит истину исключительно в памяти или логе. Прерывание между снимками безопасно: перезапуск = новый `inspect`.
- Audit-события (JSONL) помогают разбору, но не являются единственным доказательством; execution log — ссылки и digest, без секретов и полных промптов.
- Незавершенные reservation после сбоя: реестр — источник истины; запись с истекшим `expires_at` не блокирует (но её presence фиксируется в problems до снятия).

## 14. Режим shadow (срез 1)

Поставки 01–03 работают в режиме inspect/check/next: без блокировок, без записей, без запуска агентов. Расхождения с решениями ПМ фиксируются как данные (Decision'ы с DENY/UNKNOWN при реально выполненном действии) для решения о следующем срезе. Переход в enforcement — отдельное решение Заказчика (`HUMAN_APPROVAL_REQUIRED` по определению). Т.е. в shadow-режиме Decision вычисляется честно, но Orchestrator не исполняет и не запрещает: никакой компонент среза 1 не изменяет репозиторий и не блокирует работу.

## 15. Приёмка контракта

- [ ] Интерфейсы (раздел 4) и схема scope (раздел 3) согласованы со спекой deterministic-flow (16 сценариев) — противоречий нет.
- [ ] Таблица «действие → роль → входы → evidence → gates → результат» покрывает Флоу 1–5 с положительными и отрицательными примерами (раздел 7).
- [ ] Все 9 кодов причин определены с человекочитаемыми деталями и путями к evidence (раздел 9).
- [ ] Версия формата решения Заказчика зафиксирована (раздел 10).
- [ ] Адаптеры описаны честно — по фактическому поведению скриптов на HEAD, без приписывания им несуществующих проверок (раздел 12).
- [ ] Никакой новый этап не стартует только на основании текста ответа агента (раздел 2, 10).
