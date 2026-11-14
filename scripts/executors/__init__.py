"""Исполнители агентных стадий пакетного раннера (J65.5 + J64.7).

Исполнитель — модуль `scripts/executors/<имя>.py` с объектом `EXECUTOR`
(подкласс `Executor`). Манифест выбирает его префиксом стадии
`run: <имя>:<роль>` (решение Заказчика 2026-11-12 «Префикс run»); реестр
находит модуль по имени — регистрации в раннере нет. Новый исполнитель —
один файл здесь, без правок flowctl/gate_runner/session_check: ворота стадии
(prepare, резервация, сверка диффа, гейты, provenance) делает раннер вокруг
вызова, одинаково для любого исполнителя.

Интерфейс (соответствие ТЗ J65.5: start/wait/list_active/transcript):
  manual            — ручная стоп-точка ПМ (раннер не исполняет, ждет ПМ);
  preflight()       — готовность до ворот входа; провал — ExecutorUnavailable;
                      возвращает provenance (например, sha256 guard'а);
  prepare_workdir() — ролевая инструкция в рабочий каталог (start);
  command(launch)   — argv + env процесса (start); запуск с таймаутом и
                      ожидание (wait) — общий spawn раннера;
  list_active()     — активные запуски исполнителя для вотчдога обхода;
  transcript(ref)   — транскрипт запуска, если исполнитель его хранит.
"""
from __future__ import annotations

import importlib
import os
import pkgutil
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ExecutorUnavailable(Exception):
    """Исполнитель не готов (нет guard'а, команды, бинаря) — STOP стадии до
    ворот входа. code — машиночитаемый код отказа."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class StageLaunch:
    """Что исполнитель получает от раннера на одну агентную стадию."""
    role: str              # роль-пак (dev, code-reviewer, …)
    flow_role: str         # роль политики (dev, code_reviewer, …)
    workdir: Path          # worktree пакета
    brief: str             # бриф стадии (собирает раннер)
    zones: list[str]       # зоны записи — префикс-форма (normalize_zone, guard pi)
    provider: str
    model: str
    package: str
    env: dict = field(default_factory=dict)   # env процесса (раннер: os.environ + стенд)
    zone_globs: list[str] = field(default_factory=list)   # зоны манифеста как есть


@dataclass
class ActiveRun:
    """Активный запуск исполнителя (для вотчдога обхода)."""
    executor: str
    pid: int | None
    cwd: str
    command: str
    ref: str = ""          # id запуска у исполнителя (делегация Hermes и т.п.)


class Executor:
    name = ""
    manual = False         # True — стоп-точка ПМ, раннер не запускает процесс
    agents_note = ("этот файл — ролевая инструкция исполнителя, проектные "
                   "правила — в AGENTS.md.")

    def preflight(self) -> dict:
        return {}

    def prepare_workdir(self, workdir: Path, role_text: str) -> tuple[str, list[Path]]:
        """Роль → AGENTS.md в корень рабочего каталога; если там уже есть
        AGENTS.md проекта — рядом AGENTS.<имя>.md (проектные правила не
        затираются, BUG-026). Возвращает (имя файла для брифа, файлы на
        уборку после запуска — в дифф стадии они не попадают)."""
        target = workdir / "AGENTS.md"
        if target.exists():
            path = workdir / f"AGENTS.{self.name}.md"
            path.write_text(role_text + "\n---\n> Читай вместе с AGENTS.md "
                            "проекта: " + self.agents_note + "\n", encoding="utf-8")
        else:
            path = target
            path.write_text(role_text, encoding="utf-8")
        return path.name, [path]

    def command(self, launch: StageLaunch) -> tuple[list[str], dict]:
        raise NotImplementedError

    def list_active(self) -> list[ActiveRun]:
        return []

    def transcript(self, ref: str) -> str | None:
        return None


def names() -> list[str]:
    """Имена всех исполнителей (модули пакета с EXECUTOR). Сломанный модуль
    пропускается с WARN — он не роняет остальных (ревью review-002)."""
    out = []
    for info in pkgutil.iter_modules(__path__):
        if not NAME_RE.match(info.name):
            continue
        try:
            ok = get(info.name, required=False) is not None
        except Exception as exc:   # noqa: BLE001 — чужой модуль, любая ошибка
            print(f"WARN: исполнитель {info.name} не загружен ({exc!r})",
                  file=sys.stderr)
            continue
        if ok:
            out.append(info.name)
    return sorted(out)


def get(name: str, required: bool = True) -> Executor | None:
    if not NAME_RE.match(name or ""):
        if required:
            raise KeyError(f"недопустимое имя исполнителя: {name!r}")
        return None
    try:
        mod = importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"{__name__}.{name}":
            raise                          # сломан сам модуль — не маскируем
        if required:
            raise KeyError(f"исполнитель {name!r} не найден "
                           f"(scripts/executors/{name}.py)") from None
        return None
    ex = getattr(mod, "EXECUTOR", None)
    if not isinstance(ex, Executor):
        if required:
            raise KeyError(f"scripts/executors/{name}.py без EXECUTOR")
        return None
    return ex


# ------------------------------------------------- процессы (для list_active)

def processes() -> list[tuple[int, str]]:
    """(pid, командная строка) процессов машины; недоступно — []."""
    # -ww: без обрезки командной строки по ширине (procps режет args, и имя
    # бинаря в длинном пути теряется); BusyBox без -ww — запасной вызов
    out = ""
    for argv in (["ps", "-ww", "-eo", "pid=,args="], ["ps", "-eo", "pid=,args="]):
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            out = proc.stdout
            break
    rows = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), cmd.strip()))
    return rows


def process_cwd(pid: int) -> str | None:
    """Рабочий каталог процесса: /proc (Linux) или lsof (macOS)."""
    proc = Path(f"/proc/{pid}/cwd")
    if proc.exists():
        try:
            return os.path.realpath(proc)
        except OSError:
            return None
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.startswith("n"):
            return os.path.realpath(line[1:])
    return None


# Интерпретаторы: исполнитель на node/python/… виден в ps как
# `<интерпретатор> [флаги] …/<бинарь> …` (Pi — npm-пакет на node; ревью
# executor-adapter review-001).
INTERPRETER_RE = re.compile(r"^(node|nodejs|bun|deno|python[0-9.]*|pypy[0-9.]*|"
                            r"sh|bash|zsh|dash|env)$", re.I)


# Флаги интерпретаторов — по интерпретатору (ревью review-003): со значением
# (значение — следующее слово) и «код вместо файла» (-m модуль, -c/-e строка).
VALUE_FLAGS = {
    "python": {"-X", "-W", "--check-hash-based-pycs"},
    "node": {"-r", "--require", "--import", "--loader", "--experimental-loader",
             "--inspect-port", "--title", "--conditions", "-C"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "sh": {"-o", "+o"},
}
CODE_FLAGS = {
    "python": {"-m", "-c"},
    "node": {"-e", "-p", "--eval", "--print"},
    "deno": {"eval"}, "bun": {"-e", "--eval"},
    "sh": {"-c"},
}
SHELLS = {"sh", "bash", "zsh", "dash"}


def _family(base: str) -> str:
    b = base.lower()
    if b.startswith(("python", "pypy")):
        return "python"
    if b in ("node", "nodejs"):
        return "node"
    if b in SHELLS:
        return "sh"
    return b


def runs_binary(cmd: str, binary: str) -> bool:
    """Командная строка запускает бинарь: argv[0], или файл-аргумент
    интерпретатора (его флаги и их значения пропускаются, цепочка `env node
    …`), или любое звено `sh -c "<строка>"`. `python -m x`, `node -e …` —
    код, не файл."""
    words = cmd.split()
    i = 0
    while i < len(words):
        base = os.path.basename(words[i])
        if base == binary:
            return True
        if not INTERPRETER_RE.match(base):
            return False
        fam = _family(base)
        i += 1
        while i < len(words) and (words[i].startswith(("-", "+")) or "=" in words[i]):
            flag = words[i].split("=", 1)[0]
            if flag in CODE_FLAGS.get(fam, ()):
                if fam == "sh" and flag == "-c" and i + 1 < len(words):
                    text = " ".join(words[i + 1:]).strip("\"'")
                    return any(runs_binary(part.strip(), binary)
                               for part in re.split(r"[;&|\n]+", text) if part.strip())
                return False
            i += 2 if flag in VALUE_FLAGS.get(fam, ()) and "=" not in words[i] else 1
    return False


def active_by_binary(executor: str, binary: str) -> list[ActiveRun]:
    """Активные процессы исполнителя (по имени бинаря, в т.ч. под интерпретатором)."""
    own = os.getpid()
    runs = []
    for pid, cmd in processes():
        if pid == own or not runs_binary(cmd, binary):
            continue
        cwd = process_cwd(pid)
        if cwd:
            runs.append(ActiveRun(executor, pid, cwd, cmd))
    return runs

