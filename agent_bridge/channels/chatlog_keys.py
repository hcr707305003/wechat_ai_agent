"""Bounded, read-only WeChat 4.x key recovery in an isolated worker.

Adapted from anpingjing/wechat-chatlog-decrypt-export, commit
4ec68b706d510d90a52443dd6908fd7b79253f96 (scan.py and key_v4.py).
See assets/chatlog-LICENSE.txt. No debugger, injection or key files.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import hmac
import multiprocessing
import os
import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path

PROCESS_ACCESS = 0x0010 | 0x0400  # VM_READ | QUERY_INFORMATION, never ALL_ACCESS
CHUNK_BYTES = 4 * 1024**2
MAX_SCAN_BYTES = 1024**3
MAX_CANDIDATES = 128
MAX_POINTERS = 8192
MAX_PROCESSES = 4
SCAN_SECONDS = 90
WORKER_SECONDS = 120
_XOR_PATTERN = re.compile(
    rb"\x48\xBA(.{8}).{3,8}?\x48\xBA(.{8}).{3,8}?"
    rb"\x48\xBA(.{8}).{3,8}?\x48\xBA(.{8}).{3,8}?\x48\x85\xC0",
    re.DOTALL,
)
_KEY_RULE = """rule GetKeyAddrStub {
    strings:
        $a = { ?? ?? ?? ?? ?? ?? 00 00 00 00 00 00 00 00 00 00
               20 00 00 00 00 00 00 00 2f 00 00 00 00 00 00 00 }
    condition: all of them
}"""


class KeyRecoveryError(RuntimeError):
    """Safe diagnostic without keys, memory data or account paths."""


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError()


def verify_master_key(master: bytes, page: bytes) -> bool:
    if len(master) != 32 or len(page) != 4096:
        return False
    salt = page[:16]
    derived = hashlib.pbkdf2_hmac("sha512", master, salt, 256000, 32)
    mac_key = hashlib.pbkdf2_hmac(
        "sha512", derived, bytes(x ^ 0x3A for x in salt), 2, 32
    )
    actual = hmac.digest(mac_key, page[16:-64] + struct.pack("<I", 1), "sha512")
    return hmac.compare_digest(actual, page[-64:])


def xor_materials(data: bytes):
    for match in _XOR_PATTERN.finditer(data):
        yield b"".join(match.groups())


def static_xor_keys(dll: Path, deadline: float) -> list[bytes]:
    import pefile

    result: list[bytes] = []
    consumed = 0
    with pefile.PE(str(dll), fast_load=True) as pe, dll.open("rb") as source:
        for section in pe.sections:
            if not section.Characteristics & 0x20000000:
                continue
            for offset in range(0, section.SizeOfRawData, CHUNK_BYTES):
                _check_deadline(deadline)
                if consumed >= 256 * 1024**2:
                    raise LookupError("limit")
                source.seek(section.PointerToRawData + offset)
                data = source.read(
                    min(CHUNK_BYTES + 100, section.SizeOfRawData - offset)
                )
                consumed += len(data)
                for material in xor_materials(data):
                    if material not in result:
                        result.append(material)
                        if len(result) > 16:
                            raise LookupError("limit")
    return result


def main_process_ids(rows: list[dict]) -> list[int]:
    wechat = {
        row["pid"]: row
        for row in rows
        if (row.get("name") or "").lower() == "weixin.exe"
    }
    return sorted(pid for pid, row in wechat.items() if row.get("ppid") not in wechat)


@dataclass
class ScanBudget:
    deadline: float
    consumed: int = 0
    candidates: int = 0

    def check(self) -> None:
        _check_deadline(self.deadline)
        if self.consumed >= MAX_SCAN_BYTES or self.candidates >= MAX_CANDIDATES:
            raise LookupError("limit")


class _ReadOnlyMemory:
    def __init__(self, pid: int):
        from wechatauto.db import _MBI

        self.mbi_type = _MBI
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        self.kernel.OpenProcess.restype = ctypes.c_void_p
        self.kernel.ReadProcessMemory.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.kernel.ReadProcessMemory.restype = ctypes.c_int
        self.kernel.VirtualQueryEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_MBI),
            ctypes.c_size_t,
        ]
        self.kernel.VirtualQueryEx.restype = ctypes.c_size_t
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = self.kernel.OpenProcess(PROCESS_ACCESS, False, pid)
        if not self.handle:
            raise PermissionError()

    def close(self):
        self.kernel.CloseHandle(self.handle)

    def regions(self, budget: ScanBudget):
        address = 0
        while True:
            budget.check()
            info = self.mbi_type()
            if not self.kernel.VirtualQueryEx(
                self.handle, address, ctypes.byref(info), ctypes.sizeof(info)
            ):
                return
            base, size = info.BaseAddress or 0, info.RegionSize
            if not size or base + size <= address:
                return
            address = base + size
            if (
                info.State == 0x1000
                and info.Type == 0x20000
                and not info.Protect & 0x101
            ):
                yield base, size

    def read(self, address: int, length: int) -> bytes:
        buffer = ctypes.create_string_buffer(length)
        count = ctypes.c_size_t()
        ok = self.kernel.ReadProcessMemory(
            self.handle, address, buffer, length, ctypes.byref(count)
        )
        return buffer.raw[: count.value] if ok else b""


def scan_process(
    memory, rules, materials: list[bytes], page: bytes, budget: ScanBudget
) -> bytes | None:
    seen: set[int] = set()

    def read(address, length):
        budget.check()
        length = min(length, MAX_SCAN_BYTES - budget.consumed)
        budget.consumed += length
        return memory.read(address, length)

    for base, size in memory.regions(budget):
        for offset in range(0, size, CHUNK_BYTES - 64):
            data = read(base + offset, min(CHUNK_BYTES, size - offset))
            for match in rules.match(data=data, timeout=2):
                for string in match.strings:
                    for instance in string.instances:
                        budget.check()
                        pointer = int.from_bytes(
                            data[instance.offset : instance.offset + 8], "little"
                        )
                        if not pointer or pointer in seen:
                            continue
                        if len(seen) >= MAX_POINTERS:
                            raise LookupError("limit")
                        seen.add(pointer)
                        raw = read(pointer, 32)
                        if (
                            len(raw) != 32
                            or len(set(raw)) < 15
                            or sum(32 <= b <= 126 for b in raw) > 24
                        ):
                            continue
                        budget.candidates += 1
                        for material in materials:
                            _check_deadline(budget.deadline)
                            master = bytes(a ^ b for a, b in zip(raw, material))
                            if verify_master_key(master, page):
                                return master
    return None


def scan_master_key(account_dir: str) -> bytes:
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise OSError("platform")
    import psutil
    import yara
    from wechatauto.db import _find_weixin_module

    budget = ScanBudget(time.monotonic() + SCAN_SECONDS)
    with (Path(account_dir) / "db_storage" / "contact" / "contact.db").open(
        "rb"
    ) as source:
        page = source.read(4096)
    if len(page) != 4096:
        raise ValueError("page")
    rows = [p.info for p in psutil.process_iter(["pid", "ppid", "name"])]
    pids = main_process_ids(rows)
    if not pids or len(pids) > MAX_PROCESSES:
        raise LookupError("process")
    rules = yara.compile(source=_KEY_RULE)
    for pid in pids:
        budget.check()
        module = _find_weixin_module(pid)
        if not module:
            continue
        materials = static_xor_keys(Path(module[2]), budget.deadline)
        if not materials:
            continue
        with contextlib.closing(_ReadOnlyMemory(pid)) as memory:
            master = scan_process(memory, rules, materials, page, budget)
        if master is not None:
            return master
    raise LookupError("key")


def key_worker(connection, account_dir: str) -> None:
    try:
        # Third-party imports must not put diagnostics or secrets into parent logs.
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            master = scan_master_key(account_dir)
        connection.send_bytes(master)
    except Exception as error:  # noqa: BLE001 - sanitize errors at the process boundary
        kind = (
            "timeout"
            if isinstance(error, TimeoutError)
            else "dependency"
            if isinstance(error, ImportError)
            else "permission"
            if isinstance(error, PermissionError)
            else "lookup"
            if isinstance(error, LookupError)
            else "scan"
        )
        connection.send_bytes(("error:" + kind).encode("ascii"))
    finally:
        connection.close()


def recover_master_key(
    account_dir: str, *, worker=key_worker, timeout=WORKER_SECONDS
) -> bytes:
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(writer, account_dir), daemon=True)
    started = False
    try:
        process.start()
        started = True
        writer.close()
        if not reader.poll(timeout):
            raise KeyRecoveryError("微信密钥扫描超时，已停止扫描；未操作微信进程")
        try:
            result = reader.recv_bytes(128)
        except (EOFError, OSError):
            raise KeyRecoveryError("微信密钥扫描子进程异常退出") from None
        if len(result) == 32:
            return result
        kinds = {
            b"error:timeout": "timeout",
            b"error:dependency": "dependency",
            b"error:permission": "permission",
            b"error:lookup": "lookup",
        }
        kind = kinds.get(result, "scan")
        raise KeyRecoveryError(
            f"微信密钥提取失败（{kind}）；请检查微信登录、账号配置和读取依赖。不自动回退或重试。"
        )
    finally:
        reader.close()
        writer.close()
        if started:
            process.join(timeout=0.2)
            if process.is_alive():
                process.terminate()  # Only this worker; never a WeChat PID.
                process.join(timeout=2)
            if not process.is_alive():
                process.close()
