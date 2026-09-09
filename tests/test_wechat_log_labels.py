import asyncio
import logging

import pytest

from agent_bridge.channels.wechat import WeChatChannelAdapter, WeChatChannelSettings
from agent_bridge.models import ConversationType, UnifiedMessage


def message(conversation="wxid_friend", kind=ConversationType.PRIVATE, sender="wxid_friend"):
    return UnifiedMessage(
        channel="wechat", channel_account_id="bot", conversation_id=conversation,
        conversation_type=kind, sender_id=sender, message_id="row-1",
        content="PRIVATE_MESSAGE_BODY_NOT_FOR_LOGGING", sender_name="Sender",
    )


def adapter_with_aliases(monkeypatch):
    adapter = WeChatChannelAdapter(WeChatChannelSettings(
        allowed_private_ids=("visible_id", "好友备注", "visible_id"),
        allowed_group_ids=("测试群",),
    ), repository=object())
    monkeypatch.setattr(adapter, "_resolve_conversation_username",
                        lambda value, kind, sessions: "team@chatroom" if kind == ConversationType.GROUP else "wxid_friend")
    adapter._resolved_private_ids = adapter._resolve_allowlist_entries(
        adapter.settings.allowed_private_ids, ConversationType.PRIVATE, set())
    adapter._resolved_group_ids = adapter._resolve_allowlist_entries(
        adapter.settings.allowed_group_ids, ConversationType.GROUP, set())
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_received_log_uses_yaml_conversation_not_group_sender(monkeypatch, caplog, group):
    adapter = adapter_with_aliases(monkeypatch)
    incoming = message("team@chatroom", ConversationType.GROUP) if group else message()
    monkeypatch.setattr(adapter, "normalize", lambda raw: incoming)
    monkeypatch.setattr(adapter, "_sender_display_name", lambda msg: msg.sender_name)
    # Logging must reuse the existing mapping, not look up contacts for each message.
    monkeypatch.setattr(adapter, "_resolve_conversation_username", lambda *args: pytest.fail("extra query"))
    handled = asyncio.Event()
    received = []

    async def handler(msg):
        received.append(msg)
        handled.set()

    adapter._handler = handler
    adapter._loop = asyncio.get_running_loop()
    caplog.set_level(logging.INFO, logger="agent_bridge.channels.wechat")
    caplog.clear()
    adapter._on_raw_message({}, None)
    await asyncio.wait_for(handled.wait(), 1)
    line = next(r.getMessage() for r in caplog.records if "收到微信消息" in r.getMessage())
    expected = '[配置群聊: "测试群"]' if group else '[配置私聊: "visible_id"、"好友备注"]'
    assert line.startswith(expected + " 收到微信消息:")
    assert "sender=wxid_friend" in line
    assert "message_id=row-1" in line
    assert incoming.content not in line
    assert received[0].content == incoming.content
    if group:
        assert "配置私聊" not in line
        assert "visible_id" not in line


@pytest.mark.asyncio
async def test_non_allowlisted_message_gets_no_configured_log(monkeypatch, caplog):
    adapter = adapter_with_aliases(monkeypatch)
    monkeypatch.setattr(adapter, "normalize", lambda raw: message("stranger"))
    adapter._handler = lambda msg: pytest.fail("must not dispatch")
    adapter._loop = asyncio.get_running_loop()
    caplog.set_level(logging.DEBUG, logger="agent_bridge.channels.wechat")
    caplog.clear()
    adapter._on_raw_message({}, None)
    assert "忽略白名单外微信消息" in caplog.text
    assert "收到微信消息" not in caplog.text
    assert "[配置" not in caplog.text


def test_re_resolution_removes_stale_labels_and_preserves_other_type(monkeypatch):
    adapter = adapter_with_aliases(monkeypatch)
    monkeypatch.setattr(adapter, "_resolve_conversation_username", lambda *args: "replacement")
    adapter._resolve_allowlist_entries(("new-alias",), ConversationType.PRIVATE, set())
    assert (ConversationType.PRIVATE, "wxid_friend") not in adapter._allowlist_log_labels
    assert adapter._configured_message_log_tag(message("replacement")) == '[配置私聊: "new-alias"]'
    assert adapter._configured_message_log_tag(message("team@chatroom", ConversationType.GROUP)) == '[配置群聊: "测试群"]'
    adapter._resolve_allowlist_entries((), ConversationType.PRIVATE, set())
    assert not any(key[0] == ConversationType.PRIVATE for key in adapter._allowlist_log_labels)


def test_equal_config_names_are_kept_separate_by_conversation_type(monkeypatch):
    adapter = WeChatChannelAdapter(WeChatChannelSettings(
        allowed_private_ids=("相同名称",), allowed_group_ids=("相同名称",),
    ), repository=object())
    monkeypatch.setattr(adapter, "_resolve_conversation_username", lambda value, kind, sessions:
                        "friend" if kind == ConversationType.PRIVATE else "group@chatroom")
    for kind in (ConversationType.PRIVATE, ConversationType.GROUP):
        adapter._resolve_allowlist_entries(("相同名称",), kind, set())
    assert adapter._configured_message_log_tag(message("friend")) == '[配置私聊: "相同名称"]'
    assert adapter._configured_message_log_tag(message("group@chatroom", ConversationType.GROUP)) == '[配置群聊: "相同名称"]'


def test_configured_label_escapes_control_characters():
    value = 'name\nnew-line\r\t"quote"'
    adapter = WeChatChannelAdapter(WeChatChannelSettings(allowed_private_ids=(value,)), repository=object())
    tag = adapter._configured_message_log_tag(message(value))
    assert "\n" not in tag and "\r" not in tag and "\t" not in tag
    assert r"\nnew-line\r\t" in tag


def test_missing_mapping_is_not_invented_from_sender_or_conversation():
    adapter = WeChatChannelAdapter(WeChatChannelSettings(), repository=object())
    assert adapter._configured_message_log_tag(message()) == "[白名单私聊: 原始配置项未知]"
