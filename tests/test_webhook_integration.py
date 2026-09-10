import asyncio
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from agent_bridge.channels.wechat import WeChatChannelAdapter, WeChatChannelSettings
from agent_bridge.manager.config_document import ConfigDocument
from agent_bridge.models import ConversationType, UnifiedMessage
from agent_bridge.webhooks import WebhookSettings


def test_multiple_webhooks_config_round_trip(tmp_path):
    document = ConfigDocument(tmp_path / "config.yaml", {})
    rows = [asdict(WebhookSettings(name="私聊", url="https://one.example/in", enabled=True,
                                  conversation_type="private", sender="others", method="PUT",
                                  content_types=["text"],
                                  headers={"Authorization": "Bearer TEST_TOKEN"})),
            asdict(WebhookSettings(name="群本人", url="https://two.example/in", enabled=True,
                                  conversation_type="group", sender="self", include_ai_replies=True,
                                  content_types=["text", "image"],
                                  max_attempts=5, timeout_seconds=9))]
    document.set_value("channels.wechat.webhooks", rows)
    config = document.save()
    assert [asdict(s) for s in config.wechat.webhooks] == rows
    assert ConfigDocument.load(document.path).value("channels.wechat.webhooks") == rows


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ConversationType.PRIVATE, ConversationType.GROUP])
@pytest.mark.parametrize("source", ["self", "others", "ai"])
@pytest.mark.parametrize("pending", [False, True])
async def test_raw_message_webhook_identity_and_untrimmed_content(monkeypatch, kind, source, pending):
    adapter = WeChatChannelAdapter(WeChatChannelSettings(
        allowed_private_ids=("friend",), allowed_group_ids=("room@chatroom",),
        group_prefixes=("/ai",)), repository=object())
    conversation = "friend" if kind == ConversationType.PRIVATE else "room@chatroom"
    incoming = UnifiedMessage(channel="wechat", channel_account_id="bot", conversation_id=conversation,
                              conversation_type=kind, sender_id="bot" if source != "others" else "friend",
                              sender_name="本人" if source != "others" else "其他人", message_id="row1",
                              content="/ai 原始消息", metadata={"is_self": source != "others"})
    captured, handled = [], asyncio.Event()
    adapter._webhooks = SimpleNamespace(submit=lambda message, labels: captured.append((message, labels)))
    monkeypatch.setattr(adapter, "normalize", lambda raw: incoming)
    monkeypatch.setattr(adapter, "_sender_display_name", lambda m:
                        "本人" if m.metadata.get("is_self") else "其他人")
    monkeypatch.setattr(adapter, "_remember_self_identity", lambda raw: None)
    if pending:
        adapter._record_pending_outbound(conversation, incoming.content)

    async def handler(message):
        handled.set()

    adapter._handler, adapter._loop = handler, asyncio.get_running_loop()
    adapter._on_raw_message({}, None)
    await asyncio.wait_for(handled.wait(), 1)
    assert len(captured) == 1
    message, labels = captured[0]
    assert message.content == "/ai 原始消息"
    assert message.sender_name == incoming.sender_name
    assert message.sender_id == incoming.sender_id
    assert message.metadata["is_self"] == (source != "others")
    assert message.metadata["bridge_outbound"] == (pending and source != "others")
    assert labels == (conversation,)


@pytest.mark.asyncio
async def test_webhook_failure_does_not_block_reception_and_strangers_are_not_forwarded(monkeypatch):
    adapter = WeChatChannelAdapter(WeChatChannelSettings(allowed_private_ids=("friend",)), repository=object())
    incoming = UnifiedMessage(channel="wechat", channel_account_id="bot", conversation_id="stranger",
                              conversation_type=ConversationType.PRIVATE, sender_id="friend",
                              message_id="1", content="hi")
    calls, handled = [], asyncio.Event()

    def submit(*args):
        calls.append(args)
        raise RuntimeError("simulated queue failure")

    async def handler(message):
        handled.set()

    adapter._webhooks = SimpleNamespace(submit=submit)
    adapter._handler, adapter._loop = handler, asyncio.get_running_loop()
    monkeypatch.setattr(adapter, "normalize", lambda raw: incoming)
    monkeypatch.setattr(adapter, "_sender_display_name", lambda m: m.sender_name)
    adapter._on_raw_message({}, None)
    assert not calls
    incoming = replace(incoming, conversation_id="friend")
    adapter._on_raw_message({}, None)
    await asyncio.wait_for(handled.wait(), 1)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_listener_start_failure_stops_webhook_workers(monkeypatch):
    adapter = WeChatChannelAdapter(WeChatChannelSettings(), repository=object())
    events = []

    async def start_sender():
        events.append("sender")

    def start_listener():
        raise RuntimeError("listener startup failed")

    def configure():
        adapter._sender = SimpleNamespace(start=start_sender)
        adapter._listener = SimpleNamespace(start=start_listener)

    monkeypatch.setattr(adapter, "_start_blocking", configure)
    adapter._webhooks = SimpleNamespace(start=lambda: events.append("start"), stop=lambda: events.append("stop"))
    with pytest.raises(RuntimeError, match="listener startup failed"):
        await adapter.start(lambda m: None)
    assert events == ["sender", "start", "stop"]
