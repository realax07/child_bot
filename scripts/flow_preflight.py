#!/usr/bin/env python3
"""flow_preflight.py — pre-flight чеки в точке диспетчеризации (аудит АУД-4).

Дешевые проверки ДО дорогого dev-цикла, а не в merge-воротах:

  static_v          (Н24/R7) если дифф ветки касается frontend static|templates
                    или backend/app/pages.py — static_v в рабочем дереве уже
                    бампнут относительно main (иначе WARN «бампни до
                    диспетчеризации»; ловля на PR — круг зря).
  regress-staleness (Н24)    regress-final артефакт старше последнего касания
                    tests/** или backend/ в ветке → WARN «не запускай, будет
                    STALE» (чек ПОСЛЕ 15–25 мин прогона — тот же круг зря).
  check pr-body     (Н20/Н25) механика декларативных pr_body-требований
                    impact-правил IR-05/IR-06: правило сработало на диффе —
                    в теле PR есть ожидаемая строка (docker build / URL+sha256).
                    Существующие ворота pr_validate.check_impact_rules НЕ
                    тронуты: require-записи с ключом pr_body (без path) они
                    молча пропускают — правило инертно для них (см. отчет).
  snapshot          (Н17) снапшот-привязка «ветка == текущий checkout»:
                    вердикты ворот читают рабочее дерево (--repo PATH), смена
                    checkout меняет решения — сверяем ожидаемую ветку с HEAD
                    дерева и его чистотой.

Модуль автономен: только stdlib, прочие скрипты фабрики не импортирует и не
меняет. Вызывается отдельно (ручной запуск, CI-джоба, dispatcher).

Вывод — JSON в stdout: {"check": ..., "status": "OK|WARN|SKIP", "warns":
[...], ...}. SKIP — проверка неприменима (не продуктовое дерево, нет
артефактов, нет PR-контекста) — легитимное состояние, не WARN.
Exit: 0 — OK/SKIP; 1 — есть WARN и указан --strict; 2 — ошибка выполнения.

Usage:
    python3 scripts/flow_preflight.py static_v --repo PATH [--json]
    python3 scripts/flow_preflight.py regress-staleness --repo PATH [--json]
    python3 scripts/flow_preflight.py check pr-body --repo PATH
            [--pr N | --body-file F] [--strict] [--json]
    python3 scripts/flow_preflight.py snapshot --repo PATH [--branch NAME]
            [--strict] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PAGES_REL = "backend/app/pages.py"

# Триггеры static_v-чека — те же классы путей, что IR-03 (impact_rules.json):
# фронтовая статика/шаблоны любого сервиса + сам pages.py.
STATIC_TRIGGERS = [
    re.compile(r"^frontend/.*(?:static|templates)"),
    re.compile(r"^services/[^/]+/frontend/.*(?:static|templates)"),
    re.compile(r"^backend/app/pages\.py$"),
]
STATIC_V_RE = re.compile(r"(?m)^\s*static_v\s*=\s*(\d+)")

# Касание каких путей инвалидирует regress-final (семантика прогона: код
# продукта + его тесты; мета-деревья test-model//code-reviews/ не считаются —
# тот же класс исключений, что BUG-023/BUG-024 у merge-ворот).
REGRESS_TOUCH_PATHS = ("tests", "backend")
REGRESS_META_EXCLUDE = re.compile(r"^(?:test-model/|code-reviews/)")

IMPACT_RULES_REL = "scripts/impact_rules.json"
FINAL_RE = re.compile(r"(?m)^-\s*commit:\s*([0-9a-f]{7,40})\s*$")


# ----------------------------------------------------------------- git utils

def git_out(repo: Path, *args: str) -> str | None:
    """stdout git-команды в repo; ошибка/пусто → None (не исключение — чеки
    pre-flight должны давать SKIP/WARN, а не ронять диспетчеризацию)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def merge_base(repo: Path) -> str | None:
    """merge-base с main (или origin/main) — «до диффа ветки». None — базы
    нет (не git-репо, нет main): чеки дают SKIP, не verdict."""
    for base in ("main", "origin/main"):
        mb = git_out(repo, "merge-base", base, "HEAD")
        if mb:
            return mb.strip()
    return None


def branch_diff_files(repo: Path) -> list[tuple[str | None, str]]:
    """(status, path) диффа ветки: merge-base..HEAD + незакоммиченное."""
    mb = merge_base(repo)
    raw: list[str] = []
    if mb:
        out = git_out(repo, "diff", "--name-status", f"{mb}...HEAD")
        if out:
            raw.extend(out.splitlines())
    out = git_out(repo, "diff", "--name-status", "HEAD")
    if out:
        raw.extend(out.splitlines())
    files: list[tuple[str | None, str]] = []
    for line in raw:
        parts = line.split("\t", 1)
        if len(parts) == 2 and re.fullmatch(r"[AMDTUR]", parts[0].strip()[:1]):
            files.append((parts[0].strip()[:1], parts[1].strip()))
        elif len(parts) == 2:
            files.append((None, parts[1].strip()))
    return files


def head_files(repo: Path) -> list[str] | None:
    out = git_out(repo, "ls-tree", "-r", "--name-only", "HEAD")
    if out is None:
        return None
    return out.splitlines()


def _result(check: str, status: str, warns: list[str], **details) -> dict:
    return {"check": check, "status": status, "warns": warns, **details}


# ------------------------------------------------------- static_v (Н24/R7)

def check_static_v(repo: Path) -> dict:
    """IR-03 ДО диспетчеризации: дифф трогает статику — static_v бампнут
    в рабочем дереве относительно main? Нет — WARN сейчас, а не DENY на PR."""
    files = branch_diff_files(repo)
    triggered = sorted({
        p for _, p in files
        if any(rx.search(p) for rx in STATIC_TRIGGERS)
    })
    if not triggered:
        return _result("static_v", "OK", [],
                       note="дифф ветки статику/pages.py не трогает")
    hf = head_files(repo)
    if hf is None or PAGES_REL not in hf:
        return _result("static_v", "SKIP", [],
                       note=f"{PAGES_REL} отсутствует в HEAD — чек неприменим "
                            "(exists-guard J58)",
                       triggered=triggered)
    mb = merge_base(repo)
    if not mb:
        return _result("static_v", "SKIP", [],
                       note="merge-base с main не определен", triggered=triggered)
    base_text = git_out(repo, "show", f"{mb}:{PAGES_REL}")
    work_path = repo / PAGES_REL
    if base_text is None or not work_path.is_file():
        return _result("static_v", "SKIP", [],
                       note=f"{PAGES_REL} не читается (base или worktree)",
                       triggered=triggered)
    base_vals = [int(v) for v in STATIC_V_RE.findall(base_text)]
    work_vals = [int(v) for v in STATIC_V_RE.findall(
        work_path.read_text(encoding="utf-8", errors="replace"))]
    if not base_vals or not work_vals:
        return _result("static_v", "SKIP", [],
                       note="static_v в pages.py не найден", triggered=triggered)
    base_v, work_v = max(base_vals), max(work_vals)
    if work_v > base_v:
        return _result("static_v", "OK", [], base=base_v, worktree=work_v,
                       triggered=triggered)
    return _result(
        "static_v", "WARN",
        [f"static_v не бампнут относительно main (main={base_v}, "
         f"worktree={work_v}) — бампни ДО диспетчеризации (Н24/R7): "
         "сейчас круг уйдет впустую, ловится DENY-ом на PR"],
        base=base_v, worktree=work_v, triggered=triggered)


# --------------------------------------------- regress-staleness (Н24)

def _last_touch_commit(repo: Path, mb: str) -> str | None:
    args = ["log", f"{mb}..HEAD", "--format=%H", "-n", "1", "--"]
    out = git_out(repo, *args, *REGRESS_TOUCH_PATHS)
    if out and out.strip():
        return out.strip()
    return None


def check_regress_staleness(repo: Path) -> dict:
    """regress-final артефакт старше последнего касания tests/backend в ветке
    → WARN «не запускай, будет STALE» — до 15–25-минутного прогона."""
    regress_dir = repo / "test-model" / "regress"
    artifacts: list[Path] = []
    if regress_dir.is_dir():
        artifacts = sorted(p for p in regress_dir.glob("*/regress-final.md")
                           if p.is_file())
    if not artifacts:
        return _result("regress-staleness", "SKIP", [],
                       note="regress-final артефактов нет — чек неприменим")
    mb = merge_base(repo)
    if not mb:
        return _result("regress-staleness", "SKIP", [],
                       note="merge-base с main не определен")
    last = _last_touch_commit(repo, mb)
    if not last:
        return _result("regress-staleness", "OK", [],
                       note="ветка не трогает tests//backend/ после базы",
                       artifacts=[str(p.relative_to(repo)) for p in artifacts])
    warns: list[str] = []
    checked: list[dict] = []
    for art in artifacts:
        rel = str(art.relative_to(repo))
        text = art.read_text(encoding="utf-8", errors="replace")
        m = FINAL_RE.search(text)
        stale: bool | None = None
        how = ""
        if m:
            art_commit = m.group(1)
            out = git_out(repo, "merge-base", "--is-ancestor",
                          art_commit, last)
            if out is not None:
                same = art_commit == last
                stale = not same  # предок прогона (или не родня) — код уехал
                how = f"commit {art_commit} — предок {last[:12]}"
        if stale is None:
            # commit в артефакте не читается / не сверяется — свежесть по
            # mtime артефакта против времени последнего касания (как mtime-
            # fallback свежестных ворот).
            ts = git_out(repo, "show", "-s", "--format=%ct", last)
            if ts and ts.strip().isdigit():
                stale = art.stat().st_mtime < int(ts.strip())
                how = "mtime артефакта против времени последнего касания"
        checked.append({"artifact": rel, "stale": stale, "how": how})
        if stale:
            warns.append(
                f"{rel}: regress-final старше последнего касания tests//"
                f"backend/ ({how}) — НЕ запускай диспетчеризацию, прогон "
                "будет STALE (Н24): перегони регресс после фиксации кода")
    status = "WARN" if warns else "OK"
    return _result("regress-staleness", status, warns,
                   last_touch=last, artifacts=checked)


# --------------------------------------------- snapshot binding (Н17)

def check_snapshot(repo: Path, expect_branch: str | None) -> dict:
    """Н17: ворота читают рабочее дерево (--repo) — смена checkout меняет
    вердикты. Сверяем текущий checkout с ожидаемой веткой (HEAD == tip)."""
    head = git_out(repo, "rev-parse", "HEAD")
    if not head:
        return _result("snapshot", "SKIP", [], note="HEAD не читается")
    head = head.strip()
    branch = git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch.strip() if branch else None
    dirty_out = git_out(repo, "status", "--porcelain")
    dirty = sorted(
        ln[3:].strip() for ln in (dirty_out or "").splitlines() if ln.strip())
    warns: list[str] = []
    if expect_branch:
        if branch != expect_branch:
            warns.append(
                f"checkout дерева на «{branch}», ожидалась «{expect_branch}» "
                "(Н17: ворота читают это дерево — пересними снапшот на "
                "нужной ветке)")
        tip = git_out(repo, "rev-parse", "--verify", expect_branch)
        tip = tip.strip() if tip else None
        if tip and tip != head:
            warns.append(
                f"HEAD дерева ({head[:12]}) ≠ tip ветки {expect_branch} "
                f"({tip[:12]}) — вердикты ворот будут по чужому дереву")
    status = "WARN" if warns else "OK"
    return _result("snapshot", status, warns,
                   head=head, branch=branch, dirty=dirty,
                   expect_branch=expect_branch)


# ------------------------------------------- check pr-body (Н20/Н25)

def load_impact_rules(repo: Path) -> list[dict]:
    """Правила с pr_body-требованиями из impact_rules.json (тот же файл, что
    читают ворота pr_validate; файл/JSON не читаются — молча пусто: отсутствие
    правил — легитимное состояние, чек декларативный)."""
    path = repo / IMPACT_RULES_REL
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        return []
    out = []
    for r in rules:
        if not isinstance(r, dict) or not r.get("id") or not r.get("when"):
            continue
        bodies = [
            req for req in (r.get("require") or [])
            if isinstance(req, dict) and req.get("pr_body")
        ]
        if bodies:
            out.append({**r, "_pr_body": bodies})
    return out


def _diff_patch(repo: Path, mb: str | None) -> str:
    parts: list[str] = []
    if mb:
        out = git_out(repo, "diff", f"{mb}...HEAD")
        if out:
            parts.append(out)
    out = git_out(repo, "diff", "HEAD")
    if out:
        parts.append(out)
    return "\n".join(parts)


def check_pr_body(repo: Path, pr: str | None, body_file: str | None) -> dict:
    """Механика декларативных pr_body-требований IR-05/IR-06.

    Правило сработало (when[].path по диффу + опциональный content_when по
    патчу диффа) — в теле PR есть под каждый pr_body-regex хотя бы одно
    совпадение. Нет тела (нет PR-контекста) — SKIP: чек advisory, ворота
    merge остаются у pr_validate/gh.
    """
    rules = load_impact_rules(repo)
    if not rules:
        return _result("pr-body", "SKIP", [],
                       note="pr_body-правил в impact_rules.json нет")
    body = ""
    if pr:
        out = git_out(repo, "remote", "get-url", "origin")
        cmd = ["gh", "pr", "view", pr, "--json", "body"]
        if out and "github.com" in out:
            owner_repo = re.sub(r"^.*github\.com[:/]", "", out.strip()).removesuffix(".git")
            cmd += ["--repo", owner_repo]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode == 0:
                body = json.loads(proc.stdout).get("body") or ""
        except (OSError, ValueError, subprocess.TimeoutExpired):
            body = ""
    elif body_file:
        p = Path(body_file)
        body = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    else:
        body = os.environ.get("PR_BODY", "")
    mb = merge_base(repo)
    files = branch_diff_files(repo)
    patch = _diff_patch(repo, mb)
    warns: list[str] = []
    triggered_ids: list[str] = []
    for rule in rules:
        whens = rule["when"] if isinstance(rule["when"], list) else [rule["when"]]
        hit_paths = [
            p for _, p in files
            if any(isinstance(w, dict) and w.get("path")
                   and _safe_search(w["path"], p) for w in whens)
        ]
        if not hit_paths:
            continue
        content_ok = True
        for cw in rule.get("content_when") or []:
            if not isinstance(cw, dict) or not cw.get("regex"):
                continue
            scope = patch
            if cw.get("path"):
                chunks = [
                    chunk for chunk in patch.split("diff --git ")
                    if _safe_search(cw["path"], chunk.split("\n", 1)[0])
                ]
                scope = "\n".join(chunks)
            if not _safe_search(cw["regex"], scope):
                content_ok = False
                break
        if not content_ok:
            continue
        triggered_ids.append(rule["id"])
        if not body.strip():
            continue  # тело не читается — WARN ниже единый, правила перечислены
        for req in rule["_pr_body"]:
            if not _safe_search(req["pr_body"], body):
                warns.append(
                    f"{rule['id']} ({rule.get('title', rule['id'])}): дифф "
                    f"трогает {hit_paths[0]}, но в теле PR нет «"
                    f"{req.get('label') or req['pr_body']}» — "
                    f"{req.get('hint', 'добавь требуемое в тело PR')}")
    if body.strip() or not triggered_ids:
        status = "WARN" if warns else ("OK" if triggered_ids else "SKIP")
    else:
        status = "WARN"
        warns.append(
            "тело PR недоступно (укажи --pr N, --body-file F или env "
            "PR_BODY) — pr_body-требования не проверены: "
            + ", ".join(triggered_ids))
    return _result("pr-body", status, warns, rules_triggered=triggered_ids)


def _safe_search(pattern: str, text: str) -> bool:
    try:
        return bool(re.search(pattern, text))
    except re.error:
        return False


# ------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flow_preflight",
        description="Pre-flight чеки диспетчеризации (Н24/Н25/Н17)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_static = sub.add_parser("static_v", help="static_v бампнут? (Н24/R7)")
    p_static.add_argument("--repo", required=True)
    p_static.add_argument("--strict", action="store_true",
                          help="WARN → exit 1")

    p_regr = sub.add_parser("regress-staleness",
                            help="regress-final не протухнет? (Н24)")
    p_regr.add_argument("--repo", required=True)
    p_regr.add_argument("--strict", action="store_true",
                        help="WARN → exit 1")

    p_check = sub.add_parser("check", help="check pr-body — IR-05/06 (Н20/Н25)")
    check_sub = p_check.add_subparsers(dest="check_cmd", required=True)
    p_body = check_sub.add_parser("pr-body")
    p_body.add_argument("--repo", required=True)
    p_body.add_argument("--pr", help="номер PR (gh pr view)")
    p_body.add_argument("--body-file", help="файл с телом PR")
    p_body.add_argument("--strict", action="store_true",
                        help="WARN → exit 1")

    p_snap = sub.add_parser("snapshot", help="ветка == текущий checkout (Н17)")
    p_snap.add_argument("--repo", required=True)
    p_snap.add_argument("--branch", help="ожидаемая ветка снапшота")
    p_snap.add_argument("--strict", action="store_true")

    args = parser.parse_args(argv)
    strict = getattr(args, "strict", False)

    if args.cmd in ("static_v", "regress-staleness", "snapshot"):
        fn = {"static_v": check_static_v,
              "regress-staleness": check_regress_staleness,
              "snapshot": lambda repo: check_snapshot(
                  repo, getattr(args, "branch", None)),
              }[args.cmd]
        res = fn(Path(args.repo))
        code = 1 if (res["status"] == "WARN" and strict) else 0
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return code
    if args.cmd == "check":
        res = check_pr_body(Path(args.repo), args.pr, args.body_file)
        code = 1 if (res["status"] == "WARN" and strict) else 0
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return code
    parser.error("неизвестная команда")
    return 2


if __name__ == "__main__":
    sys.exit(main())
