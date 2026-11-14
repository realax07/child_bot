"""Исполнитель stub — внешняя команда с командной строкой pi (J65.5).

Нужен эталонному прогону (J65.10) и как доказательство «новый исполнитель —
один файл»: стадия `run: stub:<роль>` проходит те же ворота раннера.
Команда — env AF_EXECUTOR_STUB_CMD (путь к исполняемому файлу); вызов:
`<cmd> --role <роль> --model <модель> -p <бриф>`, env PI_ZONE_WRITE /
PI_ZONE_ROLE — как у pi. Нет env или файла — STOP EXECUTOR_UNAVAILABLE.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import ActiveRun, Executor, ExecutorUnavailable, StageLaunch, active_by_binary

ENV = "AF_EXECUTOR_STUB_CMD"


class StubExecutor(Executor):
    name = "stub"

    @staticmethod
    def _cmd() -> str:
        return os.environ.get(ENV, "").strip()

    def preflight(self) -> dict:
        cmd = self._cmd()
        if not cmd or not os.access(cmd, os.X_OK):
            raise ExecutorUnavailable(
                "EXECUTOR_UNAVAILABLE", f"{ENV} не задан или не исполняемый: {cmd!r}")
        return {"executor_cmd": cmd}

    def command(self, launch: StageLaunch) -> tuple[list[str], dict]:
        env = dict(launch.env)
        env["PI_ZONE_WRITE"] = ":".join(launch.zones)
        env["PI_ZONE_ROLE"] = launch.flow_role
        return [self._cmd(), "--role", launch.role, "--model", launch.model,
                "-p", launch.brief], env

    def list_active(self) -> list[ActiveRun]:
        cmd = self._cmd()
        return active_by_binary(self.name, Path(cmd).name) if cmd else []


EXECUTOR = StubExecutor()
