import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_bridge.models import (
    Attachment,
    ChannelTarget,
    ConversationPreferences,
    ConversationType,
    ContentType,
    EventType,
    Job,
    JobStatus,
    NativeSession,
    OutboundDeliveryStatus,
    ReplyReference,
    UnifiedEvent,
    UnifiedMessage,
    new_id,
)
from agent_bridge.sessions.repository import SQLiteRepository


def make_message(message_id: str = "m1") -> UnifiedMessage:
    return UnifiedMessage(
        channel="wechat",
        channel_account_id="bot",
        conversation_id="friend",
        conversation_type=ConversationType.PRIVATE,
        sender_id="friend",
        message_id=message_id,
        content="hello",
    )


def test_session_binding_and_native_history(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    session = repository.create_session("codex", str(tmp_path))
    message = make_message()
    repository.bind_channel(message, session.id)

    first = NativeSession(
        id=new_id("native"),
        unified_session_id=session.id,
        provider="codex",
        native_session_id="thread-1",
        working_directory=str(tmp_path),
    )
    second = NativeSession(
        id=new_id("native"),
        unified_session_id=session.id,
        provider="codex",
        native_session_id="thread-2",
        working_directory=str(tmp_path),
    )
    repository.add_native_session(first)
    repository.add_native_session(second)

    resolved = repository.find_session_for_message(message)
    active = repository.get_active_native_session(session.id, "codex")
    history = repository.list_native_sessions(session.id, "codex")

    assert resolved is not None and resolved.id == session.id
    assert active is not None and active.native_session_id == "thread-2"
    assert [item.native_session_id for item in history] == ["thread-2", "thread-1"]
    repository.close()


def test_native_session_can_be_shared_across_logical_sessions(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    first_session = repository.create_session("codex", str(tmp_path))
    second_session = repository.create_session("codex", str(tmp_path))

    for session in (first_session, second_session):
        repository.add_native_session(
            NativeSession(
                id=new_id("native"),
                unified_session_id=session.id,
                provider="codex",
                native_session_id="shared-thread",
                working_directory=str(tmp_path),
                context_initialized=True,
            )
        )

    assert (
        repository.get_active_native_session(first_session.id, "codex").native_session_id
        == "shared-thread"
    )
    assert (
        repository.get_active_native_session(second_session.id, "codex").native_session_id
        == "shared-thread"
    )
    repository.close()


def test_migrates_legacy_native_session_unique_constraint(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE unified_sessions (
            id TEXT PRIMARY KEY,
            current_provider TEXT NOT NULL,
            working_directory TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE native_sessions (
            id TEXT PRIMARY KEY,
            unified_session_id TEXT NOT NULL REFERENCES unified_sessions(id),
            provider TEXT NOT NULL,
            native_session_id TEXT NOT NULL,
            working_directory TEXT NOT NULL,
            model TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            is_active INTEGER NOT NULL DEFAULT 1,
            context_initialized INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (provider, native_session_id)
        );
        CREATE INDEX idx_native_active
            ON native_sessions(unified_session_id, provider, is_active);
        """
    )
    connection.close()

    repository = SQLiteRepository(path)
    first_session = repository.create_session("codex", str(tmp_path))
    second_session = repository.create_session("codex", str(tmp_path))
    for session in (first_session, second_session):
        repository.add_native_session(
            NativeSession(
                id=new_id("native"),
                unified_session_id=session.id,
                provider="codex",
                native_session_id="shared-thread",
                working_directory=str(tmp_path),
            )
        )

    assert repository.find_native_session("codex", "shared-thread") is not None
    repository.close()


def test_message_dedup_is_durable(tmp_path: Path) -> None:
    path = tmp_path / "bridge.db"
    repository = SQLiteRepository(path)
    assert repository.mark_inbound_processed("wechat", "m1") is True
    assert repository.mark_inbound_processed("wechat", "m1") is False
    repository.close()

    reopened = SQLiteRepository(path)
    assert reopened.mark_inbound_processed("wechat", "m1") is False
    reopened.close()


def test_restart_interrupts_running_and_queued_jobs_but_keeps_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bridge.db"
    repository = SQLiteRepository(path)
    session = repository.create_session("codex", str(tmp_path))
    inbound = make_message("restart-1")
    repository.bind_channel(inbound, session.id)
    repository.add_inbound_message(session.id, inbound)
    repository.create_job(
        Job("job-running", session.id, "codex", inbound.message_id, JobStatus.RUNNING)
    )
    repository.create_job(
        Job("job-queued", session.id, "codex", "restart-2", JobStatus.QUEUED)
    )
    repository.create_job(
        Job("job-done", session.id, "codex", "restart-3", JobStatus.COMPLETED)
    )

    assert repository.interrupt_running_jobs() == 2
    rows = repository._connection.execute(
        "SELECT id, status, error FROM jobs ORDER BY id"
    ).fetchall()
    assert [(row["id"], row["status"], row["error"]) for row in rows] == [
        ("job-done", "completed", None),
        ("job-queued", "interrupted", "service_restarted"),
        ("job-running", "interrupted", "service_restarted"),
    ]
    assert repository.session_messages(session.id)[0]["content"] == "hello"
    repository.close()


def test_inbound_message_persists_attachment_metadata(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    session = repository.create_session("codex", str(tmp_path))
    image = Attachment("image", "incoming.png", str(tmp_path / "incoming.png"))
    message = UnifiedMessage(
        channel="wechat",
        channel_account_id="bot",
        conversation_id="friend",
        conversation_type=ConversationType.PRIVATE,
        sender_id="friend",
        message_id="image-1",
        content="[图片]",
        content_type=ContentType.IMAGE,
        attachments=(image,),
    )
    repository.add_inbound_message(session.id, message)

    rows = repository.session_messages(session.id)
    assert rows[0]["metadata"]["bridge_attachments"][0]["path"] == image.path
    repository.close()


def test_rollup_preserves_recent_messages(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    session = repository.create_session("codex", str(tmp_path))
    for index in range(5):
        repository.add_inbound_message(session.id, make_message(f"m{index}"))

    summary = repository.rollup_summary(session.id, retain=2)
    recent = repository.recent_messages(session.id, limit=2)

    assert summary.count("- user:") == 3
    assert [item["content"] for item in recent] == ["hello", "hello"]
    repository.close()


def test_clear_session_messages_keeps_binding_and_resets_summary(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    session = repository.create_session("codex", str(tmp_path))
    inbound = make_message()
    repository.bind_channel(inbound, session.id)
    repository.add_inbound_message(session.id, inbound)
    repository.add_event(
        session.id,
        UnifiedEvent(EventType.ASSISTANT_MESSAGE, "你好", "codex"),
    )
    repository.update_summary(session.id, "旧摘要")

    removed = repository.clear_session_messages(session.id)

    assert removed == 2
    assert repository.session_messages(session.id) == []
    resolved = repository.find_session_for_message(inbound)
    assert resolved is not None and resolved.id == session.id
    assert repository.get_session(session.id).summary == ""
    repository.close()


def test_conversation_preferences_default_off_and_persist(tmp_path: Path) -> None:
    path = tmp_path / "bridge.db"
    repository = SQLiteRepository(path)
    defaults = repository.get_conversation_preferences(
        "wechat", "bot", "friend", ConversationType.PRIVATE
    )

    assert defaults.reply_enabled is False
    assert defaults.send_images_enabled is False
    assert defaults.load_history is False
    assert defaults.history_limit == 50

    repository.set_conversation_preferences(
        ConversationPreferences(
            "wechat",
            "bot",
            "friend",
            ConversationType.PRIVATE,
            reply_enabled=True,
            send_images_enabled=True,
            load_history=True,
            history_limit=120,
        )
    )
    repository.close()

    reopened = SQLiteRepository(path)
    saved = reopened.get_conversation_preferences(
        "wechat", "bot", "friend", ConversationType.PRIVATE
    )
    assert saved.reply_enabled is True
    assert saved.send_images_enabled is True
    assert saved.load_history is True
    assert saved.history_limit == 120
    reopened.close()


def test_image_delivery_persists_attachment_and_survives_retry(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    target = ChannelTarget("bot", "friend", ConversationType.PRIVATE)
    attachment = Attachment(
        "image",
        name="result.png",
        path=str(tmp_path / "result.png"),
        mime_type="image/png",
        metadata={"alt": "结果图"},
    )
    original = repository.enqueue_outbound_delivery(
        target,
        "[图片] 结果图",
        "job:image:1",
        600,
        content_type=ContentType.IMAGE,
        attachments=(attachment,),
    )
    repository.update_outbound_delivery(
        original.id, OutboundDeliveryStatus.FAILED, completed_at=original.created_at
    )

    loaded = repository.get_outbound_delivery(original.id)
    retried = repository.retry_outbound_delivery(original.id, 600)

    assert loaded is not None and loaded.content_type == ContentType.IMAGE
    assert loaded.attachments == (attachment,)
    assert retried.content_type == ContentType.IMAGE
    assert retried.attachments == (attachment,)
    repository.close()


def test_reply_reference_persists_and_survives_retry_and_resend(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    target = ChannelTarget("bot", "friend", ConversationType.PRIVATE)
    reference = ReplyReference(
        message_id="friend:42",
        conversation_id="friend",
        conversation_type=ConversationType.PRIVATE,
        sender_id="wxid_friend",
        sender_name="Friend",
        content="same",
        sort_seq=420,
        occurrence_from_latest=2,
    )
    original = repository.enqueue_outbound_delivery(
        target, "reply", "job:quote:1", 600, reply_to=reference
    )
    repository.update_outbound_delivery(
        original.id, OutboundDeliveryStatus.FAILED, completed_at=original.created_at
    )
    retried = repository.retry_outbound_delivery(original.id, 600)
    repository.update_outbound_delivery(
        retried.id, OutboundDeliveryStatus.SENT, completed_at=retried.created_at
    )
    resent = repository.resend_outbound_delivery(retried.id, 600)

    assert repository.get_outbound_delivery(original.id).reply_to == reference
    assert retried.reply_to == reference
    assert resent.reply_to == reference
    repository.close()


def test_outbound_delivery_is_idempotent_and_fifo(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    target = ChannelTarget("bot", "friend", ConversationType.PRIVATE)

    first = repository.enqueue_outbound_delivery(target, "one", "job:1", 600, now=now)
    duplicate = repository.enqueue_outbound_delivery(
        target, "ignored", "job:1", 600, now=now + timedelta(seconds=1)
    )
    second = repository.enqueue_outbound_delivery(
        target, "two", "job:2", 600, now=now + timedelta(seconds=2)
    )

    claimed = repository.claim_next_outbound_delivery(now=now + timedelta(seconds=3))

    assert duplicate.id == first.id
    assert duplicate.text == "one"
    assert claimed is not None and claimed.id == first.id
    assert claimed.status == OutboundDeliveryStatus.WAITING_FOR_IDLE
    assert second.id != first.id
    repository.close()


def test_outbound_delivery_expiry_and_restart_recovery(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    target = ChannelTarget("bot", "friend", ConversationType.PRIVATE)
    expired = repository.enqueue_outbound_delivery(target, "old", "job:old", 5, now=now)
    active = repository.enqueue_outbound_delivery(target, "new", "job:new", 600, now=now)
    repository.update_outbound_delivery(active.id, OutboundDeliveryStatus.SENDING)

    assert repository.recover_outbound_deliveries() == 1
    claimed = repository.claim_next_outbound_delivery(now=now + timedelta(seconds=10))

    assert repository.get_outbound_delivery(expired.id).status == OutboundDeliveryStatus.EXPIRED
    assert claimed is not None and claimed.id == active.id
    repository.close()


def test_restart_discards_pending_outbound_deliveries(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    target = ChannelTarget("bot", "friend", ConversationType.PRIVATE)
    deliveries = [
        repository.enqueue_outbound_delivery(target, f"reply-{index}", f"job:{index}", 600)
        for index in range(4)
    ]
    repository.update_outbound_delivery(
        deliveries[1].id, OutboundDeliveryStatus.WAITING_FOR_IDLE
    )
    repository.update_outbound_delivery(
        deliveries[2].id,
        OutboundDeliveryStatus.RETRY_WAIT,
        next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=10),
    )
    repository.update_outbound_delivery(
        deliveries[3].id, OutboundDeliveryStatus.SENDING
    )

    assert repository.discard_pending_outbound_deliveries() == 4
    for delivery in deliveries:
        current = repository.get_outbound_delivery(delivery.id)
        assert current is not None
        assert current.status == OutboundDeliveryStatus.EXPIRED
        assert current.error_code == "service_restarted"
    assert repository.claim_next_outbound_delivery() is None
    repository.close()


def test_failed_outbound_delivery_can_be_retried_as_new_record(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    target = ChannelTarget("bot", "friend", ConversationType.PRIVATE)
    original = repository.enqueue_outbound_delivery(target, "hello", "job:1", 600)
    repository.update_outbound_delivery(
        original.id, OutboundDeliveryStatus.FAILED, completed_at=original.created_at
    )

    retried = repository.retry_outbound_delivery(original.id, 600)

    assert retried.id != original.id
    assert retried.text == original.text
    assert retried.status == OutboundDeliveryStatus.QUEUED
    assert repository.get_outbound_delivery(original.id).status == OutboundDeliveryStatus.FAILED
    repository.close()


def test_sent_outbound_delivery_can_be_sent_again_as_new_record(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    target = ChannelTarget("bot", "friend", ConversationType.PRIVATE)
    original = repository.enqueue_outbound_delivery(target, "hello", "job:1", 600)
    repository.update_outbound_delivery(
        original.id, OutboundDeliveryStatus.SENT, completed_at=original.created_at
    )

    resent = repository.resend_outbound_delivery(original.id, 600)

    assert resent.id != original.id
    assert resent.text == original.text
    assert resent.status == OutboundDeliveryStatus.QUEUED
    assert repository.get_outbound_delivery(original.id).status == OutboundDeliveryStatus.SENT
    repository.close()


def test_retry_delay_does_not_allow_newer_delivery_to_overtake(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    target = ChannelTarget("bot", "friend", ConversationType.PRIVATE)
    first = repository.enqueue_outbound_delivery(target, "one", "job:1", 600, now=now)
    repository.update_outbound_delivery(
        first.id,
        OutboundDeliveryStatus.RETRY_WAIT,
        next_attempt_at=now + timedelta(seconds=30),
    )
    repository.enqueue_outbound_delivery(
        target, "two", "job:2", 600, now=now + timedelta(seconds=1)
    )

    assert repository.claim_next_outbound_delivery(now=now + timedelta(seconds=2)) is None
    claimed = repository.claim_next_outbound_delivery(now=now + timedelta(seconds=31))
    assert claimed is not None and claimed.id == first.id
    repository.close()
