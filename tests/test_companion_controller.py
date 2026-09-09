from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_bridge.companion.controller import CompanionController
from agent_bridge.companion.models import ConversationItem, TimelineEntry
from agent_bridge.models import (
    AgentProgressUpdate,
    Attachment,
    ChannelTarget,
    ContentType,
    ConversationType,
    EventType,
    OutboundDeliveryStatus,
    ReplyReference,
    UnifiedEvent,
    UnifiedMessage,
)
from agent_bridge.senders.wechat import SenderUpdate
from agent_bridge.sessions.repository import SQLiteRepository


def message(
    message_id: str,
    conversation_id: str = "friend",
    *,
    is_self: bool = False,
    bridge_outbound: bool = False,
) -> UnifiedMessage:
    return UnifiedMessage(
        channel="wechat",
        channel_account_id="bot",
        conversation_id=conversation_id,
        conversation_type=ConversationType.PRIVATE,
        sender_id="bot" if is_self else "friend",
        sender_name="Me" if is_self else "Friend",
        message_id=message_id,
        content="hello",
        metadata={"is_self": is_self, "bridge_outbound": bridge_outbound},
    )


def item(conversation_id: str = "friend") -> ConversationItem:
    return ConversationItem(
        "wechat",
        "bot",
        conversation_id,
        ConversationType.PRIVATE,
        conversation_id,
    )


@pytest.mark.asyncio
async def test_default_off_observes_without_dispatching(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    dispatched = []

    async def dispatch(incoming: UnifiedMessage) -> None:
        dispatched.append(incoming)

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    await controller.handle(message("m1"))

    assert dispatched == []
    assert [entry.content for entry in controller.timeline("friend")] == ["hello"]
    repository.close()


@pytest.mark.asyncio
async def test_untriggered_group_image_is_forwarded_only_to_context_observer(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    dispatched = []
    observed = []

    async def dispatch(incoming: UnifiedMessage) -> None:
        dispatched.append(incoming)

    async def observe(incoming: UnifiedMessage) -> None:
        observed.append(incoming)

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(
        repository,
        dispatch,
        load_history,
        "codex",
        context_observer=observe,
    )
    group = ConversationItem(
        "wechat", "bot", "room@chatroom", ConversationType.GROUP, "群聊"
    )
    controller.update_preferences(group, reply_enabled=True)
    incoming = replace(
        message("group-image", "room@chatroom"),
        conversation_type=ConversationType.GROUP,
        content_type=ContentType.IMAGE,
        metadata={"agent_triggered": False},
    )

    await controller.handle(incoming)

    assert observed == [incoming]
    assert dispatched == []
    repository.close()


def test_timeline_merges_sources_chronologically_and_deduplicates_messages(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    t1 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 2, 10, 1, tzinfo=timezone.utc)
    t3 = datetime(2026, 9, 2, 10, 2, tzinfo=timezone.utc)
    controller._local_history["friend"] = [
        TimelineEntry(
            "friend", "甲", "第一条", "inbound", t1, True, "local", source_key="wechat:m1"
        )
    ]
    controller._wechat_history["friend"] = [
        TimelineEntry(
            "friend", "甲", "第一条", "inbound", t1, True, "wechat", source_key="wechat:m1"
        ),
        TimelineEntry(
            "friend", "乙", "第二条", "inbound", t2, True, "wechat", source_key="wechat:m2"
        ),
    ]
    controller._realtime["friend"] = [
        TimelineEntry("friend", "我", "第三条", "outbound", t3)
    ]

    entries = controller.timeline("friend")

    assert [entry.content for entry in entries] == ["第一条", "第二条", "第三条"]
    assert [entry.sender_name for entry in entries] == ["甲", "乙", "我"]
    assert [entry.created_at for entry in entries] == [t1, t2, t3]
    repository.close()


@pytest.mark.asyncio
async def test_reply_setting_is_isolated_per_conversation(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    dispatched = []

    async def dispatch(incoming: UnifiedMessage) -> None:
        dispatched.append(incoming.message_id)

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.update_preferences(item("friend"), reply_enabled=True)

    await controller.handle(message("m1", "friend"))
    await controller.handle(message("m2", "other"))

    assert dispatched == ["m1"]
    repository.close()


@pytest.mark.asyncio
async def test_history_is_display_only(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    dispatched = []
    history_calls = []

    async def dispatch(incoming: UnifiedMessage) -> None:
        dispatched.append(incoming)

    async def load_history(conversation_id: str, limit: int):
        history_calls.append((conversation_id, limit))
        return [message("history", conversation_id)]

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.update_preferences(item(), load_history=True, history_limit=25)
    await controller.load_history(item())

    assert history_calls == [("friend", 25)]
    assert dispatched == []
    assert controller.timeline("friend")[0].historical is True
    repository.close()


def test_local_database_history_loads_when_wechat_history_is_off(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    session = repository.create_session("codex", str(tmp_path))
    inbound = message("stored")
    repository.bind_channel(inbound, session.id)
    repository.add_inbound_message(session.id, inbound)
    repository.add_event(
        session.id,
        UnifiedEvent(EventType.ASSISTANT_MESSAGE, "stored reply", "codex"),
    )

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        raise AssertionError("微信历史开关关闭时不应读取微信")

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.load_local_history(item())

    entries = controller.timeline("friend")
    assert [entry.content for entry in entries] == ["hello", "stored reply"]
    assert [entry.direction for entry in entries] == ["inbound", "outbound"]
    assert {entry.history_source for entry in entries} == {"local"}
    assert controller.preferences(item()).load_history is False
    repository.close()


def test_local_history_uses_prefixed_display_content_without_changing_context(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    session = repository.create_session("codex", str(tmp_path))
    repository.bind_channel(message("binding"), session.id)
    repository.add_event(
        session.id,
        UnifiedEvent(
            EventType.ASSISTANT_MESSAGE,
            "raw reply",
            "codex",
            metadata={"bridge_display_content": "[ai回复]raw reply"},
        ),
    )

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.load_local_history(item())

    assert controller.timeline("friend")[-1].content == "[ai回复]raw reply"
    assert repository.recent_messages(session.id)[-1]["content"] == "raw reply"
    repository.close()


def test_local_database_history_restores_image_attachments(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    session = repository.create_session("codex", str(tmp_path))
    inbound = message("stored")
    repository.bind_channel(inbound, session.id)
    image = Attachment("image", "result.png", str(tmp_path / "result.png"))
    repository.add_event(
        session.id,
        UnifiedEvent(
            EventType.ASSISTANT_MESSAGE,
            "stored reply",
            "codex",
            metadata={
                "bridge_attachments": [
                    {
                        "kind": image.kind,
                        "name": image.name,
                        "path": image.path,
                        "metadata": image.metadata,
                    }
                ]
            },
        ),
    )

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.load_local_history(item())

    entries = controller.timeline("friend")
    assert entries[-1].attachments == (image,)
    repository.close()


def test_local_history_hides_legacy_image_xml_when_attachment_is_missing(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    session = repository.create_session("codex", str(tmp_path))
    incoming = message("image-1")
    incoming = replace(
        incoming,
        content=(
            "<?xml version=\"1.0\"?>"
            "<msg><img aeskey=\"key\" md5=\"id\" /></msg>"
        ),
        content_type=ContentType.IMAGE,
        metadata={**incoming.metadata, "raw_type": "图片"},
    )
    repository.bind_channel(message("binding"), session.id)
    repository.add_inbound_message(session.id, incoming)

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.load_local_history(item())

    entry = controller.timeline("friend")[0]
    assert entry.content == "[图片]"
    assert "<img" not in entry.content
    repository.close()


@pytest.mark.asyncio
async def test_async_local_history_recovers_legacy_image_from_wechat_cache(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    session = repository.create_session("codex", str(tmp_path))
    incoming = replace(
        message("friend:23139"),
        content="<msg><img aeskey=\"key\" md5=\"id\" /></msg>",
        content_type=ContentType.IMAGE,
        metadata={"raw_type": "图片", "sender_name": "Friend"},
    )
    repository.bind_channel(message("binding"), session.id)
    repository.add_inbound_message(session.id, incoming)
    image_path = tmp_path / "restored.png"
    image_path.write_bytes(b"image")

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(conversation_id: str, limit: int):
        assert (conversation_id, limit) == ("friend", 50)
        return [
            replace(
                incoming,
                content="[图片]",
                attachments=(Attachment("image", path=str(image_path)),),
            )
        ]

    controller = CompanionController(repository, dispatch, load_history, "codex")
    await controller.load_local_history_async(item())

    entry = controller.timeline("friend")[0]
    assert entry.content == "[图片]"
    assert entry.attachments[0].path == str(image_path)
    repository.close()


def test_local_history_recovers_sent_image_delivery_after_restart(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    session = repository.create_session("codex", str(tmp_path))
    inbound = message("stored")
    repository.bind_channel(inbound, session.id)
    image = Attachment("image", "old-screenshot.png", str(tmp_path / "old-screenshot.png"))
    delivery = repository.enqueue_outbound_delivery(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        "爸爸，给你截图：",
        "legacy-image-send",
        300,
        content_type=ContentType.IMAGE,
        attachments=(image,),
    )
    repository.update_outbound_delivery(
        delivery.id,
        OutboundDeliveryStatus.SENT,
        completed_at=delivery.created_at,
    )

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.load_local_history(item())

    entries = controller.timeline("friend")
    assert entries[-1].content == "爸爸，给你截图："
    assert entries[-1].attachments == (image,)
    assert len(repository.session_messages(session.id)) == 1

    # A second load (or a new controller after restart) must not duplicate it.
    controller2 = CompanionController(repository, dispatch, load_history, "codex")
    controller2.load_local_history(item())
    assert len(controller2.timeline("friend")) == 1
    assert len(repository.session_messages(session.id)) == 1
    repository.close()


@pytest.mark.asyncio
async def test_combined_delivery_echoes_do_not_create_duplicate_bubbles(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    image = tmp_path / "result.png"
    image.write_bytes(b"image")
    attachment = Attachment("image", "result.png", str(image))
    delivery = repository.enqueue_outbound_delivery(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        "爸爸，给你截图：",
        "combined-send",
        300,
        content_type=ContentType.IMAGE,
        attachments=(attachment,),
    )

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.handle_sender_update(SenderUpdate("queued", delivery))
    sent = repository.update_outbound_delivery(
        delivery.id, OutboundDeliveryStatus.SENT, completed_at=delivery.created_at
    )
    controller.handle_sender_update(SenderUpdate("sent", sent))

    text_echo = replace(
        message("echo-text", is_self=True),
        content=delivery.text,
        content_type=ContentType.TEXT,
        metadata={"is_self": True},
    )
    image_echo = replace(
        message("echo-image", is_self=True),
        content="[图片]",
        content_type=ContentType.IMAGE,
        metadata={"is_self": True},
    )
    await controller.handle(text_echo)
    await controller.handle(image_echo)

    entries = controller.timeline("friend")
    assert len(entries) == 1
    assert entries[0].content == delivery.text
    assert entries[0].attachments == (attachment,)
    repository.close()


@pytest.mark.asyncio
async def test_clear_local_messages_keeps_loaded_wechat_history(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    session = repository.create_session("codex", str(tmp_path))
    inbound = message("stored")
    repository.bind_channel(inbound, session.id)
    repository.add_inbound_message(session.id, inbound)

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(conversation_id: str, _limit: int):
        return [message("wechat-history", conversation_id)]

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.load_local_history(item())
    controller.update_preferences(item(), load_history=True)
    await controller.load_history(item())
    await controller.handle(message("live"))

    removed = controller.clear_local_messages(item())

    assert removed == 1
    entries = controller.timeline("friend")
    assert [entry.content for entry in entries] == ["hello"]
    assert [entry.history_source for entry in entries] == ["wechat"]
    assert repository.session_messages(session.id) == []
    assert repository.find_session_for_message(inbound) is not None
    repository.close()


@pytest.mark.asyncio
async def test_filehelper_ignores_bridge_outbound_but_accepts_mobile_message(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    dispatched = []

    async def dispatch(incoming: UnifiedMessage) -> None:
        dispatched.append(incoming.message_id)

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.update_preferences(item("filehelper"), reply_enabled=True)

    await controller.handle(message("mobile", "filehelper", is_self=True))
    await controller.handle(
        message("bridge", "filehelper", is_self=True, bridge_outbound=True)
    )

    assert dispatched == ["mobile"]
    repository.close()


def test_sender_updates_create_and_update_pending_timeline_entry(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    delivery = repository.enqueue_outbound_delivery(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        "agent reply",
        "job:reply",
        600,
    )

    controller.handle_sender_update(SenderUpdate("queued", delivery))
    controller.handle_sender_update(SenderUpdate("sent", delivery))

    entries = controller.timeline("friend")
    assert len(entries) == 1
    assert entries[0].content == "agent reply"
    assert entries[0].delivery_status == "sent"
    repository.close()


@pytest.mark.asyncio
async def test_image_echo_confirms_preview_card_without_duplicate(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    image = Attachment("image", "result.png", str(tmp_path / "result.png"))
    delivery = repository.enqueue_outbound_delivery(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        "[图片] 结果图",
        "agent_reply:job_image:0",
        600,
        content_type=ContentType.IMAGE,
        attachments=(image,),
    )
    controller.handle_sender_update(SenderUpdate("queued", delivery))

    echo = replace(
        message("echo", is_self=True, bridge_outbound=True),
        content="",
        content_type=ContentType.IMAGE,
        metadata={
            "is_self": True,
            "bridge_outbound": True,
            "bridge_delivery_text": "[图片] 结果图",
        },
    )
    await controller.handle(echo)

    entries = controller.timeline("friend")
    assert len(entries) == 1
    assert entries[0].attachments == (image,)
    assert entries[0].delivery_status == "sent"
    repository.close()


@pytest.mark.asyncio
async def test_combined_image_echo_waits_for_sender_verification(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    image = Attachment("image", "result.png", str(tmp_path / "result.png"))
    delivery = repository.enqueue_outbound_delivery(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        "处理完成。",
        "agent_reply:job_combined:0",
        600,
        content_type=ContentType.IMAGE,
        attachments=(image,),
    )
    controller.handle_sender_update(SenderUpdate("queued", delivery))
    echo = replace(
        message("echo", is_self=True, bridge_outbound=True),
        content="",
        content_type=ContentType.IMAGE,
        metadata={
            "is_self": True,
            "bridge_outbound": True,
            "bridge_delivery_text": "处理完成。",
        },
    )

    await controller.handle(echo)

    entries = controller.timeline("friend")
    assert len(entries) == 1
    assert entries[0].delivery_status == "queued"
    current = repository.get_outbound_delivery(delivery.id)
    assert current is not None
    assert current.status == OutboundDeliveryStatus.QUEUED
    repository.close()


@pytest.mark.asyncio
async def test_wechat_echo_confirms_original_card_without_duplicate(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    delivery = repository.enqueue_outbound_delivery(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        "hello",
        "job:reply",
        600,
    )
    controller.handle_sender_update(SenderUpdate("queued", delivery))
    original_entry_id = controller.timeline("friend")[0].entry_id
    unknown = repository.update_outbound_delivery(
        delivery.id,
        OutboundDeliveryStatus.FAILED,
        error_code="delivery_unknown",
        error_message="verification unavailable",
    )
    controller.handle_sender_update(SenderUpdate("delivery_unknown", unknown))

    await controller.handle(
        message("wechat-echo", is_self=True, bridge_outbound=True)
    )

    entries = controller.timeline("friend")
    assert len(entries) == 1
    assert entries[0].entry_id == original_entry_id
    assert entries[0].delivery_status == "sent"
    confirmed = repository.get_outbound_delivery(delivery.id)
    assert confirmed is not None
    assert confirmed.status == OutboundDeliveryStatus.SENT
    repository.close()


def test_agent_progress_merges_with_correlated_delivery(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.handle_agent_update(
        AgentProgressUpdate("job-1", "friend", "codex", "started")
    )
    entry_id = controller.timeline("friend")[0].entry_id
    controller.handle_agent_update(
        AgentProgressUpdate(
            "job-1", "friend", "codex", "streaming", "partial"
        )
    )
    controller.handle_agent_update(
        AgentProgressUpdate(
            "job-1",
            "friend",
            "codex",
            "completed",
            "final reply",
            ("final reply",),
        )
    )
    delivery = repository.enqueue_outbound_delivery(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        "final reply",
        "agent_reply:job-1:0",
        600,
    )
    controller.handle_sender_update(SenderUpdate("queued", delivery))
    controller.handle_sender_update(SenderUpdate("sent", delivery))

    entries = controller.timeline("friend")
    assert len(entries) == 1
    assert entries[0].entry_id == entry_id
    assert entries[0].content == "final reply"
    assert entries[0].delivery_id == delivery.id
    assert entries[0].delivery_status == "sent"
    repository.close()


def test_correlated_delivery_adds_quote_preview_to_agent_card(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.handle_agent_update(
        AgentProgressUpdate("job-quote", "friend", "codex", "started")
    )
    controller.handle_agent_update(
        AgentProgressUpdate(
            "job-quote",
            "friend",
            "codex",
            "completed",
            "爸爸，在。",
            ("爸爸，在。",),
        )
    )
    reference = ReplyReference(
        "friend:255",
        "friend",
        ConversationType.PRIVATE,
        "friend",
        "在吗",
        sender_name="工藤新一",
        created_at=datetime(2026, 9, 4, 0, 30, 39, tzinfo=timezone.utc),
    )
    delivery = repository.enqueue_outbound_delivery(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        "爸爸，在。",
        "agent_reply:job-quote:0",
        600,
        reply_to=reference,
    )

    controller.handle_sender_update(SenderUpdate("queued", delivery))

    entry = controller.timeline("friend")[0]
    assert entry.quote is not None
    assert entry.quote.sender_name == "工藤新一"
    assert entry.quote.content == "在吗"
    assert entry.quote.target_source_key == "wechat:friend:255"
    repository.close()


def test_local_history_restores_persisted_quote_preview(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    session = repository.create_session("codex", str(tmp_path))
    repository.bind_channel(message("binding"), session.id)
    repository.add_event(
        session.id,
        UnifiedEvent(
            EventType.ASSISTANT_MESSAGE,
            "爸爸，在。",
            provider="codex",
            metadata={
                "bridge_quote": {
                    "message_id": "friend:255",
                    "conversation_id": "friend",
                    "sender_id": "friend",
                    "sender_name": "工藤新一",
                    "content": "在吗",
                    "content_type": "text",
                    "created_at": "2026-09-04T00:30:39+00:00",
                }
            },
        ),
    )

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.load_local_history(item())

    entry = controller.timeline("friend")[0]
    assert entry.quote is not None
    assert entry.quote.target_source_key == "wechat:friend:255"
    repository.close()


def test_local_history_migrates_legacy_quote_xml_at_read_time(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    session = repository.create_session("codex", str(tmp_path))
    repository.bind_channel(message("binding"), session.id)
    repository.add_event(
        session.id,
        UnifiedEvent(
            EventType.ASSISTANT_MESSAGE,
            (
                "<msg><appmsg><title>爸爸，在。</title><refermsg>"
                "<type>1</type><displayname>工藤新一</displayname>"
                "<content>在吗</content><createtime>1788481839</createtime>"
                "</refermsg></appmsg></msg>"
            ),
            provider="codex",
        ),
    )

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.load_local_history(item())

    entry = controller.timeline("friend")[0]
    assert entry.content == "爸爸，在。"
    assert entry.quote is not None
    assert entry.quote.content == "在吗"
    repository.close()


def test_agent_failure_updates_streaming_card_without_duplicate(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.handle_agent_update(
        AgentProgressUpdate("job-1", "friend", "codex", "started")
    )
    controller.handle_agent_update(
        AgentProgressUpdate("job-1", "friend", "codex", "streaming", "partial")
    )
    controller.handle_agent_update(
        AgentProgressUpdate(
            "job-1",
            "friend",
            "codex",
            "failed",
            "partial",
            detail="Agent 执行失败：connection closed",
        )
    )

    entries = controller.timeline("friend")
    assert len(entries) == 1
    assert entries[0].content == "partial"
    assert entries[0].delivery_status == "generation_failed"
    assert entries[0].status_detail == "Agent 执行失败：connection closed"
    repository.close()


def test_completed_long_agent_reply_remains_one_correlated_card(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(repository, dispatch, load_history, "codex")
    controller.handle_agent_update(
        AgentProgressUpdate("job-2", "friend", "claude", "started")
    )
    controller.handle_agent_update(
        AgentProgressUpdate(
            "job-2",
            "friend",
            "claude",
            "completed",
            "one two",
            ("one", "two"),
        )
    )

    first = repository.enqueue_outbound_delivery(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        "one",
        "agent_reply:job-2:0",
        600,
    )
    second = repository.enqueue_outbound_delivery(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        "two",
        "agent_reply:job-2:1",
        600,
    )
    controller.handle_sender_update(SenderUpdate("queued", first))
    controller.handle_sender_update(SenderUpdate("sent", first))

    entries = controller.timeline("friend")
    assert len(entries) == 1
    assert entries[0].content == "one two"
    assert entries[0].delivery_status == "queued"

    controller.handle_sender_update(SenderUpdate("queued", second))
    controller.handle_sender_update(SenderUpdate("sent", second))

    entry = controller.timeline("friend")[0]
    assert entry.delivery_status == "sent"
    assert entry.delivery_ids == (first.id, second.id)
    repository.close()


def test_foreground_fallback_setting_uses_global_callbacks(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    state = {"enabled": False}

    async def dispatch(_incoming: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(
        repository,
        dispatch,
        load_history,
        "codex",
        foreground_fallback_getter=lambda: state["enabled"],
        foreground_fallback_setter=lambda enabled: state.__setitem__(
            "enabled", enabled
        ),
    )
    updates = []
    controller.subscribe(updates.append)

    assert controller.foreground_fallback_enabled() is False
    controller.set_foreground_fallback_enabled(True)

    assert controller.foreground_fallback_enabled() is True
    assert updates[-1].kind == "sender_settings"
    repository.close()
