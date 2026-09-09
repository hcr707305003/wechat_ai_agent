from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

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

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class CodexAdapterSettings:
    model: str | None = None
    fallback_models: tuple[str, ...] = ()
    sandbox: str = "workspace-write"
    approval_mode: str = "deny_all"
    base_instructions: str | None = None


class CodexAdapter(AgentAdapter):
    provider = "codex"

    def __init__(self, settings: CodexAdapterSettings | None = None) -> None:
        self.settings = settings or CodexAdapterSettings()
        self._client: Any = None
        self._threads: dict[str, Any] = {}
        self._thread_models: dict[str, str | None] = {}
        self._turns: dict[str, Any] = {}

    async def _get_client(self) -> Any:
        if self._client is None:
            try:
                from openai_codex import AsyncCodex
            except ImportError as exc:
                raise RuntimeError(
                    "Codex adapter requires: pip install -e .[codex]"
                ) from exc
            self._client = AsyncCodex()
        return self._client

    def _sdk_options(self) -> dict[str, Any]:
        from openai_codex import ApprovalMode, Sandbox

        sandbox = {
            "read-only": Sandbox.read_only,
            "workspace-write": Sandbox.workspace_write,
            "full-access": Sandbox.full_access,
        }.get(self.settings.sandbox)
        approval = {
            "deny_all": ApprovalMode.deny_all,
            "auto_review": ApprovalMode.auto_review,
        }.get(self.settings.approval_mode)
        if sandbox is None or approval is None:
            raise ValueError("Invalid Codex sandbox or approval mode")
        return {
            "sandbox": sandbox,
            "approval_mode": approval,
            "base_instructions": self.settings.base_instructions,
        }

    async def create_session(
        self, unified_session_id: str, working_directory: str, model: str | None = None
    ) -> NativeSession:
        client = await self._get_client()
        options = self._sdk_options()
        thread = await client.thread_start(
            cwd=working_directory,
            model=model or self.settings.model,
            **options,
        )
        self._threads[thread.id] = thread
        self._thread_models[thread.id] = model or self.settings.model
        return NativeSession(
            id=new_id("native"),
            unified_session_id=unified_session_id,
            provider=self.provider,
            native_session_id=thread.id,
            working_directory=working_directory,
            model=model or self.settings.model,
        )

    async def resume_session(self, native: NativeSession) -> None:
        if native.native_session_id in self._threads:
            return
        client = await self._get_client()
        try:
            thread = await client.thread_resume(
                native.native_session_id,
                cwd=native.working_directory,
                model=native.model or self.settings.model,
                **self._sdk_options(),
            )
        except Exception as exc:
            if not self._is_archived_session_error(exc):
                raise
            unarchive = getattr(client, "thread_unarchive", None)
            if unarchive is None:
                raise RuntimeError(
                    f"Codex session {native.native_session_id} 已归档，当前 SDK 不支持自动解档"
                ) from exc
            logger.warning(
                "Codex session 已归档，自动解档后重试: session=%s",
                native.native_session_id,
            )
            await unarchive(native.native_session_id)
            thread = await client.thread_resume(
                native.native_session_id,
                cwd=native.working_directory,
                model=native.model or self.settings.model,
                **self._sdk_options(),
            )
        self._threads[native.native_session_id] = thread
        self._thread_models[native.native_session_id] = (
            native.model or self.settings.model
        )

    async def run(
        self,
        native: NativeSession,
        request: AgentRequest,
        on_text_delta: TextDeltaCallback | None = None,
    ) -> ProviderRun:
        await self.resume_session(native)
        thread = self._threads[native.native_session_id]
        prompt = render_request_prompt(request)
        turn_input: Any = prompt
        images = tuple(
            item
            for item in request.input_attachments
            if item.kind == "image" and item.path
        )
        if images:
            from openai_codex import LocalImageInput, TextInput

            turn_input = [
                TextInput(prompt),
                *(LocalImageInput(str(item.path)) for item in images),
            ]
        candidates = self._model_candidates(native, thread.id)
        for index, model in enumerate(candidates):
            turn = None
            try:
                turn = await thread.turn(turn_input, model=model)
                self._turns[request.job_id] = turn
                if on_text_delta is None:
                    result = await turn.run()
                else:
                    result = await self._run_stream(turn, on_text_delta)
            except asyncio.CancelledError:
                if turn is not None:
                    with suppress(Exception):
                        await turn.interrupt()
                raise
            except RuntimeError as exc:
                if not self._is_capacity_error(exc) or index + 1 >= len(candidates):
                    raise
                logger.warning(
                    "Codex 模型容量不足，切换备用模型: current=%s fallback=%s",
                    model or "default",
                    candidates[index + 1] or "default",
                )
                continue
            finally:
                if self._turns.get(request.job_id) is turn:
                    self._turns.pop(request.job_id, None)
            self._thread_models[thread.id] = model
            return ProviderRun(tuple(result.items), result, thread.id)
        raise RuntimeError("No Codex model candidates configured")

    def _model_candidates(
        self, native: NativeSession, thread_id: str
    ) -> tuple[str | None, ...]:
        preferred = self._thread_models.get(thread_id)
        primary = native.model or self.settings.model
        ordered = (
            *((preferred,) if preferred is not None else ()),
            primary,
            *self.settings.fallback_models,
        )
        candidates: list[str | None] = []
        for model in ordered:
            normalized = model.strip() if isinstance(model, str) else None
            if normalized == "":
                normalized = None
            if normalized not in candidates:
                candidates.append(normalized)
        return tuple(candidates or (None,))

    @staticmethod
    def _is_capacity_error(error: RuntimeError) -> bool:
        return "selected model is at capacity" in str(error).lower()

    @staticmethod
    def _is_archived_session_error(error: Exception) -> bool:
        message = str(error).lower()
        return "session" in message and "archived" in message

    @staticmethod
    async def _run_stream(turn: Any, on_text_delta: TextDeltaCallback) -> Any:
        from openai_codex._run import _collect_async_turn_result
        from openai_codex.generated.v2_all import AgentMessageDeltaNotification

        async def observed_stream():
            async for event in turn.stream():
                payload = event.payload
                if (
                    isinstance(payload, AgentMessageDeltaNotification)
                    and payload.turn_id == turn.id
                    and payload.delta
                ):
                    await on_text_delta(payload.delta)
                yield event

        return await _collect_async_turn_result(observed_stream(), turn_id=turn.id)

    async def cancel(self, job_id: str) -> None:
        turn = self._turns.get(job_id)
        if turn is not None:
            await turn.interrupt()

    async def health_check(self) -> HealthStatus:
        try:
            client = await self._get_client()
            account = await client.account()
        except Exception as exc:  # noqa: BLE001 - health checks must report SDK failures
            return HealthStatus(False, f"{type(exc).__name__}: {exc}")
        return HealthStatus(True, f"Codex SDK ready; account={bool(account)}")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None
        self._threads.clear()
        self._thread_models.clear()
        self._turns.clear()
