from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_bridge import cli
from agent_bridge.agents import availability
from agent_bridge.agents.availability import AgentAvailability, effective_default
from agent_bridge.config import load_config
from agent_bridge.manager import doctor
from agent_bridge.manager.agent_debug import build_debug_adapter
from agent_bridge.models import ConversationType, SessionBindingConfig, UnifiedMessage
from agent_bridge.sessions.repository import SQLiteRepository
from agent_bridge.sessions.resolver import SessionResolver


def config():
    return load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")


@pytest.mark.parametrize("installed", [(), ("codex",), ("claude",), ("codex", "claude")])
def test_one_agent_suffices_for_factory_and_manager(monkeypatch, installed):
    def probe(provider):
        return AgentAvailability(provider in installed, "测试依赖状态")

    monkeypatch.setattr(availability, "probe_agent", probe)
    monkeypatch.setattr(doctor, "probe_agent", probe)
    cfg = config()
    session = doctor.ManagerCheckSession(Path(__file__).resolve().parents[1] / "config.example.yaml")
    session.config = cfg
    results = [session._check_agent(p) for p in availability.PROVIDERS]
    assert all(not r.required for r in results)
    assert session._check_any_agent().ok is bool(installed)
    if installed:
        factory = cli.build_agent_factory(cfg)
        assert set(factory.providers()) == set(installed)
        chosen = effective_default(cfg, availability.configured_availability(cfg))
        assert chosen == ("codex" if "codex" in installed else "claude")
        assert cfg.runtime.default_provider == "codex"  # no config rewrite
        for missing in set(availability.PROVIDERS) - set(installed):
            with pytest.raises(KeyError):
                factory.get(missing)
    else:
        with pytest.raises(RuntimeError, match="均不可用"):
            cli.build_agent_factory(cfg)


def test_disabled_agent_is_not_probed_or_registered(monkeypatch):
    cfg = config()
    cfg = replace(cfg, agents={**cfg.agents, "codex": replace(cfg.agents["codex"], enabled=False)})
    calls = []

    def probe(provider):
        calls.append(provider)
        return AgentAvailability(True, "就绪")

    monkeypatch.setattr(availability, "probe_agent", probe)
    assert cli.build_agent_factory(cfg).providers() == ("claude",)
    assert calls == ["claude"]


def test_absent_sdk_does_not_import_or_start_anything(monkeypatch):
    monkeypatch.setattr(availability.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(availability.importlib, "import_module", lambda name: pytest.fail("must not import"))
    for provider in availability.PROVIDERS:
        assert not availability.probe_agent(provider).available


def test_broken_sdk_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(availability.importlib.util, "find_spec", lambda name: object())

    def broken(name):
        raise ImportError("bad SDK")

    monkeypatch.setattr(availability.importlib, "import_module", broken)
    assert "bad SDK" in availability.probe_agent("codex").reason


@pytest.mark.parametrize("exists", [True, False])
def test_codex_bundled_runtime_checked(monkeypatch, tmp_path, exists):
    import codex_cli_bin

    runtime = tmp_path / "codex.exe"
    if exists:
        runtime.touch()
    monkeypatch.setattr(codex_cli_bin, "bundled_codex_path", lambda: runtime)
    assert availability.probe_agent("codex").available is exists


@pytest.mark.parametrize("exists", [True, False])
def test_claude_bundled_runtime_checked_without_running(monkeypatch, tmp_path, exists):
    runtime = tmp_path / "_bundled" / ("claude.exe" if availability.os.name == "nt" else "claude")
    runtime.parent.mkdir()
    if exists:
        runtime.touch()
    monkeypatch.setattr(availability.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(availability.importlib, "import_module", lambda name: SimpleNamespace(__file__=str(tmp_path / "__init__.py")))
    monkeypatch.setattr(availability.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert availability.probe_agent("claude").available is exists


def test_default_may_be_disabled_or_unconfigured(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("runtime: {default_provider: codex}\nagents:\n  claude: {enabled: true}\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.runtime.default_provider == "codex"
    assert "codex" not in cfg.agents


@pytest.mark.parametrize("provider", ["codex", "unknown"])
def test_absent_bound_agent_does_not_block_other_conversations(tmp_path, provider):
    path = tmp_path / "config.yaml"
    path.write_text(
        "agents:\n  claude: {enabled: true}\nchannels:\n  wechat:\n    session_bindings:\n"
        f"      - {{conversation_id: old, provider: {provider}, session_id: native-original}}\n",
        encoding="utf-8",
    )
    if provider == "unknown":
        with pytest.raises(ValueError, match="Unsupported"):
            load_config(path)
    else:
        cfg = load_config(path)
        assert cfg.wechat.session_bindings[0].provider == "codex"
        assert "codex" not in cfg.agents


def test_fallback_does_not_rebind_existing_or_explicit_session(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "test.db"))
    try:
        message = UnifiedMessage(channel="wechat", channel_account_id="self", conversation_id="friend",
                                 conversation_type=ConversationType.PRIVATE, sender_id="friend",
                                 message_id="1", content="hello")
        old = SessionResolver(repo, "codex", str(tmp_path), (str(tmp_path),))
        session, _ = old.resolve(message)
        resolver = SessionResolver(repo, "claude", str(tmp_path), (str(tmp_path),))
        assert resolver.resolve(message)[0].current_provider == "codex"
        assert resolver.resolve(replace(message, conversation_id="new"))[0].current_provider == "claude"
        binding = SessionBindingConfig(conversation_id="bound", provider="codex", session_id="native-original")
        resolver.configure_session_bindings((binding,))
        bound, _ = resolver.resolve(replace(message, conversation_id="bound"))
        assert bound.current_provider == "codex"
        assert repo.get_active_native_session(bound.id, "codex").native_session_id == "native-original"
        assert repo.get_session(session.id).current_provider == "codex"
    finally:
        repo.close()


def test_debug_backend_rejects_missing_sdk(monkeypatch):
    from agent_bridge.manager import agent_debug

    monkeypatch.setattr(agent_debug, "probe_agent", lambda p: AgentAvailability(False, "未安装"))
    with pytest.raises(ValueError, match="不可用"):
        build_debug_adapter(config(), "codex")


def test_cli_doctor_allows_only_one_agent(monkeypatch, capsys):
    cfg = replace(config(), wechat_enabled=False)
    monkeypatch.setattr(availability, "probe_agent", lambda p: AgentAvailability(p == "claude", "测试"))
    assert cli.doctor(cfg) == 0
    assert "WARN" in capsys.readouterr().out
    monkeypatch.setattr(availability, "probe_agent", lambda p: AgentAvailability(False, "测试"))
    assert cli.doctor(cfg) == 1
