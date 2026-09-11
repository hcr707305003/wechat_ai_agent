"""Per-endpoint media uploads. Chunk wire protocol is supplied by an adapter."""
from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

MIB = 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MEDIA_MARKERS = {"image": "[图片]", "voice": "[语音]", "video": "[视频]"}
_SEGMENT = re.compile(r"([^\[\]\s>]+)((?:\[(?:0|[1-9][0-9]*)\])*)\Z")


class UploadError(ValueError):
    """Safe, non-sensitive reason that can appear in logs."""


class ChunkProtocolUnavailable(UploadError):
    def __init__(self):
        super().__init__("分片上传协议尚未接入；未上传，也未推送该媒体消息")


def path_tokens(path: str) -> list[str | int]:
    if not isinstance(path, str) or not path or len(path) > 256:
        raise ValueError("file_id_path 必须是 1–256 字符的字段路径")
    tokens = []
    for segment in path.split("->"):
        match = _SEGMENT.fullmatch(segment)
        if not match:
            raise ValueError("file_id_path 格式：field / data->field / data[0]->field")
        tokens.append(match[1])
        tokens.extend(int(index) for index in re.findall(r"\[([0-9]+)\]", match[2]))
    if len(tokens) > 32:
        raise ValueError("file_id_path 层级不能超过 32")
    return tokens


def extract_file_id(response: object, path: str) -> str:
    value = response
    for token in path_tokens(path):
        if isinstance(token, int):
            if not isinstance(value, list) or token >= len(value):
                raise UploadError("file_id_path 数组不存在或下标越界")
            value = value[token]
        else:
            if not isinstance(value, dict) or token not in value:
                raise UploadError("file_id_path 字段不存在")
            value = value[token]
    if type(value) is int:
        value = str(value)
    if (not isinstance(value, str) or not value.strip() or len(value) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise UploadError("文件 ID 必须是非空字符串或整数，不能是对象、数组或布尔值")
    return value


@dataclass(frozen=True, slots=True)
class UploadSettings:
    protocol: str = "multipart"
    url: str = ""
    chunk_url: str = ""
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    file_field: str = "file"
    file_id_path: str = "data->file_id"
    chunk_threshold_mb: int = 10
    chunk_size_mb: int = 5
    max_file_mb: int = 100
    timeout_seconds: float = 30.0

    def __post_init__(self):
        # Delayed import keeps settings and delivery code independently usable.
        from agent_bridge.webhooks import HTTP_METHODS, validated_headers, validated_url

        validated_url(self.url)
        validated_url(self.chunk_url)
        if self.protocol not in ("multipart", "evolutionary"):
            raise ValueError("上传 protocol 必须是 multipart/evolutionary")
        if self.protocol == "evolutionary" and self.method != "POST":
            raise ValueError("Evolutionary 上传协议的 method 必须为 POST")
        if self.protocol == "evolutionary" and any(urlsplit(url).query for url in (self.url, self.chunk_url)):
            raise ValueError("Evolutionary 上传地址请勿附加查询参数；会话参数由程序生成，鉴权请使用请求头")
        if self.method not in HTTP_METHODS:
            raise ValueError("上传 method 必须是 POST/PUT/PATCH")
        object.__setattr__(self, "headers", validated_headers(self.headers, multipart=True))
        if not isinstance(self.file_field, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", self.file_field):
            raise ValueError("上传 file_field 必须是 1–80 个字母、数字、下划线、点或连字符")
        path_tokens(self.file_id_path)
        for key, maximum in (("chunk_threshold_mb", 100), ("chunk_size_mb", 32), ("max_file_mb", 1024)):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"上传 {key} 必须是 1–{maximum} 的整数（MiB）")
        if self.chunk_threshold_mb > self.max_file_mb:
            raise ValueError("分片阈值不能大于最大文件大小")
        if (type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or not 1 <= self.timeout_seconds <= 120):
            raise ValueError("上传 timeout_seconds 必须在 1–120 秒之间")


def parse_upload(value: object) -> UploadSettings:
    if isinstance(value, UploadSettings):
        return value
    if not isinstance(value, dict) or set(value) - set(UploadSettings.__dataclass_fields__):
        raise ValueError("upload 必须是对象且不能包含未知配置字段")
    return UploadSettings(**value)


def multipart_upload(settings: UploadSettings, path: Path, event_id: str, stopped: threading.Event) -> object:
    """Stream a small file without buffering it; never leak local filenames or follow redirects."""
    from agent_bridge.webhooks import _NoRedirect

    boundary = "agentbridge-" + uuid4().hex
    suffix = path.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".bin"
    mime = mimetypes.guess_type("media" + suffix)[0] or "application/octet-stream"
    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="{settings.file_field}"; '
              f'filename="media{suffix}"\r\nContent-Type: {mime}\r\n\r\n').encode("ascii")
    ending = f"\r\n--{boundary}--\r\n".encode("ascii")
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= settings.chunk_threshold_mb * MIB:
            raise UploadError("普通上传文件为空、不是普通文件或超出分片阈值")

        def body():
            if stopped.is_set():
                raise UploadError("上传已取消")
            yield prefix
            remaining = info.st_size
            while remaining:
                if stopped.is_set():
                    raise UploadError("上传已取消")
                block = stream.read(min(64 * 1024, remaining))
                if not block:
                    raise UploadError("上传期间文件发生变化")
                remaining -= len(block)
                yield block
            if stream.read(1) or os.fstat(stream.fileno()).st_mtime_ns != info.st_mtime_ns:
                raise UploadError("上传期间文件发生变化")
            yield ending

        headers = {name.lower(): value for name, value in settings.headers.items()}
        headers.update({"content-type": f"multipart/form-data; boundary={boundary}",
                        "content-length": str(len(prefix) + info.st_size + len(ending)),
                        "x-agent-bridge-event-id": event_id})
        request = Request(settings.url, data=body(), method=settings.method, headers=headers)
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=settings.timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    raise UploadError(f"上传 HTTP {response.status}")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            error.close()
            raise UploadError(f"上传 HTTP {error.code}") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise UploadError("上传响应超过 64 KiB")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise UploadError("上传响应不是有效 JSON") from None


def upload_media(settings: UploadSettings, message, event_id: str, stopped: threading.Event,
                 *, small=multipart_upload, chunked=None) -> str:
    """Upload a prepared local attachment with the selected endpoint protocol."""
    paths = [Path(a.path) for a in message.attachments if a.path and a.kind == message.content_type.value]
    if len(paths) != 1:
        raise UploadError("没有唯一可上传的本地媒体文件")
    path = paths[0]
    if not path.is_file():
        raise UploadError("本地媒体文件不存在")
    size = path.stat().st_size
    if not 0 < size <= settings.max_file_mb * MIB:
        raise UploadError("媒体文件为空或超过最大文件大小")
    if message.content_type.value == "voice" and path.suffix.lower() not in {".mp3", ".wav"}:
        raise UploadError("语音尚未转换为 MP3/WAV，未上传原始 SILK")
    if message.content_type.value == "video" and path.suffix.lower() != ".mp4":
        raise UploadError("视频尚未转换为 MP4")
    if stopped.is_set():
        raise UploadError("上传已取消")
    if settings.protocol == "evolutionary":
        from agent_bridge.evolutionary_uploads import upload_file

        response = upload_file(settings, path, message, event_id, stopped)
    elif size > settings.chunk_threshold_mb * MIB:
        if chunked is None:
            raise ChunkProtocolUnavailable()
        response = chunked(settings, path, event_id, stopped)
    else:
        response = small(settings, path, event_id, stopped)
    return extract_file_id(response, settings.file_id_path)
