#!/usr/bin/env python3
"""pr_validate.py — валидатор артефактов по трассировочному ID (I3+I4, блок I).

Запускается:
  1. Локально: python3 scripts/pr_validate.py <repo> --id BUG-001 [--type bug|change|chore]
  2. GitHub Action на PR: парсит ID из title/body PR, валидирует наличие
     обязательных артефактов по контрактам artifact_contract.md.

Маркер ID обязателен в title/body PR: [BUG-NNN] | [change-id] | [chore] | [docs] | [ops]
Каждый тип влечет свой набор обязательных артефактов (см. FLOWS ниже).

Exit 0 — все артефакты на месте; exit 1 — список отсутствующих.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import session_check as _sc  # noqa: E402  (единая валидация change-id, J51)

# --- Наборы обязательных артефактов по типам (контракты) ---

def check_bug(repo: Path, num: str) -> list[str]:
    """Флоу 2: баг-репорт обязателен; после мержа — статус CLOSED.

    Н5 (аудит 2026-10-23): контракт артефакта единый с flow_transition —
    test-model/bugs/BUG-<id>.md ИЛИ BUG-<id>-<slug>.md
    (flow_check.bug_report_paths); прежде здесь принимался только glob
    с суффиксом, и файл BUG-001.md, прошедший переход Флоу 2, валил PR.
    """
    from flow_check import bug_report_paths  # noqa: E402  (единый контракт Н5)
    missing = []
    digits = re.sub(r"^BUG-", "", num)  # принимаем и "BUG-001", и "001"
    bugs = bug_report_paths(repo, num)
    if not bugs:
        return [f"test-model/bugs/BUG-{digits}.md (или BUG-{digits}-*.md): "
                "баг-репорт отсутствует (контракт 6/правило 7)"]
    text = bugs[0].read_text(encoding="utf-8", errors="replace")
    # На момент PR статус может быть открыт; CLOSED требуется после мержа (проверяет отдельно post-merge job)
    if "Статус" not in text and "CLOSED" not in text:
        missing.append(f"{bugs[0].name}: нет раздела «Статус»")
    return missing


def check_change(repo: Path, change_id: str, marker: str = "") -> list[str]:
    """Флоу 1: change-пакет + QA-артефакты (контракты 2, 4, 5, 6, 7)."""
    missing = []
    pkg = repo / "openspec" / "changes" / change_id
    if not pkg.is_dir():
        # J6: пакет заархивирован (пост-мерж) — PR не может требовать активный пакет.
        # Работы после архивации помечаются [chore]; если маркер change и пакет в archive —
        # валидируем слитность (контракт 7), а не активный пакет.
        arch = repo / "openspec" / "changes" / "archive"
        archived = [d for d in arch.iterdir() if d.is_dir() and d.name.endswith(change_id)] if arch.is_dir() else []
        if archived:
            return [f"CHANGE-ARCHIVED: пакет в archive ({archived[0].name}) — для пост-релизных работ по нему используй маркер [chore]"]
        return [f"openspec/changes/{change_id}/: change-пакет отсутствует (контракт 2)"]
    for req_file in ("proposal.md", "design.md", "tasks.md"):
        if not (pkg / req_file).is_file():
            missing.append(f"openspec/changes/{change_id}/{req_file}: отсутствует (контракт 2)")
    if not (pkg / "specs").is_dir() or not any((pkg / "specs").rglob("spec.md")):
        missing.append(f"openspec/changes/{change_id}/specs/: нет дельт (контракт 2)")

    # Impact-анализ (H5) — при MODIFIED/REMOVED дельтах обязателен
    has_modified = False
    for sf in (pkg / "specs").rglob("spec.md") if (pkg / "specs").is_dir() else []:
        if re.search(r"^##\s*(MODIFIED|REMOVED)", sf.read_text(encoding="utf-8", errors="replace"), re.M):
            has_modified = True
            break
    impact = repo / "test-model" / "impact" / f"{change_id}.md"
    if has_modified and not impact.is_file():
        missing.append(f"test-model/impact/{change_id}.md: нет impact-анализа при MODIFIED/REMOVED дельтах (H5)")

    # remove-qa-checklist: чеклист больше не обязателен (контракт 4) — кейсы
    # трассируются к спеке напрямую; существующие чеклисты не проверяются.

    # Кейсы: approved обязательны (контракт 6); new допустим только если PR не финальный
    appr = repo / "test-model" / "approved" / change_id
    if not appr.is_dir() or not any(appr.glob("*.md")):
        new = repo / "test-model" / "new" / change_id
        if new.is_dir() and any(new.glob("*.md")):
            missing.append(
                f"test-model/approved/{change_id}/: кейсы не перенесены из new/ — ревью не завершено (контракт 5/6)"
            )
        else:
            missing.append(f"test-model/approved/{change_id}/: approved-кейсы отсутствуют (контракт 6)")

    # Ревью-файл с вердиктом (контракт 5)
    rev_dir = repo / "test-model" / "reviews" / change_id
    if not rev_dir.is_dir() or not any(rev_dir.glob("review-*.md")):
        missing.append(f"test-model/reviews/{change_id}/: нет review-файла (контракт 5)")

    # J10: PR без code-review задач из дифа → FAIL.
    # Задачи берутся из ветки PR (GITHUB_HEAD_REF) и строк title/body с
    # «Merge/задача/task»; для каждой нужен review-файл с вердиктом approve
    # в code-reviews/<change-id>/ (формат agents/code_reviewer_agent.md).
    from flow_check import approved_review_tasks  # единый парсер вердиктов (J10)
    pr_tasks = extract_pr_tasks(marker + " " + os.environ.get("GITHUB_HEAD_REF", ""))
    if pr_tasks:
        # MIN-4 ревью BUG-AF-RECHECK: warns парсера (нечитаемый/противоречивый
        # вердикт последней итерации) — в диагностику, не молчаливое
        # «задача не покрыта».
        review_warns: list[str] = []
        covered = approved_review_tasks(repo, change_id, review_warns)
        for w in review_warns:
            print(f"pr_validate: FLOW-WARNING: code-reviews/{change_id}/: {w}",
                  file=sys.stderr)
        # J10 требует review только для DEV-задач: ops/docs-задачи ([ops]/[docs])
        # и незакрытые задачи tasks.md исключаются (контракт flow_control:
        # ops_task/docs_task; упоминание незакрытой задачи в тексте PR —
        # контекст, не требование ревью). Урок add-containerization: «задачи
        # 2.3/2.4» в body при [ops]-маркере давали ложный FAIL.
        try:
            from flow_check import closed_dev_tasks
            tasks_md = repo / "openspec" / "changes" / change_id / "tasks.md"
            dev_closed = set(closed_dev_tasks(tasks_md.read_text(encoding="utf-8", errors="replace"))) if tasks_md.is_file() else set()
        except Exception:
            dev_closed = None  # файл недоступен — прежнее поведение
        unreviewed = [
            t for t in pr_tasks
            if t not in covered and (dev_closed is None or t in dev_closed)
        ]
        if unreviewed:
            missing.append(
                f"code-reviews/{change_id}/: нет review-файла с вердиктом approve "
                f"для задач из дифа PR (J10): " + ", ".join(unreviewed)
            )

    # SELF_REVIEW (решение Заказчика 2026-10-03): approve засчитывается только
    # если Reviewer-Delegation существует в async_delegations, completed, и это
    # НЕ dev-делегация задачи. Git-учетка одна — независимость подтверждается
    # платформенными id (устойчивы к пересозданию main-сессий). Проверка ВНЕ
    # привязки к pr_tasks: подделка артефакта ловится даже когда задачи не
    # упомянуты в тексте PR.
    import sqlite3 as _sq
    _db = Path.home() / ".hermes" / "state.db"
    _dev_delegs: set = set()
    try:
        import json as _json
        _fstate = _json.loads((Path.home() / ".hermes" / "state" / "flowctl_state.json").read_text(encoding="utf-8"))
        for _r in _fstate.get("runs", {}).values():
            _sc = _r.get("scope") or {}
            if _sc.get("change") == change_id and _sc.get("task"):
                _dd = _r.get("delegation_id")
                if _dd:
                    _dev_delegs.add(_dd.replace("-", "_"))
    except Exception:
        pass
    _cr_dir = repo / "code-reviews" / change_id
    if _cr_dir.is_dir():
        for _rf in _cr_dir.glob("review-*.md"):
            _t = _rf.read_text(encoding="utf-8", errors="replace")
            _m = re.search(r"Reviewer-Delegation[^A-Za-z0-9]{0,6}(deleg[-_][A-Za-z0-9]+)", _t, re.I)
            if not _m:
                missing.append(
                    f"code-reviews/{change_id}/{_rf.name}: нет Reviewer-Delegation (SELF_REVIEW-защита) — ревью не засчитано"
                )
                continue
            _rd = _m.group(1).replace("-", "_")
            _exists = _ok = False
            try:
                _c = _sq.connect(f"file:{_db}?mode=ro", uri=True)
                _row = _c.execute("SELECT state FROM async_delegations WHERE delegation_id=?", (_rd,)).fetchone()
                _c.close()
                _exists = _row is not None
                _ok = _exists and _row[0] in ("completed",) and _rd not in _dev_delegs
            except _sq.Error:
                _exists = _ok = True
            if not _ok:
                missing.append(
                    f"code-reviews/{change_id}/{_rf.name}: Reviewer-Delegation '{_rd}' "
                    + ("не найдена в реестре делегаций" if not _exists else
                       "совпадает с dev-делегацией задачи (SELF_REVIEW)") +
                    " — ревью не засчитано"
                )

    # J51.3: ворота merge regress-final — полный сериальный прогон после
    # approve ревью обязателен (артефакт решает regress_runner, не ПМ).
    missing.extend(check_regress_final(repo, change_id))

    # Тесты: хотя бы один TC-ID change в tests/ (контракт 6, трассировка)
    tests_root = repo / "tests"
    tc_prefix = "TC-" + change_id.split("-")[0].upper()
    found_test = False
    if tests_root.is_dir():
        for tf in tests_root.rglob("test_*.py"):
            if re.search(rf"{re.escape(change_id)}|{tc_prefix}", tf.read_text(encoding="utf-8", errors="replace")):
                found_test = True
                break
    if not found_test:
        missing.append(
            f"tests/: не найдено тестов с трассировкой {change_id} / {tc_prefix}-NNN (контракт 6)"
        )
    return missing


def check_chore(repo: Path, marker: str) -> list[str]:
    """Флоу 4: обслуживание — минимальный набор, спека не меняется.

    Н16 (аудит 2026-10-23): [chore] блокировал только openspec/ — продуктовый
    код под [chore] проходил CI без ревью-артефактов (дыра ровно в классе
    «ошибки выбора маркера»). Дифф продуктового кода (конвенция CI:
    PR_CHANGED_CODE=1 — backend/frontend/services/src/e2e, т.е. вне
    openspec/, docs/, tests/, scripts/) под [chore] = FAIL с подсказкой.
    """
    # Запрет: chore не имеет права менять спеки (изменение as is только через change)
    changed_specs = os.environ.get("PR_CHANGED_SPECS", "")
    if changed_specs.strip() in ("1", "true"):
        return [
            "PR помечен [chore], но содержит изменения openspec/ — изменение as is только через change-пакет (контракт 7)"
        ]
    if os.environ.get("PR_CHANGED_CODE", "").strip() in ("1", "true"):
        return [
            "PR помечен [chore], но содержит изменения кода продукта "
            "(backend/frontend/services/src/e2e; вне openspec/, docs/, "
            "tests/, scripts/) — используй маркер [change-id]/[BUG-NNN] "
            "(Н16: chore не прикрывает продуктовый код)"
        ]
    return []


def check_docs(repo: Path, marker: str) -> list[str]:
    """[docs]: документация без openspec-пакета. Верификация фактов вместо
    код-ревью (контракт ops_task/docs_task). Запрещены код продукта и спеки."""
    errs: list[str] = []
    # Конвенция CI: PR_CHANGED_SPECS / PR_CHANGED_CODE — "1", если дифф трогает
    # openspec/changes/ (активные) / код продукта соответственно (см. check_chore).
    if os.environ.get("PR_CHANGED_SPECS", "").strip() in ("1", "true"):
        errs.append(
            "PR помечен [docs], но содержит изменения openspec/ — изменение as is "
            "только через change-пакет (контракт 7)")
    if os.environ.get("PR_CHANGED_CODE", "").strip() in ("1", "true"):
        errs.append(
            "PR помечен [docs], но содержит изменения кода продукта — используй "
            "маркер [change-id]/[BUG-NNN]")
    return errs


def check_ui_tests_gate(repo: Path, change_id: str, pr_text: str = "") -> list[str]:
    """J50 CI-ворота: UI-дельта (frontend/**, services/*/frontend/) в дифе PR
    + макеты (design/** в change-пакете или проекте) ⇒ в дифе обязаны быть
    изменения tests/web/*.py, ЛИБО явный маркер `[no-ui-tests]` с основанием
    (≥10 символов после маркера до конца строки; иначе маркер неполный и
    валидируется как отсутствие). Честный escape — по образцу [ops] (J26).

    Диф-контекст — env-конвенция CI (flow.yml): PR_CHANGED_UI=1, если диф
    трогает продуктовый фронтенд; PR_CHANGED_WEBTESTS=1, если tests/web/.
    Без фронтенд-путей в дифе проверка молчит (бэкенд-PR не мешает)."""
    if os.environ.get("PR_CHANGED_UI", "").strip() not in ("1", "true"):
        return []  # (1) фронтенд-путей в дифе нет — проверка молчит
    if os.environ.get("PR_CHANGED_WEBTESTS", "").strip() in ("1", "true"):
        print(
            f"pr_validate: J50 OK: UI-дельта покрыта изменениями tests/web/ ({change_id})",
            file=sys.stderr,
        )
        return []  # (3) web-тесты в дифе — OK
    m = NO_UI_TESTS_RE.search(pr_text)
    if m and len(m.group(1).strip()) >= 10:
        print(
            f"pr_validate: J50 OK: [no-ui-tests] — основание: {m.group(1).strip()}",
            file=sys.stderr,
        )
        return []  # (2) честный escape с основанием — OK
    if not _has_design_artifacts(repo, change_id):
        return []  # макетов нет — правило не применяется
    if m:
        return [
            "J50: маркер [no-ui-tests] без основания — после маркера до конца "
            "строки нужно ≥10 символов с обоснованием (почему UI-сценарий без web-тестов)"
        ]
    return [
        "J50: UI-дельта без web-тестов: добавь tests/web кейсы или [no-ui-tests] + основание"
    ]


def _has_design_artifacts(repo: Path, change_id: str) -> bool:
    """J50: макеты — design/** в change-пакете или в проекте (структура
    ekotov-wiki: design/ в корне репозитория)."""
    pkg = repo / "openspec" / "changes" / change_id
    for root in (pkg, repo):
        d = root / "design"
        if d.is_dir() and any(p.is_file() for p in d.rglob("*")):
            return True
    return False


REGRESS_FINAL_MAX_AGE_DAYS = 7


def check_regress_final(repo: Path, change_id: str) -> list[str]:
    """J51.3: ворота merge «regress-final». Для change-пакетов обязателен
    финальный регресс-артефакт test-model/regress/<change-id>/regress-final.md
    (пишет только `flowctl regress --change C --final`; нумерованные
    regress-<N>.md финальными НЕ считаются — это артефакты «регресса своего
    куска», J51.2). Требования к артефакту:
      - verdict PASS (FAIL/WARN/нечитаемый — артефакт не засчитан);
      - свежесть ≤ 7 дней (поле date; не читается — mtime файла);
      - commit == HEAD ветки PR, ИЛИ commit — предок HEAD не старше последнего
 касания измененных файлов (прогон «более свежий, чем изменения»);
 мета-файлы, не влияющие на поведение тестов, из «измененных файлов»
 исключены (BUG-023: сам артефакт regress-final.md и
 test-model/regress/<change-id>/**; BUG-024: весь
 test-model/regress/**, test-model/reviews/**, code-reviews/** —
 мета-коммит после прогона не инвалидирует его);
      - tooling-пути (scripts/**, .github/**), тронутые после commit
 прогона — REGRESS_FINAL_STALE с нотой «freeze tooling» (J54: среда
 прогона изменилась, regress-final повторяется на замороженном
 tooling);
      - если в артефакте указан worktree — HEAD этого дерева обязан совпадать
        с commit прогона (сверка «артефакт ↔ дерево», review-003 #4).
    Нарушения: REGRESS_FINAL_MISSING (нет артефакта / не-PASS) и
    REGRESS_FINAL_STALE (протух по времени / commit не совпадает).

    CI-деградации (BUG-022, ложный REGRESS_FINAL_STALE в CI-клоне):
      - worktree из артефакта — локальный путь машины прогона и в CI-клоне
        не читается как git-дерево → FLOW-WARNING в stderr, ворота НЕ блокируются
        (сверка «артефакт ↔ дерево» информационная, инвариант обеспечивает
        commit-сверка); STALE — только если дерево читается и HEAD ≠ commit;
      - shallow-клон (actions/checkout fetch-depth 1): merge-base не может
        верифицировать предка → тихий `git fetch --unshallow origin` (с
        таймаутом); если не помог и заданы GITHUB_TOKEN+GITHUB_REPOSITORY —
        предок проверяется через GitHub API compare (base=HEAD...commit,
        статус 'behind'/'identical' = предок); иначе — явный FAIL
        UNKNOWN_ANCESTRY с объяснением (не молчаливый STALE)."""
    # change-id идет в путь чтения (недоверенный вход из маркера PR) —
    # единая грамматика с пишущим концом flowctl.cmd_regress (review-003 #3,
    # review-001 #1): без валидации — traversal чтения + эхо в сообщение.
    cid_problem = _sc.change_id_problem(change_id)
    if cid_problem:
        return [f"pr_validate: {cid_problem} (J51.3: ворота regress-final "
                "не применимы)"]
    final = repo / "test-model" / "regress" / change_id / "regress-final.md"
    if not final.is_file():
        return [
            f"test-model/regress/{change_id}/regress-final.md: финальный "
            "regress-артефакт отсутствует — merge закрыт без полного "
            "сериального прогона (REGRESS_FINAL_MISSING, J51.3; нумерованные "
            "regress-<N>.md финальными не считаются — нужен "
            "`flowctl regress --change " + change_id + " --final`)"
        ]
    text = final.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"(?m)^-\s*verdict:\s*(\S+)", text)
    if not m or m.group(1) != "PASS":
        verdict = m.group(1) if m else "не читается"
        return [
            f"test-model/regress/{change_id}/regress-final.md: вердикт "
            f"финального прогона «{verdict}» — не PASS; merge невозможен "
            "(REGRESS_FINAL_MISSING, J51.3)"
        ]

    problems: list[str] = []
    raw_date = ""
    dm = re.search(r"(?m)^-\s*date:\s*(.+?)\s*$", text)
    dt = None
    if dm:
        raw_date = dm.group(1).strip()
        for cand in (raw_date, raw_date.replace("Z", "+00:00")):
            try:
                dt = datetime.datetime.fromisoformat(cand)
                break
            except ValueError:
                continue
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
    if dt is None:  # дата не читается — свежесть по mtime артефакта
        dt = datetime.datetime.fromtimestamp(
            final.stat().st_mtime, tz=datetime.timezone.utc)
    age = datetime.datetime.now(datetime.timezone.utc) - dt
    if age > datetime.timedelta(days=REGRESS_FINAL_MAX_AGE_DAYS):
        problems.append(
            f"test-model/regress/{change_id}/regress-final.md: артефакт старше "
            f"{REGRESS_FINAL_MAX_AGE_DAYS} дней (дата {raw_date or 'не читается'}) — "
            "прогон протух, перезапусти `flowctl regress --final` "
            "(REGRESS_FINAL_STALE, J51.3)")

    cm = re.search(r"(?m)^-\s*commit:\s*([0-9a-f]{7,40})\s*$", text)
    if not cm:
        problems.append(
            f"test-model/regress/{change_id}/regress-final.md: commit прогона "
            "не читается — артефакт не от flowctl regress "
            "(REGRESS_FINAL_STALE, J51.3)")
    else:
        reason = _regress_commit_problem(repo, cm.group(1), change_id)
        if reason:
            # BUG-022: неверифицируемая ancestry в shallow-клоне — отдельный
            # явный тег UNKNOWN_ANCESTRY (не ложный REGRESS_FINAL_STALE).
            tag = ("UNKNOWN_ANCESTRY"
                   if reason.startswith("UNKNOWN_ANCESTRY")
                   else "REGRESS_FINAL_STALE")
            problems.append(
                f"test-model/regress/{change_id}/regress-final.md: {reason} "
                f"({tag}, J51.3)")
        # Свежестная сверка «артефакт ↔ дерево» (review-003 #4): если в
        # артефакте указан worktree прогона, его ТЕКУЩИЙ HEAD обязан
        # совпадать с commit прогона — дерево, уехавшее вперед после
        # прогона, инвалидирует артефакт.
        art_commit = cm.group(1)
        wm = re.search(r"(?m)^-\s*worktree:\s*(\S+)\s*$", text)
        if wm:
            wt = Path(wm.group(1))
            wt_head = _git(wt, "rev-parse", "HEAD")
            if not wt_head:
                # BUG-022: worktree — локальный путь машины прогона; в CI-клоне
                # он не читается как git-дерево, и это НЕ признак протухшего
                # прогона (commit-сверка выше независима и достаточна).
                # Неблокирующий WARNING — молчаливого пропуска нет, но и
                # ложного REGRESS_FINAL_STALE в CI тоже нет.
                print(
                    f"pr_validate: FLOW-WARNING: test-model/regress/{change_id}/"
                    f"regress-final.md: worktree {wm.group(1)} не читается как "
                    "git-дерево (локальный путь машины прогона) — сверка "
                    "«артефакт ↔ дерево» пропущена, инвариант обеспечивает "
                    "commit-сверка (BUG-022)",
                    file=sys.stderr)
            elif not wt_head.startswith(art_commit):
                problems.append(
                    f"test-model/regress/{change_id}/regress-final.md: "
                    f"HEAD worktree {wm.group(1)} ({wt_head[:12]}) не "
                    f"совпадает с commit прогона {art_commit[:12]} — дерево "
                    "уехало вперед после прогона, артефакт протух "
                    "(REGRESS_FINAL_STALE, J51.3)")
    return problems


def _git(repo: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _is_shallow(repo: Path) -> bool:
    """Shallow-клон (actions/checkout fetch-depth=1)?"""
    return _git(repo, "rev-parse", "--is-shallow-repository") == "true"


REGRESS_UNSHALLOW_TIMEOUT_S = 120
REGRESS_API_TIMEOUT_S = 30


def _try_unshallow(repo: Path) -> bool:
    """BUG-022: тихий `git fetch --unshallow origin` с таймаутом.
    True — история полная, локальные git-проверки снова работают.
    CI-клон сужает refspec до одной ветки (clone -b / actions/checkout):
    перед unshallow расширяем ветки origin, иначе после unshallow не будет
    origin/main и сверка «не старше измененных файлов» все равно упадет
    («измененные файлы PR не определимы»)."""
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-branches", "--add",
         "origin", "*"],
        capture_output=True, text=True, timeout=30)  # best effort
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "fetch", "--unshallow", "origin"],
            capture_output=True, text=True, timeout=REGRESS_UNSHALLOW_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _github_compare_ancestor(head: str, commit: str) -> bool | None:
    """BUG-022: API-fallback ancestry для shallow-клона — GitHub compare
    base(HEAD)...commit. True — 'behind'/'identical' (commit — предок HEAD
    или совпадает); False — 'ahead'/'divergent' (не предок); None — нет
    GITHUB_TOKEN/GITHUB_REPOSITORY или ошибка API (верdict неизвестен)."""
    token = os.environ.get("GITHUB_TOKEN", "")
    slug = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not slug:
        return None
    url = f"https://api.github.com/repos/{slug}/compare/{head}...{commit}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pr_validate",
    })
    try:
        with urllib.request.urlopen(req, timeout=REGRESS_API_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    status = data.get("status")
    if status in ("behind", "identical"):
        return True
    if status in ("ahead", "divergent"):
        return False
    return None


def _regress_commit_problem(repo: Path, commit: str,
                            change_id: str = "") -> str | None:
    """J51.3: commit артефакта == HEAD ветки PR, ИЛИ предок HEAD не старше
    последнего касания измененных файлов. Возвращает текст проблемы или None.

    BUG-023: сам артефакт regress-final.md и весь test-model/regress/<cid>/
    того же change исключаются из «измененных файлов»: артефакт коммитится
    ПОСЛЕ прогона и потому всегда новее удостоверяемого кода — без
    исключения свежестная сверка вечно дает REGRESS_FINAL_STALE
    (ворота самоубийственны).

    BUG-024: исключение расширено до мета-файлов, не влияющих на поведение
    тестов: весь test-model/regress/** (артефакты и прогоны ЛЮБЫХ change),
    test-model/reviews/** и code-reviews/** (мета Reviewer-Delegation).
    Любой мета-коммит после regress-прогона не должен давать ложный
    REGRESS_FINAL_STALE и обязательный лишний полный перегон. Продуктовые
    и тестовые файлы (services/**, backend/**, frontend/**, tests/**,
    openspec/**) — как прежде: правка после прогона = STALE.

    J54: tooling-пути (scripts/**, .github/**) после прогона — тоже
    STALE, но с нотой «freeze tooling: tooling изменился после прогона,
    повтори regress-final» — измененный tooling инвалидирует среду
    прогона целиком (харнесс/CI/статика), прогон не воспроизводим.

    BUG-022: в shallow-клоне merge-base не может верифицировать предка
    (история неполна) → сначала тихий `git fetch --unshallow origin`;
    если не помог и есть GITHUB_TOKEN+GITHUB_REPOSITORY — ancestry через
    GitHub API compare (при 'behind'/'identical' ворота пропускают: локальная
    сверка «не старше измененных файлов» в урезанном клоне все равно
    неверифицируема, а предок-коммит не может быть свежее HEAD-изменений
    больше, чем прогон); иначе — явный FAIL UNKNOWN_ANCESTRY, не молчаливый
    REGRESS_FINAL_STALE."""
    head = _git(repo, "rev-parse", "HEAD")
    if not head:
        return f"HEAD репозитория не читается, commit {commit} не верифицирован"
    if head.startswith(commit):
        return None
    # Предок HEAD и не старше измененных файлов — прогон «более свежий,
    # чем изменения», засчитывается.
    def _is_ancestor() -> "subprocess.CompletedProcess[str] | None":
        try:
            return subprocess.run(
                ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, head],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None

    ancestor = _is_ancestor()
    if ancestor is None or ancestor.returncode != 0:
        if _is_shallow(repo):
            if _try_unshallow(repo):
                ancestor = _is_ancestor()
        if ancestor is None or ancestor.returncode != 0:
            if _is_shallow(repo):
                verdict = _github_compare_ancestor(head, commit)
                if verdict is True:
                    return None
                if verdict is False:
                    return (
                        f"commit {commit} не совпадает с HEAD ({head[:12]}) "
                        "и не является его предком (GitHub API compare)")
                return (
                    f"UNKNOWN_ANCESTRY: shallow-клон: ancestry commit "
                    f"{commit} относительно HEAD ({head[:12]}) неверифицируема — "
                    "`git fetch --unshallow origin` не удался, проверка через "
                    "GitHub API недоступна (нужны GITHUB_TOKEN и "
                    "GITHUB_REPOSITORY, либо запусти ворота на полном клоне)")
            return (f"commit {commit} не совпадает с HEAD ({head[:12]}) и не "
                    "является его предком")
    files: set[str] = set()
    for base in ("main", "origin/main"):
        out = _git(repo, "diff", "--name-only", f"{base}...HEAD")
        if out:
            files.update(out.splitlines())
    worktree = _git(repo, "diff", "--name-only", "HEAD")
    if worktree:
        files.update(worktree.splitlines())
    if not files:
        return (f"commit {commit} не совпадает с HEAD ({head[:12]}), "
                "измененные файлы PR не определимы")
    # BUG-023/BUG-024: мета-файлы, не влияющие на поведение тестов, всегда
    # новее commit прогона (коммитятся после него) — исключаем из свежестной
    # сверки, иначе ворота вечно REGRESS_FINAL_STALE. BUG-023 — собственные
    # regress-артефакты; BUG-024 — regress/прогон-артефакты ЛЮБЫХ change,
    # ревью-мета (test-model/reviews/**) и мета Reviewer-Delegation
    # (code-reviews/**).
    meta_prefixes = (
        "code-reviews/",
        "test-model/regress/",
        "test-model/reviews/",
    )
    meta_files = {f for f in files if f.startswith(meta_prefixes)}
    files -= meta_files
    if not files:
        # После исключения мета-файлов измененных (продуктовых/тестовых)
        # файлов нет — прогон удостоверяет весь дифф, ворота пропускают
        # (это НЕ «не определимы» из BUG-022-ветки выше).
        return None
    art_ts = _git(repo, "show", "-s", "--format=%ct", commit)
    if not art_ts or not art_ts.isdigit():
        return f"commit {commit} не найден в репозитории"
    newer: list[str] = []
    for f in sorted(files):
        ts = _git(repo, "log", "-1", "--format=%ct", "--", f)
        if ts and ts.isdigit() and int(ts) > int(art_ts):
            newer.append(f)
    if not newer:
        return None
    # J54: tooling-пути (scripts/**, .github/**), тронутые ПОСЛЕ commit
    # regress-прогона, инвалидируют его особо — вся среда прогона
    # (харнесс, CI-ворота, статика) могла измениться, результат не
    # воспроизводим тем же прогоном. Отдельная нота «freeze tooling»:
    # tooling замораживается на время финального прогона, прогон
    # повторяется. Мета-исключения BUG-023/BUG-024 выше приоритетны —
    # мета-файлы в newer не попадают.
    tooling = [f for f in newer if f.startswith(("scripts/", ".github/"))]
    if tooling:
        return ("freeze tooling: tooling изменился после прогона, повтори "
                f"regress-final ({tooling[0]})")
    f = newer[0]
    return (f"commit {commit} старше последнего касания измененного "
            f"файла {f}")


# --- J58: impact-гейт (карта СА + механическая сверка diff→правила) ---

IMPACT_RULES_REL = "scripts/impact_rules.json"
IMPACT_SECTION_RE = re.compile(r"(?m)^##\s*Impact\b")


def load_impact_rules(repo: Path) -> list[dict]:
    """J58: расширяемые правила diff→требование из scripts/impact_rules.json.

    Файла нет (проект без правил) или он не читается — сверка молчит
    (WARN в stderr, не молчаливый пропуск логики: отсутствие правил —
    легитимное состояние нового проекта, битый JSON — инцидент контента).
    """
    path = repo / IMPACT_RULES_REL
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        print(
            f"pr_validate: FLOW-WARNING: {IMPACT_RULES_REL} не читается как JSON — "
            "механическая сверка impact-правил пропущена (J58)",
            file=sys.stderr,
        )
        return []
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        return []
    return [
        r for r in rules
        if isinstance(r, dict)
        and r.get("id")
        and r.get("when")
        and isinstance(r.get("require"), list)
        and r["require"]
    ]


def pr_changed_files(repo: Path) -> list[tuple[str | None, str]]:
    """J58: список (status, path) измененных файлов PR.

    Источники по приоритету:
      1. env PR_CHANGED_FILES — строки «STATUS\\tpath» (конвенция CI
         `git diff --name-status`) или просто «path» (status неизвестен);
      2. fallback: `git diff --name-status main...HEAD` + незакоммиченные
         изменения (та же схема дифф-контекста, что у regress-ворот).
    """
    env = os.environ.get("PR_CHANGED_FILES", "")
    files: list[tuple[str | None, str]] = []
    for line in env.splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" in line:
            st, p = line.split("\t", 1)
            st = st.strip()
            files.append((st[:1] if st and re.fullmatch(r"[AMDTUR]", st[:1]) else None, p.strip()))
        else:
            files.append((None, line))
    if files:
        return files
    raw: set[str] = set()
    for base in ("main", "origin/main"):
        out = _git(repo, "diff", "--name-status", f"{base}...HEAD")
        if out:
            raw.update(out.splitlines())
    worktree = _git(repo, "diff", "--name-status", "HEAD")
    if worktree:
        raw.update(worktree.splitlines())
    for line in raw:
        parts = line.split("\t", 1)
        if len(parts) == 2:
            st = parts[0].strip()[:1]
            files.append((st if re.fullmatch(r"[AMDTUR]", st) else None, parts[1].strip()))
        else:
            files.append((None, line.strip()))
    return files


def impact_map_problem(repo: Path, change_id: str) -> str | None:
    """J58: impact-карта change-пакета — openspec/changes/<id>/impact.md
    ИЛИ секция «## Impact» в design.md/proposal.md."""
    pkg = repo / "openspec" / "changes" / change_id
    if (pkg / "impact.md").is_file():
        return None
    for name in ("design.md", "proposal.md"):
        f = pkg / name
        if f.is_file() and IMPACT_SECTION_RE.search(
                f.read_text(encoding="utf-8", errors="replace")):
            return None
    return (
        f"openspec/changes/{change_id}/impact.md: impact-карта отсутствует "
        "(J58: обязательный артефакт СА — создай impact.md по шаблону "
        "openspec/changes/_template/impact.md или добавь секцию "
        "«## Impact» в design.md/proposal.md: затронутые сервисы / контракты / "
        "статика (static_v?) / deploy-матрица + RUNBOOK / миграции / тесты)"
    )


def _head_files(repo: Path) -> list[str] | None:
    """J58-fix (ревью-001 замечание 1): файлы HEAD-дерева — для exists-guard.

    None — HEAD определить нельзя (не git-репо / нет коммитов / ошибка git):
    exists-guard не применяется, требование работает как раньше
    (консервативный DENY лучше молчаливого пропуска).
    """
    out = _git(repo, "ls-tree", "-r", "--name-only", "HEAD")
    if out is None:
        return None
    return out.splitlines() if out else []


def _diff_patch(repo: Path) -> str | None:
    """Патч диффа PR — для content_when-триггеров (IR-06, Н25).

    None — патч нечитаем (не git-репо / ошибка git): content_when-фильтр НЕ
    применяется — консервативный DENY лучше молчаливого пропуска. Пустая
    строка — дифф пуст: content_when-regex не совпадает, правило молчит.
    """
    parts: list[str] = []
    for base in ("main", "origin/main"):
        out = _git(repo, "diff", f"{base}...HEAD")
        if out is not None:
            if out:
                parts.append(out)
            break
    out = _git(repo, "diff", "HEAD")
    if out is None and not parts:
        return None
    if out:
        parts.append(out)
    return "\n".join(parts)


def _safe_search(pattern: str, text: str) -> bool:
    try:
        return bool(re.search(pattern, text))
    except re.error:
        return False


def _pr_body_text() -> str | None:
    """Тело PR для pr_body-требований (IR-05/06, Н20/Н25).

    Источники: env PR_BODY (локальные прогоны/тесты) либо fetch_pr_info()
    (CI: GITHUB_EVENT_PATH). None — PR-контекста нет: pr_body-требования
    SKIP (нельзя DENY-ить то, что нечем проверить).
    """
    env = os.environ.get("PR_BODY")
    if env is not None:
        return env
    return fetch_pr_info()


def _head_before_files(repo: Path) -> list[str] | None:
    """precision-fix (j58-ir02-precision): файлы дерева «до диффа» (HEAD-before).

    Для new_dir-правил: коммитится ли дифф на ветке PR (main...HEAD) или это
    незакоммиченные изменения (HEAD) — «до» всегда merge-base с main
    (origin/main); для чисто рабочих изменений merge-base = HEAD, дерево то же.
    None — дерево определить нельзя (не git-репо / нет main / ошибка git):
    new_dir-фильтр не применяется, прежнее поведение (консервативный DENY).
    Пустое дерево ([] — коммит без файлов) читается: любой каталог в нём
    отсутствовал → правило срабатывает как прежде.
    """
    for base in ("main", "origin/main"):
        mb = _git(repo, "merge-base", base, "HEAD")
        if not mb:
            continue
        out = _git(repo, "ls-tree", "-r", "--name-only", mb)
        if out is not None:
            return out.splitlines()
    return _head_files(repo)


def _require_paths_exist(paths: list[str], head_files: list[str]) -> bool:
    """J58-fix: совпадает ли хотя бы один файл HEAD хотя бы с одним паттерном."""
    for hf in head_files:
        for pat in paths:
            try:
                if re.search(pat, hf):
                    return True
            except re.error:
                continue
    return False


def check_impact_rules(repo: Path,
                       files: list[tuple[str | None, str]]) -> list[str]:
    """J58: механическая сверка диффа с правилами impact_rules.json.

    Сработавшее правило (какой-либо путь диффа совпал с when[].path и —
    если указан when[].status — статус совпал; неизвестный статус считается
    совпавшим: консервативный DENY с подсказкой лучше молчаливого пропуска)
    требует наличия в диффе хотя бы одного пути под каждым require[].path.
    Расхождение = DENY: id правила + что ожидалось + hint.

    Exists-guard (J58-fix, ревью-001 замечание 1): требование применяется
    только к репо, где целевой артефакт существует в HEAD (продуктовый
    проект). Если артефакта в HEAD нет (фабрика: нет contracts/openapi.json,
    deploy/, backend/app/pages.py) — требование молчит: нельзя DENY-ить репо
    за отсутствие артефакта, которого в этом репо не существует. Вне git
    (HEAD не определён) guard не применяется — прежнее поведение.

    new_dir-точность (precision-fix, j58-ir02-precision; дефект живого
    ekotov PR #231): правило с флагом new_dir (например IR-02 «новый сервис
    services/<name>/») срабатывает ТОЛЬКО если первый уровень каталога из
    совпавшего пути ОТСУТСТВОВАЛ в дереве HEAD-before — иначе (новый файл
    ВНУТРИ существующего сервиса, services/frontend/ уже в дереве) файл не
    считается триггером. Если НИ ОДИН файл не прошел фильтр — правило
    молчит. Дерево HEAD-before не читается — прежнее поведение
    (консервативный DENY лучше молчаливого пропуска).

    content_when (IR-06, Н25): если у сработавшего правила есть content_when
    ({path, regex}), правило остается триггером только если патч диффа
    файлов, чей путь совпал с cw.path, содержит cw.regex. Патч нечитаем —
    фильтр не применяется (консервативно: требование действует).

    pr_body-требования (АУД-4 хвост, Н20/Н25): require-запись с ключом
    pr_body (без path) сверяется с ТЕЛОМ PR (env PR_BODY или
    fetch_pr_info/GITHUB_EVENT_PATH — тот же источник, что читает маркер):
    regex не найден в теле → DENY с hint из правила; PR-контекста нет →
    SKIP (требование молчит — advisory-прогон остается у
    flow_preflight check pr-body).
    """
    errs: list[str] = []
    head_files = _head_files(repo)
    head_before: list[str] | None = None
    head_before_done = False
    patch: str | None = None  # лениво: только для content_when-правил
    patch_done = False
    for rule in load_impact_rules(repo):
        whens = rule["when"] if isinstance(rule["when"], list) else [rule["when"]]
        new_dir_rule = bool(rule.get("new_dir"))
        if new_dir_rule and not head_before_done:
            head_before = _head_before_files(repo)  # лениво: только для new_dir-правил
            head_before_done = True
        triggered: list[str] = []
        for st, p in files:
            for w in whens:
                if not isinstance(w, dict) or not w.get("path"):
                    continue
                try:
                    m = re.search(w["path"], p)
                except re.error:
                    continue
                if not m:
                    continue
                req_status = w.get("status")
                if req_status:
                    statuses = (req_status if isinstance(req_status, list)
                                else [req_status])
                    if st is not None and st not in statuses:
                        continue
                # new_dir-точность: каталог сервиса уже был в HEAD-before —
                # файл не триггер нового сервиса (новый файл внутри
                # существующего сервиса — не «новый сервис»).
                if new_dir_rule and head_before is not None:
                    seg = m.group(0).rstrip("/")
                    if seg and any(hf.startswith(seg + "/")
                                   for hf in head_before):
                        continue
                triggered.append(p)
                break
        if not triggered:
            continue
        # content_when (IR-06, Н25): уточнение триггера по содержимому патча.
        # Патч нечитаем (None) — фильтр не применяется (консервативный DENY).
        content_whens = [w for w in (rule.get("content_when") or [])
                         if isinstance(w, dict) and w.get("regex")]
        if content_whens:
            if not patch_done:
                patch = _diff_patch(repo)
                patch_done = True
            if patch is not None:
                content_ok = True
                for cw in content_whens:
                    scope = patch
                    if cw.get("path"):
                        chunks = [c for c in patch.split("diff --git ")
                                  if _safe_search(cw["path"], c.split("\n", 1)[0])]
                        scope = "\n".join(chunks)
                    if not _safe_search(cw["regex"], scope):
                        content_ok = False
                        break
                if not content_ok:
                    continue
        for req in rule["require"]:
            if not isinstance(req, dict):
                continue
            # АУД-4 хвост (Н20/Н25): pr_body-требование — regex на ТЕЛО PR
            # (env PR_BODY / fetch_pr_info), не на пути диффа. Нет PR-контекста
            # — SKIP (advisory-прогон остается за flow_preflight check pr-body).
            if req.get("pr_body"):
                body = _pr_body_text()
                if body is None:
                    continue
                if not _safe_search(req["pr_body"], body):
                    show = triggered[0] + ("…" if len(triggered) > 1 else "")
                    errs.append(
                        f"J58: правило {rule['id']} ({rule.get('title', rule['id'])}): "
                        f"дифф трогает {show}, но в теле PR нет "
                        f"«{req.get('label') or req['pr_body']}» "
                        f"— {req.get('hint', 'добавь требуемое в тело PR')}"
                    )
                continue
            if not req.get("path"):
                continue
            paths = req["path"] if isinstance(req["path"], list) else [req["path"]]
            # J58-fix exists-guard: артефакта нет в HEAD этого репо — требование
            # молчит (фабричные PR с contracts/-диффом не DENY-ятся за
            # contracts/openapi.json, которого в фабрике не существует).
            if head_files is not None and not _require_paths_exist(paths, head_files):
                continue
            found = False
            for _, p in files:
                for pat in paths:
                    try:
                        if re.search(pat, p):
                            found = True
                            break
                    except re.error:
                        continue
                if found:
                    break
            if not found:
                show = triggered[0] + ("…" if len(triggered) > 1 else "")
                errs.append(
                    f"J58: правило {rule['id']} ({rule.get('title', rule['id'])}): "
                    f"дифф трогает {show}, но в диффе нет {req.get('label') or req['path']} "
                    f"— {req.get('hint', 'добавь ожидаемый артефакт в дифф PR')}"
                )
    return errs


def check_impact_gate(repo: Path, flow_type: str, change_id: str) -> list[str]:
    """J58 CI-ворота: impact-карта + механическая сверка diff↔карта.

    Для change-маркера ([change-id]) обязателен артефакт карты
    (impact.md или секция «## Impact» в design.md/proposal.md);
    для [chore]/[docs]/[ops]/[BUG-NNN] — только механическая сверка
    правил (карта не требуется — не душить chore), расхождение диффа
    с правилами = DENY для ЛЮБОГО маркера (обобщение J50).
    """
    errs: list[str] = []
    if flow_type == "change":
        cid_problem = _sc.change_id_problem(change_id)
        if not cid_problem and (repo / "openspec" / "changes" / change_id).is_dir():
            problem = impact_map_problem(repo, change_id)
            if problem:
                errs.append(problem)
    errs.extend(check_impact_rules(repo, pr_changed_files(repo)))
    return errs


def check_ops(repo: Path, marker: str) -> list[str]:
    """[ops]: эксплуатационные работы (деплой, переключение, инфраструктура).
    Артефакт — протокол приемки Заказчика, не review-файл (контракт ops_task).
    В git-диффе запрещены спеки; деплой-скрипты разрешены."""
    errs: list[str] = []
    if os.environ.get("PR_CHANGED_SPECS", "").strip() in ("1", "true"):
        errs.append(
            "PR помечен [ops], но содержит изменения openspec/ — изменение as is "
            "только через change-пакет (контракт 7)")
    # J26: deploy-дифф допустим ТОЛЬКО в [ops]-PR (CI-ворота, детерминированно).
    return errs


def check_deploy_gate(marker: str) -> list[str]:
    """J26 CI-ворота: PR трогает deploy/** или RUNBOOK.md, но не помечен [ops].

    Деплой-инфраструктура — зона devops-роли: ПМ/dev-PR с deploy-файлами
    обязан идти как [ops] (девопс-делегация + протокол приемки Заказчика).
    Вызывается из main() для НЕ-ops типов flow."""
    if os.environ.get("PR_CHANGED_DEPLOY", "").strip() in ("1", "true"):
        return [
            "J26: PR содержит изменения deploy/**/RUNBOOK.md, но не помечен [ops] — "
            "деплой-инфраструктура меняется только через [ops]-PR (devops-делегация, "
            "приемка Заказчика)"]
    return []


FLOWS = {
    "bug": check_bug,
    "change": check_change,
    "chore": check_chore,
    "docs": check_docs,
    "ops": check_ops,
}

MARKER_RE = re.compile(r"\[(BUG-\d+|[a-z0-9]+(?:-[a-z0-9]+)+|\bchore|\bdocs|\bops)\]")

# J50: маркер-escape [no-ui-tests] — в начале строки или после пробела;
# основание = хвост строки после маркера (валидность проверяет вызыватель).
NO_UI_TESTS_RE = re.compile(r"(?m)(?:^|\s)\[no-ui-tests\][ \t]*(.*)$")

# J10: извлечение номеров задач из текста PR (ветка/заголовок/тело).
# Принимает формы: 1.1, 5.2, 2.1+2.2 (объединенная задача — обе).
PR_TASK_RE = re.compile(
    r"(?<![\d.])"          # не часть большего числа (5.12, 1.5.2)
    r"(?<!§)"              # §4.1 — ссылка на раздел документа, не задача
    r"(?<!шаг\s)(?<!шаги\s)"  # «шаг 4/6», «шаг 4.1» — шаги процедуры
    r"(\d+\.\d+)(?:\s*\+\s*(\d+\.\d+))?(?![\d.])"
)


def extract_pr_tasks(text: str) -> list[str]:
    """Номера задач из текста PR: 'feature/add-x-1.2', 'Merge 2.1+2.2: ...', 'задача 3.1'.

    Исключения (не задачи): §N.N (разделы документов), «шаг N.N» (шаги
    процедуры), N.N внутри N.N.N. Ложные срабатывания (урок J10 на
    add-containerization: «RUNBOOK §4.1» в body потребовал ревью задачи 4.1).
    """
    # §-ссылки вырезаем до матчинга (lookbehind переменной длины для «шаг »
    # не работает в re, поэтому текстовая предобработка):
    text = re.sub(r"§\d+(?:\.\d+)*", "", text)
    # «шаг 4.1», «шаги 4.1–4.2, 5» — вся хвостовая перечисление после слова:
    text = re.sub(
        r"\bшаг[аи]?\s+\d+(?:\.\d+)*(?:\s*[–,—]\s*\d+(?:\.\d+)*)*",
        "", text, flags=re.I,
    )
    tasks: list[str] = []
    for m in PR_TASK_RE.finditer(text):
        tasks.append(m.group(1))
        if m.group(2):
            tasks.append(m.group(2))
    return tasks


def parse_id(text: str) -> tuple[str, str] | None:
    """Возвращает (тип, id) из маркера в тексте PR."""
    m = re.search(r"\[(BUG-\d+)\]", text)
    if m:
        return "bug", m.group(1)
    m = re.search(r"\[chore\]", text, re.I)
    if m:
        return "chore", "chore"
    # [docs]/[ops]: задачи без openspec-пакета и без кода продукта (документация,
    # эксплуатация; J10-семантика ops/docs-задач — приемка Заказчика/верификация
    # фактов вместо код-ревью). Урок 2026-10-04: J36-запись в BACKLOG с [docs]
    # в title валится из-за незнания маркера.
    m = re.search(r"\[docs\]", text, re.I)
    if m:
        return "docs", "docs"
    m = re.search(r"\[ops\]", text, re.I)
    if m:
        return "ops", "ops"
    m = re.search(r"\[([a-z0-9]+(?:-[a-z0-9]+)+)\]", text)
    if m and "-" in m.group(1):
        return "change", m.group(1)
    return None


def fetch_pr_info() -> str | None:
    """В CI (GitHub Action): событие pull_request — читает body/title из GITHUB_EVENT_PATH."""
    ev = os.environ.get("GITHUB_EVENT_PATH")
    if not ev or not Path(ev).is_file():
        return None
    d = json.loads(Path(ev).read_text(encoding="utf-8"))
    pr = d.get("pull_request") or {}
    title = pr.get("title", "")
    body = pr.get("body") or ""
    return title + "\n" + body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", nargs="?", default=".", help="корень репозитория проекта")
    ap.add_argument("--id", help="трассировочный ID (BUG-001 / change-id / chore)")
    ap.add_argument("--type", choices=list(FLOWS), help="тип флоу (иначе выводится из ID)")
    args = ap.parse_args()

    marker_text = f"[{args.id}]" if args.id else ""
    if not marker_text:
        pr_text = fetch_pr_info()
        if pr_text:
            marker_text = pr_text
        else:
            print("pr_validate: не указан --id и нет GITHUB_EVENT_PATH")
            return 1

    parsed = parse_id(marker_text)
    if not parsed:
        print(
            "pr_validate: маркер ID не найден. Обязателен в title/body PR: "
            "[BUG-NNN] (баг-фикс) / [change-id] (функционал) / [chore] (обслуживание) / [docs] (документация) / [ops] (эксплуатация). Блок I."
        )
        return 1

    flow_type, ident = parsed
    if args.type:
        flow_type = args.type

    # J26 CI-ворота: deploy-дифф только в [ops]-PR
    if flow_type != "ops":
        gate_errs = check_deploy_gate(marker_text)
        if gate_errs:
            for e in gate_errs:
                print(f"pr_validate: {e}")
            return 1

    repo = Path(args.repo).resolve()

    # J50 CI-ворота: UI-дельта (frontend/**) + макеты (design/**) в change-пакете
    # ⇒ web-тесты обязательны, escape — [no-ui-tests] + основание в body.
    gate_errs = check_ui_tests_gate(repo, ident, marker_text)
    if gate_errs:
        for e in gate_errs:
            print(f"pr_validate: {e}")
        return 1

    # J58 CI-ворота: impact-карта (change-маркер) + механическая сверка
    # диффа с правилами scripts/impact_rules.json (любой маркер).
    gate_errs = check_impact_gate(repo, flow_type, ident)
    if gate_errs:
        for e in gate_errs:
            print(f"pr_validate: {e}")
        return 1

    if flow_type == "change":
        missing = FLOWS[flow_type](repo, ident, marker_text)
    else:
        missing = FLOWS[flow_type](repo, ident)

    if missing:
        print(f"PR-VALIDATE FAIL ({flow_type}:{ident}): отсутствуют артефакты по контрактам:")
        for m in missing:
            print(f"  - {m}")
        return 1
    print(f"PR-VALIDATE OK ({flow_type}:{ident}): обязательные артефакты на месте")
    return 0


if __name__ == "__main__":
    sys.exit(main())
