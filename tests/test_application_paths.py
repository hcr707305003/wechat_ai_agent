from __future__ import annotations

from pathlib import Path

from agent_bridge.application_paths import ApplicationPaths


def test_application_paths_create_user_directories_and_config(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "config.example.yaml").write_text("runtime: {}\n", encoding="utf-8")
    user_root = tmp_path / "local" / "AgentBridge"

    paths = ApplicationPaths.from_roots(resources, user_root)
    created = paths.initialize_user_data()

    assert created is True
    assert paths.config_file.read_text(encoding="utf-8") == "runtime: {}\n"
    assert paths.data_dir.is_dir()
    assert paths.logs_dir.is_dir()
    assert paths.cache_dir.is_dir()
    assert paths.avatars_dir.is_dir()


def test_application_paths_do_not_overwrite_existing_config(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "config.example.yaml").write_text("runtime: {}\n", encoding="utf-8")
    user_root = tmp_path / "AgentBridge"
    user_root.mkdir()
    config = user_root / "config.yaml"
    config.write_text("custom: true\n", encoding="utf-8")

    created = ApplicationPaths.from_roots(resources, user_root).initialize_user_data()

    assert created is False
    assert config.read_text(encoding="utf-8") == "custom: true\n"


def test_application_paths_discover_frozen_resources(
    tmp_path: Path, monkeypatch
) -> None:
    bundle = tmp_path / "bundle"
    local = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    paths = ApplicationPaths.discover(resource_root=bundle, frozen=True)

    assert paths.resource_root == bundle.resolve()
    assert paths.user_root == (local / "AgentBridge").resolve()
    assert paths.binary_root == Path(__import__("sys").executable).resolve().parent
    assert paths.default_config_file == paths.config_file


def test_application_paths_source_default_config_is_project_config(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    paths = ApplicationPaths.discover(
        resource_root=project,
        user_root=tmp_path / "user",
        frozen=False,
    )

    assert paths.default_config_file == (project / "config.yaml").resolve()
