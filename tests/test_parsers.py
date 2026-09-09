from dataclasses import dataclass

from agent_bridge.models import EventType, ProviderRun
from agent_bridge.parsers.claude import ClaudeParser
from agent_bridge.parsers.codex import CodexParser


@dataclass
class AgentMessageThreadItem:
    text: str


@dataclass
class Root:
    root: AgentMessageThreadItem


@dataclass
class CodexResult:
    final_response: str
    usage: dict


@dataclass
class TextBlock:
    text: str


@dataclass
class AssistantMessage:
    content: list


@dataclass
class ResultMessage:
    result: str
    usage: dict
    session_id: str
    is_error: bool = False


def test_codex_parser_normalizes_assistant_message() -> None:
    parser = CodexParser()
    run = ProviderRun(
        (Root(AgentMessageThreadItem("done")),),
        CodexResult("done", {"tokens": 5}),
        "thread-1",
    )

    response = parser.parse_final(run)

    assert response.text == "done"
    assert response.events[0].type == EventType.ASSISTANT_MESSAGE
    assert response.events[0].content == "done"


def test_claude_parser_normalizes_assistant_and_result() -> None:
    parser = ClaudeParser()
    final = ResultMessage("done", {"tokens": 5}, "session-1")
    run = ProviderRun((AssistantMessage([TextBlock("done")]), final), final, "session-1")

    response = parser.parse_final(run)

    assert response.text == "done"
    assert any(event.type == EventType.ASSISTANT_MESSAGE for event in response.events)
    assert any(event.type == EventType.USAGE for event in response.events)

