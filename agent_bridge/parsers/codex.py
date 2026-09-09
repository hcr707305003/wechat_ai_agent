from __future__ import annotations

from typing import Any

from agent_bridge.agents.base import AgentParser
from agent_bridge.models import EventType, ProviderRun, UnifiedEvent, UnifiedResponse
from agent_bridge.parsers.base import class_key, to_plain


class CodexParser(AgentParser):
    provider = "codex"

    def parse_event(self, raw_event: Any) -> list[UnifiedEvent]:
        key = class_key(raw_event)
        plain = to_plain(raw_event)
        root = getattr(raw_event, "root", raw_event)
        root_key = class_key(root)
        if "agentmessage" in root_key:
            return [
                UnifiedEvent(
                    EventType.ASSISTANT_MESSAGE,
                    getattr(root, "text", ""),
                    provider=self.provider,
                    raw=plain,
                )
            ]
        if any(token in root_key for token in ("commandexecution", "mcp", "filechange")):
            return [
                UnifiedEvent(
                    EventType.TOOL_RESULT,
                    plain,
                    provider=self.provider,
                    raw=plain,
                )
            ]
        if "usage" in key:
            return [UnifiedEvent(EventType.USAGE, plain, self.provider, plain)]
        return [UnifiedEvent(EventType.UNKNOWN, provider=self.provider, raw=plain)]

    def parse_final(self, result: ProviderRun) -> UnifiedResponse:
        raw_result = result.final_result
        text = (
            getattr(raw_result, "final_response", None)
            or "Agent 已完成任务，但没有返回文本结果。"
        )
        parsed = [event for raw in result.raw_events for event in self.parse_event(raw)]
        usage = to_plain(getattr(raw_result, "usage", None)) or {}
        if usage:
            parsed.append(UnifiedEvent(EventType.USAGE, usage, self.provider, usage))
        return UnifiedResponse(text, tuple(parsed), result.native_session_id, usage)
