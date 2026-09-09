from __future__ import annotations

import ctypes
from dataclasses import dataclass
from ctypes import wintypes


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


@dataclass(slots=True, frozen=True)
class ActivitySnapshot:
    last_input_tick: int
    foreground_hwnd: int
    cursor: tuple[int, int]
    desktop_available: bool


class WindowsActivityMonitor:
    """Read user-input state without creating synthetic input."""

    def __init__(self, user32=None, kernel32=None) -> None:
        self._user32 = user32 or ctypes.windll.user32
        self._kernel32 = kernel32 or ctypes.windll.kernel32

    def snapshot(self) -> ActivitySnapshot:
        info = _LastInputInfo(ctypes.sizeof(_LastInputInfo), 0)
        if not self._user32.GetLastInputInfo(ctypes.byref(info)):
            raise OSError("GetLastInputInfo failed")
        point = _Point()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            raise OSError("GetCursorPos failed")
        foreground = int(self._user32.GetForegroundWindow() or 0)
        return ActivitySnapshot(
            last_input_tick=int(info.dwTime),
            foreground_hwnd=foreground,
            cursor=(int(point.x), int(point.y)),
            desktop_available=foreground != 0,
        )

    def idle_seconds(self, snapshot: ActivitySnapshot | None = None) -> float:
        state = snapshot or self.snapshot()
        tick = int(self._kernel32.GetTickCount()) & 0xFFFFFFFF
        elapsed = (tick - state.last_input_tick) & 0xFFFFFFFF
        return elapsed / 1000.0

    def is_idle(self, seconds: float) -> bool:
        state = self.snapshot()
        return state.desktop_available and self.idle_seconds(state) >= seconds

    def unchanged(self, expected: ActivitySnapshot) -> bool:
        current = self.snapshot()
        return (
            current.desktop_available
            and current.last_input_tick == expected.last_input_tick
            and current.foreground_hwnd == expected.foreground_hwnd
            and current.cursor == expected.cursor
        )
