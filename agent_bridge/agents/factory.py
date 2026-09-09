from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agent_bridge.agents.base import AgentAdapter, AgentParser


@dataclass(slots=True, frozen=True)
class AgentRegistration:
    adapter_factory: Callable[[], AgentAdapter]
    parser_factory: Callable[[], AgentParser]


class AgentFactory:
    def __init__(self) -> None:
        self._registrations: dict[str, AgentRegistration] = {}
        self._instances: dict[str, tuple[AgentAdapter, AgentParser]] = {}

    def register(
        self,
        provider: str,
        adapter_factory: Callable[[], AgentAdapter],
        parser_factory: Callable[[], AgentParser],
    ) -> None:
        normalized = provider.strip().lower()
        if not normalized:
            raise ValueError("Provider name cannot be empty")
        if normalized in self._registrations:
            raise ValueError(f"Provider already registered: {normalized}")
        self._registrations[normalized] = AgentRegistration(adapter_factory, parser_factory)

    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._registrations))

    def get(self, provider: str) -> tuple[AgentAdapter, AgentParser]:
        normalized = provider.strip().lower()
        registration = self._registrations.get(normalized)
        if registration is None:
            raise KeyError(f"Unknown agent provider: {normalized}")
        if normalized not in self._instances:
            adapter = registration.adapter_factory()
            parser = registration.parser_factory()
            if adapter.provider != normalized or parser.provider != normalized:
                raise ValueError(f"Provider registration mismatch: {normalized}")
            self._instances[normalized] = adapter, parser
        return self._instances[normalized]

    async def close(self) -> None:
        for adapter, _ in self._instances.values():
            await adapter.close()
        self._instances.clear()

