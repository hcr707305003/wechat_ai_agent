from types import SimpleNamespace

import pytest

from agent_bridge.models import ConversationType, ReplyReference
from agent_bridge.senders.foreground_driver import (
    DesktopState,
    ForegroundActionUnknown,
    ForegroundInputBusy,
    ForegroundQuoteUnavailable,
    ForegroundUserInterrupted,
    ForegroundWeChatUiaDriver,
)


class ValuePattern:
    def __init__(self, value="") -> None:
        self.Value = value
        self.values = []

    def SetValue(self, value) -> None:
        self.Value = value
        self.values.append(value)


class Edit:
    def __init__(self, value="", *, fail_enter=False) -> None:
        self.value = ValuePattern(value)
        self.enter_calls = 0
        self.fail_enter = fail_enter

    def GetValuePattern(self):
        return self.value

    def SetFocus(self) -> None:
        pass

    def SendKeys(self, keys, **_kwargs) -> None:
        assert keys == "{Enter}"
        self.enter_calls += 1
        if self.fail_enter:
            raise OSError("unknown key result")
        self.value.Value = ""


class MissingList:
    def Exists(self, *_args) -> bool:
        return False


class Window:
    NativeWindowHandle = 101

    def __init__(self, on_enter=None) -> None:
        self.on_enter = on_enter
        self.enter_calls = 0

    def ListControl(self, **_kwargs):
        return MissingList()

    def SendKeys(self, keys, **_kwargs) -> None:
        assert keys == "{Enter}"
        self.enter_calls += 1
        if self.on_enter is not None:
            self.on_enter()


class FakeUia:
    def __init__(
        self, *, current="Friend", draft="", results=None, fail_enter=False
    ) -> None:
        self.current = current
        self.edit = Edit(draft, fail_enter=fail_enter)
        self.search = Edit()
        self.results = results or []
        self.fail_enter = fail_enter
        self.window_message_calls = []
        self.win = Window()

    def _find_main(self):
        return self.win

    def current_chat(self):
        return self.current

    def _chat_input(self, _win):
        return self.edit

    def _search_box(self, _win):
        return self.search

    def _collect_results(self, *_args, **_kwargs):
        return self.results

    def send_enter_to_window(self, hwnd) -> None:
        self.window_message_calls.append(hwnd)
        if self.fail_enter:
            raise OSError("unknown key result")
        if self.current != "Friend" and self.search.value.Value:
            self.current = self.search.value.Value
            self.search.value.Value = ""
            return
        self.edit.value.Value = ""


class Desktop:
    def __init__(self) -> None:
        self.activated = []
        self.restored = []

    def capture(self, _hwnd):
        return DesktopState(55, (10, 20), False)

    def activate(self, hwnd):
        self.activated.append(hwnd)

    def restore(self, state, hwnd, *, restore_user_state):
        self.restored.append((state, hwnd, restore_user_state))


def test_foreground_driver_sends_one_enter_in_exact_current_chat() -> None:
    uia = FakeUia()
    desktop = Desktop()
    driver = ForegroundWeChatUiaDriver(
        lambda: uia,
        desktop=desktop,
        sleep=lambda _seconds: None,
        window_message_sender=uia.send_enter_to_window,
    )

    driver.send_text(("Friend",), "hello", lambda: True)

    assert uia.edit.value.values == ["hello"]
    assert uia.window_message_calls == [101]
    assert desktop.activated == [101]
    assert desktop.restored[0][2] is True


def test_foreground_driver_reuses_matching_system_draft() -> None:
    uia = FakeUia(draft="hello")
    driver = ForegroundWeChatUiaDriver(
        lambda: uia,
        desktop=Desktop(),
        sleep=lambda _seconds: None,
        window_message_sender=uia.send_enter_to_window,
    )

    driver.send_text(("Friend",), "hello", lambda: True)

    assert uia.edit.value.values == []
    assert uia.window_message_calls == [101]


def test_foreground_driver_never_overwrites_other_draft() -> None:
    uia = FakeUia(draft="my draft")
    driver = ForegroundWeChatUiaDriver(
        lambda: uia,
        desktop=Desktop(),
        sleep=lambda _seconds: None,
        window_message_sender=uia.send_enter_to_window,
    )

    with pytest.raises(ForegroundInputBusy):
        driver.send_text(("Friend",), "hello", lambda: True)

    assert uia.window_message_calls == []
    assert uia.edit.value.Value == "my draft"


def test_foreground_driver_restores_when_user_interrupts() -> None:
    uia = FakeUia()
    desktop = Desktop()
    checks = iter([True, True, False, False])
    driver = ForegroundWeChatUiaDriver(
        lambda: uia,
        desktop=desktop,
        sleep=lambda _seconds: None,
        window_message_sender=uia.send_enter_to_window,
    )

    with pytest.raises(ForegroundUserInterrupted):
        driver.send_text(("Friend",), "hello", lambda: next(checks))

    assert uia.window_message_calls == []
    assert desktop.restored[0][2] is False


def test_foreground_driver_search_does_not_enumerate_blocking_result_popup() -> None:
    results = [
        {"name": "Friend", "cell": object()},
        {"name": "Friend", "cell": object()},
    ]
    uia = FakeUia(current="Other", results=results)
    driver = ForegroundWeChatUiaDriver(
        lambda: uia,
        desktop=Desktop(),
        sleep=lambda _seconds: None,
        window_message_sender=uia.send_enter_to_window,
    )

    driver.send_text(("Friend",), "hello", lambda: True)

    assert uia.window_message_calls == [101, 101]
    assert uia.search.value.Value == ""


def test_foreground_driver_enters_unique_exact_search_result_with_enter() -> None:
    results = [{"name": "Friend", "cell": object()}]
    uia = FakeUia(current="Other", results=results)
    driver = ForegroundWeChatUiaDriver(
        lambda: uia,
        desktop=Desktop(),
        sleep=lambda _seconds: None,
        window_message_sender=uia.send_enter_to_window,
    )

    driver.send_text(("Friend",), "hello", lambda: True)

    assert uia.window_message_calls == [101, 101]
    assert uia.edit.value.values == ["hello"]


def test_foreground_driver_clicks_visible_session_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        Name = "Friend\npreview"

    class Sessions:
        def Exists(self, *_args) -> bool:
            return True

        def GetChildren(self):
            return [Session()]

    uia = FakeUia(current="Other")
    uia.win.ListControl = lambda **_kwargs: Sessions()
    driver = ForegroundWeChatUiaDriver(
        lambda: uia,
        desktop=Desktop(),
        sleep=lambda _seconds: None,
        window_message_sender=uia.send_enter_to_window,
    )
    clicked = []

    def click(win, control, **_kwargs) -> None:
        clicked.append((win, control))
        uia.current = "Friend"

    monkeypatch.setattr(driver, "_click_control_via_window", click)

    driver.send_text(("Friend",), "hello", lambda: True)

    assert len(clicked) == 1
    assert clicked[0][0] is uia.win
    assert uia.search.value.values == []
    assert uia.window_message_calls == [101]


def test_foreground_driver_reports_unknown_when_enter_raises() -> None:
    uia = FakeUia(fail_enter=True)
    driver = ForegroundWeChatUiaDriver(
        lambda: uia,
        desktop=Desktop(),
        sleep=lambda _seconds: None,
        window_message_sender=uia.send_enter_to_window,
    )

    with pytest.raises(ForegroundActionUnknown):
        driver.send_text(("Friend",), "hello", lambda: True)

    assert uia.window_message_calls == [101]


def test_foreground_driver_normalizes_wechat_input_placeholder() -> None:
    uia = FakeUia(current="Friend输入文字，或按住Ctrl+Win使用语音输入")
    driver = ForegroundWeChatUiaDriver(
        lambda: uia,
        desktop=Desktop(),
        sleep=lambda _seconds: None,
        window_message_sender=uia.send_enter_to_window,
    )

    driver.send_text(("Friend",), "hello", lambda: True)

    assert uia.search.value.values == []
    assert uia.window_message_calls == [101]


def test_foreground_driver_accepts_its_own_activation() -> None:
    class Guard:
        def __init__(self) -> None:
            self.accepted = 0

        def __call__(self) -> bool:
            return True

        def accept_synthetic_input(self) -> None:
            self.accepted += 1

    uia = FakeUia()
    guard = Guard()
    driver = ForegroundWeChatUiaDriver(
        lambda: uia,
        desktop=Desktop(),
        sleep=lambda _seconds: None,
        window_message_sender=uia.send_enter_to_window,
    )

    driver.send_text(("Friend",), "hello", guard)

    assert guard.accepted == 1


def test_quote_locator_selects_exact_identical_historical_occurrence() -> None:
    class Rect:
        left, top, right, bottom = 0, 0, 400, 500

    class Message:
        ClassName = "mmui::ChatTextItemView"

        def __init__(self, runtimeid, name) -> None:
            self.runtimeid = runtimeid
            self.Name = name
            self.BoundingRectangle = Rect()

    class MessageList:
        BoundingRectangle = Rect()

        def __init__(self) -> None:
            self.page = 0
            self.pages = [
                [Message("new", "same")],
                [Message("old", "same")],
            ]

        def GetChildren(self):
            return self.pages[self.page]

    class QuoteUia:
        def __init__(self) -> None:
            self.messages = MessageList()

        def _message_list(self, _win):
            return self.messages

        def _set_cursor(self, *_args):
            pass

        def _mouse_wheel(self, delta):
            self.messages.page = 0 if delta < 0 else 1

    class Guard:
        def __call__(self):
            return True

        def accept_synthetic_input(self, **_kwargs):
            pass

    reference = ReplyReference(
        "friend:1",
        "friend",
        ConversationType.PRIVATE,
        "sender",
        "same",
        occurrence_from_latest=1,
    )
    driver = ForegroundWeChatUiaDriver(sleep=lambda _seconds: None)

    selected = driver._locate_quote_control(QuoteUia(), object(), reference, Guard())

    assert selected.runtimeid == "old"


def test_quote_locator_selects_the_30th_recent_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Rect:
        left, top, right, bottom = 0, 0, 400, 500

    class Message:
        ClassName = "mmui::ChatTextItemView"

        def __init__(self, runtimeid, name) -> None:
            self.runtimeid = runtimeid
            self.Name = name
            self.BoundingRectangle = Rect()

    class MessageList:
        BoundingRectangle = Rect()

        def __init__(self) -> None:
            self.page = 0
            self.pages = [
                [Message(f"new-{index}", f"new-{index}") for index in range(15)],
                [Message("target", "wanted")]
                + [Message(f"old-{index}", f"old-{index}") for index in range(14)],
            ]

        def GetChildren(self):
            return self.pages[self.page]

    class QuoteUia:
        def __init__(self) -> None:
            self.messages = MessageList()

        def _message_list(self, _win):
            return self.messages

    uia = QuoteUia()
    driver = ForegroundWeChatUiaDriver(sleep=lambda _seconds: None)
    monkeypatch.setattr(driver, "_scroll_to_latest", lambda *_args: None)
    monkeypatch.setattr(
        driver,
        "_scroll_message_list",
        lambda *_args: setattr(uia.messages, "page", 1),
    )
    reference = ReplyReference(
        "friend:target",
        "friend",
        ConversationType.PRIVATE,
        "sender",
        "wanted",
        occurrence_from_latest=0,
    )

    selected = driver._locate_quote_control(uia, object(), reference, lambda: True)

    assert selected.runtimeid == "target"


def test_quote_locator_does_not_scan_the_31st_recent_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Rect:
        left, top, right, bottom = 0, 0, 400, 500

    class Control:
        def __init__(self, runtimeid, name, class_name="mmui::ChatTextItemView"):
            self.runtimeid = runtimeid
            self.Name = name
            self.ClassName = class_name
            self.BoundingRectangle = Rect()

    class MessageList:
        BoundingRectangle = Rect()

        def __init__(self) -> None:
            self.page = 0
            self.pages = [
                [Control("time", "昨天", "mmui::ChatItemView")]
                + [Control(f"new-{index}", f"new-{index}") for index in range(15)],
                [Control(f"old-{index}", f"old-{index}") for index in range(15)],
                [Control("target", "wanted")],
            ]

        def GetChildren(self):
            return self.pages[self.page]

    class QuoteUia:
        def __init__(self) -> None:
            self.messages = MessageList()

        def _message_list(self, _win):
            return self.messages

    uia = QuoteUia()
    driver = ForegroundWeChatUiaDriver(sleep=lambda _seconds: None)
    monkeypatch.setattr(driver, "_scroll_to_latest", lambda *_args: None)

    def next_page(*_args) -> None:
        uia.messages.page = min(uia.messages.page + 1, 2)

    monkeypatch.setattr(driver, "_scroll_message_list", next_page)
    reference = ReplyReference(
        "friend:target",
        "friend",
        ConversationType.PRIVATE,
        "sender",
        "wanted",
        occurrence_from_latest=0,
    )

    with pytest.raises(
        ForegroundQuoteUnavailable,
        match="not found in recent 30 messages",
    ):
        driver._locate_quote_control(uia, object(), reference, lambda: True)

    assert uia.messages.page == 1


def test_quote_locator_stops_after_two_stale_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Rect:
        left, top, right, bottom = 0, 0, 400, 500

    class Message:
        ClassName = "mmui::ChatTextItemView"
        Name = "other"
        runtimeid = "same"
        BoundingRectangle = Rect()

    class MessageList:
        BoundingRectangle = Rect()

        @staticmethod
        def GetChildren():
            return [Message()]

    class QuoteUia:
        @staticmethod
        def _message_list(_win):
            return MessageList()

    driver = ForegroundWeChatUiaDriver(sleep=lambda _seconds: None)
    monkeypatch.setattr(driver, "_scroll_to_latest", lambda *_args: None)
    scrolls = []
    monkeypatch.setattr(
        driver,
        "_scroll_message_list",
        lambda *_args: scrolls.append(True),
    )
    reference = ReplyReference(
        "friend:missing",
        "friend",
        ConversationType.PRIVATE,
        "sender",
        "missing",
        occurrence_from_latest=0,
    )

    with pytest.raises(ForegroundQuoteUnavailable):
        driver._locate_quote_control(
            QuoteUia(),
            object(),
            reference,
            lambda: True,
        )

    assert len(scrolls) == 2


def test_scroll_to_latest_has_a_small_fixed_wheel_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Rect:
        left, top, right, bottom = 0, 0, 400, 500

    class Message:
        ClassName = "mmui::ChatTextItemView"
        Name = "message"
        BoundingRectangle = Rect()

        def __init__(self, runtimeid: str) -> None:
            self.runtimeid = runtimeid

    class MessageList:
        def __init__(self) -> None:
            self.reads = 0

        def GetChildren(self):
            self.reads += 1
            return [Message(str(self.reads))]

    driver = ForegroundWeChatUiaDriver(sleep=lambda _seconds: None)
    scrolls = []
    monkeypatch.setattr(
        driver,
        "_scroll_message_list",
        lambda *_args: scrolls.append(True),
    )

    with pytest.raises(ForegroundQuoteUnavailable, match="latest messages"):
        driver._scroll_to_latest(
            object(),
            MessageList(),
            lambda: True,
        )

    assert len(scrolls) == 6


def test_foreground_driver_sends_native_quote_once(monkeypatch) -> None:
    uia = FakeUia()
    desktop = Desktop()
    driver = ForegroundWeChatUiaDriver(
        lambda: uia,
        desktop=desktop,
        sleep=lambda _seconds: None,
        window_message_sender=uia.send_enter_to_window,
    )
    reference = ReplyReference(
        "friend:1",
        "friend",
        ConversationType.PRIVATE,
        "sender",
        "original",
        occurrence_from_latest=0,
    )
    selected = []
    monkeypatch.setattr(driver, "_locate_quote_control", lambda *_args: object())
    monkeypatch.setattr(driver, "_select_quote", lambda *_args: selected.append(True))
    monkeypatch.setattr(driver, "_wait_for_quote_banner", lambda *_args: True)

    driver.send_quote(("Friend",), "reply", reference, lambda: True)

    assert selected == [True]
    assert uia.edit.value.values == ["reply"]
    assert uia.window_message_calls == [101]
    assert desktop.restored[0][2] is True


def test_quote_banner_lookup_is_scoped_to_the_composer() -> None:
    class Button:
        def __init__(self) -> None:
            self.clicked = False
            self.owner = None

        def Exists(self, *_args) -> bool:
            return True

        def Click(self) -> None:
            self.clicked = True
            self.owner._children.clear()

    class Control:
        def __init__(self, class_name, name="", children=(), button=None) -> None:
            self.ClassName = class_name
            self.Name = name
            self._children = list(children)
            self._button = button
            for child in self._children:
                child._parent = self

        def GetChildren(self):
            return self._children

        def GetParentControl(self):
            return self._parent

        def ButtonControl(self, *, Name):
            assert Name == "删除引用消息"
            return self._button

    stale = Control("mmui::ReferView", "引用 别人 的消息 : 另一条历史消息")
    close_button = Button()
    current = Control("mmui::ReferView", "引用 工藤新一 的消息 : 引用测试")
    composer = Control(
        "mmui::ComposeReferView",
        children=(current,),
        button=close_button,
    )
    close_button.owner = composer
    root = Control("Window", children=(stale, composer))
    reference = ReplyReference(
        "friend:1",
        "friend",
        ConversationType.PRIVATE,
        "sender",
        "引用测试",
        occurrence_from_latest=0,
    )

    assert ForegroundWeChatUiaDriver._quote_banner_matches(root, reference) is True
    driver = ForegroundWeChatUiaDriver(sleep=lambda _seconds: None)
    assert driver._dismiss_quote_banner(root) is True
    assert close_button.clicked is True


def test_quote_banner_lookup_reaches_real_wechat_tree_depth() -> None:
    class Control:
        def __init__(self, class_name, children=()) -> None:
            self.ClassName = class_name
            self.Name = ""
            self._children = list(children)

        def GetChildren(self):
            return self._children

    banner = Control("mmui::ReferView")
    root = Control("mmui::ComposeReferView", (banner,))
    for _ in range(17):
        root = Control("mmui::XView", (root,))

    assert ForegroundWeChatUiaDriver._composer_quote_banner(root) is banner


def test_quote_wait_accepts_new_banner_with_truncated_accessible_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = ReplyReference(
        "friend:1",
        "friend",
        ConversationType.PRIVATE,
        "sender",
        "a long original message that WeChat truncates",
        occurrence_from_latest=0,
    )
    driver = ForegroundWeChatUiaDriver(sleep=lambda _seconds: None)
    monkeypatch.setattr(driver, "_composer_quote_banner", lambda _win: object())

    assert driver._wait_for_quote_banner(object(), reference) is True


def test_quoted_text_keys_require_quote_echo_and_stable_identity() -> None:
    class Message:
        ClassName = "mmui::ChatTextItemView"

        def __init__(self, runtimeid, name) -> None:
            self.runtimeid = runtimeid
            self.Name = name

    class MessageList:
        def GetChildren(self):
            return (
                Message("plain", "reply"),
                Message("quoted", "reply\n引用 Friend 的消息 : original"),
            )

    class Uia:
        def _message_list(self, _win):
            return MessageList()

    assert ForegroundWeChatUiaDriver._quoted_text_keys(
        Uia(), object(), "reply"
    ) == {"quoted"}


def test_quote_menu_falls_back_to_invoke_when_click_is_unavailable() -> None:
    class Invoke:
        def __init__(self) -> None:
            self.calls = 0

        def Invoke(self) -> None:
            self.calls += 1

    class Item:
        Name = "引用"

        def __init__(self) -> None:
            self.invoke = Invoke()

        def GetInvokePattern(self):
            return self.invoke

        def Click(self) -> None:
            raise RuntimeError("physical click unavailable")

    class Menu:
        control = object()

        def __init__(self) -> None:
            self.item = Item()
            self.option_controls = [self.item]

        def exists(self, _timeout) -> bool:
            return True

    menu = Menu()

    assert ForegroundWeChatUiaDriver._invoke_menu_item(menu, "引用") is True
    assert menu.item.invoke.calls == 1


def test_quote_menu_prefers_real_click_over_noop_qt_invoke() -> None:
    class Invoke:
        def __init__(self) -> None:
            self.calls = 0

        def Invoke(self) -> None:
            self.calls += 1

    class Item:
        Name = "引用"

        def __init__(self) -> None:
            self.invoke = Invoke()
            self.clicks = 0

        def Click(self) -> None:
            self.clicks += 1

        def GetInvokePattern(self):
            return self.invoke

    class Menu:
        control = object()

        def __init__(self) -> None:
            self.item = Item()
            self.option_controls = [self.item]

        def exists(self, _timeout) -> bool:
            return True

    menu = Menu()

    assert ForegroundWeChatUiaDriver._invoke_menu_item(menu, "引用") is True
    assert menu.item.clicks == 1
    assert menu.item.invoke.calls == 0


def test_quote_selection_scrolls_virtualized_message_into_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pattern:
        def __init__(self) -> None:
            self.calls = 0

        def ScrollIntoView(self) -> None:
            self.calls += 1

    class Control:
        def __init__(self) -> None:
            self.pattern = Pattern()

        def GetScrollItemPattern(self):
            return self.pattern

    class Menu:
        control = object()
        option_controls = ()

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def exists(self, _timeout) -> bool:
            return True

    monkeypatch.setattr("wechatauto.ui.component.Menu", Menu)
    driver = ForegroundWeChatUiaDriver(sleep=lambda _seconds: None)
    monkeypatch.setattr(driver, "_show_uia_context_menu", lambda _control: True)
    monkeypatch.setattr(driver, "_invoke_menu_item", lambda _menu, _name: True)
    control = Control()
    win = SimpleNamespace(ProcessId=1)

    driver._select_quote(win, control)

    assert control.pattern.calls == 1


def test_quote_surface_refresh_retries_transient_stale_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_uia = object()
    old_win = object()
    stale_uia = SimpleNamespace()
    fresh_uia = SimpleNamespace()
    fresh_win = object()
    factories = iter((stale_uia, fresh_uia))
    sleeps = []
    driver = ForegroundWeChatUiaDriver(
        lambda: next(factories),
        sleep=sleeps.append,
    )

    def materialized(uia):
        if uia is stale_uia:
            raise OSError("stale UIA tree")
        return fresh_win

    monkeypatch.setattr(driver, "_materialized_main", materialized)

    refreshed_uia, refreshed_win = driver._refresh_quote_surface(old_uia, old_win)

    assert refreshed_uia is fresh_uia
    assert refreshed_win is fresh_win
    assert sleeps == [0.05]


def test_quote_cleanup_failure_is_unknown_and_never_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uia = FakeUia()
    driver = ForegroundWeChatUiaDriver(
        lambda: uia,
        desktop=Desktop(),
        sleep=lambda _seconds: None,
        window_message_sender=uia.send_enter_to_window,
    )
    reference = ReplyReference(
        "friend:1",
        "friend",
        ConversationType.PRIVATE,
        "sender",
        "引用测试",
        occurrence_from_latest=0,
    )
    monkeypatch.setattr(driver, "_locate_quote_control", lambda *_args: object())
    monkeypatch.setattr(driver, "_select_quote", lambda *_args: None)
    monkeypatch.setattr(driver, "_wait_for_quote_banner", lambda *_args: False)
    monkeypatch.setattr(driver, "_dismiss_quote_banner", lambda *_args: False)

    with pytest.raises(
        ForegroundActionUnknown,
        match="quote draft could not be cleared safely",
    ):
        driver.send_quote(("Friend",), "reply", reference, lambda: True)

    assert uia.window_message_calls == []
