from __future__ import annotations

from pathlib import Path

from agent_bridge.models import (
    NativeSession,
    SessionBindingConfig,
    UnifiedMessage,
    UnifiedSession,
    new_id,
)
from agent_bridge.sessions.repository import SQLiteRepository


class SessionResolver:
    def __init__(
        self,
        repository: SQLiteRepository,
        default_provider: str,
        default_working_directory: str,
        allowed_roots: tuple[str, ...],
        session_bindings: tuple[SessionBindingConfig, ...] = (),
    ) -> None:
        self.repository = repository
        self.default_provider = default_provider
        self.default_working_directory = self._validate_path(
            default_working_directory, allowed_roots
        )
        self.allowed_roots = tuple(str(Path(root).resolve()) for root in allowed_roots)
        self._session_bindings: dict[
            tuple[str | None, str], SessionBindingConfig
        ] = {}
        self._applied_bindings: set[tuple[str, str, str]] = set()
        self.configure_session_bindings(session_bindings)

    def configure_session_bindings(
        self, session_bindings: tuple[SessionBindingConfig, ...]
    ) -> None:
        bindings: dict[tuple[str | None, str], SessionBindingConfig] = {}
        for binding in session_bindings:
            key = (
                binding.conversation_type.value
                if binding.conversation_type is not None
                else None,
                binding.conversation_id,
            )
            if key in bindings:
                raise ValueError(
                    "Duplicate session binding for conversation: "
                    + binding.conversation_id
                )
            bindings[key] = binding
        self._session_bindings = bindings
        self._applied_bindings.clear()

    def resolve(self, message: UnifiedMessage) -> tuple[UnifiedSession, bool]:
        binding = self._binding_for(message)
        existing = self.repository.find_session_for_message(message)
        if existing is not None:
            self._validate_path(existing.working_directory, self.allowed_roots)
            if binding is not None:
                self._apply_binding(existing, message, binding)
            return existing, False
        provider = binding.provider if binding is not None else self.default_provider
        session = self.repository.create_session(
            provider, self.default_working_directory
        )
        self.repository.bind_channel(message, session.id)
        if binding is not None:
            self._apply_binding(session, message, binding)
        return session, True

    def _binding_for(self, message: UnifiedMessage) -> SessionBindingConfig | None:
        exact = self._session_bindings.get(
            (message.conversation_type.value, message.conversation_id)
        )
        return exact or self._session_bindings.get((None, message.conversation_id))

    def _apply_binding(
        self,
        session: UnifiedSession,
        message: UnifiedMessage,
        binding: SessionBindingConfig,
    ) -> None:
        key = (message.channel, message.channel_account_id, message.conversation_id)
        if key in self._applied_bindings:
            return
        native = next(
            (
                item
                for item in self.repository.list_native_sessions(
                    session.id, binding.provider
                )
                if item.native_session_id == binding.session_id
            ),
            None,
        )
        if native is None:
            native = NativeSession(
                id=new_id("native"),
                unified_session_id=session.id,
                provider=binding.provider,
                native_session_id=binding.session_id,
                working_directory=session.working_directory,
                context_initialized=True,
            )
            self.repository.add_native_session(native)
        elif not native.is_active:
            self.repository.activate_native_session(native.id)
        if session.current_provider != binding.provider:
            self.repository.set_current_provider(session.id, binding.provider)
        self._applied_bindings.add(key)

    @staticmethod
    def _validate_path(path: str, allowed_roots: tuple[str, ...]) -> str:
        resolved = Path(path).resolve()
        roots = tuple(Path(root).resolve() for root in allowed_roots)
        if not roots:
            raise ValueError("At least one allowed working-directory root is required")
        if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
            raise ValueError(f"Working directory is outside allowed roots: {resolved}")
        if not resolved.is_dir():
            raise ValueError(f"Working directory does not exist: {resolved}")
        return str(resolved)
