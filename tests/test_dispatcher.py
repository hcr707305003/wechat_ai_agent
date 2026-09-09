import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from agent_bridge.agents.factory import AgentFactory
from agent_bridge.models import (
    Attachment,
    ContentType,
    ConversationPreferences,
    ConversationType,
    ProviderRun,
    UnifiedMessage,
)
from agent_bridge.runtime.dispatcher import Dispatcher, DispatcherSettings
from agent_bridge.runtime.queue import SessionTaskQueue
from agent_bridge.sessions.context import ContextBuilder
from agent_bridge.sessions.repository import SQLiteRepository
from agent_bridge.sessions.resolver import SessionResolver
from agent_bridge.testing import (
    FakeAgentAdapter,
    FakeAgentParser,
    FakeChannelAdapter,
)
from agent_bridge.tools.desktop import WindowInfo


def message(message_id: str, content: str, conversation: str = "friend") -> UnifiedMessage:
    return UnifiedMessage(
        channel="fake",
        channel_account_id="bot",
        conversation_id=conversation,
        conversation_type=ConversationType.PRIVATE,
        sender_id="user",
        message_id=message_id,
        content=content,
    )


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_dispatcher_creates_one_logical_session_and_switches_agent(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    codex = FakeAgentAdapter("codex", "codex")
    claude = FakeAgentAdapter("claude", "claude")
    factory.register("codex", lambda: codex, lambda: FakeAgentParser("codex"))
    factory.register("claude", lambda: claude, lambda: FakeAgentParser("claude"))
    resolver = SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),))
    dispatcher = Dispatcher(
        channel,
        repository,
        resolver,
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(2),
        DispatcherSettings(timeout_seconds=2),
    )
    await channel.start(dispatcher.handle)

    await channel.emit(message("m1", "hello"))
    await channel.emit(message("m2", "/agent claude"))
    await channel.emit(message("m3", "continue"))

    sessions = repository.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].current_provider == "claude"
    assert [request.prompt for request in codex.requests] == ["hello"]
    assert [request.prompt for request in claude.requests] == ["continue"]
    assert claude.requests[0].include_context is True
    assert any("claude: continue" in item.message.text for item in channel.sent)
    assistant_quotes = {
        row["metadata"]["bridge_quote"]["message_id"]
        for row in repository.session_messages(sessions[0].id)
        if row["role"] == "assistant" and "bridge_quote" in row["metadata"]
    }
    assert assistant_quotes == {"m1", "m3"}

    await channel.stop()
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_dispatcher_uses_agent_to_choose_ambiguous_desktop_window(
    tmp_path: Path,
) -> None:
    class SelectorAgent(FakeAgentAdapter):
        async def run(self, native, request, on_text_delta=None):
            self.requests.append(request)
            return ProviderRun(
                raw_events=(),
                final_result={"text": '{"index": 1}'},
                native_session_id=native.native_session_id,
            )

    repository = SQLiteRepository(tmp_path / "bridge.db")
    factory = AgentFactory()
    agent = SelectorAgent("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        FakeChannelAdapter(),
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
    )
    candidates = (
        WindowInfo(101, "QQ", "QQ.exe"),
        WindowInfo(202, "main.py - Visual Studio Code", "Code.exe"),
    )

    selected = await dispatcher.select_desktop_window(
        message("select", "截图开发工具"), "开发工具", candidates
    )

    assert selected is not None and selected.hwnd == 202
    assert '"index": 1' in agent.requests[0].prompt
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_dispatcher_deduplicates_inbound_message(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    resolver = SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),))
    dispatcher = Dispatcher(
        channel,
        repository,
        resolver,
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
    )
    await channel.start(dispatcher.handle)

    duplicate = message("same", "hello")
    await channel.emit(duplicate)
    await channel.emit(duplicate)

    assert len(agent.requests) == 1
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_dispatcher_streams_before_enqueuing_final_reply(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex", stream_chunks=("reply", ": hello"))
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
    )
    progress = []

    def capture(update) -> None:
        if update.state == "completed":
            assert channel.sent == []
        progress.append(update)

    dispatcher.subscribe_progress(capture)
    await channel.start(dispatcher.handle)

    await channel.emit(message("m1", "hello"))

    assert [update.state for update in progress] == [
        "queued",
        "started",
        "streaming",
        "completed",
    ]
    assert progress[2].text == "reply"
    assert progress[-1].text == "reply: hello"
    assert len(channel.sent) == 1
    assert channel.sent[0].message.text == "reply: hello"
    assert channel.sent[0].message.metadata["idempotency_key"].startswith(
        "agent_reply:job_"
    )
    assert channel.sent[0].message.reply_to is not None
    assert channel.sent[0].message.reply_to.message_id == "m1"
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_dispatcher_prefixes_only_completed_agent_text(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex", stream_chunks=("reply", ": hello"))
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(reply_prefix="[ai回复]"),
    )
    progress = []
    dispatcher.subscribe_progress(progress.append)
    await channel.start(dispatcher.handle)

    await channel.emit(message("prefixed", "hello"))

    assert next(item for item in progress if item.state == "streaming").text == "reply"
    assert progress[-1].text == "[ai回复]reply: hello"
    assert [item.message.text for item in channel.sent] == ["[ai回复]reply: hello"]
    rows = repository.session_messages(repository.list_sessions()[0].id)
    assistant = next(row for row in rows if row["role"] == "assistant")
    assert assistant["content"] == "reply: hello"
    assert assistant["metadata"]["bridge_display_content"] == (
        "[ai回复]reply: hello"
    )
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_split_replies_all_quote_the_same_inbound_message(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex", response_prefix="abcdefgh")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(max_reply_chars=8, reply_prefix="[ai]"),
    )
    await channel.start(dispatcher.handle)

    await channel.emit(message("split-source", "hello"))

    assert len(channel.sent) > 1
    assert {
        item.message.reply_to.message_id
        for item in channel.sent
        if item.message.reply_to is not None
    } == {"split-source"}
    assert all(item.message.reply_to is not None for item in channel.sent)
    assert all(item.message.text.startswith("[ai]") for item in channel.sent)
    assert all(item.message.text.count("[ai]") == 1 for item in channel.sent)
    assert all(len(item.message.text) <= 8 for item in channel.sent)
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_dispatcher_failure_updates_original_card_without_sending_error(
    tmp_path: Path,
) -> None:
    class FailingAgent(FakeAgentAdapter):
        async def run(self, native, request, on_text_delta=None):
            if on_text_delta is not None:
                await on_text_delta("partial")
            raise RuntimeError("turn completed event not received")

    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    factory.register(
        "codex", lambda: FailingAgent("codex"), lambda: FakeAgentParser("codex")
    )
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
    )
    progress = []
    dispatcher.subscribe_progress(progress.append)
    await channel.start(dispatcher.handle)

    await channel.emit(message("m1", "hello"))

    assert [update.state for update in progress] == [
        "queued",
        "started",
        "streaming",
        "failed",
    ]
    assert progress[-1].text == "partial"
    assert progress[-1].detail == "Agent 执行失败：turn completed event not received"
    assert channel.sent == []
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_dispatcher_turns_enabled_agent_image_directive_into_attachment(
    tmp_path: Path,
) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    class ImageAgent(FakeAgentAdapter):
        async def run(self, native, request, on_text_delta=None):
            self.requests.append(request)
            text = (
                "处理完成。\n"
                '<agent_bridge_image>{"path":"result.png","alt":"结果图"}'
                "</agent_bridge_image>"
            )
            return ProviderRun(
                ({"type": "assistant_message", "text": text},),
                {"text": text},
                native.native_session_id,
            )

    repository = SQLiteRepository(tmp_path / "bridge.db")
    repository.set_conversation_preferences(
        ConversationPreferences(
            "fake",
            "bot",
            "friend",
            ConversationType.PRIVATE,
            reply_enabled=True,
            send_images_enabled=True,
        )
    )
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = ImageAgent("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
    )
    await channel.start(dispatcher.handle)

    await channel.emit(message("image-1", "给我结果图"))

    assert "agent_bridge_image" in agent.requests[0].transport_instructions
    assert [item.message.text for item in channel.sent] == ["处理完成。"]
    assert channel.sent[0].message.attachments[0].path == str(image.resolve())
    assert channel.sent[0].message.metadata["idempotency_key"].endswith(":0")
    assert channel.sent[0].message.reply_to is not None
    assert channel.sent[0].message.reply_to.message_id == "image-1"
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_dispatcher_does_not_prefix_image_only_delivery(tmp_path: Path) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    class ImageAgent(FakeAgentAdapter):
        async def run(self, native, request, on_text_delta=None):
            self.requests.append(request)
            text = (
                '<agent_bridge_image>{"path":"result.png","alt":"结果图"}'
                "</agent_bridge_image>"
            )
            return ProviderRun(
                ({"type": "assistant_message", "text": text},),
                {"text": text},
                native.native_session_id,
            )

    repository = SQLiteRepository(tmp_path / "bridge.db")
    repository.set_conversation_preferences(
        ConversationPreferences(
            "fake",
            "bot",
            "friend",
            ConversationType.PRIVATE,
            reply_enabled=True,
            send_images_enabled=True,
        )
    )
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = ImageAgent("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(reply_prefix="[ai回复]"),
    )
    await channel.start(dispatcher.handle)

    await channel.emit(message("image-only-output", "给我结果图"))

    assert [item.message.text for item in channel.sent] == ["[图片] 结果图"]
    assert channel.sent[0].message.attachments[0].path == str(image.resolve())
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_dispatcher_does_not_parse_image_directive_when_permission_is_off(
    tmp_path: Path,
) -> None:
    class ImageAgent(FakeAgentAdapter):
        async def run(self, native, request, on_text_delta=None):
            self.requests.append(request)
            text = '<agent_bridge_image>{"path":"result.png"}</agent_bridge_image>'
            return ProviderRun((), {"text": text}, native.native_session_id)

    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = ImageAgent("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
    )
    await channel.start(dispatcher.handle)

    await channel.emit(message("image-off", "测试"))

    assert agent.requests[0].transport_instructions == ""
    assert len(channel.sent) == 1
    assert channel.sent[0].message.attachments == ()
    assert "agent_bridge_image" in channel.sent[0].message.text
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("image_first", [True, False])
async def test_dispatcher_batches_text_and_image_and_quotes_text(
    tmp_path: Path, image_first: bool
) -> None:
    image_path = tmp_path / "incoming.png"
    image_path.write_bytes(b"png")
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=0.02),
    )
    await channel.start(dispatcher.handle)
    image_message = replace(
        message("image-message", "[图片]"),
        content_type=ContentType.IMAGE,
        attachments=(
            Attachment("image", "incoming.png", str(image_path), mime_type="image/png"),
        ),
    )
    text_message = message("text-message", "图片内容是什么")

    ordered = (image_message, text_message) if image_first else (text_message, image_message)
    for item in ordered:
        await channel.emit(item)
    await wait_until(lambda: bool(channel.sent))

    assert len(agent.requests) == 1
    assert agent.requests[0].prompt == "图片内容是什么"
    assert agent.requests[0].input_attachments[0].path == str(image_path.resolve())
    assert channel.sent[0].message.reply_to is not None
    assert channel.sent[0].message.reply_to.message_id == "text-message"
    assert len(repository.session_messages(repository.list_sessions()[0].id)) == 3
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_image_only_batch_uses_newest_image_as_quote(tmp_path: Path) -> None:
    image_path = tmp_path / "incoming.png"
    image_path.write_bytes(b"png")
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=0.01),
    )
    await channel.start(dispatcher.handle)
    inbound = replace(
        message("image-only", "[图片]"),
        content_type=ContentType.IMAGE,
        attachments=(Attachment("image", "incoming.png", str(image_path)),),
    )

    await channel.emit(inbound)
    await wait_until(lambda: bool(channel.sent))

    assert agent.requests[0].prompt == "请描述这些图片的内容。"
    assert channel.sent[0].message.reply_to is not None
    assert channel.sent[0].message.reply_to.message_id == "image-only"
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_untriggered_observed_batch_is_context_only_and_dropped_on_close(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=10),
    )

    await dispatcher.observe(message("context-only", "群聊上下文"))
    assert repository.session_messages(repository.list_sessions()[0].id)[0][
        "content"
    ] == "群聊上下文"
    await dispatcher.close()

    assert agent.requests == []
    assert channel.sent == []
    repository.close()


@pytest.mark.asyncio
async def test_later_question_receives_untriggered_group_image_context(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "group-context.png"
    image_path.write_bytes(b"png")
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=0),
    )
    await channel.start(dispatcher.handle)

    # Initialize the native session, then receive a group image that does not
    # trigger a reply.  A later question must still carry that image to Agent.
    await channel.emit(message("initial", "你好"))
    observed_image = replace(
        message("observed-image", "[图片]"),
        content_type=ContentType.IMAGE,
        attachments=(Attachment("image", "group-context.png", str(image_path)),),
    )
    await dispatcher.observe(observed_image)
    await channel.emit(message("question", "这张图片是什么内容"))

    assert len(agent.requests) == 2
    assert agent.requests[1].include_context is False
    assert agent.requests[1].input_attachments[0].path == str(image_path.resolve())
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_batch_preserves_multiple_text_and_image_order_and_quotes_newest_text(
    tmp_path: Path,
) -> None:
    first_image = tmp_path / "first.png"
    second_image = tmp_path / "second.png"
    first_image.write_bytes(b"first")
    second_image.write_bytes(b"second")
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=0.02),
    )
    await channel.start(dispatcher.handle)

    items = (
        message("same-text-1", "看看"),
        replace(
            message("first-image", "[图片]"),
            content_type=ContentType.IMAGE,
            attachments=(Attachment("image", "first.png", str(first_image)),),
        ),
        message("same-text-2", "看看"),
        replace(
            message("second-image", "[图片]"),
            content_type=ContentType.IMAGE,
            attachments=(Attachment("image", "second.png", str(second_image)),),
        ),
    )
    for item in items:
        await channel.emit(item)
    await wait_until(lambda: bool(channel.sent))

    assert len(agent.requests) == 1
    assert agent.requests[0].prompt == "看看\n看看"
    assert [item.path for item in agent.requests[0].input_attachments] == [
        str(first_image.resolve()),
        str(second_image.resolve()),
    ]
    assert channel.sent[0].message.reply_to is not None
    assert channel.sent[0].message.reply_to.message_id == "same-text-2"
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_message_batches_are_isolated_under_cross_conversation_stress(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(4),
        DispatcherSettings(message_batch_window_seconds=0.02),
    )
    await channel.start(dispatcher.handle)

    conversations = tuple(f"friend-{index}" for index in range(12))
    for index, conversation in enumerate(conversations):
        image_path = tmp_path / f"{conversation}.png"
        image_path.write_bytes(conversation.encode())
        await channel.emit(
            replace(
                message(f"image-{index}", "[图片]", conversation),
                content_type=ContentType.IMAGE,
                attachments=(Attachment("image", image_path.name, str(image_path)),),
            )
        )
    for index, conversation in reversed(tuple(enumerate(conversations))):
        await channel.emit(message(f"text-{index}", f"question-{index}", conversation))
    try:
        await wait_until(lambda: len(channel.sent) == len(conversations))

        assert len(agent.requests) == len(conversations), [
            request.prompt for request in agent.requests
        ]
        by_prompt = {request.prompt: request for request in agent.requests}
        for index, conversation in enumerate(conversations):
            request = by_prompt[f"question-{index}"]
            assert [Path(item.path).name for item in request.input_attachments] == [
                f"{conversation}.png"
            ]
            assert request.context.incoming.conversation_id == conversation
    finally:
        await dispatcher.close()
        repository.close()


@pytest.mark.asyncio
async def test_batch_deadline_extends_from_the_newest_message(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=0.15),
    )
    await channel.start(dispatcher.handle)

    await channel.emit(message("first", "第一段"))
    await asyncio.sleep(0.08)
    await channel.emit(message("second", "第二段"))
    await asyncio.sleep(0.09)
    assert agent.requests == []

    for _ in range(100):
        if agent.requests:
            break
        await asyncio.sleep(0.01)

    assert [request.prompt for request in agent.requests] == ["第一段\n第二段"]
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_missing_image_keeps_text_batch_and_reports_partial_input(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=0.01),
    )
    await channel.start(dispatcher.handle)
    missing = replace(
        message("missing-image", "[图片]"),
        content_type=ContentType.IMAGE,
        attachments=(Attachment("image", "missing.png", str(tmp_path / "missing.png")),),
    )

    await channel.emit(missing)
    await channel.emit(message("question", "图片是什么"))
    await wait_until(lambda: bool(channel.sent))

    assert len(agent.requests) == 1
    assert agent.requests[0].prompt.startswith("图片是什么")
    assert "有 1 张图片未能加载" in agent.requests[0].prompt
    assert agent.requests[0].input_attachments == ()
    assert channel.sent[0].message.reply_to is not None
    assert channel.sent[0].message.reply_to.message_id == "question"
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_command_flushes_open_batch_and_bypasses_long_debounce(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=60),
    )
    await channel.start(dispatcher.handle)

    await channel.emit(message("pending", "先回答这句"))
    await channel.emit(message("help", "/help"))

    assert [request.prompt for request in agent.requests] == ["先回答这句"]
    assert channel.sent[0].message.reply_to is not None
    assert channel.sent[0].message.reply_to.message_id == "pending"
    assert channel.sent[1].message.reply_to is not None
    assert channel.sent[1].message.reply_to.message_id == "help"
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_restart_does_not_replay_an_unsealed_inbound_batch(tmp_path: Path) -> None:
    database = tmp_path / "bridge.db"
    repository = SQLiteRepository(database)
    first_channel = FakeChannelAdapter()
    first_factory = AgentFactory()
    first_agent = FakeAgentAdapter("codex")
    first_factory.register(
        "codex", lambda: first_agent, lambda: FakeAgentParser("codex")
    )
    first_dispatcher = Dispatcher(
        first_channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        first_factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=60),
    )
    await first_channel.start(first_dispatcher.handle)

    await first_channel.emit(message("before-restart", "不能重放"))
    assert len(repository.session_messages(repository.list_sessions()[0].id)) == 1
    await first_dispatcher.close()
    assert first_agent.requests == []
    repository.close()

    reopened = SQLiteRepository(database)
    second_channel = FakeChannelAdapter()
    second_factory = AgentFactory()
    second_agent = FakeAgentAdapter("codex")
    second_factory.register(
        "codex", lambda: second_agent, lambda: FakeAgentParser("codex")
    )
    second_dispatcher = Dispatcher(
        second_channel,
        reopened,
        SessionResolver(reopened, "codex", str(tmp_path), (str(tmp_path),)),
        second_factory,
        ContextBuilder(reopened),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=0),
    )
    await second_channel.start(second_dispatcher.handle)
    await second_channel.emit(message("after-restart", "只处理新消息"))

    assert [request.prompt for request in second_agent.requests] == ["只处理新消息"]
    assert len(reopened.session_messages(reopened.list_sessions()[0].id)) == 3
    await second_dispatcher.close()
    reopened.close()


@pytest.mark.asyncio
async def test_inflight_dispatched_image_is_not_replayed_into_the_next_job(
    tmp_path: Path,
) -> None:
    class BlockingFirstAgent(FakeAgentAdapter):
        def __init__(self) -> None:
            super().__init__("codex")
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, native, request, on_text_delta=None):
            self.requests.append(request)
            if len(self.requests) == 1:
                self.started.set()
                await self.release.wait()
            text = f"reply: {request.prompt}"
            return ProviderRun(
                ({"type": "assistant_message", "text": text},),
                {"text": text},
                native.native_session_id,
            )

    image_path = tmp_path / "already-submitted.png"
    image_path.write_bytes(b"png")
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = BlockingFirstAgent()
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=0),
    )
    await channel.start(dispatcher.handle)
    first = replace(
        message("first-image-job", "[图片]"),
        content_type=ContentType.IMAGE,
        attachments=(Attachment("image", image_path.name, str(image_path)),),
    )

    first_task = asyncio.create_task(channel.emit(first))
    await asyncio.wait_for(agent.started.wait(), timeout=1)
    second_task = asyncio.create_task(
        channel.emit(message("next-question", "这是另一个问题"))
    )
    await wait_until(lambda: bool(dispatcher.queued_jobs("friend")))
    agent.release.set()
    await asyncio.gather(first_task, second_task)

    assert len(agent.requests) == 2
    assert agent.requests[0].input_attachments
    assert agent.requests[1].input_attachments == ()
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_later_question_receives_untriggered_group_text_context(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=0),
    )
    await channel.start(dispatcher.handle)

    await channel.emit(message("initial-text", "先初始化会话"))
    await dispatcher.observe(message("observed-text", "项目代号是蓝鲸"))
    await channel.emit(message("context-question", "项目代号是什么"))

    assert len(agent.requests) == 2
    assert "项目代号是蓝鲸" in agent.requests[1].prompt
    assert agent.requests[1].prompt.endswith("项目代号是什么")
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_context_in_the_triggered_batch_is_not_forwarded_again(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=0.02),
    )
    await channel.start(dispatcher.handle)

    await dispatcher.observe(message("same-batch-context", "项目代号是星河"))
    await channel.emit(message("same-batch-question", "项目代号是什么"))
    await wait_until(lambda: len(channel.sent) == 1)
    assert agent.requests[0].prompt == "项目代号是星河\n项目代号是什么"

    await channel.emit(message("later-unrelated", "现在几点"))
    await wait_until(lambda: len(channel.sent) == 2)
    assert agent.requests[1].prompt == "现在几点"
    assert agent.requests[1].input_attachments == ()
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_batch_waits_for_already_received_image_hydration(
    tmp_path: Path,
) -> None:
    class MediaAwareChannel(FakeChannelAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.wait_started = asyncio.Event()
            self.media_ready = asyncio.Event()

        async def wait_for_pending_media(self, conversation_id: str) -> None:
            assert conversation_id == "friend"
            self.wait_started.set()
            await self.media_ready.wait()

    image_path = tmp_path / "slow-image.png"
    image_path.write_bytes(b"png")
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = MediaAwareChannel()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=0.01),
    )
    await channel.start(dispatcher.handle)

    await channel.emit(message("text-before-slow-image", "解释图片"))
    await asyncio.wait_for(channel.wait_started.wait(), timeout=1)
    assert agent.requests == []
    await channel.emit(
        replace(
            message("slow-image", "[图片]"),
            content_type=ContentType.IMAGE,
            attachments=(Attachment("image", image_path.name, str(image_path)),),
        )
    )
    channel.media_ready.set()
    try:
        for _ in range(100):
            if channel.sent:
                break
            await asyncio.sleep(0.01)

        assert len(agent.requests) == 1
        assert agent.requests[0].prompt == "解释图片"
        assert [item.path for item in agent.requests[0].input_attachments] == [
            str(image_path.resolve())
        ]
        assert channel.sent[0].message.reply_to is not None
        assert channel.sent[0].message.reply_to.message_id == "text-before-slow-image"
    finally:
        await dispatcher.close()
        repository.close()


@pytest.mark.asyncio
async def test_context_arriving_during_active_job_is_forwarded_once_after_completion(
    tmp_path: Path,
) -> None:
    class BlockingFirstAgent(FakeAgentAdapter):
        def __init__(self) -> None:
            super().__init__("codex")
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, native, request, on_text_delta=None):
            self.requests.append(request)
            if len(self.requests) == 1:
                self.started.set()
                await self.release.wait()
            text = f"reply: {request.prompt}"
            return ProviderRun(
                ({"type": "assistant_message", "text": text},),
                {"text": text},
                native.native_session_id,
            )

    image_path = tmp_path / "during-active.png"
    image_path.write_bytes(b"png")
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = BlockingFirstAgent()
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=0),
    )
    await channel.start(dispatcher.handle)

    active_task = asyncio.create_task(channel.emit(message("active", "慢任务")))
    await asyncio.wait_for(agent.started.wait(), timeout=1)
    await dispatcher.observe(message("during-text", "上下文代号是海豚"))
    await dispatcher.observe(
        replace(
            message("during-image", "[图片]"),
            content_type=ContentType.IMAGE,
            attachments=(Attachment("image", image_path.name, str(image_path)),),
        )
    )
    agent.release.set()
    await active_task

    await channel.emit(message("question-after-active", "代号是什么"))
    second = agent.requests[1]
    assert "上下文代号是海豚" in second.prompt
    assert [item.path for item in second.input_attachments] == [
        str(image_path.resolve())
    ]

    await channel.emit(message("unrelated-later", "现在几点"))
    third = agent.requests[2]
    assert "上下文代号是海豚" not in third.prompt
    assert third.input_attachments == ()
    await dispatcher.close()
    repository.close()


@pytest.mark.asyncio
async def test_empty_text_batch_does_not_report_an_image_loading_failure(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    channel = FakeChannelAdapter()
    factory = AgentFactory()
    agent = FakeAgentAdapter("codex")
    factory.register("codex", lambda: agent, lambda: FakeAgentParser("codex"))
    dispatcher = Dispatcher(
        channel,
        repository,
        SessionResolver(repository, "codex", str(tmp_path), (str(tmp_path),)),
        factory,
        ContextBuilder(repository),
        SessionTaskQueue(1),
        DispatcherSettings(message_batch_window_seconds=0),
    )
    await channel.start(dispatcher.handle)

    await channel.emit(message("empty-after-trigger", "   "))

    assert agent.requests == []
    assert channel.sent[0].message.text == "消息内容为空，请发送文字或图片。"
    assert channel.sent[0].message.reply_to is not None
    assert channel.sent[0].message.reply_to.message_id == "empty-after-trigger"
    await dispatcher.close()
    repository.close()
