import asyncio
import logging
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from agent_bridge.channels.wechat import (
    WeChatChannelAdapter,
    WeChatChannelSettings,
    _SerializedWeChatDb,
)
from agent_bridge.models import (
    ChannelTarget,
    ContentType,
    ConversationType,
    OutboundMessage,
    ReplyReference,
    UnifiedMessage,
)
from agent_bridge.senders.wechat import WeChatSenderSettings
from agent_bridge.sessions.repository import SQLiteRepository


class FakeListener:
    def __init__(self) -> None:
        self.registrations: list[tuple[str, object]] = []

    def add_listener(self, conversation_id: str, callback: object) -> None:
        self.registrations.append((conversation_id, callback))

    def add_all(self, callback: object, discover: bool = True) -> None:
        raise AssertionError("the adapter must not scan every WeChat conversation")


def test_foreground_fallback_setting_is_persisted_per_wechat_account(
    tmp_path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(
            sender=WeChatSenderSettings(foreground_fallback_default=True)
        ),
        repository,
    )
    adapter._account_id = "bot-a"

    assert adapter.foreground_fallback_enabled() is True
    adapter.set_foreground_fallback_enabled(False)
    assert adapter.foreground_fallback_enabled() is False

    adapter._account_id = "bot-b"
    assert adapter.foreground_fallback_enabled() is True
    repository.close()


def test_registers_only_allowlisted_wechat_conversations() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(
            allowed_private_ids=("friend-a", "friend-b"),
            allowed_group_ids=("team@chatroom",),
        ),
        repository=object(),  # type: ignore[arg-type]
    )
    listener = FakeListener()
    adapter._listener = listener

    adapter._register_allowlisted_listeners()

    assert [item[0] for item in listener.registrations] == [
        "friend-a",
        "friend-b",
        "team@chatroom",
    ]


@pytest.mark.asyncio
async def test_image_hydration_preserves_per_conversation_message_order() -> None:
    adapter = WeChatChannelAdapter(WeChatChannelSettings(), repository=object())
    received = []
    hydration_started = threading.Event()
    release_hydration = threading.Event()

    async def handler(message) -> None:
        received.append(message.message_id)

    def hydrate(message, _raw_event):
        hydration_started.set()
        assert release_hydration.wait(timeout=2)
        return message

    adapter._handler = handler
    adapter._hydrate_media_attachment = hydrate
    image = UnifiedMessage(
        "wechat",
        "bot",
        "friend",
        ConversationType.PRIVATE,
        "friend",
        "image-first",
        "[图片]",
        content_type=ContentType.IMAGE,
    )
    text = UnifiedMessage(
        "wechat",
        "bot",
        "friend",
        ConversationType.PRIVATE,
        "friend",
        "text-second",
        "图片内容是什么",
    )

    image_task = asyncio.create_task(adapter._dispatch_message_in_order(image, {}))
    await asyncio.to_thread(hydration_started.wait, 2)
    text_task = asyncio.create_task(adapter._dispatch_message_in_order(text, {}))
    await asyncio.sleep(0)
    release_hydration.set()
    await asyncio.gather(image_task, text_task)

    assert received == ["image-first", "text-second"]


@pytest.mark.asyncio
async def test_logs_an_accepted_wechat_message(caplog: pytest.LogCaptureFixture) -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)),
        repository=object(),  # type: ignore[arg-type]
    )
    received = []
    handled = asyncio.Event()

    async def handler(message) -> None:
        received.append(message)
        handled.set()

    adapter._account_id = "bot"
    adapter._nickname = "Bot"
    adapter._handler = handler
    adapter._loop = asyncio.get_running_loop()
    caplog.set_level(logging.INFO, logger="agent_bridge.channels.wechat")

    adapter._on_raw_message(
        {
            "username": "friend",
            "sender_username": "friend",
            "sender_id": 1,
            "sender_name": "Friend",
            "content": "hello",
            "type": "文本",
            "local_id": 7,
            "sort_seq": 9,
        },
        None,
    )
    await asyncio.wait_for(handled.wait(), timeout=1)

    assert [message.content for message in received] == ["hello"]
    assert "收到微信消息" in caplog.text
    assert "conversation=friend" in caplog.text
    assert '[配置私聊: "friend"] 收到微信消息' in caplog.text


@pytest.mark.asyncio
async def test_group_message_is_observed_even_without_agent_trigger() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_group_ids=("team@chatroom",)),
        repository=object(),  # type: ignore[arg-type]
    )
    handled = asyncio.Event()
    received = []

    async def handler(message) -> None:
        received.append(message)
        handled.set()

    adapter._account_id = "bot"
    adapter._nickname = "Bot"
    adapter._handler = handler
    adapter._loop = asyncio.get_running_loop()
    adapter._on_raw_message(
        {
            "username": "team@chatroom",
            "sender_username": "friend",
            "sender_id": 7,
            "content": "normal group message",
            "type": "文本",
            "local_id": 8,
        },
        None,
    )
    await asyncio.wait_for(handled.wait(), timeout=1)

    assert received[0].metadata["agent_triggered"] is False
    assert received[0].content == "normal group message"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("@胡超然 请处理这个", "请处理这个"),
        ("请 @胡超然 处理这个", "请  处理这个"),
        ("请处理这个 @胡超然", "请处理这个"),
    ],
)
def test_group_trigger_matches_anywhere_in_message(
    content: str, expected: str
) -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(
            group_prefixes=("@胡超然",),
            group_prefixes_rule="contains",
            allowed_group_ids=("team@chatroom",),
        ),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._nickname = "胡超然"
    message = UnifiedMessage(
        channel="wechat",
        channel_account_id="wxid_self",
        conversation_id="team@chatroom",
        conversation_type=ConversationType.GROUP,
        sender_id="friend",
        message_id="team@chatroom:1",
        content=content,
    )

    prepared = adapter._prepare_for_controller(message)

    assert prepared.metadata["agent_triggered"] is True
    assert prepared.content == expected


def test_group_message_without_trigger_stays_untriggered() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(
            group_prefixes=("@胡超然",),
            group_prefixes_rule="contains",
            allowed_group_ids=("team@chatroom",),
        ),
        repository=object(),  # type: ignore[arg-type]
    )
    message = UnifiedMessage(
        channel="wechat",
        channel_account_id="wxid_self",
        conversation_id="team@chatroom",
        conversation_type=ConversationType.GROUP,
        sender_id="friend",
        message_id="team@chatroom:2",
        content="普通群消息",
    )

    prepared = adapter._prepare_for_controller(message)

    assert prepared.metadata["agent_triggered"] is False
    assert prepared.content == "普通群消息"


@pytest.mark.parametrize(
    ("rule", "content", "triggered"),
    [
        ("prefix", "/ai 请处理", True),
        ("prefix", "请处理 /ai", False),
        ("suffix", "请处理 /ai", True),
        ("suffix", "/ai 请处理", False),
        ("contains", "请 /ai 处理", True),
    ],
)
def test_group_trigger_rule_controls_match_position(
    rule: str, content: str, triggered: bool
) -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(
            group_prefixes=("/ai",),
            group_prefixes_rule=rule,
            allowed_group_ids=("team@chatroom",),
        ),
        repository=object(),  # type: ignore[arg-type]
    )
    message = UnifiedMessage(
        channel="wechat",
        channel_account_id="wxid_self",
        conversation_id="team@chatroom",
        conversation_type=ConversationType.GROUP,
        sender_id="friend",
        message_id="team@chatroom:3",
        content=content,
    )

    prepared = adapter._prepare_for_controller(message)

    assert prepared.metadata["agent_triggered"] is triggered


def test_marks_matching_self_message_as_bridge_outbound() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("filehelper",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._record_pending_outbound("filehelper", "agent reply")

    assert adapter._consume_pending_outbound("filehelper", "mobile message") is None
    assert adapter._consume_pending_outbound("filehelper", "agent reply") == "agent reply"


def test_pending_image_matches_wechat_image_echo() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._record_pending_outbound(
        "friend", "[图片] 结果图", ContentType.IMAGE
    )

    assert adapter._consume_pending_outbound(
        "friend", "", ContentType.IMAGE
    ) == "[图片] 结果图"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("截图网易云和微信给我", ("网易云", "微信")),
        ("截图微信、QQ", ("微信", "QQ")),
        ("截图 Chrome, vscode", ("Chrome", "vscode")),
    ],
)
def test_application_queries_support_multiple_targets(content, expected) -> None:
    assert WeChatChannelAdapter._application_queries(content) == expected


def test_combined_pending_matches_both_text_and_image_echoes() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._record_pending_outbound(
        "friend", "处理完成。", ContentType.IMAGE
    )

    assert adapter._consume_pending_outbound(
        "friend", "处理完成。", ContentType.TEXT
    ) == "处理完成。"
    assert adapter._consume_pending_outbound(
        "friend", "", ContentType.IMAGE
    ) == "处理完成。"
    assert not adapter._pending_outbound


def test_discard_combined_pending_removes_both_echo_signatures() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._record_pending_outbound(
        "friend", "处理完成。", ContentType.IMAGE
    )

    adapter._discard_pending_outbound("friend", "处理完成。")

    assert not adapter._pending_outbound


@pytest.mark.parametrize(
    ("sender_username", "sender_id", "expected"),
    [
        ("bot", 9, True),
        # WeChat's numeric self marker is authoritative even when the
        # username field is stale.
        ("friend", 2, True),
        (None, 2, True),
    ],
)
def test_normalize_prefers_sender_username_for_message_direction(
    sender_username: str | None,
    sender_id: int,
    expected: bool,
) -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._account_id = "bot"
    event = {
        "username": "friend",
        "sender_id": sender_id,
        "content": "hello",
        "type": "文本",
        "local_id": 8,
    }
    if sender_username is not None:
        event["sender_username"] = sender_username

    message = adapter.normalize(event)

    assert message.metadata["is_self"] is expected


def test_normalize_group_falls_back_to_sender_display_name() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_group_ids=("team@chatroom",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._account_id = "bot"
    message = adapter.normalize(
        {
            "username": "team@chatroom",
            "sender_id": 7,
            "sender_name": "成员甲",
            "content": "成员甲: 这是一条图片消息",
            "type": "文本",
            "local_id": 8,
        }
    )

    assert message.sender_id == "7"
    assert message.sender_name == "成员甲"
    assert message.content == "这是一条图片消息"


def test_normalize_group_self_marker_wins_over_sender_prefix() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_group_ids=("team@chatroom",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._account_id = "wxid_self"
    adapter._nickname = "本人昵称"

    message = adapter.normalize(
        {
            "username": "team@chatroom",
            "sender_id": 2,
            "sender_username": "stale-member",
            "content": "wxid_other:\n这是本人发的消息",
            "type": "文本",
            "local_id": 10,
        }
    )

    assert message.metadata["is_self"] is True
    assert message.sender_id == "wxid_self"
    assert message.sender_name == "本人昵称"
    assert message.content == "这是本人发的消息"


def test_normalize_group_self_nickname_prefix_is_recognized() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_group_ids=("team@chatroom",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._account_id = "wxid_self"
    adapter._nickname = "本人昵称"

    message = adapter.normalize(
        {
            "username": "team@chatroom",
            "sender_id": 21,
            "sender_username": "stale-member",
            "content": "本人昵称:\n昵称前缀也代表本人",
            "type": "文本",
            "local_id": 11,
        }
    )

    assert message.metadata["is_self"] is True
    assert message.sender_id == "wxid_self"
    assert message.sender_name == "本人昵称"
    assert message.content == "昵称前缀也代表本人"


def test_group_history_self_marker_is_not_overwritten_by_prefix() -> None:
    class FakeDB:
        def get_messages(self, _conversation_id: str, limit: int):
            assert limit == 10
            return [
                {
                    "local_id": 1,
                    "type": "文本",
                    "sender_id": 2,
                    "sender_username": "stale-member",
                    "content": "wxid_other:\n本人历史消息",
                    "create_time": 1,
                }
            ]

        def get_nickname(self, username: str) -> str:
            return username

    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_group_ids=("team@chatroom",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._account_id = "wxid_self"
    adapter._nickname = "本人昵称"
    adapter._db = FakeDB()

    history = adapter._load_history_blocking("team@chatroom", 10)

    assert len(history) == 1
    assert history[0].metadata["is_self"] is True
    assert history[0].sender_id == "wxid_self"
    assert history[0].sender_name == "本人昵称"
    assert history[0].content == "本人历史消息"


def test_normalize_animation_sticker_as_displayable_image() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_group_ids=("team@chatroom",)),
        repository=object(),  # type: ignore[arg-type]
    )
    message = adapter.normalize(
        {
            "username": "team@chatroom",
            "sender_username": "friend",
            "sender_name": "成员甲",
            "content": "[动画表情]",
            "type": "动画表情",
            "local_id": 9,
        }
    )

    assert message.content_type == ContentType.IMAGE
    assert message.metadata["raw_type"] == "动画表情"


def test_normalize_image_xml_uses_display_marker_instead_of_raw_xml() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_group_ids=("team@chatroom",)),
        repository=object(),  # type: ignore[arg-type]
    )

    message = adapter.normalize(
        {
            "username": "team@chatroom",
            "sender_username": "wxid_member",
            "content": (
                "wxid_member:\n<?xml version=\"1.0\"?>"
                "<msg><img aeskey=\"cdn-key\" md5=\"image-md5\" /></msg>"
            ),
            "type": "图片",
            "local_id": 23139,
        }
    )

    assert message.content_type == ContentType.IMAGE
    assert message.content == "[图片]"
    assert "<img" not in message.content


def test_media_key_is_cached_after_fast_process_config_derivation() -> None:
    class FakeDB:
        cfg_dword = 1234
        wxid = "wxid_bot"

    class FakeDownloader:
        db = FakeDB()
        derive_calls = 0

        @staticmethod
        def _load_persisted_key():
            return None

        @staticmethod
        def _derive_cfg_key():
            return None

        def derive_image_keys(self, cfg_dword, wxid):
            assert (cfg_dword, wxid) == (1234, "wxid_bot")
            self.derive_calls += 1
            return "1234567890abcdef", 42

    adapter = WeChatChannelAdapter(WeChatChannelSettings(), repository=object())
    downloader = FakeDownloader()

    assert adapter._resolve_media_keys(downloader) == ("1234567890abcdef", 42)
    assert adapter._resolve_media_keys(downloader) == ("1234567890abcdef", 42)
    assert downloader.derive_calls == 1


def test_media_key_rejects_process_config_for_another_account() -> None:
    class FakeDB:
        cfg_dword = None
        wxid = "wxid_expected"

        @staticmethod
        def extract_master_key():
            return "master", 1234, "wxid_other"

    class FakeDownloader:
        db = FakeDB()

        @staticmethod
        def _load_persisted_key():
            return None

        @staticmethod
        def _derive_cfg_key():
            return None

        @staticmethod
        def derive_image_keys(_cfg_dword, _wxid):
            raise AssertionError("another account must not be used")

    adapter = WeChatChannelAdapter(WeChatChannelSettings(), repository=object())

    assert adapter._resolve_media_keys(FakeDownloader()) is None


def test_normalize_native_quote_hides_xml_and_exposes_reference() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)),
        repository=object(),  # type: ignore[arg-type]
    )
    message = adapter.normalize(
        {
            "username": "friend",
            "sender_username": "friend",
            "sender_name": "工藤新一",
            "content": (
                "<msg><appmsg><title>爸爸，在。</title><type>57</type>"
                "<refermsg><type>1</type><svrid>99</svrid>"
                "<fromusr>friend</fromusr><displayname>工藤新一</displayname>"
                "<content>在吗</content><createtime>1788481839</createtime>"
                "</refermsg></appmsg></msg>"
            ),
            "type": "文件/链接/卡片",
            "local_id": 258,
        }
    )

    assert message.content == "爸爸，在。"
    assert message.content_type == ContentType.TEXT
    assert "<appmsg>" not in message.content
    assert message.metadata["bridge_quote"] == {
        "message_id": None,
        "conversation_id": "friend",
        "server_id": "99",
        "sender_id": "friend",
        "sender_name": "工藤新一",
        "content": "在吗",
        "content_type": "text",
        "created_at": "2026-09-04T00:30:39+00:00",
    }


def test_normalize_malformed_native_quote_never_displays_raw_xml() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)),
        repository=object(),  # type: ignore[arg-type]
    )
    message = adapter.normalize(
        {
            "username": "friend",
            "sender_username": "friend",
            "content": (
                "<msg><appmsg><title>回复</title><refermsg><type>1</type>"
                "<displayname>对方</displayname><content>原始 & 内容</content>"
                "</refermsg></appmsg></msg>"
            ),
            "type": "文件/链接/卡片",
            "local_id": 259,
        }
    )

    assert message.content == "回复"
    assert "<" not in message.content
    assert message.metadata["bridge_quote"]["content"] == "原始 & 内容"


def test_group_history_uses_content_sender_for_text_and_image_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeDB:
        def get_messages(self, _conversation_id: str, limit: int):
            assert limit == 10
            return [
                {
                    "local_id": 3,
                    "type": "文本",
                    "sender_id": 11,
                    "sender_username": "stale-user-2",
                    "content": "liaojipeng2012:\n另一条",
                    "create_time": 3,
                },
                {
                    "local_id": 2,
                    "type": "图片",
                    "sender_id": 10,
                    "sender_username": "stale-user",
                    "content": "[图片]",
                    "create_time": 2,
                },
                {
                    "local_id": 1,
                    "type": "文本",
                    "sender_id": 10,
                    "sender_username": "stale-user",
                    "content": "wxid_real-user:\n你好",
                    "create_time": 1,
                },
            ]

        def get_nickname(self, username: str) -> str:
            return {
                "wxid_real-user": "正确成员",
                "liaojipeng2012": "Keith",
            }.get(username, username)

    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_group_ids=("team@chatroom",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._account_id = "bot"
    adapter._nickname = "Bot"
    adapter._db = FakeDB()
    monkeypatch.setattr(
        adapter,
        "_hydrate_media_attachment",
        lambda message, _raw_event: message,
    )

    history = adapter._load_history_blocking("team@chatroom", 10)

    assert [message.sender_name for message in history] == [
        "正确成员",
        "正确成员",
        "Keith",
    ]
    assert [message.content for message in history] == [
        "你好",
        "[图片]",
        "另一条",
    ]


@pytest.mark.asyncio
async def test_filehelper_self_message_is_observed_and_loop_marker_is_attached() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("filehelper",)),
        repository=object(),  # type: ignore[arg-type]
    )
    handled = asyncio.Event()
    received = []

    async def handler(message) -> None:
        received.append(message)
        handled.set()

    adapter._account_id = "bot"
    adapter._nickname = "Bot"
    adapter._handler = handler
    adapter._loop = asyncio.get_running_loop()
    adapter._record_pending_outbound("filehelper", "agent reply")
    adapter._on_raw_message(
        {
            "username": "filehelper",
            "sender_id": "2",
            "content": "agent reply",
            "type": "文本",
            "local_id": 10,
        },
        None,
    )
    await asyncio.wait_for(handled.wait(), timeout=1)

    assert received[0].metadata["is_self"] is True
    assert received[0].metadata["bridge_outbound"] is True


@pytest.mark.asyncio
async def test_pending_outbound_overrides_changed_wechat_sender_id() -> None:
    repository = object()
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)),
        repository=repository,  # type: ignore[arg-type]
    )
    handled = asyncio.Event()
    received = []

    async def handler(message) -> None:
        received.append(message)
        handled.set()

    adapter._account_id = "wxid_self"
    adapter._handler = handler
    adapter._loop = asyncio.get_running_loop()
    adapter._record_pending_outbound("friend", "agent reply")
    adapter._on_raw_message(
        {
            "username": "friend",
            "sender_id": 9,
            "sender_username": "new_self_alias",
            "content": "agent reply",
            "type": "文本",
            "local_id": 10,
        },
        None,
    )
    await asyncio.wait_for(handled.wait(), timeout=1)

    assert received[0].metadata["is_self"] is True
    assert received[0].metadata["bridge_outbound"] is True


def test_delivery_verification_learns_current_wechat_sender_identity(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)), repository
    )
    adapter._account_id = "wxid_self"
    adapter._db = type(
        "FakeDB",
        (),
        {
            "get_messages": lambda _self, _conversation_id, limit: [
                {
                    "sender_id": 9,
                    "sender_username": "new_self_alias",
                    "content": "agent reply",
                    "create_time": 2_000_000_000,
                }
            ]
        },
    )()
    delivery = type(
        "Delivery",
        (),
        {
            "conversation_id": "friend",
            "text": "agent reply",
            "next_attempt_at": datetime.fromtimestamp(
                2_000_000_000, tz=timezone.utc
            ),
        },
    )()

    assert adapter._verify_silent_delivery(delivery) is True
    assert adapter._is_self_message(
        {"sender_id": 9, "sender_username": "new_self_alias"}
    ) is True
    assert repository.get_channel_state(
        "wechat", "wxid_self", "self_sender_identities"
    ) == {
        "sender_ids": ["2", "9"],
        "sender_usernames": ["new_self_alias"],
    }
    repository.close()


def test_delivery_verification_consumes_distinct_rows_for_identical_replies(
    tmp_path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)), repository
    )
    rows = [
        {
            "local_id": 101,
            "sort_seq": 101,
            "sender_id": 2,
            "content": "same reply",
            "create_time": 2_000_000_001,
        },
        {
            "local_id": 102,
            "sort_seq": 102,
            "sender_id": 2,
            "content": "same reply",
            "create_time": 2_000_000_002,
        },
    ]
    delivery_type = type(
        "Delivery",
        (),
        {
            "conversation_id": "friend",
            "text": "same reply",
            "content_type": ContentType.TEXT,
        },
    )

    first = delivery_type()
    second = delivery_type()
    assert adapter._delivery_matches_rows(first, rows, 2_000_000_000) is True
    assert adapter._delivery_matches_rows(second, rows, 2_000_000_000) is True
    # No third delivery may reuse either already-consumed WeChat row.
    assert adapter._delivery_matches_rows(first, rows, 2_000_000_000) is False
    repository.close()


def test_image_delivery_verification_matches_recent_self_image(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)), repository
    )
    adapter._db = type(
        "FakeDB",
        (),
        {
            "get_messages": lambda _self, _conversation_id, limit: [
                {
                    "sender_id": 2,
                    "type": "图片",
                    "content": "",
                    "create_time": 2_000_000_000,
                }
            ]
        },
    )()
    delivery = type(
        "Delivery",
        (),
        {
            "conversation_id": "friend",
            "text": "[图片] 结果图",
            "content_type": ContentType.IMAGE,
            "next_attempt_at": datetime.fromtimestamp(
                2_000_000_000, tz=timezone.utc
            ),
        },
    )()

    assert adapter._verify_silent_delivery(delivery) is True
    repository.close()


def test_combined_delivery_verification_requires_recent_self_text_and_image(
    tmp_path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)), repository
    )
    rows = [
        {
            "sender_id": 2,
            "type": "图片",
            "content": "",
            "create_time": 2_000_000_000,
        }
    ]
    adapter._db = type(
        "FakeDB",
        (),
        {"get_messages": lambda _self, _conversation_id, limit: rows},
    )()
    delivery = type(
        "Delivery",
        (),
        {
            "conversation_id": "friend",
            "text": "处理完成。",
            "content_type": ContentType.IMAGE,
            "next_attempt_at": datetime.fromtimestamp(
                2_000_000_000, tz=timezone.utc
            ),
        },
    )()

    assert adapter._delivery_matches_rows(delivery, rows, 1_999_999_998) is False
    rows.append(
        {
            "sender_id": 2,
            "type": "文本",
            "content": "处理完成。",
            "create_time": 2_000_000_000,
        }
    )
    assert adapter._delivery_matches_rows(delivery, rows, 1_999_999_998) is True
    repository.close()




def test_delivery_verification_retries_transient_database_merge_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)), repository
    )

    class FlakyDB:
        calls = 0

        def get_messages(self, _conversation_id: str, limit: int):
            assert limit == 10
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    "数据库合并失败(文件被微信并发改写): message/message_1.db"
                )
            return [
                {
                    "sender_id": 2,
                    "content": "agent reply",
                    "create_time": 2_000_000_000,
                }
            ]

    adapter._db = FlakyDB()
    delivery = type(
        "Delivery",
        (),
        {
            "conversation_id": "friend",
            "text": "agent reply",
            "next_attempt_at": datetime.fromtimestamp(
                2_000_000_000, tz=timezone.utc
            ),
        },
    )()
    monkeypatch.setattr("agent_bridge.channels.wechat.time.sleep", lambda _value: None)

    assert adapter._verify_silent_delivery(delivery) is True
    assert adapter._db.calls == 2
    repository.close()


@pytest.mark.asyncio
async def test_load_history_preserves_chronological_order() -> None:
    class FakeDB:
        def get_messages(self, conversation_id: str, limit: int):
            assert (conversation_id, limit) == ("friend", 2)
            return [
                {
                    "local_id": 2,
                    "type": "文本",
                    "sender_id": 10,
                    "sender_username": "friend",
                    "create_time": 2,
                    "content": "new",
                    "sort_seq": 2,
                },
                {
                    "local_id": 1,
                    "type": "文本",
                    "sender_id": 9,
                    "sender_username": "bot",
                    "create_time": 1,
                    "content": "old",
                    "sort_seq": 1,
                },
            ]

        def get_nickname(self, username: str) -> str:
            return "Friend"

    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._account_id = "bot"
    adapter._nickname = "Bot"
    adapter._db = FakeDB()

    history = await adapter.load_history("friend", 2)

    assert [message.content for message in history] == ["old", "new"]
    assert [message.metadata["is_self"] for message in history] == [True, False]
    assert [message.sender_name for message in history] == ["Bot", "Friend"]


def test_resolves_wechat_id_and_nickname_to_internal_username() -> None:
    class FakeDB:
        def get_sessions(self, limit: int):
            assert limit == 500
            return [{"username": "wxid_internal"}]

        def search_contact(self, value: str):
            assert value in {"visible_wechat_id", "Friend Nick"}
            return [
                {
                    "username": "wxid_internal",
                    "nick_name": "Friend Nick",
                    "remark": "",
                }
            ]

    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(
            allowed_private_ids=("visible_wechat_id", "Friend Nick")
        ),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._db = FakeDB()

    adapter._resolve_allowlisted_targets()

    assert adapter._resolved_private_ids == ("wxid_internal",)
    assert adapter._display_name_cache["wxid_internal"] == "Friend Nick"
    assert adapter._allowlist_log_labels[(ConversationType.PRIVATE, "wxid_internal")] == (
        "visible_wechat_id", "Friend Nick"
    )


def test_allowlisted_conversations_include_private_and_group_avatar_urls() -> None:
    class Connection:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, sql: str, usernames: tuple[str, ...]):
            assert "small_head_url" in sql
            assert set(usernames) == {"friend", "team@chatroom"}
            return [
                {
                    "username": "friend",
                    "small_head_url": "https://wx.qlogo.cn/private.jpg",
                },
                {
                    "username": "team@chatroom",
                    "small_head_url": "https://wx.qlogo.cn/group.jpg",
                },
            ]

        def close(self) -> None:
            self.closed = True

    class FakeDB:
        _db_files = [("contact/contact.db", "contact.db", 1)]

        def __init__(self) -> None:
            self.connection = Connection()

        def _open(self, rel: str):
            assert rel == "contact/contact.db"
            return self.connection

        def get_nickname(self, username: str) -> str:
            return {"friend": "好友", "team@chatroom": "项目群"}[username]

    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(
            allowed_private_ids=("friend",),
            allowed_group_ids=("team@chatroom",),
        ),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._account_id = "bot"
    adapter._db = FakeDB()

    conversations = adapter._list_allowlisted_conversations_blocking()

    assert [item.avatar_url for item in conversations] == [
        "https://wx.qlogo.cn/private.jpg",
        "https://wx.qlogo.cn/group.jpg",
    ]
    assert adapter._db.connection.closed is True


def test_avatar_database_failure_keeps_allowlisted_conversation() -> None:
    class Connection:
        def execute(self, _sql: str, _usernames: tuple[str, ...]):
            raise RuntimeError("database unavailable")

        def close(self) -> None:
            return None

    class FakeDB:
        _db_files = [("contact.db", "contact.db", 1)]

        def _open(self, _rel: str):
            return Connection()

        def get_nickname(self, _username: str) -> str:
            return "好友"

    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._account_id = "bot"
    adapter._db = FakeDB()

    conversations = adapter._list_allowlisted_conversations_blocking()

    assert len(conversations) == 1
    assert conversations[0].avatar_url is None


@pytest.mark.asyncio
async def test_private_sender_uses_contact_display_name_instead_of_wechat_id() -> None:
    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("wxid_internal",)),
        repository=object(),  # type: ignore[arg-type]
    )
    handled = asyncio.Event()
    received = []

    async def handler(message) -> None:
        received.append(message)
        handled.set()

    adapter._display_name_cache["wxid_internal"] = "好友备注"
    adapter._handler = handler
    adapter._loop = asyncio.get_running_loop()
    adapter._on_raw_message(
        {
            "username": "wxid_internal",
            "sender_username": "liaojipeng2012",
            "sender_id": 7,
            "content": "hello",
            "type": "文本",
            "local_id": 20,
        },
        None,
    )
    await asyncio.wait_for(handled.wait(), timeout=1)

    assert received[0].sender_id == "liaojipeng2012"
    assert received[0].sender_name == "好友备注"


def test_ambiguous_nickname_is_not_silently_bound_to_wrong_contact() -> None:
    class FakeDB:
        def get_sessions(self, limit: int):
            return []

        def search_contact(self, value: str):
            return [
                {"username": "wxid_one", "nick_name": value, "remark": ""},
                {"username": "wxid_two", "nick_name": value, "remark": ""},
            ]

    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("Same Name",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._db = FakeDB()

    adapter._resolve_allowlisted_targets()

    assert adapter._resolved_private_ids == ("Same Name",)


@pytest.mark.asyncio
async def test_send_message_delegates_to_configured_sender() -> None:
    class FakeSender:
        main_window_handle = 101

        async def enqueue(self, target, message):
            assert target.conversation_id == "friend"
            assert message.text == "hello"
            return "delivery-1"

    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._sender = FakeSender()

    delivery_id = await adapter.send_message(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )

    assert delivery_id == "delivery-1"


@pytest.mark.asyncio
async def test_quote_setting_filters_reference_by_conversation_type() -> None:
    captured = []

    class FakeSender:
        main_window_handle = 101

        async def enqueue(self, _target, message):
            captured.append(message.reply_to)
            return "delivery"

    reference = ReplyReference(
        "friend:7",
        "friend",
        ConversationType.PRIVATE,
        "wxid_friend",
        "hello",
    )
    disabled = WeChatChannelAdapter(
        WeChatChannelSettings(quote_private_replies=False),
        repository=object(),  # type: ignore[arg-type]
    )
    disabled._sender = FakeSender()
    enabled = WeChatChannelAdapter(
        WeChatChannelSettings(quote_private_replies=True),
        repository=object(),  # type: ignore[arg-type]
    )
    enabled._sender = FakeSender()
    target = ChannelTarget("bot", "friend", ConversationType.PRIVATE)

    await disabled.send_message(target, OutboundMessage("one", reply_to=reference))
    await enabled.send_message(target, OutboundMessage("two", reply_to=reference))

    assert captured == [None, reference]


@pytest.mark.parametrize(
    ("conversation_id", "rows", "conversation_type"),
    [
        (
            "friend",
            [
                {"local_id": 5, "sort_seq": 50, "content": "same", "type": "文本", "sender_id": 2},
                {"local_id": 4, "sort_seq": 40, "content": "same", "type": "文本", "sender_username": "wxid_a", "sender_id": 1},
                {"local_id": 3, "sort_seq": 30, "content": "other", "type": "文本", "sender_username": "wxid_a", "sender_id": 1},
                {"local_id": 2, "sort_seq": 20, "content": "same", "type": "文本", "sender_username": "wxid_a", "sender_id": 1},
            ],
            ConversationType.PRIVATE,
        ),
        (
            "room@chatroom",
            [
                {"local_id": 5, "sort_seq": 50, "content": "wxid_b:\nsame", "type": "文本", "sender_id": 1},
                {"local_id": 4, "sort_seq": 40, "content": "wxid_a:\nsame", "type": "文本", "sender_id": 1},
                {"local_id": 2, "sort_seq": 20, "content": "wxid_a:\nsame", "type": "文本", "sender_id": 1},
            ],
            ConversationType.GROUP,
        ),
    ],
)
def test_quote_reference_resolves_identical_message_occurrence(
    conversation_id, rows, conversation_type
) -> None:
    class FakeDB:
        def get_messages(self, requested, limit):
            assert requested == conversation_id
            assert limit == 30
            return rows

    adapter = WeChatChannelAdapter(WeChatChannelSettings(), repository=object())
    adapter._db = FakeDB()
    adapter._account_id = "bot"
    reference = ReplyReference(
        f"{conversation_id}:2",
        conversation_id,
        conversation_type,
        "wxid_a",
        "same",
        sort_seq=20,
    )

    resolved = adapter._resolve_quote_reference(reference)

    assert resolved.occurrence_from_latest == 1


def test_quote_reference_outside_recent_30_messages_is_not_resolved() -> None:
    rows = [
        {
            "local_id": local_id,
            "sort_seq": local_id,
            "content": f"message-{local_id}",
            "type": "文本",
            "sender_username": "wxid_a",
            "sender_id": 1,
        }
        for local_id in range(31, 0, -1)
    ]

    class FakeDB:
        def get_messages(self, requested, limit):
            assert requested == "friend"
            assert limit == 30
            return rows

    adapter = WeChatChannelAdapter(WeChatChannelSettings(), repository=object())
    adapter._db = FakeDB()
    adapter._account_id = "bot"
    reference = ReplyReference(
        "friend:1",
        "friend",
        ConversationType.PRIVATE,
        "wxid_a",
        "message-1",
        sort_seq=1,
    )

    with pytest.raises(LookupError, match="absent from recent 30 messages"):
        adapter._resolve_quote_reference(reference)


@pytest.mark.asyncio
async def test_resend_delivery_delegates_to_configured_sender() -> None:
    class FakeSender:
        main_window_handle = 101

        async def resend(self, delivery_id):
            assert delivery_id == "delivery-1"
            return "delivery-2"

    adapter = WeChatChannelAdapter(
        WeChatChannelSettings(allowed_private_ids=("friend",)),
        repository=object(),  # type: ignore[arg-type]
    )
    adapter._sender = FakeSender()

    assert await adapter.resend_delivery("delivery-1") == "delivery-2"


def test_serialized_wechat_db_prevents_parallel_snapshot_access(tmp_path) -> None:
    class BlockingDB:
        workdir = str(tmp_path)
        _db_files = []

        def __init__(self) -> None:
            self.first_entered = threading.Event()
            self.second_entered = threading.Event()
            self.release = threading.Event()
            self.calls = 0

        def _call(self, result):
            self.calls += 1
            if self.calls == 1:
                self.first_entered.set()
                assert self.release.wait(timeout=1)
            else:
                self.second_entered.set()
            return result

        def get_messages(self, *_args, **_kwargs):
            return self._call(["message"])

        def get_sessions(self, *_args, **_kwargs):
            return self._call(["session"])

    raw = BlockingDB()
    db = _SerializedWeChatDb(raw)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(db.get_messages, "friend")
        assert raw.first_entered.wait(timeout=1)
        second = pool.submit(db.get_sessions)
        assert raw.second_entered.wait(timeout=0.05) is False
        raw.release.set()

        assert first.result(timeout=1) == ["message"]
        assert second.result(timeout=1) == ["session"]
    assert raw.second_entered.is_set()


def test_serialized_wechat_db_rebuilds_malformed_derived_snapshot(tmp_path) -> None:
    rel = os.path.join("message", "message_1.db")
    cache = tmp_path / rel.replace(os.sep, "__")
    stamp = tmp_path / f"{rel.replace(os.sep, '__')}.stamp"
    keys = tmp_path / "keys.json"
    cache.write_bytes(b"broken")
    stamp.write_text("stamp", encoding="utf-8")
    keys.write_text("{}", encoding="utf-8")

    class MalformedOnceDB:
        workdir = str(tmp_path)
        _db_files = [(rel, "source.db", 1)]

        def __init__(self) -> None:
            self.calls = 0

        def get_new_messages(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.DatabaseError("database disk image is malformed")
            return ["recovered"]

    raw = MalformedOnceDB()
    db = _SerializedWeChatDb(raw)

    assert db.get_new_messages("friend") == ["recovered"]
    assert raw.calls == 2
    assert cache.exists() is False
    assert stamp.exists() is False
    assert keys.exists() is True


def test_serialized_wechat_db_accepts_only_unused_page_warnings(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MergeCheckingDB:
        _agent_bridge_merge_validation = False

        @staticmethod
        def _check_merged(_path: str) -> bool:
            return False

    class FakeConnection:
        def __init__(self, result: str) -> None:
            self.result = result

        def execute(self, statement: str):
            assert statement == "PRAGMA quick_check"
            return self

        def fetchall(self):
            return [(self.result,)]

        def close(self) -> None:
            return None

    raw = MergeCheckingDB()
    db = _SerializedWeChatDb(raw)
    monkeypatch.setattr(
        "agent_bridge.channels.wechat.sqlite3.connect",
        lambda *_args, **_kwargs: FakeConnection(
            "*** in database main ***\nPage 12 is never used"
        ),
    )

    assert raw._check_merged(str(tmp_path / "snapshot.db")) is True

    monkeypatch.setattr(
        "agent_bridge.channels.wechat.sqlite3.connect",
        lambda *_args, **_kwargs: FakeConnection(
            "*** in database main ***\nPage 12 is never used\nwrong # of entries"
        ),
    )
    assert raw._check_merged(str(tmp_path / "snapshot.db")) is False
    assert db._unused_page_warning_logged is True


def test_serialized_wechat_db_retries_concurrent_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConcurrentRewriteOnceDB:
        def __init__(self) -> None:
            self.calls = 0

        def get_new_messages(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    "数据库合并失败(文件被微信并发改写): message/message_0.db"
                )
            return ["recovered"]

    delays: list[float] = []
    monkeypatch.setattr("agent_bridge.channels.wechat.time.sleep", delays.append)
    raw = ConcurrentRewriteOnceDB()
    db = _SerializedWeChatDb(raw)

    assert db.get_new_messages("friend") == ["recovered"]
    assert raw.calls == 2
    assert delays == [0.05]


def test_serialized_wechat_db_defers_busy_listener_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConcurrentRewriteDB:
        def __init__(self) -> None:
            self.calls = 0

        def get_new_messages(self, *_args, **_kwargs):
            self.calls += 1
            raise RuntimeError(
                "数据库合并失败(文件被微信并发改写): message/message_0.db"
            )

    delays: list[float] = []
    monkeypatch.setattr("agent_bridge.channels.wechat.time.sleep", delays.append)
    raw = ConcurrentRewriteDB()
    db = _SerializedWeChatDb(raw)

    assert db.get_new_messages("friend") == []
    assert raw.calls == 3
    assert delays == [0.05, 0.1]


def test_serialized_wechat_db_raises_busy_non_listener_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConcurrentRewriteDB:
        def __init__(self) -> None:
            self.calls = 0

        def get_messages(self, *_args, **_kwargs):
            self.calls += 1
            raise RuntimeError(
                "数据库合并失败(文件被微信并发改写): message/message_0.db"
            )

    monkeypatch.setattr("agent_bridge.channels.wechat.time.sleep", lambda _delay: None)
    raw = ConcurrentRewriteDB()
    db = _SerializedWeChatDb(raw)

    with pytest.raises(RuntimeError, match="数据库合并失败"):
        db.get_messages("friend")
    assert raw.calls == 3


def test_serialized_wechat_db_does_not_swallow_other_runtime_errors() -> None:
    class BrokenDB:
        def __init__(self) -> None:
            self.calls = 0

        def get_new_messages(self, *_args, **_kwargs):
            self.calls += 1
            raise RuntimeError("unexpected database failure")

    raw = BrokenDB()
    db = _SerializedWeChatDb(raw)

    with pytest.raises(RuntimeError, match="unexpected database failure"):
        db.get_new_messages("friend")
    assert raw.calls == 1


def test_serialized_wechat_db_closes_unused_message_shards() -> None:
    class FakeConnection:
        def __init__(self, name: str) -> None:
            self.name = name
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class ShardedDB:
        def __init__(self) -> None:
            self.connections: dict[str, FakeConnection] = {}
            self._agent_bridge_msg_conn_cleanup = False

        def _message_dbs(self):
            return ["message/message_0.db", "message/message_1.db"]

        def _open(self, rel: str):
            connection = FakeConnection(rel)
            self.connections[rel] = connection
            return connection

        def _find_msg_table(self, _user: str, conns):
            return conns[0], "Msg_target"

        def _msg_conn(self, _user: str):  # pragma: no cover - replaced at init
            raise AssertionError("cleanup wrapper was not installed")

    raw = ShardedDB()
    db = _SerializedWeChatDb(raw)

    selected, table = raw._msg_conn("friend")

    assert table == "Msg_target"
    assert selected.closed is False
    assert raw.connections["message/message_1.db"].closed is True


def test_serialized_wechat_db_does_not_retry_non_corruption_error(tmp_path) -> None:
    class LockedDB:
        workdir = str(tmp_path)
        _db_files = []

        def __init__(self) -> None:
            self.calls = 0

        def get_messages(self, *_args, **_kwargs):
            self.calls += 1
            raise sqlite3.DatabaseError("database is locked")

    raw = LockedDB()
    db = _SerializedWeChatDb(raw)

    with pytest.raises(sqlite3.DatabaseError, match="database is locked"):
        db.get_messages("friend")
    assert raw.calls == 1
