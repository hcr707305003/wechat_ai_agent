from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum


class CommandType(str, Enum):
    HELP = "help"
    SET_AGENT = "set_agent"
    ASK = "ask"
    SESSION_INFO = "session_info"
    SESSION_NEW = "session_new"
    SESSION_BIND = "session_bind"
    SESSION_HISTORY = "session_history"
    CANCEL = "cancel"


@dataclass(slots=True, frozen=True)
class Command:
    type: CommandType
    provider: str | None = None
    value: str | None = None


class CommandError(ValueError):
    pass


def parse_command(text: str) -> Command | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    try:
        parts = shlex.split(stripped)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    if not parts:
        return None
    name = parts[0].lower()
    if name == "/help" and len(parts) == 1:
        return Command(CommandType.HELP)
    if name == "/cancel" and len(parts) == 1:
        return Command(CommandType.CANCEL)
    if name == "/agent" and len(parts) == 2:
        return Command(CommandType.SET_AGENT, provider=parts[1].lower())
    if name == "/ask" and len(parts) >= 3:
        return Command(CommandType.ASK, provider=parts[1].lower(), value=" ".join(parts[2:]))
    if name == "/session" and len(parts) >= 2:
        action = parts[1].lower()
        if action == "info" and len(parts) == 2:
            return Command(CommandType.SESSION_INFO)
        if action == "new" and len(parts) == 3:
            return Command(CommandType.SESSION_NEW, provider=parts[2].lower())
        if action == "bind" and len(parts) == 4:
            return Command(CommandType.SESSION_BIND, provider=parts[2].lower(), value=parts[3])
        if action == "history" and len(parts) == 3:
            return Command(CommandType.SESSION_HISTORY, provider=parts[2].lower())
    raise CommandError("未知命令或参数错误，请发送 /help 查看用法")


HELP_TEXT = """可用命令：
/agent <codex|claude> - 切换当前 Agent
/ask <agent> <内容> - 本条消息临时使用指定 Agent
/session info - 查看统一 session 状态
/session new <agent> - 为 Agent 新建原生 session
/session bind <agent> <session-id> - 绑定已有原生 session
/session history <agent> - 查看原生 session 历史
/cancel - 取消当前任务
/help - 显示帮助"""

