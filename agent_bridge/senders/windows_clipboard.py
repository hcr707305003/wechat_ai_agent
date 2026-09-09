from __future__ import annotations

import ctypes
import time
from pathlib import Path
from typing import Any

from ctypes import wintypes


CF_HDROP = 15
GMEM_MOVEABLE = 0x0002


class DROPFILES(ctypes.Structure):
    _fields_ = (
        ("pFiles", wintypes.DWORD),
        ("pt", wintypes.POINT),
        ("fNC", wintypes.BOOL),
        ("fWide", wintypes.BOOL),
    )


def copy_file_to_clipboard(
    path: str | Path,
    *,
    user32: Any | None = None,
    kernel32: Any | None = None,
) -> None:
    """Place one local file on the Windows clipboard as CF_HDROP."""
    image = Path(path).resolve(strict=True)
    if not image.is_file():
        raise OSError("Clipboard image path is not a file")

    user32 = user32 or ctypes.windll.user32
    kernel32 = kernel32 or ctypes.windll.kernel32
    if hasattr(kernel32.GlobalAlloc, "restype"):
        kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
        kernel32.GlobalFree.restype = ctypes.c_void_p
    if hasattr(user32.SetClipboardData, "restype"):
        user32.OpenClipboard.argtypes = (wintypes.HWND,)
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = ()
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = (wintypes.UINT, ctypes.c_void_p)
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.argtypes = ()
        user32.CloseClipboard.restype = wintypes.BOOL
    encoded = (str(image) + "\0\0").encode("utf-16-le")
    header = DROPFILES()
    header.pFiles = ctypes.sizeof(DROPFILES)
    header.fWide = True
    total_size = ctypes.sizeof(DROPFILES) + len(encoded)

    memory = kernel32.GlobalAlloc(GMEM_MOVEABLE, total_size)
    if not memory:
        raise OSError("Unable to allocate clipboard memory")
    transferred = False
    clipboard_open = False
    try:
        pointer = kernel32.GlobalLock(memory)
        if not pointer:
            raise OSError("Unable to lock clipboard memory")
        address = int(pointer)
        try:
            ctypes.memmove(address, ctypes.byref(header), ctypes.sizeof(header))
            ctypes.memmove(address + ctypes.sizeof(header), encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(memory)

        for _attempt in range(10):
            if user32.OpenClipboard(None):
                clipboard_open = True
                break
            time.sleep(0.02)
        if not clipboard_open:
            raise OSError("Unable to open Windows clipboard")
        if not user32.EmptyClipboard():
            raise OSError("Unable to clear Windows clipboard")
        if not user32.SetClipboardData(CF_HDROP, memory):
            raise OSError("Unable to place image file on Windows clipboard")
        transferred = True
    finally:
        if clipboard_open:
            user32.CloseClipboard()
        if not transferred:
            kernel32.GlobalFree(memory)
