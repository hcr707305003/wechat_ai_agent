from __future__ import annotations

import ctypes
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_bridge.models import ContentType, ReplyReference
from agent_bridge.quotes import QUOTE_HISTORY_LIMIT
from agent_bridge.senders.uia_driver import chat_matches, send_enter_to_window
from agent_bridge.senders.windows_clipboard import copy_file_to_clipboard

logger = logging.getLogger(__name__)


class ForegroundSendUnavailable(RuntimeError):
    pass


class ForegroundTargetUnavailable(ForegroundSendUnavailable):
    pass


class ForegroundInputBusy(RuntimeError):
    pass


class ForegroundUserInterrupted(RuntimeError):
    pass


class ForegroundActionUnknown(RuntimeError):
    pass


class ForegroundQuoteUnavailable(ForegroundSendUnavailable):
    """The quote target could not be selected before payload mutation."""


Guard = Callable[[], bool]


class ForegroundDriverChain:
    """Use the WeChatMCP path first and UIA only before a send action."""

    def __init__(self, primary: Any, fallback: Any) -> None:
        self.primary = primary
        self.fallback = fallback

    def send_text(self, targets: Iterable[str], text: str, guard: Guard) -> None:
        try:
            self.primary.send_text(targets, text, guard)
        except ForegroundSendUnavailable as error:
            logger.warning("WeChatMCP 前台发送不可用，尝试 UIA 兜底: %s", error)
            self.fallback.send_text(targets, text, guard)

    def send_image(
        self, targets: Iterable[str], image_path: str, guard: Guard
    ) -> None:
        sender = getattr(self.primary, "send_image", None)
        if not callable(sender):
            raise ForegroundSendUnavailable(
                "The configured WeChat foreground driver cannot send images"
            )
        sender(targets, image_path, guard)

    def send_text_and_image(
        self,
        targets: Iterable[str],
        text: str,
        image_path: str,
        guard: Guard,
    ) -> None:
        sender = getattr(self.primary, "send_text_and_image", None)
        if not callable(sender):
            raise ForegroundSendUnavailable(
                "The configured WeChat foreground driver cannot combine text and images"
            )
        sender(targets, text, image_path, guard)

    def send_quote(
        self,
        targets: Iterable[str],
        text: str,
        reference: ReplyReference,
        guard: Guard,
        image_path: str | None = None,
    ) -> bool:
        sender = getattr(self.fallback, "send_quote", None)
        if not callable(sender):
            raise ForegroundQuoteUnavailable(
                "The configured WeChat foreground driver cannot quote messages"
            )
        return bool(sender(targets, text, reference, guard, image_path=image_path))


@dataclass(slots=True, frozen=True)
class DesktopState:
    foreground_hwnd: int
    cursor: tuple[int, int]
    wechat_topmost: bool


class WindowsForegroundDesktop:
    """Small Win32 boundary used by the foreground fallback driver."""

    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x00000008
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    SW_RESTORE = 9

    def __init__(self, user32: Any | None = None) -> None:
        self.user32 = user32 or ctypes.windll.user32

    def capture(self, wechat_hwnd: int) -> DesktopState:
        point = wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise OSError("Unable to read cursor position")
        style = int(self.user32.GetWindowLongW(wechat_hwnd, self.GWL_EXSTYLE))
        return DesktopState(
            int(self.user32.GetForegroundWindow() or 0),
            (int(point.x), int(point.y)),
            bool(style & self.WS_EX_TOPMOST),
        )

    def activate(self, hwnd: int) -> None:
        self.user32.ShowWindow(hwnd, self.SW_RESTORE)
        if (
            int(self.user32.GetForegroundWindow() or 0) != hwnd
            and not self.user32.SetForegroundWindow(hwnd)
        ):
            raise ForegroundSendUnavailable("Unable to activate WeChat")

    def restore(
        self,
        state: DesktopState,
        wechat_hwnd: int,
        *,
        restore_user_state: bool,
    ) -> None:
        target = self.HWND_TOPMOST if state.wechat_topmost else self.HWND_NOTOPMOST
        self.user32.SetWindowPos(
            wechat_hwnd,
            target,
            0,
            0,
            0,
            0,
            self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE,
        )
        if not restore_user_state or not state.foreground_hwnd:
            return
        current = int(self.user32.GetForegroundWindow() or 0)
        if (
            current == wechat_hwnd
            and self.user32.IsWindow(state.foreground_hwnd)
        ):
            point = wintypes.POINT()
            if (
                self.user32.GetCursorPos(ctypes.byref(point))
                and (int(point.x), int(point.y)) != state.cursor
            ):
                self.user32.SetCursorPos(*state.cursor)
            self.user32.SetForegroundWindow(state.foreground_hwnd)


class ForegroundWeChatUiaDriver:
    """Authorized foreground fallback for WeChat 4.x text delivery."""

    def __init__(
        self,
        uia_factory: Callable[[], Any] | None = None,
        desktop: Any | None = None,
        sleep: Callable[[float], None] | None = None,
        window_message_sender: Callable[[int], None] | None = None,
        clipboard_file: Callable[[str], None] | None = None,
    ) -> None:
        self._uia_factory = uia_factory or self._default_uia_factory
        self.desktop = desktop or WindowsForegroundDesktop()
        self._sleep = sleep or time.sleep
        self._window_message_sender = window_message_sender or send_enter_to_window
        self._clipboard_file = clipboard_file or copy_file_to_clipboard

    @staticmethod
    def _default_uia_factory():
        from wechatauto.uia_driver import WeChatUIA

        return WeChatUIA(timeout=2.0, search_timeout=0.5)

    def send_text(self, targets: Iterable[str], text: str, guard: Guard) -> None:
        names = tuple(dict.fromkeys(name.strip() for name in targets if name.strip()))
        if not names:
            raise ForegroundTargetUnavailable("No resolvable WeChat target name")
        uia = self._uia_factory()
        win = self._materialized_main(uia)
        if win is None:
            raise ForegroundSendUnavailable("WeChat UIA tree is unavailable")
        uia._win = win
        hwnd = int(win.NativeWindowHandle)
        state = self.desktop.capture(hwnd)
        edit = None
        value = None
        action_triggered = False
        wrote_text = False
        try:
            self._check_guard(guard)
            try:
                self.desktop.activate(hwnd)
            except ForegroundSendUnavailable:
                try:
                    win.SetActive()
                except Exception as error:
                    raise ForegroundSendUnavailable(
                        "Unable to activate WeChat"
                    ) from error
            self._sleep(0.05)
            self._accept_synthetic_input(guard)
            self._open_exact_chat(uia, win, names, guard)
            edit = uia._chat_input(win)
            if edit is None:
                raise ForegroundSendUnavailable("WeChat chat input is unavailable")
            value = self._required_pattern(edit, "GetValuePattern", "chat input value")
            draft = str(getattr(value, "Value", "") or "")
            if draft and draft != text:
                raise ForegroundInputBusy("Target chat contains an unsent draft")
            self._check_guard(guard)
            if not draft:
                try:
                    edit.SetFocus()
                except Exception as error:
                    raise ForegroundSendUnavailable(
                        "WeChat chat input cannot receive programmatic focus"
                    ) from error
                value.SetValue(text)
                wrote_text = True
            self._sleep(0.1)
            if str(getattr(value, "Value", "") or "") != text:
                raise ForegroundSendUnavailable("WeChat did not accept the message text")
            self._check_guard(guard)
            action_triggered = True
            try:
                self._window_message_sender(hwnd)
            except Exception as error:
                raise ForegroundActionUnknown(
                    "Atomic foreground send result is unknown"
                ) from error
        finally:
            if (
                not action_triggered
                and wrote_text
                and value is not None
                and str(getattr(value, "Value", "") or "") == text
            ):
                try:
                    value.SetValue("")
                except Exception:
                    pass
            try:
                restore_user_state = action_triggered or bool(guard())
            except Exception:
                restore_user_state = action_triggered
            try:
                self.desktop.restore(
                    state,
                    hwnd,
                    restore_user_state=restore_user_state,
                )
            except Exception:
                logger.warning("微信前台备用发送未能完全恢复桌面状态", exc_info=True)

    def send_quote(
        self,
        targets: Iterable[str],
        text: str,
        reference: ReplyReference,
        guard: Guard,
        *,
        image_path: str | None = None,
    ) -> bool:
        names = tuple(dict.fromkeys(name.strip() for name in targets if name.strip()))
        if not names:
            raise ForegroundTargetUnavailable("No resolvable WeChat target name")
        if reference.occurrence_from_latest is None:
            raise ForegroundQuoteUnavailable("Quote occurrence was not resolved")
        image_file = Path(image_path) if image_path is not None else None
        if image_file is not None and not image_file.is_file():
            raise ForegroundQuoteUnavailable("Image file is no longer available")
        uia = self._uia_factory()
        win = self._materialized_main(uia)
        if win is None:
            raise ForegroundQuoteUnavailable("WeChat UIA tree is unavailable")
        uia._win = win
        hwnd = int(win.NativeWindowHandle)
        state = self.desktop.capture(hwnd)
        edit = None
        value = None
        quote_selected = False
        wrote_payload = False
        action_triggered = False
        quoted_message_keys: set[str] = set()
        foreground_verified = False
        try:
            self._check_guard(guard)
            self.desktop.activate(hwnd)
            self._sleep(0.05)
            self._accept_synthetic_input(guard)
            self._open_exact_chat(uia, win, names, guard)
            if self._composer_quote_banner(win) is not None:
                if not self._dismiss_quote_banner(win):
                    raise ForegroundActionUnknown(
                        "Existing WeChat quote draft could not be cleared safely"
                    )
                self._accept_synthetic_input(guard, allow_cursor_change=True)
            control = self._locate_quote_control(uia, win, reference, guard)
            self._select_quote(win, control)
            quote_selected = True
            self._accept_synthetic_input(guard, allow_cursor_change=True)
            uia, win = self._refresh_quote_surface(uia, win)
            if not self._wait_for_quote_banner(win, reference):
                raise ForegroundQuoteUnavailable("WeChat quote banner verification failed")
            edit = uia._chat_input(win)
            if edit is None:
                raise ForegroundQuoteUnavailable("WeChat chat input is unavailable")
            value = self._required_pattern(edit, "GetValuePattern", "chat input value")
            if str(getattr(value, "Value", "") or ""):
                raise ForegroundInputBusy("Target chat contains an unsent draft")
            if text:
                quoted_message_keys = self._quoted_text_keys(uia, win, text)
            self._check_guard(guard)
            edit.SetFocus()
            if text:
                value.SetValue(text)
                wrote_payload = True
            if image_file is not None:
                self._clipboard_file(str(image_file))
                edit.SendKeys("{Ctrl}v", waitTime=0.05)
                wrote_payload = True
            self._sleep(0.15)
            if text and text not in str(getattr(value, "Value", "") or ""):
                raise ForegroundActionUnknown("WeChat did not accept quoted reply text")
            self._check_guard(guard)
            action_triggered = True
            try:
                self._window_message_sender(hwnd)
            except Exception as error:
                raise ForegroundActionUnknown(
                    "Atomic quoted send result is unknown"
                ) from error
            if text:
                foreground_verified = self._wait_for_new_quoted_text(
                    uia,
                    win,
                    text,
                    quoted_message_keys,
                )
        except (ForegroundActionUnknown, ForegroundInputBusy, ForegroundUserInterrupted):
            raise
        except ForegroundQuoteUnavailable:
            if wrote_payload:
                raise ForegroundActionUnknown(
                    "Quoted payload was written before the operation failed"
                )
            raise
        except Exception as error:
            if wrote_payload or action_triggered:
                raise ForegroundActionUnknown(
                    "Quoted send result is unknown"
                ) from error
            raise ForegroundQuoteUnavailable(
                f"Unable to prepare native quote: {type(error).__name__}"
            ) from error
        finally:
            quote_cleanup_failed = False
            if not action_triggered:
                if value is not None and wrote_payload:
                    try:
                        value.SetValue("")
                    except Exception:
                        pass
                if quote_selected:
                    uia, win = self._refresh_quote_surface(uia, win)
                    quote_cleanup_failed = not self._dismiss_quote_banner(win)
            try:
                restore_user_state = action_triggered or bool(guard())
            except Exception:
                restore_user_state = action_triggered
            try:
                self.desktop.restore(
                    state,
                    hwnd,
                    restore_user_state=restore_user_state,
                )
            except Exception:
                logger.warning("微信引用发送未能完全恢复桌面状态", exc_info=True)
            if quote_cleanup_failed:
                raise ForegroundActionUnknown(
                    "WeChat quote draft could not be cleared safely"
                )
        return foreground_verified

    def _locate_quote_control(
        self,
        uia: Any,
        win: Any,
        reference: ReplyReference,
        guard: Guard,
        *,
        max_scrolls: int = 12,
        max_messages: int = QUOTE_HISTORY_LIMIT,
    ) -> Any:
        message_list = uia._message_list(win)
        if message_list is None:
            raise ForegroundQuoteUnavailable("WeChat message list is unavailable")
        self._scroll_to_latest(uia, message_list, guard)
        seen: set[str] = set()
        inspected_messages = 0
        stale_pages = 0
        matched = 0
        wanted = int(reference.occurrence_from_latest or 0)
        for scroll_index in range(max_scrolls + 1):
            children = list(message_list.GetChildren())
            page_messages = 0
            for control in reversed(children):
                try:
                    class_name = str(control.ClassName or "")
                    if not self._is_message_control(class_name):
                        continue
                    key = self._control_key(control)
                    if key in seen:
                        continue
                    seen.add(key)
                    page_messages += 1
                    inspected_messages += 1
                    if not self._control_matches_reference(control, reference):
                        if inspected_messages >= max_messages:
                            break
                        continue
                    if matched == wanted:
                        return control
                    matched += 1
                    if inspected_messages >= max_messages:
                        break
                except Exception:
                    continue
            stale_pages = stale_pages + 1 if page_messages == 0 else 0
            if inspected_messages >= max_messages:
                break
            if stale_pages >= 2:
                break
            if scroll_index == max_scrolls:
                break
            self._check_guard(guard)
            self._scroll_message_list(uia, message_list, 360, 1)
            self._accept_synthetic_input(guard, allow_cursor_change=True)
        raise ForegroundQuoteUnavailable(
            "Referenced WeChat message was not found in recent "
            f"{max_messages} messages: {reference.message_id}"
        )

    @staticmethod
    def _is_message_control(class_name: str) -> bool:
        return class_name.startswith("mmui::Chat") and class_name != "mmui::ChatItemView"

    def _scroll_to_latest(self, uia: Any, message_list: Any, guard: Guard) -> None:
        stable = 0
        previous = None
        for _ in range(6):
            children = list(message_list.GetChildren())
            current = self._control_key(children[-1]) if children else None
            stable = stable + 1 if current == previous else 0
            if stable >= 1:
                return
            previous = current
            self._check_guard(guard)
            self._scroll_message_list(uia, message_list, -480, 1)
            self._accept_synthetic_input(guard, allow_cursor_change=True)
        raise ForegroundQuoteUnavailable(
            "Unable to reach WeChat latest messages within safe scroll budget"
        )

    def _scroll_message_list(
        self, uia: Any, message_list: Any, delta: int, times: int
    ) -> None:
        rect = message_list.BoundingRectangle
        uia._set_cursor((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        for _ in range(times):
            uia._mouse_wheel(delta)
            self._sleep(0.08)

    @staticmethod
    def _control_key(control: Any) -> str:
        runtime_id = getattr(control, "runtimeid", None)
        if runtime_id:
            return str(runtime_id)
        rect = control.BoundingRectangle
        return f"{control.ClassName}|{control.Name}|{rect.left}|{rect.top}|{rect.right}|{rect.bottom}"

    @staticmethod
    def _control_matches_reference(control: Any, reference: ReplyReference) -> bool:
        name = str(getattr(control, "Name", "") or "").strip()
        class_name = str(getattr(control, "ClassName", "") or "")
        if reference.content_type == ContentType.TEXT:
            return name == reference.content.strip()
        if reference.content_type == ContentType.IMAGE:
            return (
                name == "图片"
                or name.startswith("[动画表情")
                or "Image" in class_name
                or "Emoji" in class_name
            )
        if reference.content_type == ContentType.VOICE:
            return "Voice" in class_name or name.startswith("语音")
        if reference.content_type == ContentType.VIDEO:
            return "Video" in class_name or name.startswith("视频")
        if reference.content_type == ContentType.FILE:
            return name.startswith("文件") or "File" in class_name
        return name == reference.content.strip()

    def _select_quote(self, win: Any, control: Any) -> None:
        parent = SimpleNamespace(root=SimpleNamespace(pid=int(win.ProcessId)))
        try:
            from wechatauto.ui.component import Menu

            self._scroll_control_into_view(control)
            # Ask UI Automation itself to show the element's context menu.
            # This is the only fully non-physical path supported by Windows.
            self._show_uia_context_menu(control)
            self._sleep(0.15)
            menu = Menu(parent, timeout=1)
            result = self._invoke_menu_item(menu, "引用")
            if not result:
                # Some Qt providers acknowledge ShowContextMenu without
                # creating a popup.  Try an HWND-targeted right click next; it
                # remains silent and does not move the user's cursor.
                self._click_control_via_window(win, control, button="right")
                self._sleep(0.15)
                menu = Menu(parent, timeout=1)
                result = self._invoke_menu_item(menu, "引用")
            if not result:
                # Keep the existing physical UIA action as a compatibility
                # fallback for WeChat builds that discard targeted right-click
                # messages while an interactive desktop is available.
                control.RightClick(x=102, y=30, ratioX=0, ratioY=0)
                menu = Menu(parent, timeout=3)
                result = self._invoke_menu_item(menu, "引用")
        except Exception as error:
            raise ForegroundQuoteUnavailable("Unable to open WeChat quote menu") from error
        if not result:
            raise ForegroundQuoteUnavailable("WeChat quote menu has no quote action")

    def _scroll_control_into_view(self, control: Any) -> None:
        """Materialize a virtualized chat item before opening its context menu."""
        try:
            pattern = control.GetScrollItemPattern()
            if pattern is not None:
                pattern.ScrollIntoView()
                self._sleep(0.2)
        except Exception:
            # Older WeChat providers expose no ScrollItem pattern. In that
            # case the control may already be visible and the normal context
            # menu paths below remain valid.
            return

    @staticmethod
    def _show_uia_context_menu(control: Any) -> bool:
        try:
            from comtypes.gen.UIAutomationClient import IUIAutomationElement3

            control.Element.QueryInterface(IUIAutomationElement3).ShowContextMenu()
            return True
        except Exception:
            return False

    @staticmethod
    def _invoke_menu_item(menu: Any, name: str) -> bool:
        if not getattr(menu, "control", None) or not menu.exists(0):
            return False
        for control in menu.option_controls:
            if str(getattr(control, "Name", "") or "") != name:
                continue
            try:
                control.Click()
                return True
            except Exception:
                pass
            try:
                invoke = control.GetInvokePattern()
                if invoke is not None:
                    invoke.Invoke()
                    return True
            except Exception:
                pass
            try:
                legacy = control.GetLegacyIAccessiblePattern()
                if legacy is not None:
                    legacy.DoDefaultAction()
                    return True
            except Exception:
                pass
            return False
        return False

    def _wait_for_quote_banner(
        self, win: Any, reference: ReplyReference
    ) -> bool:
        for attempt in range(11):
            # An existing quote draft is removed and verified immediately
            # before selecting this message.  WeChat may truncate the visible
            # referenced text or expose an empty ReferView name, so the newly
            # created composer banner itself is the reliable success signal.
            if self._composer_quote_banner(win) is not None:
                return True
            if attempt < 10:
                self._sleep(0.05)
        return False

    def _wait_for_new_quoted_text(
        self,
        uia: Any,
        win: Any,
        text: str,
        previous_keys: set[str],
    ) -> bool:
        for attempt in range(11):
            uia, win = self._refresh_quote_surface(uia, win)
            if self._quoted_text_keys(uia, win, text) - previous_keys:
                return True
            if attempt < 10:
                self._sleep(0.1)
        return False

    @classmethod
    def _quoted_text_keys(cls, uia: Any, win: Any, text: str) -> set[str]:
        try:
            message_list = uia._message_list(win)
            controls = (
                list(message_list.GetChildren())
                if message_list is not None
                else []
            )
        except Exception:
            return set()
        prefix = f"{text}\n"
        return {
            cls._control_key(control)
            for control in controls
            if str(getattr(control, "Name", "") or "").startswith(prefix)
            and "引用 " in str(getattr(control, "Name", "") or "")
        }

    def _refresh_quote_surface(self, uia: Any, win: Any) -> tuple[Any, Any]:
        for attempt in range(5):
            try:
                refreshed_uia = self._uia_factory()
                refreshed_win = self._materialized_main(refreshed_uia)
                if refreshed_win is not None:
                    refreshed_uia._win = refreshed_win
                    return refreshed_uia, refreshed_win
            except Exception:
                pass
            if attempt < 4:
                self._sleep(0.05)
        return uia, win

    @classmethod
    def _quote_banner_matches(cls, win: Any, reference: ReplyReference) -> bool:
        banner = cls._composer_quote_banner(win)
        if banner is None:
            return False
        name = " ".join(str(getattr(banner, "Name", "") or "").split())
        if reference.content_type == ContentType.TEXT:
            wanted = " ".join(reference.content.split())
            return bool(wanted) and wanted in name
        return bool(name.strip())

    def _dismiss_quote_banner(self, win: Any) -> bool:
        banner = self._composer_quote_banner(win)
        if banner is None:
            return True
        try:
            parent = banner.GetParentControl()
            button = parent.ButtonControl(Name="删除引用消息")
            if button.Exists(0.2, 0.1):
                try:
                    button.Click()
                except Exception:
                    invoke = button.GetInvokePattern()
                    if invoke is None:
                        return False
                    invoke.Invoke()
        except Exception:
            return False
        else:
            for attempt in range(11):
                if self._composer_quote_banner(win) is None:
                    return True
                if attempt < 10:
                    self._sleep(0.05)
        return False

    @classmethod
    def _composer_quote_banner(cls, win: Any) -> Any | None:
        composer = cls._find_descendant(win, "mmui::ComposeReferView")
        if composer is None:
            return None
        return cls._find_descendant(composer, "mmui::ReferView")

    @staticmethod
    def _find_descendant(root: Any, class_name: str) -> Any | None:
        queue = deque([(root, 0)])
        visited = 0
        while queue and visited < 2000:
            control, depth = queue.popleft()
            visited += 1
            try:
                if str(control.ClassName or "") == class_name:
                    return control
                if depth < 32:
                    queue.extend((child, depth + 1) for child in control.GetChildren())
            except Exception:
                continue
        return None

    def _open_exact_chat(
        self, uia: Any, win: Any, names: tuple[str, ...], guard: Guard
    ) -> None:
        if chat_matches(uia.current_chat(), names):
            return
        sessions = win.ListControl(AutomationId="session_list")
        if sessions.Exists(0.3, 0.1):
            exact = [
                item
                for item in sessions.GetChildren()
                if (item.Name or "").split("\n", 1)[0].strip() in names
            ]
            if len(exact) == 1:
                self._select(win, exact[0], guard)
                self._accept_synthetic_input(guard)
                self._sleep(0.2)
                if chat_matches(uia.current_chat(), names):
                    return

        search = uia._search_box(win)
        if search is None:
            raise ForegroundTargetUnavailable("WeChat search box is unavailable")
        search_value = self._required_pattern(search, "GetValuePattern", "search value")
        for name in names:
            self._check_guard(guard)
            search_value.SetValue(name)
            self._sleep(0.4)
            self._check_guard(guard)
            try:
                search.SetFocus()
                self._window_message_sender(int(win.NativeWindowHandle))
            except Exception as error:
                raise ForegroundTargetUnavailable(
                    "Unable to enter exact WeChat search result"
                ) from error
            self._accept_synthetic_input(guard)
            self._sleep(0.2)
            if chat_matches(uia.current_chat(), (name,)):
                return
        try:
            search_value.SetValue("")
        except Exception:
            pass
        raise ForegroundTargetUnavailable("No exact WeChat target could be opened")

    def _select(self, win: Any, control: Any, guard: Guard) -> None:
        """Select a visible session without relying on a broken Qt UIA pattern.

        WeChat 4.1.12.55 exposes SelectionItemPattern and InvokePattern on
        session cells, but both return successfully without changing chats.
        A targeted window message reaches the Qt view without moving the
        user's cursor or requiring a globally active input desktop.
        """
        self._check_guard(guard)
        self._click_control_via_window(win, control)

    @staticmethod
    def _click_control_via_window(
        win: Any,
        control: Any,
        *,
        button: str = "left",
    ) -> None:
        hwnd = int(getattr(win, "NativeWindowHandle", 0) or 0)
        if not hwnd:
            raise ForegroundSendUnavailable("WeChat window handle is unavailable")
        window_rect = win.BoundingRectangle
        control_rect = control.BoundingRectangle
        left = max(int(control_rect.left), int(window_rect.left))
        top = max(int(control_rect.top), int(window_rect.top))
        right = min(int(control_rect.right), int(window_rect.right))
        bottom = min(int(control_rect.bottom), int(window_rect.bottom))
        if right <= left or bottom <= top:
            raise ForegroundTargetUnavailable("WeChat target control is not visible")

        point = wintypes.POINT((left + right) // 2, (top + bottom - 1) // 2)
        user32 = ctypes.windll.user32
        if not user32.ScreenToClient(hwnd, ctypes.byref(point)):
            raise ForegroundSendUnavailable("Unable to map WeChat control coordinates")
        lparam = ((int(point.y) & 0xFFFF) << 16) | (int(point.x) & 0xFFFF)
        if button == "right":
            messages = ((0x0200, 0), (0x0204, 0x0002), (0x0205, 0))
        else:
            messages = ((0x0200, 0), (0x0201, 0x0001), (0x0202, 0))
        for message, wparam in messages:
            result = ctypes.c_size_t()
            delivered = user32.SendMessageTimeoutW(
                hwnd,
                message,
                wparam,
                lparam,
                0x0002,  # SMTO_ABORTIFHUNG
                1000,
                ctypes.byref(result),
            )
            if not delivered:
                raise ForegroundSendUnavailable(
                    "WeChat target control did not accept a window message"
                )

    @staticmethod
    def _materialized_main(uia: Any):
        win = uia._find_main()
        if win is not None:
            return win
        wake = getattr(uia, "_wake_accessibility", None)
        if not callable(wake):
            return None
        wake()
        return uia._find_main()

    @staticmethod
    def _required_pattern(control: Any, getter: str, label: str):
        try:
            pattern = getattr(control, getter)()
        except Exception as error:
            raise ForegroundSendUnavailable(f"Missing UIA {label} pattern") from error
        if pattern is None:
            raise ForegroundSendUnavailable(f"Missing UIA {label} pattern")
        return pattern

    @staticmethod
    def _check_guard(guard: Guard) -> None:
        if not guard():
            raise ForegroundUserInterrupted("User input resumed")

    @staticmethod
    def _accept_synthetic_input(
        guard: Guard, *, allow_cursor_change: bool = False
    ) -> None:
        accept = getattr(guard, "accept_synthetic_input", None)
        if callable(accept):
            try:
                accept(allow_cursor_change=allow_cursor_change)
            except TypeError:
                accept()
