import sqlite3
from types import SimpleNamespace

from agent_bridge.channels.wechat import WeChatChannelAdapter, WeChatChannelSettings
from agent_bridge.models import SessionBindingConfig


def test_startup_searches_use_one_connection_and_preserve_ambiguity(tmp_path):
    path = tmp_path / "contact.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE contact (username, nick_name, remark, alias)")
        connection.executemany(
            "INSERT INTO contact VALUES (?, ?, ?, ?)",
            [
                ("wxid_a", "Alice", "", "alias"),
                ("wxid_b", "Duplicate", "", ""),
                ("wxid_c", "Duplicate", "", ""),
                ("room@chatroom", "Group", "", ""),
            ],
        )
    opened = []

    def open_db(rel):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        opened.append(conn)
        return conn

    def forbidden(*args):
        raise AssertionError("unbatched lookup")

    database = SimpleNamespace(
        _db_files=[("contact/contact.db", str(path), 1)],
        _open=open_db,
        get_sessions=lambda **kw: [],
        search_contact=forbidden,
    )
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(
            allowed_private_ids=("Alice", "Duplicate"),
            allowed_group_ids=("Group",),
            session_bindings=(
                SessionBindingConfig(
                    conversation_id="Alice", provider="codex", session_id="s"
                ),
            ),
        ),
        SimpleNamespace(),
    )
    adapter._db = database
    adapter._resolve_allowlisted_targets()
    assert adapter._resolved_private_ids == ("wxid_a", "Duplicate")
    assert adapter._resolved_group_ids == ("room@chatroom",)
    assert adapter._resolved_session_bindings[0].conversation_id == "wxid_a"
    assert len(opened) == 1
    assert adapter._startup_contact_matches == {}
    try:
        opened[0].execute("SELECT 1")
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("connection not closed")


def test_batch_unavailable_preserves_old_search_path():
    calls = []

    def search(value):
        calls.append(value)
        return [{"username": "wxid_test", "nick_name": value, "remark": ""}]

    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("Name",)), SimpleNamespace()
    )
    adapter._db = SimpleNamespace(get_sessions=lambda **kw: [], search_contact=search)
    adapter._resolve_allowlisted_targets()
    assert adapter._resolved_private_ids == ("wxid_test",)
    assert calls == ["Name"]
