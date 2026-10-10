import hashlib
import hmac
import os
import struct

import pytest

from agent_bridge.channels import wechat_key_cache as cache


def make_account(tmp_path, master, name="account"):
    account = tmp_path / name
    contact = account / "db_storage/contact/contact.db"
    contact.parent.mkdir(parents=True)
    page = bytearray((i * 7) % 256 for i in range(4096))
    salt = bytes(page[:16])
    enc = hashlib.pbkdf2_hmac("sha512", master, salt, 256000, 32)
    mac = hashlib.pbkdf2_hmac("sha512", enc, bytes(x ^ 0x3A for x in salt), 2, 32)
    page[-64:] = hmac.digest(mac, bytes(page[16:-64]) + struct.pack("<I", 1), "sha512")
    contact.write_bytes(page)
    return account


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_dpapi_roundtrip_is_encrypted_and_account_bound(tmp_path):
    master = bytes(range(32))
    account = make_account(tmp_path, master)
    cache.save_master_key(account, master)
    path, _ = cache.cache_location(account)
    encrypted = path.read_bytes()
    assert master not in encrypted
    assert master.hex().encode() not in encrypted
    assert cache.load_master_key(account) == master
    other = make_account(tmp_path, master, "other")
    other_path, _ = cache.cache_location(other)
    other_path.write_bytes(encrypted)
    assert cache.load_master_key(other) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_rotated_or_corrupted_cache_is_a_miss(tmp_path):
    account = make_account(tmp_path, bytes(range(32)))
    cache.save_master_key(account, bytes(reversed(range(32))))
    assert cache.load_master_key(account) is None
    path, _ = cache.cache_location(account)
    path.write_bytes(b"corrupt")
    assert cache.load_master_key(account) is None
    path.write_bytes(b"x" * (cache.MAX_CACHE_BYTES + 1))
    assert cache.load_master_key(account) is None


def test_protection_failure_never_writes_plaintext(tmp_path, monkeypatch, caplog):
    account = make_account(tmp_path, bytes(range(32)))

    def fail(*args):
        raise RuntimeError("secret-contents")

    monkeypatch.setattr(cache, "_protect", fail)
    cache.save_master_key(account, bytes(range(32)))
    path, _ = cache.cache_location(account)
    assert not path.exists()
    assert "secret-contents" not in caplog.text


def test_valid_cache_skips_scanning(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_bridge.channels import wechat_database as backend

    master = bytes(range(32))
    account = make_account(tmp_path, master)

    class DB:
        def __init__(self, **kwargs):
            self.unkeyed = []

    monkeypatch.setattr(
        backend,
        "_database_module",
        lambda: SimpleNamespace(
            WeChatDB=DB,
            auto_detect_db_dir=lambda: str(tmp_path),
            _find_account_dirs=lambda _: [account],
        ),
    )
    monkeypatch.setattr(backend, "load_master_key", lambda _: master)

    def forbidden(*args):
        raise AssertionError("warm start scanned memory")

    monkeypatch.setattr(backend, "recover_master_key", forbidden)
    backend.open_wechat_database(account.name)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_cold_warm_and_rotated_startup_scan_counts(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_bridge.channels import wechat_database as backend

    master = bytes(range(32))
    account = make_account(tmp_path, master)
    calls = []

    class DB:
        def __init__(self, **kwargs):
            self.unkeyed = []

    monkeypatch.setattr(
        backend,
        "_database_module",
        lambda: SimpleNamespace(
            WeChatDB=DB,
            auto_detect_db_dir=lambda: str(tmp_path),
            _find_account_dirs=lambda _: [account],
        ),
    )

    def scan(_):
        calls.append(True)
        return master

    monkeypatch.setattr(backend, "recover_master_key", scan)
    backend.open_wechat_database(account.name)
    backend.open_wechat_database(account.name)
    assert len(calls) == 1
    master = bytes(reversed(range(32)))
    rotated = make_account(tmp_path, master, "rotated")
    (account / "db_storage/contact/contact.db").write_bytes(
        (rotated / "db_storage/contact/contact.db").read_bytes()
    )
    backend.open_wechat_database(account.name)
    assert len(calls) == 2
