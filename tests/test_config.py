from pathlib import Path

import pytest

from agent_bridge.config import load_config


def test_load_example_config() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config.example.yaml")

    assert config.runtime.default_provider == "codex"
    assert set(config.agents) == {"codex", "claude"}
    assert config.agents["codex"].codex is not None
    assert config.agents["codex"].codex.model == "gpt-5.6-terra"
    assert config.agents["codex"].codex.fallback_models == (
        "gpt-5.6-luna",
        "gpt-5.5",
    )
    assert config.wechat.allowed_private_ids == ("filehelper",)
    assert config.wechat.webhooks == ()
    assert config.wechat.group_prefixes == ("/ai",)
    assert config.wechat.group_prefixes_rule == "prefix"
    assert config.wechat.reply_prefix == ""
    assert config.wechat.quote_private_replies is False
    assert config.wechat.quote_group_replies is False
    assert config.wechat.message_batch_window_seconds == pytest.approx(1.5)
    assert config.wechat.companion.mode == "docked"
    assert config.wechat.companion.side == "right"
    assert config.wechat.companion.theme == "system"
    assert config.wechat.companion.width == 440
    assert config.wechat.companion.height == 620
    assert config.wechat.companion.follow_interval == pytest.approx(0.016)
    assert config.wechat.listener_interval == pytest.approx(0.25)
    assert config.wechat.hook_quote.enabled is False
    assert config.wechat.hook_quote.endpoint == "http://127.0.0.1:30001"
    assert (
        config.wechat.hook_quote.token_env
        == "AGENT_BRIDGE_WECHAT_HOOK_TOKEN"
    )
    assert config.wechat.hook_quote.expected_version == "4.1.12.55"
    assert config.wechat.sender.mode == "idle_uia"
    assert config.wechat.sender.max_attempts == 3
    assert config.wechat.sender.mouse_fallback is False
    assert config.wechat.sender.foreground_fallback_default is False
    assert config.wechat.sender.foreground_driver == "wechat_mcp"
    assert config.wechat.sender.foreground_operation_timeout_seconds == pytest.approx(8)
    assert config.wechat.sender.foreground_quote_timeout_seconds == pytest.approx(30)
    assert config.wechat.sender.foreground_cooldown_seconds == pytest.approx(60)
    assert Path(config.runtime.default_working_directory) == root


def test_loads_explicit_wechat_hook_quote_settings(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {enabled: true}
channels:
  wechat:
    hook_quote:
      enabled: true
      endpoint: http://127.0.0.1:31001
      token_env: TEST_WECHAT_HOOK_TOKEN
      timeout_seconds: 1.25
      expected_version: 4.1.12.55
""",
        encoding="utf-8",
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
    tmp_path: Path, endpoint: str
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {{enabled: true}}
channels:
  wechat:
    hook_quote:
      endpoint: {endpoint}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="loopback"):
        load_config(config)


@pytest.mark.parametrize(
    "field",
    ["quote_private_replies", "quote_group_replies"],
)
def test_rejects_non_boolean_quote_settings(tmp_path: Path, field: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {{enabled: true}}
channels:
  wechat:
    {field}: "false"
""",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match=field):
        load_config(config)


def test_loads_wechat_reply_prefix(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {enabled: true}
channels:
  wechat:
    reply_prefix: "[ai回复]"
""",
        encoding="utf-8",
    )

    assert load_config(config).wechat.reply_prefix == "[ai回复]"


@pytest.mark.parametrize("value", ["null", "123", "false", "[]", "{}"])
def test_rejects_non_string_wechat_reply_prefix(tmp_path: Path, value: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {{enabled: true}}
channels:
  wechat:
    reply_prefix: {value}
""",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="channels.wechat.reply_prefix"):
        load_config(config)


@pytest.mark.parametrize("value", ["-0.1", ".nan", ".inf"])
def test_rejects_invalid_message_batch_window(tmp_path: Path, value: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {{enabled: true}}
channels:
  wechat:
    message_batch_window_seconds: {value}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="message_batch_window_seconds"):
        load_config(config)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("foreground_operation_timeout_seconds", "0"),
        ("foreground_operation_timeout_seconds", ".nan"),
        ("foreground_operation_timeout_seconds", ".inf"),
        ("foreground_quote_timeout_seconds", "0"),
        ("foreground_quote_timeout_seconds", ".nan"),
        ("foreground_quote_timeout_seconds", ".inf"),
        ("foreground_cooldown_seconds", "-0.1"),
        ("foreground_cooldown_seconds", ".nan"),
        ("foreground_cooldown_seconds", ".inf"),
    ],
)
def test_rejects_invalid_foreground_safety_settings(
    tmp_path: Path, field: str, value: str
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {{enabled: true}}
channels:
  wechat:
    sender:
      {field}: {value}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=field):
        load_config(config)


def test_rejects_invalid_companion_mode(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {enabled: true}
channels:
  wechat:
    companion: {mode: floating}
""",
        encoding="utf-8",
    )

    try:
        load_config(config)
    except ValueError as error:
        assert "mode" in str(error)
    else:
        raise AssertionError("invalid companion mode was accepted")


def test_accepts_explicit_companion_theme(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {enabled: true}
channels:
  wechat:
    companion: {theme: dark}
""",
        encoding="utf-8",
    )

    assert load_config(config).wechat.companion.theme == "dark"


def test_rejects_invalid_companion_theme(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {enabled: true}
channels:
  wechat:
    companion: {theme: neon}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="theme"):
        load_config(config)


def test_loads_idle_uia_sender_settings(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {enabled: true}
channels:
  wechat:
    sender:
      mode: idle_uia
      idle_seconds: 2
      max_queue_age_seconds: 300
      max_attempts: 4
      verify_sends: true
      mouse_fallback: false
      foreground_fallback_default: true
      foreground_driver: wechat_mcp
""",
        encoding="utf-8",
    )

    sender = load_config(config).wechat.sender

    assert sender.idle_seconds == pytest.approx(2)
    assert sender.max_queue_age_seconds == pytest.approx(300)
    assert sender.max_attempts == 4
    assert sender.verify_sends is True
    assert sender.foreground_fallback_default is True
    assert sender.foreground_driver == "wechat_mcp"


def test_rejects_mouse_fallback_for_idle_uia(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {enabled: true}
channels:
  wechat:
    sender: {mode: idle_uia, mouse_fallback: true}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="mouse_fallback"):
        load_config(config)


def test_legacy_verify_sends_is_preserved(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {enabled: true}
channels:
  wechat:
    verify_sends: false
""",
        encoding="utf-8",
    )

    assert load_config(config).wechat.sender.verify_sends is False


@pytest.mark.parametrize("rule", ("prefix", "contains", "suffix"))
def test_loads_group_prefix_matching_rule(tmp_path: Path, rule: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {{enabled: true}}
channels:
  wechat:
    group_prefixes: ["@胡超然"]
    group_prefixes_rule: {rule}
""",
        encoding="utf-8",
    )

    loaded = load_config(config)

    assert loaded.wechat.group_prefixes == ("@胡超然",)
    assert loaded.wechat.group_prefixes_rule == rule


def test_rejects_invalid_group_prefix_matching_rule(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {enabled: true}
channels:
  wechat:
    group_prefixes_rule: anywhere
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="group_prefixes_rule"):
        load_config(config)


def test_loads_multiple_private_and_group_session_bindings(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
runtime:
  default_provider: codex
  allowed_roots: [.]
agents:
  codex: {enabled: true}
  claude: {enabled: true}
channels:
  wechat:
    session_bindings:
      - conversation_id: wxid_private
        conversation_type: private
        provider: codex
        session_id: thread_private
      - conversation_id: 123@chatroom
        conversation_type: group
        provider: codex
        session_id: thread_private
      - conversation_id: 456@chatroom
        conversation_type: group
        provider: claude
        session_id: 550e8400-e29b-41d4-a716-446655440000
""",
        encoding="utf-8",
    )

    bindings = load_config(config).wechat.session_bindings

    assert len(bindings) == 3
    assert bindings[0].conversation_id == "wxid_private"
    assert bindings[0].provider == "codex"
    assert bindings[0].conversation_type.value == "private"
    assert bindings[1].conversation_id == "123@chatroom"
    assert bindings[1].provider == "codex"
    assert bindings[1].session_id == "thread_private"
    assert bindings[1].conversation_type.value == "group"
    assert bindings[2].conversation_id == "456@chatroom"
    assert bindings[2].provider == "claude"
    assert bindings[2].conversation_type.value == "group"
