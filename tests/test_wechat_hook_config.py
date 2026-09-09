from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.config import load_config


def _write_config(path: Path, hook_yaml: str) -> Path:
    path.write_text(
        """
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {enabled: true}
channels:
  wechat:
    hook_quote:
"""
        + hook_yaml,
        encoding="utf-8",
    )
    return path


def test_wechat_hook_quote_is_disabled_by_default() -> None:
    root = Path(__file__).resolve().parents[1]

    settings = load_config(root / "config.example.yaml").wechat.hook_quote

    assert settings.enabled is False
    assert settings.endpoint == "http://127.0.0.1:30001"
    assert settings.token_env == "AGENT_BRIDGE_WECHAT_HOOK_TOKEN"
    assert settings.expected_version == "4.1.12.55"


def test_loads_explicit_wechat_hook_quote_settings(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path / "config.yaml",
        """      enabled: true
      endpoint: http://127.0.0.1:31001
      token_env: TEST_WECHAT_HOOK_TOKEN
      timeout_seconds: 1.25
      expected_version: 4.1.12.55
""",
    )

    settings = load_config(config).wechat.hook_quote

    assert settings.enabled is True
    assert settings.endpoint == "http://127.0.0.1:31001"
    assert settings.token_env == "TEST_WECHAT_HOOK_TOKEN"
    assert settings.timeout_seconds == pytest.approx(1.25)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://0.0.0.0:30001",
        "http://localhost:30001",
        "http://192.168.1.2:30001",
        "https://127.0.0.1:30001",
    ],
)
def test_rejects_unsafe_wechat_hook_endpoint(
    tmp_path: Path,
    endpoint: str,
) -> None:
    config = _write_config(
        tmp_path / "config.yaml",
        f"      endpoint: {endpoint}\n",
    )

    with pytest.raises(ValueError, match="loopback"):
        load_config(config)
