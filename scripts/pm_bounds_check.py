#!/usr/bin/env python3
"""pm_bounds_check.py — детерминированные ворота границ ПМ-сабагента (J3, блок J).

Принцип Заказчика «где можно проверить скриптами — проверяем скриптами»:
декларативные границы в agents/pm_agent.md дублируются проверяемым слоем.
Проверяет два класса нарушений:

  1. Зона записи ПМ-сабагента: diff/содержимое указанных коммитов не должно
     трогать защищенные пути (спеки конвейера, контракты, промпты ролей, AGENTS.md)
     без пометки флоу в сообщении коммита (изменения конвейера легальны только
     через Флоу 1/4 с решением Заказчика, поэтому требуем явной пометки).
  2. Реестр active_sessions.json: записи ПМ-сабагента обязаны иметь project +
     owner_pm; пары project/owner_pm не должны смешиваться (у записи project
     владелец определяется однозначно).
  3. Продуктовые репо (--product-commits / --all-projects, J9): ПМ-сессия не
     исполнитель — коммиты ПМ-сессии (owner_pm из active_sessions.json) в
     продуктовых репо вне [pipeline]-маркера = FAIL. Git-история = evidence:
     «ПМ сам делал merge 7 веток» становится ловимым фактом, а не дисциплиной.
  4. Обязательный code-review (--require-review, J10): merge-коммит ПМ в
     продуктовом репо, для которого нет review-файла с вердиктом approve и
     датой раньше коммита → FAIL (задним числом не отмазаться). Формат
     review-файла: agents/code_reviewer_agent.md.

Использование:
  pm_bounds_check.py --commits HASH[,HASH...] [--repo PATH]   # проверка дифов
  pm_bounds_check.py --sessions [PATH]                        # проверка реестра
  pm_bounds_check.py --product-commits A..B --repo PATH       # коммиты ПМ в продуктовом репо
  pm_bounds_check.py --product-commits A..B --repo PATH --require-review   # + J10
  pm_bounds_check.py --all-projects                           # по реестру, все продуктовые репо
  pm_bounds_check.py --all --commits ... --sessions ...       # всё сразу

Exit 0 = чисто; exit 1 = нарушения (список в stdout).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

FACTORY = Path(__file__).resolve().parent.parent

# Защищенные пути фабрики: ПМ-сабагент не меняет их без явной пометки
# "[pipeline]" / "[флоу 4]" в subject коммита (изменение конвейера — отдельная
# задача, запущенная Заказчиком, а не побочный эффект проектной фазы).
# Канонический список защищенных путей конвейера (фабрика).
# Продуктовые репо адаптируют через env PM_PROTECTED_EXCLUDE
# (например, PM_PROTECTED_EXCLUDE=contracts/ — в проекте это OpenAPI-контракты
# продукта, FR-68, а не контракты конвейера; урок ekotov-wiki 2026-10-04).
# J26 (2026-10-09): deploy-дерево — зона devops-роли. ПМ-коммит с этими
# путями требует [ops]-маркера (девопс-делегация/решение), иначе нарушение.
DEPLOY_PATHS = tuple(
    p for p in (
        "deploy/",
        "RUNBOOK.md",
    )
)
PROTECTED_PATHS = tuple(
    p for p in (
        "openspec/specs/",
        "contracts/",
        "AGENTS.md",
        "agents/README.md",
    )
    if p not in [x.strip() for x in __import__("os").environ.get("PM_PROTECTED_EXCLUDE", "").split(",") if x.strip()]
)
# Промпты ролей: менять можно, но каждый такой диф — событие конвейера.
ROLE_PROMPT_PREFIX = "agents/"

PIPELINE_MARKERS = ("[pipeline]", "[флоу 4]", "[flow 4]", "[конвейер]")
ALLOWED_EXTENSIONS_IN_ROLES = (".md", ".yaml", ".yml")

REQUIRED_SESSION_FIELDS = ("delegation_id", "role", "project", "owner_pm", "status")

# Продуктовые репо: коммитить без [pipeline]-маркера может любая роль, КРОМЕ
# ПМ-сессии (J9: merge/исполнение — через dev-lead и другие роли; git-история
# продукта = evidence «ПМ не исполнитель»). Исполнительские роли (dev, qa,
# dev-lead) маркера не требуют.
PM_SESSION_OWNER = "main-session"
CONVEYOR_PROJECT = "ai-factory"


def sh(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    return proc.stdout or ""


def check_commit(repo: Path, commit: str) -> list[str]:
    problems: list[str] = []
    files_raw = sh(repo, "show", "--name-only", "--format=", commit)
    files = [f for f in files_raw.splitlines() if f.strip()]
    if not files:
        return [f"{commit}: коммит не найден или пуст"]

    subject = sh(repo, "show", "-s", "--format=%s", commit).strip()
    has_marker = any(m in subject for m in PIPELINE_MARKERS)
    protected_touched = [
        f for f in files
        if f.startswith(PROTECTED_PATHS) or f == "AGENTS.md"
    ]
    role_touched = [
        f for f in files
        if f.startswith(ROLE_PROMPT_PREFIX) and f != "agents/README.md"
        and f.endswith(ALLOWED_EXTENSIONS_IN_ROLES)
    ]

    if (protected_touched or role_touched) and not has_marker:
        what = ", ".join(protected_touched[:3] or role_touched[:3])
        problems.append(
            f"{commit}: затронуты защищенные пути конвейера ({what}) без пометки "
            f"[pipeline] в subject — изменения конвейера только через Флоу 1/4 "
            f"с решением Заказчика (граница J3)"
        )

    # J26: deploy-дерево — зона devops-роли. Коммит с deploy-путями обязан нести
    # [ops]-маркер (девопс-делегация/решение); ПМ-коммит без маркера = нарушение
    # («работа девопса, сделанная ПМ» — прецедент Р4/6.3 nginx-аватары).
    ops_marker = any(m in subject for m in ("[ops]",))
    deploy_touched = [f for f in files if f.startswith(DEPLOY_PATHS) or f == "RUNBOOK.md"]
    if deploy_touched and not has_marker and not ops_marker:
        what = ", ".join(deploy_touched[:3])
        problems.append(
            f"{commit}: затронуто deploy-дерево ({what}) без пометки [ops] в subject — "
            f"изменения деплой-инфраструктуры только через devops-делегацию (J26)"
        )

    for f in files:
        if f.startswith(ROLE_PROMPT_PREFIX) and not f.endswith(ALLOWED_EXTENSIONS_IN_ROLES):
            problems.append(
                f"{commit}: {f} — не-документный файл в agents/ вне зоны ПМ (J3)"
            )
    return problems


def check_sessions(path: Path) -> list[str]:
    problems: list[str] = []
    if not path.exists():
        return [f"{path}: файл реестра не найден"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f"{path}: не читается как JSON: {exc}"]

    seen: dict[tuple[str, str], str] = {}
    for i, s in enumerate(data.get("sessions", [])):
        label = f"sessions[{i}] ({s.get('delegation_id', '?')})"
        for field in REQUIRED_SESSION_FIELDS:
            if not s.get(field):
                problems.append(f"{label}: отсутствует обязательное поле '{field}' (J2)")
        project, owner = s.get("project", ""), s.get("owner_pm", "")
        if project and owner:
            key = (project, owner)
            if key in seen and seen[key] != s.get("delegation_id"):
                problems.append(
                    f"{label}: пара project/owner_pm {key} повторяется — "
                    f"один ПМ = один проект (J2)"
                )
            seen.setdefault(key, s.get("delegation_id", ""))
    return problems


def load_pm_projects(sessions_path: Path) -> tuple[set[str], list[str]]:
    """Продуктовые проекты из реестра (J9, --all-projects).

    Возвращает (множество путей репо, список проблем реестра).
    Пути репо: для каждой записи с project != 'ai-factory' — каталог
    /home/<user>/<project> (канон расположения продуктовых репо конвейера);
    несуществующие пропускаются (репо не на этой машине).
    """
    problems = check_sessions(sessions_path)
    if not sessions_path.exists():
        return set(), problems
    try:
        data = json.loads(sessions_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set(), problems
    repos: set[str] = set()
    for s in data.get("sessions", []):
        project = s.get("project", "")
        if not project or project == CONVEYOR_PROJECT:
            continue
        candidate = Path.home() / project
        if candidate.is_dir() and (candidate / ".git").exists():
            repos.add(str(candidate))
    return repos, problems


def check_pm_commits(
    repo: Path, rev_range: str, author_of_record: str = "",
    require_review: bool = False,
) -> list[str]:
    """Проверка диапазона коммитов продуктового репо на исполнительство ПМ (J9).

    Коммит приписывается ПМ-сессии, если:
      - git author name совпадает с author_of_record (если указан), ИЛИ
      - author_of_record не определен (реестр не дал owner) — тогда ВСЕ
        коммиты диапазона считаются ПМ-коммитами (консервативно: ворота
        требуют либо маркер [pipeline], либо определенный в реестре owner).
    Коммит вне подозрения, если в subject есть [pipeline]-маркер: явная пометка
    «это изменение конвейера, задача Флоу 4» легальна для любой сессии.
    """
    problems: list[str] = []
    hashes_raw = sh(repo, "rev-list", rev_range)
    if not hashes_raw.strip():
        return [f"{repo}: диапазон '{rev_range}' пуст или не читается"]
    for h in [h for h in hashes_raw.splitlines() if h.strip()]:
        subject = sh(repo, "show", "-s", "--format=%s", h).strip()
        if any(m in subject for m in PIPELINE_MARKERS):
            continue
        if require_review:
            problems += check_review_coverage(repo, h)
        author = sh(repo, "show", "-s", "--format=%an", h).strip()
        if author_of_record and author != author_of_record:
            continue  # коммит другой роли (dev, dev-lead) — легален
        whose = f" (author: {author})" if author else ""
        problems.append(
            f"{repo}: {h[:9]} '{subject[:80]}'{whose} — коммит ПМ-сессии в "
            f"продуктовом репо без пометки [pipeline] (J9: ПМ не исполнитель; "
            f"merge — через dev-lead, изменения конвейера — отдельная задача Флоу 4)"
        )
    return problems


# --- J10: обязательный code-review merge-коммитов ПМ (--require-review) ---

MERGE_TASK_RE = re.compile(
    r"(?:merge|задача|task)\s+(\d+(?:\.\d+)+)", re.I
)


def review_approve_date(text: str) -> str | None:
    """Вердикт approve + дата из review-файла (формат agents/code_reviewer_agent.md)."""
    # повторяем семантику flow_check.parse_verdict без импорта (скрипт самодостаточен)
    verdict = None
    for m in re.finditer(r"^#{1,4}\s*Вердикт\s*:?\s*(.+)$", text, re.I | re.M):
        v = m.group(1).strip().strip("*").strip().lower()
        if re.search(r"\b(return|доработк\w*)\b", v, re.I):
            verdict = "return"
        elif re.search(r"\b(approve|approved|одобрен\w*)\b", v, re.I):
            verdict = "approve"
    if verdict != "approve":
        return None
    dm = re.search(r"Дата\s*[:\*]*\s*(\d{4}-\d{2}-\d{2})", text, re.I)
    return dm.group(1) if dm else None


def check_review_coverage(repo: Path, commit: str) -> list[str]:
    """J10: merge-коммит без review-файла с approve-вердиктом и датой раньше коммита."""
    subject = sh(repo, "show", "-s", "--format=%s", commit).strip()
    tasks = [m.group(1) for m in MERGE_TASK_RE.finditer(subject)]
    if not tasks:
        return []  # номера задач не извлечь — J9-проверка отработает отдельно
    commit_date = sh(repo, "show", "-s", "--format=%cI", commit).strip()[:10]
    problems: list[str] = []
    for task in tasks:
        covered_by = None
        cr_dir = repo / "code-reviews"
        candidates = []
        if cr_dir.is_dir():
            for change_dir in sorted(p for p in cr_dir.iterdir() if p.is_dir()):
                for rf in change_dir.glob("review-*.md"):
                    m = re.match(r"^review-(.+?)-(\d{3})\.md$", rf.name, re.I)
                    if m and m.group(1) == task:
                        candidates.append((int(m.group(2)), rf))
        for _, rf in sorted(candidates, reverse=True):
            verdict_date = review_approve_date(
                rf.read_text(encoding="utf-8", errors="replace")
            )
            if verdict_date and verdict_date <= commit_date:
                covered_by = rf
                break
        if covered_by is None:
            detail = (
                "нет approve-вердикта с датой раньше коммита"
                if candidates
                else "нет review-файла"
            )
            problems.append(
                f"{repo}: {commit[:9]} '{subject[:80]}' — merge задачи {task} без "
                f"code-review: {detail} в code-reviews/ (J10 --require-review)"
            )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=str(FACTORY), help="git-репозиторий для --commits")
    ap.add_argument("--commits", help="коммит(ы) через запятую")
    ap.add_argument("--sessions", nargs="?", const=str(
        Path.home() / ".hermes/state/active_sessions.json"),
        help="путь к active_sessions.json")
    ap.add_argument("--product-commits", metavar="RANGE",
        help="git-диапазон (например origin/main..main или A..B) в продуктовом "
             "репо (--repo) для проверки коммитов ПМ-сессии (J9)")
    ap.add_argument("--owner", default="",
        help="git author name ПМ-сессии: коммиты других авторов считаются "
             "легальными (dev/dev-lead). Без --owner весь диапазон проверяется "
             "как ПМ-коммиты (консервативный режим)")
    ap.add_argument("--all-projects", action="store_true",
        help="проверить коммиты ПМ-сессии во ВСЕХ продуктовых репо из реестра "
             "(нужен --sessions или путь реестра по умолчанию)")
    ap.add_argument("--require-review", action="store_true",
        help="J10: merge-коммиты ПМ в продуктовом репо без review-файла с "
             "вердиктом approve и датой раньше коммита → FAIL")
    args = ap.parse_args()

    problems: list[str] = []
    if args.commits:
        repo = Path(args.repo).resolve()
        for c in [c.strip() for c in args.commits.split(",") if c.strip()]:
            problems += check_commit(repo, c)
    if args.sessions:
        problems += check_sessions(Path(args.sessions).expanduser())
    if args.product_commits:
        problems += check_pm_commits(
            Path(args.repo).resolve(), args.product_commits, args.owner,
            require_review=args.require_review)
    if args.all_projects:
        sessions_path = Path(args.sessions).expanduser() if args.sessions else (
            Path.home() / ".hermes/state/active_sessions.json")
        repos, registry_problems = load_pm_projects(sessions_path)
        if not args.sessions:
            # при явном --sessions реестр уже проверен выше — не дублируем
            problems += registry_problems
        if not repos:
            problems.append(
                "--all-projects: продуктовые репо не найдены по реестру "
                f"({sessions_path}) — проверять нечего"
            )
        for r in sorted(repos):
            repo = Path(r)
            head = sh(repo, "rev-parse", "--verify", "origin/main").strip() or \
                sh(repo, "rev-parse", "--verify", "main").strip()
            if not head:
                problems.append(f"{repo}: нет main/origin/main — пропущен")
                continue
            problems += check_pm_commits(
                repo, f"{head}..HEAD", require_review=args.require_review)

    if problems:
        print("pm_bounds_check: FAIL — нарушения границ ПМ:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("pm_bounds_check: OK — границы ПМ-сабагента соблюдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
