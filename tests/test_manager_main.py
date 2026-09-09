import sys
from pathlib import Path

import pytest

from agent_bridge.manager_main import activate_existing_manager, resolve_manager_paths


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskbar identity")
def test_manager_taskbar_identity_in_isolated_process():
    import subprocess

    script = '''
import ctypes
from agent_bridge.manager_main import configure_taskbar_identity, MANAGER_APP_ID
configure_taskbar_identity()
value = ctypes.c_wchar_p()
read_id = ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID
read_id.argtypes = [ctypes.POINTER(ctypes.c_wchar_p)]
read_id.restype = ctypes.c_long
assert read_id(ctypes.byref(value)) == 0
try:
    assert value.value == MANAGER_APP_ID
finally:
    free = ctypes.windll.ole32.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    free(ctypes.cast(value, ctypes.c_void_p))
'''
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


class FakeWindowApi:
    def __init__(self, handle: int) -> None:
        self.handle = handle
        self.calls = []

    def FindWindowW(self, _class_name, title: str) -> int:
        assert title == "Agent Bridge 管理面板"
        return self.handle

    def ShowWindow(self, handle: int, command: int) -> None:
        self.calls.append(("show", handle, command))

    def SetForegroundWindow(self, handle: int) -> None:
        self.calls.append(("foreground", handle))


def test_activate_existing_manager_restores_window() -> None:
    api = FakeWindowApi(42)

    assert activate_existing_manager(api) is True
    assert api.calls == [("show", 42, 9), ("foreground", 42)]


def test_activate_existing_manager_returns_false_without_window() -> None:
    api = FakeWindowApi(0)

    assert activate_existing_manager(api) is False
    assert api.calls == []


def test_resolve_manager_paths_uses_project_config_in_source_mode() -> None:
    paths = resolve_manager_paths(frozen=False)

    project_root = Path(__file__).resolve().parents[1]
    assert paths.user_root == project_root
    assert paths.config_file == project_root / "config.yaml"


def test_resolve_manager_paths_honors_explicit_user_data_dir(tmp_path: Path) -> None:
    paths = resolve_manager_paths(tmp_path, frozen=False)

    assert paths.user_root == tmp_path.resolve()
    assert paths.config_file == tmp_path.resolve() / "config.yaml"


def test_resolve_manager_paths_uses_local_app_data_when_frozen(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    paths = resolve_manager_paths(frozen=True)

    assert paths.user_root == (tmp_path / "AgentBridge").resolve()
    assert paths.config_file == (tmp_path / "AgentBridge" / "config.yaml").resolve()


@pytest.mark.parametrize("interrupt", [True, False])
def test_manager_event_loop_exits_cleanly_in_child_process(tmp_path, interrupt):
    import os
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent('''
        import runpy
        import signal
        import sys
        from pathlib import Path
        from types import SimpleNamespace
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from agent_bridge.manager_main import run_manager_event_loop

        helpers = runpy.run_path('tests/test_manager_qt.py')
        app = QApplication([])
        app.setQuitOnLastWindowClosed(False)
        window = helpers['make_window'](Path(sys.argv[1]))
        hidden = []
        window.attach_tray(SimpleNamespace(hide=lambda: hidden.append(True)))
        def unexpected_stop():
            raise AssertionError('Exiting manager must not stop workbench')
        window.controller.stop = unexpected_stop
        previous = signal.getsignal(signal.SIGINT)
        def interrupt_twice():
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)
        QTimer.singleShot(150, interrupt_twice if sys.argv[2] == 'True' else window.request_exit)
        result = run_manager_event_loop(app, window)
        assert result == 0
        assert signal.getsignal(signal.SIGINT) is previous
        assert window._allow_close
        assert not window._poll_timer.isActive()
        assert not window._status_timer.isActive()
        assert not window._log_timer.isActive()
        assert hidden == [True]
        print('clean-exit')
    ''')
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(interrupt)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean-exit" in result.stdout
    assert "KeyboardInterrupt" not in result.stderr


def test_main_releases_manager_lock_after_ctrl_c(tmp_path):
    import os
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent('''
        import signal
        import sys
        from pathlib import Path
        from PySide6.QtCore import QLockFile, QTimer
        from PySide6.QtWidgets import QApplication
        from agent_bridge.manager_main import main
        app = QApplication([])
        QTimer.singleShot(300, lambda: signal.raise_signal(signal.SIGINT))
        assert main(['--user-data-dir', sys.argv[1]]) == 0
        lock = QLockFile(str(Path(sys.argv[1]) / 'manager.lock'))
        assert lock.tryLock(0), 'manager lock was not released'
        lock.unlock()
        print('lock-released')
    ''')
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "lock-released" in result.stdout
    assert "KeyboardInterrupt" not in result.stderr
