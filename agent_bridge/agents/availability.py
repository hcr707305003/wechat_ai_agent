"""Read-only local dependency checks; never start an Agent or test credentials."""
from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_bridge.config import AppConfig

PROVIDERS = ("codex", "claude")


@dataclass(frozen=True, slots=True)
class AgentAvailability:
    available: bool
    reason: str


def probe_agent(provider: str) -> AgentAvailability:
    """Check the runtime used by our adapters, including SDK-bundled executables."""
    module = {"codex": "openai_codex", "claude": "claude_agent_sdk"}.get(provider)
    if module is None:
        return AgentAvailability(False, "不支持的 Agent")
    try:
        if importlib.util.find_spec(module) is None:
            return AgentAvailability(False, f"未安装 {module}")
        sdk = importlib.import_module(module)
        if provider == "codex":
            from codex_cli_bin import bundled_codex_path

            runtime = Path(bundled_codex_path())
        else:
            name = "claude.exe" if os.name == "nt" else "claude"
            runtime = Path(sdk.__file__).parent / "_bundled" / name
            if not runtime.is_file():
                candidates = [shutil.which(name), Path.home() / ".local/bin" / name]
                if os.name != "nt":
                    candidates.extend([
                        Path.home() / ".npm-global/bin/claude",
                        Path("/usr/local/bin/claude"),
                        Path.home() / "node_modules/.bin/claude",
                        Path.home() / ".yarn/bin/claude",
                        Path.home() / ".claude/local/claude",
                    ])
                runtime = next((Path(p) for p in candidates if p and Path(p).is_file()
                                and (os.name != "nt" or Path(p).suffix.lower() == ".exe")), runtime)
        if not runtime.is_file():
            return AgentAvailability(False, f"未找到 {provider} 运行程序")
        return AgentAvailability(True, "本地依赖就绪（登录、网络请通过聊天调试验证）")
    except Exception as error:  # noqa: BLE001 - isolate broken optional SDK imports
        return AgentAvailability(False, f"本地依赖不可用：{error}")


def configured_availability(config: AppConfig) -> dict[str, AgentAvailability]:
    return {
        provider: probe_agent(provider)
        if provider in config.agents and config.agents[provider].enabled
        else AgentAvailability(False, "配置未启用")
        for provider in PROVIDERS
    }


def effective_default(config: AppConfig, states: dict[str, AgentAvailability]) -> str:
    preferred = config.runtime.default_provider
    if states[preferred].available:
        return preferred
    for provider, state in states.items():
        if state.available:
            return provider
    raise RuntimeError("Codex 和 Claude 均不可用，请至少启用并安装其中一个 Agent。")
