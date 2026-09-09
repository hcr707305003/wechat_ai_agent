from __future__ import annotations

import os
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from agent_bridge.application_paths import ApplicationPaths
from agent_bridge.manager.config_document import ConfigDocument
from agent_bridge.manager.doctor import CheckResult, CheckTask
from agent_bridge.manager.process_controller import WorkbenchState, WorkbenchStatus
from agent_bridge.manager.qt_window import (
    AgentBridgeManagerWindow,
    decode_workbench_log,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


class FakeController:
    def status(self) -> WorkbenchStatus:
        return WorkbenchStatus(WorkbenchState.STOPPED)

    def start(self) -> WorkbenchStatus:
        return WorkbenchStatus(WorkbenchState.STARTING)

    def show(self) -> bool:
        return True

    def hide(self) -> bool:
        return True

    def stop(self) -> WorkbenchStatus:
        return WorkbenchStatus(WorkbenchState.STOPPED)


def make_window(tmp_path: Path) -> AgentBridgeManagerWindow:
    root = Path(__file__).resolve().parents[1]
    resources = tmp_path / "resources"
    resources.mkdir()
    template = (root / "config.example.yaml").read_text(encoding="utf-8")
    (resources / "config.example.yaml").write_text(template, encoding="utf-8")
    paths = ApplicationPaths(resources, tmp_path / "user", False)
    paths.initialize_user_data()
    return AgentBridgeManagerWindow(
        paths,
        ConfigDocument.load(paths.config_file),
        FakeController(),
    )


def test_manager_window_reflects_workbench_state(qt_app, tmp_path: Path) -> None:
    window = make_window(tmp_path)
    try:
        window._apply_status(WorkbenchStatus(WorkbenchState.RUNNING))
        assert window.status_label.text() == "●  已开启"
        assert window.start_button.isEnabled() is False
        assert window.show_button.isEnabled() is True
        assert window.stop_button.isEnabled() is True
    finally:
        window.request_exit()


def test_manager_window_saves_form_changes(qt_app, tmp_path: Path) -> None:
    window = make_window(tmp_path)
    try:
        window.reply_prefix.setText("[AI] ")
        assert window.save_config(show_success=False) is True
        saved = ConfigDocument.load(window.paths.config_file)
        assert saved.value("channels.wechat.reply_prefix") == "[AI] "
    finally:
        window.request_exit()


def test_manager_window_advanced_yaml_preserves_unknown_field(
    qt_app, tmp_path: Path
) -> None:
    window = make_window(tmp_path)
    try:
        window.tabs.setCurrentIndex(window._advanced_index)
        window.yaml_editor.appendPlainText("future_feature:\n  enabled: true")
        assert window.save_config(show_success=False) is True
        saved = ConfigDocument.load(window.paths.config_file)
        assert saved.value("future_feature.enabled") is True
    finally:
        window.request_exit()


def test_decode_workbench_log_supports_mixed_utf8_and_legacy_chinese() -> None:
    payload = "UTF-8：工作台启动\n".encode() + "收到微信消息\n".encode("gb18030")

    assert decode_workbench_log(payload) == "UTF-8：工作台启动\n收到微信消息\n"


@pytest.mark.parametrize("installed", [(), ("codex",), ("claude",), ("codex", "claude")])
def test_agent_choices_disable_missing_provider_and_preserve_binding(qt_app, tmp_path, installed):
    from agent_bridge.agents.availability import AgentAvailability

    window = make_window(tmp_path)
    try:
        window.session_bindings.set_values([
            {"conversation_id": "friend", "provider": "codex", "session_id": "old-native"}
        ])
        window._agent_states = {p: AgentAvailability(p in installed, "就绪" if p in installed else "缺失")
                                for p in ("codex", "claude")}
        window._refresh_agent_choices()
        for combo in (window.default_provider, window.agent_debug.provider,
                      window.session_bindings.table.cellWidget(0, 2)):
            assert combo.model().item(0).isEnabled() == ("codex" in installed)
            assert combo.model().item(1).isEnabled() == ("claude" in installed)
        # Saved values must not silently change, especially native session bindings.
        assert window.default_provider.currentText() == "codex"
        assert window.session_bindings.values()[0]["provider"] == "codex"
        assert window.session_bindings.values()[0]["session_id"] == "old-native"
        assert window.agent_debug.send_button.isEnabled() == bool(installed)
        assert window.agent_debug.probe_button.isEnabled() == bool(installed)
        if installed:
            assert window.agent_debug.provider.currentData() in installed
        else:
            assert window.agent_debug.provider.currentIndex() == -1
        window.claude_enabled.setChecked(False)
        assert not window.agent_debug.provider.model().item(1).isEnabled()
    finally:
        window.request_exit()


def test_optional_agent_failure_does_not_block_start(qt_app, tmp_path, monkeypatch):
    from agent_bridge.agents.availability import AgentAvailability

    window = make_window(tmp_path)
    try:
        cfg = window.document.validate()
        window._check_session = SimpleNamespace(config=cfg, agent_states={
            "codex": AgentAvailability(False, "未安装"),
            "claude": AgentAvailability(True, "就绪"),
        })
        window._check_outcomes = [
            CheckResult("Codex Agent", False, "未安装", required=False),
            CheckResult("Claude Agent", True, "就绪", required=False),
            CheckResult("可用 Agent", True, "claude"),
        ]
        window._start_after_checks = True
        calls = []
        monkeypatch.setattr(window, "_submit_operation", lambda *args: calls.append(args))
        window._finish_checks()
        assert window._checked_config == cfg
        assert len(calls) == 1
        assert "0 项失败" in window.check_summary.text()
        assert "1 个可选 Agent 不可用" in window.check_summary.text()
    finally:
        window.request_exit()


def test_manager_window_displays_legacy_chinese_log(qt_app, tmp_path: Path) -> None:
    window = make_window(tmp_path)
    try:
        window.paths.workbench_log.write_bytes("收到微信消息\n".encode("gb18030"))

        window._refresh_log()

        assert window.log_view.toPlainText() == "收到微信消息\n"
    finally:
        window.request_exit()


@pytest.mark.parametrize("extra_lines", [3, 3000])
@pytest.mark.parametrize("position", ["top", "middle", "near_bottom"])
def test_log_pauses_while_reading_and_resumes_only_at_bottom(
    qt_app, tmp_path, extra_lines, position
):
    window = make_window(tmp_path)
    try:
        window._initial_checks_pending = False
        window._log_timer.stop()
        window.tabs.setCurrentWidget(window.log_view.parentWidget())
        window.show()
        qt_app.processEvents()
        original = "".join(f"旧日志 {i:04d} {'x' * 200}\n" for i in range(200))
        window.paths.workbench_log.write_text(original, encoding="utf-8")
        window._refresh_log()
        qt_app.processEvents()
        view = window.log_view
        vertical = view.verticalScrollBar()
        horizontal = view.horizontalScrollBar()
        assert vertical.maximum() > 10
        assert vertical.value() == vertical.maximum()
        assert horizontal.maximum() > 0
        reading_position = {
            "top": 0, "middle": vertical.maximum() // 2,
            "near_bottom": vertical.maximum() - 1,
        }[position]
        vertical.setValue(reading_position)
        horizontal.setValue(horizontal.maximum() // 2)
        horizontal_position = horizontal.value()
        first_line = view.firstVisibleBlock().blockNumber()

        # The larger case pushes old lines outside the on-disk 128 KiB tail.
        incoming = "".join(f"新日志 {i:04d} {'y' * 200}\n" for i in range(extra_lines))
        window.paths.workbench_log.write_text(original + incoming, encoding="utf-8")
        for _ in range(3):
            window._refresh_log()
            qt_app.processEvents()
            assert view.toPlainText() == original
            assert vertical.value() == reading_position
            assert horizontal.value() == horizontal_position
            assert view.firstVisibleBlock().blockNumber() == first_line

        vertical.setValue(vertical.maximum())
        window._refresh_log()
        qt_app.processEvents()
        assert view.toPlainText().endswith(incoming.splitlines()[-1] + "\n")
        assert vertical.value() == vertical.maximum()

        with window.paths.workbench_log.open("a", encoding="utf-8") as handle:
            handle.write("继续跟随新日志\n")
        window._refresh_log()
        qt_app.processEvents()
        assert view.toPlainText().endswith("继续跟随新日志\n")
        assert vertical.value() == vertical.maximum()
    finally:
        window.request_exit()


def test_log_does_not_refresh_during_scrollbar_drag(qt_app, tmp_path):
    window = make_window(tmp_path)
    try:
        window.paths.workbench_log.write_text("旧日志\n", encoding="utf-8")
        window._refresh_log()
        scrollbar = window.log_view.verticalScrollBar()
        scrollbar.setSliderDown(True)
        window.paths.workbench_log.write_text("旧日志\n新日志\n", encoding="utf-8")
        window._refresh_log()
        assert window.log_view.toPlainText() == "旧日志\n"
        scrollbar.setSliderDown(False)
        window._refresh_log()
        assert window.log_view.toPlainText() == "旧日志\n新日志\n"
    finally:
        window.request_exit()


def test_clear_log_truncates_only_workbench_log_and_keeps_following(qt_app, tmp_path):
    window = make_window(tmp_path)
    try:
        log_path = window.paths.workbench_log
        preserved = [
            window.paths.manager_log, log_path.with_suffix(".log.1"),
            window.paths.data_dir / "history.db",
        ]
        for path in preserved:
            path.write_bytes(b"keep this data")
        with log_path.open("ab", buffering=0) as writer:
            writer.write("旧日志\n".encode())
            window._refresh_log()
            window._log_follow_tail = False
            button = next(b for b in window.findChildren(QPushButton) if b.text() == "清空日志")
            button.click()
            assert log_path.read_bytes() == b""
            assert window.log_view.toPlainText() == ""
            assert window._log_follow_tail is True
            window._refresh_log()
            assert window.log_view.toPlainText() == ""
            writer.write("新日志\n".encode())
            window._refresh_log()
            assert log_path.read_bytes() == "新日志\n".encode()
            assert window.log_view.toPlainText() == "新日志\n"
        for path in preserved:
            assert path.read_bytes() == b"keep this data"
    finally:
        window.request_exit()


def test_clear_missing_log_clears_display_without_creating_file(qt_app, tmp_path):
    window = make_window(tmp_path)
    try:
        window.log_view.setPlainText("旧显示")
        window._clear_workbench_log()
        assert window.log_view.toPlainText() == ""
        assert not window.paths.workbench_log.exists()
    finally:
        window.request_exit()


def test_clear_log_failure_keeps_display_and_reports_error(qt_app, tmp_path, monkeypatch):
    window = make_window(tmp_path)
    try:
        window.paths.workbench_log.write_text("保留旧日志", encoding="utf-8")
        window._refresh_log()
        window._log_follow_tail = False
        errors = []
        original_open = Path.open

        def fail_log_write(path, mode="r", *args, **kwargs):
            if path == window.paths.workbench_log and mode == "r+b":
                raise PermissionError("测试：无法写入日志")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_log_write)
        monkeypatch.setattr(QMessageBox, "critical", lambda *args: errors.append(args[1:]))
        window._clear_workbench_log()
        assert window.log_view.toPlainText() == "保留旧日志"
        assert window.paths.workbench_log.read_text(encoding="utf-8") == "保留旧日志"
        assert window._log_follow_tail is False
        assert errors == [("日志清空失败", "测试：无法写入日志")]
    finally:
        window.request_exit()


def test_clear_log_allows_redirected_child_to_continue_writing(qt_app, tmp_path):
    import subprocess
    import sys

    from agent_bridge.logging_setup import open_redirected_log

    window = make_window(tmp_path)
    child = None
    try:
        marker = tmp_path / "child-ready"
        script = (
            "import sys; from pathlib import Path; "
            "print('old log', flush=True); Path(sys.argv[1]).touch(); "
            "sys.stdin.readline(); print('new log', flush=True)"
        )
        with open_redirected_log(window.paths.workbench_log) as writer:
            child = subprocess.Popen(
                [sys.executable, "-u", "-c", script, str(marker)],
                stdin=subprocess.PIPE, stdout=writer, stderr=writer,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        from time import monotonic, sleep

        deadline = monotonic() + 10
        while not marker.exists() and monotonic() < deadline:
            sleep(0.01)
        assert marker.exists()
        window._clear_workbench_log()
        child.communicate(b"continue\n", timeout=10)
        assert child.returncode == 0
        assert window.paths.workbench_log.read_bytes().replace(b"\r\n", b"\n") == b"new log\n"
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
        window.request_exit()


@pytest.mark.parametrize(
    "state,label,closed",
    [
        (WorkbenchState.STOPPED, "已关闭", True),
        (WorkbenchState.STARTING, "开启中", False),
        (WorkbenchState.RUNNING, "已开启", False),
        (WorkbenchState.STOPPING, "关闭中", False),
        (WorkbenchState.EXITED, "已关闭", True),
        (WorkbenchState.UNKNOWN, "已关闭", True),
    ],
)
def test_status_colors_and_controls(qt_app, tmp_path, state, label, closed):
    window = make_window(tmp_path)
    try:
        window._apply_status(WorkbenchStatus(state, exit_code=42, detail="query error"))
        assert window.status_label.accessibleName() == label
        assert window.status_label.property("closed") is closed
        assert window.start_button.isEnabled() is closed
        assert window.show_button.isEnabled() is (state == WorkbenchState.RUNNING)
        assert window.stop_button.isEnabled() is (state == WorkbenchState.RUNNING)
        if state == WorkbenchState.EXITED:
            assert "42" in window.status_detail.text()
        if state == WorkbenchState.UNKNOWN:
            assert "query error" in window.status_detail.text()
    finally:
        window.request_exit()


class ControlledExecutor:
    """Keep work pending until the test explicitly completes one task."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn):
        future = Future()
        self.jobs.append((future, fn))
        return future

    def complete(self):
        future, fn = self.jobs.pop(0)
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(fn())
            except Exception as error:  # noqa: BLE001 - mirror a worker future
                future.set_exception(error)

    def shutdown(self, **kwargs):
        for future, _ in self.jobs:
            future.cancel()


def control_background_work(window):
    window._poll_timer.stop()
    window._status_timer.stop()
    window._executor.shutdown(wait=True)
    window._status_future = None
    window._executor = ControlledExecutor()
    return window._executor


def complete_check(window, executor):
    executor.complete()
    window._check_started -= 1
    window._poll_background_work()


@pytest.mark.parametrize(
    "failed,start_after", [(False, True), (True, True), (False, False)]
)
def test_checks_run_in_order_and_gate_start(
    qt_app, tmp_path, monkeypatch, failed, start_after
):
    from agent_bridge.manager import qt_window

    window = make_window(tmp_path)
    executor = control_background_work(window)
    calls = []

    def check(name, ok):
        calls.append(name)
        return CheckResult(name, ok, "detail", "请修复" if not ok else "")

    tasks = [
        CheckTask("first", lambda: check("first", not failed)),
        CheckTask("second", lambda: check("second", True)),
    ]
    monkeypatch.setattr(
        qt_window, "ManagerCheckSession", lambda path: SimpleNamespace(tasks=tasks)
    )
    try:
        window._run_checks(start_after=start_after)
        assert calls == []
        assert [row.state for row in window._check_rows] == ["running", "waiting"]
        assert not window.check_button.isEnabled()
        assert not window.save_button.isEnabled()
        window._run_checks()
        window._apply_status(WorkbenchStatus(WorkbenchState.STOPPED))
        assert not window.start_button.isEnabled()
        assert len(executor.jobs) == 1

        complete_check(window, executor)
        assert calls == ["first"]
        assert window._check_rows[1].state == "running"
        assert window.check_progress.value() == 1
        complete_check(window, executor)
        assert calls == ["first", "second"]
        assert window.check_progress.value() == 2
        assert window._check_session is None
        assert window.save_button.isEnabled()
        assert window._check_rows[0].state == ("failed" if failed else "success")
        if start_after and not failed:
            assert len(executor.jobs) == 1
            assert executor.jobs[0][1] == window.controller.start
            assert window.status_label.accessibleName() == "开启中"
        else:
            assert not executor.jobs
            assert window.check_button.isEnabled()
        if failed:
            assert "1 项失败" in window.check_summary.text()
            assert "请修复" in window._check_rows[0].detail.text()
    finally:
        window.request_exit()


def test_exit_cancels_pending_checks_and_does_not_start(qt_app, tmp_path):
    window = make_window(tmp_path)
    control_background_work(window)
    window._start_workbench()
    future = window._check_future
    window.request_exit()
    assert future.cancelled()
    window._poll_background_work()
    assert window._check_session is None
    assert window._operation is None


def test_operation_discards_old_status_query(qt_app, tmp_path):
    window = make_window(tmp_path)
    executor = control_background_work(window)
    try:
        old_query = Future()
        old_query.set_result(WorkbenchStatus(WorkbenchState.STOPPED))
        window._status_future = old_query
        window._submit_operation(window.controller.start, WorkbenchState.STARTING)
        window._poll_background_work()
        assert window.status_label.accessibleName() == "开启中"
        assert window._status_future is None
        assert len(executor.jobs) == 1
    finally:
        window.request_exit()


def test_environment_check_does_not_block_qt_events(qt_app, tmp_path, monkeypatch):
    from threading import Event

    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest

    from agent_bridge.manager import qt_window

    window = make_window(tmp_path)
    entered, release = Event(), Event()

    def slow_check():
        entered.set()
        release.wait(3)
        return CheckResult("slow", True, "done")

    monkeypatch.setattr(
        qt_window,
        "ManagerCheckSession",
        lambda path: SimpleNamespace(tasks=[CheckTask("slow", slow_check)]),
    )
    try:
        window.check_button.click()
        assert entered.wait(1)
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        QTest.qWait(30)
        assert heartbeat == [True]
        assert window._check_rows[0].state == "running"
        assert not window._check_future.done()
    finally:
        release.set()
        window.request_exit()


@pytest.mark.parametrize("invalid_config", [False, True])
def test_first_show_checks_once_without_saving_or_starting(
    qt_app, tmp_path, monkeypatch, invalid_config,
):
    from PySide6.QtTest import QTest

    window = make_window(tmp_path)
    executor = control_background_work(window)
    if invalid_config:
        window.paths.config_file.write_text("- invalid configuration\n", encoding="utf-8")
    original = window.paths.config_file.read_bytes()

    def unexpected_save(**kwargs):
        pytest.fail("Opening the manager must not save configuration")

    monkeypatch.setattr(window, "save_config", unexpected_save)
    try:
        assert window._check_session is None
        window.show()
        QTest.qWait(20)
        assert window._check_session is not None
        assert len(executor.jobs) == 1
        while window._check_session is not None:
            complete_check(window, executor)
        assert not executor.jobs
        assert window._operation is None
        assert window.paths.config_file.read_bytes() == original
        assert window._check_outcomes[0].ok is (not invalid_config)
        outcomes = window._check_outcomes
        window.hide()
        window.show()
        QTest.qWait(20)
        assert window._check_session is None
        assert window._check_outcomes is outcomes
        assert not executor.jobs
    finally:
        window.request_exit()


def test_finished_status_query_is_consumed_before_rescheduling(qt_app, tmp_path):
    window = make_window(tmp_path)
    executor = control_background_work(window)
    try:
        window._apply_status(WorkbenchStatus(WorkbenchState.RUNNING))
        finished = Future()
        finished.set_result(WorkbenchStatus(WorkbenchState.EXITED, exit_code=0))
        window._status_future = finished
        window._schedule_status_check()
        assert window._status_future is finished
        assert not executor.jobs
        window._poll_background_work()
        assert window.status_label.accessibleName() == "已关闭"
        assert window.status_label.property("closed") is True
    finally:
        window.request_exit()


def test_stop_keeps_pending_status_then_shows_closed(qt_app, tmp_path):
    window = make_window(tmp_path)
    executor = control_background_work(window)
    try:
        window._apply_status(WorkbenchStatus(WorkbenchState.RUNNING))
        stale = Future()
        stale.set_result(WorkbenchStatus(WorkbenchState.RUNNING))
        window._status_future = stale
        window.stop_button.click()
        window._poll_background_work()
        assert window.status_label.accessibleName() == "关闭中"
        executor.complete()
        window._poll_background_work()
        assert window.status_label.accessibleName() == "已关闭"
        assert window.status_label.property("closed") is True
        assert window.start_button.isEnabled()
        assert not window.stop_button.isEnabled()
    finally:
        window.request_exit()


def test_visibility_button_and_tray_follow_confirmed_window_state(qt_app, tmp_path):
    from PySide6.QtGui import QAction

    window = make_window(tmp_path)
    executor = control_background_work(window)
    action = QAction()
    window.attach_visibility_action(action)
    try:
        window._apply_status(WorkbenchStatus(WorkbenchState.RUNNING, window_visible=True))
        assert window.show_button.text() == action.text() == "收起工作台"
        window.show_button.click()
        window.show_button.click()
        assert len(executor.jobs) == 1
        assert executor.jobs[0][1] == window.controller.hide
        executor.complete()
        window._poll_background_work()
        executor.jobs.clear()
        window._status_future = None
        window._apply_status(WorkbenchStatus(WorkbenchState.RUNNING, window_visible=False))
        assert window.show_button.text() == action.text() == "打开工作台"
        assert window.status_label.accessibleName() == "已开启"
        window.show_button.click()
        assert executor.jobs[0][1] == window.controller.show
        assert window._operation is None
    finally:
        window.request_exit()


@pytest.mark.parametrize("scenario", ["unchanged", "edited", "failed"])
def test_start_reuses_only_successful_checks_for_same_config(qt_app, tmp_path, monkeypatch, scenario):
    from agent_bridge.manager import doctor

    window = make_window(tmp_path)
    executor = control_background_work(window)
    probes = []

    def find_spec(module):
        probes.append(module)
        return None if scenario == "failed" and module == "wechatauto" else object()

    monkeypatch.setattr(doctor.importlib.util, "find_spec", find_spec)
    try:
        window._run_initial_checks()
        while window._check_session is not None:
            complete_check(window, executor)
        assert bool(window._checked_config) is (scenario != "failed")
        previous_rows = window._check_rows
        probe_count = len(probes)
        if scenario == "edited":
            window.reply_prefix.setText("[new prefix] ")
        window._start_workbench()
        if scenario == "unchanged":
            assert window._check_session is None
            assert window._check_rows is previous_rows
            assert len(probes) == probe_count
            assert len(executor.jobs) == 1
            assert executor.jobs[0][1] == window.controller.start
        else:
            assert window._check_session is not None
            assert window._operation is None
            assert window._check_rows is not previous_rows
    finally:
        window.request_exit()


def test_start_during_initial_check_waits_for_that_check(qt_app, tmp_path, monkeypatch):
    from agent_bridge.manager import doctor

    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda module: object())
    window = make_window(tmp_path)
    executor = control_background_work(window)
    try:
        window._run_initial_checks()
        session = window._check_session
        window._start_workbench()
        assert window._check_session is session
        assert len(executor.jobs) == 1
        while window._check_session is not None:
            complete_check(window, executor)
        assert len(executor.jobs) == 1
        assert executor.jobs[0][1] == window.controller.start
    finally:
        window.request_exit()


def test_agent_debug_uses_current_form_without_saving_file(qt_app, tmp_path):
    window = make_window(tmp_path)
    try:
        original = window.paths.config_file.read_bytes()
        window.codex_model.setText("debug-model")
        snapshot = window._load_debug_config()
        assert snapshot.agents["codex"].codex.model == "debug-model"
        assert window.paths.config_file.read_bytes() == original
        assert not window.paths.config_file.with_suffix(".yaml.bak").exists()
        assert not list(window.paths.config_file.parent.glob("*.validate.*"))
    finally:
        window.request_exit()
