import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from agent_bridge import cli
from agent_bridge.channels.wechat import WeChatCompanionSettings
from agent_bridge.companion.controller import CompanionController
from agent_bridge.companion.models import ConversationItem
from agent_bridge.companion.qt_window import WeChatCompanionWindow
from agent_bridge.models import ConversationType
from agent_bridge.sessions.repository import SQLiteRepository


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


@pytest.fixture
def workbench(qt_app, tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "test.db")

    async def noop(*args):
        return []

    controller = CompanionController(repo, noop, noop, "codex")
    window = WeChatCompanionWindow(
        WeChatCompanionSettings(mode="independent"), controller, [],
        lambda: None, lambda: None, lambda value: None,
        probe=object(), mover=object(), owner=object(), loading=True,
    )
    history = []
    # Isolate UI hydration from database history tasks (covered separately).
    monkeypatch.setattr(window, "_load_conversation_history", history.append)
    yield window, controller, history
    window.close()
    qt_app.processEvents()
    repo.close()


def conversation():
    return ConversationItem("wechat", "test", "friend", ConversationType.PRIVATE, "测试会话")


async def wait_until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_loading_is_inside_full_workbench(qt_app, workbench):
    window, _, history = workbench
    window.show()
    window.set_loading_stage("正在加载会话")
    qt_app.processEvents()
    assert window.isVisible()
    assert window.title_bar.isVisible() and window.sidebar.isVisible()
    assert window.loading_panel.window() is window
    assert window.timeline_stack.currentWidget() is window.loading_panel
    assert window.loading_panel.progress.maximum() == 0
    assert window.loading_panel.stage.text() == "正在加载会话"
    assert "已等待" in window.loading_panel.elapsed.text()
    assert window.loading_panel.timer.isActive()
    assert not window.reply_switch.isEnabled()
    assert not window.settings_button.isEnabled()
    assert not window.conversation_list.isEnabled()
    assert window.close_button.isEnabled()
    assert window.history_page_button.isHidden()
    assert history == []


@pytest.mark.asyncio
async def test_paints_before_connect_and_hydrates_same_window(qt_app, workbench):
    window, controller, history = workbench
    release = asyncio.Event()
    started = asyncio.Event()
    bound = []
    original_handle = int(window.winId())

    async def start(handler):
        qt_app.processEvents()
        assert window.isVisible() and window.loading_panel.isVisible()
        assert not window._profile_refresh_timer.isActive()
        started.set()
        await release.wait()

    async def profiles():
        assert "会话列表" in window.loading_panel.stage.text()
        return [conversation()]

    channel = SimpleNamespace(start=start, resolved_session_bindings=(),
                              list_allowlisted_conversations_initial=profiles)
    resolver = SimpleNamespace(configure_session_bindings=bound.append)
    task = asyncio.create_task(cli._run_workbench_startup(window, channel, controller, resolver))
    await started.wait()
    assert window._loading
    release.set()
    await wait_until(lambda: not window._loading)
    qt_app.processEvents()
    assert int(window.winId()) == original_handle
    assert window.isVisible()
    assert window.title_label.text() == "测试会话"
    assert window.timeline_stack.currentWidget() is window.timeline_scroll
    assert window.reply_switch.isEnabled()
    assert window.conversation_list.isEnabled()
    assert not window.loading_panel.timer.isActive()
    assert history == [conversation()]
    assert bound == [()]
    window.close()
    await task


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_close_during_loading_never_reopens_or_leaves_worker(workbench, cancel):
    window, controller, history = workbench
    release = asyncio.Event()
    started = asyncio.Event()
    completed = []

    async def start(handler):
        started.set()
        await release.wait()
        completed.append(True)

    channel = SimpleNamespace(start=start)
    task = asyncio.create_task(cli._run_workbench_startup(window, channel, controller, object()))
    await started.wait()
    if cancel:
        task.cancel()
        await asyncio.sleep(0)
    else:
        window.close()
    assert not task.done()  # Drain worker before outer resource cleanup.
    release.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert completed == [True]
    assert window.is_closed and not window.isVisible()
    assert history == []
    assert not window.loading_panel.timer.isActive()


@pytest.mark.asyncio
async def test_failure_stays_in_same_window_until_closed(workbench):
    window, controller, _ = workbench

    async def start(handler):
        raise RuntimeError("test init error")

    task = asyncio.create_task(cli._run_workbench_startup(
        window, SimpleNamespace(start=start), controller, object()))
    await wait_until(lambda: "失败" in window.title_label.text())
    assert window.isVisible() and not task.done()
    assert not window.loading_panel.timer.isActive()
    assert window.loading_panel.progress.isHidden()
    window.close()
    with pytest.raises(RuntimeError, match="test init error"):
        await task


def test_qt_keeps_painting_during_background_loading(qt_app, workbench):
    import time

    from qasync import QEventLoop

    window, controller, _ = workbench
    ticks = []

    async def start(handler):
        assert window.isVisible()
        window.loading_panel.timer.setInterval(10)
        window.loading_panel.timer.timeout.connect(lambda: ticks.append(True))
        await asyncio.to_thread(time.sleep, 0.15)
        window.close()

    with QEventLoop(qt_app) as loop:
        loop.run_until_complete(cli._run_workbench_startup(
            window, SimpleNamespace(start=start), controller, object()))
    assert len(ticks) >= 2


def test_empty_conversations_finish_loading(workbench):
    window, _, history = workbench
    window.complete_startup([])
    assert not window._loading
    assert window.title_label.text() == "没有白名单会话"
    assert not window.loading_panel.timer.isActive()
    assert history == []


@pytest.mark.asyncio
async def test_loading_follows_main_window_before_sender_is_ready(workbench):
    from dataclasses import replace

    from agent_bridge.companion.follower import WindowRect, WindowSnapshot

    window, _, _ = workbench
    window.settings = replace(window.settings, mode="docked")
    snapshot = WindowSnapshot(True, True, False, WindowRect(100, 100, 800, 700))
    moves = []
    handles = []
    window.probe = SimpleNamespace(
        main_window_handle=lambda: 101,
        snapshot=lambda hwnd: handles.append(hwnd) or snapshot,
    )
    window.owner = SimpleNamespace(bind=lambda *_: True)
    window.mover = SimpleNamespace(move=lambda hwnd, geometry: moves.append(geometry) or True)
    task = asyncio.create_task(window.run())
    await asyncio.sleep(0)
    try:
        assert window._loading and window._follow_timer.isActive()
        assert handles == [101] and len(moves) == 1
        assert "加载" in window.status_label.text()
        snapshot = replace(snapshot, rect=WindowRect(200, 100, 900, 700))
        window._follow_wechat_window()
        assert moves[-1].x == moves[0].x + 100
        snapshot = replace(snapshot, minimized=True)
        window._follow_wechat_window()
        assert not window.isVisible()
        snapshot = replace(snapshot, minimized=False)
        window._follow_wechat_window()
        assert window.isVisible() and window.loading_panel.isVisible()
        assert "加载" in window.status_label.text()
    finally:
        window.close()
        await task
    assert not window._follow_timer.isActive()
