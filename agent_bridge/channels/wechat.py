from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import types
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_bridge.channels.base import ChannelAdapter, MessageHandler
from agent_bridge.companion.models import ConversationItem
from agent_bridge.models import (
    Attachment,
    ChannelTarget,
    ContentType,
    ConversationType,
    OutboundDelivery,
    OutboundMessage,
    ReplyReference,
    SessionBindingConfig,
    UnifiedMessage,
)
from agent_bridge.quotes import (
    QUOTE_HISTORY_LIMIT,
    QUOTE_METADATA_KEY,
    parse_wechat_quote_payload,
)
from agent_bridge.senders.wechat import (
    SenderSubscriber,
    WeChatSender,
    WeChatSenderSettings,
    build_wechat_sender,
)
from agent_bridge.senders.wechat_hook_driver import WeChatHookQuoteSettings
from agent_bridge.sessions.repository import SQLiteRepository
from agent_bridge.tools.desktop import DesktopToolbox, WindowInfo
from agent_bridge.webhooks import WebhookDispatcher, WebhookSettings

logger = logging.getLogger(__name__)

DesktopWindowSelector = Callable[
    [UnifiedMessage, str, tuple[WindowInfo, ...]], Awaitable[WindowInfo | None]
]


def _safe_message_connection(raw: Any, user: str) -> Any:
    """Drop all non-selected shard connections before returning a match."""
    cache = getattr(raw, "_agent_bridge_msg_conn_cache", None)
    if cache is None:
        cache = {}
        raw._agent_bridge_msg_conn_cache = cache
    cached = cache.get(user)
    if cached is not None:
        cached_rel, _cached_table = cached
        cached_connection = None
        try:
            cached_connection = raw._open(cached_rel)
            found = raw._find_msg_table(user, [cached_connection])
            if found is not None:
                return found
            cached_connection.close()
        except Exception:
            if cached_connection is not None:
                try:
                    cached_connection.close()
                except Exception:
                    pass
        cache.pop(user, None)

    db_files = tuple(raw._message_dbs())
    conns = [raw._open(rel) for rel in db_files]
    try:
        found = raw._find_msg_table(user, conns)
    except Exception:
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        raise
    if not found:
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        return None
    selected, table = found
    selected_rel = db_files[conns.index(selected)]
    cache[user] = (selected_rel, table)
    for conn in conns:
        if conn is selected:
            continue
        try:
            conn.close()
        except Exception:
            pass
    return selected, table


class _SerializedWeChatDb:
    """Serialize access to wechatauto's shared decrypted database snapshots."""

    _CONCURRENT_REWRITE_MARKER = "数据库合并失败(文件被微信并发改写)"
    _CONCURRENT_REWRITE_DELAYS = (0.05, 0.1)
    _CORRUPTION_MARKERS = (
        "database disk image is malformed",
        "file is not a database",
        "not a database",
    )

    def __init__(self, raw: Any, lock: threading.RLock | None = None) -> None:
        self._raw = raw
        self._lock = lock or threading.RLock()
        self._unused_page_warning_logged = False
        self._patch_merge_validation()
        self._patch_message_connection_cleanup()

    def _patch_merge_validation(self) -> None:
        """Accept readable snapshots with only SQLite unused-page warnings."""
        if not callable(getattr(self._raw, "_check_merged", None)):
            return
        if getattr(self._raw, "_agent_bridge_merge_validation", False):
            return
        try:
            self._raw._check_merged = self._check_merged_snapshot
            self._raw._agent_bridge_merge_validation = True
        except (AttributeError, TypeError):
            logger.debug("无法为微信数据库安装快照校验包装", exc_info=True)

    def _check_merged_snapshot(self, path: str) -> bool:
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                rows = connection.execute("PRAGMA quick_check").fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return False
        issues = [
            line.strip()
            for row in rows
            for line in str(row[0]).splitlines()
            if line.strip()
        ]
        if issues == ["ok"]:
            return True
        findings = [
            issue for issue in issues if issue != "*** in database main ***"
        ]
        usable = bool(findings) and all(
            re.fullmatch(r"Page \d+ is never used", issue)
            for issue in findings
        )
        if usable and not self._unused_page_warning_logged:
            self._unused_page_warning_logged = True
            logger.warning(
                "微信数据库快照仅包含未使用页面告警，继续只读消息读取"
            )
        return usable

    def _patch_message_connection_cleanup(self) -> None:
        """Close unused WeChat message-shard connections after each lookup.

        Older wechatauto releases open every message shard in ``_msg_conn``
        but only close the shard containing the requested table.  On Windows
        those leaked handles prevent cache snapshots from being replaced or
        deleted, which makes corruption recovery fail repeatedly.
        """
        if not callable(getattr(self._raw, "_msg_conn", None)):
            return
        if getattr(self._raw, "_agent_bridge_msg_conn_cleanup", False):
            return
        try:
            self._raw._msg_conn = types.MethodType(
                _safe_message_connection, self._raw
            )
            self._raw._agent_bridge_msg_conn_cleanup = True
        except (AttributeError, TypeError):
            # Keep compatibility with alternate/immutable DB implementations.
            logger.debug("无法为微信数据库安装分片连接清理包装", exc_info=True)

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._raw, name)
        if not callable(attribute):
            return attribute

        def serialized(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                corruption_retried = False
                concurrent_attempt = 0
                while True:
                    try:
                        return attribute(*args, **kwargs)
                    except sqlite3.DatabaseError as error:
                        if (
                            corruption_retried
                            or not self._is_snapshot_corruption(error)
                        ):
                            raise
                        corruption_retried = True
                        removed = self._invalidate_derived_snapshots()
                        logger.warning(
                            "微信解密数据库快照损坏，已清理缓存并重试: "
                            "method=%s removed=%d error=%s",
                            name,
                            removed,
                            error,
                        )
                    except RuntimeError as error:
                        if self._CONCURRENT_REWRITE_MARKER not in str(error):
                            raise
                        if concurrent_attempt >= len(
                            self._CONCURRENT_REWRITE_DELAYS
                        ):
                            if name == "get_new_messages":
                                logger.warning(
                                    "微信消息库正在写入，本轮延后读取且保留消息水位: %s",
                                    error,
                                )
                                return []
                            raise
                        delay = self._CONCURRENT_REWRITE_DELAYS[concurrent_attempt]
                        concurrent_attempt += 1
                        logger.debug(
                            "微信数据库被并发改写，短暂等待后重试: "
                            "method=%s attempt=%d delay=%.2fs",
                            name,
                            concurrent_attempt,
                            delay,
                        )
                        time.sleep(delay)

        return serialized

    @classmethod
    def _is_snapshot_corruption(cls, error: sqlite3.DatabaseError) -> bool:
        message = str(error).lower()
        return any(marker in message for marker in cls._CORRUPTION_MARKERS)

    def _invalidate_derived_snapshots(self) -> int:
        workdir = Path(str(self._raw.workdir)).resolve()
        removed = 0
        for rel, _source, _size in tuple(self._raw._db_files):
            cache_name = str(rel).replace(os.sep, "__")
            cache_path = (workdir / cache_name).resolve()
            if cache_path.parent != workdir:
                logger.error("拒绝清理工作目录外的微信数据库缓存: %s", cache_path)
                continue
            for suffix in ("", ".stamp", "-wal", "-shm"):
                candidate = Path(f"{cache_path}{suffix}")
                if not candidate.exists():
                    continue
                try:
                    candidate.unlink()
                except OSError:
                    logger.warning("清理微信数据库缓存失败: %s", candidate)
                else:
                    removed += 1
        return removed


@dataclass(slots=True, frozen=True)
class WeChatCompanionSettings:
    mode: str = "docked"
    side: str = "right"
    width: int = 620
    height: int = 620
    follow_interval: float = 0.016
    theme: str = "system"

    def __post_init__(self) -> None:
        if self.mode not in {"docked", "independent"}:
            raise ValueError("WeChat companion mode must be docked or independent")
        if self.side not in {"left", "right", "top", "bottom"}:
            raise ValueError("WeChat companion side must be left, right, top, or bottom")
        if self.width < 320 or self.height < 240:
            raise ValueError("WeChat companion width/height are too small")
        if self.follow_interval <= 0:
            raise ValueError("WeChat companion follow_interval must be positive")
        if self.theme not in {"system", "light", "dark"}:
            raise ValueError("WeChat companion theme must be system, light, or dark")


@dataclass(slots=True, frozen=True)
class WeChatChannelSettings:
    account: str | None = None
    allowed_private_ids: tuple[str, ...] = ()
    allowed_group_ids: tuple[str, ...] = ()
    group_controllers: frozenset[str] = field(default_factory=frozenset)
    group_prefixes: tuple[str, ...] = ("/ai",)
    # How configured group triggers are located in a message.
    group_prefixes_rule: str = "prefix"
    reply_prefix: str = ""
    quote_private_replies: bool = False
    quote_group_replies: bool = False
    message_batch_window_seconds: float = 1.5
    # Optional per-conversation agent session bindings loaded from YAML.
    session_bindings: tuple[SessionBindingConfig, ...] = ()
    webhooks: tuple[WebhookSettings, ...] = ()
    # Keep message pickup responsive without making the WeChat DB poller busy.
    # This is the upper bound on delivery latency for an otherwise idle bridge.
    listener_interval: float = 0.25
    hook_quote: WeChatHookQuoteSettings = field(
        default_factory=WeChatHookQuoteSettings
    )
    sender: WeChatSenderSettings = field(default_factory=WeChatSenderSettings)
    companion: WeChatCompanionSettings = field(default_factory=WeChatCompanionSettings)

    def __post_init__(self) -> None:
        if not isinstance(self.reply_prefix, str):
            raise TypeError("WeChat reply_prefix must be a string")
        if self.group_prefixes_rule not in {"prefix", "contains", "suffix"}:
            raise ValueError(
                "WeChat group_prefixes_rule must be prefix, contains, or suffix"
            )
        if (
            not math.isfinite(self.message_batch_window_seconds)
            or self.message_batch_window_seconds < 0
        ):
            raise ValueError(
                "WeChat message_batch_window_seconds must be a finite non-negative number"
            )

class WeChatChannelAdapter(ChannelAdapter):
    name = "wechat"
    _MEDIA_KEY_RETRY_SECONDS = 300.0
    _FOREGROUND_FALLBACK_STATE = "foreground_fallback_enabled"
    _SELF_IDENTITIES_STATE = "self_sender_identities"
    _SCREENSHOT_PHRASES = ("截图", "截屏", "屏幕截图")

    def __init__(
        self, settings: WeChatChannelSettings, repository: SQLiteRepository
    ) -> None:
        self.settings = settings
        self.repository = repository
        self._webhooks = WebhookDispatcher(settings.webhooks)
        self._handler: MessageHandler | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._db_lock = threading.RLock()
        self._db: Any = None
        self._listener: Any = None
        self._sender: WeChatSender | None = None
        self._sender_subscribers: list[SenderSubscriber] = []
        repository_path = getattr(repository, "path", None)
        output_dir = (
            Path(repository_path).parent / "screenshots"
            if repository_path is not None
            else Path.cwd() / ".data" / "screenshots"
        )
        self._desktop_tools = DesktopToolbox(output_dir)
        self._window_selector: DesktopWindowSelector | None = None
        self._account_id = ""
        self._nickname = ""
        self._resolved_private_ids = settings.allowed_private_ids
        self._resolved_group_ids = settings.allowed_group_ids
        self._allowlist_log_labels: dict[tuple[ConversationType, str], tuple[str, ...]] = {
            (kind, value): (value,)
            for kind, values in (
                (ConversationType.PRIVATE, settings.allowed_private_ids),
                (ConversationType.GROUP, settings.allowed_group_ids),
            )
            for value in values
        }
        self._resolved_session_bindings = settings.session_bindings
        self._display_name_cache: dict[str, str] = {}
        self._self_sender_ids: set[str] = {"2"}
        self._self_sender_usernames: set[str] = set()
        self._pending_outbound: deque[tuple[str, str, ContentType, float]] = deque()
        self._pending_outbound_lock = threading.Lock()
        # A text-only verification must consume the exact WeChat row it
        # matched.  Without this cursor, two consecutive identical replies
        # can both be marked sent by repeatedly matching the same newest row.
        self._verified_delivery_rows: dict[str, set[tuple[str, ...]]] = {}
        self._message_dispatch_locks: dict[str, asyncio.Lock] = {}
        self._pending_media_counts: dict[str, int] = {}
        self._pending_media_lock = threading.Lock()
        self._media_key_lock = threading.Lock()
        self._media_keys: tuple[str, int | None] | None = None
        self._media_key_retry_after = 0.0

    def is_builtin_message(self, message: UnifiedMessage) -> bool:
        return self._is_screenshot_request(message.content)

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def resolved_session_bindings(self) -> tuple[SessionBindingConfig, ...]:
        return self._resolved_session_bindings

    @property
    def main_window_handle(self) -> int | None:
        return self._sender.main_window_handle if self._sender is not None else None

    def subscribe_sender(self, subscriber: SenderSubscriber) -> None:
        self._sender_subscribers.append(subscriber)
        if self._sender is not None:
            self._sender.subscribe(subscriber)

    def set_desktop_window_selector(
        self, selector: DesktopWindowSelector | None
    ) -> None:
        """Set the optional Agent-backed resolver for ambiguous app names."""
        self._window_selector = selector

    def foreground_fallback_enabled(self) -> bool:
        if not self._account_id:
            return self.settings.sender.foreground_fallback_default
        saved = self.repository.get_channel_state(
            self.name, self._account_id, self._FOREGROUND_FALLBACK_STATE
        )
        if saved is None:
            return self.settings.sender.foreground_fallback_default
        return bool(saved)

    def set_foreground_fallback_enabled(self, enabled: bool) -> None:
        if not self._account_id:
            raise RuntimeError("WeChat account is not available")
        self.repository.set_channel_state(
            self.name,
            self._account_id,
            self._FOREGROUND_FALLBACK_STATE,
            bool(enabled),
        )

    async def start(self, handler: MessageHandler) -> None:
        if self._listener is not None:
            return
        self._handler = handler
        self._loop = asyncio.get_running_loop()
        started = time.perf_counter()
        stage_started = started
        await asyncio.to_thread(self._start_blocking)
        logger.info(
            "微信通道启动计时: 数据库与监听配置 ready elapsed=%.3fs",
            time.perf_counter() - stage_started,
        )
        assert self._sender is not None
        stage_started = time.perf_counter()
        await self._sender.start()
        logger.info(
            "微信通道启动计时: sender ready elapsed=%.3fs total=%.3fs",
            time.perf_counter() - stage_started,
            time.perf_counter() - started,
        )
        assert self._listener is not None
        stage_started = time.perf_counter()
        self._webhooks.start()
        try:
            await asyncio.to_thread(self._listener.start)
        except BaseException:
            self._webhooks.stop()
            raise
        logger.info(
            "微信通道启动计时: listener ready elapsed=%.3fs total=%.3fs",
            time.perf_counter() - stage_started,
            time.perf_counter() - started,
        )
        logger.info(
            "微信白名单监听已启动: account=%s conversations=%d private=%d groups=%d",
            self._account_id,
            len(set((*self._resolved_private_ids, *self._resolved_group_ids))),
            len(self._resolved_private_ids),
            len(self._resolved_group_ids),
        )

    def _start_blocking(self) -> None:
        started = time.perf_counter()
        try:
            from wechatauto import WeChatDB
            from wechatauto.db import Listener
        except ImportError as exc:
            raise RuntimeError(
                "WeChat adapter requires: pip install -e .[wechat]"
            ) from exc
        self._db = _SerializedWeChatDb(
            WeChatDB(account=self.settings.account), self._db_lock
        )
        logger.info(
            "微信通道启动计时: WeChatDB ready elapsed=%.3fs",
            time.perf_counter() - started,
        )
        stage_started = time.perf_counter()
        info = self._db.get_self_info()
        self._account_id = str(info.get("username") or self.settings.account or "wechat")
        self._nickname = str(info.get("remark") or info.get("nick_name") or self._account_id)
        logger.info(
            "微信通道启动计时: self info ready elapsed=%.3fs total=%.3fs",
            time.perf_counter() - stage_started,
            time.perf_counter() - started,
        )
        stage_started = time.perf_counter()
        self._load_self_identities()
        self._display_name_cache[self._account_id] = self._nickname
        logger.info(
            "微信通道启动计时: identities ready elapsed=%.3fs total=%.3fs",
            time.perf_counter() - stage_started,
            time.perf_counter() - started,
        )
        stage_started = time.perf_counter()
        self._resolve_allowlisted_targets()
        logger.info(
            "微信通道启动计时: allowlist resolved elapsed=%.3fs total=%.3fs",
            time.perf_counter() - stage_started,
            time.perf_counter() - started,
        )
        watermark = self.repository.get_channel_state(
            self.name, self._account_id, "watermark"
        ) or {}
        stage_started = time.perf_counter()
        self._sender = build_wechat_sender(
            self.settings.sender,
            self.repository,
            self._silent_target_names,
            self._verify_silent_delivery,
            self._record_pending_outbound,
            self._discard_pending_outbound,
            self.foreground_fallback_enabled,
            self._resolve_quote_reference,
        )
        logger.info(
            "微信通道启动计时: sender constructed elapsed=%.3fs total=%.3fs",
            time.perf_counter() - stage_started,
            time.perf_counter() - started,
        )
        for subscriber in self._sender_subscribers:
            self._sender.subscribe(subscriber)
        self._listener = Listener(
            self._db,
            interval=self.settings.listener_interval,
            watermark={str(key): int(value) for key, value in watermark.items()},
        )
        stage_started = time.perf_counter()
        self._register_allowlisted_listeners()
        logger.info(
            "微信通道启动计时: listeners registered elapsed=%.3fs total=%.3fs",
            time.perf_counter() - stage_started,
            time.perf_counter() - started,
        )

    def _register_allowlisted_listeners(self) -> tuple[str, ...]:
        targets = tuple(
            dict.fromkeys(
                (*self._resolved_private_ids, *self._resolved_group_ids)
            )
        )
        for conversation_id in targets:
            self._listener.add_listener(conversation_id, self._on_raw_message)
        return targets

    async def stop(self) -> None:
        self._webhooks.stop()
        sender = self._sender
        if sender is not None:
            await sender.stop()
            self._sender = None
        listener = self._listener
        if listener is None:
            return
        await asyncio.to_thread(listener.stop)
        self.repository.set_channel_state(
            self.name, self._account_id, "watermark", listener.watermark
        )
        self._listener = None
        self._handler = None

    async def send_message(self, target: ChannelTarget, message: OutboundMessage) -> str:
        if self._sender is None:
            raise RuntimeError("WeChat channel is not running")
        quote_enabled = (
            self.settings.quote_private_replies
            if target.conversation_type == ConversationType.PRIVATE
            else self.settings.quote_group_replies
        )
        if not quote_enabled and message.reply_to is not None:
            message = replace(message, reply_to=None)
        if self.settings.sender.mode == "legacy_gui":
            self._record_pending_outbound(target.conversation_id, message.text)
        return await self._sender.enqueue(target, message)

    def _resolve_quote_reference(self, reference: ReplyReference) -> ReplyReference:
        """Resolve an exact duplicate occurrence immediately before delivery."""
        if self._db is None:
            raise LookupError("WeChat database is unavailable")
        rows = list(
            self._db.get_messages(
                reference.conversation_id,
                limit=QUOTE_HISTORY_LIMIT,
            )
        )
        rows.sort(
            key=lambda row: int(row.get("sort_seq") or 0),
            reverse=True,
        )
        rows = rows[:QUOTE_HISTORY_LIMIT]
        local_id = reference.message_id.rsplit(":", 1)[-1]
        occurrence = 0
        found = False
        for raw in rows:
            row_local_id = str(raw.get("local_id") or "")
            row_sort_seq = raw.get("sort_seq")
            if row_local_id == local_id or (
                reference.sort_seq is not None
                and row_sort_seq is not None
                and int(row_sort_seq) == reference.sort_seq
            ):
                found = True
                break
            event = dict(raw)
            event["username"] = reference.conversation_id
            candidate = self.normalize(event)
            if (
                candidate.sender_id == reference.sender_id
                and candidate.content_type == reference.content_type
                and (
                    reference.content_type != ContentType.TEXT
                    or candidate.content == reference.content
                )
            ):
                occurrence += 1
        if not found:
            raise LookupError(
                "WeChat quote target is absent from recent "
                f"{QUOTE_HISTORY_LIMIT} messages: {reference.message_id}"
            )
        return replace(reference, occurrence_from_latest=occurrence)

    async def handle_builtin(
        self, message: UnifiedMessage, target: ChannelTarget
    ) -> OutboundMessage | None:
        """Handle small channel-native actions before invoking an Agent.

        A request such as “截图我的微信客户端” cannot be fulfilled by a
        Codex/Claude session unless the host exposes a desktop screenshot tool.
        The bridge already owns the WeChat window handle, so capture it locally
        and send it through the normal, verified image delivery path instead of
        producing a misleading Computer Use authorization reply.
        """
        if not self._is_screenshot_request(message.content):
            return False
        try:
            is_desktop = self._is_desktop_screenshot_request(message.content)
            application_queries = self._application_queries(message.content)
            # Treat 微信 as the dedicated client capture only when it is the
            # sole target. In a request like “截图网易云和微信”, it is one
            # member of a multi-window capture and must not short-circuit the
            # other targets.
            is_wechat = (
                len(application_queries) == 1
                and self._is_wechat_target(application_queries[0])
            )
            is_current = self._is_current_application_request(message.content)
            if is_desktop:
                captured = [("桌面", await asyncio.to_thread(self._capture_desktop))]
            elif is_wechat:
                captured = [("微信窗口", await asyncio.to_thread(self._capture_wechat_window))]
            elif is_current:
                captured = [("当前应用", await asyncio.to_thread(self._capture_foreground_application))]
            else:
                captured = []
                for query in application_queries:
                    if self._is_wechat_target(query):
                        path = await asyncio.to_thread(self._capture_wechat_window)
                    else:
                        path = await self._capture_application(message, query=query)
                    captured.append((self._application_label(query), path))
                requested_labels = tuple(label for label, _path in captured)
                multi_capture = len(captured) > 1
                if multi_capture:
                    path = await asyncio.to_thread(
                        self._desktop_tools.combine_images, captured, "apps"
                    )
                    captured = [("多窗口截图", path)]
                elif captured:
                    path = captured[0][1]
                else:
                    raise RuntimeError("请指定要截图的应用名称")
            if is_desktop or is_wechat or is_current:
                requested_labels = ()
                multi_capture = False
            path = captured[0][1]
            attachment = Attachment(
                kind="image",
                name=path.name,
                path=str(path),
                mime_type="image/png",
                metadata={
                    "alt": (
                        "桌面截图"
                        if is_desktop
                        else (
                            "微信客户端截图"
                            if is_wechat
                            else "当前应用截图"
                            if is_current
                            else (
                                "、".join(requested_labels)
                                + "截图"
                                if multi_capture
                                else "应用窗口截图"
                            )
                        )
                    ),
                    "builtin": "wechat_screenshot",
                },
            )
            outbound = OutboundMessage(
                "爸爸，给你当前桌面截图："
                if is_desktop
                else (
                    "爸爸，给你当前微信客户端截图："
                    if is_wechat
                    else (
                        "爸爸，给你当前应用截图："
                        if is_current
                        else (
                            "爸爸，给你多窗口截图："
                            if multi_capture
                            else "爸爸，给你当前应用窗口截图："
                        )
                    )
                ),
                attachments=(attachment,),
                reply_to=ReplyReference.from_message(message),
            )
            await self.send_message(target, outbound)
            return outbound
        except Exception as error:  # noqa: BLE001 - report action failure in chat
            logger.warning("桌面截图请求失败", exc_info=True)
            outbound = OutboundMessage(
                f"截图失败：{type(error).__name__}",
                reply_to=ReplyReference.from_message(message),
            )
            await self.send_message(
                target,
                outbound,
            )
            return outbound

    @classmethod
    def _is_screenshot_request(cls, content: str) -> bool:
        normalized = re.sub(r"\s+", "", str(content or "")).lower()
        # Once a screenshot verb is present, treat the remaining text as a
        # target description (微信、桌面或任意应用名).  Unknown/empty
        # descriptions are rejected later with a clear usage message.
        if any(phrase in normalized for phrase in ("不要截图", "别截图", "无需截图")):
            return False
        return any(phrase in normalized for phrase in cls._SCREENSHOT_PHRASES) and len(
            normalized
        ) > 2

    @staticmethod
    def _is_wechat_screenshot_request(content: str) -> bool:
        normalized = re.sub(r"\s+", "", str(content or "")).lower()
        return "微信" in normalized or "客户端" in normalized

    @staticmethod
    def _is_desktop_screenshot_request(content: str) -> bool:
        normalized = re.sub(r"\s+", "", str(content or "")).lower()
        return (
            any(
                term in normalized
                for term in ("桌面", "屏幕", "当前画面", "所有", "全部", "全屏")
            )
            and "微信" not in normalized
            and "客户端" not in normalized
        )

    @staticmethod
    def _is_current_application_request(content: str) -> bool:
        normalized = re.sub(r"\s+", "", str(content or "")).lower()
        return "当前应用" in normalized or "当前窗口" in normalized

    def _capture_wechat_window(self) -> Path:
        hwnd = int(self.main_window_handle or 0)
        if not hwnd:
            raise RuntimeError("微信窗口句柄不可用")
        return self._desktop_tools.capture_window(hwnd, "wechat")

    async def _capture_application(
        self, message: UnifiedMessage, *, query: str | None = None
    ) -> Path:
        query = query or self._application_query(message.content)
        if not query:
            raise RuntimeError("请指定应用名称，例如：截图 Chrome 窗口")
        windows = tuple(await asyncio.to_thread(self._desktop_tools.list_windows))
        matches = self._desktop_tools.match_windows(query, list(windows))
        window = matches[0] if len(matches) == 1 else None
        if self._window_selector is not None and len(matches) != 1:
            try:
                selected = await asyncio.wait_for(
                    self._window_selector(message, query, windows), timeout=20.0
                )
            except (asyncio.TimeoutError, OSError, RuntimeError, ValueError) as error:
                logger.warning("Agent 窗口识别失败，回退本地匹配: %s", error)
                selected = None
            if selected is not None and any(
                selected.hwnd == candidate.hwnd for candidate in windows
            ):
                window = selected
        if window is None and matches:
            window = matches[0]
        if window is None:
            raise RuntimeError(f"未找到可截图的应用窗口：{query}")
        return await asyncio.to_thread(
            self._desktop_tools.capture_window, window.hwnd, "app"
        )

    @classmethod
    def _application_queries(cls, content: str) -> tuple[str, ...]:
        value = cls._application_query(content)
        if not value:
            return ()
        parts = re.split(r"(?:和|及|以及|与|、|,|，|\+|&)", value)
        return tuple(part for part in (item.strip() for item in parts) if part)

    @staticmethod
    def _is_wechat_target(query: str) -> bool:
        return re.sub(r"\s+", "", str(query or "")).lower() in {
            "微信",
            "微信客户端",
            "wechat",
        }

    @staticmethod
    def _application_label(query: str) -> str:
        if WeChatChannelAdapter._is_wechat_target(query):
            return "微信窗口"
        return f"{query}窗口"

    def _capture_foreground_application(self) -> Path:
        return self._desktop_tools.capture_foreground("foreground")

    @staticmethod
    def _application_query(content: str) -> str:
        value = str(content or "")
        # Remove the natural-language shell around the application name while
        # preserving both Chinese and English window-title fragments.
        value = re.sub(r"截图|截屏|屏幕截图|请|给我|一下|当前|我的|那个", "", value)
        value = re.sub(r"桌面|屏幕|应用|窗口|画面|所有|全部|全屏", "", value)
        return re.sub(r"\s+", "", value).strip("：:，,。.!！")

    def _capture_desktop(self) -> Path:
        return self._desktop_tools.capture_desktop("desktop")

    async def retry_delivery(self, delivery_id: str) -> str:
        if self._sender is None:
            raise RuntimeError("WeChat channel is not running")
        return await self._sender.retry(delivery_id)

    async def resend_delivery(self, delivery_id: str) -> str:
        if self._sender is None:
            raise RuntimeError("WeChat channel is not running")
        return await self._sender.resend(delivery_id)

    async def cancel_delivery(self, delivery_id: str) -> None:
        if self._sender is None:
            raise RuntimeError("WeChat channel is not running")
        await self._sender.cancel(delivery_id)

    async def list_allowlisted_conversations(self) -> list[ConversationItem]:
        if self._db is None:
            raise RuntimeError("WeChat channel is not running")
        return await asyncio.to_thread(self._list_allowlisted_conversations_blocking)

    async def list_allowlisted_conversations_initial(self) -> list[ConversationItem]:
        """Build lightweight rows without opening the contact database."""
        if self._db is None:
            raise RuntimeError("WeChat channel is not running")
        return self._list_allowlisted_conversations_initial()

    def _list_allowlisted_conversations_initial(self) -> list[ConversationItem]:
        targets = (
            *((item, ConversationType.PRIVATE) for item in self._resolved_private_ids),
            *((item, ConversationType.GROUP) for item in self._resolved_group_ids),
        )
        return [
            ConversationItem(
                self.name,
                self._account_id,
                conversation_id,
                conversation_type,
                self._display_name_cache.get(conversation_id) or conversation_id,
            )
            for conversation_id, conversation_type in targets
        ]

    async def load_history(
        self, conversation_id: str, limit: int
    ) -> list[UnifiedMessage]:
        if not 1 <= limit <= 500:
            raise ValueError("WeChat history limit must be between 1 and 500")
        if self._db is None:
            raise RuntimeError("WeChat channel is not running")
        return await asyncio.to_thread(
            self._load_history_blocking, conversation_id, limit
        )

    def normalize(self, raw_event: Any) -> UnifiedMessage:
        if not isinstance(raw_event, dict):
            raise TypeError("WeChat listener event must be a dictionary")
        conversation_id = str(raw_event.get("username") or "")
        is_group = conversation_id.endswith("@chatroom")
        group_prefix = self._group_sender_prefix(raw_event.get("content"))
        # WeChat's DB/listener uses sender_id=2 for messages sent by the
        # logged-in account.  In group chats the textual ``sender:`` prefix
        # is useful for identifying other members, but must never override
        # that authoritative self marker.
        raw_is_self = self._is_self_message(raw_event) or (
            is_group and self._is_self_sender_value(group_prefix)
        )
        group_sender = (
            group_prefix
            if self._is_group_sender_token(group_prefix)
            or self._is_self_sender_value(group_prefix)
            else None
        )
        sender_id = str(
            self._account_id
            if raw_is_self and self._account_id
            else group_sender
            or raw_event.get("sender_username")
            or raw_event.get("sender_id")
            or conversation_id
        )
        content = str(raw_event.get("content") or "")
        # Strip the group prefix independently from the normalized sender ID:
        # self messages use the account wxid above, while their content may
        # still begin with another token in WeChat's raw group representation.
        content_sender = group_sender or sender_id
        if is_group and content_sender:
            original_content = content
            content = re.sub(
                rf"^{re.escape(content_sender)}:\s*", "", content, count=1
            )
            if content == original_content:
                display_name = str(raw_event.get("sender_name") or "").strip()
                if display_name and display_name != content_sender:
                    content = re.sub(
                        rf"^{re.escape(display_name)}:\s*", "", content, count=1
                    )
        raw_type = raw_event.get("type")
        identity_event = raw_event
        if group_sender:
            identity_event = {**raw_event, "sender_username": group_sender}
        is_self = raw_is_self or self._is_self_message(identity_event)
        if is_self and self._account_id:
            sender_id = self._account_id
        content_type = {
            "文本": ContentType.TEXT,
            "图片": ContentType.IMAGE,
            # WeChat stores animated stickers as local_type 47.  They are
            # still image media from the companion's point of view.
            "动画表情": ContentType.IMAGE,
            "语音": ContentType.VOICE,
            "视频": ContentType.VIDEO,
            "文件/链接/卡片": ContentType.FILE,
        }.get(raw_type, ContentType.UNKNOWN)
        # Image XML is transport metadata, not chat text.  The media hydrator
        # still receives the untouched raw event for cache lookup/decryption.
        if content_type == ContentType.IMAGE:
            content = "[动画表情]" if raw_type == "动画表情" else "[图片]"
        content, quote_metadata = parse_wechat_quote_payload(content)
        if quote_metadata is not None:
            quote_metadata["conversation_id"] = conversation_id
            # Native quote messages carry their actual reply in appmsg/title;
            # the outer database type is a generic card and must not make the
            # workbench display the XML payload as a file.
            content_type = ContentType.TEXT
        timestamp = raw_event.get("create_time")
        created_at = (
            datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
            if isinstance(timestamp, (int, float))
            else datetime.now(timezone.utc)
        )
        mentions = (self._account_id,) if self._is_mentioned(content) else ()
        return UnifiedMessage(
            channel=self.name,
            channel_account_id=self._account_id,
            conversation_id=conversation_id,
            conversation_type=(
                ConversationType.GROUP if is_group else ConversationType.PRIVATE
            ),
            sender_id=sender_id,
            sender_name=str(
                (self._nickname if is_self else raw_event.get("sender_name"))
                or sender_id
            ),
            message_id=f"{conversation_id}:{raw_event.get('local_id')}",
            content=content,
            content_type=content_type,
            mentions=mentions,
            created_at=created_at,
            metadata={
                "sort_seq": raw_event.get("sort_seq"),
                "raw_type": raw_type,
                "is_self": is_self,
                **(
                    {QUOTE_METADATA_KEY: quote_metadata}
                    if quote_metadata is not None
                    else {}
                ),
            },
        )

    def _is_self_message(self, raw_event: dict[str, Any]) -> bool:
        sender_username = str(raw_event.get("sender_username") or "")
        sender_id = str(raw_event.get("sender_id") or "")
        # sender_id=2 is WeChat's stable self marker.  Check it first so a
        # stale username or a group sender prefix cannot turn our own message
        # into an inbound member message.
        if sender_id in self._self_sender_ids:
            return True
        if sender_username:
            return (
                sender_username == self._account_id
                or sender_username in self._self_sender_usernames
            )
        return False

    def _is_self_sender_value(self, value: Any) -> bool:
        """Match a group prefix against the account's known identities."""
        candidate = str(value or "").strip()
        if not candidate:
            return False
        return candidate in {
            self._account_id,
            self._nickname,
            *self._self_sender_usernames,
        }

    def _load_self_identities(self) -> None:
        saved = self.repository.get_channel_state(
            self.name, self._account_id, self._SELF_IDENTITIES_STATE
        )
        if not isinstance(saved, dict):
            return
        self._self_sender_ids.update(
            str(value) for value in saved.get("sender_ids", ()) if str(value)
        )
        self._self_sender_usernames.update(
            str(value)
            for value in saved.get("sender_usernames", ())
            if str(value)
        )

    def _remember_self_identity(self, raw_event: dict[str, Any]) -> None:
        sender_id = str(raw_event.get("sender_id") or "")
        sender_username = str(raw_event.get("sender_username") or "")
        if sender_id:
            self._self_sender_ids.add(sender_id)
        if sender_username:
            self._self_sender_usernames.add(sender_username)
        setter = getattr(self.repository, "set_channel_state", None)
        if not self._account_id or not callable(setter):
            return
        setter(
            self.name,
            self._account_id,
            self._SELF_IDENTITIES_STATE,
            {
                "sender_ids": sorted(self._self_sender_ids),
                "sender_usernames": sorted(self._self_sender_usernames),
            },
        )

    def _on_raw_message(self, raw_event: dict[str, Any], _listener: Any) -> None:
        if self._handler is None or self._loop is None:
            return
        try:
            message = self.normalize(raw_event)
        except Exception:
            logger.warning("忽略无法解析的微信消息", exc_info=True)
            return
        if not self._is_allowed(message):
            logger.debug(
                "忽略白名单外微信消息: conversation=%s", message.conversation_id
            )
            return
        webhook_message = message
        webhook_is_self = message.metadata.get("is_self") is True
        pending_outbound = self._consume_pending_outbound(
            message.conversation_id, message.content, message.content_type
        )
        bridge_outbound = pending_outbound is not None
        if bridge_outbound:
            self._remember_self_identity(raw_event)
            message = replace(
                message,
                metadata={
                    **message.metadata,
                    "is_self": True,
                    "bridge_outbound": True,
                    "bridge_delivery_text": pending_outbound,
                },
            )
        message = replace(message, sender_name=self._sender_display_name(message))
        webhook_message = replace(webhook_message, sender_name=(
            message.sender_name if not bridge_outbound or webhook_is_self
            else self._sender_display_name(webhook_message)
        ), metadata={
            **webhook_message.metadata, "is_self": webhook_is_self,
            "bridge_outbound": bridge_outbound and webhook_is_self,
        })
        try:
            self._webhooks.submit(webhook_message, self._allowlist_log_labels.get(
                (message.conversation_type, message.conversation_id), ()))
        except Exception as error:  # noqa: BLE001 - webhooks must never break message reception
            logger.warning("Webhook 消息入队失败: error=%s", type(error).__name__)
        message = self._prepare_for_controller(message)
        if message.metadata.get("is_self") and not bridge_outbound:
            message = replace(
                message,
                metadata={
                    **message.metadata,
                    "bridge_outbound": False,
                },
            )
        logger.info(
            "%s 收到微信消息: conversation=%s type=%s sender=%s message_id=%s",
            self._configured_message_log_tag(message),
            message.conversation_id,
            message.conversation_type.value,
            message.sender_id,
            message.message_id,
        )
        media_pending = message.content_type == ContentType.IMAGE
        if media_pending:
            self._change_pending_media(message.conversation_id, 1)

        async def dispatch_message() -> None:
            try:
                await self._dispatch_message_in_order(message, raw_event)
            finally:
                if media_pending:
                    self._change_pending_media(message.conversation_id, -1)

        future = asyncio.run_coroutine_threadsafe(dispatch_message(), self._loop)
        future.add_done_callback(self._log_handler_failure)

    def _change_pending_media(self, conversation_id: str, delta: int) -> None:
        with self._pending_media_lock:
            count = max(0, self._pending_media_counts.get(conversation_id, 0) + delta)
            if count:
                self._pending_media_counts[conversation_id] = count
            else:
                self._pending_media_counts.pop(conversation_id, None)

    async def wait_for_pending_media(self, conversation_id: str) -> None:
        """Wait until already-received images in one conversation are hydrated."""
        while True:
            with self._pending_media_lock:
                pending = self._pending_media_counts.get(conversation_id, 0)
            if pending <= 0:
                return
            await asyncio.sleep(0.05)

    async def _dispatch_message_in_order(
        self, message: UnifiedMessage, raw_event: dict[str, Any]
    ) -> None:
        """Hydrate and dispatch one conversation without reordering its messages."""
        if self._handler is None:
            return
        lock = self._message_dispatch_locks.setdefault(
            message.conversation_id, asyncio.Lock()
        )
        async with lock:
            hydrated = message
            if message.content_type == ContentType.IMAGE:
                # Image decryption can be slower than the following text row.
                # Serialize only this conversation so the downstream batch sees
                # WeChat's original image -> text ordering without blocking other chats.
                hydrated = await asyncio.to_thread(
                    self._hydrate_media_attachment, message, raw_event
                )
            await self._handler(hydrated)

    @staticmethod
    def _log_handler_failure(future: Any) -> None:
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            logger.error(
                "微信消息处理失败: %s",
                type(error).__name__,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _is_allowed(self, message: UnifiedMessage) -> bool:
        if message.conversation_type == ConversationType.GROUP:
            return message.conversation_id in self._resolved_group_ids
        return message.conversation_id in self._resolved_private_ids

    def _is_mentioned(self, content: str) -> bool:
        return bool(self._nickname and f"@{self._nickname}" in content)

    def _find_group_trigger(self, content: str) -> str | None:
        """Return the longest configured trigger matching the selected rule.

        ``group_prefixes`` is kept as the existing configuration name for
        compatibility.  ``prefix`` matches the beginning (the default),
        ``contains`` matches anywhere, and ``suffix`` matches the end.
        """
        candidates = tuple(
            item.strip() for item in self.settings.group_prefixes if item.strip()
        )
        if self.settings.group_prefixes_rule == "prefix":
            haystack = content.lstrip()
            matches = (item for item in candidates if haystack.startswith(item))
        elif self.settings.group_prefixes_rule == "suffix":
            haystack = content.rstrip()
            matches = (item for item in candidates if haystack.endswith(item))
        else:
            matches = (item for item in candidates if item in content)
        return max(
            matches,
            key=len,
            default=None,
        )

    def _prepare_for_controller(self, message: UnifiedMessage) -> UnifiedMessage:
        original_content = message.content
        triggered = True
        content = original_content
        if message.conversation_type == ConversationType.GROUP:
            trigger = self._find_group_trigger(original_content)
            triggered = bool(message.mentions or trigger)
            if trigger:
                content = original_content.replace(trigger, "", 1).strip()
            elif message.mentions and self._nickname:
                content = re.sub(
                    rf"@{re.escape(self._nickname)}\s*",
                    "",
                    original_content,
                    count=1,
                ).strip()
        return replace(
            message,
            content=content,
            metadata={
                **message.metadata,
                "original_content": original_content,
                "agent_triggered": triggered,
            },
        )

    def _list_allowlisted_conversations_blocking(self) -> list[ConversationItem]:
        started = time.perf_counter()
        targets = (
            *((item, ConversationType.PRIVATE) for item in self._resolved_private_ids),
            *((item, ConversationType.GROUP) for item in self._resolved_group_ids),
        )
        try:
            stage_started = time.perf_counter()
            contact_rows = self._load_contact_rows(
                tuple(conversation_id for conversation_id, _ in targets)
            )
            logger.info(
                "微信会话列表计时: 联系人批量查询 rows=%d elapsed=%.3fs",
                len(contact_rows),
                time.perf_counter() - stage_started,
            )
        except (
            AttributeError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ):
            contact_rows = {}
            logger.warning("读取微信联系人信息失败，将使用名称占位头像", exc_info=True)
        result: list[ConversationItem] = []
        fallback_count = 0
        for conversation_id, conversation_type in targets:
            contact = contact_rows.get(conversation_id, {})
            if not contact:
                fallback_count += 1
            display_name = str(
                contact.get("remark")
                or contact.get("nick_name")
                or self._safe_nickname(conversation_id)
            )
            if display_name:
                self._display_name_cache[conversation_id] = display_name
            result.append(
                ConversationItem(
                    self.name,
                    self._account_id,
                    conversation_id,
                    conversation_type,
                    display_name or conversation_id,
                    str(contact.get("small_head_url") or "") or None,
                )
            )
        logger.info(
            "微信会话列表计时: profiles built count=%d fallback=%d total=%.3fs",
            len(result),
            fallback_count,
            time.perf_counter() - started,
        )
        return result

    def _load_contact_rows(
        self, conversation_ids: tuple[str, ...]
    ) -> dict[str, dict[str, Any]]:
        if not conversation_ids:
            return {}
        with self._db_lock:
            for rel, path, _size in self._db._db_files:
                if os.path.basename(path) != "contact.db":
                    continue
                placeholders = ",".join("?" for _ in conversation_ids)
                connection = self._db._open(rel)
                try:
                    rows = connection.execute(
                        "SELECT username, nick_name, remark, small_head_url "
                        "FROM contact "
                        f"WHERE username IN ({placeholders})",
                        conversation_ids,
                    )
                    result: dict[str, dict[str, Any]] = {}
                    for row in rows:
                        def value(column: str) -> Any:
                            if hasattr(row, "get"):
                                return row.get(column)
                            try:
                                return row[column]
                            except (IndexError, KeyError):
                                return None

                        username = value("username")
                        if username:
                            result[str(username)] = {
                                "username": username,
                                "nick_name": value("nick_name"),
                                "remark": value("remark"),
                                "small_head_url": value("small_head_url"),
                            }
                    return result
                finally:
                    connection.close()
        return {}

    def _load_avatar_urls(self, conversation_ids: tuple[str, ...]) -> dict[str, str]:
        return {
            username: str(row.get("small_head_url") or "")
            for username, row in self._load_contact_rows(conversation_ids).items()
            if row.get("small_head_url")
        }

    def _resolve_allowlisted_targets(self) -> None:
        try:
            session_ids = {
                str(item.get("username") or "")
                for item in self._db.get_sessions(limit=500)
            }
        except (OSError, RuntimeError, TypeError, ValueError):
            session_ids = set()
            logger.warning("读取微信会话索引失败，白名单将按原值监听", exc_info=True)
        self._resolved_private_ids = self._resolve_allowlist_entries(
            self.settings.allowed_private_ids,
            ConversationType.PRIVATE,
            session_ids,
        )
        self._resolved_group_ids = self._resolve_allowlist_entries(
            self.settings.allowed_group_ids,
            ConversationType.GROUP,
            session_ids,
        )
        self._resolved_session_bindings = self._resolve_session_bindings(session_ids)

    def _resolve_session_bindings(
        self, session_ids: set[str]
    ) -> tuple[SessionBindingConfig, ...]:
        resolved: list[SessionBindingConfig] = []
        for binding in self.settings.session_bindings:
            candidates = (
                (binding.conversation_type,)
                if binding.conversation_type is not None
                else (
                    (ConversationType.GROUP,)
                    if binding.conversation_id.endswith("@chatroom")
                    else (ConversationType.PRIVATE, ConversationType.GROUP)
                )
            )
            conversation_id = binding.conversation_id
            for conversation_type in candidates:
                candidate = self._resolve_conversation_username(
                    binding.conversation_id, conversation_type, session_ids
                )
                if candidate != binding.conversation_id or conversation_type == candidates[-1]:
                    conversation_id = candidate
                    break
            resolved.append(replace(binding, conversation_id=conversation_id))
            if conversation_id != binding.conversation_id:
                logger.info(
                    "微信 session 绑定已解析: %s -> %s",
                    binding.conversation_id,
                    conversation_id,
                )
        return tuple(resolved)

    def _resolve_allowlist_entries(
        self,
        configured: tuple[str, ...],
        conversation_type: ConversationType,
        session_ids: set[str],
    ) -> tuple[str, ...]:
        resolved: list[str] = []
        labels: dict[str, list[str]] = {}
        for value in configured:
            username = self._resolve_conversation_username(
                value, conversation_type, session_ids
            )
            if username not in resolved:
                resolved.append(username)
            originals = labels.setdefault(username, [])
            if value not in originals:
                originals.append(value)
            if username == value:
                logger.info("微信白名单使用内部会话 ID: %s", value)
            else:
                logger.info("微信白名单已解析: %s -> %s", value, username)
        # Replace this type's mapping atomically; keep private/group names separate.
        self._allowlist_log_labels = {
            **{key: values for key, values in self._allowlist_log_labels.items()
               if key[0] != conversation_type},
            **{(conversation_type, username): tuple(values) for username, values in labels.items()},
        }
        return tuple(resolved)

    def _configured_message_log_tag(self, message: UnifiedMessage) -> str:
        kind = "群聊" if message.conversation_type == ConversationType.GROUP else "私聊"
        values = self._allowlist_log_labels.get(
            (message.conversation_type, message.conversation_id), ()
        )
        if not values:
            return f"[白名单{kind}: 原始配置项未知]"
        # Quoted values retain Unicode but escape newlines/control characters.
        label = "、".join(json.dumps(value, ensure_ascii=False) for value in values)
        return f"[配置{kind}: {label}]"

    def _resolve_conversation_username(
        self,
        value: str,
        conversation_type: ConversationType,
        session_ids: set[str],
    ) -> str:
        if value == "filehelper" or value in session_ids:
            return value
        try:
            matches = self._db.search_contact(value)
        except (OSError, RuntimeError, TypeError, ValueError):
            logger.warning("微信白名单联系人查询失败: %s", value, exc_info=True)
            return value
        candidates = [
            item
            for item in matches
            if bool(str(item.get("username") or "").endswith("@chatroom"))
            == (conversation_type == ConversationType.GROUP)
        ]
        exact = [
            item
            for item in candidates
            if value
            in {
                str(item.get("username") or ""),
                str(item.get("nick_name") or ""),
                str(item.get("remark") or ""),
            }
        ]
        selected = self._select_unique_contact(exact, session_ids)
        if selected is None:
            selected = self._select_unique_contact(candidates, session_ids)
        if selected is not None:
            self._cache_contact_display_name(selected)
            return str(selected["username"])
        if candidates:
            logger.warning(
                "微信白名单匹配不唯一，继续按原值监听: value=%s matches=%d",
                value,
                len(candidates),
            )
        else:
            logger.warning("微信白名单无法解析，继续按原值监听: %s", value)
        return value

    @staticmethod
    def _select_unique_contact(
        contacts: list[dict[str, Any]], session_ids: set[str]
    ) -> dict[str, Any] | None:
        if len(contacts) == 1:
            return contacts[0]
        active = [
            item for item in contacts if str(item.get("username") or "") in session_ids
        ]
        return active[0] if len(active) == 1 else None

    def _load_history_blocking(
        self, conversation_id: str, limit: int
    ) -> list[UnifiedMessage]:
        messages: list[UnifiedMessage] = []
        rows = self._db.get_messages(conversation_id, limit=limit)
        group_sender_by_numeric_id: dict[str, str] = {}
        if conversation_id.endswith("@chatroom"):
            for raw in rows:
                hinted = self._group_sender_prefix(raw.get("content"))
                numeric_id = str(raw.get("sender_id") or "").strip()
                if (
                    self._is_group_sender_token(hinted)
                    and numeric_id
                    and numeric_id not in self._self_sender_ids
                ):
                    group_sender_by_numeric_id[numeric_id] = hinted
        for raw in reversed(rows):
            event = dict(raw)
            event["username"] = conversation_id
            if conversation_id.endswith("@chatroom"):
                hinted = self._group_sender_prefix(event.get("content"))
                numeric_id = str(event.get("sender_id") or "").strip()
                raw_is_self = self._is_self_message(event) or self._is_self_sender_value(
                    hinted
                )
                if raw_is_self:
                    # Preserve the raw self marker.  A group prefix may still
                    # be removed by normalize(), but it must not change who
                    # sent the message.
                    event["sender_username"] = self._account_id
                else:
                    event["sender_username"] = (
                        (
                            hinted
                            if self._is_group_sender_token(hinted)
                            else group_sender_by_numeric_id.get(numeric_id)
                        )
                        or event.get("sender_username")
                    )
            is_self = self._is_self_message(event)
            sender_id = str(
                event.get("sender_username")
                or event.get("sender_id")
                or conversation_id
            )
            if is_self:
                event["sender_name"] = self._nickname
            elif conversation_id.endswith("@chatroom"):
                sender_name = self._safe_nickname(sender_id)
                if sender_name == sender_id:
                    prefix = re.match(
                        r"^([^:\n]{1,80}):\s*", str(event.get("content") or "")
                    )
                    if prefix:
                        sender_name = prefix.group(1).strip()
                event["sender_name"] = sender_name
            else:
                event["sender_name"] = self._safe_nickname(conversation_id)
            message = self.normalize(event)
            message = self._hydrate_media_attachment(message, event)
            messages.append(
                replace(
                    message,
                    metadata={
                        **message.metadata,
                        "original_content": message.content,
                    },
                )
            )
        return messages

    @staticmethod
    def _group_sender_prefix(content: Any) -> str | None:
        """Extract the authoritative sender token from a group DB row.

        WeChat's ``real_sender_id``/``SenderName2Id`` mapping can be stale or
        account-global.  Group rows commonly carry the actual sender as the
        first ``sender:`` prefix; using it also lets image rows inherit the
        correct sender from an adjacent text row with the same numeric ID.
        """
        match = re.match(r"^([^:\r\n]{1,128}):\s*(?:\r?\n)?", str(content or ""))
        if match is None:
            return None
        candidate = match.group(1).strip()
        return candidate or None

    @staticmethod
    def _is_group_sender_token(value: str | None) -> bool:
        """Return whether a group prefix looks like a WeChat username.

        Display names may also precede a colon.  Only stable username-shaped
        tokens (notably ``wxid_*`` from WeChat's group rows) may override the
        numeric sender mapping; display-name prefixes remain presentation data.
        """
        candidate = str(value or "").strip()
        return bool(
            candidate.startswith(("wxid_", "gh_"))
            or candidate in {"filehelper", "newsapp", "fmessage"}
            or bool(re.fullmatch(r"[a-z0-9_]{3,64}", candidate))
        )

    def _hydrate_media_attachment(
        self, message: UnifiedMessage, raw_event: dict[str, Any]
    ) -> UnifiedMessage:
        """Materialize WeChat image rows so the companion can render them.

        WeChat's message database exposes image metadata (type/local_id), but
        not a directly usable file path.  ``MediaDownloader`` resolves and
        decrypts the matching cached ``.dat`` file into a stable local cache.
        Failures are intentionally non-fatal: the text/``[图片]`` marker still
        remains visible when the media key or cache is unavailable.
        """
        if message.content_type != ContentType.IMAGE or not raw_event.get("local_id"):
            return message
        try:
            from wechatauto import MediaDownloader

            media_root = Path(self.repository.path).resolve().parent / "wechat_media"
            downloader = MediaDownloader(self._db, save_dir=str(media_root))
            keys = self._resolve_media_keys(downloader)
            if keys is None:
                return message
            aes_key, xor_key = keys
            path = downloader.download_image(
                message.conversation_id,
                int(raw_event["local_id"]),
                aes_key=aes_key,
                xor_key=xor_key,
            )
            # MediaDownloader's public image method intentionally accepts
            # local_type 3 only.  Animated stickers use local_type 47 but are
            # stored in the same encrypted Img cache, so handle that row with
            # the downloader's already-tested primitives.
            if not path and str(raw_event.get("type") or "") == "动画表情":
                path = self._download_emoji_media(
                    downloader,
                    message.conversation_id,
                    int(raw_event["local_id"]),
                    aes_key,
                    xor_key,
                    str(media_root),
                )
        except (
            AttributeError,
            ImportError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ) as error:
            logger.debug(
                "微信图片历史无法下载: conversation=%s local_id=%s error=%s",
                message.conversation_id,
                raw_event.get("local_id"),
                type(error).__name__,
            )
            return message
        if not path:
            return message
        attachment = Attachment(
            kind="image",
            name=Path(path).name,
            path=str(Path(path).resolve()),
            mime_type="image/*",
        )
        return replace(message, attachments=(attachment,))

    def _resolve_media_keys(self, downloader: Any) -> tuple[str, int | None] | None:
        """Resolve image keys once without entering MediaDownloader's wait loop.

        WeChatDB may already expose a deterministic cfg-derived key.  If it
        does not, read only the compact process cfg record and derive the key;
        never enter MediaDownloader's minute-long full-memory scan.  Failures
        are cooled down so an image burst cannot stall media workers.
        """
        with self._media_key_lock:
            if self._media_keys is not None:
                return self._media_keys
            now = time.monotonic()
            if now < self._media_key_retry_after:
                return None

            aes_key: str | None = None
            xor_key: int | None = None
            load_cached_key = getattr(downloader, "_load_persisted_key", None)
            if callable(load_cached_key):
                aes_key = load_cached_key()
            if not aes_key:
                derive_cfg_key = getattr(downloader, "_derive_cfg_key", None)
                if callable(derive_cfg_key):
                    derived = derive_cfg_key()
                    if derived:
                        aes_key, xor_key = derived
            if not aes_key:
                direct = self._derive_media_keys_from_process_config(downloader)
                if direct is not None:
                    aes_key, xor_key = direct
            if not aes_key:
                self._media_key_retry_after = now + self._MEDIA_KEY_RETRY_SECONDS
                logger.debug(
                    "微信图片密钥暂不可用，%.0f 秒内不重复探测",
                    self._MEDIA_KEY_RETRY_SECONDS,
                )
                return None
            self._media_keys = (aes_key, xor_key)
            self._media_key_retry_after = 0.0
            return self._media_keys

    @staticmethod
    def _derive_media_keys_from_process_config(
        downloader: Any,
    ) -> tuple[str, int] | None:
        """Use WeChat's small cfg record without the minute-long memory scan.

        Some wechatauto versions validate a derived key against the first file
        returned by an unsorted cache glob.  An old/incompatible first file can
        reject a correct key.  Derive from the authoritative cfg/wxid pair and
        let the selected message image provide the final format validation.
        """
        database = downloader.db
        cfg_dword = getattr(database, "cfg_dword", None)
        database_wxid = str(getattr(database, "wxid", "") or "")
        wxid = database_wxid
        if not cfg_dword:
            extract = getattr(database, "extract_master_key", None)
            if callable(extract):
                extracted = extract()
                if extracted:
                    _master_key, cfg_dword, extracted_wxid = extracted
                    extracted_wxid = str(extracted_wxid or "")
                    if database_wxid and extracted_wxid != database_wxid:
                        return None
                    wxid = extracted_wxid or database_wxid
        derive = getattr(downloader, "derive_image_keys", None)
        if not cfg_dword or not wxid or not callable(derive):
            return None
        return derive(int(cfg_dword), wxid)

    @staticmethod
    def _download_emoji_media(
        downloader: Any,
        conversation_id: str,
        local_id: int,
        aes_key: str,
        xor_key: int | None,
        save_dir: str,
    ) -> str | None:
        """Decode an animated-sticker row using the shared Img cache.

        WeChat's sticker payload may be a normal GIF/PNG or a ``wxgf`` HEVC
        container.  Keep the conversion in the media worker and return a
        regular image path that Qt can display.
        """
        row = downloader.db.get_message_row(conversation_id, local_id)
        if not row or int(row.get("local_type") or 0) != 47:
            return None
        md5 = downloader._img_md5(row)
        if not md5:
            return None
        dat_path = downloader._find_dat(
            conversation_id, md5, row["create_time"]
        )
        suffix = ""
        if not dat_path:
            dat_path = downloader._find_dat(
                conversation_id, md5, row["create_time"], thumbnail=True
            )
            suffix = "_thumb"
        if not dat_path:
            return None
        data = downloader.decrypt_image(dat_path, aes_key, xor_key)
        if data[:3] == b"\xff\xd8\xff":
            ext = "jpg"
        elif data[:4] == b"\x89PNG":
            ext = "png"
        elif data[:3] == b"GIF":
            ext = "gif"
        elif data[:4] == b"wxgf":
            data = downloader._wxgf_to_jpg(data)
            if not data:
                return None
            ext = "jpg"
        else:
            return None
        output = downloader._out(
            save_dir, f"{conversation_id}_{local_id}{suffix}_emoji.{ext}"
        )
        Path(output).write_bytes(data)
        return output

    def _safe_nickname(self, username: str) -> str:
        cached = self._display_name_cache.get(username)
        if cached:
            return cached
        if self._db is None:
            return username
        try:
            nickname = str(self._db.get_nickname(username))
        except (OSError, RuntimeError, TypeError, ValueError):
            return username
        if nickname == username:
            try:
                matches = self._db.search_contact(username)
            except (OSError, RuntimeError, TypeError, ValueError):
                matches = []
            exact = [
                item
                for item in matches
                if str(item.get("username") or "") == username
            ]
            if len(exact) == 1:
                self._cache_contact_display_name(exact[0])
                return self._display_name_cache[username]
        self._display_name_cache[username] = nickname or username
        return self._display_name_cache[username]

    def _sender_display_name(self, message: UnifiedMessage) -> str:
        if message.metadata.get("is_self"):
            return self._nickname or "我"
        if message.conversation_type == ConversationType.PRIVATE:
            lookup_key = message.conversation_id
        else:
            lookup_key = message.sender_id
        display_name = self._safe_nickname(lookup_key)
        if display_name == lookup_key and message.sender_name:
            return message.sender_name
        return display_name

    def _cache_contact_display_name(self, contact: dict[str, Any]) -> None:
        username = str(contact.get("username") or "")
        if not username:
            return
        display_name = str(
            contact.get("remark") or contact.get("nick_name") or username
        )
        self._display_name_cache[username] = display_name

    def _silent_target_names(self, conversation_id: str) -> tuple[str, ...]:
        candidates = [self._safe_nickname(conversation_id), conversation_id]
        try:
            matches = self._db.search_contact(conversation_id)
        except (OSError, RuntimeError, TypeError, ValueError):
            matches = []
        for match in matches:
            if str(match.get("username") or "") != conversation_id:
                continue
            for key in ("remark", "nick_name", "alias"):
                value = str(match.get(key) or "").strip()
                if value:
                    candidates.append(value)
        return tuple(dict.fromkeys(value for value in candidates if value))

    def _verify_silent_delivery(self, delivery: OutboundDelivery) -> bool:
        # WeChatDB may need one incremental WAL refresh after the UI action.
        # Keep checking long enough for a second read so a delivered message is
        # never retried merely because the first decrypted snapshot was stale.
        deadline = time.monotonic() + 15.0
        minimum_timestamp = delivery.next_attempt_at.timestamp() - 2.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                rows = self._db.get_messages(delivery.conversation_id, limit=10)
            except (sqlite3.Error, OSError, RuntimeError) as error:
                last_error = error
                time.sleep(0.25)
                continue
            last_error = None
            if self._delivery_matches_rows(delivery, rows, minimum_timestamp):
                return True
            time.sleep(0.25)
        if last_error is not None:
            logger.warning(
                "微信发送结果暂时无法验证，等待微信回显确认: %s",
                last_error,
            )
        return False

    def _delivery_matches_rows(
        self,
        delivery: OutboundDelivery,
        rows: list[dict[str, Any]],
        minimum_timestamp: float,
    ) -> bool:
        content_type = getattr(delivery, "content_type", ContentType.TEXT)
        if content_type != ContentType.IMAGE:
            for row in rows:
                if (
                    float(row.get("create_time") or 0) >= minimum_timestamp
                    and str(row.get("content") or "") == delivery.text
                    and self._claim_delivery_row(delivery.conversation_id, row)
                ):
                    self._remember_self_identity(row)
                    return True
            return False

        caption = str(delivery.text or "").strip()
        requires_text = bool(caption and not caption.startswith("[图片]"))
        matched_image = False
        matched_text = not requires_text
        matched_rows: list[dict[str, Any]] = []
        for row in rows:
            if float(row.get("create_time") or 0) < minimum_timestamp:
                continue
            if self._delivery_row_key(row) in self._verified_delivery_rows.get(
                delivery.conversation_id, set()
            ):
                continue
            if not self._is_self_message(row):
                continue
            if str(row.get("type") or "") == "图片":
                matched_image = True
                matched_rows.append(row)
            elif requires_text and str(row.get("content") or "") == caption:
                matched_text = True
                matched_rows.append(row)
        if matched_image and matched_text:
            for row in matched_rows:
                self._claim_delivery_row(delivery.conversation_id, row)
                self._remember_self_identity(row)
            return True
        return False

    @staticmethod
    def _delivery_row_key(row: dict[str, Any]) -> tuple[str, ...]:
        """Return a stable identity for one decrypted WeChat message row."""
        return (
            str(row.get("local_id") or ""),
            str(row.get("sort_seq") or ""),
            str(row.get("create_time") or ""),
            str(row.get("type") or ""),
            str(row.get("sender_id") or ""),
            str(row.get("content") or ""),
        )

    def _claim_delivery_row(
        self, conversation_id: str, row: dict[str, Any]
    ) -> bool:
        key = self._delivery_row_key(row)
        claimed = self._verified_delivery_rows.setdefault(conversation_id, set())
        if key in claimed:
            return False
        claimed.add(key)
        # Verification is process-local, but keep a bad/stale WeChat session
        # from growing this set without bound.
        if len(claimed) > 512:
            claimed.clear()
            claimed.add(key)
        return True

    def _record_pending_outbound(
        self,
        conversation_id: str,
        content: str,
        content_type: ContentType = ContentType.TEXT,
    ) -> None:
        timestamp = time.monotonic()
        pending_types = (content_type,)
        if (
            content_type == ContentType.IMAGE
            and content.strip()
            and not content.lstrip().startswith("[图片]")
        ):
            pending_types = (ContentType.TEXT, ContentType.IMAGE)
        with self._pending_outbound_lock:
            self._pending_outbound.extend(
                (conversation_id, content, pending_type, timestamp)
                for pending_type in pending_types
            )

    def _consume_pending_outbound(
        self,
        conversation_id: str,
        content: str,
        content_type: ContentType = ContentType.TEXT,
    ) -> str | None:
        now = time.monotonic()
        with self._pending_outbound_lock:
            while self._pending_outbound and now - self._pending_outbound[0][3] > 60:
                self._pending_outbound.popleft()
            for pending in self._pending_outbound:
                content_matches = (
                    content_type == ContentType.IMAGE
                    if pending[2] == ContentType.IMAGE
                    else content_type == pending[2] and pending[1] == content
                )
                if pending[0] == conversation_id and content_matches:
                    self._pending_outbound.remove(pending)
                    return pending[1]
        return None

    def _discard_pending_outbound(self, conversation_id: str, content: str) -> None:
        with self._pending_outbound_lock:
            for pending in tuple(self._pending_outbound):
                if pending[0] == conversation_id and pending[1] == content:
                    self._pending_outbound.remove(pending)
