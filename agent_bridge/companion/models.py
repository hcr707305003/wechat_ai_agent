from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from agent_bridge.models import (
    Attachment,
    ContentType,
    ConversationType,
    new_id,
    utc_now,
)


@dataclass(slots=True, frozen=True)
class ConversationItem:
    channel: str
    channel_account_id: str
    conversation_id: str
    conversation_type: ConversationType
    display_name: str
    avatar_url: str | None = None

    @property
    def binding_key(self) -> tuple[str, str, str]:
        return self.channel, self.channel_account_id, self.conversation_id


@dataclass(slots=True, frozen=True)
class QuotePreview:
    sender_name: str
    content: str
    content_type: ContentType = ContentType.TEXT
    target_source_key: str | None = None
    target_created_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class TimelineEntry:
    conversation_id: str
    sender_name: str
    content: str
    direction: str
    created_at: datetime = field(default_factory=utc_now)
    historical: bool = False
    history_source: str | None = None
    delivery_id: str | None = None
    delivery_ids: tuple[str, ...] = ()
    delivery_status: str | None = None
    status_detail: str = ""
    expected_deliveries: int = 1
    source_key: str | None = None
    attachments: tuple[Attachment, ...] = ()
    quote: QuotePreview | None = None
    entry_id: str = field(default_factory=lambda: new_id("timeline"))


@dataclass(slots=True, frozen=True)
class CompanionUpdate:
    kind: str
    conversation_id: str | None = None
    detail: str = ""
