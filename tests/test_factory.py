import pytest

from agent_bridge.agents.factory import AgentFactory
from agent_bridge.testing import FakeAgentAdapter, FakeAgentParser


def test_factory_registers_and_caches_provider() -> None:
    factory = AgentFactory()
    factory.register("fake", FakeAgentAdapter, FakeAgentParser)

    first = factory.get("FAKE")
    second = factory.get("fake")

    assert first is second
    assert factory.providers() == ("fake",)


def test_factory_rejects_mismatched_registration() -> None:
    factory = AgentFactory()
    factory.register(
        "wrong",
        lambda: FakeAgentAdapter(provider="fake"),
        lambda: FakeAgentParser(provider="fake"),
    )

    with pytest.raises(ValueError, match="mismatch"):
        factory.get("wrong")

