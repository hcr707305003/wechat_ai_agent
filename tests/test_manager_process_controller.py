from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_bridge.application_paths import ApplicationPaths
from agent_bridge.manager.process_controller import (
    WorkbenchProcessController,
    WorkbenchState,
)


def make_paths(tmp_path: Path, *, frozen: bool = False) -> ApplicationPaths:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "config.example.yaml").write_text("runtime: {}\n", encoding="utf-8")
    return ApplicationPaths(resources, tmp_path / "user", frozen)


def test_process_controller_builds_frozen_workbench_command(tmp_path: Path) -> None:
    paths = make_paths(tmp_path, frozen=True)
    controller = WorkbenchProcessController(paths)

    assert controller.command() == [
        str(paths.binary_root / "AgentBridge.exe"),
        "--config",
        str(paths.config_file),
        "run",
    ]


def test_process_controller_starts_once_and_redirects_log(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    calls = []
    process = SimpleNamespace(poll=lambda: None)

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return process

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("not running")

    controller = WorkbenchProcessController(
        paths, popen=popen, lifecycle_sender=unavailable
    )

    assert controller.start().state == WorkbenchState.STARTING
    assert controller.start().state == WorkbenchState.STARTING
    assert len(calls) == 1
    assert calls[0][1]["cwd"] == str(paths.user_root)
    assert paths.workbench_log.exists()


def test_process_controller_forces_utf8_child_output(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    calls = []
    process = SimpleNamespace(poll=lambda: None)

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return process

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("not running")

    controller = WorkbenchProcessController(
        paths, popen=popen, lifecycle_sender=unavailable
    )

    controller.start()

    child_environment = calls[0][1]["env"]
    assert child_environment is not os.environ
    assert child_environment["PYTHONIOENCODING"] == "utf-8"


def test_process_controller_existing_instance_is_shown_not_started(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    commands = []

    def lifecycle(_config, command, **_kwargs):
        commands.append(command)
        return {"ok": True, "state": "running"}

    controller = WorkbenchProcessController(
        paths,
        popen=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        lifecycle_sender=lifecycle,
    )

    assert controller.start().state == WorkbenchState.RUNNING
    assert commands == ["status", "show"]


def test_process_controller_stop_timeout_does_not_kill(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    process = SimpleNamespace(poll=lambda: None)

    def lifecycle(_config, command, **_kwargs):
        if command == "shutdown":
            return {"ok": True, "state": "stopping"}
        raise ConnectionError("endpoint closing")

    controller = WorkbenchProcessController(paths, lifecycle_sender=lifecycle)
    controller._process = process

    result = controller.stop(timeout_seconds=0)

    assert result.state == WorkbenchState.STOPPING
    assert "not forced" in result.detail


def test_process_controller_treats_held_lock_as_starting(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("endpoint not ready")

    controller = WorkbenchProcessController(
        paths,
        lifecycle_sender=unavailable,
        lock_checker=lambda _path: True,
    )

    assert controller.status().state == WorkbenchState.STARTING


@pytest.mark.parametrize("owned", [True, False])
def test_shutdown_timeout_does_not_revert_to_running(tmp_path, owned):
    paths = make_paths(tmp_path)
    state = {"endpoint": True, "alive": True}

    def lifecycle(_config, command, **_kwargs):
        if not state["endpoint"]:
            raise ConnectionError("closed")
        # Simulate the old endpoint, which always reported running.
        return {"ok": True, "state": "running"}

    controller = WorkbenchProcessController(
        paths, lifecycle_sender=lifecycle, lock_checker=lambda path: state["alive"],
        popen=lambda *args, **kwargs: pytest.fail("Must not restart while stopping"),
    )
    if owned:
        controller._process = SimpleNamespace(poll=lambda: None if state["alive"] else 0)
    assert controller.status().state == WorkbenchState.RUNNING
    assert controller.stop(timeout_seconds=0).state == WorkbenchState.STOPPING
    assert controller.status().state == WorkbenchState.STOPPING
    assert controller.start().state == WorkbenchState.STOPPING
    state["endpoint"] = False
    assert controller.status().state == WorkbenchState.STOPPING
    state["alive"] = False
    assert controller.status().state == (WorkbenchState.EXITED if owned else WorkbenchState.STOPPED)


def test_controller_recognizes_stopping_from_another_manager(tmp_path):
    controller = WorkbenchProcessController(
        make_paths(tmp_path),
        lifecycle_sender=lambda *args, **kwargs: {"ok": True, "state": "stopping"},
    )
    assert controller.status().state == WorkbenchState.STOPPING


def test_controller_reads_visibility_and_hides_without_shutdown(tmp_path):
    commands = []

    def lifecycle(_config, command, **kwargs):
        commands.append(command)
        return {"ok": True, "state": "running", "visible": False}

    controller = WorkbenchProcessController(make_paths(tmp_path), lifecycle_sender=lifecycle)
    assert controller.status().window_visible is False
    assert controller.hide()
    assert commands == ["status", "hide"]
