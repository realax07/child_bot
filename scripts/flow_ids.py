#!/usr/bin/env python3
"""flow_ids.py — единая грамматика scope флоу (J65.3).

До J65.3 формат scope разбирался в нескольких местах (flow_state, flowctl,
flow_transition, энфорсер, flow_check) и расходился: энфорсер пропускал
Ф2 `BUG-AF-RECHECK-001`, а движок отвечал FLOW_ID_INVALID; Ф3 `S1-…`
энфорсер требовал, а движок не принимал вовсе. Теперь грамматика — только
здесь; остальные импортируют модуль. Автономные копии (flow_check в
проектах, энфорсер внутри Hermes) держат литерал или fallback-копию,
совпадение с модулем проверяет тест (tests/unit/test_j65_flow_ids.py).

Грамматика (решения Заказчика 2026-11-05):

| Флоу | scope                                  | пример                     |
|------|----------------------------------------|----------------------------|
| 1    | kebab-case change-id, ≥1 дефис         | add-widget                 |
| 2    | имя репорта test-model/bugs/           | BUG-001, BUG-AF-RECHECK-001, BUG-001-search-card |
| 3    | incident-ref S<1-3>-<slug> (канон);    | S1-prod-down               |
|      | легаси BUG-… — только движок           | BUG-042                    |
| 4    | `chore` или kebab-slug                 | repo-cleanup               |
| 5    | kebab-slug (дефис не обязателен)       | typo, fix-footer           |

Энфорсер для Ф3 требует incident-ref (`legacy=False`): severity-
классификация обязательна до реакции (J48, FR-1/FR-4); движок принимает и
легаси BUG-… (на нем построены снимки Флоу 3).
"""
from __future__ import annotations

import re

FLOW_IDS = (1, 2, 3, 4, 5)

CHANGE_ID_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)+$"
SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
# Имя репорта test-model/bugs/<scope>.md (BUG-AF-RECHECK, 5c21489):
# BUG-001, BUG-AF-RECHECK-001, BUG-001-search-card-click; только заглавные
# в коде бага — без re.IGNORECASE.
BUG_ID_PATTERN = r"^BUG-(?:[A-Z0-9]+-)*\d{3}(?:-[a-z0-9][a-z0-9.-]*)?$"
# Severity-уровни Ф3 — одним allowlist-туплом (J48 I-1): новый уровень —
# одна правка здесь + отдельный change-пакет.
SEVERITY_PREFIXES = ("S1", "S2", "S3")
SEVERITY_PREFIX_PATTERN = (
    "^(" + "|".join(re.escape(p) for p in SEVERITY_PREFIXES) + ")-")
INCIDENT_REF_PATTERN = (
    "^(?:" + "|".join(re.escape(p) for p in SEVERITY_PREFIXES) + ")"
    r"-[a-z0-9]+(?:-[a-z0-9]+)*$")

CHANGE_ID_RE = re.compile(CHANGE_ID_PATTERN)
SLUG_RE = re.compile(SLUG_PATTERN)
BUG_ID_RE = re.compile(BUG_ID_PATTERN)
SEVERITY_PREFIX_RE = re.compile(SEVERITY_PREFIX_PATTERN)
INCIDENT_REF_RE = re.compile(INCIDENT_REF_PATTERN)

# Человекочитаемый формат scope по флоу — для диагностики отказов.
SCOPE_HINTS = {
    1: "kebab-case change-id, например add-widget",
    2: "BUG-<КОМПЛЕКС>-NNN, например BUG-042 или BUG-AF-RECHECK-001",
    3: "incident-ref S<1|2|3>-<slug>, например S1-prod-down",
    4: "chore или kebab-slug пакета, например repo-cleanup",
    5: "kebab-slug, например fix-typo",
}


def incident_severity(scope: str | None) -> str | None:
    """Severity-префикс incident-ref ('S1'…'S3') или None (не классифицирован)."""
    m = SEVERITY_PREFIX_RE.match(scope or "")
    return m.group(1) if m else None


def valid_scope(flow: int, scope: str, *, legacy: bool = True) -> bool:
    """scope соответствует грамматике своего флоу (fullmatch: `$` пропустил
    бы завершающий перевод строки — `add-widget\n`).

    legacy=True (движок): Ф3 принимает и BUG-…; legacy=False (маркер
    делегации): Ф3 — только incident-ref с severity."""
    scope = scope or ""
    if flow == 1:
        return bool(CHANGE_ID_RE.fullmatch(scope))
    if flow == 2:
        return bool(BUG_ID_RE.fullmatch(scope))
    if flow == 3:
        if INCIDENT_REF_RE.fullmatch(scope):
            return True
        return legacy and bool(BUG_ID_RE.fullmatch(scope))
    if flow in (4, 5):
        return bool(SLUG_RE.fullmatch(scope))
    return False


if __name__ == "__main__":  # pragma: no cover — ручная проверка scope
    import sys
    if len(sys.argv) != 3:
        print("usage: flow_ids.py <flow 1-5> <scope>", file=sys.stderr)
        sys.exit(2)
    f, s = int(sys.argv[1]), sys.argv[2]
    ok = valid_scope(f, s)
    print(f"Флоу {f}: {s!r} — {'OK' if ok else 'не соответствует: ' + SCOPE_HINTS.get(f, '?')}")
    sys.exit(0 if ok else 1)
