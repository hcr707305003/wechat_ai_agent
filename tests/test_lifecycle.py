import os
import time
from pathlib import Path
from threading import Thread

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from agent_bridge.lifecycle import (
    WorkbenchLifecycleServer,
    lifecycle_endpoint_name,
    send_lifecycle_command,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


def test_lifecycle_endpoint_is_stable_and_config_specific(tmp_path: Path) -> None:
    first = tmp_path / "one" / "config.yaml"
    second = tmp_path / "two" / "config.yaml"

    assert lifecycle_endpoint_name(first) == lifecycle_endpoint_name(first)
    assert lifecycle_endpoint_name(first) != lifecycle_endpoint_name(second)


def test_lifecycle_server_dispatches_status_and_rejects_unknown(tmp_path: Path) -> None:
    server = WorkbenchLifecycleServer(tmp_path / "config.yaml")

    assert server._dispatch("status") == {"ok": True, "state": "running"}
    assert server._dispatch("invalid") == {
        "ok": False,
        "error": "Unsupported lifecycle command: invalid",
    }


def test_lifecycle_server_round_trips_local_command(
    qt_app: QApplication, tmp_path: Path
) -> None:
    config = tmp_path / "config.yaml"
    server = WorkbenchLifecycleServer(config)
    shown = []
    server.show_requested.connect(lambda: shown.append(True))
    server.start()
    responses = []
    errors = []

    def request() -> None:
        try:
            responses.append(send_lifecycle_command(config, "show"))
        except Exception as error:  # noqa: BLE001 - transferred to test thread
            errors.append(error)

    thread = Thread(target=request)
    thread.start()
    deadline = time.monotonic() + 3
    while thread.is_alive() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    thread.join(timeout=0.1)
    qt_app.processEvents()
    server.close()

    assert errors == []
    assert responses == [{"ok": True, "state": "running"}]
    assert shown == [True]


def test_shutdown_status_remains_stopping_until_endpoint_closes(qt_app, tmp_path):
    config = tmp_path / "config.yaml"
    server = WorkbenchLifecycleServer(config)
    server.start()
    try:
        assert send_lifecycle_command(config, "status")["state"] == "running"
        assert send_lifecycle_command(config, "shutdown")["state"] == "stopping"
        for _ in range(3):
            assert send_lifecycle_command(config, "status")["state"] == "stopping"
        assert send_lifecycle_command(config, "show")["ok"] is False
    finally:
        server.close()
    with pytest.raises(ConnectionError):
        send_lifecycle_command(config, "status")


def test_local_shutdown_also_updates_lifecycle_state(tmp_path):
    server = WorkbenchLifecycleServer(tmp_path / "config.yaml")
    server.begin_shutdown()
    assert server._dispatch("status")["state"] == "stopping"
