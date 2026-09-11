"""Evolutionary AI Files API: scoped raw uploads and zero-based multipart sessions."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

from agent_bridge.webhook_uploads import MAX_RESPONSE_BYTES, MIB, UploadError

logger = logging.getLogger(__name__)


def memory_scope(message):
    scope = {"source": "wechat", "account_id": message.channel_account_id,
             "conversation_type": message.conversation_type.value, "conversation_id": message.conversation_id}
    if not all(isinstance(value, str) and value for value in scope.values()):
        raise UploadError("缺少上传所需的账号或会话范围")
    return scope


def _url(base, query):
    parsed = urlsplit(base)
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _check(stopped):
    if stopped.is_set():
        raise UploadError("上传已取消")


def _request(settings, method, url, data, content_type, size, event_id, stopped):
    from agent_bridge.webhooks import _NoRedirect

    _check(stopped)
    headers = {key.lower(): value for key, value in settings.headers.items()}
    headers.update({"content-type": content_type, "content-length": str(size),
                    "x-agent-bridge-event-id": event_id})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(
            Request(url, data=data, method=method, headers=headers), timeout=settings.timeout_seconds
        ) as response:
            if not 200 <= response.status < 300:
                raise UploadError(f"上传 HTTP {response.status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        error.close()
        raise UploadError(f"上传 HTTP {error.code}") from None
    _check(stopped)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise UploadError("上传响应超过 64 KiB")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise UploadError("上传响应不是有效 JSON") from None
    if not isinstance(result, dict):
        raise UploadError("上传响应不是 JSON 对象")
    return result


def _unchanged(stream, initial):
    current = os.fstat(stream.fileno())
    if (current.st_size, current.st_mtime_ns) != (initial.st_size, initial.st_mtime_ns):
        raise UploadError("上传期间文件发生变化")


def _blocks(stream, size, stopped):
    remaining = size
    while remaining:
        _check(stopped)
        block = stream.read(min(64 * 1024, remaining))
        if not block:
            raise UploadError("上传期间文件发生变化")
        remaining -= len(block)
        yield block


def _verify_file(result, expected_sha, size):
    if (result.get("status") != "uploaded" or result.get("sha256") != expected_sha
            or type(result.get("size")) is not int or result["size"] != size):
        raise UploadError("上传结果状态、大小或 SHA-256 校验失败")


def upload_file(settings, path, message, event_id, stopped):
    """Bound memory to 64 KiB; no UI calls, redirects, automatic re-uploads or remote URL discovery."""
    scope = memory_scope(message)
    kind = {"image": "image", "voice": "audio", "video": "video"}[message.content_type.value]
    suffix = path.suffix.lower()
    filename = "media" + (suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ".bin")
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= settings.max_file_mb * MIB:
            raise UploadError("文件为空、不是普通文件或超过大小上限")
        digest = hashlib.sha256()
        for block in _blocks(stream, info.st_size, stopped):
            digest.update(block)
        checksum = digest.hexdigest()
        _unchanged(stream, info)
        stream.seek(0)
        spec = {"kind": kind, "filename": filename, "size": info.st_size, "sha256": checksum}
        if info.st_size <= settings.chunk_threshold_mb * MIB:
            logger.info("Webhook 原始文件上传: event=%s bytes=%s", event_id, info.st_size)
            result = _request(settings, "POST", _url(settings.url, {**scope, **spec}),
                              _blocks(stream, info.st_size, stopped), "application/octet-stream",
                              info.st_size, event_id, stopped)
        else:
            if not settings.chunk_url:
                raise UploadError("未配置分片初始化地址 chunk_url")
            part_size = settings.chunk_size_mb * MIB
            payload = json.dumps({**spec, "memory_scope": scope, "part_size": part_size}).encode("utf-8")
            initialized = _request(settings, "POST", settings.chunk_url, payload, "application/json",
                                   len(payload), event_id, stopped)
            upload_id = initialized.get("id")
            count = (info.st_size + part_size - 1) // part_size
            if (not isinstance(upload_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", upload_id)
                    or initialized.get("status") != "uploading"
                    or type(initialized.get("part_size")) is not int or initialized["part_size"] != part_size
                    or type(initialized.get("part_count")) is not int or initialized["part_count"] != count):
                raise UploadError("分片初始化返回的 ID、状态或分片参数无效")
            base = settings.chunk_url.rstrip("/") + "/" + upload_id
            logger.info("Webhook 分片上传开始: event=%s bytes=%s parts=%s", event_id, info.st_size, count)
            for index in range(count):
                _unchanged(stream, info)
                length = min(part_size, info.st_size - index * part_size)
                part_digest = hashlib.sha256()
                def part_body(length=length, digest=part_digest):
                    for block in _blocks(stream, length, stopped):
                        digest.update(block)
                        yield block
                part = _request(settings, "PUT", _url(base + f"/parts/{index}", scope), part_body(),
                                "application/octet-stream", length, event_id, stopped)
                if (part.get("upload_id") != upload_id or type(part.get("index")) is not int
                        or part["index"] != index or part.get("size") != length
                        or part.get("sha256") != part_digest.hexdigest()):
                    raise UploadError("分片响应的 ID、序号、大小或 SHA-256 校验失败")
                logger.info("Webhook 分片上传完成: event=%s part=%s/%s", event_id, index + 1, count)
            _unchanged(stream, info)
            result = _request(settings, "POST", _url(base + "/complete", scope), b"", "application/json",
                              0, event_id, stopped)
        _unchanged(stream, info)
        _verify_file(result, checksum, info.st_size)
        return result
