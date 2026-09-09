import time
from threading import Thread

import pytest

from agent_bridge.models import ConversationType, ReplyReference
from agent_bridge.senders.foreground_driver import ForegroundQuoteUnavailable
from agent_bridge.senders.foreground_process import (
    ForegroundOperationCoolingDown,
    ForegroundOperationTimedOut,
    ProcessIsolatedForegroundDriver,
)


def _success_worker(connection, request) -> None:
    connection.send(
        {
            "status": "ok",
            "result": request["action"] == "quote",
        }
    )
    connection.close()


def _hanging_worker(connection, request) -> None:
    time.sleep(30)


def _quote_unavailable_worker(connection, request) -> None:
    connection.send(
        {
            "status": "error",
            "kind": "ForegroundQuoteUnavailable",
            "message": "quote target is unavailable",
        }
    )
    connection.close()


def _slow_quote_worker(connection, request) -> None:
    if request["action"] == "quote":
        time.sleep(0.3)
    connection.send({"status": "ok", "result": True})
    connection.close()


def test_process_driver_returns_child_result() -> None:
    driver = ProcessIsolatedForegroundDriver(
        "uia",
        timeout_seconds=2,
        cooldown_seconds=1,
        idle_seconds=0.1,
        worker=_success_worker,
    )
    reference = ReplyReference(
        "friend:1",
        "friend",
        ConversationType.PRIVATE,
        "friend",
        "hello",
        occurrence_from_latest=0,
    )

    class Guard:
        synchronized = False

        def __call__(self) -> bool:
            return True

        def accept_synthetic_input(self, *, allow_cursor_change=False) -> None:
            assert allow_cursor_change is False
            self.synchronized = True

    guard = Guard()

    assert driver.send_quote(("Friend",), "reply", reference, guard) is True
    assert guard.synchronized is True
    assert driver.cooldown_remaining() == 0
    driver.close()


def test_process_driver_uses_dedicated_quote_timeout() -> None:
    driver = ProcessIsolatedForegroundDriver(
        "uia",
        timeout_seconds=0.15,
        quote_timeout_seconds=2,
        cooldown_seconds=1,
        idle_seconds=0.1,
        worker=_slow_quote_worker,
    )
    reference = ReplyReference(
        "friend:1",
        "friend",
        ConversationType.PRIVATE,
        "friend",
        "hello",
        occurrence_from_latest=0,
    )

    assert driver.send_quote(("Friend",), "reply", reference, lambda: True) is True
    driver.close()


def test_process_driver_terminates_hang_and_opens_circuit() -> None:
    driver = ProcessIsolatedForegroundDriver(
        "uia",
        timeout_seconds=0.15,
        cooldown_seconds=1,
        idle_seconds=0.1,
        worker=_hanging_worker,
    )
    started = time.monotonic()

    with pytest.raises(ForegroundOperationTimedOut):
        driver.send_text(("Friend",), "reply", lambda: True)

    assert time.monotonic() - started < 2
    assert driver.cooldown_remaining() > 0
    with pytest.raises(ForegroundOperationCoolingDown):
        driver.send_text(("Friend",), "reply", lambda: True)
    driver.close()


def test_process_driver_restores_known_child_error_type() -> None:
    driver = ProcessIsolatedForegroundDriver(
        "uia",
        timeout_seconds=2,
        cooldown_seconds=1,
        idle_seconds=0.1,
        worker=_quote_unavailable_worker,
    )

    with pytest.raises(ForegroundQuoteUnavailable, match="quote target"):
        driver.send_text(("Friend",), "reply", lambda: True)

    driver.close()


def test_process_driver_accepts_new_work_after_cooldown() -> None:
    driver = ProcessIsolatedForegroundDriver(
        "uia",
        timeout_seconds=0.15,
        cooldown_seconds=0.1,
        idle_seconds=0.1,
        worker=_hanging_worker,
    )

    with pytest.raises(ForegroundOperationTimedOut):
        driver.send_text(("Friend",), "first", lambda: True)

    time.sleep(0.15)
    driver.timeout_seconds = 2
    driver._worker = _success_worker
    driver.send_text(("Friend",), "second", lambda: True)
    driver.close()


def test_process_driver_close_terminates_active_child() -> None:
    driver = ProcessIsolatedForegroundDriver(
        "uia",
        timeout_seconds=10,
        cooldown_seconds=1,
        idle_seconds=0.1,
        worker=_hanging_worker,
    )
    errors = []

    def send() -> None:
        try:
            driver.send_text(("Friend",), "reply", lambda: True)
        except Exception as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    thread = Thread(target=send)
    thread.start()
    deadline = time.monotonic() + 2
    while driver._active_process is None and time.monotonic() < deadline:
        time.sleep(0.01)

    driver.close()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors
