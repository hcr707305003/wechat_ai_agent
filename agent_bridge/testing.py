from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_bridge.agents.base import AgentAdapter, AgentParser
from agent_bridge.channels.base import ChannelAdapter, MessageHandler
from agent_bridge.models import (
    AgentRequest,
    ChannelTarget,
    EventType,
    HealthStatus,
    NativeSession,
    OutboundMessage,
    ProviderRun,
    TextDeltaCallback,
    UnifiedEvent,
    UnifiedMessage,
    UnifiedResponse,
    new_id,
)


class FakeAgentAdapter(AgentAdapter):
    def __init__(
        self,
        provider: str = "fake",
        response_prefix: str = "reply",
        stream_chunks: tuple[str, ...] = (),
    ) -> None:
        self.provider = provider
        self.response_prefix = response_prefix
        self.stream_chunks = stream_chunks
        self.requests: list[AgentRequest] = []
        self.cancelled: list[str] = []

    async def create_session(
        self, unified_session_id: str, working_directory: str, model: str | None = None
    ) -> NativeSession:
        return NativeSession(
            id=new_id("native"),
            unified_session_id=unified_session_id,
            provider=self.provider,
            native_session_id=new_id(f"{self.provider}_session"),
            working_directory=working_directory,
            model=model,
        )

    async def resume_session(self, native: NativeSession) -> None:
        return None

    async def run(
        self,
        native: NativeSession,
        request: AgentRequest,
        on_text_delta: TextDeltaCallback | None = None,
    ) -> ProviderRun:
        self.requests.append(request)
        text = f"{self.response_prefix}: {request.prompt}"
        if on_text_delta is not None:
            for chunk in self.stream_chunks:
                await on_text_delta(chunk)
        return ProviderRun(
            raw_events=({"type": "assistant_message", "text": text},),
            final_result={"text": text},
            native_session_id=native.native_session_id,
        )

    async def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)

    async def health_check(self) -> HealthStatus:
        return HealthStatus(True, "fake adapter ready")


class FakeAgentParser(AgentParser):
    def __init__(self, provider: str = "fake") -> None:
        self.provider = provider

    def parse_event(self, raw_event: Any) -> list[UnifiedEvent]:
        if isinstance(raw_event, dict) and raw_event.get("type") == "assistant_message":
            return [
                UnifiedEvent(
                    EventType.ASSISTANT_MESSAGE,
                    raw_event.get("text", ""),
                    provider=self.provider,
                    raw=raw_event,
                )
            ]
        return [UnifiedEvent(EventType.UNKNOWN, provider=self.provider, raw=raw_event)]

    def parse_final(self, result: ProviderRun) -> UnifiedResponse:
        text = result.final_result.get("text", "") if isinstance(result.final_result, dict) else str(result.final_result)
        events = tuple(event for raw in result.raw_events for event in self.parse_event(raw))
        return UnifiedResponse(text, events, result.native_session_id)


@dataclass(slots=True)
class SentMessage:
    target: ChannelTarget
    message: OutboundMessage
    message_id: str


class FakeChannelAdapter(ChannelAdapter):
    name = "fake"

    def __init__(self) -> None:
        self.handler: MessageHandler | None = None
        self.sent: list[SentMessage] = []

    async def start(self, handler: MessageHandler) -> None:
        self.handler = handler

    async def stop(self) -> None:
        self.handler = None

    async def send_message(self, target: ChannelTarget, message: OutboundMessage) -> str:
        message_id = new_id("outbound")
        self.sent.append(SentMessage(target, message, message_id))
        return message_id

    def normalize(self, raw_event: Any) -> UnifiedMessage:
        if not isinstance(raw_event, UnifiedMessage):
            raise TypeError("Fake channel accepts UnifiedMessage events")
        return raw_event

    async def emit(self, message: UnifiedMessage) -> None:
        if self.handler is None:
            raise RuntimeError("Fake channel has not started")
        await self.handler(message)
