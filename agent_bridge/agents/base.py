from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from agent_bridge.models import (
    AgentRequest,
    HealthStatus,
    NativeSession,
    ProviderRun,
    TextDeltaCallback,
)


class AgentAdapter(ABC):
    provider: str

    @abstractmethod
    async def create_session(
        self, unified_session_id: str, working_directory: str, model: str | None = None
    ) -> NativeSession:
        raise NotImplementedError

    @abstractmethod
    async def resume_session(self, native: NativeSession) -> None:
        raise NotImplementedError

    @abstractmethod
    async def run(
        self,
        native: NativeSession,
        request: AgentRequest,
        on_text_delta: TextDeltaCallback | None = None,
    ) -> ProviderRun:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, job_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class AgentParser(ABC):
    provider: str

    @abstractmethod
    def parse_event(self, raw_event: Any) -> list[Any]:
        raise NotImplementedError

    @abstractmethod
    def parse_final(self, result: ProviderRun) -> Any:
        raise NotImplementedError
