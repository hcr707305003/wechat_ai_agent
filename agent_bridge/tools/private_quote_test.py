"""Manual smoke test for native WeChat quoting in a private chat.

This intentionally bypasses Agent Bridge's production sender.  It exercises the
native wechatauto quote flow against one explicitly supplied private target so
we can validate the installed WeChat/UIA version before wiring quote delivery
into queued Agent replies.

Example (run while the bridge is stopped):
    .venv\\Scripts\\python.exe -m agent_bridge.tools.private_quote_test \\
        --target "工藤新一" --text "这是私聊引用测试"
"""

from __future__ import annotations

import argparse


def validate_private_target(target: str) -> str:
    value = target.strip()
    if not value:
        raise ValueError("必须提供私聊目标")
    if value.lower().endswith("@chatroom"):
        raise ValueError("该测试只允许私聊目标，不能传入群聊 ID")
    return value


def run_private_quote(target: str, text: str, *, verify: bool = True):
    """Quote the latest message in one private chat and send ``text``."""
    target = validate_private_target(target)
    text = text.strip()
    if not text:
        raise ValueError("必须提供引用后的回复内容")

    # quick_reply performs: open private chat -> locate latest message ->
    # choose WeChat's native Reply/Quote action -> type -> send.
    from wechatauto import quick_reply

    return quick_reply(text, who=target, verify=verify)


def main() -> int:
    parser = argparse.ArgumentParser(description="测试微信私聊原生引用发送")
    parser.add_argument("--target", required=True, help="私聊联系人名称或 wxid")
    parser.add_argument("--text", required=True, help="引用后的回复内容")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="不等待微信数据库确认（仅用于排查）",
    )
    args = parser.parse_args()
    try:
        response = run_private_quote(
            args.target,
            args.text,
            verify=not args.no_verify,
        )
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    print(response)
    return 0 if bool(response) else 1


if __name__ == "__main__":
    raise SystemExit(main())
