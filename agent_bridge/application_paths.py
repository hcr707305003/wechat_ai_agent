from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ApplicationPaths:
    resource_root: Path
    user_root: Path
    frozen: bool
    executable_root: Path | None = None

    @classmethod
    def discover(
        cls,
        *,
        resource_root: str | Path | None = None,
        user_root: str | Path | None = None,
        frozen: bool | None = None,
    ) -> ApplicationPaths:
        is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        if resource_root is None:
            if is_frozen:
                resource_root = getattr(sys, "_MEIPASS", Path(sys.executable).parent)
            else:
                resource_root = Path(__file__).resolve().parents[1]
        if user_root is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                raise RuntimeError("LOCALAPPDATA is unavailable")
            user_root = Path(local_app_data) / "AgentBridge"
        return cls(
            Path(resource_root).resolve(),
            Path(user_root).resolve(),
            is_frozen,
            Path(sys.executable).resolve().parent
            if is_frozen
            else Path(resource_root).resolve(),
        )

    @classmethod
    def from_roots(
        cls, resource_root: str | Path, user_root: str | Path
    ) -> ApplicationPaths:
        return cls(Path(resource_root).resolve(), Path(user_root).resolve(), False)

    @property
    def config_template(self) -> Path:
        return self.resource_root / "config.example.yaml"

    @property
    def binary_root(self) -> Path:
        return (self.executable_root or self.resource_root).resolve()

    @property
    def config_file(self) -> Path:
        return self.user_root / "config.yaml"

    @property
    def data_dir(self) -> Path:
        return self.user_root / "data"

    @property
    def logs_dir(self) -> Path:
        return self.user_root / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.user_root / "cache"

    @property
    def avatars_dir(self) -> Path:
        return self.user_root / "avatars"

    @property
    def workbench_log(self) -> Path:
        return self.logs_dir / "workbench.log"

    @property
    def manager_log(self) -> Path:
        return self.logs_dir / "manager.log"

    @property
    def default_config_file(self) -> Path:
        if self.frozen:
            return self.config_file
        return self.resource_root / "config.yaml"

    def initialize_user_data(self) -> bool:
        for directory in (
            self.user_root,
            self.data_dir,
            self.logs_dir,
            self.cache_dir,
            self.avatars_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        if self.config_file.exists():
            return False
        if not self.config_template.is_file():
            raise FileNotFoundError(
                f"Configuration template is missing: {self.config_template}"
            )
        shutil.copyfile(self.config_template, self.config_file)
        return True
