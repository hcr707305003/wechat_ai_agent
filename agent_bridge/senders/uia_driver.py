from __future__ import annotations

import ctypes
import logging
import time
from collections.abc import Callable, Iterable
from ctypes import wintypes
from typing import Any

logger = logging.getLogger(__name__)


class SilentUiaUnavailable(RuntimeError):
    pass


class SilentUiaTargetUnavailable(SilentUiaUnavailable):
    pass


class UserActivityInterrupted(RuntimeError):
    pass


class WeChatInputBusy(RuntimeError):
    pass


Guard = Callable[[], bool]

_CHAT_INPUT_PLACEHOLDER = "输入文字，或按住Ctrl+Win使用语音输入"


def normalize_chat_name(value: Any) -> str:
    name = str(value or "").strip()
    if name.endswith(_CHAT_INPUT_PLACEHOLDER):
        name = name[: -len(_CHAT_INPUT_PLACEHOLDER)].rstrip()
    return name


def chat_matches(value: Any, names: Iterable[str]) -> bool:
    return normalize_chat_name(value) in names


def send_enter_to_window(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = wintypes.LPARAM
    for message, lparam in (
        (0x0100, 0x001C0001),  # WM_KEYDOWN, Return
        (0x0102, 0x00000001),  # WM_CHAR, Return
        (0x0101, 0xC01C0001),  # WM_KEYUP, Return
    ):
        user32.SendMessageW(hwnd, message, 0x0D, lparam)


class SilentWeChatUiaDriver:
    """WeChat text sender restricted to non-physical UIA patterns."""

    def __init__(
        self,
        uia_factory: Callable[[], Any] | None = None,
        sleep=None,
        window_message_sender: Callable[[int], None] | None = None,
        foreground_getter: Callable[[], int] | None = None,
    ) -> None:
        self._uia_factory = uia_factory or self._default_uia_factory
        self._sleep = sleep or time.sleep
        self._window_message_sender = (
            window_message_sender or send_enter_to_window
        )
        self._foreground_getter = foreground_getter or self._get_foreground_window
        self._pinned_main_handle: int | None = None
        self._pinned_main_identity: tuple[int, str] | None = None
        self._window_identity_reader = self._native_window_identity

    @staticmethod
    def _default_uia_factory():
        from wechatauto.uia_driver import WeChatUIA

        return WeChatUIA(timeout=2.0, search_timeout=0.5)

    def send_text(
        self, targets: Iterable[str], text: str, guard: Guard
    ) -> None:
        names = tuple(dict.fromkeys(name.strip() for name in targets if name.strip()))
        if not names:
            raise SilentUiaTargetUnavailable("No resolvable WeChat target name")
        uia = self._uia_factory()
        win = self._materialized_main(uia)
        if win is None:
            raise SilentUiaUnavailable("WeChat UIA tree is unavailable")
        uia._win = win
        hwnd = int(getattr(win, "NativeWindowHandle", 0) or 0)
        if not hwnd:
            raise SilentUiaUnavailable("WeChat main window has no native handle")
        foreground = int(self._foreground_getter() or 0)
        if foreground not in (0, hwnd):
            raise SilentUiaUnavailable(
                "Another application is active; atomic foreground delivery is required"
            )

        current = uia.current_chat()
        if not chat_matches(current, names):
            raise SilentUiaTargetUnavailable(
                "The current WeChat conversation is not the delivery target"
            )

        edit = uia._chat_input(win)
        if edit is None:
            raise SilentUiaUnavailable("WeChat chat input has no UIA control")
        value = self._required_pattern(edit, "GetValuePattern", "chat input value")
        if str(getattr(value, "Value", "") or ""):
            raise WeChatInputBusy("Target chat contains an unsent draft")
        self._check_guard(guard)
        try:
            edit.SetFocus()
        except Exception as error:
            raise SilentUiaUnavailable(
                "WeChat chat input cannot receive programmatic focus"
            ) from error
        value.SetValue(text)

        send_button = win.ButtonControl(Name="发送")
        if not send_button.Exists(0.5, 0.1):
            raise SilentUiaUnavailable("WeChat send button has no UIA control")
        invoke = self._required_pattern(send_button, "GetInvokePattern", "send invoke")
        self._check_guard(guard)
        invoke.Invoke()
        self._sleep(0.2)

        remaining = str(getattr(value, "Value", "") or "")
        if not remaining:
            return
        if remaining != text:
            raise WeChatInputBusy("Target chat draft changed during delivery")

        # WeChat 4.1.12.55 exposes a valid UIA InvokePattern but its provider
        # does not execute the button action while the desktop is backgrounded.
        # The Qt top-level HWND still owns keyboard focus, so a targeted Enter
        # window message consumes the UIA-created draft without global input,
        # cursor movement, clipboard access, or foreground activation.
        self._check_guard(guard)
        self._window_message_sender(hwnd)

    @staticmethod
    def _get_foreground_window() -> int:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)

    def main_window_handle(self) -> int | None:
        try:
            # Image viewers can share the main window's title/native Qt class.
            # Keep a confirmed main HWND instead of selecting the largest shell.
            if self._pinned_main_handle is not None:
                identity = self._window_identity_reader(self._pinned_main_handle)
                if identity is not None and identity == self._pinned_main_identity:
                    return self._pinned_main_handle
            self._pinned_main_handle = None
            self._pinned_main_identity = None
            uia = self._uia_factory()
            handle = 0
            source = "UIA"
            try:
                win = uia._find_main()
                if win is not None:
                    handle = int(win.NativeWindowHandle or 0)
            except Exception as error:  # noqa: BLE001 - third-party UIA failure must permit native discovery
                logger.debug("微信跟随 UIA 识别不可用: %s", type(error).__name__)
            if not handle:
                # Cold-start WeChat may expose only native Qt/render windows.
                # _wechat_hwnds verifies the owning Weixin process, but its area
                # ordering is NOT a reliable indication of window purpose.
                matches = [h for h in uia._wechat_hwnds() if self._is_native_main_shell(h)]
                if len(matches) == 1:
                    handle = int(matches[0])
                    source = "native"
            identity = self._window_identity_reader(handle) if handle else None
            if identity is not None:
                self._pinned_main_handle = handle
                self._pinned_main_identity = identity
                logger.info("微信跟随已绑定主窗口: hwnd=%s source=%s", handle, source)
                return handle
            return None
        except Exception:
            return None

    @staticmethod
    def _is_native_main_shell(handle: int) -> bool:
        import win32con
        import win32gui

        try:
            native_class = win32gui.GetClassName(handle)
            if not (native_class.startswith("Qt") and native_class.endswith("QWindowIcon")):
                return False
            style = win32gui.GetWindowLong(handle, win32con.GWL_STYLE)
            frame = (win32con.WS_CAPTION | win32con.WS_THICKFRAME
                     | win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX)
            # WeChat 4.x main shell uses custom caption controls (no system
            # menu). Preview/browser shells must not qualify just by title/size.
            if style & frame != frame or style & (win32con.WS_CHILD | win32con.WS_SYSMENU):
                return False
            if win32gui.GetWindow(handle, win32con.GW_OWNER):
                return False
            if win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
                return False
            render_children = []
            win32gui.EnumChildWindows(
                handle,
                lambda child, _: render_children.append(child)
                if win32gui.GetClassName(child).startswith("MMUIRenderSubWindow") else None,
                None,
            )
            return bool(render_children)
        except (OSError, win32gui.error):
            return False

    @staticmethod
    def _native_window_identity(handle: int) -> tuple[int, str] | None:
        import win32gui
        import win32process

        if not win32gui.IsWindow(handle):
            return None
        _thread_id, process_id = win32process.GetWindowThreadProcessId(handle)
        return (process_id, win32gui.GetClassName(handle)) if process_id else None

    def current_draft(self, targets: Iterable[str]) -> str | None:
        names = tuple(dict.fromkeys(name.strip() for name in targets if name.strip()))
        try:
            uia = self._uia_factory()
            win = self._materialized_main(uia)
            if win is None:
                return None
            uia._win = win
            if not chat_matches(uia.current_chat(), names):
                return None
            edit = uia._chat_input(win)
            if edit is None:
                return None
            value = self._required_pattern(edit, "GetValuePattern", "chat input value")
            return str(getattr(value, "Value", "") or "")
        except Exception:
            return None

    def clear_current_draft(self, targets: Iterable[str], expected: str) -> bool:
        names = tuple(dict.fromkeys(name.strip() for name in targets if name.strip()))
        try:
            uia = self._uia_factory()
            win = self._materialized_main(uia)
            if win is None:
                return False
            uia._win = win
            if not chat_matches(uia.current_chat(), names):
                return False
            edit = uia._chat_input(win)
            if edit is None:
                return False
            value = self._required_pattern(edit, "GetValuePattern", "chat input value")
            if str(getattr(value, "Value", "") or "") != expected:
                return False
            value.SetValue("")
            return True
        except Exception:
            return False

    def probe_capabilities(self) -> tuple[bool, str]:
        try:
            uia = self._uia_factory()
            win = self._materialized_main(uia)
            if win is None:
                return False, "WeChat UIA tree not found"
            search = uia._search_box(win)
            edit = uia._chat_input(win)
            send_button = win.ButtonControl(Name="发送")
            if search is None or edit is None or not send_button.Exists(0.5, 0.1):
                return False, "required WeChat UIA controls not found"
            self._required_pattern(search, "GetValuePattern", "search value")
            self._required_pattern(edit, "GetValuePattern", "chat input value")
            self._required_pattern(send_button, "GetInvokePattern", "send invoke")
            if not int(getattr(win, "NativeWindowHandle", 0) or 0):
                return False, "WeChat main window has no native handle"
            return True, "SetValue + Invoke + targeted Enter"
        except Exception as error:
            return False, f"{type(error).__name__}: {error}"

    @staticmethod
    def _materialized_main(uia: Any):
        win = uia._find_main()
        if win is not None:
            return win
        wake_accessibility = getattr(uia, "_wake_accessibility", None)
        if not callable(wake_accessibility):
            return None
        try:
            wake_accessibility()
        except Exception as error:
            raise SilentUiaUnavailable(
                "Unable to activate WeChat UIA tree"
            ) from error
        return uia._find_main()

    @staticmethod
    def _required_pattern(control: Any, getter: str, label: str):
        try:
            pattern = getattr(control, getter)()
        except Exception as error:
            raise SilentUiaUnavailable(f"Missing UIA {label} pattern") from error
        if pattern is None:
            raise SilentUiaUnavailable(f"Missing UIA {label} pattern")
        return pattern

    @staticmethod
    def _check_guard(guard: Guard) -> None:
        if not guard():
            raise UserActivityInterrupted("User input or foreground window changed")
