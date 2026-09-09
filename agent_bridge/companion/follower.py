from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from agent_bridge.channels.wechat import WeChatCompanionSettings


@dataclass(slots=True, frozen=True)
class WindowRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(slots=True, frozen=True)
class WindowGeometry:
    width: int
    height: int
    x: int
    y: int

    def as_tk_geometry(self) -> str:
        x_sign = "+" if self.x >= 0 else ""
        y_sign = "+" if self.y >= 0 else ""
        return f"{self.width}x{self.height}{x_sign}{self.x}{y_sign}{self.y}"


@dataclass(slots=True, frozen=True)
class WindowSnapshot:
    available: bool
    visible: bool = False
    minimized: bool = False
    rect: WindowRect | None = None
    client_rect: WindowRect | None = None


def calculate_companion_geometry(
    wechat: WindowRect, settings: WeChatCompanionSettings
) -> WindowGeometry:
    # In docked mode the companion keeps the configured dimensions.  Previously
    # left/right docking always used the full WeChat window height, which made
    # ``companion.height`` appear to have no effect whenever the window followed
    # WeChat.
    docked_height = max(1, min(int(settings.height), wechat.height))
    if settings.side == "left":
        return WindowGeometry(
            settings.width,
            docked_height,
            wechat.left - settings.width,
            wechat.top,
        )
    if settings.side == "right":
        return WindowGeometry(
            settings.width,
            docked_height,
            wechat.right,
            wechat.top,
        )
    if settings.side == "top":
        return WindowGeometry(
            wechat.width,
            settings.height,
            wechat.left,
            wechat.top - settings.height,
        )
    return WindowGeometry(
        wechat.width,
        settings.height,
        wechat.left,
        wechat.bottom,
    )


def calculate_launcher_geometry(wechat_client: WindowRect) -> WindowGeometry:
    return WindowGeometry(
        width=44,
        height=44,
        x=wechat_client.left + 12,
        y=wechat_client.top + 12,
    )


class Win32WindowProbe:
    def __init__(self, user32: Any | None = None) -> None:
        self._user32 = user32

    def snapshot(self, hwnd: int | None) -> WindowSnapshot:
        if not hwnd:
            return WindowSnapshot(False)
        user32 = self._load_user32()
        if user32 is None:
            return WindowSnapshot(False)
        handle = wintypes.HWND(hwnd)
        if not user32.IsWindow(handle):
            return WindowSnapshot(False)
        rect = wintypes.RECT()
        if not user32.GetWindowRect(handle, ctypes.byref(rect)):
            return WindowSnapshot(False)
        return WindowSnapshot(
            available=True,
            visible=bool(user32.IsWindowVisible(handle)),
            minimized=bool(user32.IsIconic(handle)),
            rect=WindowRect(rect.left, rect.top, rect.right, rect.bottom),
            client_rect=self._read_client_rect(user32, handle),
        )

    def _load_user32(self) -> Any | None:
        if self._user32 is not None:
            return self._user32
        if os.name != "nt":
            return None
        self._user32 = ctypes.windll.user32
        return self._user32

    @staticmethod
    def _read_client_rect(user32: Any, handle: wintypes.HWND) -> WindowRect | None:
        try:
            rect = wintypes.RECT()
            origin = wintypes.POINT()
            if not user32.GetClientRect(handle, ctypes.byref(rect)):
                return None
            if not user32.ClientToScreen(handle, ctypes.byref(origin)):
                return None
            return WindowRect(
                origin.x,
                origin.y,
                origin.x + rect.right - rect.left,
                origin.y + rect.bottom - rect.top,
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return None


class Win32WindowOwner:
    _GWLP_HWNDPARENT = -8
    _GA_ROOT = 2
    # Binding is called from the follow timer (normally 60 times per second).
    # Reading GWLP_HWNDPARENT on every tick is surprisingly expensive on
    # multi-monitor desktops, so only revalidate an unchanged pair
    # periodically.  A changed owner/window pair is still rebound immediately.
    _OWNER_VALIDATE_INTERVAL = 0.5

    def __init__(self, user32: Any | None = None) -> None:
        self._user32 = user32
        self._bound_pair: tuple[int, int] | None = None
        self._last_owner_validation = 0.0

    def bind(self, window_handle: int, owner_handle: int) -> bool:
        if not window_handle or not owner_handle:
            return False
        try:
            user32 = self._load_user32()
            if user32 is None:
                return False
            window = self._top_level_handle(user32, window_handle)
            owner = self._top_level_handle(user32, owner_handle)
            pair = (window, owner)
            if self._bound_pair == pair:
                now = time.monotonic()
                if now - self._last_owner_validation < self._OWNER_VALIDATE_INTERVAL:
                    return True
                actual_owner = self._handle_value(
                    user32.GetWindowLongPtrW(
                        wintypes.HWND(window), self._GWLP_HWNDPARENT
                    )
                )
                self._last_owner_validation = now
                if actual_owner == owner:
                    return True
            if window == owner:
                return False
            if not user32.IsWindow(wintypes.HWND(window)):
                return False
            if not user32.IsWindow(wintypes.HWND(owner)):
                return False
            user32.SetWindowLongPtrW(
                wintypes.HWND(window),
                self._GWLP_HWNDPARENT,
                owner,
            )
            actual_owner = self._handle_value(
                user32.GetWindowLongPtrW(
                    wintypes.HWND(window), self._GWLP_HWNDPARENT
                )
            )
            if actual_owner != owner:
                return False
            self._bound_pair = pair
            self._last_owner_validation = time.monotonic()
            return True
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def _load_user32(self) -> Any | None:
        if self._user32 is not None:
            return self._user32
        if os.name != "nt":
            return None
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.SetWindowLongPtrW.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_ssize_t,
        ]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        self._user32 = user32
        return user32

    @classmethod
    def _top_level_handle(cls, user32: Any, window_handle: int) -> int:
        top_level = user32.GetAncestor(
            wintypes.HWND(window_handle), cls._GA_ROOT
        )
        return cls._handle_value(top_level) or window_handle

    @staticmethod
    def _handle_value(handle: Any) -> int:
        return int(getattr(handle, "value", handle) or 0)


class Win32WindowMover:
    def move(self, window_handle: int, geometry: WindowGeometry) -> bool:
        if os.name != "nt" or not window_handle:
            return False
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        handle = wintypes.HWND(window_handle)
        top_level = user32.GetAncestor(handle, 2) or window_handle
        flags = 0x0004 | 0x0010 | 0x0200  # NOZORDER | NOACTIVATE | NOOWNERZORDER
        return bool(
            user32.SetWindowPos(
                wintypes.HWND(top_level),
                wintypes.HWND(0),
                geometry.x,
                geometry.y,
                geometry.width,
                geometry.height,
                flags,
            )
        )
