from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agent_bridge.agents.factory import AgentFactory
from agent_bridge.channels.base import ChannelAdapter
from agent_bridge.models import (
    AgentContext,
    AgentProgressUpdate,
    AgentRequest,
    Attachment,
    ChannelTarget,
    ContentType,
    EventType,
    Job,
    JobStatus,
    NativeSession,
    OutboundMessage,
    ReplyReference,
    UnifiedEvent,
    UnifiedMessage,
    new_id,
)
from agent_bridge.quotes import QUOTE_METADATA_KEY, reply_reference_metadata
from agent_bridge.runtime.commands import (
    HELP_TEXT,
    Command,
    CommandError,
    CommandType,
    parse_command,
)
from agent_bridge.runtime.image_directives import (
    ImageDirectiveResult,
    image_delivery_instruction,
    resolve_image_directives,
)
from agent_bridge.runtime.queue import SessionTaskQueue
from agent_bridge.sessions.context import ContextBuilder
from agent_bridge.sessions.repository import SQLiteRepository
from agent_bridge.sessions.resolver import SessionResolver
from agent_bridge.tools.desktop import WindowInfo

logger = logging.getLogger(__name__)
ProgressSubscriber = Callable[[AgentProgressUpdate], None]


class _ProgressReporter:
    def __init__(
        self,
        notify: ProgressSubscriber,
        job: Job,
        conversation_id: str,
    ) -> None:
        self._notify = notify
        self._job = job
        self._conversation_id = conversation_id
        self._text = ""
        self._last_emit = 0.0

    def start(self) -> None:
        self._emit("started")

    async def append(self, delta: str) -> None:
        self._text += delta
        now = time.monotonic()
        if now - self._last_emit >= 0.05:
            self._last_emit = now
            self._emit("streaming")

    def reset(self) -> None:
        self._text = ""
        self._last_emit = 0.0
        self._emit("streaming")

    def complete(self, text: str, chunks: tuple[str, ...]) -> None:
        self._text = text
        self._emit("completed", chunks)

    def finish(self, state: str, detail: str) -> None:
        self._emit(state, detail=detail)

    def _emit(
        self, state: str, chunks: tuple[str, ...] = (), detail: str = ""
    ) -> None:
        self._notify(
            AgentProgressUpdate(
                self._job.id,
                self._conversation_id,
                self._job.provider,
                state,
                self._text,
                chunks,
                detail,
            )
        )


@dataclass(slots=True, frozen=True)
class DispatcherSettings:
    timeout_seconds: float = 600.0
    acknowledgement: str = "已接收，正在处理。"
    max_reply_chars: int = 1800
    group_controllers: frozenset[str] = field(default_factory=frozenset)
    message_batch_window_seconds: float = 0.0
    reply_prefix: str = ""


@dataclass(slots=True)
class _PendingMessageBatch:
    session_id: str
    summary: str
    recent_messages: tuple[dict, ...]
    messages: list[UnifiedMessage] = field(default_factory=list)
    included_context_message_ids: list[str] = field(default_factory=list)
    dispatch_requested: bool = False
    timer: asyncio.Task[None] | None = None


class Dispatcher:
    def __init__(
        self,
        channel: ChannelAdapter,
        repository: SQLiteRepository,
        resolver: SessionResolver,
        agents: AgentFactory,
        context_builder: ContextBuilder,
        task_queue: SessionTaskQueue,
        settings: DispatcherSettings | None = None,
    ) -> None:
        self.channel = channel
        self.repository = repository
        self.resolver = resolver
        self.agents = agents
        self.context_builder = context_builder
        self.task_queue = task_queue
        self.settings = settings or DispatcherSettings()
        self._active_jobs: dict[str, tuple[str, str]] = {}
        self._queued_jobs: dict[str, dict[str, str]] = {}
        self._queued_job_sessions: dict[str, str] = {}
        self._forwarded_context_messages: dict[tuple[str, str], set[str]] = {}
        self._progress_subscribers: list[ProgressSubscriber] = []
        self._desktop_selector_sessions: dict[str, NativeSession] = {}
        self._pending_batches: dict[tuple[str, str, str], _PendingMessageBatch] = {}
        self._closed = False

    def subscribe_progress(self, subscriber: ProgressSubscriber) -> None:
        self._progress_subscribers.append(subscriber)

    async def select_desktop_window(
        self,
        message: UnifiedMessage,
        query: str,
        windows: tuple[WindowInfo, ...],
    ) -> WindowInfo | None:
        """Ask the active Agent to choose among ambiguous local windows.

        The Agent only receives a compact candidate catalog and returns an
        index.  It never receives desktop-control access and never sends a
        chat reply; the local channel performs the actual capture.
        """
        if not windows:
            return None
        session, _ = self.resolver.resolve(message)
        provider = session.current_provider
        try:
            adapter, parser = self.agents.get(provider)
        except (KeyError, RuntimeError, ValueError):
            return None
        native = self._desktop_selector_sessions.get(provider)
        try:
            if native is None:
                native = await adapter.create_session(
                    f"desktop-selector:{provider}", session.working_directory
                )
                self._desktop_selector_sessions[provider] = native
            else:
                await adapter.resume_session(native)
            catalog = [
                {
                    "index": index,
                    "title": window.title,
                    "process": window.process_name,
                    "minimized": window.minimized,
                }
                for index, window in enumerate(windows)
            ]
            prompt = (
                "选择要截图的桌面窗口。用户请求："
                f"{query}\n候选窗口(JSON)：{json.dumps(catalog, ensure_ascii=False)}\n"
                "只返回JSON，例如 {\"index\": 3}。如果没有合适窗口，返回 {\"index\": null}。"
            )
            request = AgentRequest(
                prompt=prompt,
                context=AgentContext("", (), message),
                job_id=new_id("desktop_select"),
                include_context=False,
            )
            result = await adapter.run(native, request)
            response = parser.parse_final(result)
            index = self._parse_desktop_window_index(response.text)
            if index is None or not 0 <= index < len(windows):
                return None
            return windows[index]
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - local matching remains available
            logger.warning("Agent 桌面窗口识别失败: %s", error)
            return None

    @staticmethod
    def _parse_desktop_window_index(text: str) -> int | None:
        value = str(text or "").strip()
        match = re.search(r'"index"\s*:\s*(null|-?\d+)', value, re.IGNORECASE)
        if match is None:
            match = re.search(r"\bindex\s*[:=]\s*(-?\d+)\b", value, re.IGNORECASE)
        if match is None or match.group(1).lower() == "null":
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    async def handle(self, message: UnifiedMessage) -> None:
        await self._route_message(message, dispatch_requested=True)

    async def observe(self, message: UnifiedMessage) -> None:
        """Retain untriggered group context without creating an Agent job."""
        await self._route_message(message, dispatch_requested=False)

    async def _route_message(
        self, message: UnifiedMessage, *, dispatch_requested: bool
    ) -> None:
        if self._closed:
            return
        if not self.repository.mark_inbound_processed(message.channel, message.message_id):
            return
        key = message.binding_key
        if message.content_type not in {ContentType.TEXT, ContentType.IMAGE}:
            if dispatch_requested:
                await self._flush_message_batch(key)
                await self._handle_immediate(message, already_processed=True)
            else:
                session, _ = self.resolver.resolve(message)
                self.repository.add_inbound_message(session.id, message)
            return

        if dispatch_requested and message.content_type == ContentType.TEXT:
            try:
                command = parse_command(message.content)
            except CommandError:
                command = True
            builtin_matcher = getattr(self.channel, "is_builtin_message", None)
            is_builtin = bool(
                callable(builtin_matcher) and builtin_matcher(message)
            )
            if command is not None or is_builtin:
                await self._flush_message_batch(key)
                await self._handle_immediate(message, already_processed=True)
                return

        await self._enqueue_message_batch(message, dispatch_requested)

    async def _handle_immediate(
        self,
        message: UnifiedMessage,
        *,
        already_processed: bool = False,
        already_persisted: bool = False,
        prompt_override: str | None = None,
        input_attachments: tuple[Attachment, ...] = (),
        context_override: AgentContext | None = None,
        reply_message: UnifiedMessage | None = None,
        skip_channel_builtin: bool = False,
        included_context_message_ids: tuple[str, ...] = (),
    ) -> None:
        if not already_processed and not self.repository.mark_inbound_processed(
            message.channel, message.message_id
        ):
            return
        session, _ = self.resolver.resolve(message)
        response_source = reply_message or message
        target = self._target(response_source)
        reply_to = ReplyReference.from_message(response_source)

        if message.content_type != ContentType.TEXT:
            await self._send(
                target,
                "暂不支持该消息类型，首版仅支持文本消息。",
                reply_to=reply_to,
            )
            return

        try:
            command = parse_command(message.content)
        except CommandError as exc:
            await self._send(target, str(exc), reply_to=reply_to)
            return

        if command and command.type != CommandType.ASK:
            try:
                await self._handle_command(session.id, message, target, command)
            except CommandError as exc:
                await self._send(target, str(exc), reply_to=reply_to)
            return

        # Channel-native actions (for example a WeChat window screenshot) are
        # handled by the bridge itself.  This avoids asking an Agent session to
        # use a desktop tool that is not part of its configured SDK surface.
        builtin = getattr(self.channel, "handle_builtin", None)
        if not skip_channel_builtin and callable(builtin):
            handled = await builtin(message, target)
            if handled:
                self.repository.add_inbound_message(session.id, message)
                if isinstance(handled, OutboundMessage):
                    self.repository.add_event(
                        session.id,
                        UnifiedEvent(
                            EventType.ASSISTANT_MESSAGE,
                            handled.text,
                            provider="bridge",
                            metadata={
                                "builtin": "channel_action",
                                "bridge_attachments": [
                                    {
                                        "kind": item.kind,
                                        "name": item.name,
                                        "path": item.path,
                                        "url": item.url,
                                        "mime_type": item.mime_type,
                                        "metadata": item.metadata,
                                    }
                                    for item in handled.attachments
                                ],
                            },
                        ),
                    )
                return

        provider = command.provider if command else session.current_provider
        prompt = prompt_override or (command.value if command else message.content)
        assert provider is not None and prompt is not None
        if provider not in self.agents.providers():
            await self._send(
                target, f"未配置 Agent：{provider}", reply_to=reply_to
            )
            return

        context = context_override or self.context_builder.build(session, message)
        if not already_persisted:
            self.repository.add_inbound_message(session.id, message)
        job = Job(new_id("job"), session.id, provider, response_source.message_id)
        self.repository.create_job(job)
        async def execute() -> None:
            await self._execute_job(
                job,
                response_source,
                target,
                prompt,
                context,
                input_attachments=input_attachments,
                included_context_message_ids=included_context_message_ids,
            )

        logger.info(
            "Agent 任务已提交: job=%s session=%s provider=%s conversation=%s",
            job.id,
            session.id,
            provider,
            message.conversation_id,
        )
        self._queued_jobs.setdefault(message.conversation_id, {})[job.id] = message.content
        self._queued_job_sessions[job.id] = session.id
        self._notify_progress(
            AgentProgressUpdate(
                job.id,
                message.conversation_id,
                provider,
                "queued",
                message.content,
            )
        )
        try:
            await self.task_queue.submit(session.id, execute, item_id=job.id)
        except asyncio.CancelledError:
            # The queue is process-local.  A pending item cancelled during
            # shutdown must not be left looking queued in persistent state.
            self.repository.update_job(job.id, JobStatus.CANCELLED, "queue_closed")
            return
        finally:
            self._remove_queued_job(job.id, message.conversation_id)

    async def _enqueue_message_batch(
        self, message: UnifiedMessage, dispatch_requested: bool
    ) -> None:
        session, _ = self.resolver.resolve(message)
        key = message.binding_key
        batch = self._pending_batches.get(key)
        if batch is None:
            context = self.context_builder.build(session, message)
            batch = _PendingMessageBatch(
                session.id,
                context.summary,
                context.recent_messages,
            )
            self._pending_batches[key] = batch
        stored_message = message
        if not dispatch_requested:
            stored_message = replace(
                message,
                metadata={**message.metadata, "_bridge_context_only": True},
            )
        batch.messages.append(stored_message)
        batch.dispatch_requested = batch.dispatch_requested or dispatch_requested
        history_id = self.repository.add_inbound_message(session.id, stored_message)
        if not dispatch_requested:
            batch.included_context_message_ids.append(history_id)

        if batch.timer is not None:
            batch.timer.cancel()
        delay = self.settings.message_batch_window_seconds
        if delay <= 0:
            await self._seal_message_batch(key)
            return
        batch.timer = asyncio.create_task(
            self._seal_message_batch_after(key, delay),
            name=f"message-batch-{message.conversation_id}",
        )

    async def _seal_message_batch_after(
        self, key: tuple[str, str, str], delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)
            wait_for_media = getattr(self.channel, "wait_for_pending_media", None)
            if callable(wait_for_media):
                await wait_for_media(key[2])
            await self._seal_message_batch(key)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("合并消息批次处理失败: conversation=%s", key[2])

    async def _flush_message_batch(self, key: tuple[str, str, str]) -> None:
        await self._seal_message_batch(key)

    async def _seal_message_batch(self, key: tuple[str, str, str]) -> None:
        batch = self._pending_batches.pop(key, None)
        if batch is None:
            return
        current = asyncio.current_task()
        if batch.timer is not None and batch.timer is not current:
            batch.timer.cancel()
        if self._closed or not batch.dispatch_requested or not batch.messages:
            return

        messages = tuple(batch.messages)
        text_messages = tuple(
            item for item in messages if item.content_type == ContentType.TEXT
        )
        image_messages = tuple(
            item for item in messages if item.content_type == ContentType.IMAGE
        )
        reply_source = text_messages[-1] if text_messages else image_messages[-1]
        usable_images = tuple(
            attachment
            for item in image_messages
            for attachment in item.attachments
            if (
                attachment.kind == "image"
                and attachment.path
                and Path(attachment.path).is_file()
            )
        )
        missing_images = sum(
            1
            for item in image_messages
            if not any(
                attachment.kind == "image"
                and attachment.path
                and Path(attachment.path).is_file()
                for attachment in item.attachments
            )
        )
        text = "\n".join(
            item.content.strip() for item in text_messages if item.content.strip()
        )
        if not text and not usable_images:
            failure_text = (
                "图片加载失败，暂时无法识别图片内容。"
                if image_messages
                else "消息内容为空，请发送文字或图片。"
            )
            await self._send(
                self._target(reply_source),
                failure_text,
                reply_to=ReplyReference.from_message(reply_source),
            )
            return
        prompt = text or "请描述这些图片的内容。"
        if missing_images:
            prompt = (
                f"{prompt}\n\n[系统提示：有 {missing_images} 张图片未能加载，"
                "请基于可用内容回答。]"
            )
        synthetic = replace(
            reply_source,
            content=prompt,
            content_type=ContentType.TEXT,
            attachments=(),
        )
        context = AgentContext(
            batch.summary,
            batch.recent_messages,
            reply_source,
        )
        await self._handle_immediate(
            synthetic,
            already_processed=True,
            already_persisted=True,
            prompt_override=prompt,
            input_attachments=usable_images,
            context_override=context,
            reply_message=reply_source,
            skip_channel_builtin=True,
            included_context_message_ids=tuple(batch.included_context_message_ids),
        )

    def queued_jobs(self, conversation_id: str) -> tuple[tuple[str, str], ...]:
        """Return pending jobs for a conversation; active jobs are excluded."""
        return tuple(self._queued_jobs.get(conversation_id, {}).items())

    async def remove_queued_job(self, conversation_id: str, job_id: str) -> bool:
        session_id = self._queued_job_sessions.get(job_id)
        if session_id is None or job_id not in self._queued_jobs.get(conversation_id, {}):
            return False
        removed = self.task_queue.remove_queued(session_id, job_id)
        if not removed:
            return False
        self._remove_queued_job(job_id, conversation_id)
        self.repository.update_job(job_id, JobStatus.CANCELLED, "queue_removed")
        self._notify_progress(
            AgentProgressUpdate(job_id, conversation_id, "bridge", "cancelled", detail="已从队列删除")
        )
        return True

    async def clear_queued_jobs(self, conversation_id: str) -> int:
        pending = tuple(self._queued_jobs.get(conversation_id, {}))
        removed = 0
        for job_id in pending:
            if await self.remove_queued_job(conversation_id, job_id):
                removed += 1
        return removed

    def _remove_queued_job(self, job_id: str, conversation_id: str) -> None:
        jobs = self._queued_jobs.get(conversation_id)
        if jobs is not None:
            jobs.pop(job_id, None)
            if not jobs:
                self._queued_jobs.pop(conversation_id, None)
        self._queued_job_sessions.pop(job_id, None)

    async def close(self) -> None:
        self._closed = True
        timers = tuple(
            batch.timer
            for batch in self._pending_batches.values()
            if batch.timer is not None
        )
        self._pending_batches.clear()
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        await self.task_queue.close()
        await self.agents.close()

    async def _execute_job(
        self,
        job,
        message,
        target,
        prompt,
        context,
        *,
        input_attachments: tuple[Attachment, ...] = (),
        included_context_message_ids: tuple[str, ...] = (),
    ) -> None:
        self._remove_queued_job(job.id, message.conversation_id)
        adapter, parser = self.agents.get(job.provider)
        reporter = _ProgressReporter(
            self._notify_progress, job, message.conversation_id
        )
        reporter.start()
        self.repository.update_job(job.id, JobStatus.RUNNING)
        logger.info(
            "Agent 任务开始执行: job=%s session=%s provider=%s",
            job.id,
            job.unified_session_id,
            job.provider,
        )
        self._active_jobs[job.unified_session_id] = (job.id, job.provider)
        native = self.repository.get_active_native_session(job.unified_session_id, job.provider)
        include_context = native is None or not native.context_initialized
        try:
            if native is None:
                session = self.repository.get_session(job.unified_session_id)
                assert session is not None
                native = await adapter.create_session(
                    session.id, session.working_directory, model=None
                )
                self.repository.add_native_session(native)
            else:
                await adapter.resume_session(native)
            preferences = self.repository.get_conversation_preferences(
                message.channel,
                message.channel_account_id,
                message.conversation_id,
                message.conversation_type,
            )
            reply_reference = ReplyReference.from_message(message)
            quote_metadata = reply_reference_metadata(reply_reference)
            context_only_messages = self._unforwarded_context_messages(
                job.unified_session_id,
                job.provider,
                context,
            )
            request_prompt = self._agent_prompt(
                context,
                prompt,
                include_context=include_context,
                context_only_messages=context_only_messages,
            )
            request = AgentRequest(
                prompt=request_prompt,
                context=context,
                job_id=job.id,
                include_context=include_context,
                transport_instructions=(
                    image_delivery_instruction()
                    if preferences.send_images_enabled
                    else ""
                ),
                input_attachments=self._agent_input_attachments(
                    context,
                    input_attachments,
                    include_context=include_context,
                    context_only_messages=context_only_messages,
                ),
            )
            result = await asyncio.wait_for(
                self._run_with_transient_retry(
                    adapter, native, request, reporter
                ),
                timeout=self.settings.timeout_seconds,
            )
            self.repository.mark_native_initialized(native.id)
            self._mark_context_messages_forwarded(
                job.unified_session_id,
                job.provider,
                context_only_messages,
                included_context_message_ids,
            )
            response = parser.parse_final(result)
            image_result = (
                resolve_image_directives(response.text, native.working_directory)
                if preferences.send_images_enabled
                else ImageDirectiveResult(response.text)
            )
            reply_text = image_result.text
            if image_result.errors:
                rejected = "\n".join(
                    f"图片未发送：{error}" for error in image_result.errors
                )
                reply_text = f"{reply_text}\n\n{rejected}".strip()
            stored_assistant = False
            for event in response.events:
                event_metadata = dict(event.metadata)
                if event.type == EventType.ASSISTANT_MESSAGE:
                    event_metadata[QUOTE_METADATA_KEY] = quote_metadata
                if event.type == EventType.ASSISTANT_MESSAGE and isinstance(
                    event.content, str
                ) and preferences.send_images_enabled:
                    event_image_result = resolve_image_directives(
                        event.content, native.working_directory
                    )
                    cleaned = event_image_result.text
                    if not cleaned:
                        continue
                    if event_image_result.attachments:
                        event_metadata["bridge_attachments"] = [
                            {
                                "kind": item.kind,
                                "name": item.name,
                                "path": item.path,
                                "url": item.url,
                                "mime_type": item.mime_type,
                                "metadata": item.metadata,
                            }
                            for item in event_image_result.attachments
                        ]
                    event = UnifiedEvent(
                        event.type,
                        cleaned,
                        event.provider,
                        event.raw,
                        event_metadata,
                        event.created_at,
                    )
                elif event.type == EventType.ASSISTANT_MESSAGE:
                    event = UnifiedEvent(
                        event.type,
                        event.content,
                        event.provider,
                        event.raw,
                        event_metadata,
                        event.created_at,
                    )
                if event.type == EventType.ASSISTANT_MESSAGE and isinstance(
                    event.content, str
                ):
                    event_metadata["bridge_display_content"] = (
                        self._with_reply_prefix(event.content)
                    )
                    event = replace(event, metadata=event_metadata)
                self.repository.add_event(job.unified_session_id, event)
                stored_assistant = stored_assistant or (
                    event.type == EventType.ASSISTANT_MESSAGE
                )
            if not stored_assistant:
                self.repository.add_event(
                    job.unified_session_id,
                    UnifiedEvent(
                        EventType.ASSISTANT_MESSAGE,
                        reply_text
                        or "\n".join(
                            self._image_label(item)
                            for item in image_result.attachments
                        ),
                        provider=job.provider,
                        metadata={
                            QUOTE_METADATA_KEY: quote_metadata,
                            **(
                                {
                                    "bridge_display_content": self._with_reply_prefix(
                                        reply_text
                                    )
                                }
                                if reply_text
                                else {}
                            ),
                            "bridge_attachments": [
                                {
                                    "kind": item.kind,
                                    "name": item.name,
                                    "path": item.path,
                                    "url": item.url,
                                    "mime_type": item.mime_type,
                                    "metadata": item.metadata,
                                }
                                for item in image_result.attachments
                            ]
                        },
                    ),
                )
            if response.native_session_id != native.native_session_id:
                replacement = NativeSession(
                    id=new_id("native"),
                    unified_session_id=native.unified_session_id,
                    provider=native.provider,
                    native_session_id=response.native_session_id or native.native_session_id,
                    working_directory=native.working_directory,
                    model=native.model,
                )
                self.repository.add_native_session(replacement)
            self.repository.update_job(job.id, JobStatus.COMPLETED)
            logger.info(
                "Agent 任务执行完成: job=%s session=%s provider=%s",
                job.id,
                job.unified_session_id,
                job.provider,
            )
            text_limit = max(
                1,
                self.settings.max_reply_chars - len(self.settings.reply_prefix),
            )
            text_chunks = tuple(
                self._with_reply_prefix(chunk)
                for chunk in (
                    split_reply(reply_text, text_limit) if reply_text else ()
                )
            )
            image_labels = tuple(
                self._image_label(item) for item in image_result.attachments
            )
            deliveries = [(chunk, ()) for chunk in text_chunks]
            attachments = image_result.attachments
            if deliveries and attachments:
                last_text, _ = deliveries[-1]
                deliveries[-1] = (last_text, (attachments[0],))
                attachments = attachments[1:]
                image_labels = image_labels[1:]
            deliveries.extend(
                (label, (attachment,))
                for label, attachment in zip(image_labels, attachments)
            )
            deliveries = tuple(deliveries)
            display_text = (
                self._with_reply_prefix(reply_text)
                if reply_text
                else "\n".join(image_labels)
            )
            reporter.complete(
                display_text,
                tuple(text for text, _attachments in deliveries),
            )
            for index, (delivery_text, attachments) in enumerate(deliveries):
                await self._send(
                    target,
                    delivery_text,
                    idempotency_key=f"agent_reply:{job.id}:{index}",
                    attachments=attachments,
                    reply_to=reply_reference,
                )
        except asyncio.CancelledError:
            await adapter.cancel(job.id)
            self.repository.update_job(job.id, JobStatus.CANCELLED)
            reporter.finish("cancelled", "任务已取消")
            raise
        except asyncio.TimeoutError:
            await adapter.cancel(job.id)
            self.repository.update_job(job.id, JobStatus.FAILED, "timeout")
            reporter.finish("failed", "Agent 执行超时，任务已停止")
            logger.error("Agent 任务超时: job=%s provider=%s", job.id, job.provider)
        except Exception as exc:
            self.repository.update_job(job.id, JobStatus.FAILED, type(exc).__name__)
            error_detail = str(exc).strip() or type(exc).__name__
            reporter.finish("failed", f"Agent 执行失败：{error_detail}")
            logger.exception(
                "Agent 任务执行失败: job=%s provider=%s", job.id, job.provider
            )
        finally:
            self._active_jobs.pop(job.unified_session_id, None)

    async def _run_with_transient_retry(
        self, adapter, native, request, reporter: _ProgressReporter
    ):
        try:
            return await adapter.run(native, request, reporter.append)
        except (ConnectionError, TimeoutError, OSError):
            reporter.reset()
            return await adapter.run(native, request, reporter.append)

    async def _handle_command(self, session_id, message, target, command: Command) -> None:
        session = self.repository.get_session(session_id)
        assert session is not None
        reply_to = ReplyReference.from_message(message)
        if command.type == CommandType.HELP:
            await self._send(target, HELP_TEXT, reply_to=reply_to)
            return
        if command.type == CommandType.CANCEL:
            cancelled = await self.task_queue.cancel(session_id)
            await self._send(
                target,
                "已请求取消当前任务。" if cancelled else "当前没有运行中的任务。",
                reply_to=reply_to,
            )
            return
        if not self._can_mutate(message):
            self.repository.add_session_event(
                session_id,
                "permission_denied",
                {"sender_id": message.sender_id, "command": command.type.value},
            )
            await self._send(
                target,
                "你没有权限修改该群聊的 Agent 或 session。",
                reply_to=reply_to,
            )
            return
        if command.type == CommandType.SET_AGENT:
            self._require_provider(command.provider)
            self.repository.set_current_provider(session_id, command.provider or "")
            self.repository.add_session_event(
                session_id,
                "provider_changed",
                {"provider": command.provider, "sender_id": message.sender_id},
            )
            await self._send(
                target,
                f"当前 Agent 已切换为 {command.provider}。",
                reply_to=reply_to,
            )
        elif command.type == CommandType.SESSION_INFO:
            lines = [f"UnifiedSession: {session.id}", f"当前 Agent: {session.current_provider}"]
            for provider in self.agents.providers():
                native = self.repository.get_active_native_session(session_id, provider)
                lines.append(f"{provider}: {native.native_session_id if native else '未创建'}")
            await self._send(target, "\n".join(lines), reply_to=reply_to)
        elif command.type == CommandType.SESSION_NEW:
            provider = self._require_provider(command.provider)
            adapter, _ = self.agents.get(provider)
            native = await adapter.create_session(session.id, session.working_directory)
            self.repository.add_native_session(native)
            self.repository.set_current_provider(session_id, provider)
            self.repository.add_session_event(
                session_id,
                "native_session_created",
                {"provider": provider, "native_session_id": native.native_session_id},
            )
            await self._send(
                target,
                f"已创建 {provider} session：{native.native_session_id}",
                reply_to=reply_to,
            )
        elif command.type == CommandType.SESSION_BIND:
            provider = self._require_provider(command.provider)
            assert command.value
            native = NativeSession(
                id=new_id("native"),
                unified_session_id=session_id,
                provider=provider,
                native_session_id=command.value,
                working_directory=session.working_directory,
                context_initialized=True,
            )
            adapter, _ = self.agents.get(provider)
            await adapter.resume_session(native)
            self.repository.add_native_session(native)
            self.repository.set_current_provider(session_id, provider)
            self.repository.add_session_event(
                session_id,
                "native_session_bound",
                {"provider": provider, "native_session_id": command.value},
            )
            await self._send(
                target,
                f"已绑定 {provider} session：{command.value}",
                reply_to=reply_to,
            )
        elif command.type == CommandType.SESSION_HISTORY:
            provider = self._require_provider(command.provider)
            history = self.repository.list_native_sessions(session_id, provider)
            text = "\n".join(
                f"{'*' if item.is_active else '-'} {item.native_session_id}" for item in history
            ) or "暂无历史 session。"
            await self._send(target, text, reply_to=reply_to)

    def _can_mutate(self, message: UnifiedMessage) -> bool:
        return (
            message.conversation_type.value == "private"
            or message.sender_id in self.settings.group_controllers
        )

    def _require_provider(self, provider: str | None) -> str:
        if provider is None or provider not in self.agents.providers():
            raise CommandError(f"未配置 Agent：{provider or ''}")
        return provider

    async def _send(
        self,
        target: ChannelTarget,
        text: str,
        *,
        idempotency_key: str | None = None,
        attachments: tuple[Attachment, ...] = (),
        reply_to: ReplyReference | None = None,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                metadata = (
                    {"idempotency_key": idempotency_key}
                    if idempotency_key is not None
                    else {}
                )
                return await self.channel.send_message(
                    target,
                    OutboundMessage(
                        text,
                        metadata,
                        tuple(attachments),
                        reply_to=reply_to,
                    ),
                )
            except (ConnectionError, OSError, RuntimeError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
        assert last_error is not None
        raise last_error

    def _with_reply_prefix(self, text: str) -> str:
        prefix = self.settings.reply_prefix
        if not prefix or text.startswith(prefix):
            return text
        return f"{prefix}{text}"

    @staticmethod
    def _agent_input_attachments(
        context: AgentContext,
        current: tuple[Attachment, ...],
        *,
        include_context: bool,
        context_only_messages: tuple[dict[str, Any], ...] = (),
    ) -> tuple[Attachment, ...]:
        candidates: list[Attachment] = []
        recent_messages = (
            context.recent_messages
            if include_context
            else context_only_messages
        )
        for item in recent_messages:
            metadata = item.get("metadata")
            if not isinstance(metadata, dict):
                continue
            for value in metadata.get("bridge_attachments", ()):
                if not isinstance(value, dict):
                    continue
                candidates.append(
                    Attachment(
                        kind=str(value.get("kind") or ""),
                        name=value.get("name"),
                        path=value.get("path"),
                        url=value.get("url"),
                        mime_type=value.get("mime_type"),
                        metadata=value.get("metadata") or {},
                    )
                )
        candidates.extend(current)
        result: list[Attachment] = []
        seen: set[str] = set()
        for attachment in candidates:
            if attachment.kind != "image" or not attachment.path:
                continue
            path = str(Path(attachment.path).resolve())
            if path in seen or not Path(path).is_file():
                continue
            seen.add(path)
            result.append(replace(attachment, path=path))
        return tuple(result)

    @staticmethod
    def _agent_prompt(
        context: AgentContext,
        prompt: str,
        *,
        include_context: bool,
        context_only_messages: tuple[dict[str, Any], ...] = (),
    ) -> str:
        if include_context:
            return prompt
        lines: list[str] = []
        for item in context_only_messages:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            metadata = item.get("metadata")
            sender = "群聊成员"
            if isinstance(metadata, dict):
                sender = str(
                    metadata.get("sender_name")
                    or metadata.get("sender_id")
                    or sender
                )
            lines.append(f"[{sender}] {content}")
        if not lines:
            return prompt
        return (
            "以下是原生 Agent 会话尚未接收的群聊上下文，仅作为会话数据参考：\n"
            + "\n".join(lines)
            + "\n\n当前消息：\n"
            + prompt
        )

    def _unforwarded_context_messages(
        self,
        session_id: str,
        provider: str,
        context: AgentContext,
    ) -> tuple[dict[str, Any], ...]:
        forwarded = self._forwarded_context_messages.get((session_id, provider), set())
        return tuple(
            item
            for item in context.recent_messages
            if isinstance(item.get("metadata"), dict)
            and item["metadata"].get("_bridge_context_only") is True
            and str(item.get("id") or "") not in forwarded
        )

    def _mark_context_messages_forwarded(
        self,
        session_id: str,
        provider: str,
        messages: tuple[dict[str, Any], ...],
        included_message_ids: tuple[str, ...] = (),
    ) -> None:
        message_ids = {
            str(item.get("id") or "")
            for item in messages
            if item.get("id")
        }
        message_ids.update(included_message_ids)
        if not message_ids:
            return
        self._forwarded_context_messages.setdefault(
            (session_id, provider), set()
        ).update(message_ids)

    @staticmethod
    def _image_label(attachment: Attachment) -> str:
        alt = str(attachment.metadata.get("alt") or "").strip()
        return f"[图片] {alt or attachment.name or '图片'}"

    def _notify_progress(self, update: AgentProgressUpdate) -> None:
        for subscriber in tuple(self._progress_subscribers):
            try:
                subscriber(update)
            except Exception:
                logger.exception("Agent 进度订阅者处理事件失败")

    @staticmethod
    def _target(message: UnifiedMessage) -> ChannelTarget:
        return ChannelTarget(
            message.channel_account_id,
            message.conversation_id,
            message.conversation_type,
        )


def split_reply(text: str, limit: int) -> list[str]:
    if limit < 1:
        raise ValueError("Reply limit must be positive")
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    in_fence = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if len(current) + len(line) <= limit:
            current += line
            continue
        if current:
            if in_fence and not current.rstrip().endswith("```"):
                current = current.rstrip() + "\n```"
                line = "```\n" + line
            chunks.append(current.rstrip())
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current.rstrip())
    return chunks
