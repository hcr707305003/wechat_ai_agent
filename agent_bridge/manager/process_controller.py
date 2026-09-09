from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Event
from typing import Any

from agent_bridge.application_paths import ApplicationPaths
from agent_bridge.config import load_config
from agent_bridge.lifecycle import send_lifecycle_command
from agent_bridge.logging_setup import open_redirected_log


class WorkbenchState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    EXITED = "exited"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WorkbenchStatus:
    state: WorkbenchState
    exit_code: int | None = None
    detail: str = ""
    window_visible: bool | None = None


class WorkbenchProcessController:
    def __init__(
        self,
        paths: ApplicationPaths,
        *,
        popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
        lifecycle_sender: Callable[..., dict[str, Any]] = send_lifecycle_command,
        lock_checker: Callable[[str | Path], bool] | None = None,
    ) -> None:
        self.paths = paths
        self._popen = popen
        self._lifecycle_sender = lifecycle_sender
        self._lock_checker = lock_checker or workbench_lock_is_held
        self._process: subprocess.Popen[Any] | None = None
        self._stop_requested = Event()

    def command(self) -> list[str]:
        config = str(self.paths.config_file)
        if self.paths.frozen:
            executable = self.paths.binary_root / "AgentBridge.exe"
            return [str(executable), "--config", config, "run"]
        return [
            sys.executable,
            "-m",
            "agent_bridge",
            "--config",
            config,
            "run",
        ]

    def status(self) -> WorkbenchStatus:
        try:
            response = self._lifecycle_sender(
                self.paths.config_file, "status", timeout_ms=350
            )
            if response.get("ok"):
                state = response.get("state")
                if state == "stopping":
                    self._stop_requested.set()
                    return WorkbenchStatus(WorkbenchState.STOPPING, detail="工作台正在释放资源，请稍候。")
                if state == "running":
                    # Older workbenches report running throughout resource cleanup.
                    if self._stop_requested.is_set():
                        return WorkbenchStatus(WorkbenchState.STOPPING, detail="已请求关闭，正在等待工作台退出。")
                    visible = response.get("visible")
                    return WorkbenchStatus(
                        WorkbenchState.RUNNING,
                        window_visible=visible if isinstance(visible, bool) else None,
                    )
        except (ConnectionError, OSError, RuntimeError, TimeoutError):
            pass
        if self._process is None:
            if self._lock_checker(self.paths.config_file):
                return WorkbenchStatus(
                    WorkbenchState.STOPPING if self._stop_requested.is_set() else WorkbenchState.STARTING,
                    detail=("工作台正在释放资源，请稍候。" if self._stop_requested.is_set()
                            else "Workbench lock is held while its control endpoint is starting"),
                )
            self._stop_requested.clear()
            return WorkbenchStatus(WorkbenchState.STOPPED)
        exit_code = self._process.poll()
        if exit_code is None:
            return WorkbenchStatus(
                WorkbenchState.STOPPING if self._stop_requested.is_set() else WorkbenchState.STARTING,
                detail=("工作台正在释放资源，请稍候。" if self._stop_requested.is_set()
                        else "Workbench process has not opened its control endpoint yet"),
            )
        self._stop_requested.clear()
        return WorkbenchStatus(WorkbenchState.EXITED, exit_code=exit_code)

    def start(self) -> WorkbenchStatus:
        current = self.status()
        if current.state == WorkbenchState.RUNNING:
            self.show()
            return current
        if current.state in {WorkbenchState.STARTING, WorkbenchState.STOPPING}:
            return current
        self.paths.initialize_user_data()
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        log_handle = open_redirected_log(self.paths.workbench_log)
        try:
            creation_flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            )
            child_environment = os.environ.copy()
            child_environment["PYTHONIOENCODING"] = "utf-8"
            self._process = self._popen(
                self.command(),
                cwd=str(self.paths.user_root),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
                env=child_environment,
            )
        finally:
            log_handle.close()
        return WorkbenchStatus(WorkbenchState.STARTING)

    def show(self) -> bool:
        return self._set_window_visibility("show")

    def hide(self) -> bool:
        return self._set_window_visibility("hide")

    def _set_window_visibility(self, command: str) -> bool:
        try:
            response = self._lifecycle_sender(
                self.paths.config_file, command, timeout_ms=1000
            )
        except (ConnectionError, OSError, RuntimeError, TimeoutError):
            return False
        return bool(response.get("ok"))

    def stop(self, *, timeout_seconds: float = 15.0) -> WorkbenchStatus:
        try:
            response = self._lifecycle_sender(
                self.paths.config_file, "shutdown", timeout_ms=1500
            )
        except (ConnectionError, OSError, RuntimeError, TimeoutError) as error:
            return WorkbenchStatus(WorkbenchState.UNKNOWN, detail=str(error))
        if not response.get("ok"):
            return WorkbenchStatus(
                WorkbenchState.UNKNOWN, detail=str(response.get("error", ""))
            )
        self._stop_requested.set()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while time.monotonic() < deadline:
            status = self.status()
            if status.state in {WorkbenchState.STOPPED, WorkbenchState.EXITED}:
                return status
            time.sleep(0.1)
        return WorkbenchStatus(
            WorkbenchState.STOPPING,
            detail="Workbench did not stop within the allowed time; it was not forced closed",
        )


def workbench_lock_is_held(config_path: str | Path) -> bool:
    try:
        config = load_config(config_path)
        lock_path = Path(config.runtime.database).with_suffix(".lock")
        if not lock_path.is_file():
            return False
        with lock_path.open("r+", encoding="utf-8") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                return False
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
