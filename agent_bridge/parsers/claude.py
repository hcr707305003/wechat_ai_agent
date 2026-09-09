from __future__ import annotations

from typing import Any

from agent_bridge.agents.base import AgentParser
from agent_bridge.models import EventType, ProviderRun, UnifiedEvent, UnifiedResponse
from agent_bridge.parsers.base import class_key, to_plain


class ClaudeParser(AgentParser):
    provider = "claude"

    def parse_event(self, raw_event: Any) -> list[UnifiedEvent]:
        key = class_key(raw_event)
        plain = to_plain(raw_event)
        if key == "assistantmessage":
            events: list[UnifiedEvent] = []
            for block in getattr(raw_event, "content", ()):
                block_key = class_key(block)
                if block_key == "textblock":
                    events.append(
                        UnifiedEvent(
                            EventType.ASSISTANT_MESSAGE,
                            getattr(block, "text", ""),
                            self.provider,
                            to_plain(block),
                        )
                    )
                elif "tooluse" in block_key:
                    events.append(
                        UnifiedEvent(EventType.TOOL_CALL, to_plain(block), self.provider, to_plain(block))
                    )
            return events or [UnifiedEvent(EventType.UNKNOWN, provider=self.provider, raw=plain)]
        if key == "usermessage":
            tool_results = [
                UnifiedEvent(EventType.TOOL_RESULT, to_plain(block), self.provider, to_plain(block))
                for block in getattr(raw_event, "content", ())
                if "toolresult" in class_key(block)
            ]
            return tool_results or [UnifiedEvent(EventType.UNKNOWN, provider=self.provider, raw=plain)]
        if key == "resultmessage":
            event_type = EventType.ERROR if getattr(raw_event, "is_error", False) else EventType.USAGE
            return [UnifiedEvent(event_type, plain, self.provider, plain)]
        if key == "systemmessage":
            return [UnifiedEvent(EventType.STATUS, plain, self.provider, plain)]
        return [UnifiedEvent(EventType.UNKNOWN, provider=self.provider, raw=plain)]

    def parse_final(self, result: ProviderRun) -> UnifiedResponse:
        events = tuple(event for raw in result.raw_events for event in self.parse_event(raw))
        final = result.final_result
        text = getattr(final, "result", None) or ""
        if not text:
            assistant_text = [
                str(event.content)
                for event in events
                if event.type == EventType.ASSISTANT_MESSAGE and event.content
            ]
            text = "\n".join(assistant_text)
        if not text:
            text = "Agent 已完成任务，但没有返回文本结果。"
        usage = to_plain(getattr(final, "usage", None)) or {}
        return UnifiedResponse(text, events, result.native_session_id, usage)
