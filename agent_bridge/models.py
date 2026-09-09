from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class ConversationType(str, Enum):
    PRIVATE = "private"
    GROUP = "group"


@dataclass(slots=True, frozen=True)
class SessionBindingConfig:
    """Configuration for attaching a channel conversation to an agent session."""

    conversation_id: str
    provider: str
    session_id: str
    conversation_type: ConversationType | None = None


class ContentType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    VOICE = "voice"
    VIDEO = "video"
    UNKNOWN = "unknown"


class EventType(str, Enum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    STATUS = "status"
    ERROR = "error"
    USAGE = "usage"
    ATTACHMENT = "attachment"
    UNKNOWN = "unknown"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class OutboundDeliveryStatus(str, Enum):
    QUEUED = "queued"
    WAITING_FOR_IDLE = "waiting_for_idle"
    SENDING = "sending"
    RETRY_WAIT = "retry_wait"
    SENT = "sent"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(slots=True, frozen=True)
class Attachment:
    kind: str
    name: str | None = None
    path: str | None = None
    url: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class UnifiedMessage:
    channel: str
    channel_account_id: str
    conversation_id: str
    conversation_type: ConversationType
    sender_id: str
    message_id: str
    content: str
    sender_name: str | None = None
    content_type: ContentType = ContentType.TEXT
    mentions: tuple[str, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    reply_to: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def binding_key(self) -> tuple[str, str, str]:
        return self.channel, self.channel_account_id, self.conversation_id


@dataclass(slots=True)
class UnifiedSession:
    id: str
    current_provider: str
    working_directory: str
    summary: str = ""
    status: str = "active"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class NativeSession:
    id: str
    unified_session_id: str
    provider: str
    native_session_id: str
    working_directory: str
    model: str | None = None
    status: str = "active"
    is_active: bool = True
    context_initialized: bool = False
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True, frozen=True)
class UnifiedEvent:
    type: EventType
    content: Any = None
    provider: str | None = None
    raw: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True, frozen=True)
class UnifiedResponse:
    text: str
    events: tuple[UnifiedEvent, ...] = ()
    native_session_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class AgentContext:
    summary: str
    recent_messages: tuple[dict[str, Any], ...]
    incoming: UnifiedMessage


@dataclass(slots=True, frozen=True)
class AgentRequest:
    prompt: str
    context: AgentContext
    job_id: str
    model: str | None = None
    include_context: bool = False
    transport_instructions: str = ""
    input_attachments: tuple[Attachment, ...] = ()


TextDeltaCallback = Callable[[str], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class AgentProgressUpdate:
    job_id: str
    conversation_id: str
    provider: str
    state: str
    text: str = ""
    chunks: tuple[str, ...] = ()
    detail: str = ""


@dataclass(slots=True, frozen=True)
class ProviderRun:
    raw_events: tuple[Any, ...]
    final_result: Any
    native_session_id: str


@dataclass(slots=True, frozen=True)
class HealthStatus:
    ok: bool
    detail: str = ""


@dataclass(slots=True, frozen=True)
class ReplyReference:
    """Stable identity of the inbound message an outbound delivery should quote."""

    message_id: str
    conversation_id: str
    conversation_type: ConversationType
    sender_id: str
    content: str
    content_type: ContentType = ContentType.TEXT
    sender_name: str | None = None
    sort_seq: int | None = None
    created_at: datetime = field(default_factory=utc_now)
    occurrence_from_latest: int | None = None

    @classmethod
    def from_message(cls, message: UnifiedMessage) -> ReplyReference:
        raw_sort_seq = message.metadata.get("sort_seq")
        try:
            sort_seq = int(raw_sort_seq) if raw_sort_seq is not None else None
        except (TypeError, ValueError):
            sort_seq = None
        return cls(
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            conversation_type=message.conversation_type,
            sender_id=message.sender_id,
            sender_name=message.sender_name,
            content=message.content,
            content_type=message.content_type,
            sort_seq=sort_seq,
            created_at=message.created_at,
        )


@dataclass(slots=True, frozen=True)
class OutboundMessage:
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: tuple[Attachment, ...] = ()
    reply_to: ReplyReference | None = None


@dataclass(slots=True, frozen=True)
class ChannelTarget:
    channel_account_id: str
    conversation_id: str
    conversation_type: ConversationType


@dataclass(slots=True, frozen=True)
class OutboundDelivery:
    id: str
    idempotency_key: str
    channel: str
    channel_account_id: str
    conversation_id: str
    conversation_type: ConversationType
    text: str
    content_type: ContentType = ContentType.TEXT
    attachments: tuple[Attachment, ...] = ()
    reply_to: ReplyReference | None = None
    status: OutboundDeliveryStatus = OutboundDeliveryStatus.QUEUED
    attempts: int = 0
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    next_attempt_at: datetime = field(default_factory=utc_now)
    expires_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class ConversationPreferences:
    channel: str
    channel_account_id: str
    conversation_id: str
    conversation_type: ConversationType
    reply_enabled: bool = False
    send_images_enabled: bool = False
    load_history: bool = False
    history_limit: int = 50

    def __post_init__(self) -> None:
        if not 1 <= self.history_limit <= 500:
            raise ValueError("history_limit must be between 1 and 500")


@dataclass(slots=True)
class Job:
    id: str
    unified_session_id: str
    provider: str
    inbound_message_id: str
    status: JobStatus = JobStatus.QUEUED
    error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
