#!/usr/bin/env python3
"""role_zone_policy.py — P0.3: зоны записи из политики роли.

Источник политики — единый реестр ролей `agents/roles.json` (J65.1): зоны
записи каждой роли (glob), а также строки таблицы зон `agents/README.md` и
таблицы роль-паков, которые генерирует `scripts/gen_role_docs.py`. До J65.1
зоны жили в словаре этого модуля, а таблица README — отдельным текстом с
отпечатком, и они расходились (у architect/devops/design_validator строк в
таблице не было).

P0.3 (пересмотр плана Заказчика): `--zone` CLI НЕ может расширить политику —
он только СУЖАЕТ. Запрошенные пути сверяются с зоной роли; итоговая
(суженная) зона — пересечение запроса с политикой — становится резервацией и
зоной сверки diff на finish. Запрос пути вне политики роли — отказ с
перечнем недопустимых путей и указанием политики. Это процессное
ограничение, а не ОС-песочница: обходит его только тот, кто пишет файлы
руками, минуя flowctl.

Версия политики (policy_version) — sha256 файла реестра: фиксирует, какой
именно редакцией реестра была ограничена зона записи. Записывается в запись
делегации flowctl и в резервацию реестра сессий — аудит сверяет, что diff
сверялся с зоной той же редакции, что действовала при prepare.

Отпечаток зоны роли (role_zone_digest, J65.2) — sha256 списка зон ОДНОЙ
роли. Пишется в резервацию рядом с policy_version; finish сверяет именно
его: правка зоны чужой роли не роняет активную сессию, изменение своей —
ZONE_POLICY_MISMATCH.

Роли, отсутствующие в реестре, получают пустую политику = запись запрещена:
неизвестная роль не может резервировать зоны через общий '**'. Роли с
`empty_policy` (relay/customer) — то же, явно.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import session_check as sc

# Реестр ролей: в фабрике — agents/roles.json рядом со scripts/; в проекте —
# его копия (factory_init FLOW_CONTROL_FILES). Нет реестра — модуль не
# импортируется (fail-closed: ворота без политики не работают молча).
ROLES_PATH = Path(__file__).resolve().parent.parent / "agents" / "roles.json"
POLICY_SOURCE = "agents/roles.json"


def _load_registry(path: Path = ROLES_PATH) -> tuple[dict, str]:
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"реестр ролей {path} не прочитан ({exc}) — политика зон "
            f"недоступна (J65.1)") from exc
    if data.get("schema") != "roles/1" or not isinstance(data.get("roles"), list):
        raise RuntimeError(f"реестр ролей {path}: ожидается schema roles/1")
    return data, hashlib.sha256(raw).hexdigest()


REGISTRY, _REGISTRY_SHA = _load_registry()
POLICY_VERSION = f"{POLICY_SOURCE} sha256 {_REGISTRY_SHA}"

# Имена сохранены для вызывающих: роль → кортеж glob-зон; glob-семантика —
# session_check.glob_matches ('*' — ровно один сегмент: место change-id).
# Плейсхолдер реестра CHANGE_ID ('<id>') — сегмент change-id; для ворот это
# '*' (любой change-id), раннер подставляет id пакета (policy_zones).
CHANGE_ID = "<id>"
ROLE_ZONE_POLICY: dict[str, tuple[str, ...]] = {
    r["id"]: tuple(z.replace(CHANGE_ID, "*") for z in r.get("zones") or ())
    for r in REGISTRY["roles"] if r.get("zones")
}


def registry_zones_for(role: str) -> tuple[str, ...]:
    """Зоны роли в форме реестра (с '<id>') — для подстановки change-id."""
    for r in REGISTRY["roles"]:
        if r["id"] == str(role or "").strip():
            return tuple(r.get("zones") or ())
    return ()

# Роли конвейера, ведущие запись ВНЕ продуктового репозитория (или не ведущие
# запись вообще): политика пуста = резервация/запись запрещены (P0.3).
EMPTY_POLICY_ROLES: tuple[str, ...] = tuple(
    r["id"] for r in REGISTRY["roles"] if r.get("empty_policy"))


def policy_version() -> str:
    """Версия политики для записи делегации (аудит: какая редакция таблицы)."""
    return POLICY_VERSION


def role_zone_digest(role: str) -> str:
    """Отпечаток зоны ОДНОЙ роли (J65.2): sha256 канонического списка ее зон
    в форме реестра. Пишется в резервацию; finish сверяет его, а не версию
    реестра целиком — правка зоны чужой роли активную сессию не задевает.
    Роль без политики — отпечаток пустого списка (стабильная строка)."""
    rid = str(role or "").strip()
    zones = sorted(registry_zones_for(rid))
    raw = json.dumps(zones, ensure_ascii=False, separators=(",", ":"))
    return f"role={rid} sha256 {hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


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
      policy_version — версия политики (аудит: редакция реестра целиком);
      role_zone_digest — отпечаток зоны этой роли (J65.2: сверка на finish).

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
            "policy_version": policy_version(),
            "role_zone_digest": role_zone_digest(role)}
