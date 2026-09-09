from __future__ import annotations

import json
import socket
from io import BytesIO
from urllib.error import URLError

import pytest

from agent_bridge.senders.wechat_hook_driver import (
    HookDeliveryStatus,
    HookQuoteReference,
    WeChatHookQuoteDriver,
    WeChatHookRejected,
    WeChatHookResultUnknown,
    WeChatHookUnavailable,
)


class Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def reference() -> HookQuoteReference:
    return HookQuoteReference(
        conversation_id="friend",
        sender_wxid="wxid_friend",
        sender_name="工藤新一",
        message_type=1,
        content="引用测试",
        server_id="987654321",
        local_id="246",
    )


def test_driver_sends_exact_payload_and_token() -> None:
    calls = []

    def send(request, timeout):
        calls.append((request, timeout))
        return Response(
            json.dumps(
                {"status": "confirmed", "request_id": "request-1"}
            ).encode()
        )

    driver = WeChatHookQuoteDriver(
        "http://127.0.0.1:30001",
        "secret",
        timeout_seconds=1.5,
        request_sender=send,
    )

    result = driver.send_quote(
        "wxid_target", "aaa", reference(), request_id="request-1"
    )

    request, timeout = calls[0]
    body = json.loads(request.data.decode())
    assert request.full_url == "http://127.0.0.1:30001/SendQuoteMsg"
    assert request.get_header("X-agent-bridge-token") == "secret"
    assert timeout == 1.5
    assert body["to_wxid"] == "wxid_target"
    assert body["content"] == "aaa"
    assert body["reference"]["server_id"] == "987654321"
    assert body["reference"]["local_id"] == "246"
    assert result.status is HookDeliveryStatus.CONFIRMED


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://0.0.0.0:30001",
        "http://localhost:30001",
        "http://192.168.1.2:30001",
        "https://127.0.0.1:30001",
        "http://127.0.0.1:30001/api",
        "http://127.0.0.1",
    ],
)
def test_driver_rejects_non_loopback_or_ambiguous_endpoint(endpoint: str) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        WeChatHookQuoteDriver(endpoint, "secret")


def test_reference_requires_stable_message_identity() -> None:
    with pytest.raises(ValueError, match="server_id or local_id"):
        HookQuoteReference("friend", "wxid_friend", "Friend", 1, "same")


@pytest.mark.parametrize("status", ["accepted", "confirmed"])
def test_driver_accepts_only_nonfailure_terminal_statuses(status: str) -> None:
    def send(_request, _timeout):
        return Response(
            json.dumps({"status": status, "request_id": "same"}).encode()
        )

    result = WeChatHookQuoteDriver(
        "http://[::1]:30001", "secret", request_sender=send
    ).send_quote("friend", "reply", reference(), request_id="same")

    assert result.status.value == status


def test_explicit_rejection_is_safe_to_surface() -> None:
    def send(_request, _timeout):
        return Response(
            b'{"status":"rejected","request_id":"same","detail":"bad ref"}'
        )

    driver = WeChatHookQuoteDriver(
        "http://127.0.0.1:30001", "secret", request_sender=send
    )

    with pytest.raises(WeChatHookRejected, match="bad ref"):
        driver.send_quote("friend", "reply", reference(), request_id="same")


@pytest.mark.parametrize(
    "failure",
    [
        socket.timeout(),
        TimeoutError(),
        URLError(socket.timeout()),
    ],
)
def test_timeout_after_quote_request_is_unknown(failure: Exception) -> None:
    def send(_request, _timeout):
        raise failure

    driver = WeChatHookQuoteDriver(
        "http://127.0.0.1:30001", "secret", request_sender=send
    )

    with pytest.raises(WeChatHookResultUnknown):
        driver.send_quote("friend", "reply", reference(), request_id="same")


def test_probe_failure_is_pre_mutation_unavailable() -> None:
    def send(_request, _timeout):
        raise URLError(ConnectionRefusedError())

    driver = WeChatHookQuoteDriver(
        "http://127.0.0.1:30001", "secret", request_sender=send
    )

    with pytest.raises(WeChatHookUnavailable):
        driver.probe_status()


def test_malformed_or_mismatched_response_is_unknown() -> None:
    responses = iter(
        [
            Response(b"not-json"),
            Response(b'{"status":"confirmed","request_id":"other"}'),
        ]
    )

    driver = WeChatHookQuoteDriver(
        "http://127.0.0.1:30001",
        "secret",
        request_sender=lambda _request, _timeout: next(responses),
    )

    with pytest.raises(WeChatHookResultUnknown):
        driver.send_quote("friend", "reply", reference(), request_id="same")
    with pytest.raises(WeChatHookResultUnknown):
        driver.send_quote("friend", "reply", reference(), request_id="same")
