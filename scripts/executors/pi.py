"""Исполнитель pi — боевой для 5.3-ролей (J59): Pi роль-пак + zone-guard.

Командная строка: `pi --provider P --model M --no-session --no-extensions
-e <zone-guard репо> -p <бриф>` (--no-extensions: автозагрузка расширений
~/.pi и .pi/ проекта выключена — действует только эталон guard'а, J64.10).
Env guard'а: PI_ZONE_WRITE (зоны, префикс-форма), PI_ZONE_ROLE (bash-правила),
PI_ZONE_GUARD_LOG (лог отказов вне worktree). Нет guard'а — STOP
ZONE_GUARD_MISSING до ворот входа (решение Заказчика 2026-11-11 «Прямо из
репо»).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import ActiveRun, Executor, ExecutorUnavailable, StageLaunch, active_by_binary

FACTORY = Path(__file__).resolve().parents[2]
ZONE_GUARD_REL = Path("skills") / "pm-factory" / "pi" / "zone-guard.ts"
ZONE_GUARD_MISSING = "ZONE_GUARD_MISSING"


def _state_dir() -> Path:
    import factory_paths as fp   # scripts/ в sys.path у раннера
    return fp.state_dir()


class PiExecutor(Executor):
    name = "pi"
    agents_note = ("этот файл — ролевая инструкция Pi, проектные правила — "
                   "в AGENTS.md.")

    def __init__(self) -> None:
        self.guard_path = FACTORY / ZONE_GUARD_REL

    def preflight(self) -> dict:
        if not self.guard_path.is_file():
            raise ExecutorUnavailable(
                ZONE_GUARD_MISSING, f"zone-guard не найден ({self.guard_path}) — "
                "pi без guard'а роли не запускается (J64.10)")
        sha = hashlib.sha256(self.guard_path.read_bytes()).hexdigest()
        return {"zone_guard_sha256": sha,
                "zone_guard_ref": f"{ZONE_GUARD_REL.as_posix()} sha256 {sha}"}

    @staticmethod
    def guard_log(package: str) -> Path:
        return _state_dir() / "zone-guard" / f"{package}.log"

    def command(self, launch: StageLaunch) -> tuple[list[str], dict]:
        env = dict(launch.env)
        env["PI_ZONE_WRITE"] = ":".join(launch.zones)
        env["PI_ZONE_ROLE"] = launch.flow_role
        env["PI_ZONE_GUARD_LOG"] = str(self.guard_log(launch.package))
        argv = ["pi", "--provider", launch.provider, "--model", launch.model,
                "--no-session", "--no-extensions", "-e", str(self.guard_path),
                "-p", launch.brief]
        return argv, env

    def list_active(self) -> list[ActiveRun]:
        return active_by_binary(self.name, "pi")


EXECUTOR = PiExecutor()
