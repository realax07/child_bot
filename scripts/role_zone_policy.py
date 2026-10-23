#!/usr/bin/env python3
"""role_zone_policy.py — P0.3: зоны записи из политики роли.

Источник политики — таблица «Зона записи (только она)» agents/README.md
(реестр ролей конвейера). P0.3 (пересмотр плана Заказчика): `--zone` CLI
НЕ может расширить политику — он только СУЖАЕТ. Запрошенные пути сверяются
с зоной роли; итоговая (суженная) зона — пересечение запроса с политикой —
становится резервацией и зоной сверки diff на finish. Запрос пути вне
политики роли — отказ с перечнем недопустимых путей и указанием политики.
Это процессное ограничение, а не ОС-песочница: обходит его только тот, кто
пишет файлы руками, минуя flowctl.

Версия политики (policy_version) — digest таблицы ролей + дата: фиксирует,
какой именно редакцией таблицы была ограничена зона записи. Записывается в
запись делегации flowctl и в резервацию реестра сессий — аудит сверяет, что
diff сверялся с зоной той же редакции, что действовала при prepare.

Роли, отсутствующие в таблице (в т.ч. relay/customer — они не
пишут в продуктовый репозиторий), получают пустую политику = запись
запрещена: неизвестная роль не может резервировать зоны через общий '**'.
Поправка Н13 (аудит 2026-10-23): integrator получил зону merge-артефактов
(code-reviews/**, test-model/**), pm — PLAN-*.md (J41) и docs/**; таблица
agents/README.md подлежит синхронной правке владельцем политики.
"""
from __future__ import annotations

import session_check as sc

# Digest вычисляется sha256 по таблице «Зона записи (только она)»
# agents/README.md (строка-заголовок «| Агент | Режим | …» до последней
# строки-ряда включительно, join "\n", без завершающего перевода строки,
# UTF-8) в указанном коммите.
POLICY_TABLE_DIGEST = (
    "agents/README.md@1b6040b:таблица-записи (роль→зона), sha256 "
    "fc87330d1b8344bce8fcdbb4bb4b31707f2125f2e7096494b453bd513a3876b1"
)
POLICY_TABLE_DATE = "2026-10-23"
# Поправка Н13 (аудит 2026-10-23): зоны integrator (merge-артефакты),
# pm (PLAN-*.md, docs/**) расширены относительно запиненной таблицы —
# редакция политики фиксируется в version-строке, чтобы резервации/сверки
# diff до и после поправки были различимы (контракт policy_version).
POLICY_AMENDMENT = "; поправка Н13: 2026-10-23 (integrator/pm-зоны, таблица синхронизирована)"
POLICY_VERSION = f"{POLICY_TABLE_DIGEST}; дата: {POLICY_TABLE_DATE}{POLICY_AMENDMENT}"
POLICY_SOURCE = "agents/README.md"

# ------------------------------------------------------------- политика ролей
# Источник — таблица «Зона записи (только она)» agents/README.md, дословно
# (каждой роли — её зона). Заголовки <id> — место подстановки change-id
# вызова; glob-семантика — session_check.glob_matches (заголовок матчится
# как '*' — ровно один сегмент).
ROLE_ZONE_POLICY: dict[str, tuple[str, ...]] = {
    "ba": (
        "requirements.md",
        "docs/ba/*",
    ),
    "sa": (
        "openspec/changes/*/proposal.md",
        # решение Заказчика 2026-10-05 (ekotov-wiki, дословно: «Расширяем (обе
        # репы учти)»): requirements.md/research.md — выходы роли SA
        # (sa_agent.md); прецедент OUT_OF_ZONE — R7 add-netdata-monitoring E1
        "openspec/changes/*/requirements.md",
        "openspec/changes/*/research.md",
        "openspec/changes/*/specs/**",
        "openspec/changes/*/design.md",
        "openspec/changes/*/tasks.md",
        "sdd.md",
    ),
    "dev": (
        "openspec/changes/*/tasks.md",
        # проектная адаптация (wiki, из ekotov-wiki@8cdd0af/462e017 — эталон
        # синхронизирован оттуда, директива двусторонней синхронизации
        # 2026-10-03): реальная структура монорепо + инфра-зона
        "backend/**",
        "frontend/**",
        "e2e/**",
        "services/**",
        "scripts/**",
        "contracts/**",
        # решение Заказчика 2026-10-04 (диалог, дословно: «Расширить зону
        # dev на deploy/**»): инфра-задачи микросервисного релиза (deploy.sh
        # v3 и последующие) исполняет роль dev — devops не входит в dev_task
        # графа Флоу 1 (WRONG_ROLE), а devops-зона не покрывает код задачи.
        "deploy/**",
        "src/**",
        "tests/**",
        "test-model/bugs/*",
        "feature/*",
    ),
    "code_reviewer": (
        "code-reviews/*/review-*.md",
    ),
    # remove-qa-checklist: зона роли qa_checklist удалена (роль упразднена);
    # test-model/checklists/ — append-only история, новых чеклистов не пишут.
    "qa_author": (
        "test-model/new/*/**",
    ),
    "qa_case_author": (
        "test-model/new/*/**",
    ),
    "qa_case_reviewer": (
        "test-model/reviews/**",
        "test-model/approved/**",
    ),
    "qa_automation": (
        "tests/**",
        "test-model/bugs/*",
    ),
    "qa_impact_analyst": (
        "test-model/impact/*.md",
    ),
    "qa_regression_analyst": (
        "test-model/regression/manifest.md",
    ),
    # J51.4 (review-003 #2): роль regress_runner — таблица agents/README.md,
    # дословно: «только `test-model/regress/<change-id>/`» (артефакты самих
    # прогонов пишет flowctl regress; glob-заголовок <change-id> → '*',
    # содержимое каталога — '/*/**', по образцу зоны qa_author).
    "regress_runner": (
        "test-model/regress/*/**",
    ),
    "architect": (
        "architecture/**",
        "test-model/reviews/*/review-*-architecture.md",
    ),
    "devops": (
        "deploy/**",
        "ops/**",
    ),
    # Н13 (аудит 2026-10-23): integrator легален в merge_task, но имел ПУСТУЮ
    # политику — merge-артефакты некому резервировать (любой prepare под
    # integrator давал violation). Зона покрывает артефакты слияния:
    # review-файлы code-reviews/ и контур test-model/; продуктовый код,
    # спеки и деплой по-прежнему вне зоны.
    "integrator": (
        "code-reviews/**",
        "test-model/**",
    ),
    "design_validator": (
        "test-model/reviews/*/review-*-design.md",
    ),
    "ui_designer": (
        "design/**",
    ),
    "dev_lead": (
        "openspec/changes/*/tasks.md",
    ),
    "pm": (
        "PLAN.md",
        # Н13 (аудит 2026-10-23): J41 обязывает ПМ писать PLAN-<id>.md
        # (план релиза change/бага/легкий Флоу 4) — pm-зона обязана их
        # покрывать, иначе prepare плана дает OUT_OF_ZONE.
        "PLAN-*.md",
        "BACKLOG.md",
        "README.md",
        # Н13 (аудит 2026-10-23): docs/** был ничьей — назначена docs-зона
        # ПМ (docs_task ведет ПМ-делегация; продуктовый код по-прежнему вне).
        "docs/**",
    ),
}

# Роли конвейера, ведущие запись ВНЕ продуктового репозитория (или не ведущие
# запись вообще): политика пуста = резервация/запись запрещены (P0.3).
# Н13 (аудит 2026-10-23): integrator исключен отсюда — легальная роль
# merge_task получила зону merge-артефактов (code-reviews/**, test-model/**).
EMPTY_POLICY_ROLES: tuple[str, ...] = ("relay", "customer")


def policy_version() -> str:
    """Версия политики для записи делегации (аудит: какая редакция таблицы)."""
    return POLICY_VERSION


def allowed_zones_for(role: str) -> tuple[str, ...]:
    """Допустимые glob-пути роли из политики; неизвестная роль — пусто."""
    return ROLE_ZONE_POLICY.get(str(role or "").strip(), ())


def _concrete(path: str) -> bool:
    return not any(ch in path for ch in "*?[")


def _pattern_lang_subset(sub: str, sup: str) -> bool:
    """Язык glob-паттерна sub ⊆ языка sup (по сегментной структуре).

    Грубая, но КОНСЕРВАТИВНАЯ проверка (в сторону отказа): фиксированные
    сегменты sub должны покрываться сегментами sup по позициям структуры
    (lit↔lit, lit↔glob в соответствующем ролевом ранге, glob↔glob),
    '**'-хвост sub допускается только при '**'-хвосте sup. Не распознает
    все случаи (например 'a/x*' ⊆ 'a/*y') — тогда отказ: запрос сужается
    жестче, чем мог бы, но никогда не расширяется.
    """
    sub_segs, sup_segs = sub.split("/"), sup.split("/")
    if "**" in sub_segs and "**" not in sup_segs:
        return False
    # Сопоставление слева: фиксированные сегменты sub против «ролевых» рангов
    # sup (0 = литерал каталога/файла, 1 = '*', 2 = '**'). '**' в sup
    # «поглощает» все оставшиеся сегменты sub.
    rank = {"lit": 0, "star": 1, "dstar": 2}

    def seg_rank(s: str) -> str:
        if s == "**":
            return "dstar"
        if any(ch in s for ch in "*?["):
            return "star"
        return "lit"

    i = j = 0
    while i < len(sub_segs):
        if j >= len(sup_segs):
            return False  # sub длиннее: не покрыто
        ssub, ssup = sub_segs[i], sup_segs[j]
        rsub, rsup = seg_rank(ssub), seg_rank(ssup)
        if rsup == "dstar":
            return True  # '**' поглощает весь остаток sub
        if rsub == "dstar":
            return False  # '**' в sub при не-'**' сегменте sup — расширение
        if rsub == "lit" and rsup == "lit":
            if ssub != ssup:
                return False
        elif rsub == "lit" and rsup == "star":
            pass  # '*' покрывает один литеральный сегмент
        else:  # glob в sub против glob/lit в sup — консервативный отказ
            return False
        i += 1
        j += 1
    return True


def _allowed_superset(zone: str, allowed: tuple[str, ...]) -> bool:
    """Запрошенная зона не выходит за пределы допустимых роли.

    Конкретный путь — по факту принадлежности (glob_matches). Glob-запрос —
    консервативное посегментное покрытие (_pattern_lang_subset): запрос,
    который нельзя доказать подмножеством политики, отвергается.
    """
    if _concrete(zone):
        return any(sc.glob_matches(a, zone) for a in allowed)
    return any(zone == a or _pattern_lang_subset(zone, a) for a in allowed)


def narrow_zones(role: str, requested: list | tuple) -> dict:
    """Сужение запрошенных зон до политики роли (P0.3).

    Возвращает:
      zones          — суженный список: только запрошенные зоны, покрытые
                       политикой (политика целиком НЕ подставляется);
      violations     — запрошенные пути вне политики роли (для отказа
                       с перечнем недопустимых путей);
      policy_version — версия политики (для записи делегации/резервации).

    Роль без политики (неизвестная/не пишущая): zones=[], violations=
    весь запрос. Сужение прозрачно: вызывающий обязан показать фактически
    зарезервированные (суженные) зоны, а не эхо --path.
    """
    allowed = allowed_zones_for(role)
    zones: list[str] = []
    violations: list[str] = []
    for raw in (requested or []):
        zone = sc.canonical(str(raw))
        if _allowed_superset(zone, allowed):
            if zone not in zones:
                zones.append(zone)
        else:
            violations.append(zone)
    return {"zones": zones, "violations": violations,
            "policy_version": policy_version()}
