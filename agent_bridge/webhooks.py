"""Opt-in, bounded, process-local webhook delivery, independent of Agent replies."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from queue import Empty, Full, Queue
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from agent_bridge.models import ContentType, UnifiedMessage
from agent_bridge.webhook_uploads import (
    MEDIA_MARKERS,
    UploadError,
    UploadSettings,
    parse_upload,
    upload_media,
)

logger = logging.getLogger(__name__)
QUEUE_LIMIT = 200
MAX_PAYLOAD_BYTES = 1024 * 1024
HTTP_METHODS = ("POST", "PUT", "PATCH")
CONTENT_TYPES = tuple(kind.value for kind in ContentType)
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_RESERVED_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection", "keep-alive", "te",
    "trailer", "upgrade", "expect", "proxy-authorization", "proxy-connection",
    "content-encoding", "x-agent-bridge-event-id",
}


def validated_headers(value: object, *, multipart: bool = False) -> dict[str, str]:
    # The editor can retain duplicate draft rows; never silently overwrite them.
    if isinstance(value, dict):
        rows = list(value.items())
    elif isinstance(value, list):
        if any(not isinstance(row, dict) or set(row) != {"name", "value"} for row in value):
            raise ValueError("Webhook headers 列表每项必须包含 name 和 value")
        rows = [(row["name"], row["value"]) for row in value]
    else:
        raise TypeError("Webhook headers 必须是名称/值映射或列表")
    if len(rows) > 32:
        raise ValueError("Webhook 最多设置 32 个请求头")
    result, seen, size = {}, set(), 0
    for index, (name, content) in enumerate(rows, 1):
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise ValueError(f"Webhook 请求头 #{index} 名称无效")
        lowered = name.lower()
        if lowered in seen:
            raise ValueError(f"Webhook 请求头 #{index} 名称重复（不区分大小写）")
        if lowered in _RESERVED_HEADERS:
            raise ValueError(f"Webhook 请求头 #{index} 属于程序管理的传输字段，不可自定义")
        if multipart and lowered == "content-type":
            raise ValueError("上传 Content-Type 由程序自动生成 multipart/form-data 边界，请勿自定义")
        if not isinstance(content, str) or any(not 32 <= ord(c) <= 126 for c in content):
            raise ValueError(f"Webhook 请求头 #{index} 值必须是无换行的可打印 ASCII 文本")
        if lowered == "content-type" and not re.fullmatch(
            r"application/(?:json|[a-z0-9!#$&^_.+-]+\+json)(?:\s*;\s*charset=utf-8)?", content, re.IGNORECASE
        ):
            raise ValueError("Webhook 正文固定为 UTF-8 JSON，Content-Type 必须是 JSON 媒体类型")
        size += len(name) + len(content) + 4
        if size > 16 * 1024:
            raise ValueError("Webhook 请求头总大小不能超过 16 KiB")
        result[name] = content
        seen.add(lowered)
    return result


def validated_content_types(value: object) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(kind, str) or kind not in CONTENT_TYPES for kind in value
    ):
        raise ValueError("Webhook content_types 必须是类型列表：text/image/file/voice/video/unknown；[] 表示全部")
    return list(dict.fromkeys(value))


def validated_url(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("URL 必须是字符串")
    if not value:
        return
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and parsed.username is None and parsed.password is None and not parsed.fragment
                 and not any(c.isspace() or ord(c) < 32 for c in value))
        if not valid or parsed.port == 0:
            raise ValueError
    except ValueError:
        raise ValueError("Webhook URL 必须是有效 HTTP/HTTPS 地址，不含用户密码或片段") from None


@dataclass(frozen=True, slots=True)
class WebhookSettings:
    name: str = "Webhook"
    url: str = ""
    enabled: bool = False
    conversation_type: str = "all"
    sender: str = "others"
    include_ai_replies: bool = False
    timeout_seconds: float = 5.0
    max_attempts: int = 3
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    content_types: list[str] = field(default_factory=list)
    payload_format: str = "basic"
    upload: UploadSettings = field(default_factory=UploadSettings)

    def __post_init__(self) -> None:
        if not isinstance(self.method, str) or self.method not in HTTP_METHODS:
            raise ValueError("Webhook method 必须是 POST/PUT/PATCH")
        object.__setattr__(self, "headers", validated_headers(self.headers))
        object.__setattr__(self, "content_types", validated_content_types(self.content_types))
        object.__setattr__(self, "upload", parse_upload(self.upload))
        if self.payload_format not in ("basic", "memory"):
            raise ValueError("Webhook payload_format 必须是 basic/memory")
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 80:
            raise ValueError("Webhook name 必须是 1–80 个字符")
        if type(self.enabled) is not bool or type(self.include_ai_replies) is not bool:
            raise ValueError("Webhook enabled/include_ai_replies 必须是布尔值")
        if self.conversation_type not in {"all", "private", "group"}:
            raise ValueError("Webhook conversation_type 必须是 all/private/group")
        if self.sender not in {"all", "self", "others"}:
            raise ValueError("Webhook sender 必须是 all/self/others")
        if (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or not 1 <= self.timeout_seconds <= 60):
            raise ValueError("Webhook timeout_seconds 必须在 1–60 秒之间")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 10:
            raise ValueError("Webhook max_attempts 必须是 1–10（含首次发送）")
        if not isinstance(self.url, str) or (self.enabled and not self.url):
            raise ValueError("Webhook 启用时必须填写 URL")
        validated_url(self.url)

    def matches(self, message: UnifiedMessage) -> bool:
        is_self = message.metadata.get("is_self") is True
        is_ai = message.metadata.get("bridge_outbound") is True and is_self
        return (
            self.enabled
            and (not self.content_types or message.content_type.value in self.content_types)
            and (self.conversation_type == "all" or self.conversation_type == message.conversation_type.value)
            and (self.sender == "all" or (self.sender == "self" and is_self)
                 or (self.sender == "others" and not is_self))
            and (not is_ai or self.include_ai_replies)
        )


def parse_webhooks(value: object) -> tuple[WebhookSettings, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError("channels.wechat.webhooks 必须是列表")
    if len(value) > 32:
        raise ValueError("最多配置 32 个 webhook")
    result = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict) or set(item) - set(WebhookSettings.__dataclass_fields__):
            raise ValueError(f"Webhook #{index} 配置项无效或含未知字段")
        try:
            result.append(WebhookSettings(**item))
        except (TypeError, ValueError) as error:
            raise ValueError(f"Webhook #{index}: {error}") from None
    return tuple(result)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_json(settings: WebhookSettings, body: bytes, event_id: str) -> int:
    """Do not follow redirects or forward private messages via environment proxies."""
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    headers = {
        "content-type": "application/json; charset=utf-8",
        "x-agent-bridge-event-id": event_id,
        "user-agent": "AgentBridge-Webhook/1",
    }
    headers.update({name.lower(): value for name, value in settings.headers.items()})
    request = Request(settings.url, data=body, method=settings.method, headers=headers)
    try:
        with opener.open(request, timeout=settings.timeout_seconds) as response:
            return response.status  # Do not download/log arbitrary response bodies.
    except HTTPError as error:
        error.close()
        return error.code


def message_payload(message: UnifiedMessage, labels: tuple[str, ...]) -> dict:
    """Six-field receiver contract; local labels and metadata never go on the wire."""
    identity = [message.channel, message.channel_account_id, message.conversation_id, message.message_id]
    event_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    if not message.message_id or message.message_id.endswith(":None"):
        event_id = uuid4().hex  # Missing row IDs must not collapse unrelated messages.
    kind = message.content_type.value
    content = message.content if message.content_type == ContentType.TEXT else MEDIA_MARKERS.get(kind, f"[{kind}]")
    return {
        "source": "wechat",
        "event_id": event_id,
        "content": content,
        "sender": message.sender_id,
        "conversation_id": message.conversation_id,
        "occurred_at": message.created_at.isoformat(),
    }


class _EndpointWorker:
    def __init__(self, index, settings, post, retry_delay, upload):
        self.index, self.settings, self.post = index, settings, post
        self.upload = upload
        self.retry_delay = retry_delay
        self.queue = Queue(maxsize=QUEUE_LIMIT)
        self.stopped = threading.Event()
        self.last_overflow_log = float("-inf")
        self.thread = threading.Thread(target=self._run, name=f"webhook-{index}", daemon=True)

    def enqueue(self, item) -> None:
        try:
            self.queue.put_nowait(item)
        except Full:
            now = time.monotonic()
            if now - self.last_overflow_log >= 30:
                logger.warning("Webhook 队列已满，丢弃新消息: webhook=#%s capacity=%s", self.index, QUEUE_LIMIT)
                self.last_overflow_log = now

    def stop(self) -> None:
        self.stopped.set()
        while True:
            try:
                self.queue.get_nowait()
            except Empty:
                break
        self.queue.put_nowait(None)  # Wake a worker blocked on an empty queue.

    def _run(self) -> None:
        while not self.stopped.is_set():
            item = self.queue.get()
            if item is None or self.stopped.is_set():
                return
            event_id, body, message = item
            payload = json.loads(body)
            if self.settings.payload_format == "memory":
                payload.update(account_id=message.channel_account_id,
                               conversation_type=message.conversation_type.value, content_type="text")
            if self.settings.upload.url and message.content_type.value in MEDIA_MARKERS:
                # Upload once, before webhook retries; never create another remote file
                # just because the notification receiver returned a transient error.
                try:
                    logger.info("Webhook 媒体上传开始: webhook=#%s event=%s", self.index, event_id)
                    file_id = self.upload(self.settings.upload, message, event_id, self.stopped)
                    payload["content"] = file_id
                    if self.settings.payload_format == "memory":
                        payload["content_type"] = {"image": "image", "voice": "audio", "video": "video"}[
                            message.content_type.value]
                except Exception as error:  # noqa: BLE001 - endpoint failures must remain isolated
                    reason = str(error) if isinstance(error, UploadError) else type(error).__name__
                    logger.warning("Webhook 媒体上传失败，未推送: webhook=#%s event=%s reason=%s",
                                   self.index, event_id, reason)
                    continue
                logger.info("Webhook 媒体上传成功: webhook=#%s event=%s", self.index, event_id)
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(body) > MAX_PAYLOAD_BYTES:
                logger.warning("Webhook 消息过大，未推送: event=%s", event_id)
                continue
            for attempt in range(1, self.settings.max_attempts + 1):
                if self.stopped.is_set():
                    return
                try:
                    status = self.post(self.settings, body, event_id)
                    success = 200 <= status < 300
                    retry = status in {408, 425, 429} or 500 <= status < 600
                    outcome = f"HTTP {status}"
                except Exception as error:  # noqa: BLE001 - isolate network failures, never log URL/body
                    success, retry, outcome = False, True, type(error).__name__
                if success:
                    logger.info("Webhook 推送成功: webhook=#%s event=%s attempt=%s", self.index, event_id, attempt)
                    break
                logger.warning("Webhook 推送失败: webhook=#%s event=%s attempt=%s/%s reason=%s",
                               self.index, event_id, attempt, self.settings.max_attempts, outcome)
                if not retry or attempt == self.settings.max_attempts:
                    break
                if self.stopped.wait(self.retry_delay * min(2 ** (attempt - 1), 30)):
                    return


class WebhookDispatcher:
    def __init__(self, settings: tuple[WebhookSettings, ...], *, post=post_json, retry_delay=1.0,
                 upload=upload_media):
        self.settings, self._post, self._retry_delay = settings, post, retry_delay
        self._upload = upload
        self._lock = threading.Lock()
        self._workers: list[_EndpointWorker] = []
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._started_at = datetime.max.replace(tzinfo=timezone.utc)

    def start(self) -> None:
        with self._lock:
            if self._workers:
                return
            self._started_at = datetime.now(timezone.utc).replace(microsecond=0)
            self._seen.clear()
            self._workers = [_EndpointWorker(i, s, self._post, self._retry_delay, self._upload)
                             for i, s in enumerate(self.settings, 1) if s.enabled]
            for worker in self._workers:
                worker.thread.start()

    def stop(self) -> None:
        with self._lock:
            for worker in self._workers:
                worker.stop()
            self._workers.clear()
            self._seen.clear()

    def submit(self, message: UnifiedMessage, labels: tuple[str, ...]) -> None:
        with self._lock:
            if not self._workers or message.created_at < self._started_at:
                return
            payload = message_payload(message, labels)
            event_id = payload["event_id"]
            if event_id in self._seen:
                return
            # Also remember filtered AI echoes, so a repeat with weaker metadata cannot leak through.
            self._seen[event_id] = None
            if len(self._seen) > 4096:
                self._seen.popitem(last=False)
            targets = [w for w in self._workers if w.settings.matches(message)]
            if not targets:
                return
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(body) > MAX_PAYLOAD_BYTES:
                logger.warning("Webhook 消息过大，未推送: event=%s", event_id)
                return
            for worker in targets:
                worker.enqueue((event_id, body, message))
