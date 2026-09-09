import ctypes

import pytest

from agent_bridge.senders.foreground_driver import (
    DesktopState,
    ForegroundActionUnknown,
    ForegroundDriverChain,
    ForegroundSendUnavailable,
    ForegroundUserInterrupted,
)
from agent_bridge.senders.wechat_mcp_driver import WeChatMcpForegroundDriver


class FakeGuard:
    def __init__(self) -> None:
        self.accepted = []

    def __call__(self) -> bool:
        return True

    def accept_synthetic_input(self, *, allow_cursor_change=False) -> None:
        self.accepted.append(allow_cursor_change)


class FakeEdit:
    def __init__(self, window, name: str) -> None:
        self.window = window
        self.name = name
        self.clicked = 0
        self.keys = []
        self.value = ""

    def Exists(self, *_args) -> bool:
        return True

    def IsEnabled(self) -> bool:
        return True

    def Click(self) -> None:
        self.clicked += 1
        self.window.focused = self.name

    def SendKeys(self, keys, **_kwargs) -> None:
        self.keys.append(keys)
        if keys == "{Ctrl}a":
            return
        if keys == "{Delete}":
            self.value = ""
            return
        self.value += keys

    def GetValuePattern(self):
        return type("Value", (), {"Value": self.value})()


class FakeWindow:
    NativeWindowHandle = 101

    def __init__(self, *, rendered_input: bool = False) -> None:
        self.search = FakeEdit(self, "搜索")
        self.input = FakeEdit(self, "输入")
        self.rendered_input = rendered_input
        self.keys = []
        self.active = 0
        self.topmost = []
        self.current = "Other"
        self.focused = ""
        self.final_send_triggered = False

    def Exists(self, *_args) -> bool:
        return True

    def SetActive(self) -> None:
        self.active += 1

    def SetTopmost(self, value: bool) -> None:
        self.topmost.append(value)

    def EditControl(self, *, Name=None, **_kwargs):
        if Name == "搜索":
            return self.search
        if self.rendered_input:
            return MissingEdit()
        return self.input

    def GetChildren(self):
        return []

    def SendKeys(self, keys, **_kwargs) -> None:
        self.keys.append(keys)
        if keys == "{Enter}" and self.current != "Friend":
            self.current = "Friend"


class FakeAutomation:
    def __init__(self, window: FakeWindow) -> None:
        self.window = window
        self.clicks = []
        self.keys = []
        self.clipboard = "original clipboard"

    def WindowControl(self, **_kwargs):
        return self.window

    def EditControl(self, **_kwargs):
        return self.window.input

    def Click(self, x, y) -> None:
        self.clicks.append((x, y))
        self.window.focused = "rendered"

    def SendKeys(self, keys, **_kwargs) -> None:
        self.keys.append(keys)
        if self.window.focused == "搜索":
            self.window.search.SendKeys(keys)
            if keys == "{Enter}":
                self.window.current = "Friend"
            return
        if self.window.focused == "输入":
            if keys == "{Enter}":
                self.window.final_send_triggered = True
            else:
                self.window.input.SendKeys(keys)
            return
        if self.window.focused == "rendered" and keys == "{Enter}":
            self.window.final_send_triggered = True

    def GetClipboardText(self) -> str:
        return self.clipboard

    def SetClipboardText(self, value: str) -> None:
        self.clipboard = value


class MissingEdit:
    def Exists(self, *_args) -> bool:
        return False


class FakeDesktop:
    def __init__(self) -> None:
        self.restored = []
        self.activated = []

    def capture(self, _hwnd):
        return DesktopState(55, (10, 20), False)

    def activate(self, hwnd):
        self.activated.append(hwnd)

    def restore(self, state, hwnd, *, restore_user_state):
        self.restored.append((state, hwnd, restore_user_state))


def test_wechat_mcp_driver_uses_proven_ctrl_f_send_sequence() -> None:
    window = FakeWindow()
    desktop = FakeDesktop()
    guard = FakeGuard()
    driver = WeChatMcpForegroundDriver(
        automation=FakeAutomation(window),
        desktop=desktop,
        sleep=lambda _seconds: None,
        current_chat=lambda: window.current,
    )

    driver.send_text(("Friend", "wxid_friend"), "hello", guard)

    assert window.active == 0
    assert desktop.activated == [window.NativeWindowHandle]
    assert window.topmost == []
    assert driver.automation.keys == [
        "{Ctrl}F",
        "{Ctrl}a",
        "{Delete}",
        "Friend",
        "{Enter}",
        "{Ctrl}v",
        "{Enter}",
    ]
    assert window.search.clicked == 1
    assert window.search.keys == ["{Ctrl}a", "{Delete}", "Friend", "{Enter}"]
    assert window.input.clicked == 1
    assert window.input.keys == ["{Ctrl}v"]
    assert desktop.restored[0][2] is True
    assert True in guard.accepted


def test_wechat_mcp_driver_activates_by_hwnd_without_uia_control() -> None:
    class BrokenSetActiveWindow(FakeWindow):
        def SetActive(self) -> None:
            raise ctypes.ArgumentError("stale UIA window")

    window = BrokenSetActiveWindow()
    desktop = FakeDesktop()
    driver = WeChatMcpForegroundDriver(
        automation=FakeAutomation(window),
        desktop=desktop,
        sleep=lambda _seconds: None,
        current_chat=lambda: window.current,
    )

    driver.send_text(("Friend",), "hello", FakeGuard())

    assert desktop.activated == [window.NativeWindowHandle]
    assert window.final_send_triggered is True


def test_wechat_mcp_driver_pastes_image_file_then_sends() -> None:
    window = FakeWindow()
    desktop = FakeDesktop()
    copied = []
    driver = WeChatMcpForegroundDriver(
        automation=FakeAutomation(window),
        desktop=desktop,
        sleep=lambda _seconds: None,
        current_chat=lambda: window.current,
        clipboard_file=copied.append,
    )

    driver.send_image(("Friend",), "C:/workspace/result.png", FakeGuard())

    assert copied == ["C:/workspace/result.png"]
    assert driver.automation.keys[-2:] == ["{Ctrl}v", "{Enter}"]
    assert window.input.keys == ["{Ctrl}v"]
    assert window.final_send_triggered is True
    assert desktop.restored[0][2] is True


def test_wechat_mcp_driver_writes_text_pastes_image_then_sends_once() -> None:
    window = FakeWindow()
    desktop = FakeDesktop()
    copied = []
    driver = WeChatMcpForegroundDriver(
        automation=FakeAutomation(window),
        desktop=desktop,
        sleep=lambda _seconds: None,
        current_chat=lambda: window.current,
        clipboard_file=copied.append,
    )

    driver.send_text_and_image(
        ("Friend",), "处理完成。", "C:/workspace/result.png", FakeGuard()
    )

    assert copied == ["C:/workspace/result.png"]
    assert window.input.keys == ["{Ctrl}v", "{Ctrl}v"]
    assert driver.automation.keys.count("{Enter}") == 2
    assert window.final_send_triggered is True


def test_wechat_mcp_driver_retries_transient_pre_send_argument_error() -> None:
    window = FakeWindow()
    desktop = FakeDesktop()

    class FlakyAutomation(FakeAutomation):
        def __init__(self, target_window: FakeWindow) -> None:
            super().__init__(target_window)
            self.failures = 1

        def SendKeys(self, keys, **kwargs) -> None:
            if keys == "{Ctrl}F" and self.failures:
                self.failures -= 1
                raise ctypes.ArgumentError("stale UIA control")
            super().SendKeys(keys, **kwargs)

    automation = FlakyAutomation(window)
    copied = []
    driver = WeChatMcpForegroundDriver(
        automation=automation,
        desktop=desktop,
        sleep=lambda _seconds: None,
        current_chat=lambda: window.current,
        clipboard_file=copied.append,
    )

    driver.send_image(("Friend",), "C:/workspace/result.png", FakeGuard())

    assert automation.failures == 0
    assert copied == ["C:/workspace/result.png"]
    assert window.final_send_triggered is True
    assert len(desktop.restored) == 2


def test_wechat_mcp_driver_initializes_uia_in_worker_thread() -> None:
    window = FakeWindow()

    class ThreadBoundAutomation(FakeAutomation):
        def __init__(self, target_window: FakeWindow) -> None:
            super().__init__(target_window)
            self.initialized = False
            self.initializer_entries = 0

        def UIAutomationInitializerInThread(self):
            automation = self

            class Initializer:
                def __enter__(self):
                    automation.initialized = True
                    automation.initializer_entries += 1
                    return self

                def __exit__(self, *_args):
                    automation.initialized = False

            return Initializer()

        def WindowControl(self, **kwargs):
            if not self.initialized:
                raise ctypes.ArgumentError("UIA thread is not initialized")
            return super().WindowControl(**kwargs)

    automation = ThreadBoundAutomation(window)
    copied = []
    driver = WeChatMcpForegroundDriver(
        automation=automation,
        desktop=FakeDesktop(),
        sleep=lambda _seconds: None,
        current_chat=lambda: window.current,
        clipboard_file=copied.append,
    )

    driver.send_image(("Friend",), "C:/workspace/result.png", FakeGuard())

    assert automation.initializer_entries == 1
    assert automation.initialized is False
    assert copied == ["C:/workspace/result.png"]
    assert window.final_send_triggered is True


def test_wechat_mcp_driver_clicks_rendered_input_on_wechat_4() -> None:
    window = FakeWindow(rendered_input=True)
    window.BoundingRectangle = type(
        "Rect",
        (),
        {"left": -874, "right": 6, "top": 439, "bottom": 1067},
    )()
    automation = FakeAutomation(window)
    driver = WeChatMcpForegroundDriver(
        automation=automation,
        desktop=FakeDesktop(),
        sleep=lambda _seconds: None,
        current_chat=lambda: "工藤新一",
    )

    driver.send_text(("工藤新一",), "hello", FakeGuard())

    assert automation.clicks == [(-275, 977)]
    assert automation.keys[-4:] == ["{Ctrl}a", "{Ctrl}c", "{Ctrl}v", "{Enter}"]
    assert automation.clipboard == "original clipboard"


class InterruptAfterFinalSend(FakeGuard):
    def __init__(self, window: FakeWindow) -> None:
        super().__init__()
        self.window = window

    def accept_synthetic_input(self, *, allow_cursor_change=False) -> None:
        super().accept_synthetic_input(allow_cursor_change=allow_cursor_change)
        if self.window.final_send_triggered:
            raise ForegroundUserInterrupted("User input resumed")


def test_wechat_mcp_driver_treats_post_send_interruption_as_unknown() -> None:
    window = FakeWindow()
    automation = FakeAutomation(window)
    driver = WeChatMcpForegroundDriver(
        automation=automation,
        desktop=FakeDesktop(),
        sleep=lambda _seconds: None,
        current_chat=lambda: window.current,
    )

    with pytest.raises(ForegroundActionUnknown):
        driver.send_text(("Friend",), "hello", InterruptAfterFinalSend(window))

    assert window.final_send_triggered is True


class InterruptBeforeFinalSend(FakeGuard):
    def __init__(self, window: FakeWindow) -> None:
        super().__init__()
        self.window = window

    def __call__(self) -> bool:
        return self.window.current != "Friend"


def test_wechat_mcp_driver_keeps_pre_send_interruption_retryable() -> None:
    window = FakeWindow()
    driver = WeChatMcpForegroundDriver(
        automation=FakeAutomation(window),
        desktop=FakeDesktop(),
        sleep=lambda _seconds: None,
        current_chat=lambda: window.current,
    )

    with pytest.raises(ForegroundUserInterrupted):
        driver.send_text(("Friend",), "hello", InterruptBeforeFinalSend(window))

    assert window.final_send_triggered is False


class StubDriver:
    def __init__(self, error=None) -> None:
        self.error = error
        self.calls = 0

    def send_text(self, _targets, _text, _guard) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error

    def send_image(self, _targets, _path, _guard) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


def test_foreground_chain_uses_uia_only_when_mcp_is_unavailable() -> None:
    primary = StubDriver(ForegroundSendUnavailable("missing control"))
    fallback = StubDriver()
    chain = ForegroundDriverChain(primary, fallback)

    chain.send_text(("Friend",), "hello", lambda: True)

    assert primary.calls == 1
    assert fallback.calls == 1


def test_foreground_chain_never_retries_unknown_mcp_send() -> None:
    primary = StubDriver(ForegroundActionUnknown("send may have happened"))
    fallback = StubDriver()
    chain = ForegroundDriverChain(primary, fallback)

    with pytest.raises(ForegroundActionUnknown):
        chain.send_text(("Friend",), "hello", lambda: True)

    assert primary.calls == 1
    assert fallback.calls == 0


def test_foreground_chain_sends_images_only_through_mcp_driver() -> None:
    primary = StubDriver()
    fallback = StubDriver()
    chain = ForegroundDriverChain(primary, fallback)

    chain.send_image(("Friend",), "C:/workspace/result.png", lambda: True)

    assert primary.calls == 1
    assert fallback.calls == 0
