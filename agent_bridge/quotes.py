from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree

from agent_bridge.models import ContentType, ReplyReference

QUOTE_METADATA_KEY = "bridge_quote"
QUOTE_HISTORY_LIMIT = 30

_REFERENCE_TYPES = {
    "1": ContentType.TEXT,
    "3": ContentType.IMAGE,
    "34": ContentType.VOICE,
    "43": ContentType.VIDEO,
    "47": ContentType.IMAGE,
    "49": ContentType.FILE,
}


def reply_reference_metadata(reference: ReplyReference) -> dict[str, Any]:
    """Serialize a reply target into stable, UI-neutral message metadata."""
    return {
        "message_id": reference.message_id,
        "conversation_id": reference.conversation_id,
        "sender_id": reference.sender_id,
        "sender_name": reference.sender_name,
        "content": reference.content,
        "content_type": reference.content_type.value,
        "created_at": reference.created_at.isoformat(),
    }


def parse_wechat_quote_payload(payload: str) -> tuple[str, dict[str, Any] | None]:
    """Split a WeChat type-57 payload into reply text and quote metadata.

    WeChat stores native replies as an app-message XML document. Malformed or
    partially decoded XML must never leak into the companion timeline.
    """
    text = str(payload or "")
    if "<refermsg" not in text.lower():
        return text, None

    try:
        root = ElementTree.fromstring(text.strip())
    except (ElementTree.ParseError, ValueError):
        return _parse_malformed_quote_payload(text)

    refer = root.find(".//refermsg")
    if refer is None:
        return _read_xml_text(root, ".//appmsg/title") or "[引用消息]", None

    reply_text = _read_xml_text(root, ".//appmsg/title") or "[引用消息]"
    quote_content = _read_element_text(refer, "content") or "[原消息]"
    quote_type = _REFERENCE_TYPES.get(
        _read_element_text(refer, "type"), ContentType.UNKNOWN
    )
    created_at = _reference_created_at(_read_element_text(refer, "createtime"))
    metadata: dict[str, Any] = {
        "message_id": None,
        "conversation_id": None,
        "server_id": _read_element_text(refer, "svrid") or None,
        "sender_id": _read_element_text(refer, "fromusr") or None,
        "sender_name": (
            _read_element_text(refer, "displayname")
            or _read_element_text(refer, "fromusr")
            or "原消息"
        ),
        "content": quote_content,
        "content_type": quote_type.value,
        "created_at": created_at,
    }
    return reply_text, metadata


def _parse_malformed_quote_payload(payload: str) -> tuple[str, dict[str, Any] | None]:
    reply_text = _tag_text(payload, "title") or "[引用消息]"
    refer_match = re.search(
        r"<refermsg(?:\s[^>]*)?>(.*?)</refermsg\s*>",
        payload,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if refer_match is None:
        return reply_text, None
    refer = refer_match.group(1)
    quote_type = _REFERENCE_TYPES.get(_tag_text(refer, "type"), ContentType.UNKNOWN)
    return reply_text, {
        "message_id": None,
        "conversation_id": None,
        "server_id": _tag_text(refer, "svrid") or None,
        "sender_id": _tag_text(refer, "fromusr") or None,
        "sender_name": (
            _tag_text(refer, "displayname")
            or _tag_text(refer, "fromusr")
            or "原消息"
        ),
        "content": _tag_text(refer, "content") or "[原消息]",
        "content_type": quote_type.value,
        "created_at": _reference_created_at(_tag_text(refer, "createtime")),
    }


def _read_xml_text(root: ElementTree.Element, path: str) -> str:
    element = root.find(path)
    return _element_value(element)


def _read_element_text(root: ElementTree.Element, tag: str) -> str:
    return _element_value(root.find(tag))


def _element_value(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _tag_text(payload: str, tag: str) -> str:
    match = re.search(
        rf"<{re.escape(tag)}(?:\s[^>]*)?>(.*?)</{re.escape(tag)}\s*>",
        payload,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return ""
    value = re.sub(r"^<!\[CDATA\[|\]\]>$", "", match.group(1).strip())
    return html.unescape(value).strip()


def _reference_created_at(value: str) -> str | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None
