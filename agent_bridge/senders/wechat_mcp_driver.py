from __future__ import annotations

import ctypes
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from agent_bridge.senders.foreground_driver import (
    ForegroundActionUnknown,
    ForegroundInputBusy,
    ForegroundSendUnavailable,
    ForegroundTargetUnavailable,
    ForegroundUserInterrupted,
    Guard,
    WindowsForegroundDesktop,
)
from agent_bridge.senders.windows_clipboard import copy_file_to_clipboard

# Operation sequence adapted from zhouzdx/WeChatMCP (MIT), commit a255617.
# https://github.com/zhouzdx/WeChatMCP


logger = logging.getLogger(__name__)


class WeChatMcpForegroundDriver:
    """In-process WeChatMCP-compatible Windows foreground sender."""

    def __init__(
        self,
        *,
        automation: Any | None = None,
        desktop: Any | None = None,
        sleep: Callable[[float], None] | None = None,
        current_chat: Callable[[], str] | None = None,
        clipboard_file: Callable[[str], None] | None = None,
    ) -> None:
        self._automation = automation
        self.desktop = desktop or WindowsForegroundDesktop()
        self._sleep = sleep or time.sleep
        self._current_chat = current_chat or self._default_current_chat
        self._clipboard_file = clipboard_file or copy_file_to_clipboard

    @property
    def automation(self) -> Any:
        if self._automation is None:
            import uiautomation

            self._automation = uiautomation
        return self._automation

    def send_text(self, targets: Iterable[str], text: str, guard: Guard) -> None:
        self._send_with_transient_retry(tuple(targets), guard, text=text)

    def send_image(self, targets: Iterable[str], image_path: str, guard: Guard) -> None:
        self._send_with_transient_retry(tuple(targets), guard, image_path=image_path)

    def send_text_and_image(
        self,
        targets: Iterable[str],
        text: str,
        image_path: str,
        guard: Guard,
    ) -> None:
        if not text.strip():
            raise ValueError("Combined WeChat payload text must not be empty")
        self._send_with_transient_retry(
            tuple(targets), guard, text=text, image_path=image_path
        )

    def _send_with_transient_retry(
        self,
        targets: tuple[str, ...],
        guard: Guard,
        *,
        text: str | None = None,
        image_path: str | None = None,
    ) -> None:
        initializer = getattr(self.automation, "UIAutomationInitializerInThread", None)
        if callable(initializer):
            with initializer():
                self._send_initialized(targets, guard, text=text, image_path=image_path)
            return
        self._send_initialized(targets, guard, text=text, image_path=image_path)

    def _send_initialized(
        self,
        targets: tuple[str, ...],
        guard: Guard,
        *,
        text: str | None = None,
        image_path: str | None = None,
    ) -> None:
        for attempt in range(2):
            try:
                self._send_payload(targets, guard, text=text, image_path=image_path)
                return
            except (ctypes.ArgumentError, ForegroundSendUnavailable) as error:
                if attempt or not self._caused_by_argument_error(error):
                    if isinstance(error, ctypes.ArgumentError):
                        raise ForegroundSendUnavailable(
                            "WeChatMCP foreground operation failed at "
                            "window_lookup: ArgumentError"
                        ) from error
                    raise
                logger.warning("WeChatMCP 前台控件瞬态失效，重新获取窗口后重试一次")
                self._sleep(0.15)

    @staticmethod
    def _caused_by_argument_error(error: BaseException) -> bool:
        current: BaseException | None = error
        while current is not None:
            if isinstance(current, ctypes.ArgumentError):
                return True
            current = current.__cause__
        return False

    def _send_payload(
        self,
        targets: Iterable[str],
        guard: Guard,
        *,
        text: str | None = None,
        image_path: str | None = None,
    ) -> None:
        if text is None and image_path is None:
            raise ValueError("At least one WeChat payload must be provided")
        names = tuple(dict.fromkeys(name.strip() for name in targets if name.strip()))
        if not names:
            raise ForegroundTargetUnavailable("No resolvable WeChat target name")
        win = self.automation.WindowControl(searchDepth=1, Name="微信")
        if not win.Exists(0):
            raise ForegroundSendUnavailable("WeChatMCP could not find WeChat window")

        hwnd = int(getattr(win, "NativeWindowHandle", 0) or 0)
        if not hwnd:
            raise ForegroundSendUnavailable("WeChatMCP window handle is unavailable")
        state = self.desktop.capture(hwnd)
        action_triggered = False
        started_at = time.monotonic()
        stage = "activate"
        try:
            self._check_guard(guard)
            self.desktop.activate(hwnd)
            self._sleep(0.2)
            stage = "open_search"
            self._send_window_key(win, "{Ctrl}F", guard)
            self._sleep(0.2)

            stage = "find_search"
            search = self._find_search_box(win)
            if search is None:
                raise ForegroundSendUnavailable("WeChatMCP search box not found")
            opened = False
            for name in names:
                stage = "search_target"
                self._check_guard(guard)
                search.Click()
                self._accept_synthetic_input(guard, allow_cursor_change=True)
                self._send_window_key(win, "{Ctrl}a", guard)
                self._send_window_key(win, "{Delete}", guard)
                self._send_window_key(win, name, guard)
                self._sleep(0.4)
                self._send_window_key(win, "{Enter}", guard)
                self._sleep(0.4)
                stage = "verify_target"
                current = self._safe_current_chat()
                if not current or current in names:
                    opened = True
                    break
            if not opened:
                raise ForegroundTargetUnavailable(
                    "WeChatMCP did not open an allowed target"
                )

            stage = "find_input"
            input_box = self._find_input_box(win)
            if input_box is None:
                draft = self._focus_rendered_input(win, guard)
            else:
                draft = self._control_value(input_box)
            if draft and (image_path is not None or draft != text):
                raise ForegroundInputBusy("Target chat contains an unsent draft")
            if input_box is not None:
                input_box.Click()
                self._accept_synthetic_input(guard, allow_cursor_change=True)
            if not draft:
                stage = "write_message"
                if text is not None:
                    # Always paste the final text instead of passing it to
                    # uiautomation.SendKeys directly.  SendKeys treats some
                    # punctuation (braces, percent signs, plus, etc.) as key
                    # syntax, which can silently change an Agent reply before
                    # it reaches WeChat.  Clipboard paste preserves the exact
                    # Unicode payload for both text-only and combined sends.
                    self._check_guard(guard)
                    previous_clipboard: str | None = None
                    if image_path is None:
                        try:
                            previous_clipboard = self.automation.GetClipboardText()
                        except Exception:
                            pass
                    self.automation.SetClipboardText(text)
                    self._send_window_key(win, "{Ctrl}v", guard)
                    if image_path is None and previous_clipboard is not None:
                        try:
                            self.automation.SetClipboardText(previous_clipboard)
                        except Exception:
                            pass
                if image_path is not None:
                    self._check_guard(guard)
                    self._clipboard_file(image_path)
                    self._send_window_key(win, "{Ctrl}v", guard)
                self._sleep(0.2)
            self._check_guard(guard)
            action_triggered = True
            stage = "send_message"
            try:
                self._send_keys(win, "{Enter}")
            except Exception as error:
                raise ForegroundActionUnknown(
                    "WeChatMCP send key result is unknown"
                ) from error
            stage = "post_send_guard"
            self._accept_synthetic_input(guard)
            self._sleep(0.3)
        except ForegroundUserInterrupted as error:
            if action_triggered:
                raise ForegroundActionUnknown(
                    "WeChatMCP send may have completed before user input resumed"
                ) from error
            raise
        except (
            ForegroundActionUnknown,
            ForegroundInputBusy,
            ForegroundSendUnavailable,
            ForegroundTargetUnavailable,
        ):
            raise
        except Exception as error:
            if action_triggered:
                raise ForegroundActionUnknown(
                    "WeChatMCP send result is unknown"
                ) from error
            raise ForegroundSendUnavailable(
                "WeChatMCP foreground operation failed at "
                f"{stage}: {type(error).__name__}"
            ) from error
        finally:
            elapsed = time.monotonic() - started_at
            if elapsed >= 5.0:
                logger.warning(
                    "WeChatMCP 前台发送耗时过长: stage=%s elapsed=%.1fs "
                    "action_triggered=%s",
                    stage,
                    elapsed,
                    action_triggered,
                )
            try:
                self.desktop.restore(
                    state,
                    hwnd,
                    restore_user_state=bool(guard()),
                )
            except Exception:
                pass

    def _find_search_box(self, win: Any) -> Any | None:
        # Keep this lookup shallow.  Calling Exists() forces UIAutomation to
        # traverse the whole WeChat tree and can make a wedged COM provider
        # block for tens of seconds.  The named top-level proxy is enough for
        # the normal layout; if it is stale, the first Click below fails fast
        # and the configured UIA fallback takes over.
        try:
            return win.EditControl(Name="搜索")
        except Exception as error:
            logger.debug("WeChatMCP search control lookup failed: %s", error)
        return None

    def _find_input_box(self, win: Any) -> Any | None:
        try:
            input_box = win.EditControl(Name="输入")
            # Reading the value pattern validates the direct proxy without a
            # full descendant search.  We still return None for WeChat 4's
            # custom-rendered editor, which uses the geometry fallback.
            input_box.GetValuePattern()
            return input_box
        except Exception:
            return None

    def _focus_rendered_input(self, win: Any, guard: Guard) -> str:
        """Focus WeChat 4's custom-rendered editor and read its draft safely."""
        rect = win.BoundingRectangle
        left = int(getattr(rect, "left", 0))
        right = int(getattr(rect, "right", 0))
        top = int(getattr(rect, "top", 0))
        bottom = int(getattr(rect, "bottom", 0))
        width = right - left
        height = bottom - top
        if width < 500 or height < 350:
            raise ForegroundSendUnavailable(
                "WeChatMCP rendered input geometry is unavailable"
            )
        x = int(left + width * 0.68)
        y = int(bottom - 90)
        self._check_guard(guard)
        self.automation.Click(x, y)
        self._accept_synthetic_input(guard, allow_cursor_change=True)
        self._sleep(0.2)

        try:
            original_clipboard = self.automation.GetClipboardText()
        except Exception as error:
            raise ForegroundSendUnavailable(
                "WeChatMCP could not inspect the rendered input draft"
            ) from error
        try:
            self.automation.SetClipboardText("")
            self._send_window_key(win, "{Ctrl}a", guard)
            self._send_window_key(win, "{Ctrl}c", guard)
            self._sleep(0.1)
            return str(self.automation.GetClipboardText() or "")
        finally:
            try:
                self.automation.SetClipboardText(original_clipboard)
            except Exception:
                pass

    @staticmethod
    def _control_value(control: Any) -> str:
        try:
            pattern = control.GetValuePattern()
            return str(getattr(pattern, "Value", "") or "")
        except Exception:
            return ""

    def _safe_current_chat(self) -> str:
        try:
            return str(self._current_chat() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _default_current_chat() -> str:
        from wechatauto.uia_driver import WeChatUIA

        return str(WeChatUIA(timeout=1.0, search_timeout=0.3).current_chat() or "")

    def _send_window_key(self, win: Any, keys: str, guard: Guard) -> None:
        self._check_guard(guard)
        self._send_keys(win, keys)
        self._accept_synthetic_input(guard)

    def _send_keys(self, win: Any, keys: str) -> None:
        """Type into the already focused WeChat control without refocusing it."""
        sender = getattr(self.automation, "SendKeys", None)
        if callable(sender):
            try:
                sender(keys, waitTime=0.05)
            except TypeError:
                sender(keys)
            return
        win.SendKeys(keys)

    @staticmethod
    def _check_guard(guard: Guard) -> None:
        if not guard():
            raise ForegroundUserInterrupted("User input resumed")

    @staticmethod
    def _accept_synthetic_input(
        guard: Guard, *, allow_cursor_change: bool = False
    ) -> None:
        accept = getattr(guard, "accept_synthetic_input", None)
        if not callable(accept):
            return
        try:
            accept(allow_cursor_change=allow_cursor_change)
        except TypeError:
            accept()
