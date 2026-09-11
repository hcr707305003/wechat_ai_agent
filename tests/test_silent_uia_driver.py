import pytest

from agent_bridge.senders.uia_driver import (
    SilentUiaTargetUnavailable,
    SilentUiaUnavailable,
    SilentWeChatUiaDriver,
    UserActivityInterrupted,
    WeChatInputBusy,
)


class ValuePattern:
    def __init__(self, value: str = "") -> None:
        self.values = []
        self.Value = value

    def SetValue(self, value: str) -> None:
        self.values.append(value)
        self.Value = value


class SelectPattern:
    def __init__(self, uia, name: str) -> None:
        self.uia = uia
        self.name = name

    def Select(self) -> None:
        self.uia.current = self.name


class InvokePattern:
    def __init__(self, on_invoke=None) -> None:
        self.calls = 0
        self.on_invoke = on_invoke

    def Invoke(self) -> None:
        self.calls += 1
        if self.on_invoke is not None:
            self.on_invoke()


class Control:
    def __init__(self, *, name="", value=None, select=None, invoke=None, exists=True):
        self.Name = name
        self._value = value
        self._select = select
        self._invoke = invoke
        self._exists = exists
        self.focus_calls = 0

    def Exists(self, *_args) -> bool:
        return self._exists

    def GetValuePattern(self):
        return self._value

    def GetSelectionItemPattern(self):
        return self._select

    def GetInvokePattern(self):
        return self._invoke

    def SetFocus(self):
        self.focus_calls += 1

    def Click(self):
        raise AssertionError("physical/control Click is forbidden")

    def SendKeys(self, *_args, **_kwargs):
        raise AssertionError("SendKeys is forbidden")


class SessionList(Control):
    def __init__(self, children):
        super().__init__()
        self.children = children

    def GetChildren(self):
        return self.children


class Window(Control):
    def __init__(self, sessions, send_button):
        super().__init__()
        self.NativeWindowHandle = 101
        self.sessions = sessions
        self.send_button = send_button

    def ListControl(self, **_kwargs):
        return self.sessions

    def ButtonControl(self, **_kwargs):
        return self.send_button


class FakeUia:
    def __init__(self, *, invoke_consumes: bool = True) -> None:
        self.current = "other"
        self.input_value = ValuePattern()
        self.search_value = ValuePattern()
        self.invoke = InvokePattern(
            (lambda: setattr(self.input_value, "Value", ""))
            if invoke_consumes
            else None
        )
        session = Control(name="friend\nhello")
        session._select = SelectPattern(self, "friend")
        self.win = Window(SessionList([session]), Control(invoke=self.invoke))

    def _find_main(self):
        return self.win

    def current_chat(self):
        assert self._win is self.win
        return self.current

    def _chat_input(self, _win):
        return Control(value=self.input_value)

    def _search_box(self, _win):
        return Control(value=self.search_value)

    def _collect_results(self, *_args, **_kwargs):
        return []


def test_silent_driver_never_switches_another_conversation() -> None:
    uia = FakeUia()
    driver = SilentWeChatUiaDriver(
        lambda: uia,
        sleep=lambda _seconds: None,
        foreground_getter=lambda: 101,
    )

    with pytest.raises(SilentUiaTargetUnavailable):
        driver.send_text(["friend"], "hello", lambda: True)

    assert uia.current == "other"
    assert uia.input_value.values == []
    assert uia.invoke.calls == 0


def test_silent_driver_falls_back_to_targeted_enter_when_invoke_is_noop() -> None:
    uia = FakeUia(invoke_consumes=False)
    uia.current = "friend"
    sent_to = []
    driver = SilentWeChatUiaDriver(
        lambda: uia,
        sleep=lambda _seconds: None,
        window_message_sender=sent_to.append,
        foreground_getter=lambda: 101,
    )

    driver.send_text(["friend"], "hello", lambda: True)

    assert uia.input_value.Value == "hello"
    assert uia.invoke.calls == 1
    assert sent_to == [101]


def test_silent_driver_aborts_when_user_activity_changes() -> None:
    uia = FakeUia()
    uia.current = "friend"
    driver = SilentWeChatUiaDriver(
        lambda: uia,
        sleep=lambda _seconds: None,
        foreground_getter=lambda: 101,
    )
    checks = iter([False])

    with pytest.raises(UserActivityInterrupted):
        driver.send_text(["friend"], "hello", lambda: next(checks))

    assert uia.input_value.values == []
    assert uia.invoke.calls == 0


def test_silent_driver_never_overwrites_an_unsent_draft() -> None:
    uia = FakeUia()
    uia.current = "friend"
    uia.input_value.Value = "my draft"
    driver = SilentWeChatUiaDriver(
        lambda: uia,
        sleep=lambda _seconds: None,
        foreground_getter=lambda: 101,
    )

    with pytest.raises(WeChatInputBusy):
        driver.send_text(["friend"], "agent reply", lambda: True)

    assert uia.input_value.values == []
    assert uia.invoke.calls == 0


def test_silent_driver_requires_atomic_route_when_another_app_is_active() -> None:
    uia = FakeUia()
    uia.current = "friend"
    driver = SilentWeChatUiaDriver(
        lambda: uia,
        sleep=lambda _seconds: None,
        foreground_getter=lambda: 202,
    )

    with pytest.raises(SilentUiaUnavailable):
        driver.send_text(["friend"], "hello", lambda: True)

    assert uia.input_value.values == []
    assert uia.invoke.calls == 0


def test_silent_driver_normalizes_wechat_input_placeholder() -> None:
    uia = FakeUia()
    uia.current = "friend输入文字，或按住Ctrl+Win使用语音输入"
    driver = SilentWeChatUiaDriver(
        lambda: uia,
        sleep=lambda _seconds: None,
        foreground_getter=lambda: 101,
    )

    driver.send_text(["friend"], "hello", lambda: True)

    assert uia.input_value.values == ["hello"]
    assert uia.invoke.calls == 1


def test_main_window_handle_rejects_unverified_native_wechat_shell() -> None:
    class ShellOnlyUia:
        def _find_main(self):
            return None

        def _wechat_hwnds(self):
            return [68532]

    driver = SilentWeChatUiaDriver(ShellOnlyUia)

    assert driver.main_window_handle() is None


def test_main_window_handle_prefers_materialized_uia_window() -> None:
    class MainWindow:
        NativeWindowHandle = 101

    class MaterializedUia:
        def _find_main(self):
            return MainWindow()

        def _wechat_hwnds(self):
            raise AssertionError("native fallback should not be needed")

    driver = SilentWeChatUiaDriver(MaterializedUia)
    driver._window_identity_reader = lambda hwnd: (77, "Qt51514QWindowIcon")

    assert driver.main_window_handle() == 101


@pytest.mark.parametrize("uia_error", [False, True])
def test_cold_start_follows_native_main_without_accessibility(uia_error):
    from types import SimpleNamespace

    def find_main():
        if uia_error:
            raise RuntimeError("UIA provider unavailable")

    driver = SilentWeChatUiaDriver(lambda: SimpleNamespace(
        _find_main=find_main, _wechat_hwnds=lambda: [202, 101]))
    driver._window_identity_reader = lambda hwnd: (77, "Qt51514QWindowIcon")
    driver._is_native_main_shell = lambda hwnd: hwnd == 101
    assert driver.main_window_handle() == 101
    driver._uia_factory = lambda: pytest.fail("pinned shell must not query UIA")
    assert driver.main_window_handle() == 101


def test_native_main_shell_ambiguity_does_not_choose_largest():
    from types import SimpleNamespace

    driver = SilentWeChatUiaDriver(lambda: SimpleNamespace(
        _find_main=lambda: None, _wechat_hwnds=lambda: [202, 101]))
    driver._window_identity_reader = lambda hwnd: (77, "Qt51514QWindowIcon")
    driver._is_native_main_shell = lambda hwnd: True
    assert driver.main_window_handle() is None


@pytest.mark.parametrize(
    "style,exstyle,owner,render,expected",
    [
        (0x96C70000, 0x100, 0, True, True),  # Native main observed locally.
        (0x86C70000, 0x100, 0, True, True),  # Hidden main remains the target.
        (0xB6C70000, 0x100, 0, True, True),  # Minimized main.
        (0x96CF0000, 0x100, 0, True, False),  # Same-title browser/system menu.
        (0xD60B0000, 0, 0, True, False),  # Non-main Qt shell.
        (0x96C70000, 0x180, 0, True, False),  # Tool window.
        (0x96C70000, 0x100, 55, True, False),  # Owned secondary window.
        (0x96C70000, 0x100, 0, False, False),  # No WeChat renderer.
    ],
)
def test_native_main_shell_checks_structure(monkeypatch, style, exstyle, owner, render, expected):
    import sys
    from types import SimpleNamespace

    constants = SimpleNamespace(GWL_STYLE=-16, GWL_EXSTYLE=-20, GW_OWNER=4,
        WS_CAPTION=0xC00000, WS_THICKFRAME=0x40000, WS_MINIMIZEBOX=0x20000,
        WS_MAXIMIZEBOX=0x10000, WS_CHILD=0x40000000, WS_SYSMENU=0x80000,
        WS_EX_TOOLWINDOW=0x80)
    gui = SimpleNamespace(
        GetClassName=lambda h: "Qt51514QWindowIcon" if h == 101 else (
            "MMUIRenderSubWindowHW" if render else "Chrome_WidgetWin_0"),
        GetWindowLong=lambda h, field: style if field == -16 else exstyle,
        GetWindow=lambda h, kind: owner,
        EnumChildWindows=lambda h, callback, data: callback(102, data),
        error=OSError,
    )
    monkeypatch.setitem(sys.modules, "win32con", constants)
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    assert SilentWeChatUiaDriver._is_native_main_shell(101) is expected


def test_main_window_stays_pinned_when_image_viewer_becomes_largest():
    from types import SimpleNamespace

    current = {"main": SimpleNamespace(NativeWindowHandle=101)}
    uia = SimpleNamespace(_find_main=lambda: current["main"], _wechat_hwnds=lambda: [202, 101])
    driver = SilentWeChatUiaDriver(lambda: uia)
    driver._window_identity_reader = lambda hwnd: (77, "Qt51514QWindowIcon")
    assert driver.main_window_handle() == 101
    # UIA is temporarily unavailable while a larger image viewer with the same title opens.
    current["main"] = None
    assert driver.main_window_handle() == 101
    # Once pinned, following must not probe UIA or depend on focus/visibility.
    driver._uia_factory = lambda: pytest.fail("cached main window must not query UIA")
    assert driver.main_window_handle() == 101


def test_main_window_destroyed_waits_for_verified_replacement():
    from types import SimpleNamespace

    current = {"main": SimpleNamespace(NativeWindowHandle=101)}
    identities = {101: (77, "QtWindow")}
    driver = SilentWeChatUiaDriver(lambda: SimpleNamespace(
        _find_main=lambda: current["main"], _wechat_hwnds=lambda: [202]))
    driver._window_identity_reader = identities.get
    assert driver.main_window_handle() == 101
    identities.clear()
    current["main"] = None
    assert driver.main_window_handle() is None
    current["main"] = SimpleNamespace(NativeWindowHandle=303)
    identities[303] = (88, "QtWindow")
    assert driver.main_window_handle() == 303


def test_cached_main_handle_reused_by_another_process_is_rejected():
    from types import SimpleNamespace

    identity = [(77, "QtWindow")]
    current = [SimpleNamespace(NativeWindowHandle=101)]
    driver = SilentWeChatUiaDriver(lambda: SimpleNamespace(
        _find_main=lambda: current[0], _wechat_hwnds=lambda: [202]))
    driver._window_identity_reader = lambda hwnd: identity[0]
    assert driver.main_window_handle() == 101
    identity[0] = (88, "OtherWindow")
    current[0] = None
    assert driver.main_window_handle() is None


def test_clear_current_draft_only_clears_exact_system_text() -> None:
    uia = FakeUia()
    uia.current = "friend"
    uia.input_value.Value = "system reply"
    driver = SilentWeChatUiaDriver(lambda: uia, sleep=lambda _seconds: None)

    assert driver.clear_current_draft(("friend",), "other") is False
    assert uia.input_value.Value == "system reply"
    assert driver.clear_current_draft(("friend",), "system reply") is True
    assert uia.input_value.Value == ""
