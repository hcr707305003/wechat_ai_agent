from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from queue import Queue
from threading import Event, Thread

from agent_bridge.agents.availability import probe_agent
from agent_bridge.agents.claude import ClaudeAdapter
from agent_bridge.agents.codex import CodexAdapter
from agent_bridge.config import AppConfig
from agent_bridge.models import (
    AgentContext,
    AgentRequest,
    ConversationType,
    EventType,
    UnifiedMessage,
    new_id,
)
from agent_bridge.parsers.claude import ClaudeParser
from agent_bridge.parsers.codex import CodexParser

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DebugEvent:
    kind: str
    text: str = ""


def build_debug_adapter(config: AppConfig, provider: str):
    settings = config.agents.get(provider)
    if settings is None or not settings.enabled:
        raise ValueError(f"请先在参数配置中启用 {provider}。")
    state = probe_agent(provider)
    if not state.available:
        raise ValueError(f"{provider} 不可用：{state.reason}")
    if provider == "codex":
        return CodexAdapter(settings.codex), CodexParser()
    if provider == "claude":
        return ClaudeAdapter(settings.claude), ClaudeParser()
    raise ValueError(f"不支持的 Agent：{provider}")


class AgentDebugRunner:
    """Each turn owns its SDK clients and event loop; only native IDs survive turns."""

    def __init__(self, adapter_factory=build_debug_adapter) -> None:
        self.events: Queue[DebugEvent] = Queue()
        self._adapter_factory = adapter_factory
        self._sessions = {}
        self._thread: Thread | None = None
        self._loop = None
        self._task = None
        self._cancelled = Event()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: AppConfig, provider: str, prompt: str) -> None:
        if self.busy:
            raise RuntimeError("请等待当前回复结束。")
        self._cancelled.clear()
        self._thread = Thread(
            target=self._worker,
            args=(config, provider, prompt),
            name="agent-chat-debug",
            daemon=True,
        )
        self._thread.start()

    def reset(self) -> None:
        if self.busy:
            raise RuntimeError("请先停止当前回复。")
        self._sessions.clear()

    def cancel(self) -> None:
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The turn has already closed its loop.

    def _worker(self, config, provider, prompt) -> None:
        try:
            text = asyncio.run(self._run(config, provider, prompt))
            outcome = DebugEvent("done", text)
        except asyncio.CancelledError:
            self._sessions.pop(provider, None)
            outcome = DebugEvent(
                "cancelled", "已停止生成；下次发送会创建新的调试会话。"
            )
        except TimeoutError:
            self._sessions.pop(provider, None)
            outcome = DebugEvent(
                "error", "Agent 响应超时，请检查网络、登录状态或超时配置。"
            )
        except Exception as error:  # noqa: BLE001 - isolated SDK worker boundary
            self._sessions.pop(provider, None)
            outcome = DebugEvent("error", f"{type(error).__name__}: {error}")
        finally:
            self._loop = self._task = None
        self.events.put(outcome)

    async def _run(self, config, provider, prompt) -> str:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        if self._cancelled.is_set():
            raise asyncio.CancelledError
        adapter, parser = self._adapter_factory(config, provider)
        try:
            return await asyncio.wait_for(
                self._turn(adapter, parser, config, provider, prompt),
                timeout=config.runtime.timeout_seconds,
            )
        finally:
            try:
                await asyncio.wait_for(adapter.close(), timeout=5)
            except Exception:
                logger.warning("Agent 调试客户端清理失败", exc_info=True)

    async def _turn(self, adapter, parser, config, provider, prompt) -> str:
        key = (config.agents[provider], config.runtime.default_working_directory)
        previous = self._sessions.get(provider)
        if previous is None or previous[0] != key:
            self.events.put(DebugEvent("status", "正在创建独立调试会话…"))
            native = await adapter.create_session(
                new_id("debug"),
                config.runtime.default_working_directory,
            )
        else:
            native = previous[1]
        message = UnifiedMessage(
            channel="manager_debug",
            channel_account_id="local",
            conversation_id=native.unified_session_id,
            conversation_type=ConversationType.PRIVATE,
            sender_id="debug_user",
            message_id=new_id("debug_message"),
            content=prompt,
        )
        request = AgentRequest(
            prompt=prompt,
            context=AgentContext("", (), message),
            job_id=new_id("debug_job"),
            model=native.model,
        )

        async def on_delta(text: str) -> None:
            if self._cancelled.is_set():
                raise asyncio.CancelledError
            self.events.put(DebugEvent("delta", text))

        self.events.put(DebugEvent("status", "正在等待 Agent 回复…"))
        result = await adapter.run(native, request, on_text_delta=on_delta)
        response = parser.parse_final(result)
        if getattr(result.final_result, "is_error", False) or any(
            event.type == EventType.ERROR for event in response.events
        ):
            raise RuntimeError(response.text or "Agent 返回错误。")
        if (
            not response.text.strip()
            or response.text == "Agent 已完成任务，但没有返回文本结果。"
        ):
            raise RuntimeError("Agent 未返回文本，未通过调试。")
        self._sessions[provider] = (
            key,
            replace(
                native,
                native_session_id=result.native_session_id,
                context_initialized=True,
            ),
        )
        return response.text
