from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_bridge.channels import wechat_database as backend


@pytest.fixture(autouse=True)
def isolated_dpapi_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local-app-data'))


def test_account_selection_is_exact_and_rejects_ambiguity(tmp_path):
    one = tmp_path / "wxid_test_1234"
    two = tmp_path / "wxid_other_5678"
    for p in (one, two):
        (p / "db_storage").mkdir(parents=True)
    assert backend.select_account([one, two], "wxid_test") == one
    assert backend.select_account([one, two], one.name) == one
    with pytest.raises(RuntimeError, match="唯一"):
        backend.select_account([one, two], None)
    with pytest.raises(RuntimeError):
        backend.select_account([one, two], "../wxid_test")
    three = tmp_path / "wxid_test_9999"
    (three / "db_storage").mkdir(parents=True)
    with pytest.raises(RuntimeError):
        backend.select_account([one, two, three], "wxid_test")


def test_factory_injects_new_key_without_persisting_it(tmp_path, monkeypatch):
    account = tmp_path / "wxid_test_1234"
    (account / "db_storage").mkdir(parents=True)
    calls = []

    class DB:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.unkeyed = []
            self._save_keys()

        def _save_keys(self):
            raise AssertionError("key persisted")

    module = SimpleNamespace(
        WeChatDB=DB,
        auto_detect_db_dir=lambda: str(tmp_path),
        _find_account_dirs=lambda _: [str(account)],
    )
    monkeypatch.setattr(backend, "_database_module", lambda: module)
    monkeypatch.setattr(backend, "recover_master_key", lambda path: bytes(range(32)))
    db = backend.open_wechat_database("wxid_test")
    assert isinstance(db, DB)
    assert calls == [
        {
            "db_dir": str(tmp_path),
            "account": account.name,
            "master_key": bytes(range(32)).hex(),
        }
    ]


def test_factory_propagates_recovery_failure_without_legacy_fallback(
    tmp_path, monkeypatch
):
    account = tmp_path / "wxid_test_1234"
    (account / "db_storage").mkdir(parents=True)

    def fail(_):
        raise RuntimeError("recovery failed")

    def old_db(**kwargs):
        raise AssertionError("legacy fallback invoked")

    monkeypatch.setattr(
        backend,
        "_database_module",
        lambda: SimpleNamespace(
            WeChatDB=old_db,
            auto_detect_db_dir=lambda: str(tmp_path),
            _find_account_dirs=lambda _: [str(account)],
        ),
    )
    monkeypatch.setattr(backend, "recover_master_key", fail)
    with pytest.raises(RuntimeError, match="recovery failed"):
        backend.open_wechat_database("wxid_test")


@pytest.mark.parametrize("missing", ["contact/contact.db", "message\\message_0.db"])
def test_factory_rejects_unreadable_required_databases(tmp_path, monkeypatch, missing):
    account = tmp_path / "wxid_test_1234"
    (account / "db_storage").mkdir(parents=True)

    class DB:
        def __init__(self, **kwargs):
            self.unkeyed = [missing]

    monkeypatch.setattr(
        backend,
        "_database_module",
        lambda: SimpleNamespace(
            WeChatDB=DB,
            auto_detect_db_dir=lambda: str(tmp_path),
            _find_account_dirs=lambda _: [str(account)],
        ),
    )
    monkeypatch.setattr(backend, "recover_master_key", lambda _: bytes(range(32)))
    with pytest.raises(RuntimeError, match="数据库"):
        backend.open_wechat_database("wxid_test")


def test_listener_keeps_initial_watermark_and_poll_deduplication():
    from wechatauto.db import Listener

    histories = {
        "friend": [{"sort_seq": 10, "local_id": 1}],
        "room@chatroom": [{"sort_seq": 20, "local_id": 2}],
    }

    class DB:
        def get_messages(self, user, limit):
            return histories[user][-limit:][::-1]

        def get_new_messages(self, user, since_seq):
            return [r for r in histories[user] if r["sort_seq"] > since_seq]

    listener = Listener(DB())
    received = []
    listener._dispatch = lambda user, rows: received.extend(
        (user, row["local_id"]) for row in rows
    )
    for user in histories:
        listener.add_listener(user, lambda *_: None)
    listener._poll_once()
    assert received == []
    histories["friend"].extend(
        [{"sort_seq": 11, "local_id": 3}, {"sort_seq": 12, "local_id": 4}]
    )
    histories["room@chatroom"].append({"sort_seq": 21, "local_id": 5})
    listener._poll_once()
    listener._poll_once()
    assert received == [("friend", 3), ("friend", 4), ("room@chatroom", 5)]
    assert listener.watermark == {"friend": 12, "room@chatroom": 21}


def test_real_parser_derives_keys_without_creating_plaintext_cache(
    tmp_path, monkeypatch
):
    import hashlib
    import hmac
    import struct

    from wechatauto import db as module

    account = tmp_path / "wxid_synthetic_1234"
    master = bytes(range(32))
    for rel in ("contact/contact.db", "message/message_0.db"):
        path = account / "db_storage" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        page = bytearray((i * 11) % 256 for i in range(4096))
        salt = bytes(page[:16])
        enc = hashlib.pbkdf2_hmac("sha512", master, salt, 256000, 32)
        mac = hashlib.pbkdf2_hmac("sha512", enc, bytes(x ^ 0x3A for x in salt), 2, 32)
        page[-64:] = hmac.digest(
            mac, bytes(page[16:-64]) + struct.pack("<I", 1), "sha512"
        )
        path.write_bytes(page)
    monkeypatch.setattr(module, "auto_detect_db_dir", lambda: str(tmp_path))
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path / "cache"))
    monkeypatch.setattr(backend, "recover_master_key", lambda _: master)

    def forbidden(*args):
        raise AssertionError("legacy extraction")

    monkeypatch.setattr(module.WeChatDB, "extract_keys", forbidden)
    monkeypatch.setattr(module.WeChatDB, "extract_master_key", forbidden)
    reader = backend.open_wechat_database(account.name)
    assert reader.unkeyed == []
    assert len(reader._keys) == 2
    assert not Path(reader.keys_file).exists()


def test_production_channel_uses_new_factory_and_existing_wrapper(monkeypatch):
    from wechatauto import db

    from agent_bridge.channels import wechat
    from agent_bridge.channels.wechat import (
        WeChatChannelAdapter,
        WeChatChannelSettings,
        _SerializedWeChatDb,
    )

    calls = []
    raw = SimpleNamespace(
        get_self_info=lambda: {"username": "bot", "nick_name": "Test"}
    )

    def factory(account):
        calls.append(account)
        return raw

    monkeypatch.setattr(backend, "open_wechat_database", factory)
    monkeypatch.setattr(
        wechat, "build_wechat_sender", lambda *args, **kw: SimpleNamespace()
    )
    monkeypatch.setattr(
        db, "Listener", lambda database, **kw: SimpleNamespace(db=database, **kw)
    )
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(account="test"),
        SimpleNamespace(get_channel_state=lambda *args: {"friend": 7}),
    )
    monkeypatch.setattr(adapter, "_load_self_identities", lambda: None)
    monkeypatch.setattr(adapter, "_resolve_allowlisted_targets", lambda: None)
    monkeypatch.setattr(adapter, "_register_allowlisted_listeners", lambda: None)
    adapter._start_blocking()
    assert calls == ["test"]
    assert isinstance(adapter._db, _SerializedWeChatDb)
    assert adapter._listener.db is adapter._db
    assert adapter._listener.watermark == {"friend": 7}
