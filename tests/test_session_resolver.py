from pathlib import Path

from agent_bridge.models import (
    ConversationType,
    SessionBindingConfig,
    UnifiedMessage,
)
from agent_bridge.sessions.repository import SQLiteRepository
from agent_bridge.sessions.resolver import SessionResolver


def _message(
    conversation_id: str, conversation_type: ConversationType
) -> UnifiedMessage:
    return UnifiedMessage(
        channel="wechat",
        channel_account_id="bot",
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        sender_id="user",
        message_id=f"message-{conversation_id}",
        content="hello",
    )


def test_resolver_applies_multiple_session_bindings(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    resolver = SessionResolver(
        repository,
        "codex",
        str(tmp_path),
        (str(tmp_path),),
        (
            SessionBindingConfig(
                "wxid_private", "codex", "thread-private", ConversationType.PRIVATE
            ),
            SessionBindingConfig(
                "123@chatroom", "codex", "thread-private", ConversationType.GROUP
            ),
        ),
    )

    private, private_created = resolver.resolve(
        _message("wxid_private", ConversationType.PRIVATE)
    )
    group, group_created = resolver.resolve(
        _message("123@chatroom", ConversationType.GROUP)
    )

    assert private_created is True
    assert private.current_provider == "codex"
    assert repository.get_active_native_session(private.id, "codex").native_session_id == (
        "thread-private"
    )
    assert group_created is True
    assert group.current_provider == "codex"
    assert repository.get_active_native_session(group.id, "codex").native_session_id == (
        "thread-private"
    )

    repository.close()


def test_resolver_reapplies_binding_to_existing_session_once(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    binding = SessionBindingConfig(
        "wxid_private", "codex", "thread-private", ConversationType.PRIVATE
    )
    resolver = SessionResolver(
        repository, "codex", str(tmp_path), (str(tmp_path),), (binding,)
    )
    message = _message("wxid_private", ConversationType.PRIVATE)

    first, _ = resolver.resolve(message)
    second, created = resolver.resolve(
        _message("wxid_private", ConversationType.PRIVATE)
    )

    assert created is False
    assert second.id == first.id
    assert len(repository.list_native_sessions(first.id, "codex")) == 1

    repository.close()
