import pytest

from agent_bridge.runtime.commands import CommandError, CommandType, parse_command


def test_parse_agent_and_ask_commands() -> None:
    assert parse_command("/agent Claude").provider == "claude"
    ask = parse_command('/ask codex "fix the tests"')
    assert ask is not None
    assert ask.type == CommandType.ASK
    assert ask.provider == "codex"
    assert ask.value == "fix the tests"


def test_reject_invalid_command() -> None:
    with pytest.raises(CommandError):
        parse_command("/session bind codex")

