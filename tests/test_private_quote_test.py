from __future__ import annotations

import sys
import types

import pytest

from agent_bridge.tools.private_quote_test import (
    run_private_quote,
    validate_private_target,
)


def test_private_quote_rejects_group_target() -> None:
    with pytest.raises(ValueError, match="只允许私聊"):
        validate_private_target("123456@chatroom")


def test_private_quote_rejects_empty_values() -> None:
    with pytest.raises(ValueError, match="必须提供私聊目标"):
        validate_private_target("  ")
    with pytest.raises(ValueError, match="必须提供引用后的回复内容"):
        run_private_quote("Friend", "  ")


def test_private_quote_calls_native_quick_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def quick_reply(text: str, *, who: str, verify: bool):
        calls.append((text, who, verify))
        return {"status": "成功"}

    monkeypatch.setitem(sys.modules, "wechatauto", types.SimpleNamespace(quick_reply=quick_reply))

    response = run_private_quote("  Friend  ", "  测试引用  ", verify=False)

    assert response["status"] == "成功"
    assert calls == [("测试引用", "Friend", False)]
