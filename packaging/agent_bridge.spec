from pathlib import Path
import importlib.util

from PyInstaller.utils.hooks import collect_all


project_root = Path(SPECPATH).resolve().parent
datas = [
    (str(project_root / "config.example.yaml"), "."),
    (str(project_root / "agent_bridge" / "assets" / "manager-tray.png"), "agent_bridge/assets"),
]
binaries = []
hiddenimports = ["agent_bridge.cli", "agent_bridge.manager_main"]

for package in (
    "wechatauto",
    "uiautomation",
    "openai_codex",
    "codex_cli_bin",
    "claude_agent_sdk",
    "qasync",
):
    if importlib.util.find_spec(package) is None:
        continue
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += [
        name
        for name in package_hidden
        if ".demo" not in name
        and not name.endswith(".__main__")
        and ".testing" not in name
    ]

a = Analysis(
    [str(project_root / "agent_bridge" / "frozen_main.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(project_root / "packaging" / "runtime_hook.py")],
    excludes=["pytest", "tests"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AgentBridge",
    icon=str(project_root / "agent_bridge" / "assets" / "manager-tray.png"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(project_root / "packaging" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AgentBridge",
)
