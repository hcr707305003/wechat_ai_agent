from __future__ import annotations

import json
import socket
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class WeChatHookError(RuntimeError):
    pass


class WeChatHookUnavailable(WeChatHookError):
    """The request was rejected before a send result was accepted."""


class WeChatHookRejected(WeChatHookError):
    """The Hook explicitly rejected the request before mutation."""


class WeChatHookResultUnknown(WeChatHookError):
    """The Hook may have accepted the request, so retrying is unsafe."""


class HookDeliveryStatus(StrEnum):
    ACCEPTED = "accepted"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HookQuoteReference:
    conversation_id: str
    sender_wxid: str
    sender_name: str
    message_type: int
    content: str
    server_id: str | None = None
    local_id: str | None = None

    def __post_init__(self) -> None:
        if not self.conversation_id.strip():
            raise ValueError("Quote conversation ID is required")
        if not self.sender_wxid.strip():
            raise ValueError("Quote sender wxid is required")
        if not (self.server_id or self.local_id):
            raise ValueError("Quote server_id or local_id is required")


@dataclass(frozen=True, slots=True)
class HookDeliveryResult:
    status: HookDeliveryStatus
    request_id: str
    detail: str = ""


RequestSender = Callable[[Request, float], Any]


@dataclass(frozen=True, slots=True)
class WeChatHookQuoteSettings:
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:30001"
    token_env: str = "AGENT_BRIDGE_WECHAT_HOOK_TOKEN"
    timeout_seconds: float = 2.0
    expected_version: str = "4.1.12.55"

    def __post_init__(self) -> None:
        WeChatHookQuoteDriver._validate_endpoint(self.endpoint)
        if not self.token_env.strip():
            raise ValueError("WeChat Hook token_env is required")
        if self.timeout_seconds <= 0:
            raise ValueError("WeChat Hook timeout must be positive")
        if not self.expected_version.strip():
            raise ValueError("WeChat Hook expected_version is required")


def _default_request_sender(request: Request, timeout: float) -> Any:
    return urlopen(request, timeout=timeout)


class WeChatHookQuoteDriver:
    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        timeout_seconds: float = 2.0,
        request_sender: RequestSender | None = None,
    ) -> None:
        self.endpoint = self._validate_endpoint(endpoint)
        self.token = token.strip()
        if not self.token:
            raise ValueError("WeChat Hook token is required")
        if timeout_seconds <= 0:
            raise ValueError("WeChat Hook timeout must be positive")
        self.timeout_seconds = timeout_seconds
        self._request_sender = request_sender or _default_request_sender

    @staticmethod
    def _validate_endpoint(value: str) -> str:
        parsed = urlsplit(value.strip())
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
            raise ValueError("WeChat Hook endpoint must use loopback HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("WeChat Hook endpoint must not contain credentials or query data")
        if parsed.path not in ("", "/"):
            raise ValueError("WeChat Hook endpoint must not contain a path")
        if parsed.port is None:
            raise ValueError("WeChat Hook endpoint must include an explicit port")
        return value.strip().rstrip("/")

    def probe_status(self) -> dict[str, Any]:
        payload = self._request("GET", "/status")
        if not isinstance(payload, dict):
            raise WeChatHookUnavailable("Malformed WeChat Hook status response")
        return payload

    def send_quote(
        self,
        to_wxid: str,
        text: str,
        reference: HookQuoteReference,
        *,
        request_id: str | None = None,
    ) -> HookDeliveryResult:
        target = to_wxid.strip()
        if not target:
            raise ValueError("Quote target wxid is required")
        if not text:
            raise ValueError("Quoted reply text is required")
        identifier = request_id or str(uuid.uuid4())
        payload = self._request(
            "POST",
            "/SendQuoteMsg",
            {
                "to_wxid": target,
                "content": text,
                "reference": asdict(reference),
                "request_id": identifier,
            },
            may_mutate=True,
        )
        if not isinstance(payload, dict):
            raise WeChatHookResultUnknown("Malformed WeChat Hook quote response")
        raw_status = str(payload.get("status") or "").strip().lower()
        try:
            status = HookDeliveryStatus(raw_status)
        except ValueError as error:
            raise WeChatHookResultUnknown(
                f"Unrecognized WeChat Hook quote status: {raw_status or 'empty'}"
            ) from error
        detail = str(payload.get("detail") or payload.get("message") or "")
        response_id = str(payload.get("request_id") or identifier)
        if response_id != identifier:
            raise WeChatHookResultUnknown("WeChat Hook response request_id mismatch")
        if status is HookDeliveryStatus.REJECTED:
            raise WeChatHookRejected(detail or "WeChat Hook rejected quoted reply")
        if status is HookDeliveryStatus.UNKNOWN:
            raise WeChatHookResultUnknown(detail or "WeChat Hook quote result is unknown")
        return HookDeliveryResult(status, identifier, detail)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        may_mutate: bool = False,
    ) -> Any:
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            self.endpoint + path,
            data=encoded,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Agent-Bridge-Token": self.token,
            },
        )
        try:
            response = self._request_sender(request, self.timeout_seconds)
            with response:
                raw = response.read()
        except HTTPError as error:
            if may_mutate and error.code >= 500:
                raise WeChatHookResultUnknown(
                    f"WeChat Hook HTTP {error.code} after quote request"
                ) from error
            raise WeChatHookUnavailable(f"WeChat Hook HTTP {error.code}") from error
        except (TimeoutError, socket.timeout) as error:
            exception = WeChatHookResultUnknown if may_mutate else WeChatHookUnavailable
            raise exception("WeChat Hook request timed out") from error
        except URLError as error:
            reason = error.reason
            if may_mutate and isinstance(reason, (TimeoutError, socket.timeout)):
                raise WeChatHookResultUnknown("WeChat Hook request timed out") from error
            raise WeChatHookUnavailable(f"WeChat Hook is unavailable: {reason}") from error
        except OSError as error:
            raise WeChatHookUnavailable("WeChat Hook connection failed") from error
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            exception = WeChatHookResultUnknown if may_mutate else WeChatHookUnavailable
            raise exception("WeChat Hook returned invalid JSON") from error
