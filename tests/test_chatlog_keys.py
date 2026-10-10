import hashlib
import hmac
import struct
import time

import pytest

from agent_bridge.channels import chatlog_keys as keys


def make_page(master):
    page = bytearray((i * 7) % 256 for i in range(4096))
    salt = bytes(page[:16])
    enc = hashlib.pbkdf2_hmac("sha512", master, salt, 256000, 32)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc, bytes(x ^ 0x3A for x in salt), 2, 32)
    page[-64:] = hmac.digest(
        mac_key, bytes(page[16:-64]) + struct.pack("<I", 1), "sha512"
    )
    return bytes(page)


def test_master_key_is_bound_to_selected_database_page():
    master = bytes(range(32))
    page = make_page(master)
    assert keys.verify_master_key(master, page)
    assert not keys.verify_master_key(bytes(reversed(master)), page)
    assert not keys.verify_master_key(master, page[:-1])
    assert not keys.verify_master_key(master[:31], page)


def test_static_signature_collects_four_immediates():
    expected = bytes(range(32))
    signature = b"".join(
        b"\x48\xba" + expected[i : i + 8] + b"\x90" * 4 for i in range(0, 32, 8)
    )
    signature += b"\x48\x85\xc0"
    assert list(keys.xor_materials(b"padding" + signature)) == [expected]
    assert list(keys.xor_materials(signature[:-1])) == []


def test_static_scan_handles_signature_across_chunks(tmp_path, monkeypatch):
    from types import SimpleNamespace

    expected = bytes(range(32))
    signature = (
        b"".join(
            b"\x48\xba" + expected[i : i + 8] + b"\x90" * 4 for i in range(0, 32, 8)
        )
        + b"\x48\x85\xc0"
    )
    dll = tmp_path / "mock.dll"
    dll.write_bytes(b"x" * 60 + signature)

    class PE:
        sections = (
            SimpleNamespace(
                Characteristics=0x20000000,
                PointerToRawData=0,
                SizeOfRawData=dll.stat().st_size,
            ),
        )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(keys, "CHUNK_BYTES", 64)
    import pefile

    monkeypatch.setattr(pefile, "PE", lambda *a, **kw: PE())
    assert keys.static_xor_keys(dll, time.monotonic() + 5) == [expected]


def test_scan_constants_never_request_write_or_debug_access():
    assert keys.PROCESS_ACCESS == 0x410
    assert keys.SCAN_SECONDS < keys.WORKER_SECONDS
    assert keys.CHUNK_BYTES == 4 * 1024**2
    assert keys.MAX_SCAN_BYTES == 1024**3
    assert keys.MAX_CANDIDATES == 128


def test_process_selection_skips_wechat_children():
    rows = [
        {"pid": 1, "ppid": 100, "name": "Weixin.exe"},
        {"pid": 2, "ppid": 1, "name": "Weixin.exe"},
        {"pid": 3, "ppid": 100, "name": "Other.exe"},
    ]
    assert keys.main_process_ids(rows) == [1]


def _ok_worker(connection, account):
    connection.send_bytes(bytes(range(32)))
    connection.close()


def _blocked_worker(connection, account):
    time.sleep(20)


def _failed_worker(connection, account):
    connection.send_bytes(b"error:lookup")
    connection.close()


def test_supervisor_returns_key_via_pipe():
    assert keys.recover_master_key("unused", worker=_ok_worker, timeout=5) == bytes(
        range(32)
    )


def test_supervisor_times_out_and_reaps_only_own_child():
    import multiprocessing

    before = {p.pid for p in multiprocessing.active_children()}
    with pytest.raises(keys.KeyRecoveryError, match="超时"):
        keys.recover_master_key("unused", worker=_blocked_worker, timeout=0.2)
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_supervisor_reports_sanitized_error():
    with pytest.raises(keys.KeyRecoveryError, match="lookup"):
        keys.recover_master_key("unused", worker=_failed_worker, timeout=5)


def test_worker_does_not_expose_exception_secrets(monkeypatch):
    class Pipe:
        result = None

        def send_bytes(self, value):
            self.result = value

        def close(self):
            pass

    pipe = Pipe()

    def fail(_):
        raise ValueError("PRIVATE_KEY_AND_ACCOUNT")

    monkeypatch.setattr(keys, "scan_master_key", fail)
    keys.key_worker(pipe, "unused")
    assert b"PRIVATE" not in pipe.result
    assert pipe.result.startswith(b"error:")


def test_real_yara_scan_verifies_candidate_and_deduplicates_pointers():
    import yara

    raw = bytes(range(32))
    material = bytes([0x55] * 32)
    master = bytes(x ^ y for x, y in zip(raw, material))
    page = make_page(master)
    pointer = 0x123456
    stub = struct.pack("<QQQQ", pointer, 0, 32, 47)

    class Memory:
        def __init__(self):
            self.reads = []

        def regions(self, budget):
            yield 0x1000, len(stub) * 2

        def read(self, address, length):
            self.reads.append((address, length))
            return raw if address == pointer else stub * 2

    memory = Memory()
    budget = keys.ScanBudget(time.monotonic() + 5)
    assert (
        keys.scan_process(
            memory, yara.compile(source=keys._KEY_RULE), [material], page, budget
        )
        == master
    )
    assert budget.candidates == 1
    # Wrong material: repeated pointer is still verified only once.
    budget = keys.ScanBudget(time.monotonic() + 5)
    assert (
        keys.scan_process(
            memory, yara.compile(source=keys._KEY_RULE), [bytes(32)], page, budget
        )
        is None
    )
    assert budget.candidates == 1


def test_memory_byte_budget_caps_reads(monkeypatch):
    import yara

    monkeypatch.setattr(keys, "MAX_SCAN_BYTES", 128)

    class Memory:
        def __init__(self):
            self.sizes = []

        def regions(self, budget):
            yield 0x1000, 10000

        def read(self, address, length):
            self.sizes.append(length)
            return bytes(length)

    memory = Memory()
    budget = keys.ScanBudget(time.monotonic() + 5)
    keys.scan_process(
        memory, yara.compile(source=keys._KEY_RULE), [], bytes(4096), budget
    )
    assert sum(memory.sizes) == 128


def test_candidate_budget_stops_verification(monkeypatch):
    import yara

    monkeypatch.setattr(keys, "MAX_CANDIDATES", 1)
    calls = []
    monkeypatch.setattr(
        keys, "verify_master_key", lambda *args: calls.append(args) or False
    )
    stubs = b"".join(struct.pack("<QQQQ", ptr, 0, 32, 47) for ptr in (0x2000, 0x3000))

    class Memory:
        def regions(self, budget):
            yield 0x1000, len(stubs)

        def read(self, address, length):
            return stubs if address == 0x1000 else bytes(range(32))

    with pytest.raises(LookupError):
        keys.scan_process(
            Memory(),
            yara.compile(source=keys._KEY_RULE),
            [bytes(32)],
            bytes(4096),
            keys.ScanBudget(time.monotonic() + 5),
        )
    assert len(calls) == 1


def test_expired_budget_does_not_read_memory():
    import yara

    class Memory:
        def regions(self, budget):
            yield 0x1000, 4096

        def read(self, *args):
            raise AssertionError("read after deadline")

    with pytest.raises(TimeoutError):
        keys.scan_process(
            Memory(),
            yara.compile(source=keys._KEY_RULE),
            [],
            bytes(4096),
            keys.ScanBudget(0),
        )
