from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from agent_bridge.config import AppConfig, load_config


class ConfigDocument:
    def __init__(self, path: str | Path, data: dict[str, Any]) -> None:
        self.path = Path(path).resolve()
        self._data = deepcopy(data)

    @classmethod
    def load(cls, path: str | Path) -> ConfigDocument:
        config_path = Path(path).resolve()
        with config_path.open("r", encoding="utf-8") as handle:
            value = yaml.safe_load(handle) or {}
        if not isinstance(value, dict):
            raise TypeError("Configuration root must be a mapping")
        return cls(config_path, value)

    @property
    def data(self) -> dict[str, Any]:
        return deepcopy(self._data)

    def value(self, dotted_path: str, default: Any = None) -> Any:
        current: Any = self._data
        for part in dotted_path.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return deepcopy(current)

    def set_value(self, dotted_path: str, value: Any) -> None:
        parts = dotted_path.split(".")
        if not all(parts):
            raise ValueError("Configuration path cannot be empty")
        current = self._data
        for part in parts[:-1]:
            child = current.get(part)
            if not isinstance(child, dict):
                child = {}
                current[part] = child
            current = child
        current[parts[-1]] = deepcopy(value)

    def apply_defaults(self, defaults: dict[str, Any]) -> None:
        self._merge_missing(self._data, defaults)

    @classmethod
    def _merge_missing(cls, target: dict[str, Any], defaults: dict[str, Any]) -> None:
        for key, value in defaults.items():
            if key not in target:
                target[key] = deepcopy(value)
            elif isinstance(target[key], dict) and isinstance(value, dict):
                cls._merge_missing(target[key], value)

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self._data,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )

    def replace_from_yaml(self, text: str) -> None:
        value = yaml.safe_load(text) or {}
        if not isinstance(value, dict):
            raise TypeError("Configuration root must be a mapping")
        self._data = value

    def save(
        self,
        validator: Callable[[str | Path], AppConfig] = load_config,
    ) -> AppConfig:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(self.to_yaml())
                handle.flush()
                os.fsync(handle.fileno())
            validated = validator(temporary)
            if self.path.exists():
                shutil.copyfile(
                    self.path, self.path.with_suffix(self.path.suffix + ".bak")
                )
            os.replace(temporary, self.path)
            return validated
        finally:
            if temporary.exists():
                temporary.unlink()

    def validate(self) -> AppConfig:
        """Resolve current edits beside the real config without replacing it."""
        fd, name = tempfile.mkstemp(
            prefix=f".{self.path.name}.validate.", suffix=".tmp", dir=self.path.parent,
        )
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self.to_yaml())
            return load_config(temporary)
        finally:
            temporary.unlink(missing_ok=True)
