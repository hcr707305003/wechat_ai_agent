from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_bridge.config import AppConfig, load_config


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    suggestion: str = ""


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
        for provider, agent in config.agents.items():
            if agent.enabled:
                dependencies["Codex SDK" if provider == "codex" else "Claude SDK"] = (
                    "openai_codex" if provider == "codex" else "claude_agent_sdk"
                )
        for label, module in dependencies.items():
            remaining.append(
                CheckTask(
                    label,
                    lambda label=label, module=module: _dependency_check(label, module),
                )
            )
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


def run_manager_checks(config_path: str | Path) -> list[CheckResult]:
    session = ManagerCheckSession(config_path)
    # The first task appends configuration-dependent tasks to this same list.
    return [task.run() for task in session.tasks]
