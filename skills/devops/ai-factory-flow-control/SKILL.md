---
name: ai-factory-flow-control
description: "Run factory work through flowctl gates: prepare→run→finish."
version: 1.0.0
author: Egor Kotov (realax07), Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [ai-factory, flow-control, flowctl, enforcing]
    related_skills: [ekotov-hermes-session-guard]
---

# AI Factory Flow Control (operational)

How to run work through the deterministic gates of ai-factory / ekotov-wiki in
enforcing mode. Repo docs are the process source of truth
(`ai-factory/docs/README-flow-control.md`, AGENTS.md rule 9); this skill is the
operational sequence and the pitfalls that cost real time.

## When to Use

- Any delegation cycle on ekotov-wiki or ai-factory (Флоу 1–5) — enforcing is ON,
  DENY/UNKNOWN block the action.
- Writing decisions-journal entries, review-mapping, or merging to main
  (branch rulesets active — direct push is rejected).
- Multi-step repo work delegates to subagents (1 задача = 1 сабагент + отдельный
  ревьюер); PM verifies by facts, never by the agent's report alone.

## Core loop (flowctl, adapter=manual)

Scripts live in the target repo's `scripts/` (installed by factory_init since
factory-init-flow-core) or `~/ai-factory/scripts/` — prefer the target's copy.

1. **Decision** (starting a change): write `decisions/<YYYY-MM-DD>-<slug>.md`
   with a FENCED JSON block ```decision-record {...}``` (schema
   `decision-record/1`; required fields decision_id/date/scope/action/commit/source).
   `scope.project` must equal the `--project` value (`wiki`), NOT the repo name.
2. **prepare**:
   `python3 scripts/flowctl.py prepare --repo . --project wiki --flow N
   --change <id> [--task T] --action A --role R --path "zone/**"
   --approval-ref <decision_id> --owner-pm pm-main`
   - role/zone source: `role_zone_policy.py`; CLI may only narrow.
   - Флоу 1 create_change requires requirements.md УТВЕРЖДЕН + valid decision.
   - Флоу 4 change-id must be exactly `chore` (CHORE_RE `^chore$`).
   - Gate matching is field-EXACT; iterate by reading the DENY detail, not by
     guessing. The decision-record `action` must equal the requested `--action`
     (a generic «старт пакета» covers only `package_start`, not `create_change`),
     `scope.phase` must equal the phase the action requires (create_change = 1),
     and `--path` values must match `role_zone_policy.allowed_zones_for(role)`
     glob PATTERNS verbatim (`openspec/changes/*/tasks.md`, NOT a specific
     `openspec/changes/<id>/**` — the CLI only narrows policy); omitting
     `--path` fails the reservation with «пустая зона записи». Each DENY names
     the mismatch — one fix per iteration, never shotgun all fields at once.
   - **PLAN-файл релиза обязателен (J41):** `create_change` и `dev_task` (транзитивно) DENY без валидного плана релиза — `PLAN-<change>.md` в корне репо или `PLAN.md` внутри каталога пакета, содержащий строку «Флоу: N» (N 1–5). Коды отказа: `PLAN_RELEASE_MISSING` / `PLAN_FLOW_UNDECLARED`. План пишет ПМ до диспатча СА; СА детализирует в tasks.md, план не переписывает. Watchdog дополняет контур: bypass-делегация (нет валидной flowctl-записи) метится `undelivered_bypass` — ее результат не считается доставленным этапом.
   - ALWAYS dry-run first: run the exact prepare command with `--dry-run` and
     read the decision verdict (ALLOW/DENY + details) BEFORE the real run.
     Deny reasons resolve in dry-runs at zero cost; blind real attempts count
     sessions in the registry, clutter the gated map, and each rejected
     decision-format fix becomes a wasted docs-PR cycle. The dry-run surfaces
     every decision-record mismatch (action, scope.project, commit field,
     phase) in one pass — fix all named fields, re-dry-run, only then run for
     real.
   - The decision-record `commit` field is REQUIRED (P0.4) — the merge SHA of
     the PR that landed the decision file itself; a decision without it fails
     «запись неполна — нет полей: commit».
   - Decision-record `action` is validated against the exact `--action` being
     prepared: a decision authorizing one action does NOT transfer to another
     («разрешение чужого действия не переносится») — when an action changes
     mid-flight, amend the decision's action field in a follow-up PR before
     re-preparing.
   - omitting `--owner-pm` fails with «отсутствует owner_pm» even on actions that
     succeeded before; always pass it on a non-dry run.
3. **run**: ONLY `--correlation-id <id> --registry
   ~/.hermes/state/active_sessions.json --state
   ~/.hermes/state/flowctl_state.json`. Run does NOT re-take prepare's
   role/zone/task flags («unrecognized arguments»); it re-reads them from the
   prepare record and is idempotent. The state path MUST equal the one prepare
   used — a fresh /tmp path yields MISSING_INPUT «нет записи prepare».
   `reconcile` likewise takes `--delegation-id` (the flowctl `deleg-…` id, not
   the platform `deleg_…`), not `--correlation-id`.
   - Pass the correlation id at FULL length — a truncated id fails
     «нет записи prepare» even though the run exists; recover the exact id
     from the keys of `runs` in flowctl_state.json, not from memory.
   - The code_review action's goal file is auto-generated and contains no PR
     context — put the PR number/URL, review focus points, verdict file path
     and the «НЕ мержи» boundary in the delegate_task goal text itself.

### Re-dispatch after a crashed/dropped delegation

A delegated subtask dies on limit-break, gateway stall or
`delivery_state=dropped`; the flowctl session stays `running`. Sequence:

1. `flowctl.py reconcile --repo . --delegation-id deleg-<flowctl-id>
   --registry … --state …` — marks the session stale; reconcile decides
   nothing, the PM decision to redo is explicit.
2. Verify the zone is untouched BEFORE re-running: `git status` in the target
   worktree — a half-finished subagent may have left artifacts; accept or clean
   them explicitly, never let the re-run duplicate them. If the work is
   effectively DONE (commit pushed, PR open), admin-close the session instead
   of re-running: append a lifecycle-history entry and set status=completed in
   `active_sessions.json` — a lingering `running` session HOLDS its zone, and
   every later prepare over intersecting paths fails ZONE_CONFLICT until you
   close it.
3. Clean the QA stand of the subagent's residue: interrupted QA runs leave
   QAGAL-prefixed rows in the stand DB and files in its volumes (grep tables
   and data dirs for `QAGAL`) — tests expect a clean state and fail on it.
4. Fresh prepare → run (new correlation_id), re-dispatch delegate_task with the
   new goal file, then add the id mapping to
   `~/.hermes/state/delegate_gate_gated_map.json`
   (`"<platform_deleg_…>": "deleg-<flowctl-id>"`) — without it the watchdog
   counts the gated delegation as a BYPASS incident.
5. Read the dead delegation's partial result from
   `async_delegations.result_json` (state.db) first — the subagent's own
   diagnosis of where it died often saves a full repeat.
4. Do the work (manual adapter: delegate externally; run status is not proof).
5. **finish**: `--correlation-id <id> --report <file> --state ... --registry ...
   --gate-scope pre_merge --pm-mode commits --pm-commits . [--pr-id N]`.
   Required gates always run; `--gates` only ADDS; `--diagnostic` never yields
   `accepted`. SKIPPED required gate → verdict is not accepted (Б1).
   - `--report` resolves against the flowctl CWD, not the target repo — pass
     an ABSOLUTE path; a relative path silently fails «агентский отчет не
     найден» even when the file exists in the repo.
   - `--pr-id` takes the PR NUMBER (for pm_bounds_check), but pr_validate
     derives its marker check from the change id — run the adapter manually
     as `python3 scripts/pr_validate.py <repo> --id BUG-NNN` (NOT the PR
     number) before finish; a number id fails «маркер ID не найден» even when
     the title carries a valid `[BUG-NNN]`.
   - After an APPROVE verdict, record provenance via
     `python3 scripts/gate_runner.py record-review --review-path <file>
     --project wiki --change BUG-NNN --tasks 1 --author-delegation <deleg-…>
     --reviewer-delegation <deleg-…> --commit <sha> --verdict approve
     --diff-digest <sha256-16>`; it writes a `.provenance.json` sidecar next
     to the review — commit the sidecars in a follow-up `[chore]` PR.
   - Review loop: RETURN verdict → fix-cycle subagent works on the SAME PR
     branch (new commit, new SHA → previous review becomes STALE_EVIDENCE) →
     review-002 as a fresh reviewer delegation pinned to the new SHA. The PM
     decides semantics questions the reviewer flags (record the decision in
     the fix-cycle ТЗ, not in chat).
   - A finish on a reviewer session whose review commit also carries files
     outside the reviewer zone (e.g. PM bug-card updates in the same commit)
     records OUT_OF_ZONE + gate FAIL and returns (not accepted) — acceptable
     for an already-merged review PR; to keep the verdict clean, commit the
     review file alone and PM cards in a separate commit/PR.
   - A failed finish IRREVERSIBLY moves the session to `blocked`: a first
     finish without `--pm-mode` gives pm_bounds_check ERROR («режим не задан» —
     the gate itself is fine, the launcher config was incomplete), and every
     retry with corrected flags is rejected («статус blocked: приемка
     невозможна»). Recovery: re-run finish with the same flags PLUS
     `--pm-resume` — it works only when ALL recorded defects are gate
     ERROR/SKIPPED; any FAIL or zone-* defect makes resume a hard no (recovery
     must not bypass substantive defects). The blocked→running transition is
     recorded in the session's lifecycle-history. Before resuming, verify the
     gate really passes with correct args by running the adapter directly.

## Ruleset / PR discipline

- NEVER merge locally into main, even when the PR content is already reviewed —
  the ruleset rejects the direct push, and a local merge silently skips the
  required checks. Always `gh pr merge <n> --merge`; re-open a superseded PR
  rather than reproducing its diff by hand.
- PR title/body must contain a marker: `[BUG-NNN]` / `[change-id]` / `[chore]` /
  `[docs]` / `[ops]` (pr_validate Блок I) — without it job `flow` fails.
  Arbitrary task-prefixed markers (`[j41]`, `[pipeline]`, `[design]`) are NOT in
  the valid set — pipeline/tooling changes are titled `[chore]`, package PRs
  `[docs]`.
- Marker semantics: `[change-id]` requires the FULL QA artifact set (impact,
  checklist, approved cases, review files) — which exists only after the QA
  wave. This hits intermediate dev-wave PRs exactly like create_change PRs:
  title both `[docs]`; switch to `[change-id]` only on the post-QA merge PR.
- Task checkboxes stay `[ ]` until the task's code-review lands: flow_check
  fails «N закрытых dev-задач без code-review» on any `[x]` task without an
  approve-review. Dev subagents tend to pre-check their box — revert it on the
  PR branch before merging and let the review-PR close it.
- NEVER branch a sync/hotfix branch off an in-flight feature branch: the sync
  PR's merge silently merges the feature into main ahead of its own QA gate,
  and GitHub marks the feature PR «merged» by that merge. Branch off main.
- Editing title/labels does NOT retrigger required checks (GitHub reuses the
  event payload). Close+reopen the PR DOES retrigger them — use it when the fix
  is in the title/body itself (marker, description); otherwise push one empty
  commit to the PR branch.
- A RED required check blocks merge even with `--admin` unless the token has
  admin permission (PM token: admin=false). When violations are expected by
  design (e.g. a new audit gate catching legacy packages), the merge path is a
  LEGAL green: flagged stub artifacts («восстановлен постфактум», закрывается
  при архивации) or archiving the legacy items — not an API workaround.
- Required checks are matched by WORKFLOW JOB name. Renaming a job breaks the
  ruleset until required-checks are edited (Settings → Rules) — sync both sides.
- Legacy `/branches/main/protection` API returns 404 under a write-only token;
  read rules via `GET /repos/{repo}/rules/branches/{branch}` (rulesets API).

## Review loop (Флоу 2): RETURN — норма, не провал

Независимый ревьюер может вернуть фикс (RETURN с blocker). Процедура:
1. ПМ решает семантические вопросы, которые ревьюер вынес (решение — в ТЗ
   фикс-цикла 2, не в чат: чат-решение не попадает в артефакты).
2. Фикс-цикл 2 работает В ТОТ ЖЕ PR (новый SHA → прошлое ревью STALE_EVIDENCE).
3. review-002 — свежая ревьюерская делегация, привязанная к новому SHA.
4. Ревьюер, нашедший потерю данных, которую не ловит сьют (мутационный пробел),
   прав: требовать e2e-тест на пробел в том же цикле, что и фикс.
5. Мержит ПМ только после APPROVE; provenance ревью — `gate_runner record-review`.

Семантические развилки фикс-цикла (типовой класс, bug-fix меняет правило):
 ПМ обязан сам сформулировать правило ДО диспатча цикла (в ТЗ), иначе dev
 закодирует свою трактовку и review-002 вернет по ней же. Пример-шаблон:
 «потребление применяется только к X (текущий контекст), остальные Y
 сохраняются безусловно» — узкое правило с явным списком того, что НЕ задевается.

## Деплой прод-матрицы агентом (r8-polish-класс)

Агент может прогнать боевой деплой сам (`sg docker -c 'RELEASE_TAG=<тег>
 SRC_DIR=/home/openclaw/ekotov-wiki
 PROJECT=ekotov-wiki-par
 BACKUP_DIR=/tmp/<метка>-backups
 bash
 deploy/deploy.sh'` фоном с логом). Ловушки:
 - BACKUP_DIR в /tmp: каталог нужен на запись И uid 10001 (sidecar), И openclaw
 (mkdir staging) — права ставить контейнером (`docker run --rm -v <dir>:/bkp
 alpine chown/chmod`), host-chmod от openclaw может молча не примениться.
 - images-сервис вне матрицы deploy.sh (SERVICES/image_for/6.8): если
 services/images менялся — собрать образ отдельно и поднять отдельным up; ЛЮБОЙ
 `up -d <svc>` передавать с ПОЛНЫМ набором *_IMAGE env — иначе compose поднял
 app на дефолте `:local`.
 - Бэкап в /tmp не канон — сразу после деплоя запросить Заказчика одной
 sudo-командой перенести в /var/backups/ekotov-wiki; задокументировать отклонение
 в протоколе приемки.
 - Верификация «фикс на проде»: sha256 файла по прод-URL == sha256 в main
   (побайтно), маркер `v=<тег>` в HTML — второй факт; подстрочный grep не исключает
   «старый образ». static_v freshness deploy.sh НЕ гейтит: тег, меняющий статику
   без бампа static_v, проходит все смоуки (v= в HTML показывает СТАРЫЙ маркер) —
   перед деплоем сверяй static_v в pages.py с маркером на живом проде и бампь до.
 - После пересборки тегированных образов `compose up -d` может оставить часть
   контейнеров на старых sha256-образах (compose считает их «up-to-date»):
   передавай ПОЛНЫЙ набор *_IMAGE env и `--force-recreate` затронутые сервисы,
   затем `compose ps --format '{{.Service}} {{.Image}} {{.Status}}'` — все теги
   релиза, ни одного sha256.

Отчет препарации: исходное состояние хвоста, судьба каждого BUG (закрыт/на
ревью, PR, SHA), смоук-матрица таблицей с цифрами, известные хвосты (НЕ блокеры,
явно помечены), готовность к выкатке и что осталось за Заказчиком. Влит как
`[docs]` PR. Каждый пункт опирается на проверенный на диске факт (PR state,
чеки, лог-файл), не на отчет сабагента.

## Status reports (AGENTS.md rule 9 — standing rule)

Every work-status report carries the gate-stage block:
`prepare: X | reserve: X | zone-check+gates: X | provenance: X` with
PASS/DENY/UNKNOWN/SKIPPED and failure codes. Never soften, never omit.

Customer-facing reports stay SHORT: per-task line (status, artifacts, next
step), the gates block, and open questions as a numbered list — never paste
command output, diffs or log tails into the chat; the customer reads decisions,
not execution traces. Each open question carries its answer options AND an
explicit default («дефолт: X») — the customer then answers by numbers in one
message, and an unanswered question takes its stated default instead of
blocking the pipeline.
When the customer says «дай статус», answer from state
files first, not by re-running things.
When the customer asks what you are absorbed in («чем увлекся»), the answer is
a scope correction: state what is actually done vs the remaining mechanical
step, and prefer the minimal manual workaround over another gated loop —
drafting the decision file correctly on the first try (dry-run verified)
beats a visible chain of rejected attempts.

## Pitfalls

- Every test file in ai-factory `tests/**` needs a `TC-…` id in EACH test's
  docstring (flow_check rule 6 — CI job `flow` fails «тесты без TC-трассировки»
  otherwise). Batch-inserting TC labels by script: the naive sed-class rewrite
  corrupts multiline signatures (docstring inside a wrapped def line) — verify
  with `ast.parse` and pytest collection before pushing.
- flow_check checklist format (3 independent rules): CHK rows must start with a
  single `|` (`|| CHK-…` is not matched by the parser), every CHK row needs an
  FR-N/NFR-N source in the row, and the file needs a «Дефекты спеки» section.
  Copy the row layout from an archived checklist instead of authoring from
  scratch.
- When the same blocker survives two workaround attempts, STOP and report the
  failure facts to the user — continuing to retrigger CI or re-propose variants
  reads as spinning and burns trust.
- When the delegation platform drops a gated subtask (or it returns with the
  TRUNCATED/max-iterations flag — its summary is then explicitly incomplete),
  verify the WORK before reconciling: the subagent's partial result lives in
  `async_delegations.result_json` (state.db), and uncommitted artifacts in the
  worktree may be a finished, already-green test or fix that only lacks
  commit/PR — harvest them into the next commit or cycle rather than wiping and
  repeating from zero.
- Decision-record `scope.project` is the flowctl project key (`wiki`), not the
  repo DNS name (`ekotov-wiki`) — mismatch = «решение относится к другому scope».
- Role zones are fixed GLOB patterns in `role_zone_policy.ROLE_ZONE_POLICY`, and
  prepare rejects any path outside them even inside the role's own directory:
  SA writes `openspec/changes/*/proposal.md` etc. (7 pattern files incl. the
  generic `sdd.md`), NOT `openspec/changes/<id>/**`. Inspect with
  `python3 -c "import sys; sys.path.insert(0,'scripts'); from role_zone_policy import allowed_zones_for; print(allowed_zones_for('sa'))"`
  before the first prepare of a new role.
- The decision-record block must be FENCED JSON (```decision-record {…}```);
  a YAML-styled block parses as «нет машиночитаемого блока».
- Контракт 6 accepts `test-model/regression/<domain>/` (with md files) as a
  case source equal to `approved/<change>/` — after moving cases to regression/,
  контракт 6 passes without approved/ (Флоу 4 as-is base).
- ekotov-wiki web-tests: run on the conftest auto-stand (`env -u DB_PATH -u
  EKOTOV_WIKI_BASE_URL -u PYTHONPATH` from tests/web) — it raises uvicorn+static+DB itself
  (after the search-family integration it covers suggestions too). ALWAYS add `-u
  PYTHONPATH`: the hermes kernel's PYTHONPATH (3.14 site-packages) leaks into
  conftest's uvicorn subprocess and the stand never comes up («стенд не готов за
  60s» with connection refused), and test collection errors on PIL/pydantic
  imports. An external BASE_URL stand must be assembled by hand after every host
  reboot — the full recipe (app env, nginx HTTP config with mime.types/temp-
  paths/images-proxy, images-service env, PYTHONPATH for test collection) lives
  in `references/qa-stand.md`; do NOT rediscover it from scratch. /tmp is wiped
  on host reboot (stands, worktrees, DBs are not durable); ports 8377/8378/8379
  on host-network are PRODUCTION containers (kill returns Operation not
  permitted — a container marker, not a bug).
- Flowctl stands: a fix-cycle that continues a FINISHED session's work runs
  OUTSIDE the gates (no new prepare — zones intersect with the closed one);
  note it in delegate_gate_gated_map.json `notes`, and close the loop with a
  fresh prepare/finish only at accept-review. Never dispatch into a closed
  session's zones while it still reads `running` — reconcile/admin-close first
  (ZONE_CONFLICT).
- Before starting a new wave, sweep the registry for zombie `running` sessions
  over the target zone: a finished-but-never-closed cycle (e.g. a shipped bug
  whose session was never admin-closed) silently blocks every prepare over
  intersecting paths with ZONE_CONFLICT. Admin-close with an honest lifecycle
  entry (what landed, where), never delete history.
- Acceptance complaints: reproduce each on the stand BEFORE dispatching fix
  cycles, and if the named surface is clean, check sibling surfaces sharing the
  component — the customer may see the defect through an OLD render path, making
  two complaints one root (a blind fix per complaint burns a full cycle on a
  defect that does not exist). When a cycle returns «not reproducible» with a
  green regression test, keep the test (commit it — it guards the surface), mark
  the bug a duplicate of the root-cause bug, and fold the complaint into that
  fix-cycle's ТЗ.
- J10 review→task coverage has two sources: task-id in the review FILENAME, or
  `code-reviews/<change>/review-mapping.json` (`{"review-00X.md": ["0.1",…]}`).
  Historical review names without task-id are covered only via the mapping.
- test-model as-is structure: live cases in `regression/<domain>/cases/`,
  R1–R3 common files as `regression/<domain>/legacy/` (never split inline-TC
  into per-file cases — H1 applies to live cases, legacy is an as-is document),
  intermediates (checklists/reviews/impact of closed changes) in
  `archive/workflow/`. Do not move R4–R6 cases out of live paths — контракт 6
  and 4/5 gate checks depend on them.

## Platform layer (Telegram sessions, watchdog, dropped delegations)

Session-routing problems (dropped delegation on session-rotation boundary,
watchdog, `/resume main`) are covered by the user-owned skill
`ekotov-hermes-session-guard` — consult it via skill_view; it is not
auto-loaded and not editable by the curator. Boundary rule: do not dispatch
delegate_task mid-turn on a session-rotation boundary; finish the turn first.
When a stall kills the whole session (platform "stuck, no activity" prompt,
`/stop`/`/new`), the new session recovers state from DISK, not from memory:
flowctl_state.json (running = stale → reconcile),
async_delegations.result_json (partial subagent output),
delegate_gate_gated_map.json (id mapping), active_sessions.json (registry).
Before answering the user's "give me status", rebuild the picture from these
files — a fresh session has zero conversation memory of what was dispatched.

## Verification

- `python3 scripts/flow_check.py .` → exit 0 in the target repo.
- `pytest tests/` green (ai-factory 446+; ekotov-wiki api needs a live stand:
  uvicorn on :8080 with DB_PATH/SECRET_KEY/AVATARS env, else 202 collection errors).
- `gh pr checks <n>` all green before merge; direct push rejected is expected.
- Post-deploy verification of a fix on prod: sha256 of the file fetched via the
  prod URL (`curl -sk '.../<path>?v=<tag>' | sha256sum`) must equal the sha256 of
  the file in main — substring grep finds markers in old builds too; the `v=<tag>`
  marker in served HTML is the second fact, not a substitute.
