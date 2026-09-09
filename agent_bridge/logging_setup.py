from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO


def open_redirected_log(path: Path) -> BinaryIO:
    """Open an append-only stream whose inherited Windows handle survives truncation."""
    if os.name != "nt":
        return path.open("ab", buffering=0)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    # FILE_APPEND_DATA without FILE_WRITE_DATA makes the kernel append every
    # inherited stdout write, rather than relying on the parent's CRT O_APPEND.
    # Share read/write/delete so clearing and log rotation remain possible.
    handle = create_file(str(path), 0x0004, 0x0007, None, 4, 0x0080, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except BaseException:
        close_handle(handle)
        raise
    try:
        return os.fdopen(descriptor, "ab", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


def configure_file_logging(
    path: str | Path,
    *,
    logger_name: str | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    log_path = Path(path).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    resolved = str(log_path)
    for handler in logger.handlers:
        if (
            isinstance(handler, RotatingFileHandler)
            and handler.baseFilename == resolved
        ):
            return logger
    handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] [%(levelname)s] %(message)s")
    )
    logger.addHandler(handler)
    return logger
