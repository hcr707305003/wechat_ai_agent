from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any


def to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return to_plain(value.model_dump(by_alias=True, mode="json"))
    if is_dataclass(value):
        return to_plain(asdict(value))
    if hasattr(value, "__dict__"):
        return to_plain(vars(value))
    return repr(value)


def class_key(value: Any) -> str:
    return type(value).__name__.replace("_", "").lower()

