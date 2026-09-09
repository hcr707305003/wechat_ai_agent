from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_bridge.agents.availability import PROVIDERS, AgentAvailability, probe_agent
from agent_bridge.config import AppConfig, load_config


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    suggestion: str = ""
    required: bool = True


@dataclass(frozen=True, slots=True)
class CheckTask:
    name: str
    check: Callable[[], CheckResult]

    def run(self) -> CheckResult:
        try:
            return self.check()
        except Exception as error:  # noqa: BLE001 - independent diagnostic boundary
            return CheckResult(self.name, False, str(error), "请修复此项后重新检查。")


def _directory_check(name: str, directory: str, suggestion: str) -> CheckResult:
    exists = Path(directory).is_dir()
    return CheckResult(name, exists, directory, "" if exists else suggestion)


def _dependency_check(name: str, module: str) -> CheckResult:
    available = importlib.util.find_spec(module) is not None
    return CheckResult(
        name,
        available,
        module,
        "" if available else "请重新安装完整版本的 Agent Bridge。",
    )


class ManagerCheckSession:
    """Execute tasks serially; configuration expands the remaining task list.

    The UI reads tasks only after the active task's future has completed.
    Constructing a session performs no disk access or dependency detection.
    """

    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path)
        self.config: AppConfig | None = None
        self.agent_states: dict[str, AgentAvailability] = {}
        self.tasks = [CheckTask("配置文件", self._load_configuration)]

    def _load_configuration(self) -> CheckResult:
        try:
            config = load_config(self.config_path)
        except (OSError, TypeError, ValueError) as error:
            return CheckResult(
                "配置文件",
                False,
                str(error),
                "请修正配置并保存后重新检查；依赖配置的检查未执行。",
            )

        self.config = config
        directory = str(config.runtime.default_working_directory)
        remaining = [
            CheckTask(
                "默认工作目录",
                lambda: _directory_check(
                    "默认工作目录", directory, "请选择已存在的工作目录。"
                ),
            )
        ]
        for root in config.runtime.allowed_roots:
            remaining.append(
                CheckTask(
                    "允许目录",
                    lambda root=root: _directory_check(
                        "允许目录",
                        root,
                        "请删除无效目录或创建该目录。",
                    ),
                )
            )
        dependencies = {
            "微信组件": "wechatauto",
            "PySide6": "PySide6",
            "qasync": "qasync",
        }
        for label, module in dependencies.items():
            remaining.append(
                CheckTask(
                    label,
                    lambda label=label, module=module: _dependency_check(label, module),
                )
            )
        for provider in PROVIDERS:
            remaining.append(CheckTask(
                f"{provider.title()} Agent",
                lambda provider=provider: self._check_agent(provider),
            ))
        remaining.append(CheckTask("可用 Agent", self._check_any_agent))
        has_conversation = bool(
            config.wechat.allowed_private_ids or config.wechat.allowed_group_ids
        )
        remaining.append(
            CheckTask(
                "微信白名单",
                lambda: CheckResult(
                    "微信白名单",
                    has_conversation,
                    "已配置" if has_conversation else "未配置",
                    "" if has_conversation else "请至少添加一个私聊或群聊。",
                ),
            )
        )
        self.tasks.extend(remaining)
        return CheckResult("配置文件", True, str(self.config_path.resolve()))

    def _check_agent(self, provider: str) -> CheckResult:
        state = probe_agent(provider)
        self.agent_states[provider] = state
        agent = self.config.agents.get(provider)
        enabled = agent is not None and agent.enabled
        return CheckResult(
            f"{provider.title()} Agent", state.available and enabled,
            state.reason if enabled else "配置未启用",
            required=False,
        )

    def _check_any_agent(self) -> CheckResult:
        available = [p for p, s in self.agent_states.items()
                     if s.available and p in self.config.agents and self.config.agents[p].enabled]
        return CheckResult("可用 Agent", bool(available),
                           "、".join(available) if available else "Codex 和 Claude 均不可用",
                           "" if available else "请至少启用并安装其中一个 Agent。")


def run_manager_checks(config_path: str | Path) -> list[CheckResult]:
    session = ManagerCheckSession(config_path)
    # The first task appends configuration-dependent tasks to this same list.
    return [task.run() for task in session.tasks]
