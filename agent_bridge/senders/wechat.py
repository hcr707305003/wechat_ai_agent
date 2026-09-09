from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from agent_bridge.models import (
    ChannelTarget,
    ContentType,
    OutboundDelivery,
    OutboundDeliveryStatus,
    OutboundMessage,
    ReplyReference,
    new_id,
    utc_now,
)
from agent_bridge.senders.activity import WindowsActivityMonitor
from agent_bridge.senders.foreground_driver import (
    ForegroundActionUnknown,
    ForegroundInputBusy,
    ForegroundQuoteUnavailable,
    ForegroundSendUnavailable,
    ForegroundTargetUnavailable,
    ForegroundUserInterrupted,
)
from agent_bridge.senders.foreground_process import (
    ForegroundOperationCoolingDown,
    ProcessIsolatedForegroundDriver,
)
from agent_bridge.senders.uia_driver import (
    SilentUiaTargetUnavailable,
    SilentUiaUnavailable,
    SilentWeChatUiaDriver,
    UserActivityInterrupted,
    WeChatInputBusy,
)
from agent_bridge.sessions.repository import SQLiteRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class WeChatSenderSettings:
    mode: str = "idle_uia"
    idle_seconds: float = 1.5
    max_queue_age_seconds: float = 600.0
    max_attempts: int = 3
    verify_sends: bool = True
    mouse_fallback: bool = False
    foreground_fallback_default: bool = False
    foreground_driver: str = "wechat_mcp"
    foreground_operation_timeout_seconds: float = 8.0
    foreground_quote_timeout_seconds: float = 30.0
    foreground_cooldown_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.mode not in {"idle_uia", "legacy_gui"}:
            raise ValueError("WeChat sender mode must be idle_uia or legacy_gui")
        if self.idle_seconds <= 0:
            raise ValueError("WeChat sender idle_seconds must be positive")
        if not 1 <= self.max_queue_age_seconds <= 86400:
            raise ValueError(
                "WeChat sender max_queue_age_seconds must be between 1 and 86400"
            )
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("WeChat sender max_attempts must be between 1 and 20")
        if self.mode == "idle_uia" and self.mouse_fallback:
            raise ValueError("WeChat idle_uia sender forbids mouse_fallback")
        if self.foreground_driver not in {"wechat_mcp", "uia"}:
            raise ValueError("WeChat foreground_driver must be wechat_mcp or uia")
        if (
            not math.isfinite(self.foreground_operation_timeout_seconds)
            or self.foreground_operation_timeout_seconds <= 0
        ):
            raise ValueError(
                "WeChat foreground_operation_timeout_seconds must be a finite positive number"
            )
        if (
            not math.isfinite(self.foreground_quote_timeout_seconds)
            or self.foreground_quote_timeout_seconds <= 0
        ):
            raise ValueError(
                "WeChat foreground_quote_timeout_seconds must be a finite positive number"
            )
        if (
            not math.isfinite(self.foreground_cooldown_seconds)
            or self.foreground_cooldown_seconds < 0
        ):
            raise ValueError(
                "WeChat foreground_cooldown_seconds must be a finite non-negative number"
            )


@dataclass(slots=True, frozen=True)
class SenderUpdate:
    state: str
    delivery: OutboundDelivery | None = None
    detail: str = ""


SenderSubscriber = Callable[[SenderUpdate], None]
TargetResolver = Callable[[str], tuple[str, ...]]
DeliveryVerifier = Callable[[OutboundDelivery], bool]
PendingRecorder = Callable[[str, str, ContentType], None]
PendingDiscarder = Callable[[str, str], None]
FallbackAuthorizer = Callable[[], bool]
QuoteReferenceResolver = Callable[[ReplyReference], ReplyReference]


class _ForegroundActivityGuard:
    def __init__(self, activity: Any, snapshot: Any) -> None:
        self._activity = activity
        self._snapshot = snapshot

    def __call__(self) -> bool:
        try:
            current = self._activity.snapshot()
        except OSError:
            return False
        return bool(
            getattr(current, "desktop_available", False)
            and getattr(current, "last_input_tick", None)
            == getattr(self._snapshot, "last_input_tick", None)
            and getattr(current, "cursor", None)
            == getattr(self._snapshot, "cursor", None)
        )

    def accept_synthetic_input(self, *, allow_cursor_change: bool = False) -> None:
        try:
            current = self._activity.snapshot()
        except OSError as error:
            raise ForegroundUserInterrupted(
                "User input state is unavailable"
            ) from error
        if (
            not getattr(current, "desktop_available", False)
            or (
                not allow_cursor_change
                and getattr(current, "cursor", None)
                != getattr(self._snapshot, "cursor", None)
            )
        ):
            raise ForegroundUserInterrupted("User input resumed")
        self._snapshot = current

    def rebase_after_restore(self) -> Any:
        try:
            current = self._activity.snapshot()
        except OSError as error:
            raise ForegroundUserInterrupted(
                "User input state is unavailable"
            ) from error
        if (
            not getattr(current, "desktop_available", False)
            or getattr(current, "last_input_tick", None)
            != getattr(self._snapshot, "last_input_tick", None)
        ):
            raise ForegroundUserInterrupted("User input resumed")
        self._snapshot = current
        return current


class BackgroundSendIneffective(RuntimeError):
    pass


class DeliveryResultUnknown(RuntimeError):
    pass


class WeChatSender(Protocol):
    @property
    def main_window_handle(self) -> int | None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def enqueue(self, target: ChannelTarget, message: OutboundMessage) -> str: ...
    async def retry(self, delivery_id: str) -> str: ...
    async def cancel(self, delivery_id: str) -> None: ...
    def subscribe(self, subscriber: SenderSubscriber) -> None: ...


class IdleUiaSender:
    _HANDLE_REFRESH_SECONDS = 1.0

    def __init__(
        self,
        settings: WeChatSenderSettings,
        repository: SQLiteRepository,
        resolve_target: TargetResolver,
        verify_delivery: DeliveryVerifier,
        record_pending: PendingRecorder,
        discard_pending: PendingDiscarder,
        *,
        activity: Any | None = None,
        driver: Any | None = None,
        clock: Callable[[], float] | None = None,
        poll_seconds: float = 0.2,
        retry_base_seconds: float = 1.0,
        foreground_allowed: FallbackAuthorizer | None = None,
        foreground_driver: Any | None = None,
        foreground_image_driver: Any | None = None,
        resolve_quote_reference: QuoteReferenceResolver | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.resolve_target = resolve_target
        self.verify_delivery = verify_delivery
        self.record_pending = record_pending
        self.discard_pending = discard_pending
        self.activity = activity or WindowsActivityMonitor()
        self.driver = driver or SilentWeChatUiaDriver()
        self._clock = clock or time.monotonic
        self._main_window_handle: int | None = None
        self._main_window_handle_checked_at: float | None = None
        self.poll_seconds = poll_seconds
        self.retry_base_seconds = retry_base_seconds
        self.foreground_allowed = foreground_allowed or (lambda: False)
        self.foreground_driver = foreground_driver or self._build_foreground_driver()
        self.foreground_image_driver = (
            foreground_image_driver
            or self._image_driver_for(self.foreground_driver)
        )
        self.resolve_quote_reference = resolve_quote_reference or (lambda reference: reference)
        self._subscribers: list[SenderSubscriber] = []
        self._wake = asyncio.Event()
        self._stopping = False
        self._worker: asyncio.Task[None] | None = None
        self._last_notification: tuple[str, str | None] | None = None

    def _build_foreground_driver(self) -> Any:
        return ProcessIsolatedForegroundDriver(
            self.settings.foreground_driver,
            timeout_seconds=self.settings.foreground_operation_timeout_seconds,
            quote_timeout_seconds=self.settings.foreground_quote_timeout_seconds,
            cooldown_seconds=self.settings.foreground_cooldown_seconds,
            idle_seconds=self.settings.idle_seconds,
            clock=self._clock,
        )

    @staticmethod
    def _image_driver_for(text_driver: Any) -> Any:
        if callable(getattr(text_driver, "send_image", None)):
            return text_driver
        from agent_bridge.senders.wechat_mcp_driver import WeChatMcpForegroundDriver

        return WeChatMcpForegroundDriver()

    @property
    def main_window_handle(self) -> int | None:
        now = self._clock()
        if (
            self._main_window_handle_checked_at is None
            or now - self._main_window_handle_checked_at >= self._HANDLE_REFRESH_SECONDS
        ):
            self._main_window_handle = self.driver.main_window_handle()
            self._main_window_handle_checked_at = now
        return self._main_window_handle

    def subscribe(self, subscriber: SenderSubscriber) -> None:
        self._subscribers.append(subscriber)

    async def start(self) -> None:
        if self._worker is not None:
            return
        self.repository.recover_outbound_deliveries()
        for delivery in self.repository.list_recovered_outbound_deliveries():
            self.record_pending(
                delivery.conversation_id, delivery.text, delivery.content_type
            )
        self._stopping = False
        self._worker = asyncio.create_task(self._run(), name="wechat-idle-uia-sender")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        closers = []
        seen = set()
        for driver in (self.foreground_driver, self.foreground_image_driver):
            close = getattr(driver, "close", None)
            if callable(close) and id(driver) not in seen:
                seen.add(id(driver))
                closers.append(close)
        for close in closers:
            await asyncio.to_thread(close)
        worker = self._worker
        self._worker = None
        if worker is not None:
            await worker

    async def enqueue(self, target: ChannelTarget, message: OutboundMessage) -> str:
        key = str(message.metadata.get("idempotency_key") or new_id("outbound_request"))
        content_type = (
            ContentType.IMAGE
            if any(item.kind == "image" for item in message.attachments)
            else ContentType.TEXT
        )
        delivery = self.repository.enqueue_outbound_delivery(
            target,
            message.text,
            key,
            self.settings.max_queue_age_seconds,
            content_type=content_type,
            attachments=message.attachments,
            reply_to=message.reply_to,
        )
        self._notify("queued", delivery)
        self._wake.set()
        return delivery.id

    async def retry(self, delivery_id: str) -> str:
        delivery = self.repository.retry_outbound_delivery(
            delivery_id, self.settings.max_queue_age_seconds
        )
        self._notify("queued", delivery)
        self._wake.set()
        return delivery.id

    async def resend(self, delivery_id: str) -> str:
        delivery = self.repository.resend_outbound_delivery(
            delivery_id, self.settings.max_queue_age_seconds
        )
        self._notify("queued", delivery)
        self._wake.set()
        return delivery.id

    async def cancel(self, delivery_id: str) -> None:
        delivery = self.repository.cancel_outbound_delivery(delivery_id)
        self._notify("cancelled", delivery)
        self._wake.set()

    async def _run(self) -> None:
        while not self._stopping:
            delivery = self.repository.claim_next_outbound_delivery()
            if delivery is None:
                self._notify("idle", None)
                await self._wait_for_wake()
                continue
            try:
                await self._deliver_when_idle(delivery)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(
                    "微信发送线程已隔离未处理异常: delivery=%s",
                    delivery.id,
                )
                self.discard_pending(delivery.conversation_id, delivery.text)
                updated = self.repository.update_outbound_delivery(
                    delivery.id,
                    OutboundDeliveryStatus.FAILED,
                    error_code="sender_internal_error",
                    error_message=f"{type(error).__name__}: {error}",
                    completed_at=utc_now(),
                )
                self._notify("failed", updated, updated.error_message or "")

    async def _wait_for_wake(self) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
        except TimeoutError:
            pass

    async def _deliver_when_idle(self, delivery: OutboundDelivery) -> None:
        if delivery.error_code in {
            "recovered_sending",
            "recovered_foreground_inflight",
        } and self.settings.verify_sends:
            if await asyncio.to_thread(self.verify_delivery, delivery):
                updated = self.repository.update_outbound_delivery(
                    delivery.id,
                    OutboundDeliveryStatus.SENT,
                    completed_at=utc_now(),
                )
                self._notify("sent", updated)
                return
            if delivery.error_code == "recovered_foreground_inflight":
                updated = self.repository.update_outbound_delivery(
                    delivery.id,
                    OutboundDeliveryStatus.FAILED,
                    error_code="delivery_unknown",
                    error_message="Foreground send was interrupted before verification",
                    completed_at=utc_now(),
                )
                self._notify("delivery_unknown", updated)
                return
            self.discard_pending(delivery.conversation_id, delivery.text)
        self._notify("waiting_for_idle", delivery)
        while not self._stopping:
            current = self.repository.get_outbound_delivery(delivery.id)
            if current is None or current.status == OutboundDeliveryStatus.CANCELLED:
                return
            if utc_now() >= delivery.expires_at:
                updated = self.repository.update_outbound_delivery(
                    delivery.id,
                    OutboundDeliveryStatus.EXPIRED,
                    error_code="queue_expired",
                    error_message="Delivery exceeded its queue age",
                    completed_at=utc_now(),
                )
                self._notify("expired", updated)
                return
            try:
                if self.activity.is_idle(self.settings.idle_seconds):
                    break
            except OSError as error:
                self._notify("waiting_for_idle", delivery, str(error))
            await asyncio.sleep(self.poll_seconds)
        if self._stopping:
            return

        current = self.repository.get_outbound_delivery(delivery.id)
        if current is None or current.status == OutboundDeliveryStatus.CANCELLED:
            return
        delivery = current
        if (
            delivery.reply_to is not None
            or delivery.content_type == ContentType.IMAGE
        ) and await self._defer_for_foreground_cooldown(delivery):
            return
        if delivery.attempts >= self.settings.max_attempts:
            updated = self.repository.update_outbound_delivery(
                delivery.id,
                OutboundDeliveryStatus.FAILED,
                error_code=delivery.error_code or "attempt_limit_exhausted",
                error_message=(
                    delivery.error_message
                    or "Delivery reached the configured attempt limit"
                ),
                completed_at=utc_now(),
            )
            self._notify("failed", updated, updated.error_message or "")
            return

        try:
            snapshot = self.activity.snapshot()
        except OSError as error:
            updated = self.repository.update_outbound_delivery(
                delivery.id,
                OutboundDeliveryStatus.WAITING_FOR_IDLE,
                next_attempt_at=utc_now() + timedelta(seconds=1),
                error_code="input_desktop_unavailable",
                error_message=str(error),
            )
            self._notify("waiting_for_idle", updated, str(error))
            return
        if self.activity.idle_seconds(snapshot) < self.settings.idle_seconds:
            updated = self.repository.update_outbound_delivery(
                delivery.id,
                OutboundDeliveryStatus.WAITING_FOR_IDLE,
                next_attempt_at=utc_now(),
                error_code="user_active",
                error_message="User input resumed before delivery",
            )
            self._notify("waiting_for_idle", updated)
            return
        attempts = delivery.attempts + 1
        attempt_started = utc_now()
        delivery = self.repository.update_outbound_delivery(
            delivery.id,
            OutboundDeliveryStatus.SENDING,
            attempts=attempts,
            next_attempt_at=attempt_started,
        )
        self._notify("sending", delivery)
        self.record_pending(
            delivery.conversation_id, delivery.text, delivery.content_type
        )
        quote_attempted = False
        try:
            targets = self.resolve_target(delivery.conversation_id)
            if delivery.reply_to is not None:
                quote_attempted = True
                quote_guard = _ForegroundActivityGuard(self.activity, snapshot)
                handled = await self._deliver_quoted(
                    delivery, targets, quote_guard
                )
                if handled:
                    return
                snapshot = quote_guard.rebase_after_restore()
            if delivery.content_type == ContentType.IMAGE:
                await self._deliver_with_foreground_fallback(
                    delivery,
                    targets,
                    snapshot,
                    SilentUiaUnavailable(
                        "Image delivery requires the foreground WeChat channel"
                    ),
                )
                return
            await asyncio.to_thread(
                self.driver.send_text,
                targets,
                delivery.text,
                lambda: self._guard_unchanged(snapshot),
            )
            try:
                post_send = self.activity.snapshot()
                post_send_violation = self._background_changed_without_input(
                    snapshot, post_send
                )
            except OSError:
                post_send_violation = False
            if self.settings.verify_sends:
                verified = await self._verify_after_send(delivery, "Background")
                if not verified:
                    draft = await asyncio.to_thread(
                        self._current_background_draft, targets
                    )
                    if draft == delivery.text:
                        raise BackgroundSendIneffective(
                            "WeChat send Invoke did not consume the system draft"
                        )
                    raise DeliveryResultUnknown(
                        "WeChat send action was not verified and the draft disappeared"
                    )
        except SilentUiaTargetUnavailable as error:
            await self._deliver_with_foreground_fallback(
                delivery, targets, snapshot, error
            )
            return
        except (SilentUiaUnavailable, BackgroundSendIneffective) as error:
            await self._deliver_with_foreground_fallback(
                delivery, targets, snapshot, error
            )
            return
        except DeliveryResultUnknown as error:
            confirmed = self._confirmed_delivery(delivery.id)
            if confirmed is not None:
                self._notify("sent", confirmed)
                return
            updated = self.repository.update_outbound_delivery(
                delivery.id,
                OutboundDeliveryStatus.FAILED,
                error_code="delivery_unknown",
                error_message=str(error),
                completed_at=utc_now(),
            )
            self._notify("delivery_unknown", updated, str(error))
            return
        except UserActivityInterrupted as error:
            self.discard_pending(delivery.conversation_id, delivery.text)
            try:
                current = self.activity.snapshot()
            except OSError:
                current = None
            if current is not None and self._background_changed_without_input(
                snapshot, current
            ):
                violation = RuntimeError(
                    "WeChat UIA changed the foreground window or cursor"
                )
                await self._handle_failure(
                    delivery, violation, error_code="background_violation"
                )
                return
            if quote_attempted:
                await self._handle_failure(
                    delivery,
                    error,
                    error_code="quote_fallback_interrupted",
                )
                return
            updated = self.repository.update_outbound_delivery(
                delivery.id,
                OutboundDeliveryStatus.WAITING_FOR_IDLE,
                attempts=delivery.attempts - 1,
                next_attempt_at=utc_now(),
                error_code="user_active",
                error_message=str(error),
            )
            self._notify("waiting_for_idle", updated, str(error))
            return
        except WeChatInputBusy as error:
            self.discard_pending(delivery.conversation_id, delivery.text)
            if quote_attempted:
                await self._handle_failure(
                    delivery,
                    error,
                    error_code="input_busy",
                )
                return
            updated = self.repository.update_outbound_delivery(
                delivery.id,
                OutboundDeliveryStatus.WAITING_FOR_IDLE,
                attempts=delivery.attempts - 1,
                next_attempt_at=utc_now() + timedelta(seconds=1),
                error_code="input_busy",
                error_message=str(error),
            )
            self._notify("waiting_for_idle", updated, str(error))
            return
        except (OSError, RuntimeError) as error:
            await self._handle_failure(delivery, error)
            return

        updated = self.repository.update_outbound_delivery(
            delivery.id,
            OutboundDeliveryStatus.SENT,
            completed_at=utc_now(),
        )
        self._notify("sent", updated)
        if post_send_violation:
            logger.warning(
                "微信 UIA 发送后改变了前台窗口或光标；后续发送将重新执行安全检查"
            )

    async def _deliver_quoted(
        self,
        delivery: OutboundDelivery,
        targets: tuple[str, ...],
        guard: Any,
    ) -> bool:
        assert delivery.reply_to is not None
        try:
            reference = await asyncio.to_thread(
                self.resolve_quote_reference, delivery.reply_to
            )
        except (LookupError, OSError, RuntimeError, ValueError) as error:
            logger.warning(
                "微信引用目标解析失败，降级普通发送: delivery=%s reference=%s error=%s",
                delivery.id,
                delivery.reply_to.message_id,
                error,
            )
            return False
        sender = getattr(self.foreground_driver, "send_quote", None)
        if not callable(sender):
            logger.warning(
                "微信引用驱动不可用，降级普通发送: delivery=%s",
                delivery.id,
            )
            return False
        image_path = (
            self._delivery_image_path(delivery)
            if delivery.content_type == ContentType.IMAGE
            else None
        )
        quote_text = delivery.text
        if image_path and (
            not quote_text.strip() or quote_text.lstrip().startswith("[图片]")
        ):
            quote_text = ""
        try:
            foreground_verified = await asyncio.to_thread(
                sender,
                targets,
                quote_text,
                reference,
                guard,
                image_path=image_path,
            )
            if self.settings.verify_sends and not foreground_verified:
                verified = await self._verify_after_send(delivery, "Quoted foreground")
                if not verified:
                    raise DeliveryResultUnknown(
                        "Quoted foreground send action was not verified"
                    )
        except ForegroundOperationCoolingDown as error:
            self.discard_pending(delivery.conversation_id, delivery.text)
            await self._defer_for_foreground_cooldown(
                delivery, undo_attempt=True, minimum=error.retry_after_seconds
            )
            return True
        except (ForegroundQuoteUnavailable, ForegroundTargetUnavailable) as error:
            logger.warning(
                "微信原生引用不可用，降级普通发送: delivery=%s reference=%s error=%s",
                delivery.id,
                reference.message_id,
                error,
            )
            return False
        except (DeliveryResultUnknown, ForegroundActionUnknown) as error:
            confirmed = self._confirmed_delivery(delivery.id)
            if confirmed is not None:
                self._notify("sent", confirmed)
                return True
            updated = self.repository.update_outbound_delivery(
                delivery.id,
                OutboundDeliveryStatus.FAILED,
                error_code="delivery_unknown",
                error_message=str(error),
                completed_at=utc_now(),
            )
            self._notify("delivery_unknown", updated, str(error))
            return True
        except ForegroundUserInterrupted as error:
            self.discard_pending(delivery.conversation_id, delivery.text)
            await self._defer_for_user_activity(delivery, error, undo_attempt=True)
            return True
        except ForegroundInputBusy as error:
            self.discard_pending(delivery.conversation_id, delivery.text)
            await self._handle_failure(
                delivery,
                error,
                error_code="input_busy",
            )
            return True
        updated = self.repository.update_outbound_delivery(
            delivery.id,
            OutboundDeliveryStatus.SENT,
            completed_at=utc_now(),
        )
        self._notify("foreground_sent", updated)
        return True

    async def _deliver_with_foreground_fallback(
        self,
        delivery: OutboundDelivery,
        targets: tuple[str, ...],
        snapshot: Any,
        background_error: Exception,
    ) -> None:
        if await self._defer_for_foreground_cooldown(
            delivery, undo_attempt=True
        ):
            self.discard_pending(delivery.conversation_id, delivery.text)
            return
        foreground_enabled = self.foreground_allowed()
        if not foreground_enabled or not self.settings.verify_sends:
            if delivery.content_type == ContentType.TEXT:
                await asyncio.to_thread(
                    self._clear_background_draft, targets, delivery.text
                )
            self.discard_pending(delivery.conversation_id, delivery.text)
            updated = self.repository.update_outbound_delivery(
                delivery.id,
                OutboundDeliveryStatus.FAILED,
                error_code=(
                    "foreground_not_authorized"
                    if not foreground_enabled
                    else "foreground_verification_required"
                ),
                error_message=(
                    str(background_error)
                    if not foreground_enabled
                    else "Foreground fallback requires verify_sends=true"
                ),
                completed_at=utc_now(),
            )
            self._notify(
                "foreground_unavailable", updated, updated.error_message or ""
            )
            return

        delivery = self.repository.update_outbound_delivery(
            delivery.id,
            OutboundDeliveryStatus.SENDING,
            error_code="foreground_inflight",
            error_message=str(background_error),
        )
        self._notify("foreground_sending", delivery, str(background_error))
        # Keep the same foreground activity guard for built-in screenshot
        # requests as for Agent-generated image deliveries.  WeChatMCP
        # temporarily activates the client and uses its input/clipboard; if
        # the user starts interacting, aborting is safer than leaving WeChat
        # in an unknown draft/search state.
        guard: Any = _ForegroundActivityGuard(self.activity, snapshot)
        try:
            if delivery.content_type == ContentType.IMAGE:
                image_path = self._delivery_image_path(delivery)
                await asyncio.to_thread(
                    self._send_foreground_image,
                    targets,
                    delivery.text,
                    image_path,
                    guard,
                )
            else:
                await asyncio.to_thread(
                    self.foreground_driver.send_text,
                    targets,
                    delivery.text,
                    guard,
                )
            if self.settings.verify_sends:
                verified = await self._verify_after_send(delivery, "Foreground")
                if not verified:
                    raise DeliveryResultUnknown(
                        "Foreground send action was not verified"
                    )
        except (DeliveryResultUnknown, ForegroundActionUnknown) as error:
            confirmed = self._confirmed_delivery(delivery.id)
            if confirmed is not None:
                self._notify("sent", confirmed)
                return
            updated = self.repository.update_outbound_delivery(
                delivery.id,
                OutboundDeliveryStatus.FAILED,
                error_code="delivery_unknown",
                error_message=str(error),
                completed_at=utc_now(),
            )
            self._notify("delivery_unknown", updated, str(error))
            return
        except (
            ForegroundOperationCoolingDown,
            ForegroundUserInterrupted,
        ) as error:
            # A guard interruption before the final send key is a transient
            # focus/cursor change, not a delivery failure.  Put the same
            # delivery back into the retry queue so screenshots and other
            # attachments are not lost merely because WeChatMCP refreshed a
            # control while the desktop was settling.
            self.discard_pending(delivery.conversation_id, delivery.text)
            if isinstance(error, ForegroundOperationCoolingDown):
                await self._defer_for_foreground_cooldown(
                    delivery,
                    undo_attempt=True,
                    minimum=error.retry_after_seconds,
                )
            else:
                await self._defer_for_user_activity(
                    delivery, error, undo_attempt=True
                )
            return
        except (
            ForegroundInputBusy,
            ForegroundSendUnavailable,
            ForegroundTargetUnavailable,
            OSError,
            RuntimeError,
        ) as error:
            self.discard_pending(delivery.conversation_id, delivery.text)
            updated = self.repository.update_outbound_delivery(
                delivery.id,
                OutboundDeliveryStatus.FAILED,
                error_code="foreground_failed",
                error_message=str(error),
                completed_at=utc_now(),
            )
            self._notify("failed", updated, str(error))
            return

        updated = self.repository.update_outbound_delivery(
            delivery.id,
            OutboundDeliveryStatus.SENT,
            completed_at=utc_now(),
        )
        self._notify("foreground_sent", updated)

    @staticmethod
    def _delivery_image_path(delivery: OutboundDelivery) -> str:
        images = [item for item in delivery.attachments if item.kind == "image"]
        if len(images) != 1 or not images[0].path:
            raise ForegroundSendUnavailable(
                "Image delivery must contain exactly one local image"
            )
        path = Path(images[0].path)
        if not path.is_file():
            raise ForegroundSendUnavailable("Image file is no longer available")
        return str(path)

    def _send_foreground_image(
        self,
        targets: tuple[str, ...],
        text: str,
        image_path: str,
        guard: Any,
    ) -> None:
        if text.strip() and not text.lstrip().startswith("[图片]"):
            sender = getattr(
                self.foreground_image_driver, "send_text_and_image", None
            )
            if not callable(sender):
                raise ForegroundSendUnavailable(
                    "The configured WeChat image driver cannot combine text and images"
                )
            sender(targets, text, image_path, guard)
            return
        sender = getattr(self.foreground_image_driver, "send_image", None)
        if not callable(sender):
            raise ForegroundSendUnavailable(
                "The configured WeChat image driver is unavailable"
            )
        sender(targets, image_path, guard)

    async def _verify_after_send(
        self, delivery: OutboundDelivery, action: str
    ) -> bool:
        try:
            return await asyncio.to_thread(self.verify_delivery, delivery)
        except (OSError, RuntimeError) as error:
            raise DeliveryResultUnknown(
                f"{action} send verification is unavailable: {error}"
            ) from error

    async def _defer_for_foreground_cooldown(
        self,
        delivery: OutboundDelivery,
        *,
        undo_attempt: bool = False,
        minimum: float = 0.0,
    ) -> bool:
        remaining = max(minimum, self._foreground_cooldown_remaining())
        if remaining <= 0:
            return False
        updated = self.repository.update_outbound_delivery(
            delivery.id,
            OutboundDeliveryStatus.WAITING_FOR_IDLE,
            attempts=max(0, delivery.attempts - 1) if undo_attempt else delivery.attempts,
            next_attempt_at=utc_now() + timedelta(seconds=remaining),
            error_code="foreground_cooldown",
            error_message=f"WeChat foreground automation cooling down for {remaining:.1f}s",
        )
        self._notify("waiting_for_idle", updated, updated.error_message or "")
        return True

    async def _defer_for_user_activity(
        self,
        delivery: OutboundDelivery,
        error: Exception,
        *,
        undo_attempt: bool = False,
    ) -> None:
        """Return a pre-send interruption to the idle queue without retry loss."""
        delay = max(self.poll_seconds, self.settings.idle_seconds)
        updated = self.repository.update_outbound_delivery(
            delivery.id,
            OutboundDeliveryStatus.WAITING_FOR_IDLE,
            attempts=(
                max(0, delivery.attempts - 1)
                if undo_attempt
                else delivery.attempts
            ),
            next_attempt_at=utc_now() + timedelta(seconds=delay),
            error_code="user_active",
            error_message=str(error),
        )
        self._notify("waiting_for_idle", updated, str(error))

    def _foreground_cooldown_remaining(self) -> float:
        values = []
        seen = set()
        for driver in (self.foreground_driver, self.foreground_image_driver):
            if id(driver) in seen:
                continue
            seen.add(id(driver))
            remaining = getattr(driver, "cooldown_remaining", None)
            if callable(remaining):
                try:
                    values.append(float(remaining()))
                except (TypeError, ValueError):
                    continue
        return max(values, default=0.0)

    def _confirmed_delivery(self, delivery_id: str) -> OutboundDelivery | None:
        current = self.repository.get_outbound_delivery(delivery_id)
        if current is not None and current.status == OutboundDeliveryStatus.SENT:
            return current
        return None

    def _current_background_draft(self, targets: tuple[str, ...]) -> str | None:
        reader = getattr(self.driver, "current_draft", None)
        if not callable(reader):
            return None
        return reader(targets)

    def _clear_background_draft(
        self, targets: tuple[str, ...], expected: str
    ) -> bool:
        clearer = getattr(self.driver, "clear_current_draft", None)
        if not callable(clearer):
            return False
        return bool(clearer(targets, expected))

    async def _handle_failure(
        self,
        delivery: OutboundDelivery,
        error: Exception,
        *,
        error_code: str | None = None,
    ) -> None:
        if error_code is not None:
            code = error_code
        elif isinstance(error, SilentUiaTargetUnavailable):
            code = "target_unavailable"
        elif isinstance(error, SilentUiaUnavailable):
            code = "uia_unavailable"
        else:
            code = "send_failed"
        if delivery.attempts >= self.settings.max_attempts:
            updated = self.repository.update_outbound_delivery(
                delivery.id,
                OutboundDeliveryStatus.FAILED,
                error_code=code,
                error_message=str(error),
                completed_at=utc_now(),
            )
            self._notify("unavailable" if code == "uia_unavailable" else "failed", updated, str(error))
            return
        delay = min(
            30.0,
            self.retry_base_seconds * float(2 ** (delivery.attempts - 1)),
        )
        updated = self.repository.update_outbound_delivery(
            delivery.id,
            OutboundDeliveryStatus.RETRY_WAIT,
            next_attempt_at=utc_now() + timedelta(seconds=delay),
            error_code=code,
            error_message=str(error),
        )
        self._notify("retrying", updated, str(error))

    def _notify(
        self, state: str, delivery: OutboundDelivery | None, detail: str = ""
    ) -> None:
        update = SenderUpdate(state, delivery, detail)
        notification_key = (state, delivery.id if delivery is not None else None)
        if notification_key == self._last_notification:
            return
        self._last_notification = notification_key
        for subscriber in tuple(self._subscribers):
            try:
                subscriber(update)
            except Exception:
                logger.exception("微信发送状态订阅者处理失败")

    @staticmethod
    def _background_changed_without_input(before: Any, after: Any) -> bool:
        input_unchanged = (
            getattr(after, "last_input_tick", None)
            == getattr(before, "last_input_tick", None)
        )
        background_changed = (
            getattr(after, "foreground_hwnd", None)
            != getattr(before, "foreground_hwnd", None)
            or getattr(after, "cursor", None) != getattr(before, "cursor", None)
        )
        return input_unchanged and background_changed

    def _guard_unchanged(self, snapshot: Any) -> bool:
        try:
            return bool(self.activity.unchanged(snapshot))
        except OSError:
            return False


class LegacyGuiSender:
    def __init__(self, gui: Any, settings: WeChatSenderSettings) -> None:
        self.gui = gui
        self.settings = settings
        self._subscribers: list[SenderSubscriber] = []

    @property
    def main_window_handle(self) -> int | None:
        return int(self.gui.main_hwnd)

    def subscribe(self, subscriber: SenderSubscriber) -> None:
        self._subscribers.append(subscriber)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def enqueue(self, target: ChannelTarget, message: OutboundMessage) -> str:
        result = await asyncio.to_thread(
            self.gui.send_msg,
            message.text,
            target.conversation_id,
            self.settings.verify_sends,
        )
        if not result.is_success:
            raise RuntimeError(str(result.get("message") or "WeChat send failed"))
        return new_id("wechat_outbound")

    async def retry(self, delivery_id: str) -> str:
        raise RuntimeError("Legacy WeChat sender does not persist deliveries")

    async def cancel(self, delivery_id: str) -> None:
        raise RuntimeError("Legacy WeChat sender does not persist deliveries")


def build_wechat_sender(
    settings: WeChatSenderSettings,
    repository: SQLiteRepository,
    resolve_target: TargetResolver,
    verify_delivery: DeliveryVerifier,
    record_pending: PendingRecorder,
    discard_pending: PendingDiscarder,
    foreground_allowed: FallbackAuthorizer | None = None,
    resolve_quote_reference: QuoteReferenceResolver | None = None,
) -> WeChatSender:
    if settings.mode == "legacy_gui":
        from wechatauto import WeChatGUI

        return LegacyGuiSender(WeChatGUI(), settings)
    return IdleUiaSender(
        settings,
        repository,
        resolve_target,
        verify_delivery,
        record_pending,
        discard_pending,
        foreground_allowed=foreground_allowed,
        resolve_quote_reference=resolve_quote_reference,
    )
