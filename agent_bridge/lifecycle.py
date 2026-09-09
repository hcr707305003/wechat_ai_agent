from __future__ import annotations

import json
import os
import secrets
import socket
import socketserver
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
from typing import Any

from PySide6.QtCore import QObject, Signal


def lifecycle_endpoint_name(config_path: str | Path) -> str:
    normalized = str(Path(config_path).resolve()).casefold().encode("utf-8")
    return "agent-bridge-" + sha256(normalized).hexdigest()[:24]


def lifecycle_endpoint_file(config_path: str | Path) -> Path:
    config = Path(config_path).resolve()
    return config.with_name(f".{lifecycle_endpoint_name(config)}.json")


class _LifecycleRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        owner: WorkbenchLifecycleServer = self.server.lifecycle_owner
        try:
            payload = json.loads(self.rfile.readline(64 * 1024).decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("token") != owner.token:
                response = {"ok": False, "error": "Invalid lifecycle token"}
            else:
                response = owner._dispatch(str(payload.get("command", "")))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as error:
            response = {"ok": False, "error": str(error)}
        self.wfile.write(
            json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
        )


class _LifecycleTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class WorkbenchLifecycleServer(QObject):
    shutdown_requested = Signal()
    show_requested = Signal()
    hide_requested = Signal()

    def __init__(self, config_path: str | Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config_path = Path(config_path).resolve()
        self.endpoint_file = lifecycle_endpoint_file(self.config_path)
        self.token = secrets.token_urlsafe(32)
        self._server: _LifecycleTcpServer | None = None
        self._thread: Thread | None = None
        self._stopping = Event()
        self._window_visible: bool | None = None

    def start(self) -> None:
        self._stopping.clear()
        self.endpoint_file.parent.mkdir(parents=True, exist_ok=True)
        server = _LifecycleTcpServer(("127.0.0.1", 0), _LifecycleRequestHandler)
        server.lifecycle_owner = self
        self._server = server
        port = int(server.server_address[1])
        temporary = self.endpoint_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"port": port, "token": self.token, "pid": os.getpid()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.endpoint_file)
        self._thread = Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="workbench-lifecycle",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self.begin_shutdown()
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        try:
            current = json.loads(self.endpoint_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            current = {}
        if isinstance(current, dict) and current.get("token") == self.token:
            try:
                self.endpoint_file.unlink()
            except FileNotFoundError:
                pass

    def begin_shutdown(self) -> None:
        self._stopping.set()

    def set_window_visible(self, visible: bool) -> None:
        # Updated on the Qt thread; socket handlers only read this snapshot.
        self._window_visible = bool(visible)

    def _dispatch(self, command: str) -> dict[str, Any]:
        if command == "status":
            response = {"ok": True, "state": "stopping" if self._stopping.is_set() else "running"}
            if self._window_visible is not None:
                response["visible"] = self._window_visible
            return response
        if command in {"show", "hide"}:
            if self._stopping.is_set():
                return {"ok": False, "state": "stopping", "error": "工作台正在关闭。"}
            if command == "show":
                self.show_requested.emit()
            else:
                self.hide_requested.emit()
            return {"ok": True, "state": "running"}
        if command == "shutdown":
            self.begin_shutdown()
            self.shutdown_requested.emit()
            return {"ok": True, "state": "stopping"}
        return {"ok": False, "error": f"Unsupported lifecycle command: {command}"}


def send_lifecycle_command(
    config_path: str | Path,
    command: str,
    *,
    timeout_ms: int = 1500,
) -> dict[str, Any]:
    endpoint = lifecycle_endpoint_file(config_path)
    try:
        settings = json.loads(endpoint.read_text(encoding="utf-8"))
        port = int(settings["port"])
        token = str(settings["token"])
    except (OSError, KeyError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ConnectionError(
            f"Workbench control endpoint is unavailable: {error}"
        ) from error
    timeout = max(0.05, timeout_ms / 1000)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as client:
            client.settimeout(timeout)
            payload = json.dumps({"command": command, "token": token}).encode("utf-8")
            client.sendall(payload + b"\n")
            response_data = client.makefile("rb").readline(64 * 1024)
    except (OSError, TimeoutError) as error:
        raise ConnectionError(f"Workbench control request failed: {error}") from error
    try:
        response = json.loads(response_data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TypeError("Invalid workbench lifecycle response") from error
    if not isinstance(response, dict):
        raise TypeError("Invalid workbench lifecycle response")
    return response
