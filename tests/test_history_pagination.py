import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agent_bridge.companion.controller import CompanionController
from agent_bridge.companion.models import ConversationItem
from agent_bridge.models import ConversationType, UnifiedMessage
from agent_bridge.sessions.repository import SQLiteRepository


@pytest.mark.asyncio
async def test_native_page_and_media_lookup_are_bounded(monkeypatch):
    from agent_bridge.channels.wechat import WeChatChannelAdapter, WeChatChannelSettings

    calls = []

    class Database:
        def get_messages(self, conversation_id, *, limit, offset=0):
            calls.append((conversation_id, limit, offset))
            return []

        def get_message_row(self, conversation_id, local_id):
            calls.append((conversation_id, local_id))
            return {"local_id": local_id}

    channel = WeChatChannelAdapter(WeChatChannelSettings(), repository=object())
    channel._db = Database()
    monkeypatch.setattr(
        channel, "_normalize_history_rows", lambda conversation_id, rows: rows
    )
    await channel.load_history_page("friend", 20, 40)
    rows = await channel.load_history_messages(
        "friend", ("friend:1", "other:2", "friend:3")
    )
    assert calls == [("friend", 20, 40), ("friend", 1), ("friend", 3)]
    assert rows == [{"local_id": 1}, {"local_id": 3}]
    with pytest.raises(ValueError):
        await channel.load_history_page("friend", 500)


@pytest.mark.asyncio
async def test_failed_page_can_retry_without_skipping_messages(tmp_path):
    repo, _, item, base, controller = make_history(tmp_path, 0)
    fail = True
    calls = []

    async def page(conversation_id, limit, offset):
        calls.append(offset)
        if offset and fail:
            raise OSError("temporary read failure")
        return [replace(base, message_id=f"native-{offset + i}") for i in range(limit)]

    controller.history_page_loader = page
    controller.update_preferences(item, load_history=True)
    await controller.load_local_history_async(item)
    await controller.load_history(item)
    with pytest.raises(OSError):
        await controller.load_older_history(item)
    fail = False
    await controller.load_older_history(item)
    assert calls == [0, 20, 20]
    assert len(controller.timeline("friend")) == 40
    repo.close()


def make_history(tmp_path, count=65):
    repo = SQLiteRepository(tmp_path / "pages.db")
    session = repo.create_session("codex", str(tmp_path))
    item = ConversationItem(
        "wechat", "bot", "friend", ConversationType.PRIVATE, "Friend"
    )
    base = UnifiedMessage(
        channel="wechat",
        channel_account_id="bot",
        conversation_id="friend",
        conversation_type=ConversationType.PRIVATE,
        sender_id="friend",
        sender_name="Friend",
        message_id="0",
        content="0",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    repo.bind_channel(base, session.id)
    for index in range(count):
        repo.add_inbound_message(
            session.id, replace(base, message_id=str(index), content=str(index))
        )

    async def dispatch(message):
        raise AssertionError("History must not dispatch messages")

    async def load_history(conversation_id, limit):
        return []

    controller = CompanionController(repo, dispatch, load_history, "codex")
    return repo, session, item, base, controller


@pytest.mark.asyncio
async def test_keyset_pages_are_bounded_and_stable_during_new_inserts(
    tmp_path, monkeypatch
):
    repo, session, item, base, controller = make_history(tmp_path, 1005)

    def forbid_all(*args):
        raise AssertionError("Must not load all history")

    monkeypatch.setattr(repo, "session_messages", forbid_all)
    calls = []
    original = repo.session_message_page

    def page(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(len(result))
        return result

    monkeypatch.setattr(repo, "session_message_page", page)
    try:
        await controller.load_local_history_async(item)
        assert len(controller.timeline("friend")) == 20
        assert {int(e.content) for e in controller.timeline("friend")} == set(
            range(985, 1005)
        )
        repo.add_inbound_message(
            session.id, replace(base, message_id="new", content="new")
        )
        while controller.has_older_history(item):
            await controller.load_older_history(item)
        entries = controller.timeline("friend")
        assert len(entries) == 1005
        assert {int(e.content) for e in entries} == set(range(1005))
        assert len({e.entry_id for e in entries}) == 1005
        assert max(calls) == 21  # one extra row probes whether another page exists
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_clear_during_page_load_does_not_resurrect_messages(
    tmp_path, monkeypatch
):
    repo, _, item, _, controller = make_history(tmp_path)
    await controller.load_local_history_async(item)
    started = asyncio.Event()
    release = asyncio.Event()

    async def recover(*args):
        started.set()
        await release.wait()
        return {}

    monkeypatch.setattr(controller, "_recover_legacy_wechat_media", recover)
    task = asyncio.create_task(controller.load_older_history(item))
    await started.wait()
    controller.clear_local_messages(item)
    release.set()
    await task
    assert controller.timeline("friend") == ()
    assert not controller.has_older_history(item)
    repo.close()


@pytest.mark.asyncio
async def test_wechat_history_pages_fetch_only_twenty_and_respect_existing_cap(
    tmp_path,
):
    repo, _, item, base, controller = make_history(tmp_path, 0)
    calls = []

    async def page(conversation_id, limit, offset):
        calls.append((limit, offset))
        return [
            replace(
                base,
                message_id=f"native-{i}",
                content=str(i),
                created_at=base.created_at + timedelta(seconds=i),
            )
            for i in range(54 - offset, max(-1, 54 - offset - limit), -1)
        ]

    controller.history_page_loader = page
    controller.update_preferences(item, load_history=True, history_limit=45)
    await controller.load_local_history_async(item)
    await controller.load_history(item)
    await controller.load_older_history(item)
    await controller.load_older_history(item)
    assert calls == [(20, 0), (20, 20), (5, 40)]
    assert len(controller.timeline("friend")) == 45
    assert not controller.has_older_history(item)
    repo.close()


@pytest.mark.asyncio
async def test_page_media_recovery_uses_exact_message_ids(tmp_path):
    repo, session, item, base, controller = make_history(tmp_path, 0)
    for i in range(45):
        repo.add_inbound_message(
            session.id,
            replace(base, message_id=f"friend:{i}", content="<msg><img /></msg>"),
        )
    calls = []

    async def recover(conversation_id, ids):
        calls.append(ids)
        return []

    controller.history_message_loader = recover
    await controller.load_local_history_async(item)
    await controller.load_older_history(item)
    assert calls == [
        tuple(f"friend:{i}" for i in range(25, 45)),
        tuple(f"friend:{i}" for i in range(5, 25)),
    ]
    repo.close()
