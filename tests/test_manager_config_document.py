from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.manager.config_document import ConfigDocument


def test_config_document_updates_known_path_and_preserves_unknown_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "runtime:\n  concurrency: 3\nfuture_feature:\n  enabled: true\n",
        encoding="utf-8",
    )
    document = ConfigDocument.load(path)

    document.set_value("runtime.concurrency", 5)

    assert document.value("runtime.concurrency") == 5
    assert document.value("future_feature.enabled") is True


def test_config_document_advanced_yaml_requires_mapping(tmp_path: Path) -> None:
    document = ConfigDocument(tmp_path / "config.yaml", {})

    with pytest.raises(TypeError, match="root must be a mapping"):
        document.replace_from_yaml("- item\n")


def test_config_document_applies_nested_defaults_without_overwriting_values(
    tmp_path: Path,
) -> None:
    document = ConfigDocument(tmp_path / "config.yaml", {"runtime": {"concurrency": 8}})

    document.apply_defaults(
        {"runtime": {"concurrency": 3, "timeout_seconds": 600}, "future": True}
    )

    assert document.value("runtime.concurrency") == 8
    assert document.value("runtime.timeout_seconds") == 600
    assert document.value("future") is True


def test_config_document_invalid_save_keeps_original(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("original: true\n", encoding="utf-8")
    document = ConfigDocument(path, {"replacement": True})

    def reject(_path):
        raise ValueError("invalid setting")

    with pytest.raises(ValueError, match="invalid setting"):
        document.save(reject)

    assert path.read_text(encoding="utf-8") == "original: true\n"
    assert not path.with_suffix(".yaml.bak").exists()


def test_config_document_atomic_save_creates_backup(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("original: true\n", encoding="utf-8")
    document = ConfigDocument(path, {"replacement": True, "unknown": {"x": 1}})
    validated = object()

    result = document.save(lambda _path: validated)

    assert result is validated
    assert (
        path.with_suffix(".yaml.bak").read_text(encoding="utf-8") == "original: true\n"
    )
    saved = ConfigDocument.load(path)
    assert saved.value("replacement") is True
    assert saved.value("unknown.x") == 1
