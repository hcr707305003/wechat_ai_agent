import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_bridge.agents.claude import ClaudeAdapter
from agent_bridge.agents.codex import CodexAdapter, CodexAdapterSettings
from agent_bridge.models import (
    AgentContext,
    AgentRequest,
    Attachment,
    ConversationType,
    NativeSession,
    UnifiedMessage,
)


def test_codex_sdk_exports_expected_security_options() -> None:
    pytest.importorskip("openai_codex")
    options = CodexAdapter()._sdk_options()

    assert options["sandbox"].value == "workspace-write"
    assert options["approval_mode"].value == "deny_all"


def test_claude_partial_text_accepts_only_text_delta() -> None:
    StreamEvent = type("StreamEvent", (), {})
    item = StreamEvent()
    item.event = {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": "hello"},
    }

    assert ClaudeAdapter._partial_text(item) == "hello"

    item.event["delta"] = {"type": "thinking_delta", "thinking": "hidden"}
    assert ClaudeAdapter._partial_text(item) == ""


@pytest.mark.asyncio
async def test_codex_stream_collects_delta_and_final_result() -> None:
    from openai_codex.generated.v2_all import (
        AgentMessageDeltaNotification,
        AgentMessageThreadItem,
        ItemCompletedNotification,
        MessagePhase,
        ThreadItem,
        Turn,
        TurnCompletedNotification,
        TurnStatus,
    )
    from openai_codex.models import Notification

    message = AgentMessageThreadItem(
        id="item-1",
        text="final",
        phase=MessagePhase.final_answer,
        type="agentMessage",
    )
    item = ThreadItem(root=message)
    completed = Turn(id="turn-1", items=[], status=TurnStatus.completed)
    events = (
        Notification(
            method="future/agent-message-delta",
            payload=AgentMessageDeltaNotification(
                delta="fin",
                itemId="item-1",
                threadId="thread-1",
                turnId="turn-1",
            ),
        ),
        Notification(
            method="future/item-completed",
            payload=ItemCompletedNotification(
                completedAtMs=1,
                item=item,
                threadId="thread-1",
                turnId="turn-1",
            ),
        ),
        Notification(
            method="future/turn-completed",
            payload=TurnCompletedNotification(
                threadId="thread-1",
                turn=completed,
            ),
        ),
    )

    class Turn:
        id = "turn-1"

        async def stream(self):
            for event in events:
                yield event

    deltas = []

    async def capture(delta: str) -> None:
        deltas.append(delta)

    result = await CodexAdapter._run_stream(Turn(), capture)

    assert deltas == ["fin"]
    assert result.final_response == "final"


@pytest.mark.asyncio
async def test_codex_capacity_error_falls_back_and_remembers_model() -> None:
    selected_models = []

    class Turn:
        def __init__(self, model: str | None) -> None:
            self.id = f"turn-{len(selected_models)}"
            self.model = model

        async def run(self):
            if self.model == "gpt-5.6-sol":
                raise RuntimeError(
                    "Selected model is at capacity. Please try a different model."
                )
            return SimpleNamespace(items=[])

    class Thread:
        id = "thread-1"

        async def turn(self, _prompt: str, *, model: str | None = None):
            selected_models.append(model)
            return Turn(model)

    adapter = CodexAdapter(
        CodexAdapterSettings(
            model="gpt-5.6-sol",
            fallback_models=("gpt-5.6-terra", "gpt-5.6-luna"),
        )
    )
    adapter._threads["thread-1"] = Thread()
    incoming = UnifiedMessage(
        channel="wechat",
        channel_account_id="bot",
        conversation_id="friend",
        conversation_type=ConversationType.PRIVATE,
        sender_id="user",
        message_id="message-1",
        content="hello",
    )
    request = AgentRequest(
        "hello", AgentContext("", (), incoming), "job-1"
    )
    native = NativeSession(
        "native-1", "session-1", "codex", "thread-1", "."
    )

    await adapter.run(native, request)
    await adapter.run(native, request)

    assert selected_models == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-terra",
    ]


@pytest.mark.asyncio
async def test_codex_non_capacity_error_does_not_switch_model() -> None:
    selected_models = []

    class Turn:
        id = "turn-1"

        async def run(self):
            raise RuntimeError("authentication failed")

    class Thread:
        id = "thread-1"

        async def turn(self, _prompt: str, *, model: str | None = None):
            selected_models.append(model)
            return Turn()

    adapter = CodexAdapter(
        CodexAdapterSettings(
            model="gpt-5.6-sol", fallback_models=("gpt-5.6-terra",)
        )
    )
    adapter._threads["thread-1"] = Thread()
    incoming = UnifiedMessage(
        channel="wechat",
        channel_account_id="bot",
        conversation_id="friend",
        conversation_type=ConversationType.PRIVATE,
        sender_id="user",
        message_id="message-1",
        content="hello",
    )
    request = AgentRequest(
        "hello", AgentContext("", (), incoming), "job-1"
    )
    native = NativeSession(
        "native-1", "session-1", "codex", "thread-1", "."
    )

    with pytest.raises(RuntimeError, match="authentication failed"):
        await adapter.run(native, request)

    assert selected_models == ["gpt-5.6-sol"]


@pytest.mark.asyncio
async def test_codex_resume_unarchives_archived_session() -> None:
    calls = []

    class Client:
        async def thread_resume(self, thread_id: str, **_kwargs):
            calls.append(("resume", thread_id))
            if len(calls) == 1:
                raise RuntimeError(
                    f"JSON-RPC error -32600: session {thread_id} is archived"
                )
            return SimpleNamespace(id=thread_id)

        async def thread_unarchive(self, thread_id: str):
            calls.append(("unarchive", thread_id))
            return SimpleNamespace(id=thread_id)

    adapter = CodexAdapter()
    client = Client()

    async def get_client():
        return client

    adapter._get_client = get_client
    adapter._sdk_options = lambda: {}
    native = NativeSession("native-1", "session-1", "codex", "thread-1", ".")

    await adapter.resume_session(native)

    assert calls == [
        ("resume", "thread-1"),
        ("unarchive", "thread-1"),
        ("resume", "thread-1"),
    ]
    assert adapter._threads["thread-1"].id == "thread-1"


@pytest.mark.asyncio
async def test_codex_passes_local_images_as_native_turn_inputs(tmp_path: Path) -> None:
    from openai_codex import LocalImageInput, TextInput

    captured = []

    class Turn:
        id = "turn-image"

        async def run(self):
            return SimpleNamespace(items=[])

    class Thread:
        id = "thread-image"

        async def turn(self, value, *, model=None):
            captured.append(value)
            return Turn()

    image = tmp_path / "incoming.png"
    image.write_bytes(b"png")
    incoming = UnifiedMessage(
        "wechat",
        "bot",
        "friend",
        ConversationType.PRIVATE,
        "user",
        "image-message",
        "图片内容是什么",
    )
    request = AgentRequest(
        "图片内容是什么",
        AgentContext("", (), incoming),
        "job-image",
        input_attachments=(Attachment("image", path=str(image)),),
    )
    adapter = CodexAdapter()
    adapter._threads["thread-image"] = Thread()
    native = NativeSession(
        "native-image", "session-image", "codex", "thread-image", str(tmp_path)
    )

    await adapter.run(native, request)

    assert isinstance(captured[0][0], TextInput)
    assert isinstance(captured[0][1], LocalImageInput)
    assert captured[0][1].path == str(image)


@pytest.mark.asyncio
async def test_claude_passes_images_as_base64_content_blocks(tmp_path: Path) -> None:
    captured = []

    class Options:
        def __init__(self, **values):
            self.values = values

    class ResultMessage:
        session_id = "claude-session"

    class Client:
        def __init__(self, _options):
            pass

        async def connect(self, prompt):
            async for item in prompt:
                captured.append(item)

        async def receive_response(self):
            yield ResultMessage()

        async def disconnect(self):
            pass

    image = tmp_path / "incoming.png"
    image.write_bytes(b"png-data")
    incoming = UnifiedMessage(
        "wechat",
        "bot",
        "friend",
        ConversationType.PRIVATE,
        "user",
        "image-message",
        "图片内容是什么",
    )
    request = AgentRequest(
        "图片内容是什么",
        AgentContext("", (), incoming),
        "job-image",
        input_attachments=(
            Attachment("image", path=str(image), mime_type="image/png"),
        ),
    )
    adapter = ClaudeAdapter()
    adapter._load_sdk = lambda: (Options, Client)
    native = NativeSession(
        "native-image", "session-image", "claude", "claude-session", str(tmp_path)
    )

    await adapter.run(native, request)

    content = captured[0]["message"]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image"
    assert content[1]["source"]["data"] == base64.b64encode(b"png-data").decode()

@pytest.mark.asyncio
async def test_claude_permission_guard_restricts_paths(tmp_path: Path) -> None:
    pytest.importorskip("claude_agent_sdk")
    guard = ClaudeAdapter()._permission_guard(str(tmp_path))

    allowed = await guard("Read", {"file_path": str(tmp_path / "file.txt")}, None)
    denied = await guard("Read", {"file_path": "C:/Windows/win.ini"}, None)
    bash = await guard("Bash", {"command": "whoami"}, None)

    assert type(allowed).__name__ == "PermissionResultAllow"
    assert type(denied).__name__ == "PermissionResultDeny"
    assert type(bash).__name__ == "PermissionResultDeny"
