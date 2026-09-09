from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from agent_bridge.companion.avatars import AvatarCache, AvatarResponse
from agent_bridge.companion.models import ConversationItem
from agent_bridge.models import ConversationType


def conversation(
    conversation_id: str = "wxid_private",
    avatar_url: str | None = "https://wx.qlogo.cn/avatar.jpg",
    *,
    account_id: str = "bot",
) -> ConversationItem:
    return ConversationItem(
        "wechat",
        account_id,
        conversation_id,
        ConversationType.PRIVATE,
        "好友",
        avatar_url,
    )


def png_bytes(
    color: str = "#6D5CE7", *, width: int = 8, height: int = 8
) -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


def test_cache_key_is_scoped_and_does_not_expose_conversation_id(
    tmp_path: Path,
) -> None:
    cache = AvatarCache(tmp_path)

    first = cache.cache_key(conversation())
    same = cache.cache_key(conversation())
    other_account = cache.cache_key(conversation(account_id="other"))

    assert first == same
    assert first != other_account
    assert "wxid_private" not in first
    assert len(first) == 64


@pytest.mark.asyncio
async def test_avatar_is_cached_and_fresh_url_skips_second_download(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def fetch(url: str) -> AvatarResponse:
        calls.append(url)
        return AvatarResponse(png_bytes(), "image/png")

    cache = AvatarCache(tmp_path, fetcher=fetch, clock=lambda: 100.0)
    item = conversation()

    first = await cache.ensure(item)
    second = await cache.ensure(item)

    assert first is not None and first.is_file()
    assert second == first
    assert calls == [item.avatar_url]
    metadata = json.loads(cache.metadata_path(item).read_text(encoding="utf-8"))
    assert metadata["checked_at"] == 100.0
    assert "avatar.jpg" not in cache.metadata_path(item).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_changed_url_refreshes_immediately_and_same_url_expires_after_day(
    tmp_path: Path,
) -> None:
    now = [100.0]
    calls: list[str] = []

    def fetch(url: str) -> AvatarResponse:
        calls.append(url)
        return AvatarResponse(png_bytes("#17834B"), "image/png")

    cache = AvatarCache(tmp_path, fetcher=fetch, clock=lambda: now[0])
    original = conversation(avatar_url="https://wx.qlogo.cn/one")
    changed = conversation(avatar_url="https://wx.qlogo.cn/two")

    await cache.ensure(original)
    await cache.ensure(changed)
    now[0] += 24 * 60 * 60 - 1
    await cache.ensure(changed)
    now[0] += 2
    await cache.ensure(changed)

    assert calls == [original.avatar_url, changed.avatar_url, changed.avatar_url]


@pytest.mark.asyncio
async def test_failed_refresh_preserves_last_good_avatar(tmp_path: Path) -> None:
    responses: list[AvatarResponse] = [
        AvatarResponse(png_bytes(), "image/png"),
        AvatarResponse(b"not an image", "image/png"),
    ]

    cache = AvatarCache(tmp_path, fetcher=lambda _url: responses.pop(0))
    original = conversation(avatar_url="https://wx.qlogo.cn/one")
    changed = conversation(avatar_url="https://wx.qlogo.cn/two")

    path = await cache.ensure(original)
    assert path is not None
    previous = path.read_bytes()

    refreshed = await cache.ensure(changed)

    assert refreshed == path
    assert path.read_bytes() == previous


@pytest.mark.asyncio
async def test_invalid_scheme_and_oversized_response_use_fallback(
    tmp_path: Path,
) -> None:
    called = False

    def fetch(_url: str) -> AvatarResponse:
        nonlocal called
        called = True
        return AvatarResponse(b"x" * (1024 * 1024 + 1), "image/png")

    cache = AvatarCache(tmp_path, fetcher=fetch)

    assert await cache.ensure(conversation(avatar_url="file:///avatar.png")) is None
    assert called is False
    assert await cache.ensure(conversation()) is None
    assert called is True


@pytest.mark.asyncio
async def test_malformed_url_and_excessive_dimensions_use_fallback(
    tmp_path: Path,
) -> None:
    calls = 0

    def fetch(_url: str) -> AvatarResponse:
        nonlocal calls
        calls += 1
        return AvatarResponse(png_bytes(width=4097, height=1), "image/png")

    cache = AvatarCache(tmp_path, fetcher=fetch)

    assert await cache.ensure(conversation(avatar_url="http://[invalid")) is None
    assert calls == 0
    assert await cache.ensure(conversation()) is None
    assert calls == 1


@pytest.mark.asyncio
async def test_duplicate_requests_share_one_inflight_download(tmp_path: Path) -> None:
    calls = 0

    def fetch(_url: str) -> AvatarResponse:
        nonlocal calls
        calls += 1
        return AvatarResponse(png_bytes(), "image/png")

    cache = AvatarCache(tmp_path, fetcher=fetch)
    item = conversation()

    first, second = await asyncio.gather(cache.ensure(item), cache.ensure(item))

    assert first == second
    assert calls == 1


@pytest.mark.asyncio
async def test_changed_url_waits_for_old_inflight_then_downloads_new_avatar(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def fetch(url: str) -> AvatarResponse:
        calls.append(url)
        if url.endswith("/old"):
            started.set()
            assert release.wait(timeout=1)
        return AvatarResponse(png_bytes(), "image/png")

    cache = AvatarCache(tmp_path, fetcher=fetch)
    old = conversation(avatar_url="https://wx.qlogo.cn/old")
    new = conversation(avatar_url="https://wx.qlogo.cn/new")

    old_task = asyncio.create_task(cache.ensure(old))
    assert await asyncio.to_thread(started.wait, 1)
    new_task = asyncio.create_task(cache.ensure(new))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(old_task, new_task)
    await cache.ensure(new)

    assert calls == [old.avatar_url, new.avatar_url]
