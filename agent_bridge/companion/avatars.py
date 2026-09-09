from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImageReader

from agent_bridge.companion.models import ConversationItem

logger = logging.getLogger(__name__)

_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_IMAGE_DIMENSION = 4096
_FRESH_SECONDS = 24 * 60 * 60


@dataclass(slots=True, frozen=True)
class AvatarResponse:
    data: bytes
    content_type: str


AvatarFetcher = Callable[[str], AvatarResponse]


class AvatarCache:
    def __init__(
        self,
        root: str | Path,
        *,
        fetcher: AvatarFetcher | None = None,
        clock: Callable[[], float] = time.time,
        fresh_seconds: float = _FRESH_SECONDS,
        concurrency: int = 4,
    ) -> None:
        if fresh_seconds <= 0 or concurrency < 1:
            raise ValueError("Avatar freshness and concurrency must be positive")
        self.root = Path(root)
        self._fetcher = fetcher or _download_avatar
        self._clock = clock
        self._fresh_seconds = fresh_seconds
        self._semaphore = asyncio.Semaphore(concurrency)
        self._inflight: dict[
            str, tuple[str, asyncio.Task[Path | None]]
        ] = {}

    def cache_key(self, item: ConversationItem) -> str:
        identity = "\0".join(item.binding_key).encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    def image_path(self, item: ConversationItem) -> Path:
        return self.root / f"{self.cache_key(item)}.image"

    def metadata_path(self, item: ConversationItem) -> Path:
        return self.root / f"{self.cache_key(item)}.json"

    def cached_path(self, item: ConversationItem) -> Path | None:
        path = self.image_path(item)
        return path if path.is_file() else None

    async def ensure(self, item: ConversationItem) -> Path | None:
        cached = self.cached_path(item)
        url = item.avatar_url
        if not url or not _is_http_url(url):
            return cached
        if cached is not None and self._is_fresh(item, url):
            return cached

        key = self.cache_key(item)
        desired_source = _source_hash(url)
        active = self._inflight.get(key)
        if active is None:
            task = asyncio.create_task(self._refresh(item, url))
            self._inflight[key] = (desired_source, task)
            task.add_done_callback(
                lambda completed, cache_key=key: self._forget(cache_key, completed)
            )
            return await asyncio.shield(task)

        active_source, task = active
        result = await asyncio.shield(task)
        if active_source == desired_source:
            return result
        if self._inflight.get(key) == active:
            self._inflight.pop(key, None)
        return await self.ensure(item)

    def _forget(self, key: str, task: asyncio.Task[Path | None]) -> None:
        active = self._inflight.get(key)
        if active is not None and active[1] is task:
            self._inflight.pop(key, None)

    def _is_fresh(self, item: ConversationItem, url: str) -> bool:
        try:
            metadata = json.loads(
                self.metadata_path(item).read_text(encoding="utf-8")
            )
            checked_at = float(metadata["checked_at"])
            source_hash = str(metadata["source_hash"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
        return (
            source_hash == _source_hash(url)
            and self._clock() - checked_at < self._fresh_seconds
        )

    async def _refresh(self, item: ConversationItem, url: str) -> Path | None:
        async with self._semaphore:
            try:
                return await asyncio.to_thread(self._refresh_blocking, item, url)
            except Exception as error:
                logger.warning(
                    "头像刷新失败: channel=%s cache=%s error=%s",
                    item.channel,
                    self.cache_key(item)[:12],
                    type(error).__name__,
                )
                return self.cached_path(item)

    def _refresh_blocking(self, item: ConversationItem, url: str) -> Path:
        response = self._fetcher(url)
        if not response.content_type.lower().split(";", 1)[0].startswith("image/"):
            raise ValueError("Avatar response is not an image")
        if len(response.data) > _MAX_RESPONSE_BYTES:
            raise ValueError("Avatar response exceeds byte limit")
        buffer = QBuffer()
        buffer.setData(response.data)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        dimensions = reader.size()
        if dimensions.isValid() and (
            dimensions.width() > _MAX_IMAGE_DIMENSION
            or dimensions.height() > _MAX_IMAGE_DIMENSION
        ):
            raise ValueError("Avatar dimensions exceed limit")
        image = reader.read()
        if image.isNull():
            raise ValueError("Avatar response cannot be decoded")
        if (
            image.width() > _MAX_IMAGE_DIMENSION
            or image.height() > _MAX_IMAGE_DIMENSION
        ):
            raise ValueError("Avatar dimensions exceed limit")

        self.root.mkdir(parents=True, exist_ok=True)
        image_path = self.image_path(item)
        metadata_path = self.metadata_path(item)
        image_temp = _temporary_path(self.root, ".image.tmp")
        metadata_temp = _temporary_path(self.root, ".json.tmp")
        try:
            image_temp.write_bytes(response.data)
            metadata_temp.write_text(
                json.dumps(
                    {
                        "source_hash": _source_hash(url),
                        "checked_at": self._clock(),
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.replace(image_temp, image_path)
            os.replace(metadata_temp, metadata_path)
        finally:
            image_temp.unlink(missing_ok=True)
            metadata_temp.unlink(missing_ok=True)
        return image_path


def _source_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _is_http_url(url: str) -> bool:
    try:
        return urlsplit(url).scheme.lower() in {"http", "https"}
    except ValueError:
        return False


def _temporary_path(root: Path, suffix: str) -> Path:
    descriptor, value = tempfile.mkstemp(dir=root, prefix=".avatar-", suffix=suffix)
    os.close(descriptor)
    return Path(value)


def _download_avatar(url: str) -> AvatarResponse:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "agent-session-bridge/0.1"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        content_type = response.headers.get_content_type()
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) > _MAX_RESPONSE_BYTES:
            raise ValueError("Avatar response exceeds byte limit")
        data = response.read(_MAX_RESPONSE_BYTES + 1)
    return AvatarResponse(data, content_type)
