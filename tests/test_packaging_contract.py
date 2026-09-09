from pathlib import Path

import pytest


def test_readme_demo_asset_exists_and_is_not_ignored():
    import subprocess

    root = Path(__file__).resolve().parents[1]
    assert "(assets/manager-demo.gif)" in (root / "README.md").read_text(encoding="utf-8")
    with (root / "assets" / "manager-demo.gif").open("rb") as handle:
        assert handle.read(6) in {b"GIF87a", b"GIF89a"}
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "assets/manager-demo.gif"],
            cwd=root, capture_output=True, check=False,
        )
        assert result.returncode == 1


@pytest.mark.parametrize("arguments", [[], ["manager"]])
def test_frozen_executable_opens_manager_by_default(monkeypatch, arguments):
    import sys

    from agent_bridge import frozen_main, manager_main

    calls = []
    monkeypatch.setattr(sys, "argv", ["AgentBridge.exe", *arguments])
    monkeypatch.setattr(frozen_main.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(manager_main, "main", lambda: calls.append(tuple(sys.argv)) or 0)

    assert frozen_main.main() == 0
    assert calls == [("AgentBridge.exe",)]


def test_git_ignores_local_data_and_binaries_but_not_source():
    import subprocess

    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        pytest.skip("Git metadata is not present in source archives")
    private_paths = [
        "config.yaml", "config.yaml.bak", ".env", ".env.local", "secret.pem",
        "logs/workbench.log", "artifacts/screenshot.png", "wechatauto_logs/test.log",
        "data/bridge.db", "dist/AgentBridge.exe", "@AutomationLog.txt",
        "doc/internal.md", "docs/internal.md", "manager.lock",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--stdin", "-z"], input="\0".join(private_paths).encode(),
        cwd=root, capture_output=True, check=False,
    )
    assert set(result.stdout.decode().rstrip("\0").split("\0")) == set(private_paths)
    source = subprocess.run(
        ["git", "check-ignore", "--no-index", "config.example.yaml", "packaging/agent_bridge.spec",
         "agent_bridge/assets/manager-tray.png"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    assert source.returncode == 1


def test_manager_executable_and_shortcuts_share_icon_identity():
    from agent_bridge.manager_main import MANAGER_APP_ID

    root = Path(__file__).resolve().parents[1]
    spec = (root / "packaging" / "agent_bridge.spec").read_text(encoding="utf-8")
    installer = (root / "packaging" / "agent_bridge.iss").read_text(encoding="utf-8")
    assert 'icon=str(project_root / "agent_bridge" / "assets" / "manager-tray.png")' in spec
    assert installer.count(f'AppUserModelID: "{MANAGER_APP_ID}"') == 2
    assert (root / "agent_bridge" / "assets" / "manager-tray.png").is_file()


def test_pyinstaller_spec_includes_template_but_not_user_data() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "packaging" / "agent_bridge.spec").read_text(encoding="utf-8")

    assert "config.example.yaml" in text
    assert '"codex_cli_bin"' in text
    assert 'config.yaml"' not in text
    assert '".data"' not in text
    assert "frozen_main.py" in text


def test_installer_defaults_to_program_files_and_preserves_user_data() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "packaging" / "agent_bridge.iss").read_text(encoding="utf-8")

    assert "DefaultDirName={autopf}\\Agent Bridge" in text
    assert "DeleteUserDataCheckbox.Checked := False" in text
    assert 'Parameters: "manager"' in text


def test_frozen_process_uses_single_packaged_executable() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "agent_bridge" / "manager" / "process_controller.py").read_text(
        encoding="utf-8"
    )

    assert '"AgentBridge.exe"' in text
    assert '"--config"' in text
