from __future__ import annotations

import asyncio
import inspect
import sys
from types import ModuleType, SimpleNamespace

import pytest

from agent_bridge import cli


def test_bridge_instance_lock_rejects_second_owner(tmp_path) -> None:
    path = tmp_path / "bridge.lock"
    first = cli._BridgeInstanceLock(path)
    with first:
        with pytest.raises(RuntimeError, match="已在运行"):
            with cli._BridgeInstanceLock(path):
                pass


def test_bridge_instance_lock_reclaims_verified_owner(tmp_path, monkeypatch) -> None:
    lock = cli._BridgeInstanceLock(tmp_path / "bridge.lock")
    attempts = iter((False, True))
    terminated = []
    monkeypatch.setattr(lock, "_try_acquire", lambda _handle: next(attempts))
    monkeypatch.setattr(lock, "_read_owner", lambda: (12345, "python.exe"))
    monkeypatch.setattr(lock, "_is_reclaimable_owner", lambda _pid, _exe: True)
    monkeypatch.setattr(lock, "_terminate_owner", terminated.append)

    lock.__enter__()
    try:
        assert terminated == [12345]
    finally:
        assert lock._handle is not None
        lock._handle.close()
        lock._handle = None


class FakeTask:
    def __init__(self) -> None:
        self.cancel_calls = 0

    def cancel(self) -> None:
        self.cancel_calls += 1


class FakeSignal:
    SIGINT = 2

    def __init__(self) -> None:
        self.original_handler = object()
        self.current_handler = self.original_handler
        self.installed_handlers = []

    def getsignal(self, signum: int):
        assert signum == self.SIGINT
        return self.current_handler

    def signal(self, signum: int, handler):
        assert signum == self.SIGINT
        previous = self.current_handler
        self.current_handler = handler
        self.installed_handlers.append(handler)
        return previous


def install_fake_qt(monkeypatch: pytest.MonkeyPatch, run_callback):
    class FakeApplication:
        last_instance = None

        def __init__(self, _argv) -> None:
            self.quit_called = False
            FakeApplication.last_instance = self

        @classmethod
        def instance(cls):
            return None

        def setQuitOnLastWindowClosed(self, _enabled: bool) -> None:
            return None

        def quit(self) -> None:
            self.quit_called = True

    class FakeEventLoop:
        last_instance = None

        def __init__(self, _app) -> None:
            self.task = FakeTask()
            FakeEventLoop.last_instance = self

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def create_task(self, coroutine):
            coroutine.close()
            return self.task

        def run_until_complete(self, awaitable) -> None:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            run_callback(self)

    qt_widgets = ModuleType("PySide6.QtWidgets")
    qt_widgets.QApplication = FakeApplication
    qasync = ModuleType("qasync")
    qasync.QEventLoop = FakeEventLoop
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", qt_widgets)
    monkeypatch.setitem(sys.modules, "qasync", qasync)
    monkeypatch.setattr(asyncio, "set_event_loop", lambda _loop: None)
    return FakeApplication, FakeEventLoop


def test_qt_sigint_cancels_bridge_once_and_restores_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_signal = FakeSignal()

    def interrupt(_loop) -> None:
        handler = fake_signal.current_handler
        if callable(handler):
            handler(fake_signal.SIGINT, None)
            handler(fake_signal.SIGINT, None)
        raise asyncio.CancelledError

    fake_app, fake_loop = install_fake_qt(monkeypatch, interrupt)
    monkeypatch.setattr(cli, "signal", fake_signal, raising=False)

    with pytest.raises(KeyboardInterrupt):
        cli.run_bridge_with_qt(SimpleNamespace())

    assert fake_loop.last_instance.task.cancel_calls == 1
    assert len(fake_signal.installed_handlers) == 2
    assert fake_signal.current_handler is fake_signal.original_handler
    assert fake_app.last_instance.quit_called is True


def test_qt_normal_completion_restores_sigint_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_signal = FakeSignal()
    fake_app, fake_loop = install_fake_qt(monkeypatch, lambda _loop: None)
    monkeypatch.setattr(cli, "signal", fake_signal, raising=False)

    cli.run_bridge_with_qt(SimpleNamespace())

    assert fake_loop.last_instance.task.cancel_calls == 0
    assert len(fake_signal.installed_handlers) == 2
    assert fake_signal.current_handler is fake_signal.original_handler
    assert fake_app.last_instance.quit_called is True


def test_qt_unrelated_cancellation_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_signal = FakeSignal()

    def cancel_without_sigint(_loop) -> None:
        raise asyncio.CancelledError

    fake_app, fake_loop = install_fake_qt(monkeypatch, cancel_without_sigint)
    monkeypatch.setattr(cli, "signal", fake_signal, raising=False)

    with pytest.raises(asyncio.CancelledError):
        cli.run_bridge_with_qt(SimpleNamespace())

    assert fake_loop.last_instance.task.cancel_calls == 0
    assert fake_signal.current_handler is fake_signal.original_handler
    assert fake_app.last_instance.quit_called is True


def test_main_reports_clean_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda _path: SimpleNamespace())

    def interrupt(_config, _config_path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_bridge_with_qt", interrupt)

    assert cli.main(["--config", "unused.yaml", "run"]) == 0
    assert capsys.readouterr().out.strip() == "Agent bridge stopped."
