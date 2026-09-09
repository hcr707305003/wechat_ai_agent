import asyncio
from pathlib import Path

from agent_bridge.models import (
    Attachment,
    ChannelTarget,
    ContentType,
    ConversationType,
    OutboundDeliveryStatus,
    OutboundMessage,
    ReplyReference,
)
from agent_bridge.senders.activity import ActivitySnapshot
from agent_bridge.senders.foreground_driver import (
    ForegroundActionUnknown,
    ForegroundInputBusy,
    ForegroundQuoteUnavailable,
    ForegroundUserInterrupted,
)
from agent_bridge.senders.foreground_process import ForegroundOperationTimedOut
from agent_bridge.senders.uia_driver import (
    SilentUiaTargetUnavailable,
    SilentUiaUnavailable,
    UserActivityInterrupted,
)
from agent_bridge.senders.wechat import IdleUiaSender, WeChatSenderSettings
from agent_bridge.sessions.repository import SQLiteRepository


class FakeActivity:
    def __init__(self, idle: bool = True) -> None:
        self.idle = idle
        self.snapshot_value = ActivitySnapshot(1_000, 42, (10, 20), True)

    def is_idle(self, _seconds: float) -> bool:
        return self.idle

    def snapshot(self):
        return self.snapshot_value

    def idle_seconds(self, _snapshot=None) -> float:
        return 2.0 if self.idle else 0.0

    def unchanged(self, _snapshot) -> bool:
        return self.idle and self.snapshot_value == _snapshot


class FakeDriver:
    def __init__(self, errors=(), draft=None) -> None:
        self.errors = list(errors)
        self.calls = []
        self.draft = draft

    def main_window_handle(self):
        return 101

    def send_text(self, targets, text, guard) -> None:
        self.calls.append((tuple(targets), text))
        if self.errors:
            raise self.errors.pop(0)
        if not guard():
            raise UserActivityInterrupted("active")

    def current_draft(self, _targets):
        return self.draft


class FakeForegroundDriver:
    def __init__(self, error=None) -> None:
        self.error = error
        self.calls = []

    def send_text(self, targets, text, guard) -> None:
        self.calls.append((tuple(targets), text))
        if self.error is not None:
            raise self.error
        assert guard() is True

    def send_image(self, targets, image_path, guard) -> None:
        self.calls.append((tuple(targets), image_path))
        if self.error is not None:
            raise self.error
        assert guard() is True

    def send_text_and_image(self, targets, text, image_path, guard) -> None:
        self.calls.append((tuple(targets), text, image_path))
        if self.error is not None:
            raise self.error
        assert guard() is True

    def send_quote(
        self, targets, text, reference, guard, *, image_path=None
    ) -> None:
        self.calls.append((tuple(targets), text, reference, image_path))
        if self.error is not None:
            raise self.error
        assert guard() is True


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def wait_for_status(repository, delivery_id, status) -> None:
    for _ in range(200):
        delivery = repository.get_outbound_delivery(delivery_id)
        if delivery is not None and delivery.status == status:
            return
        await asyncio.sleep(0.01)
    current = repository.get_outbound_delivery(delivery_id)
    raise AssertionError(f"delivery did not reach {status}: {current}")


def build_sender(
    tmp_path: Path,
    *,
    activity=None,
    driver=None,
    attempts=3,
    foreground_allowed=None,
    foreground_driver=None,
    foreground_image_driver=None,
    verifier=None,
    verify_sends=True,
    resolve_quote_reference=None,
    retry_base_seconds=0.01,
    idle_seconds=1.5,
):
    repository = SQLiteRepository(tmp_path / "bridge.db")
    pending = []
    sender = IdleUiaSender(
        WeChatSenderSettings(
            max_attempts=attempts,
            verify_sends=verify_sends,
            idle_seconds=idle_seconds,
        ),
        repository,
        lambda conversation_id: ("Friend", conversation_id),
        verifier or (lambda _delivery: True),
        lambda conversation_id, text, _content_type: pending.append(
            (conversation_id, text)
        ),
        lambda conversation_id, text: pending.remove((conversation_id, text)),
        activity=activity or FakeActivity(),
        driver=driver or FakeDriver(),
        poll_seconds=0.01,
        retry_base_seconds=retry_base_seconds,
        foreground_allowed=foreground_allowed,
        foreground_driver=foreground_driver,
        foreground_image_driver=foreground_image_driver,
        resolve_quote_reference=resolve_quote_reference,
    )
    return repository, sender, pending


def quote_reference() -> ReplyReference:
    return ReplyReference(
        "friend:7",
        "friend",
        ConversationType.PRIVATE,
        "sender",
        "original",
        occurrence_from_latest=0,
    )


async def test_quoted_delivery_bypasses_background_sender(tmp_path: Path) -> None:
    background = FakeDriver()
    foreground = FakeForegroundDriver()
    reference = quote_reference()
    repository, sender, _pending = build_sender(
        tmp_path,
        driver=background,
        foreground_driver=foreground,
        resolve_quote_reference=lambda value: value,
    )
    await sender.start()

    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("reply", reply_to=reference),
    )
    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)

    assert background.calls == []
    assert foreground.calls == [
        (("Friend", "friend"), "reply", reference, None)
    ]
    await sender.stop()
    repository.close()


async def test_foreground_verified_quote_skips_delayed_database_verification(
    tmp_path: Path,
) -> None:
    class VerifiedForeground(FakeForegroundDriver):
        def send_quote(
            self, targets, text, reference, guard, *, image_path=None
        ) -> bool:
            super().send_quote(
                targets,
                text,
                reference,
                guard,
                image_path=image_path,
            )
            return True

    verifier_calls = []
    foreground = VerifiedForeground()
    repository, sender, _pending = build_sender(
        tmp_path,
        foreground_driver=foreground,
        resolve_quote_reference=lambda value: value,
        verifier=lambda delivery: verifier_calls.append(delivery) or False,
    )
    await sender.start()

    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("reply", reply_to=quote_reference()),
    )
    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)

    assert verifier_calls == []
    await sender.stop()
    repository.close()


async def test_quoted_image_does_not_send_placeholder_text(tmp_path: Path) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"image")
    foreground = FakeForegroundDriver()
    reference = quote_reference()
    repository, sender, _pending = build_sender(
        tmp_path,
        foreground_driver=foreground,
        resolve_quote_reference=lambda value: value,
    )
    await sender.start()

    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage(
            "[图片] 截图",
            attachments=(Attachment("image", "result.png", str(image)),),
            reply_to=reference,
        ),
    )
    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)

    assert foreground.calls == [
        (("Friend", "friend"), "", reference, str(image))
    ]
    await sender.stop()
    repository.close()


async def test_quote_lookup_failure_falls_back_to_plain_send(tmp_path: Path) -> None:
    background = FakeDriver()
    foreground = FakeForegroundDriver(ForegroundQuoteUnavailable("missing"))
    repository, sender, _pending = build_sender(
        tmp_path,
        driver=background,
        foreground_driver=foreground,
        resolve_quote_reference=lambda value: value,
    )
    await sender.start()

    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("reply", reply_to=quote_reference()),
    )
    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)

    assert len(foreground.calls) == 1
    assert background.calls == [(('Friend', 'friend'), 'reply')]
    await sender.stop()
    repository.close()


async def test_quote_reference_resolution_failure_sends_plain_text_once(
    tmp_path: Path,
) -> None:
    background = FakeDriver()
    foreground = FakeForegroundDriver()

    def missing_reference(_reference):
        raise LookupError("absent from recent 30 messages")

    repository, sender, _pending = build_sender(
        tmp_path,
        driver=background,
        foreground_driver=foreground,
        resolve_quote_reference=missing_reference,
    )
    await sender.start()

    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("reply", reply_to=quote_reference()),
    )
    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)

    assert foreground.calls == []
    assert background.calls == [(('Friend', 'friend'), 'reply')]
    await sender.stop()
    repository.close()


async def test_quote_fallback_rebases_accepted_synthetic_input(
    tmp_path: Path,
) -> None:
    activity = FakeActivity()
    background = FakeDriver()

    class QuoteUnavailableAfterSyntheticInput(FakeForegroundDriver):
        def send_quote(
            self, targets, text, reference, guard, *, image_path=None
        ) -> None:
            self.calls.append((tuple(targets), text, reference, image_path))
            activity.snapshot_value = ActivitySnapshot(
                1_001, 101, (30, 40), True
            )
            guard.accept_synthetic_input(allow_cursor_change=True)
            activity.snapshot_value = ActivitySnapshot(
                1_001, 42, (10, 20), True
            )
            raise ForegroundQuoteUnavailable("missing banner")

    foreground = QuoteUnavailableAfterSyntheticInput()
    repository, sender, _pending = build_sender(
        tmp_path,
        activity=activity,
        driver=background,
        attempts=1,
        foreground_driver=foreground,
        resolve_quote_reference=lambda value: value,
    )
    await sender.start()

    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("reply", reply_to=quote_reference()),
    )
    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)

    assert len(foreground.calls) == 1
    assert background.calls == [(('Friend', 'friend'), 'reply')]
    await sender.stop()
    repository.close()


async def test_quote_fallback_interruption_stops_at_max_attempts(
    tmp_path: Path,
) -> None:
    background = FakeDriver(
        [UserActivityInterrupted("active") for _ in range(3)]
    )
    foreground = FakeForegroundDriver(ForegroundQuoteUnavailable("missing"))
    repository, sender, _pending = build_sender(
        tmp_path,
        driver=background,
        attempts=3,
        foreground_driver=foreground,
        resolve_quote_reference=lambda value: value,
        retry_base_seconds=0,
    )
    await sender.start()

    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("reply", reply_to=quote_reference()),
    )
    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.FAILED)
    delivery = repository.get_outbound_delivery(delivery_id)

    assert delivery is not None
    assert delivery.attempts == 3
    assert len(foreground.calls) == 3
    assert len(background.calls) == 3
    await asyncio.sleep(0.03)
    assert len(foreground.calls) == 3
    await sender.stop()
    repository.close()


async def test_quote_input_busy_stops_at_max_attempts(tmp_path: Path) -> None:
    background = FakeDriver()
    foreground = FakeForegroundDriver(ForegroundInputBusy("busy"))
    repository, sender, _pending = build_sender(
        tmp_path,
        driver=background,
        attempts=3,
        foreground_driver=foreground,
        resolve_quote_reference=lambda value: value,
        retry_base_seconds=0,
    )
    await sender.start()

    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("reply", reply_to=quote_reference()),
    )
    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.FAILED)
    delivery = repository.get_outbound_delivery(delivery_id)

    assert delivery is not None
    assert delivery.attempts == 3
    assert len(foreground.calls) == 3
    assert background.calls == []
    await sender.stop()
    repository.close()


async def test_unknown_quoted_send_never_falls_back_or_duplicates(tmp_path: Path) -> None:
    background = FakeDriver()
    foreground = FakeForegroundDriver(ForegroundActionUnknown("unknown"))
    repository, sender, _pending = build_sender(
        tmp_path,
        driver=background,
        foreground_driver=foreground,
        resolve_quote_reference=lambda value: value,
    )
    await sender.start()

    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("reply", reply_to=quote_reference()),
    )
    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.FAILED)
    delivery = repository.get_outbound_delivery(delivery_id)

    assert delivery is not None and delivery.error_code == "delivery_unknown"
    assert background.calls == []
    assert len(foreground.calls) == 1
    await sender.stop()
    repository.close()


def test_main_window_handle_refreshes_without_polling_every_frame(tmp_path: Path) -> None:
    class RefreshingDriver(FakeDriver):
        def __init__(self) -> None:
            super().__init__()
            self.handles = [None, 202]
            self.handle_calls = 0

        def main_window_handle(self):
            self.handle_calls += 1
            return self.handles.pop(0)

    clock = FakeClock()
    driver = RefreshingDriver()
    repository = SQLiteRepository(tmp_path / "bridge.db")
    sender = IdleUiaSender(
        WeChatSenderSettings(),
        repository,
        lambda conversation_id: (conversation_id,),
        lambda _delivery: True,
        lambda _conversation_id, _text, _content_type: None,
        lambda _conversation_id, _text: None,
        activity=FakeActivity(),
        driver=driver,
        clock=clock,
    )

    assert sender.main_window_handle is None
    assert sender.main_window_handle is None
    assert driver.handle_calls == 1

    clock.now = 1.0

    assert sender.main_window_handle == 202
    assert driver.handle_calls == 2
    repository.close()


async def test_idle_sender_waits_for_idle_then_sends(tmp_path: Path) -> None:
    activity = FakeActivity(idle=False)
    driver = FakeDriver()
    repository, sender, pending = build_sender(
        tmp_path, activity=activity, driver=driver
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )

    await asyncio.sleep(0.04)
    assert driver.calls == []
    activity.idle = True
    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)

    assert driver.calls == [(('Friend', 'friend'), 'hello')]
    assert pending == [("friend", "hello")]
    await sender.stop()
    repository.close()


async def test_idle_sender_queues_sent_delivery_again(tmp_path: Path) -> None:
    repository, sender, _pending = build_sender(tmp_path)
    original = repository.enqueue_outbound_delivery(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        "hello",
        "job:sent",
        600,
    )
    repository.update_outbound_delivery(
        original.id,
        OutboundDeliveryStatus.SENT,
        completed_at=original.created_at,
    )
    updates = []
    sender.subscribe(updates.append)

    delivery_id = await sender.resend(original.id)

    resent = repository.get_outbound_delivery(delivery_id)
    assert resent is not None and resent.status == OutboundDeliveryStatus.QUEUED
    assert resent.text == "hello"
    assert updates[-1].state == "queued"
    repository.close()


async def test_user_interruption_does_not_consume_retry(tmp_path: Path) -> None:
    driver = FakeDriver([UserActivityInterrupted("active")])
    repository, sender, _pending = build_sender(tmp_path, driver=driver)
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)
    delivery = repository.get_outbound_delivery(delivery_id)

    assert len(driver.calls) == 2
    assert delivery is not None and delivery.attempts == 1
    await sender.stop()
    repository.close()


async def test_uia_failure_stops_when_foreground_fallback_is_not_authorized(
    tmp_path: Path,
) -> None:
    driver = FakeDriver(
        [SilentUiaUnavailable("missing")]
    )
    repository, sender, _pending = build_sender(
        tmp_path, driver=driver, attempts=2
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.FAILED)
    delivery = repository.get_outbound_delivery(delivery_id)

    assert delivery is not None and delivery.error_code == "foreground_not_authorized"
    assert delivery.attempts == 1
    next_delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("next"),
    )
    await wait_for_status(
        repository, next_delivery_id, OutboundDeliveryStatus.SENT
    )
    assert len(driver.calls) == 2
    await sender.stop()
    repository.close()


async def test_uia_failure_uses_authorized_foreground_fallback_once(
    tmp_path: Path,
) -> None:
    driver = FakeDriver([SilentUiaUnavailable("missing")])
    foreground = FakeForegroundDriver()
    repository, sender, _pending = build_sender(
        tmp_path,
        driver=driver,
        foreground_allowed=lambda: True,
        foreground_driver=foreground,
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)

    assert len(driver.calls) == 1
    assert foreground.calls == [(("Friend", "friend"), "hello")]
    await sender.stop()
    repository.close()


async def test_image_delivery_skips_background_and_uses_foreground(
    tmp_path: Path,
) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"image")
    driver = FakeDriver()
    foreground = FakeForegroundDriver()
    repository, sender, pending = build_sender(
        tmp_path,
        driver=driver,
        foreground_allowed=lambda: True,
        foreground_driver=foreground,
    )
    await sender.start()

    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage(
            "[图片] 结果图",
            attachments=(
                Attachment("image", "result.png", str(image), mime_type="image/png"),
            ),
        ),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)
    delivery = repository.get_outbound_delivery(delivery_id)
    assert delivery is not None and delivery.content_type == ContentType.IMAGE
    assert driver.calls == []
    assert foreground.calls == [(('Friend', 'friend'), str(image))]
    assert pending == [("friend", "[图片] 结果图")]
    await sender.stop()
    repository.close()


async def test_foreground_user_interruption_retries_image_delivery(
    tmp_path: Path,
) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"image")

    class InterruptingForegroundDriver(FakeForegroundDriver):
        def send_image(self, targets, image_path, guard) -> None:
            self.calls.append((tuple(targets), image_path))
            if len(self.calls) == 1:
                raise ForegroundUserInterrupted("User input resumed")
            assert guard() is True

    foreground = InterruptingForegroundDriver()
    repository, sender, _pending = build_sender(
        tmp_path,
        foreground_allowed=lambda: True,
        foreground_driver=foreground,
        idle_seconds=0.001,
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage(
            "[图片] 截图",
            attachments=(Attachment("image", "result.png", str(image)),),
        ),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)
    delivery = repository.get_outbound_delivery(delivery_id)
    assert delivery is not None and delivery.attempts == 1
    assert len(foreground.calls) == 2
    await sender.stop()
    repository.close()


async def test_foreground_user_interruption_defers_quote_without_consuming_attempt(
    tmp_path: Path,
) -> None:
    foreground = FakeForegroundDriver(
        ForegroundUserInterrupted("User input resumed")
    )
    repository, sender, _pending = build_sender(
        tmp_path,
        attempts=1,
        foreground_driver=foreground,
        resolve_quote_reference=lambda value: value,
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("reply", reply_to=quote_reference()),
    )

    await wait_for_status(
        repository, delivery_id, OutboundDeliveryStatus.WAITING_FOR_IDLE
    )
    delivery = repository.get_outbound_delivery(delivery_id)

    assert delivery is not None
    assert delivery.error_code == "user_active"
    assert delivery.attempts == 0
    await sender.stop()
    repository.close()


async def test_text_and_image_delivery_uses_one_combined_foreground_action(
    tmp_path: Path,
) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"image")
    foreground = FakeForegroundDriver()
    repository, sender, _pending = build_sender(
        tmp_path,
        foreground_allowed=lambda: True,
        foreground_driver=foreground,
    )
    await sender.start()

    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage(
            "处理完成。",
            attachments=(
                Attachment("image", "result.png", str(image), mime_type="image/png"),
            ),
        ),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)

    assert foreground.calls == [
        (("Friend", "friend"), "处理完成。", str(image))
    ]
    await sender.stop()
    repository.close()


async def test_uia_text_configuration_routes_images_through_mcp(
    tmp_path: Path, monkeypatch,
) -> None:
    image_driver = FakeForegroundDriver()
    monkeypatch.setattr(
        "agent_bridge.senders.wechat_mcp_driver.WeChatMcpForegroundDriver",
        lambda: image_driver,
    )
    from agent_bridge.senders.foreground_process import _build_child_drivers

    text_driver, routed_image_driver = _build_child_drivers("uia")

    assert text_driver is not image_driver
    assert routed_image_driver is image_driver


async def test_foreground_timeout_is_unknown_and_never_retried(
    tmp_path: Path,
) -> None:
    background = FakeDriver([SilentUiaUnavailable("missing")])
    foreground = FakeForegroundDriver(
        ForegroundOperationTimedOut("foreground timed out")
    )
    repository, sender, _pending = build_sender(
        tmp_path,
        driver=background,
        foreground_allowed=lambda: True,
        foreground_driver=foreground,
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.FAILED)
    delivery = repository.get_outbound_delivery(delivery_id)

    assert delivery is not None and delivery.error_code == "delivery_unknown"
    assert delivery.attempts == 1
    assert len(foreground.calls) == 1
    await sender.stop()
    repository.close()


async def test_foreground_cooldown_defers_quote_without_consuming_attempt(
    tmp_path: Path,
) -> None:
    class CoolingForeground(FakeForegroundDriver):
        def cooldown_remaining(self) -> float:
            return 30.0

    foreground = CoolingForeground()
    repository, sender, _pending = build_sender(
        tmp_path,
        foreground_driver=foreground,
        resolve_quote_reference=lambda value: value,
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("reply", reply_to=quote_reference()),
    )

    await wait_for_status(
        repository, delivery_id, OutboundDeliveryStatus.WAITING_FOR_IDLE
    )
    delivery = repository.get_outbound_delivery(delivery_id)

    assert delivery is not None and delivery.error_code == "foreground_cooldown"
    assert delivery.attempts == 0
    assert foreground.calls == []
    await sender.stop()
    repository.close()


async def test_foreground_cooldown_defers_plain_fallback_without_consuming_attempt(
    tmp_path: Path,
) -> None:
    class CoolingForeground(FakeForegroundDriver):
        def cooldown_remaining(self) -> float:
            return 30.0

    background = FakeDriver([SilentUiaUnavailable("missing")])
    foreground = CoolingForeground()
    repository, sender, _pending = build_sender(
        tmp_path,
        driver=background,
        foreground_allowed=lambda: True,
        foreground_driver=foreground,
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("reply"),
    )

    await wait_for_status(
        repository, delivery_id, OutboundDeliveryStatus.WAITING_FOR_IDLE
    )
    delivery = repository.get_outbound_delivery(delivery_id)

    assert delivery is not None and delivery.error_code == "foreground_cooldown"
    assert delivery.attempts == 0
    assert foreground.calls == []
    await sender.stop()
    repository.close()


async def test_unexpected_image_driver_error_does_not_kill_sender_worker(
    tmp_path: Path,
) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"image")

    class BrokenImageDriver:
        def send_image(self, _targets, _image_path, _guard) -> None:
            raise AttributeError("broken image capability")

    driver = FakeDriver()
    repository, sender, _pending = build_sender(
        tmp_path,
        driver=driver,
        foreground_allowed=lambda: True,
        foreground_driver=FakeForegroundDriver(),
        foreground_image_driver=BrokenImageDriver(),
    )
    await sender.start()
    failed_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage(
            "[图片] 结果图",
            attachments=(Attachment("image", path=str(image)),),
        ),
    )

    await wait_for_status(repository, failed_id, OutboundDeliveryStatus.FAILED)
    failed = repository.get_outbound_delivery(failed_id)
    assert failed is not None and failed.error_code == "sender_internal_error"

    next_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("next"),
    )
    await wait_for_status(repository, next_id, OutboundDeliveryStatus.SENT)
    assert driver.calls == [(('Friend', 'friend'), 'next')]
    await sender.stop()
    repository.close()


async def test_background_target_open_failure_uses_authorized_mcp_fallback(
    tmp_path: Path,
) -> None:
    driver = FakeDriver(
        [SilentUiaTargetUnavailable("search result did not open")]
    )
    foreground = FakeForegroundDriver()
    repository, sender, _pending = build_sender(
        tmp_path,
        driver=driver,
        foreground_allowed=lambda: True,
        foreground_driver=foreground,
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)

    assert driver.calls == [(("Friend", "friend"), "hello")]
    assert foreground.calls == [(("Friend", "friend"), "hello")]
    await sender.stop()
    repository.close()


async def test_foreground_guard_accepts_its_own_navigation_key(
    tmp_path: Path,
) -> None:
    activity = FakeActivity()
    driver = FakeDriver([SilentUiaUnavailable("missing")])

    class NavigatingForegroundDriver(FakeForegroundDriver):
        def send_text(self, targets, text, guard) -> None:
            self.calls.append((tuple(targets), text))
            assert guard() is True
            activity.snapshot_value = ActivitySnapshot(
                1_001, 101, (10, 20), True
            )
            guard.accept_synthetic_input()
            assert guard() is True

    foreground = NavigatingForegroundDriver()
    repository, sender, _pending = build_sender(
        tmp_path,
        activity=activity,
        driver=driver,
        foreground_allowed=lambda: True,
        foreground_driver=foreground,
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)

    assert foreground.calls == [(("Friend", "friend"), "hello")]
    await sender.stop()
    repository.close()


async def test_unverified_foreground_action_is_terminal_unknown(
    tmp_path: Path,
) -> None:
    driver = FakeDriver([SilentUiaUnavailable("missing")])
    foreground = FakeForegroundDriver()
    repository, sender, _pending = build_sender(
        tmp_path,
        driver=driver,
        foreground_allowed=lambda: True,
        foreground_driver=foreground,
        verifier=lambda _delivery: False,
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.FAILED)
    delivery = repository.get_outbound_delivery(delivery_id)

    assert delivery is not None and delivery.error_code == "delivery_unknown"
    assert foreground.calls == [(("Friend", "friend"), "hello")]
    await asyncio.sleep(0.03)
    assert len(driver.calls) == 1
    await sender.stop()
    repository.close()


async def test_foreground_verification_exception_is_unknown_without_resend(
    tmp_path: Path,
) -> None:
    driver = FakeDriver([SilentUiaUnavailable("missing")])
    foreground = FakeForegroundDriver()

    def unavailable_verifier(_delivery):
        raise RuntimeError(
            "数据库合并失败(文件被微信并发改写): message/message_1.db"
        )

    repository, sender, pending = build_sender(
        tmp_path,
        driver=driver,
        foreground_allowed=lambda: True,
        foreground_driver=foreground,
        verifier=unavailable_verifier,
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.FAILED)
    delivery = repository.get_outbound_delivery(delivery_id)

    assert delivery is not None and delivery.error_code == "delivery_unknown"
    assert foreground.calls == [(("Friend", "friend"), "hello")]
    assert driver.calls == [(("Friend", "friend"), "hello")]
    assert pending == [("friend", "hello")]
    await asyncio.sleep(0.03)
    assert len(foreground.calls) == 1
    await sender.stop()
    repository.close()


async def test_ineffective_background_invoke_reuses_draft_in_foreground(
    tmp_path: Path,
) -> None:
    driver = FakeDriver(draft="hello")
    foreground = FakeForegroundDriver()
    verification = iter([False, True])
    repository, sender, _pending = build_sender(
        tmp_path,
        driver=driver,
        foreground_allowed=lambda: True,
        foreground_driver=foreground,
        verifier=lambda _delivery: next(verification),
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)

    assert driver.calls == [(("Friend", "friend"), "hello")]
    assert foreground.calls == [(("Friend", "friend"), "hello")]
    await sender.stop()
    repository.close()


async def test_foreground_fallback_requires_delivery_verification(
    tmp_path: Path,
) -> None:
    driver = FakeDriver([SilentUiaUnavailable("missing")])
    foreground = FakeForegroundDriver()
    repository, sender, _pending = build_sender(
        tmp_path,
        driver=driver,
        foreground_allowed=lambda: True,
        foreground_driver=foreground,
        verify_sends=False,
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.FAILED)
    delivery = repository.get_outbound_delivery(delivery_id)

    assert delivery is not None
    assert delivery.error_code == "foreground_verification_required"
    assert foreground.calls == []
    await sender.stop()
    repository.close()


async def test_waiting_delivery_can_be_cancelled_without_sending(tmp_path: Path) -> None:
    activity = FakeActivity(idle=False)
    driver = FakeDriver()
    repository, sender, _pending = build_sender(
        tmp_path, activity=activity, driver=driver
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )
    await wait_for_status(
        repository, delivery_id, OutboundDeliveryStatus.WAITING_FOR_IDLE
    )

    await sender.cancel(delivery_id)
    await asyncio.sleep(0.03)

    delivery = repository.get_outbound_delivery(delivery_id)
    assert delivery is not None and delivery.status == OutboundDeliveryStatus.CANCELLED
    assert driver.calls == []
    await sender.stop()
    repository.close()


async def test_recovered_inflight_delivery_is_verified_before_resend(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    target = ChannelTarget("bot", "friend", ConversationType.PRIVATE)
    delivery = repository.enqueue_outbound_delivery(target, "hello", "job:1", 600)
    repository.update_outbound_delivery(
        delivery.id, OutboundDeliveryStatus.SENDING, attempts=1
    )
    driver = FakeDriver()
    sender = IdleUiaSender(
        WeChatSenderSettings(verify_sends=True),
        repository,
        lambda conversation_id: (conversation_id,),
        lambda _delivery: True,
        lambda _conversation_id, _text, _content_type: None,
        lambda _conversation_id, _text: None,
        activity=FakeActivity(),
        driver=driver,
        poll_seconds=0.01,
    )

    await sender.start()
    await wait_for_status(repository, delivery.id, OutboundDeliveryStatus.SENT)

    assert driver.calls == []
    await sender.stop()
    repository.close()


async def test_recovered_foreground_inflight_is_never_automatically_resent(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    target = ChannelTarget("bot", "friend", ConversationType.PRIVATE)
    delivery = repository.enqueue_outbound_delivery(target, "hello", "job:fg", 600)
    repository.update_outbound_delivery(
        delivery.id,
        OutboundDeliveryStatus.SENDING,
        attempts=1,
        error_code="foreground_inflight",
    )
    driver = FakeDriver()
    sender = IdleUiaSender(
        WeChatSenderSettings(verify_sends=True),
        repository,
        lambda conversation_id: (conversation_id,),
        lambda _delivery: False,
        lambda _conversation_id, _text, _content_type: None,
        lambda _conversation_id, _text: None,
        activity=FakeActivity(),
        driver=driver,
        poll_seconds=0.01,
    )

    await sender.start()
    await wait_for_status(repository, delivery.id, OutboundDeliveryStatus.FAILED)
    recovered = repository.get_outbound_delivery(delivery.id)

    assert recovered is not None and recovered.error_code == "delivery_unknown"
    assert driver.calls == []
    await sender.stop()
    repository.close()


async def test_uia_focus_change_retries_without_disabling_sender(tmp_path: Path) -> None:
    activity = FakeActivity()

    class FocusChangingDriver(FakeDriver):
        def send_text(self, targets, text, guard) -> None:
            self.calls.append((tuple(targets), text))
            if len(self.calls) == 1:
                activity.snapshot_value = ActivitySnapshot(
                    1_000, 99, (10, 20), True
                )
                assert guard() is False
                raise UserActivityInterrupted("foreground changed")
            assert guard() is True

    driver = FocusChangingDriver()
    repository, sender, _pending = build_sender(
        tmp_path, activity=activity, driver=driver
    )
    await sender.start()
    first = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("one"),
    )
    await wait_for_status(repository, first, OutboundDeliveryStatus.SENT)
    second = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("two"),
    )
    await wait_for_status(repository, second, OutboundDeliveryStatus.SENT)

    assert len(driver.calls) == 3
    await sender.stop()
    repository.close()


async def test_temporary_input_probe_failure_does_not_crash_worker(
    tmp_path: Path,
) -> None:
    class FlakyActivity(FakeActivity):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 1

        def is_idle(self, seconds: float) -> bool:
            if self.failures:
                self.failures -= 1
                raise OSError("input desktop unavailable")
            return super().is_idle(seconds)

    activity = FlakyActivity()
    driver = FakeDriver()
    repository, sender, _pending = build_sender(
        tmp_path, activity=activity, driver=driver
    )
    await sender.start()
    delivery_id = await sender.enqueue(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        OutboundMessage("hello"),
    )

    await wait_for_status(repository, delivery_id, OutboundDeliveryStatus.SENT)

    assert len(driver.calls) == 1
    await sender.stop()
    repository.close()
