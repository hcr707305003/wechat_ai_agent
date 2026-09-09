from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from queue import Queue
from threading import Event
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from agent_bridge.config import load_config
from agent_bridge.manager.agent_debug import AgentDebugRunner, DebugEvent
from agent_bridge.manager.agent_debug_panel import AgentDebugPanel
from agent_bridge.models import NativeSession, ProviderRun, UnifiedResponse


class FakeAdapter:
    def __init__(self, calls, gate=None, error=False):
        self.calls, self.gate, self.error = calls, gate, error

    async def create_session(self, unified_id, directory):
        self.calls.append("create")
        return NativeSession("n", unified_id, "codex", "debug-native", directory)

    async def run(self, native, request, on_text_delta):
        self.calls.append(("run", native.context_initialized, request.prompt))
        await on_text_delta("你好")
        if self.gate is not None:
            while not self.gate.is_set():
                await asyncio.sleep(0.005)
        await on_text_delta("，调试成功")
        return ProviderRun(
            (), SimpleNamespace(is_error=self.error), native.native_session_id
        )

    async def close(self):
        self.calls.append("close")


def debug_config():
    return load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")


def collect_result(runner):
    events = []
    while True:
        event = runner.events.get(timeout=3)
        events.append(event)
        if event.kind in {"done", "error", "cancelled"}:
            runner._thread.join(timeout=1)
            return events


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_debug_streams_before_completion_and_resumes_separate_session(provider):
    calls, gate = [], Event()
    parser = SimpleNamespace(
        parse_final=lambda result: UnifiedResponse("你好，调试成功")
    )
    runner = AgentDebugRunner(
        lambda config, selected: (FakeAdapter(calls, gate), parser)
    )
    config = debug_config()
    runner.start(config, provider, "测试")
    try:
        while runner.events.get(timeout=2).kind != "delta":
            pass
        assert runner.busy
    finally:
        gate.set()
    assert collect_result(runner)[-1] == DebugEvent("done", "你好，调试成功")
    runner.start(config, provider, "继续")
    assert collect_result(runner)[-1].kind == "done"
    assert calls.count("create") == 1
    assert ("run", True, "继续") in calls
    assert calls.count("close") == 2
    runner.reset()
    runner.start(config, provider, "新会话")
    collect_result(runner)
    assert calls.count("create") == 2


@pytest.mark.parametrize("mode", ["cancel", "timeout", "error"])
def test_debug_failure_and_cancellation_close_client(mode):
    calls, gate = [], Event()
    parser = SimpleNamespace(
        parse_final=lambda result: UnifiedResponse("provider error")
    )
    runner = AgentDebugRunner(
        lambda config, selected: (
            FakeAdapter(
                calls, None if mode == "error" else gate, error=mode == "error"
            ),
            parser,
        )
    )
    config = debug_config()
    if mode == "timeout":
        config = replace(config, runtime=replace(config.runtime, timeout_seconds=0.05))
    runner.start(config, "codex", "测试")
    if mode == "cancel":
        while runner.events.get(timeout=2).kind != "delta":
            pass
        runner.cancel()
    events = collect_result(runner)
    assert events[-1].kind == ("cancelled" if mode == "cancel" else "error")
    assert "close" in calls
    assert not runner.busy
    assert not runner._sessions


class FakeRunner:
    def __init__(self):
        self.events = Queue()
        self.busy = False
        self.calls = []

    def start(self, config, provider, prompt):
        self.calls.append(prompt)
        self.busy = True

    def cancel(self):
        self.busy = False

    def reset(self):
        self.calls.clear()


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


def test_typewriter_is_incremental_and_does_not_duplicate_final_reply(qt_app):
    runner = FakeRunner()
    panel = AgentDebugPanel(lambda: object(), runner=runner)
    try:
        panel.input.setPlainText("我的问题")
        panel.send_button.click()
        panel.send_button.click()
        assert len(runner.calls) == 1
        runner.events.put(DebugEvent("delta", "你好，测试回复"))
        panel._tick()
        assert 0 < len(panel._typed) < len("你好，测试回复")
        assert "我的问题" in panel.transcript.toPlainText()
        runner.events.put(DebugEvent("done", "你好，测试回复"))
        runner.busy = False
        for _ in range(30):
            panel._tick()
            if not panel._active:
                break
        assert not panel._active
        assert panel.transcript.toPlainText().count("你好，测试回复") == 1
        assert "调试通过" in panel.status.text()
    finally:
        panel.shutdown()
        panel.close()


def test_stop_also_stops_buffered_typewriter_text(qt_app):
    runner = FakeRunner()
    panel = AgentDebugPanel(lambda: object(), runner=runner)
    try:
        panel._send_prompt("问题")
        runner.busy = False
        runner.events.put(DebugEvent("done", "这是一段很长的回复" * 100))
        panel._tick()
        typed = panel._typed
        panel._stop()
        panel._tick()
        assert panel._typed == typed
        assert not panel._active
        assert "已停止" in panel.status.text()
    finally:
        panel.shutdown()
        panel.close()
