from __future__ import annotations

import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

from agent_bridge.models import Attachment

_DIRECTIVE = re.compile(
    r"<agent_bridge_image>(.*?)</agent_bridge_image>",
    re.DOTALL | re.IGNORECASE,
)
_ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_DEFAULT_MAX_BYTES = 20 * 1024 * 1024
_DEFAULT_MAX_IMAGES = 3


@dataclass(slots=True, frozen=True)
class ImageDirectiveResult:
    text: str
    attachments: tuple[Attachment, ...] = ()
    errors: tuple[str, ...] = ()


def image_delivery_instruction() -> str:
    return (
        "\n\nAgent Bridge image delivery is enabled for this conversation. "
        "When an existing image inside the current working directory materially helps "
        "the reply, you may append up to three directives in the exact form "
        '<agent_bridge_image>{"path":"relative/path.png","alt":"short description"}'
        "</agent_bridge_image>. Do not use URLs or paths outside the current working "
        "directory. The directive is a transport instruction and will be removed from "
        "the visible text reply."
    )


def resolve_image_directives(
    text: str,
    working_directory: str | Path,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    max_images: int = _DEFAULT_MAX_IMAGES,
) -> ImageDirectiveResult:
    root = Path(working_directory).resolve()
    attachments: list[Attachment] = []
    errors: list[str] = []
    matches = tuple(_DIRECTIVE.finditer(text))
    for index, match in enumerate(matches):
        if index >= max_images:
            if not any("最多发送" in item for item in errors):
                errors.append(f"每轮最多发送 {max_images} 张图片，已忽略其余请求。")
            continue
        try:
            payload = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            errors.append("图片发送指令格式无效，已忽略。")
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("path"), str):
            errors.append("图片发送指令缺少有效 path，已忽略。")
            continue
        attachment, error = _resolve_attachment(
            root,
            payload["path"],
            str(payload.get("alt") or "").strip(),
            max_bytes,
        )
        if error:
            errors.append(error)
        elif attachment is not None:
            attachments.append(attachment)
    cleaned = _DIRECTIVE.sub("", text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return ImageDirectiveResult(cleaned, tuple(attachments), tuple(errors))


def _resolve_attachment(
    root: Path, value: str, alt: str, max_bytes: int
) -> tuple[Attachment | None, str | None]:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None, f"图片不存在：{value}"
    if resolved != root and not resolved.is_relative_to(root):
        return None, f"图片位于 Session 工作目录外，已拒绝：{value}"
    if not resolved.is_file():
        return None, f"图片路径不是普通文件：{value}"
    extension = resolved.suffix.lower()
    if extension not in _ALLOWED_EXTENSIONS:
        return None, f"不支持的图片格式：{extension or '无扩展名'}"
    try:
        size = resolved.stat().st_size
    except OSError:
        return None, f"无法读取图片：{value}"
    if size > max_bytes:
        return None, f"图片大小超过 {max_bytes // (1024 * 1024) or max_bytes} MiB：{resolved.name}"
    mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    metadata = {"alt": alt} if alt else {}
    return (
        Attachment(
            kind="image",
            name=resolved.name,
            path=str(resolved),
            mime_type=mime_type,
            metadata=metadata,
        ),
        None,
    )
