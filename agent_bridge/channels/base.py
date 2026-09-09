from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from agent_bridge.models import ChannelTarget, OutboundMessage, UnifiedMessage

MessageHandler = Callable[[UnifiedMessage], Awaitable[None]]


class ChannelAdapter(ABC):
    name: str

    @abstractmethod
    async def start(self, handler: MessageHandler) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def send_message(self, target: ChannelTarget, message: OutboundMessage) -> str:
        raise NotImplementedError

    async def send_status(self, target: ChannelTarget, status: str) -> None:
        await self.send_message(target, OutboundMessage(status))

    async def edit_message(
        self, target: ChannelTarget, message_id: str, message: OutboundMessage
    ) -> None:
        raise NotImplementedError(f"{self.name} does not support message editing")

    @abstractmethod
    def normalize(self, raw_event: Any) -> UnifiedMessage:
        raise NotImplementedError

