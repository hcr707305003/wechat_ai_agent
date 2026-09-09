from __future__ import annotations

import multiprocessing
import threading
import time
from collections.abc import Callable, Iterable
from multiprocessing.connection import Connection
from typing import Any

from agent_bridge.models import ReplyReference
from agent_bridge.senders.activity import WindowsActivityMonitor
from agent_bridge.senders.foreground_driver import (
    ForegroundActionUnknown,
    ForegroundDriverChain,
    ForegroundInputBusy,
    ForegroundQuoteUnavailable,
    ForegroundSendUnavailable,
    ForegroundTargetUnavailable,
    ForegroundUserInterrupted,
    ForegroundWeChatUiaDriver,
)


class ForegroundOperationTimedOut(ForegroundActionUnknown):
    """A foreground child exceeded its hard deadline and was terminated."""


class ForegroundOperationCoolingDown(RuntimeError):
    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        super().__init__(
            "WeChat foreground automation is cooling down for "
            f"{self.retry_after_seconds:.1f}s"
        )


class _ChildActivityGuard:
    def __init__(self, idle_seconds: float) -> None:
        self._activity = WindowsActivityMonitor()
        self._snapshot = self._activity.snapshot()
        if self._activity.idle_seconds(self._snapshot) < idle_seconds:
            raise ForegroundUserInterrupted("User input resumed")

    def __call__(self) -> bool:
        try:
            current = self._activity.snapshot()
        except OSError:
            return False
        return bool(
            current.desktop_available
            and current.last_input_tick == self._snapshot.last_input_tick
            and current.cursor == self._snapshot.cursor
        )

    def accept_synthetic_input(self, *, allow_cursor_change: bool = False) -> None:
        current = self._activity.snapshot()
        if (
            not current.desktop_available
            or (
                not allow_cursor_change
                and current.cursor != self._snapshot.cursor
            )
        ):
            raise ForegroundUserInterrupted("User input resumed")
        self._snapshot = current


def _build_child_drivers(driver_name: str) -> tuple[Any, Any]:
    from agent_bridge.senders.wechat_mcp_driver import WeChatMcpForegroundDriver

    uia_driver = ForegroundWeChatUiaDriver()
    if driver_name == "uia":
        return uia_driver, WeChatMcpForegroundDriver()

    chain = ForegroundDriverChain(WeChatMcpForegroundDriver(), uia_driver)
    return chain, chain


def _foreground_worker(connection: Connection, request: dict[str, Any]) -> None:
    try:
        guard = _ChildActivityGuard(float(request["idle_seconds"]))
        text_driver, image_driver = _build_child_drivers(str(request["driver"]))
        action = str(request["action"])
        targets = tuple(str(item) for item in request["targets"])
        text = str(request.get("text") or "")
        image_path = request.get("image_path")
        if action == "text":
            result = text_driver.send_text(targets, text, guard)
        elif action == "image":
            result = image_driver.send_image(targets, str(image_path), guard)
        elif action == "text_image":
            result = image_driver.send_text_and_image(
                targets, text, str(image_path), guard
            )
        elif action == "quote":
            reference = request.get("reference")
            if not isinstance(reference, ReplyReference):
                raise ValueError("Quote request is missing its reference")
            result = text_driver.send_quote(
                targets,
                text,
                reference,
                guard,
                image_path=str(image_path) if image_path else None,
            )
        else:
            raise ValueError(f"Unsupported foreground action: {action}")
        connection.send({"status": "ok", "result": result})
    except BaseException as error:  # noqa: BLE001 - process boundary serialization
        try:
            connection.send(
                {
                    "status": "error",
                    "kind": type(error).__name__,
                    "message": str(error),
                }
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


_ERROR_TYPES: dict[str, type[Exception]] = {
    "ForegroundActionUnknown": ForegroundActionUnknown,
    "ForegroundInputBusy": ForegroundInputBusy,
    "ForegroundQuoteUnavailable": ForegroundQuoteUnavailable,
    "ForegroundSendUnavailable": ForegroundSendUnavailable,
    "ForegroundTargetUnavailable": ForegroundTargetUnavailable,
    "ForegroundUserInterrupted": ForegroundUserInterrupted,
}


class ProcessIsolatedForegroundDriver:
    """Run one foreground automation request in one killable child process."""

    def __init__(
        self,
        driver_name: str,
        *,
        timeout_seconds: float,
        quote_timeout_seconds: float | None = None,
        cooldown_seconds: float,
        idle_seconds: float,
        clock: Callable[[], float] | None = None,
        worker: Callable[[Connection, dict[str, Any]], None] = _foreground_worker,
    ) -> None:
        self.driver_name = driver_name
        self.timeout_seconds = float(timeout_seconds)
        self.quote_timeout_seconds = float(
            timeout_seconds
            if quote_timeout_seconds is None
            else quote_timeout_seconds
        )
        self.cooldown_seconds = float(cooldown_seconds)
        self.idle_seconds = float(idle_seconds)
        self._clock = clock or time.monotonic
        self._worker = worker
        self._context = multiprocessing.get_context("spawn")
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_process: multiprocessing.Process | None = None
        self._cooldown_until = 0.0
        self._closed = False

    def cooldown_remaining(self) -> float:
        with self._state_lock:
            return max(0.0, self._cooldown_until - self._clock())

    def send_text(self, targets: Iterable[str], text: str, guard: Callable) -> None:
        self._execute("text", targets, guard, text=text)

    def send_image(
        self, targets: Iterable[str], image_path: str, guard: Callable
    ) -> None:
        self._execute("image", targets, guard, image_path=image_path)

    def send_text_and_image(
        self,
        targets: Iterable[str],
        text: str,
        image_path: str,
        guard: Callable,
    ) -> None:
        self._execute(
            "text_image", targets, guard, text=text, image_path=image_path
        )

    def send_quote(
        self,
        targets: Iterable[str],
        text: str,
        reference: ReplyReference,
        guard: Callable,
        *,
        image_path: str | None = None,
    ) -> bool:
        return bool(
            self._execute(
                "quote",
                targets,
                guard,
                text=text,
                image_path=image_path,
                reference=reference,
            )
        )

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            process = self._active_process
        if process is not None:
            self._terminate(process)

    def _execute(
        self,
        action: str,
        targets: Iterable[str],
        guard: Callable,
        **payload: Any,
    ) -> Any:
        if not guard():
            raise ForegroundUserInterrupted("User input resumed")
        with self._run_lock:
            remaining = self.cooldown_remaining()
            if remaining > 0:
                raise ForegroundOperationCoolingDown(remaining)
            with self._state_lock:
                if self._closed:
                    raise ForegroundSendUnavailable(
                        "WeChat foreground automation is stopped"
                    )
            request = {
                "action": action,
                "targets": tuple(targets),
                "driver": self.driver_name,
                "idle_seconds": self.idle_seconds,
                **payload,
            }
            receiver, sender = self._context.Pipe(duplex=False)
            process = self._context.Process(
                target=self._worker,
                args=(sender, request),
                name="wechat-foreground-operation",
                daemon=True,
            )
            started = False
            try:
                process.start()
                started = True
                sender.close()
                with self._state_lock:
                    stopped = self._closed
                    if not stopped:
                        self._active_process = process
                if stopped:
                    self._terminate(process)
                    raise ForegroundActionUnknown(
                        "WeChat foreground operation was stopped"
                    )
                timeout_seconds = (
                    self.quote_timeout_seconds
                    if action == "quote"
                    else self.timeout_seconds
                )
                response = self._wait_for_result(
                    process, receiver, timeout_seconds
                )
                self._synchronize_parent_guard(guard)
            finally:
                sender.close()
                receiver.close()
                with self._state_lock:
                    if self._active_process is process:
                        self._active_process = None
                if started:
                    if process.is_alive():
                        self._terminate(process)
                    else:
                        process.join(timeout=0.2)
            if response.get("status") == "ok":
                return response.get("result")
            kind = str(response.get("kind") or "")
            message = str(response.get("message") or kind or "Foreground failed")
            error_type = _ERROR_TYPES.get(kind, ForegroundSendUnavailable)
            raise error_type(message)

    @staticmethod
    def _synchronize_parent_guard(guard: Callable) -> None:
        accept = getattr(guard, "accept_synthetic_input", None)
        if not callable(accept):
            return
        try:
            accept(allow_cursor_change=False)
        except TypeError:
            accept()

    def _wait_for_result(
        self,
        process: multiprocessing.Process,
        receiver: Connection,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        deadline = self._clock() + timeout_seconds
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                self._trip_circuit()
                self._terminate(process)
                raise ForegroundOperationTimedOut(
                    "WeChat foreground operation exceeded "
                    f"{timeout_seconds:.1f}s and was terminated"
                )
            if receiver.poll(min(0.05, remaining)):
                try:
                    response = receiver.recv()
                except EOFError:
                    break
                if isinstance(response, dict):
                    return response
                break
            if not process.is_alive():
                break
        self._trip_circuit()
        raise ForegroundActionUnknown(
            "WeChat foreground helper exited without a result"
        )

    def _trip_circuit(self) -> None:
        with self._state_lock:
            self._cooldown_until = max(
                self._cooldown_until, self._clock() + self.cooldown_seconds
            )

    @staticmethod
    def _terminate(process: multiprocessing.Process) -> None:
        if not process.is_alive():
            process.join(timeout=0.2)
            return
        process.terminate()
        process.join(timeout=0.5)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=0.5)
