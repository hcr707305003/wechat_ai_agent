from __future__ import annotations

import ctypes
from pathlib import Path

from agent_bridge.senders.windows_clipboard import (
    CF_HDROP,
    DROPFILES,
    copy_file_to_clipboard,
)


class FakeKernel32:
    def __init__(self) -> None:
        self.buffer = None
        self.freed = False

    def GlobalAlloc(self, _flags, size):
        self.buffer = ctypes.create_string_buffer(size)
        return ctypes.addressof(self.buffer)

    def GlobalLock(self, memory):
        return memory

    def GlobalUnlock(self, _memory):
        return True

    def GlobalFree(self, _memory):
        self.freed = True


class FakeUser32:
    def __init__(self) -> None:
        self.format = None
        self.memory = None
        self.closed = False

    def OpenClipboard(self, _owner):
        return True

    def EmptyClipboard(self):
        return True

    def SetClipboardData(self, clipboard_format, memory):
        self.format = clipboard_format
        self.memory = memory
        return memory

    def CloseClipboard(self):
        self.closed = True
        return True


def test_copy_file_to_clipboard_builds_unicode_hdrop(tmp_path: Path) -> None:
    image = tmp_path / "结果.png"
    image.write_bytes(b"image")
    user32 = FakeUser32()
    kernel32 = FakeKernel32()

    copy_file_to_clipboard(image, user32=user32, kernel32=kernel32)

    assert user32.format == CF_HDROP
    assert user32.closed is True
    assert kernel32.freed is False
    assert kernel32.buffer is not None
    raw = bytes(kernel32.buffer)
    header = DROPFILES.from_buffer_copy(raw[: ctypes.sizeof(DROPFILES)])
    assert bool(header.fWide) is True
    payload = raw[header.pFiles :].decode("utf-16-le").rstrip("\0")
    assert payload == str(image.resolve())
