#!/usr/bin/env python3
"""ruleset_check.py — проверка ruleset ветки main (J64.9, аварийный merge).

Сверяет, что ruleset main в боевом состоянии: нацелен на ветку main,
enforcement=active, bypass — никому, правила «только PR», «без force-push» и обязательные проверки
`test-flow-control` и `flow`. Используется в RUNBOOK аварийного merge
(docs/runbook-emergency-merge.md): до процедуры (снимок), после нее
(ruleset возвращен) и в любой момент для аудита.

Usage:
  python3 scripts/ruleset_check.py [--repo owner/name] [--from-json FILE] [--json]

  --repo       репозиторий (по умолчанию — origin текущего клона через gh);
  --from-json  проверить сохраненный ответ `gh api repos/<r>/rulesets/<id>`
               (офлайн, для тестов и снимков);
  --json       машинный вывод.

Exit: 0 — ruleset в боевом состоянии; 1 — расхождение (список в выводе);
2 — ruleset не прочитан или состояние не доказуемо (нет gh/доступа,
ruleset main не найден, в ответе нет bypass_actors).
Только чтение: ruleset этот скрипт не меняет.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

RULESET_NAME = "main"
REQUIRED_CHECKS = ("test-flow-control", "flow")
REQUIRED_RULES = ("pull_request", "non_fast_forward", "required_status_checks")


def _gh(args: list[str]) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"gh {' '.join(args)}: rc {r.returncode}")
    return r.stdout


def fetch_ruleset(repo: str | None) -> dict:
    if not repo:
        repo = _gh(["repo", "view", "--json", "nameWithOwner",
                    "--jq", ".nameWithOwner"]).strip()
    listing = json.loads(_gh(["api", f"repos/{repo}/rulesets"]))
    match = [r for r in listing if r.get("name") == RULESET_NAME]
    if not match:
        raise RuntimeError(f"{repo}: ruleset «{RULESET_NAME}» не найден")
    return json.loads(_gh(["api", f"repos/{repo}/rulesets/{match[0]['id']}"]))


TARGET_REFS = ("refs/heads/main", "~DEFAULT_BRANCH")


class Unknown(Exception):
    """Ответ не позволяет судить о состоянии (нет полей) — exit 2."""


def problems(ruleset: dict) -> list[str]:
    out: list[str] = []
    if "bypass_actors" not in ruleset:
        # GitHub отдает поле только пользователю с правом записи в ruleset:
        # без него «bypass никому» не доказано — не OK, а UNKNOWN.
        raise Unknown("в ответе нет bypass_actors (нужно право admin/write "
                      "на ruleset) — состояние bypass не проверено")
    if ruleset.get("enforcement") != "active":
        out.append(f"enforcement={ruleset.get('enforcement')!r} (нужно active)")
    if ruleset.get("bypass_actors"):
        out.append(f"bypass_actors не пуст: {ruleset['bypass_actors']}")
    if ruleset.get("target", "branch") != "branch":
        out.append(f"target={ruleset.get('target')!r} (нужно branch)")
    include = (((ruleset.get("conditions") or {}).get("ref_name") or {})
               .get("include") or [])
    if not any(ref in include for ref in TARGET_REFS):
        out.append(f"ruleset не нацелен на main (conditions.ref_name.include="
                   f"{include})")
    rules = {r.get("type"): r for r in ruleset.get("rules") or []}
    for t in REQUIRED_RULES:
        if t not in rules:
            out.append(f"нет правила {t}")
    checks = {c.get("context") for c in
              ((rules.get("required_status_checks") or {}).get("parameters") or {})
              .get("required_status_checks") or []}
    for c in REQUIRED_CHECKS:
        if c not in checks:
            out.append(f"нет обязательной проверки {c}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo")
    ap.add_argument("--from-json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        ruleset = (json.loads(Path(args.from_json).read_text(encoding="utf-8"))
                   if args.from_json else fetch_ruleset(args.repo))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired,
            FileNotFoundError) as exc:
        print(f"ruleset_check: ruleset не прочитан: {exc}", file=sys.stderr)
        return 2
    try:
        found = problems(ruleset)
    except Unknown as exc:
        if args.json:
            print(json.dumps({"ok": False, "unknown": True, "problems": [str(exc)]},
                             ensure_ascii=False))
        print(f"ruleset_check: UNKNOWN — {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"ok": not found, "enforcement": ruleset.get("enforcement"),
                          "problems": found}, ensure_ascii=False))
    elif found:
        print("ruleset_check: РАСХОЖДЕНИЕ — " + "; ".join(found))
    else:
        print("ruleset_check: OK — ruleset main active, bypass никому, "
              "PR-only, без force-push, проверки " + ", ".join(REQUIRED_CHECKS))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
