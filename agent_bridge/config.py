from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from agent_bridge.agents.claude import ClaudeAdapterSettings
from agent_bridge.agents.codex import CodexAdapterSettings
from agent_bridge.channels.wechat import (
    WeChatChannelSettings,
    WeChatCompanionSettings,
    WeChatSenderSettings,
)
from agent_bridge.models import ConversationType, SessionBindingConfig
from agent_bridge.senders.wechat_hook_driver import WeChatHookQuoteSettings
from agent_bridge.webhooks import parse_webhooks


@dataclass(slots=True, frozen=True)
class RuntimeConfig:
    database: str
    default_provider: str
    default_working_directory: str
    allowed_roots: tuple[str, ...]
    concurrency: int = 3
    timeout_seconds: float = 600.0
    recent_messages: int = 40
    max_reply_chars: int = 1800
    acknowledgement: str = "已接收，正在处理。"


@dataclass(slots=True, frozen=True)
class AgentConfig:
    enabled: bool = True
    codex: CodexAdapterSettings | None = None
    claude: ClaudeAdapterSettings | None = None


@dataclass(slots=True, frozen=True)
class AppConfig:
    runtime: RuntimeConfig
    agents: dict[str, AgentConfig]
    wechat: WeChatChannelSettings
    wechat_enabled: bool = True


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise TypeError("Configuration root must be a mapping")
    base = config_path.parent
    runtime_raw = _mapping(raw.get("runtime"), "runtime")
    default_working_directory = _resolve_path(
        base, runtime_raw.get("default_working_directory", ".")
    )
    roots_raw = runtime_raw.get("allowed_roots", [default_working_directory])
    if not isinstance(roots_raw, list) or not roots_raw:
        raise ValueError("runtime.allowed_roots must be a non-empty list")
    runtime = RuntimeConfig(
        database=_resolve_path(base, runtime_raw.get("database", ".data/bridge.db")),
        default_provider=str(runtime_raw.get("default_provider", "codex")).lower(),
        default_working_directory=default_working_directory,
        allowed_roots=tuple(_resolve_path(base, item) for item in roots_raw),
        concurrency=int(runtime_raw.get("concurrency", 3)),
        timeout_seconds=float(runtime_raw.get("timeout_seconds", 600)),
        recent_messages=int(runtime_raw.get("recent_messages", 40)),
        max_reply_chars=int(runtime_raw.get("max_reply_chars", 1800)),
        acknowledgement=str(runtime_raw.get("acknowledgement", "已接收，正在处理。")),
    )
    if runtime.concurrency < 1 or runtime.timeout_seconds <= 0:
        raise ValueError("Runtime concurrency and timeout must be positive")

    agents_raw = _mapping(raw.get("agents", {}), "agents")
    agents: dict[str, AgentConfig] = {}
    for provider, value in agents_raw.items():
        settings = _mapping(value, f"agents.{provider}")
        enabled = bool(settings.pop("enabled", True))
        provider = str(provider).lower()
        if provider == "codex":
            codex_settings = _known(settings, CodexAdapterSettings)
            if "fallback_models" in codex_settings:
                codex_settings["fallback_models"] = tuple(
                    _string_list(codex_settings["fallback_models"])
                )
            agents[provider] = AgentConfig(
                enabled=enabled,
                codex=CodexAdapterSettings(**codex_settings),
            )
        elif provider == "claude":
            agents[provider] = AgentConfig(
                enabled=enabled,
                claude=ClaudeAdapterSettings(**_known(settings, ClaudeAdapterSettings)),
            )
        else:
            raise ValueError(f"Unsupported configured agent provider: {provider}")
    if runtime.default_provider not in {"codex", "claude"}:
        raise ValueError("runtime.default_provider must be codex or claude")

    channels_raw = _mapping(raw.get("channels", {}), "channels")
    wechat_raw = _mapping(channels_raw.get("wechat", {}), "channels.wechat")
    wechat_enabled = bool(wechat_raw.pop("enabled", True))
    companion_raw = _mapping(
        wechat_raw.get("companion", {}), "channels.wechat.companion"
    )
    sender_raw = _mapping(wechat_raw.get("sender", {}), "channels.wechat.sender")
    hook_quote_raw = _mapping(
        wechat_raw.get("hook_quote", {}), "channels.wechat.hook_quote"
    )
    if "verify_sends" not in sender_raw and "verify_sends" in wechat_raw:
        sender_raw["verify_sends"] = bool(wechat_raw["verify_sends"])
    wechat = WeChatChannelSettings(
        account=_optional_string(wechat_raw.get("account")),
        allowed_private_ids=tuple(
            _string_list(wechat_raw.get("allowed_private_ids", []))
        ),
        allowed_group_ids=tuple(_string_list(wechat_raw.get("allowed_group_ids", []))),
        group_controllers=frozenset(_string_list(wechat_raw.get("group_controllers", []))),
        group_prefixes=tuple(_string_list(wechat_raw.get("group_prefixes", ["/ai"]))),
        group_prefixes_rule=str(
            wechat_raw.get("group_prefixes_rule", "prefix")
        ).strip().lower(),
        reply_prefix=_strict_string(
            wechat_raw.get("reply_prefix", ""),
            "channels.wechat.reply_prefix",
        ),
        quote_private_replies=_strict_bool(
            wechat_raw.get("quote_private_replies", False),
            "channels.wechat.quote_private_replies",
        ),
        quote_group_replies=_strict_bool(
            wechat_raw.get("quote_group_replies", False),
            "channels.wechat.quote_group_replies",
        ),
        message_batch_window_seconds=float(
            wechat_raw.get("message_batch_window_seconds", 1.5)
        ),
        session_bindings=_session_bindings(wechat_raw.get("session_bindings", [])),
        webhooks=parse_webhooks(wechat_raw.get("webhooks", [])),
        listener_interval=float(wechat_raw.get("listener_interval", 0.25)),
        hook_quote=WeChatHookQuoteSettings(
            **_known(hook_quote_raw, WeChatHookQuoteSettings)
        ),
        sender=WeChatSenderSettings(**_known(sender_raw, WeChatSenderSettings)),
        companion=WeChatCompanionSettings(
            **_known(companion_raw, WeChatCompanionSettings)
        ),
    )
    return AppConfig(runtime, agents, wechat, wechat_enabled)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a mapping")
    return dict(value)


def _resolve_path(base: Path, value: Any) -> str:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path.resolve())


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("Expected a list of strings")
    return [str(item) for item in value]


def _optional_string(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _strict_string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _session_bindings(value: Any) -> tuple[SessionBindingConfig, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError("channels.wechat.session_bindings must be a list")
    bindings: list[SessionBindingConfig] = []
    seen_targets: set[tuple[str | None, str]] = set()
    allowed = {"conversation_id", "conversation_type", "provider", "session_id"}
    for index, item in enumerate(value):
        entry = _mapping(item, f"channels.wechat.session_bindings[{index}]")
        unknown = set(entry) - allowed
        if unknown:
            raise ValueError(
                "Unknown session binding keys: " + ", ".join(sorted(unknown))
            )
        conversation_id = str(entry.get("conversation_id", "")).strip()
        provider = str(entry.get("provider", "")).strip().lower()
        session_id = str(entry.get("session_id", "")).strip()
        if not conversation_id or not provider or not session_id:
            raise ValueError(
                "Each session binding requires conversation_id, provider, and session_id"
            )
        if provider not in {"codex", "claude"}:
            raise ValueError(f"Unsupported session binding agent: {provider}")
        raw_type = entry.get("conversation_type")
        conversation_type: ConversationType | None = None
        if raw_type not in (None, ""):
            normalized_type = str(raw_type).strip().lower()
            try:
                conversation_type = ConversationType(normalized_type)
            except ValueError as exc:
                raise ValueError(
                    "Session binding conversation_type must be private or group"
                ) from exc
        if provider == "claude":
            try:
                UUID(session_id)
            except ValueError as exc:
                raise ValueError(
                    "Claude session binding session_id must be a UUID"
                ) from exc
        target_key = (
            conversation_type.value if conversation_type is not None else None,
            conversation_id,
        )
        if target_key in seen_targets:
            raise ValueError(
                "Duplicate session binding for conversation: " + conversation_id
            )
        seen_targets.add(target_key)
        bindings.append(
            SessionBindingConfig(
                conversation_id=conversation_id,
                provider=provider,
                session_id=session_id,
                conversation_type=conversation_type,
            )
        )
    return tuple(bindings)


def _known(values: dict[str, Any], model: type) -> dict[str, Any]:
    names = set(model.__dataclass_fields__)
    unknown = set(values) - names
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    return values
