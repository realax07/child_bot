#!/usr/bin/env python3
"""factory_paths.py — единственный источник машинных путей фабрики (J64.5).

Фабрика не прошивает пути машины в коде: каждый путь вычисляется здесь и
переопределяется переменной окружения. Перенос фабрики на другую машину
или под другого пользователя = задать env, а не править скрипты.

Пути делятся на четыре вида — у каждого свой смысл и своя переменная:

| Функция            | Что это                                   | env                | Дефолт            |
|--------------------|-------------------------------------------|--------------------|-------------------|
| `state_dir()`      | память движка ворот: реестр сессий,       | `AF_STATE_DIR`     | `~/.hermes/state` |
|                    | flowctl_state, режим flow_mode, инциденты |                    |                   |
| `hermes_home()`    | данные исполнителя Hermes: state.db       | `HERMES_HOME`      | `~/.hermes`       |
|                    | (async_delegations), sessions, кеш, логи  |                    |                   |
| `factory_home()`   | где лежит репозиторий фабрики             | `AI_FACTORY_HOME`  | `~/ai-factory`    |
| `projects_root()`  | где лежат проекты (`<root>/<project>`)    | `AF_PROJECTS_ROOT` | `~`               |
| `pi_agent_dir()`   | конфиг исполнителя Pi (models, extensions)| `PI_AGENT_DIR`     | `~/.pi/agent`     |

`state_dir()` по умолчанию — `~/.hermes/state` (историческое место: фабрика
выросла на Hermes), но от `HERMES_HOME` он НЕ зависит: память движка общая
для всех процессов (шелл ПМ, крон, процесс Hermes, раннер). Если бы она
следовала `HERMES_HOME`, процесс с другим `HERMES_HOME` (например, профиль
Hermes) не нашел бы flow_mode.json — режим молча стал бы shadow — и реестр
резерваций раздвоился бы (ревью J64.5). Переопределение — только явно,
через `AF_STATE_DIR`.

Функции вычисляются в момент вызова (env и HOME читаются каждый раз):
скрипты с константами уровня модуля фиксируют значение при импорте, как и
раньше; тесты подменяют env (tests/conftest.py) или сами константы.

Модуль копируется в проекты вместе со скриптами ворот (factory_init:
COPY_FILES/FLOW_CONTROL_FILES) — только stdlib, без побочных эффектов.
"""
from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


def hermes_home() -> Path:
    """Данные исполнителя Hermes (state.db, sessions, кеш делегаций, логи)."""
    return _env_path("HERMES_HOME", Path.home() / ".hermes")


def state_dir() -> Path:
    """Память движка ворот: реестр сессий, flowctl_state, flow_mode, инциденты."""
    return _env_path("AF_STATE_DIR", Path.home() / ".hermes" / "state")


def registry_path() -> Path:
    """Реестр сессий (резервации зон) — общий для делегаций и раннера."""
    return state_dir() / "active_sessions.json"


def flowctl_state_path() -> Path:
    """State циклов flowctl (prepare/run/finish)."""
    return state_dir() / "flowctl_state.json"


def factory_home() -> Path:
    """Репозиторий фабрики (агенты, скрипты, скиллы)."""
    return _env_path("AI_FACTORY_HOME", Path.home() / "ai-factory")


def projects_root() -> Path:
    """Каталог, в котором лежат проекты (`<root>/<project>`)."""
    return _env_path("AF_PROJECTS_ROOT", Path.home())


def pi_agent_dir() -> Path:
    """Конфиг исполнителя Pi (models.json, extensions/)."""
    return _env_path("PI_AGENT_DIR", Path.home() / ".pi" / "agent")


def describe() -> dict[str, str]:
    """Текущие пути (для диагностики: `python3 scripts/factory_paths.py`)."""
    return {
        "state_dir": str(state_dir()),
        "registry": str(registry_path()),
        "flowctl_state": str(flowctl_state_path()),
        "hermes_home": str(hermes_home()),
        "factory_home": str(factory_home()),
        "projects_root": str(projects_root()),
        "pi_agent_dir": str(pi_agent_dir()),
    }


# --------------------------------------------------- doctor (J65.12)
# Перед мержем правок путей (J64.5) их сверяли руками: запустить этот модуль
# из шелла, из крона и из процесса Hermes и сравнить. `--doctor` снимает
# снимок контекста в JSON (env, пути, проверки), `--compare` сверяет снимки:
# пути, разошедшиеся между контекстами, — первая причина «ворота в кроне
# видят другой реестр».

DOCTOR_ENV = ("HOME", "HERMES_HOME", "AF_STATE_DIR", "AI_FACTORY_HOME",
              "AF_PROJECTS_ROOT", "PI_AGENT_DIR", "FLOW_MODE_FILE",
              "FLOW_ENFORCER_STATE_DIR", "PYTHONUSERBASE")


def doctor(label: str) -> dict:
    """Снимок контекста: env, пути и проверки существования."""
    import getpass
    import sys
    paths = describe()
    checks = {
        "state_dir_exists": Path(paths["state_dir"]).is_dir(),
        "registry_exists": Path(paths["registry"]).is_file(),
        "factory_has_flowctl":
            (Path(paths["factory_home"]) / "scripts" / "flowctl.py").is_file(),
        "hermes_home_exists": Path(paths["hermes_home"]).is_dir(),
        # J64.5: память движка не зависит от HERMES_HOME — расхождение
        # нормально, но видно в снимке.
        "state_under_hermes_home":
            Path(paths["state_dir"]).is_relative_to(Path(paths["hermes_home"])),
    }
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 — нет пользователя в окружении (контейнер)
        user = None
    return {
        "label": label,
        "user": user,
        "python": sys.executable,
        "env": {k: os.environ.get(k) for k in DOCTOR_ENV},
        "paths": paths,
        "checks": checks,
    }


def _snapshot_names(snapshots: list[dict], names: list[str] | None) -> list[str]:
    """Имя снимка в отчете: label (или переданное имя файла); совпадающие
    метки различаются номером — снимок `shell` с Mac и `shell` с VPS не
    схлопываются в один (иначе расхождение давало бы ложный OK)."""
    base = list(names) if names else [str(s.get("label", "?")) for s in snapshots]
    dup = {n for n in base if base.count(n) > 1}
    return [f"{n}#{i + 1}" if n in dup else n for i, n in enumerate(base)]


def compare(snapshots: list[dict], names: list[str] | None = None) -> list[str]:
    """Пути, различающиеся между снимками: «<путь>: <снимок>=<значение> …»."""
    labels = _snapshot_names(snapshots, names)
    diffs = []
    for key in describe():
        values = [(lbl, s.get("paths", {}).get(key))
                  for lbl, s in zip(labels, snapshots)]
        if len({v for _, v in values}) > 1:
            diffs.append(f"{key}: " + ", ".join(
                f"{lbl}={val}" for lbl, val in values))
    return diffs


def _load_snapshot(path: str) -> dict:
    """Снимок --doctor или ValueError с причиной (битый/неполный JSON).
    Строго: снимок обязан нести все пути текущего describe() — при смене
    состава путей снимки старой версии снимаются заново."""
    import json
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: не прочитан ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
        raise ValueError(f"{path}: не снимок --doctor (нет объекта paths)")
    missing = [k for k in describe() if k not in data["paths"]]
    if missing:
        raise ValueError(f"{path}: в снимке нет путей {', '.join(missing)}")
    return data


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys
    ap = argparse.ArgumentParser(description="Пути машины фабрики (J64.5)")
    ap.add_argument("--doctor", metavar="LABEL",
                    help="снимок контекста (shell/cron/hermes) в JSON")
    ap.add_argument("--out", help="файл или каталог снимка (по умолчанию stdout)")
    ap.add_argument("--compare", nargs="+", metavar="SNAPSHOT",
                    help="сверить снимки --doctor: exit 1 — пути расходятся, "
                         "exit 2 — снимки битые или их меньше двух")
    args = ap.parse_args(argv)

    if args.compare:
        if len(args.compare) < 2:
            print("--compare: нужно минимум два снимка", file=sys.stderr)
            return 2
        try:
            snaps = [_load_snapshot(p) for p in args.compare]
        except ValueError as exc:
            print(f"--compare: {exc}", file=sys.stderr)
            return 2
        names = [f"{s.get('label', '?')} ({Path(p).name})"
                 for s, p in zip(snaps, args.compare)]
        diffs = compare(snaps, names)
        labels = ", ".join(names)
        if diffs:
            print(f"РАСХОЖДЕНИЕ путей между контекстами ({labels}):")
            for d in diffs:
                print(f"  {d}")
            return 1
        print(f"OK — пути совпадают во всех контекстах ({labels})")
        return 0

    if args.doctor:
        text = json.dumps(doctor(args.doctor), ensure_ascii=False, indent=2)
        if args.out:
            out = Path(args.out).expanduser()
            if out.is_dir() or args.out.endswith("/"):
                out.mkdir(parents=True, exist_ok=True)
                out = out / f"factory-paths-{args.doctor}.json"
            out.write_text(text + "\n", encoding="utf-8")
            print(f"снимок записан: {out}")
        else:
            print(text)
        return 0

    for key, value in describe().items():
        print(f"{key:14} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
