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
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

# --- Наборы обязательных артефактов по типам (контракты) ---

def check_bug(repo: Path, num: str) -> list[str]:
    """Флоу 2: баг-репорт обязателен; после мержа — статус CLOSED."""
    missing = []
    digits = re.sub(r"^BUG-", "", num)  # принимаем и "BUG-001", и "001"
    bugs = list((repo / "test-model" / "bugs").glob(f"BUG-{digits}-*.md")) if (repo / "test-model" / "bugs").is_dir() else []
    if not bugs:
        return [f"test-model/bugs/BUG-{digits}-*.md: баг-репорт отсутствует (контракт 6/правило 7)"]
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
    """Флоу 4: обслуживание — минимальный набор, спека не меняется."""
    # Запрет: chore не имеет права менять спеки (изменение as is только через change)
    changed_specs = os.environ.get("PR_CHANGED_SPECS", "")
    if changed_specs.strip() in ("1", "true"):
        return [
            "PR помечен [chore], но содержит изменения openspec/ — изменение as is только через change-пакет (контракт 7)"
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
