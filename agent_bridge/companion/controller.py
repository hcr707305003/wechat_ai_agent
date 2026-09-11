from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime

from agent_bridge.companion.models import (
    CompanionUpdate,
    ConversationItem,
    QuotePreview,
    TimelineEntry,
)
from agent_bridge.models import (
    AgentProgressUpdate,
    Attachment,
    ContentType,
    ConversationPreferences,
    ConversationType,
    EventType,
    OutboundDeliveryStatus,
    UnifiedEvent,
    UnifiedMessage,
)
from agent_bridge.quotes import (
    QUOTE_METADATA_KEY,
    parse_wechat_quote_payload,
    reply_reference_metadata,
)
from agent_bridge.senders.wechat import SenderUpdate
from agent_bridge.sessions.repository import SQLiteRepository

logger = logging.getLogger(__name__)

MessageDispatcher = Callable[[UnifiedMessage], Awaitable[None]]
HistoryLoader = Callable[[str, int], Awaitable[list[UnifiedMessage]]]
HistoryPageLoader = Callable[[str, int, int], Awaitable[list[UnifiedMessage]]]
HISTORY_PAGE_SIZE = 20
UpdateSubscriber = Callable[[CompanionUpdate], None]
DeliveryRetrier = Callable[[str], Awaitable[str]]
DeliveryResender = Callable[[str], Awaitable[str]]
DeliveryCanceller = Callable[[str], Awaitable[None]]
QueuedJobsGetter = Callable[[str], tuple[tuple[str, str], ...]]
QueuedJobRemover = Callable[[str, str], Awaitable[bool]]
QueuedJobsClearer = Callable[[str], Awaitable[int]]
ContextObserver = Callable[[UnifiedMessage], Awaitable[None]]
BooleanGetter = Callable[[], bool]
BooleanSetter = Callable[[bool], None]


class CompanionController:
    def __init__(
        self,
        repository: SQLiteRepository,
        dispatcher: MessageDispatcher,
        history_loader: HistoryLoader,
        default_provider: str,
        retry_delivery: DeliveryRetrier | None = None,
        cancel_delivery: DeliveryCanceller | None = None,
        resend_delivery: DeliveryResender | None = None,
        foreground_fallback_getter: BooleanGetter | None = None,
        foreground_fallback_setter: BooleanSetter | None = None,
        queued_jobs_getter: QueuedJobsGetter | None = None,
        queued_job_remover: QueuedJobRemover | None = None,
        queued_jobs_clearer: QueuedJobsClearer | None = None,
        context_observer: ContextObserver | None = None,
        available_providers: tuple[str, ...] | None = None,
        history_page_loader: HistoryPageLoader | None = None,
        history_message_loader: Callable[
            [str, tuple[str, ...]], Awaitable[list[UnifiedMessage]]
        ]
        | None = None,
    ) -> None:
        self.repository = repository
        self.dispatcher = dispatcher
        self.history_loader = history_loader
        self.history_page_loader = history_page_loader
        self.history_message_loader = history_message_loader
        self.default_provider = default_provider
        self.available_providers = available_providers
        self._retry_delivery = retry_delivery
        self._cancel_delivery = cancel_delivery
        self._resend_delivery = resend_delivery
        self._foreground_fallback_getter = foreground_fallback_getter
        self._foreground_fallback_setter = foreground_fallback_setter
        self._queued_jobs_getter = queued_jobs_getter
        self._queued_job_remover = queued_job_remover
        self._queued_jobs_clearer = queued_jobs_clearer
        self._context_observer = context_observer
        self._realtime: dict[str, list[TimelineEntry]] = defaultdict(list)
        self._local_history: dict[str, list[TimelineEntry]] = defaultdict(list)
        self._wechat_history: dict[str, list[TimelineEntry]] = defaultdict(list)
        self._local_history_loaded: set[str] = set()
        self._local_history_loading: set[str] = set()
        self._local_history_generation: dict[str, int] = defaultdict(int)
        self._history_cursors: dict[str, tuple[str, int]] = {}
        self._local_history_more: dict[str, bool] = defaultdict(lambda: True)
        self._wechat_history_more: dict[str, bool] = defaultdict(bool)
        self._wechat_history_offsets: dict[str, int] = defaultdict(int)
        self._wechat_history_loading: set[str] = set()
        self._subscribers: list[UpdateSubscriber] = []
        self._delivery_entries: dict[str, tuple[str, int]] = {}
        self._delivery_states: dict[str, dict[str, str]] = defaultdict(dict)
        self._agent_entries: dict[str, tuple[str, int]] = {}
        self._source_entries: dict[str, tuple[str, int]] = {}

    def foreground_fallback_enabled(self) -> bool:
        if self._foreground_fallback_getter is None:
            return False
        return bool(self._foreground_fallback_getter())

    def set_foreground_fallback_enabled(self, enabled: bool) -> None:
        if self._foreground_fallback_setter is None:
            raise RuntimeError("Foreground fallback setting is unavailable")
        self._foreground_fallback_setter(bool(enabled))
        self._notify("sender_settings", detail=str(bool(enabled)).lower())

    def queued_jobs(self, conversation_id: str) -> tuple[tuple[str, str], ...]:
        if self._queued_jobs_getter is None:
            return ()
        return self._queued_jobs_getter(conversation_id)

    async def remove_queued_job(self, conversation_id: str, job_id: str) -> bool:
        if self._queued_job_remover is None:
            return False
        return await self._queued_job_remover(conversation_id, job_id)

    async def clear_queued_jobs(self, conversation_id: str) -> int:
        if self._queued_jobs_clearer is None:
            return 0
        return await self._queued_jobs_clearer(conversation_id)

    def subscribe(self, subscriber: UpdateSubscriber) -> None:
        self._subscribers.append(subscriber)

    async def handle(self, message: UnifiedMessage) -> None:
        if message.metadata.get("bridge_outbound"):
            if self._mark_matching_delivery_sent(message):
                return
        elif message.metadata.get("is_self"):
            # A combined text+image send is echoed by WeChat as two native
            # messages.  If the listener missed the in-memory pending marker,
            # recognize those echoes from the active delivery entry before
            # adding them as duplicate bubbles in the companion.
            if self._suppress_unmarked_delivery_echo(message):
                return
        self._realtime[message.conversation_id].append(
            self._timeline_entry(message, historical=False)
        )
        self._notify("timeline", message.conversation_id, "realtime")

        preferences = self.preferences_for_message(message)
        if not preferences.reply_enabled:
            logger.info(
                "会话回复已关闭，仅展示消息: conversation=%s",
                message.conversation_id,
            )
            return
        if message.metadata.get("bridge_outbound"):
            return
        if message.metadata.get("is_self") and message.conversation_id != "filehelper":
            return
        if (
            message.conversation_type == ConversationType.GROUP
            and not message.metadata.get("agent_triggered", False)
        ):
            if self._context_observer is not None and message.content_type in {
                ContentType.TEXT,
                ContentType.IMAGE,
            }:
                await self._context_observer(message)
            return
        try:
            await self.dispatcher(message)
        except Exception as error:
            self.add_system_event(
                message.conversation_id,
                f"消息处理失败：{type(error).__name__}",
            )
            raise
        self._notify("provider", message.conversation_id)

    def handle_sender_update(self, update: SenderUpdate) -> None:
        delivery = update.delivery
        if delivery is not None:
            location = self._delivery_entries.get(delivery.id)
            if location is None:
                source_key = self._delivery_source_key(delivery.idempotency_key)
                if source_key is not None:
                    location = self._source_entries.get(source_key)
            if location is None:
                entries = self._realtime[delivery.conversation_id]
                entries.append(
                    TimelineEntry(
                        delivery.conversation_id,
                        "我",
                        delivery.text,
                        "outbound",
                        created_at=delivery.created_at,
                        delivery_id=delivery.id,
                        delivery_ids=(delivery.id,),
                        delivery_status=update.state,
                        status_detail=(
                            delivery.error_message or delivery.error_code or ""
                        ),
                        attachments=delivery.attachments,
                        quote=self._quote_preview(
                            reply_reference_metadata(delivery.reply_to)
                            if delivery.reply_to is not None
                            else None,
                            channel=delivery.channel,
                        ),
                    )
                )
                self._delivery_entries[delivery.id] = (
                    delivery.conversation_id,
                    len(entries) - 1,
                )
                self._delivery_states[entries[-1].entry_id][delivery.id] = update.state
            else:
                conversation_id, index = location
                entries = self._realtime[conversation_id]
                updated_entry = self._updated_delivery_entry(
                    entries[index],
                    delivery.id,
                    update.state,
                    delivery.error_message or delivery.error_code or "",
                )
                if delivery.attachments:
                    updated_entry = replace(
                        updated_entry,
                        attachments=self._merge_attachments(
                            updated_entry.attachments, delivery.attachments
                        ),
                    )
                if delivery.reply_to is not None and updated_entry.quote is None:
                    updated_entry = replace(
                        updated_entry,
                        quote=self._quote_preview(
                            reply_reference_metadata(delivery.reply_to),
                            channel=delivery.channel,
                        ),
                    )
                entries[index] = updated_entry
                self._delivery_entries[delivery.id] = location
            self._notify("timeline", delivery.conversation_id)
        self._notify("sender", detail=update.state)

    def handle_agent_update(self, update: AgentProgressUpdate) -> None:
        if update.state == "queued":
            self._notify("queue", update.conversation_id)
            return
        location = self._agent_entries.get(update.job_id)
        if update.state == "started":
            self._notify("queue", update.conversation_id)
            entries = self._realtime[update.conversation_id]
            entries.append(
                TimelineEntry(
                    update.conversation_id,
                    update.provider.upper(),
                    "",
                    "outbound",
                    delivery_status="generating",
                    source_key=update.job_id,
                )
            )
            self._agent_entries[update.job_id] = (
                update.conversation_id,
                len(entries) - 1,
            )
            self._notify("timeline", update.conversation_id)
            return
        if location is None:
            return
        conversation_id, index = location
        entries = self._realtime[conversation_id]
        current = entries[index]
        if update.state == "streaming":
            entries[index] = replace(current, content=update.text)
            self._notify("timeline_stream", conversation_id, current.entry_id)
            return
        if update.state == "completed":
            chunks = update.chunks or (update.text,)
            entries[index] = replace(
                current,
                content=update.text,
                delivery_status="generated",
                status_detail="",
                source_key=update.job_id,
                expected_deliveries=len(chunks),
            )
            self._agent_entries.pop(update.job_id, None)
            self._notify("queue", update.conversation_id)
            for chunk_index, _chunk in enumerate(chunks):
                source_key = f"{update.job_id}:{chunk_index}"
                self._source_entries[source_key] = (conversation_id, index)
            self._notify("timeline", conversation_id)
            return
        status = "generation_failed" if update.state == "failed" else "cancelled"
        entries[index] = replace(
            current,
            content=update.text,
            delivery_status=status,
            status_detail=update.detail,
        )
        self._agent_entries.pop(update.job_id, None)
        self._notify("queue", update.conversation_id)
        self._notify("timeline", conversation_id, "realtime")

    async def retry_delivery(self, delivery_id: str) -> None:
        if self._retry_delivery is None:
            raise RuntimeError("Delivery retry is unavailable")
        original = self.repository.get_outbound_delivery(delivery_id)
        if original is not None and original.status not in {
            OutboundDeliveryStatus.FAILED,
            OutboundDeliveryStatus.EXPIRED,
        }:
            return
        try:
            await self._retry_delivery(delivery_id)
        except (KeyError, OSError, RuntimeError, ValueError) as error:
            if original is not None:
                self.add_system_event(
                    original.conversation_id,
                    f"重新发送失败：{type(error).__name__}",
                )
            return

    async def cancel_delivery(self, delivery_id: str) -> None:
        if self._cancel_delivery is None:
            return
        original = self.repository.get_outbound_delivery(delivery_id)
        if original is not None and original.status not in {
            OutboundDeliveryStatus.QUEUED,
            OutboundDeliveryStatus.WAITING_FOR_IDLE,
            OutboundDeliveryStatus.RETRY_WAIT,
        }:
            return
        try:
            await self._cancel_delivery(delivery_id)
        except (KeyError, OSError, RuntimeError, ValueError) as error:
            if original is not None:
                self.add_system_event(
                    original.conversation_id,
                    f"取消发送失败：{type(error).__name__}",
                )

    async def resend_delivery(self, delivery_id: str) -> None:
        if self._resend_delivery is None:
            raise RuntimeError("Delivery resend is unavailable")
        original = self.repository.get_outbound_delivery(delivery_id)
        if original is not None and original.status != OutboundDeliveryStatus.SENT:
            return
        try:
            await self._resend_delivery(delivery_id)
        except (KeyError, OSError, RuntimeError, ValueError) as error:
            if original is not None:
                self.add_system_event(
                    original.conversation_id,
                    f"再次发送失败：{type(error).__name__}",
                )

    def preferences(self, item: ConversationItem) -> ConversationPreferences:
        return self.repository.get_conversation_preferences(
            item.channel,
            item.channel_account_id,
            item.conversation_id,
            item.conversation_type,
        )

    def preferences_for_message(
        self, message: UnifiedMessage
    ) -> ConversationPreferences:
        return self.repository.get_conversation_preferences(
            message.channel,
            message.channel_account_id,
            message.conversation_id,
            message.conversation_type,
        )

    def update_preferences(
        self,
        item: ConversationItem,
        *,
        reply_enabled: bool | None = None,
        send_images_enabled: bool | None = None,
        load_history: bool | None = None,
        history_limit: int | None = None,
    ) -> ConversationPreferences:
        current = self.preferences(item)
        updated = replace(
            current,
            reply_enabled=(
                current.reply_enabled if reply_enabled is None else reply_enabled
            ),
            send_images_enabled=(
                current.send_images_enabled
                if send_images_enabled is None
                else send_images_enabled
            ),
            load_history=current.load_history if load_history is None else load_history,
            history_limit=(
                current.history_limit if history_limit is None else history_limit
            ),
        )
        self.repository.set_conversation_preferences(updated)
        if not updated.load_history:
            self._wechat_history.pop(item.conversation_id, None)
            self._notify("timeline", item.conversation_id)
        self._notify("preferences", item.conversation_id)
        return updated

    async def load_history(self, item: ConversationItem) -> None:
        await self._load_wechat_page(item, older=False)

    async def _load_wechat_page(self, item: ConversationItem, *, older: bool) -> None:
        preferences = self.preferences(item)
        conversation_id = item.conversation_id
        if (
            not preferences.load_history
            or conversation_id in self._wechat_history_loading
        ):
            return
        offset = self._wechat_history_offsets[conversation_id] if older else 0
        limit = min(HISTORY_PAGE_SIZE, preferences.history_limit - offset)
        if limit <= 0 or (older and not self._wechat_history_more[conversation_id]):
            return
        self._wechat_history_loading.add(conversation_id)
        generation = self._local_history_generation[conversation_id]
        try:
            if self.history_page_loader:
                messages = await self.history_page_loader(
                    conversation_id, limit, offset
                )
            else:
                messages = await self.history_loader(conversation_id, limit)
        except Exception as error:  # noqa: BLE001 - history failures are isolated to the UI
            self._wechat_history_more[conversation_id] = bool(self.history_page_loader)
            if older:
                raise
            self.add_system_event(
                item.conversation_id,
                f"微信历史加载失败：{type(error).__name__}",
            )
            return
        finally:
            self._wechat_history_loading.discard(conversation_id)
        latest = self.preferences(item)
        if (
            generation != self._local_history_generation[conversation_id]
            or not latest.load_history
            or latest.history_limit != preferences.history_limit
        ):
            return
        entries = [
            self._timeline_entry(message, historical=True, history_source="wechat")
            for message in messages
        ]
        self._wechat_history[conversation_id] = (
            entries + self._wechat_history[conversation_id] if older else entries
        )
        self._wechat_history_offsets[conversation_id] = offset + len(messages)
        self._wechat_history_more[conversation_id] = bool(
            self.history_page_loader
            and len(messages) == limit
            and offset + len(messages) < preferences.history_limit
        )
        if not older:
            self._notify("timeline", conversation_id, "history")

    def has_older_history(self, item: ConversationItem) -> bool:
        return self._local_history_more[item.conversation_id] or (
            self.preferences(item).load_history
            and self._wechat_history_more[item.conversation_id]
        )

    def history_generation(self, item: ConversationItem) -> int:
        """Token used to discard page/quote loads after history was cleared."""
        return self._local_history_generation[item.conversation_id]

    async def load_older_history(self, item: ConversationItem) -> None:
        """Fetch at most one page per source; the view merges overlapping rows."""
        if self._local_history_more[item.conversation_id]:
            await self.load_local_history_async(item, older=True)
        await self._load_wechat_page(item, older=True)

    def _accept_local_page(
        self, item: ConversationItem, rows: list[dict]
    ) -> list[dict]:
        conversation_id = item.conversation_id
        self._local_history_more[conversation_id] = len(rows) > HISTORY_PAGE_SIZE
        rows = rows[-HISTORY_PAGE_SIZE:]
        if rows:
            self._history_cursors[conversation_id] = (
                str(rows[0]["created_at"]),
                int(rows[0]["sequence"]),
            )
        return rows

    def load_local_history(self, item: ConversationItem) -> None:
        conversation_id = item.conversation_id
        if conversation_id in self._local_history_loaded:
            return
        session = self.repository.find_session_for_binding(*item.binding_key)
        rows = (
            self.repository.session_message_page(session.id, limit=21)
            if session
            else []
        )
        if session is not None:
            rows = self._recover_sent_attachment_history(item, session.id, rows)
        rows = self._accept_local_page(item, rows)
        self._local_history[conversation_id] = [
            entry
            for row in rows
            if (entry := self._local_timeline_entry(item, row)) is not None
        ]
        self._local_history_loaded.add(conversation_id)
        self._notify("timeline", conversation_id, "history")

    async def load_local_history_async(
        self, item: ConversationItem, *, older: bool = False
    ) -> None:
        """Load local history without blocking the Qt event loop on startup."""
        conversation_id = item.conversation_id
        if (
            not older and conversation_id in self._local_history_loaded
        ) or conversation_id in self._local_history_loading:
            return
        started = time.perf_counter()
        self._local_history_loading.add(conversation_id)
        generation = self._local_history_generation[conversation_id]
        try:
            session = self.repository.find_session_for_binding(*item.binding_key)
            if session is None:
                rows: list[dict[str, object]] = []
            else:
                rows = await asyncio.to_thread(
                    self.repository.session_message_page,
                    session.id,
                    before=self._history_cursors.get(conversation_id)
                    if older
                    else None,
                    limit=HISTORY_PAGE_SIZE + 1,
                )
                if not older:
                    rows = await asyncio.to_thread(
                        self._recover_sent_attachment_history,
                        item,
                        session.id,
                        rows,
                    )
            if generation != self._local_history_generation[conversation_id]:
                return
            page_rows = rows[-HISTORY_PAGE_SIZE:]
            recovered_media = await self._recover_legacy_wechat_media(item, page_rows)
            if generation != self._local_history_generation[conversation_id]:
                return
            rows = self._accept_local_page(item, rows)
            entries = [
                entry
                for row in rows
                if (entry := self._local_timeline_entry(item, row)) is not None
            ]
            page = [
                self._merge_recovered_media(entry, recovered_media) for entry in entries
            ]
            self._local_history[conversation_id] = (
                page + self._local_history[conversation_id] if older else page
            )
            self._local_history_loaded.add(conversation_id)
            if not older:
                self._notify("timeline", conversation_id, "history")
            logger.info(
                "工作台启动计时: 本地历史 loaded conversation=%s count=%d elapsed=%.3fs",
                conversation_id,
                len(rows),
                time.perf_counter() - started,
            )
        finally:
            self._local_history_loading.discard(conversation_id)

    async def _recover_legacy_wechat_media(
        self,
        item: ConversationItem,
        rows: list[dict[str, object]],
    ) -> dict[str, TimelineEntry]:
        """Rehydrate recent local image-XML rows without enabling full history."""
        if item.channel != "wechat" or not any(
            self._is_legacy_image_row(row) for row in rows
        ):
            return {}
        try:
            if self.history_message_loader:
                ids = tuple(
                    str(row["channel_message_id"])
                    for row in rows
                    if row.get("channel_message_id") and self._is_legacy_image_row(row)
                )
                messages = await self.history_message_loader(item.conversation_id, ids)
            else:
                messages = await self.history_loader(
                    item.conversation_id, HISTORY_PAGE_SIZE
                )
        except Exception as error:  # noqa: BLE001 - media recovery is best effort
            logger.debug(
                "工作台旧图片恢复失败: conversation=%s error=%s",
                item.conversation_id,
                type(error).__name__,
            )
            return {}
        recovered: dict[str, TimelineEntry] = {}
        for message in messages:
            if not message.message_id or not message.attachments:
                continue
            entry = self._timeline_entry(
                message,
                historical=True,
                history_source="wechat",
            )
            if entry.source_key:
                recovered[entry.source_key] = entry
        return recovered

    @staticmethod
    def _is_legacy_image_row(row: dict[str, object]) -> bool:
        metadata = row.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        if metadata.get("bridge_attachments"):
            return False
        content = row.get("content")
        return str(metadata.get("raw_type") or "") in {"图片", "动画表情"} or (
            isinstance(content, str) and "<msg" in content and "<img" in content
        )

    @staticmethod
    def _merge_recovered_media(
        entry: TimelineEntry,
        recovered: dict[str, TimelineEntry],
    ) -> TimelineEntry:
        candidate = recovered.get(entry.source_key or "")
        if candidate is None:
            return entry
        return replace(
            entry,
            content=candidate.content,
            attachments=candidate.attachments,
        )

    def _recover_sent_attachment_history(
        self,
        item: ConversationItem,
        session_id: str,
        rows: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Persist attachment deliveries that predate message-history events.

        Older bridge versions stored image sends only in ``outbound_deliveries``
        and therefore lost their cards when the companion restarted.  Recover
        only confirmed sends with attachments, and compare attachment paths so
        normal Agent events (which are already persisted) are not duplicated.
        """
        if item.channel != "wechat":
            return rows
        deliveries = self.repository.list_outbound_deliveries(
            item.channel_account_id, item.conversation_id
        )
        represented_paths = {
            str(attachment.get("path"))
            for row in rows
            if isinstance(row.get("metadata"), dict)
            for attachment in (row["metadata"].get("bridge_attachments") or [])
            if isinstance(attachment, dict) and attachment.get("path")
        }
        recovered = False
        for delivery in deliveries:
            if (
                delivery.status != OutboundDeliveryStatus.SENT
                or not delivery.attachments
            ):
                continue
            paths = {
                str(attachment.path)
                for attachment in delivery.attachments
                if attachment.path
            }
            if (
                not paths
                or paths.issubset(represented_paths)
                or all(
                    self.repository.has_attachment_history(session_id, path)
                    for path in paths
                )
            ):
                continue
            self.repository.add_event(
                session_id,
                UnifiedEvent(
                    EventType.ASSISTANT_MESSAGE,
                    delivery.text,
                    provider="bridge",
                    metadata={
                        "builtin": "delivery_recovery",
                        "bridge_delivery_id": delivery.id,
                        **(
                            {
                                QUOTE_METADATA_KEY: reply_reference_metadata(
                                    delivery.reply_to
                                )
                            }
                            if delivery.reply_to is not None
                            else {}
                        ),
                        "bridge_attachments": [
                            {
                                "kind": attachment.kind,
                                "name": attachment.name,
                                "path": attachment.path,
                                "url": attachment.url,
                                "mime_type": attachment.mime_type,
                                "metadata": attachment.metadata,
                            }
                            for attachment in delivery.attachments
                        ],
                    },
                    created_at=delivery.completed_at or delivery.created_at,
                ),
            )
            represented_paths.update(paths)
            recovered = True
        return (
            self.repository.session_message_page(session_id, limit=21)
            if recovered
            else rows
        )

    def clear_local_messages(self, item: ConversationItem) -> int:
        session = self.repository.find_session_for_binding(*item.binding_key)
        removed = self.repository.clear_session_messages(session.id) if session else 0
        conversation_id = item.conversation_id
        self._local_history_generation[conversation_id] += 1
        self._history_cursors.pop(conversation_id, None)
        self._local_history_more[conversation_id] = False
        cleared_entry_ids = {
            entry.entry_id for entry in self._realtime.get(conversation_id, ())
        }
        self._local_history[conversation_id] = []
        self._local_history_loaded.add(conversation_id)
        self._realtime.pop(conversation_id, None)
        self._delivery_entries = {
            delivery_id: location
            for delivery_id, location in self._delivery_entries.items()
            if location[0] != conversation_id
        }
        self._agent_entries = {
            job_id: location
            for job_id, location in self._agent_entries.items()
            if location[0] != conversation_id
        }
        self._source_entries = {
            source_key: location
            for source_key, location in self._source_entries.items()
            if location[0] != conversation_id
        }
        for entry_id in cleared_entry_ids:
            self._delivery_states.pop(entry_id, None)
        self._notify("timeline", conversation_id, "history")
        return removed

    def timeline(self, conversation_id: str) -> tuple[TimelineEntry, ...]:
        entries = (
            *self._local_history.get(conversation_id, ()),
            *self._wechat_history.get(conversation_id, ()),
            *self._realtime.get(conversation_id, ()),
        )
        # Local history, WeChat history and realtime events overlap by design.
        # Merge them into one chronological stream instead of rendering source
        # blocks, and prefer the richest/newest representation of a duplicate.
        ordered = sorted(entries, key=lambda entry: entry.created_at)
        result: list[TimelineEntry] = []
        locations: dict[str, int] = {}
        for entry in ordered:
            key = entry.source_key
            if not key or key not in locations:
                if key:
                    locations[key] = len(result)
                result.append(entry)
                continue
            index = locations[key]
            result[index] = self._prefer_timeline_entry(result[index], entry)
        return tuple(result)

    @staticmethod
    def _prefer_timeline_entry(
        current: TimelineEntry, candidate: TimelineEntry
    ) -> TimelineEntry:
        current_score = (
            bool(current.attachments),
            bool(current.quote),
            1 if current.history_source is None else 0,
            bool(current.content and not current.content.startswith("[")),
        )
        candidate_score = (
            bool(candidate.attachments),
            bool(candidate.quote),
            1 if candidate.history_source is None else 0,
            bool(candidate.content and not candidate.content.startswith("[")),
        )
        return candidate if candidate_score > current_score else current

    def add_system_event(self, conversation_id: str, content: str) -> None:
        self._realtime[conversation_id].append(
            TimelineEntry(conversation_id, "系统", content, "system")
        )
        self._notify("timeline", conversation_id)

    def _mark_matching_delivery_sent(self, message: UnifiedMessage) -> bool:
        delivery_text = str(
            message.metadata.get("bridge_delivery_text") or message.content
        )
        entries = self._realtime.get(message.conversation_id, ())
        for index in range(len(entries) - 1, -1, -1):
            entry = entries[index]
            matching_delivery_id = None
            matching_delivery = None
            for delivery_id in entry.delivery_ids or (
                (entry.delivery_id,) if entry.delivery_id else ()
            ):
                delivery = self.repository.get_outbound_delivery(delivery_id)
                if delivery is not None and delivery.text == delivery_text:
                    matching_delivery_id = delivery_id
                    matching_delivery = delivery
                    break
            if entry.content == delivery_text and entry.delivery_id:
                matching_delivery_id = entry.delivery_id
                matching_delivery = self.repository.get_outbound_delivery(
                    matching_delivery_id
                )
            if matching_delivery_id is None:
                continue
            if (
                matching_delivery is not None
                and matching_delivery.content_type == ContentType.IMAGE
                and not matching_delivery.text.lstrip().startswith("[图片]")
            ):
                return True
            if (
                matching_delivery is not None
                and matching_delivery.status != OutboundDeliveryStatus.SENT
            ):
                self.repository.update_outbound_delivery(
                    matching_delivery_id,
                    OutboundDeliveryStatus.SENT,
                    completed_at=message.created_at,
                )
            entries[index] = self._updated_delivery_entry(
                entry, matching_delivery_id, "sent"
            )
            self._notify("timeline", message.conversation_id)
            return True
        return False

    def _suppress_unmarked_delivery_echo(self, message: UnifiedMessage) -> bool:
        """Hide self-echoes that belong to a delivery already on the timeline."""
        if message.channel != "wechat":
            return False
        deliveries = self.repository.list_outbound_deliveries(
            message.channel_account_id, message.conversation_id
        )
        for delivery in reversed(deliveries):
            if delivery.status not in {
                OutboundDeliveryStatus.QUEUED,
                OutboundDeliveryStatus.WAITING_FOR_IDLE,
                OutboundDeliveryStatus.RETRY_WAIT,
                OutboundDeliveryStatus.SENDING,
                OutboundDeliveryStatus.SENT,
            }:
                continue
            location = self._delivery_entries.get(delivery.id)
            if location is None or location[0] != message.conversation_id:
                continue
            entries = self._realtime.get(message.conversation_id, ())
            if location[1] >= len(entries):
                continue
            entry = entries[location[1]]
            if delivery.id not in entry.delivery_ids:
                continue
            if delivery.content_type == ContentType.IMAGE:
                matches = message.content_type == ContentType.IMAGE or (
                    bool(delivery.text.strip()) and message.content == delivery.text
                )
            else:
                matches = message.content == delivery.text
            if not matches:
                continue
            echo = replace(
                message,
                metadata={
                    **message.metadata,
                    "bridge_outbound": True,
                    "bridge_delivery_text": delivery.text,
                },
            )
            return self._mark_matching_delivery_sent(echo)
        return False

    def _updated_delivery_entry(
        self,
        entry: TimelineEntry,
        delivery_id: str,
        state: str,
        detail: str = "",
    ) -> TimelineEntry:
        delivery_ids = entry.delivery_ids
        if delivery_id not in delivery_ids:
            delivery_ids = (*delivery_ids, delivery_id)
        states = self._delivery_states[entry.entry_id]
        states[delivery_id] = state
        aggregate = self._aggregate_delivery_status(
            tuple(states.values()), entry.expected_deliveries
        )
        failure_states = self._failure_delivery_states()
        return replace(
            entry,
            delivery_id=entry.delivery_id or delivery_id,
            delivery_ids=delivery_ids,
            delivery_status=aggregate,
            status_detail=(
                detail
                if state in failure_states
                else entry.status_detail
                if aggregate in failure_states
                else ""
            ),
        )

    @staticmethod
    def _failure_delivery_states() -> frozenset[str]:
        return frozenset(
            {
                "failed",
                "expired",
                "unavailable",
                "foreground_unavailable",
                "delivery_unknown",
                "cancelled",
            }
        )

    @classmethod
    def _aggregate_delivery_status(cls, states: tuple[str, ...], expected: int) -> str:
        for failure in (
            "failed",
            "expired",
            "unavailable",
            "foreground_unavailable",
            "delivery_unknown",
            "cancelled",
        ):
            if failure in states:
                return failure
        sent_states = {"sent", "foreground_sent"}
        if (
            len(states) >= expected
            and states
            and all(state in sent_states for state in states)
        ):
            return "sent"
        for active in (
            "foreground_sending",
            "sending",
            "waiting_for_idle",
            "retrying",
        ):
            if active in states:
                return active
        return "queued"

    def current_provider(self, item: ConversationItem) -> str:
        session = self.repository.find_session_for_binding(*item.binding_key)
        return session.current_provider if session else self.default_provider

    def provider_available(self, item: ConversationItem) -> bool:
        return (
            self.available_providers is None
            or self.current_provider(item) in self.available_providers
        )

    def current_session_status(self, item: ConversationItem) -> str:
        session = self.repository.find_session_for_binding(*item.binding_key)
        return session.status if session else "new"

    def _notify(
        self, kind: str, conversation_id: str | None = None, detail: str = ""
    ) -> None:
        update = CompanionUpdate(kind, conversation_id, detail)
        for subscriber in tuple(self._subscribers):
            try:
                subscriber(update)
            except Exception:
                logger.exception("伴随窗口订阅者处理事件失败")

    @staticmethod
    def _delivery_source_key(idempotency_key: str) -> str | None:
        prefix = "agent_reply:"
        if not idempotency_key.startswith(prefix):
            return None
        return idempotency_key[len(prefix) :]

    @staticmethod
    def _timeline_entry(
        message: UnifiedMessage,
        *,
        historical: bool,
        history_source: str | None = None,
    ) -> TimelineEntry:
        is_self = bool(message.metadata.get("is_self"))
        return TimelineEntry(
            conversation_id=message.conversation_id,
            sender_name=message.sender_name or message.sender_id,
            content=str(message.metadata.get("original_content", message.content)),
            direction="outbound" if is_self else "inbound",
            created_at=message.created_at,
            historical=historical,
            history_source=history_source,
            **(
                {"entry_id": f"history:{message.channel}:{message.message_id}"}
                if historical and message.message_id
                else {}
            ),
            attachments=message.attachments,
            quote=CompanionController._quote_preview(
                message.metadata.get(QUOTE_METADATA_KEY),
                channel=message.channel,
            ),
            source_key=(
                f"{message.channel}:{message.message_id}"
                if message.message_id
                else None
            ),
        )

    @staticmethod
    def _merge_attachments(*groups):
        result = []
        seen = set()
        for attachment in (item for group in groups for item in group):
            key = (attachment.kind, attachment.path, attachment.url, attachment.name)
            if key in seen:
                continue
            seen.add(key)
            result.append(attachment)
        return tuple(result)

    @staticmethod
    def _local_timeline_entry(
        item: ConversationItem, row: dict[str, object]
    ) -> TimelineEntry | None:
        role = str(row.get("role", ""))
        if role not in {"user", "assistant"}:
            return None
        metadata = row.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        attachments = CompanionController._attachments_from_metadata(
            metadata.get("bridge_attachments")
        )
        content = row.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        raw_type = str(metadata.get("raw_type") or "")
        if raw_type in {"图片", "动画表情"} or (
            "<msg" in content and "<img" in content
        ):
            content = "[动画表情]" if raw_type == "动画表情" else "[图片]"
        if role == "assistant" and isinstance(
            metadata.get("bridge_display_content"), str
        ):
            content = metadata["bridge_display_content"]
        content, legacy_quote = parse_wechat_quote_payload(content)
        channel_message_id = row.get("channel_message_id")
        channel_message_id = (
            str(channel_message_id) if channel_message_id is not None else ""
        )
        created_at = datetime.fromisoformat(str(row["created_at"]))
        if role == "assistant":
            sender_name = str(row.get("provider") or "我").upper()
            direction = "outbound"
        else:
            sender_name = str(
                metadata.get("sender_name")
                or metadata.get("sender_id")
                or item.display_name
            )
            direction = "inbound"
        return TimelineEntry(
            conversation_id=item.conversation_id,
            sender_name=sender_name,
            content=content,
            direction=direction,
            created_at=created_at,
            historical=True,
            history_source="local",
            **({"entry_id": f"local:{row['id']}"} if row.get("id") else {}),
            attachments=attachments,
            quote=CompanionController._quote_preview(
                metadata.get(QUOTE_METADATA_KEY) or legacy_quote,
                channel=str(row.get("channel") or item.channel),
            ),
            source_key=(
                f"{row.get('channel') or item.channel}:{channel_message_id}"
                if channel_message_id
                else None
            ),
        )

    @staticmethod
    def _quote_preview(value: object, *, channel: str) -> QuotePreview | None:
        if not isinstance(value, dict):
            return None
        content = str(value.get("content") or "").strip()
        sender_name = str(
            value.get("sender_name") or value.get("sender_id") or "原消息"
        ).strip()
        if not content:
            return None
        raw_content_type = str(value.get("content_type") or ContentType.UNKNOWN.value)
        try:
            content_type = ContentType(raw_content_type)
        except ValueError:
            content_type = ContentType.UNKNOWN
        raw_created_at = value.get("created_at")
        target_created_at = None
        if raw_created_at:
            try:
                target_created_at = datetime.fromisoformat(str(raw_created_at))
            except ValueError:
                target_created_at = None
        message_id = str(value.get("message_id") or "").strip()
        return QuotePreview(
            sender_name=sender_name or "原消息",
            content=content,
            content_type=content_type,
            target_source_key=f"{channel}:{message_id}" if message_id else None,
            target_created_at=target_created_at,
        )

    @staticmethod
    def _attachments_from_metadata(value: object) -> tuple[Attachment, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        result: list[Attachment] = []
        for item in value:
            if isinstance(item, Attachment):
                result.append(item)
                continue
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            if not kind:
                continue
            metadata = item.get("metadata")
            result.append(
                Attachment(
                    kind=kind,
                    name=str(item["name"]) if item.get("name") is not None else None,
                    path=str(item["path"]) if item.get("path") is not None else None,
                    url=str(item["url"]) if item.get("url") is not None else None,
                    mime_type=(
                        str(item["mime_type"])
                        if item.get("mime_type") is not None
                        else None
                    ),
                    metadata=metadata if isinstance(metadata, dict) else {},
                )
            )
        return tuple(result)
