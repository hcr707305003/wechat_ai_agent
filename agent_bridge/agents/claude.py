from __future__ import annotations

import asyncio
import base64
import mimetypes
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from agent_bridge.agents.base import AgentAdapter
from agent_bridge.models import (
    AgentRequest,
    HealthStatus,
    NativeSession,
    ProviderRun,
    TextDeltaCallback,
    new_id,
)
from agent_bridge.sessions.context import render_request_prompt


@dataclass(slots=True, frozen=True)
class ClaudeAdapterSettings:
    model: str | None = None
    permission_mode: str = "default"
    system_prompt: str | None = None
    allow_bash: bool = False
    allow_network: bool = False


class ClaudeAdapter(AgentAdapter):
    provider = "claude"

    def __init__(self, settings: ClaudeAdapterSettings | None = None) -> None:
        self.settings = settings or ClaudeAdapterSettings()
        self._active_clients: dict[str, Any] = {}

    @staticmethod
    def _load_sdk() -> tuple[Any, Any]:
        try:
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
        except ImportError as exc:
            raise RuntimeError(
                "Claude adapter requires: pip install -e .[claude]"
            ) from exc
        return ClaudeAgentOptions, ClaudeSDKClient

    async def create_session(
        self, unified_session_id: str, working_directory: str, model: str | None = None
    ) -> NativeSession:
        return NativeSession(
            id=new_id("native"),
            unified_session_id=unified_session_id,
            provider=self.provider,
            native_session_id=str(uuid4()),
            working_directory=working_directory,
            model=model or self.settings.model,
        )

    async def resume_session(self, native: NativeSession) -> None:
        UUID(native.native_session_id)

    async def run(
        self,
        native: NativeSession,
        request: AgentRequest,
        on_text_delta: TextDeltaCallback | None = None,
    ) -> ProviderRun:
        ClaudeAgentOptions, ClaudeSDKClient = self._load_sdk()
        if self.settings.permission_mode not in {"default", "plan", "dontAsk"}:
            raise ValueError(
                "Claude permission_mode must be default, plan, or dontAsk so the workspace guard remains active"
            )
        options: dict[str, Any] = {
            "cwd": native.working_directory,
            "model": native.model or self.settings.model,
            "permission_mode": self.settings.permission_mode,
            "system_prompt": self.settings.system_prompt,
            "can_use_tool": self._permission_guard(native.working_directory),
            "include_partial_messages": on_text_delta is not None,
        }
        if native.context_initialized:
            options["resume"] = native.native_session_id
        else:
            options["session_id"] = native.native_session_id
        client = ClaudeSDKClient(ClaudeAgentOptions(**options))
        self._active_clients[request.job_id] = client
        messages: list[Any] = []
        try:
            prompt = render_request_prompt(request)
            images = tuple(
                item
                for item in request.input_attachments
                if item.kind == "image" and item.path
            )
            await client.connect(
                self._multimodal_prompt(prompt, images) if images else prompt
            )
            async for item in client.receive_response():
                messages.append(item)
                delta = self._partial_text(item)
                if delta and on_text_delta is not None:
                    await on_text_delta(delta)
        except asyncio.CancelledError:
            with suppress(Exception):
                await client.interrupt()
            raise
        finally:
            self._active_clients.pop(request.job_id, None)
            await client.disconnect()
        final = next(
            (item for item in reversed(messages) if type(item).__name__ == "ResultMessage"),
            messages[-1] if messages else None,
        )
        native_id = getattr(final, "session_id", None) or native.native_session_id
        return ProviderRun(tuple(messages), final, native_id)

    @staticmethod
    async def _multimodal_prompt(prompt: str, images):
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for attachment in images:
            path = Path(str(attachment.path))
            mime_type = ClaudeAdapter._image_mime_type(
                path, attachment.mime_type
            )
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                    },
                }
            )
        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": "default",
        }

    @staticmethod
    def _image_mime_type(path: Path, configured: str | None) -> str:
        supported = {"image/jpeg", "image/png", "image/gif", "image/webp"}
        if configured in supported:
            return configured
        guessed = mimetypes.guess_type(path.name)[0]
        if guessed in supported:
            return str(guessed)
        header = path.read_bytes()[:16]
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if header.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if header.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return "image/webp"
        raise ValueError(f"Unsupported Claude image format: {path.name}")

    @staticmethod
    def _partial_text(item: Any) -> str:
        if type(item).__name__ != "StreamEvent":
            return ""
        event = getattr(item, "event", {})
        if event.get("type") != "content_block_delta":
            return ""
        delta = event.get("delta", {})
        if delta.get("type") != "text_delta":
            return ""
        return str(delta.get("text", "") or "")

    def _permission_guard(self, working_directory: str):
        root = Path(working_directory).resolve()

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], _context: Any):
            from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

            read_tools = {"Read", "Glob", "Grep"}
            write_tools = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
            neutral_tools = {"TodoWrite", "Task", "TaskOutput"}
            if tool_name in neutral_tools:
                return PermissionResultAllow()
            if tool_name in read_tools | write_tools:
                paths = [
                    tool_input.get(key)
                    for key in ("file_path", "path", "notebook_path")
                    if tool_input.get(key)
                ]
                if not paths:
                    paths = [str(root)]
                if all(self._is_within_root(root, str(path)) for path in paths):
                    return PermissionResultAllow()
                return PermissionResultDeny(
                    message="Tool path is outside the configured working directory"
                )
            if tool_name == "Bash" and self.settings.allow_bash:
                return PermissionResultAllow()
            if tool_name in {"WebFetch", "WebSearch"} and self.settings.allow_network:
                return PermissionResultAllow()
            return PermissionResultDeny(message=f"Tool is not enabled: {tool_name}")

        return can_use_tool

    @staticmethod
    def _is_within_root(root: Path, value: str) -> bool:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        return resolved == root or resolved.is_relative_to(root)

    async def cancel(self, job_id: str) -> None:
        client = self._active_clients.get(job_id)
        if client is not None:
            await client.interrupt()

    async def health_check(self) -> HealthStatus:
        try:
            self._load_sdk()
        except Exception as exc:  # noqa: BLE001 - health checks must report SDK failures
            return HealthStatus(False, f"{type(exc).__name__}: {exc}")
        return HealthStatus(True, "Claude Agent SDK import is available")
