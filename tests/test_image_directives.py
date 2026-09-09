import json
from pathlib import Path

from agent_bridge.runtime.image_directives import resolve_image_directives


def test_resolves_image_directive_and_removes_it_from_reply(tmp_path: Path) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    result = resolve_image_directives(
        '处理好了。\n<agent_bridge_image>{"path":"result.png","alt":"结果图"}</agent_bridge_image>',
        tmp_path,
    )

    assert result.text == "处理好了。"
    assert len(result.attachments) == 1
    assert result.attachments[0].kind == "image"
    assert result.attachments[0].path == str(image.resolve())
    assert result.attachments[0].metadata == {"alt": "结果图"}
    assert result.errors == ()


def test_rejects_path_outside_session_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "private.png"
    outside.write_bytes(b"secret")

    result = resolve_image_directives(
        "<agent_bridge_image>"
        + json.dumps({"path": str(outside)})
        + "</agent_bridge_image>",
        workspace,
    )

    assert result.attachments == ()
    assert result.text == ""
    assert "工作目录外" in result.errors[0]


def test_rejects_symlink_that_resolves_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "private.png"
    outside.write_bytes(b"secret")
    link = workspace / "link.png"
    try:
        link.symlink_to(outside)
    except OSError:
        return

    result = resolve_image_directives(
        '<agent_bridge_image>{"path":"link.png"}</agent_bridge_image>', workspace
    )

    assert result.attachments == ()
    assert "工作目录外" in result.errors[0]


def test_rejects_unsupported_or_oversized_image(tmp_path: Path) -> None:
    text_file = tmp_path / "notes.txt"
    text_file.write_text("not image", encoding="utf-8")
    large = tmp_path / "large.png"
    large.write_bytes(b"x" * 11)
    reply = (
        '<agent_bridge_image>{"path":"notes.txt"}</agent_bridge_image>'
        '<agent_bridge_image>{"path":"large.png"}</agent_bridge_image>'
    )

    result = resolve_image_directives(reply, tmp_path, max_bytes=10)

    assert result.attachments == ()
    assert any("格式" in error for error in result.errors)
    assert any("大小" in error for error in result.errors)


def test_limits_each_agent_turn_to_three_images(tmp_path: Path) -> None:
    for index in range(4):
        (tmp_path / f"{index}.png").write_bytes(b"png")
    reply = "".join(
        f'<agent_bridge_image>{{"path":"{index}.png"}}</agent_bridge_image>'
        for index in range(4)
    )

    result = resolve_image_directives(reply, tmp_path)

    assert len(result.attachments) == 3
    assert any("最多发送 3 张" in error for error in result.errors)


def test_malformed_directive_is_removed_and_reported(tmp_path: Path) -> None:
    result = resolve_image_directives(
        "完成\n<agent_bridge_image>{bad json}</agent_bridge_image>", tmp_path
    )

    assert result.text == "完成"
    assert result.attachments == ()
    assert "格式无效" in result.errors[0]
