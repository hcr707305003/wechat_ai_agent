from __future__ import annotations

import argparse
import ctypes
import multiprocessing
import signal
import sys
from pathlib import Path
from threading import Event

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from agent_bridge.application_paths import ApplicationPaths
from agent_bridge.logging_setup import configure_file_logging
from agent_bridge.manager.config_document import ConfigDocument
from agent_bridge.manager.process_controller import WorkbenchProcessController
from agent_bridge.manager.qt_window import AgentBridgeManagerWindow

MANAGER_APP_ID = "AgentBridge.Manager"


def configure_taskbar_identity() -> None:
    if sys.platform == "win32":
        # Set before creating any Qt windows so Windows does not group us under Python.
        set_app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [ctypes.c_wchar_p]
        set_app_id.restype = ctypes.c_long
        result = set_app_id(MANAGER_APP_ID)
        if result < 0:
            raise OSError(f"Unable to set manager taskbar identity: HRESULT {result:#x}")


def activate_existing_manager(window_api=None) -> bool:
    api = window_api or ctypes.windll.user32
    handle = api.FindWindowW(None, "Agent Bridge 管理面板")
    if not handle:
        return False
    api.ShowWindow(handle, 9)
    api.SetForegroundWindow(handle)
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="AgentBridge manager")
    parser.add_argument("--user-data-dir", default=None)
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def resolve_manager_paths(
    user_data_dir: str | Path | None = None, *, frozen: bool | None = None
) -> ApplicationPaths:
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if user_data_dir is not None or is_frozen:
        return ApplicationPaths.discover(user_root=user_data_dir, frozen=is_frozen)
    project_root = Path(__file__).resolve().parents[1]
    return ApplicationPaths.discover(
        resource_root=project_root,
        user_root=project_root,
        frozen=False,
    )


def run_manager_event_loop(app: QApplication, window: AgentBridgeManagerWindow) -> int:
    exit_requested = Event()

    def request_interrupt(_signum, _frame) -> None:
        # Python may deliver SIGINT inside any Qt callback. Defer widget teardown
        # to the next timer tick instead of raising or re-entering that callback.
        exit_requested.set()

    def process_interrupt() -> None:
        if exit_requested.is_set():
            window.request_exit()

    interrupt_timer = QTimer(window)
    interrupt_timer.setInterval(100)
    interrupt_timer.timeout.connect(process_interrupt)
    previous_sigint = signal.signal(signal.SIGINT, request_interrupt)
    interrupt_timer.start()
    try:
        return app.exec()
    finally:
        try:
            window.request_exit()
        finally:
            interrupt_timer.stop()
            signal.signal(signal.SIGINT, previous_sigint)


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = build_parser().parse_args(argv)
    paths = resolve_manager_paths(args.user_data_dir)
    paths.initialize_user_data()
    configure_file_logging(paths.manager_log)
    configure_taskbar_identity()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Agent Bridge")
    app.setQuitOnLastWindowClosed(False)
    icon = QIcon(str(Path(__file__).with_name("assets") / "manager-tray.png"))
    if icon.isNull():
        icon = app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
    app.setWindowIcon(icon)
    manager_lock = QLockFile(str(paths.user_root / "manager.lock"))
    manager_lock.setStaleLockTime(0)
    if not manager_lock.tryLock(0):
        activate_existing_manager()
        return 0
    document = ConfigDocument.load(paths.config_file)
    document.apply_defaults(ConfigDocument.load(paths.config_template).data)
    controller = WorkbenchProcessController(paths)
    window = AgentBridgeManagerWindow(paths, document, controller)
    window.setWindowIcon(icon)

    if args.smoke_test:
        QTimer.singleShot(0, window.request_exit)
        try:
            return run_manager_event_loop(app, window)
        finally:
            manager_lock.unlock()

    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("Agent Bridge")
    menu = QMenu()
    open_manager = QAction("打开管理面板", menu)
    open_workbench = QAction("打开工作台", menu)
    start_workbench = QAction("启动工作台", menu)
    stop_workbench = QAction("关闭工作台", menu)
    exit_manager = QAction("退出管理程序", menu)
    open_manager.triggered.connect(window.show_manager)
    open_workbench.triggered.connect(window._show_workbench)
    window.attach_visibility_action(open_workbench)
    start_workbench.triggered.connect(window._start_workbench)
    stop_workbench.triggered.connect(window._stop_workbench)
    exit_manager.triggered.connect(window.request_exit)
    menu.addAction(open_manager)
    menu.addSeparator()
    menu.addAction(open_workbench)
    menu.addAction(start_workbench)
    menu.addAction(stop_workbench)
    menu.addSeparator()
    menu.addAction(exit_manager)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: (
            window.show_manager()
            if reason == QSystemTrayIcon.ActivationReason.Trigger
            else None
        )
    )
    tray.show()
    window.attach_tray(tray)
    window.show()
    try:
        return run_manager_event_loop(app, window)
    finally:
        manager_lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
