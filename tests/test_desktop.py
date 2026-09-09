from pathlib import Path

from PIL import Image

from agent_bridge.tools.desktop import DesktopToolbox, WindowInfo


def test_find_window_resolves_vscode_title_and_process_aliases(
    tmp_path: Path, monkeypatch
) -> None:
    toolbox = DesktopToolbox(tmp_path)
    monkeypatch.setattr(
        toolbox,
        "list_windows",
        lambda: [WindowInfo(101, "main.py - Visual Studio Code", "Code.exe")],
    )

    assert toolbox.find_window("vscode").hwnd == 101
    assert toolbox.find_window("代码").hwnd == 101
    assert toolbox.find_window("vsc").hwnd == 101
    assert toolbox.find_window("visul studio code").hwnd == 101


def test_find_window_resolves_qq_aliases(tmp_path: Path, monkeypatch) -> None:
    toolbox = DesktopToolbox(tmp_path)
    monkeypatch.setattr(
        toolbox,
        "list_windows",
        lambda: [WindowInfo(202, "QQ", "QQ.exe")],
    )

    assert toolbox.find_window("qq").hwnd == 202
    assert toolbox.find_window("腾讯QQ").hwnd == 202


def test_find_window_tolerates_short_typo(tmp_path: Path, monkeypatch) -> None:
    toolbox = DesktopToolbox(tmp_path)
    monkeypatch.setattr(
        toolbox,
        "list_windows",
        lambda: [WindowInfo(303, "Adobe Photoshop", "Photoshop.exe")],
    )

    assert toolbox.find_window("phoshop").hwnd == 303


def test_find_window_prefers_netease_main_window_over_desktop_lyrics(
    tmp_path: Path, monkeypatch
) -> None:
    toolbox = DesktopToolbox(tmp_path)
    monkeypatch.setattr(
        toolbox,
        "list_windows",
        lambda: [
            WindowInfo(401, "桌面歌词", "cloudmusic.exe"),
            WindowInfo(402, "Song title - 网易云音乐", "cloudmusic.exe", minimized=True),
        ],
    )

    assert toolbox.find_window("网易云").hwnd == 402


def test_netease_match_returns_only_largest_process_window(
    tmp_path: Path, monkeypatch
) -> None:
    toolbox = DesktopToolbox(tmp_path)
    monkeypatch.setattr(
        toolbox,
        "list_windows",
        lambda: [
            WindowInfo(501, "", "cloudmusic.exe"),
            WindowInfo(502, "网易云音乐", "cloudmusic.exe"),
            WindowInfo(503, "", "cloudmusic.exe"),
        ],
    )
    monkeypatch.setattr(
        "win32gui.GetWindowRect",
        lambda hwnd: {
            501: (0, 0, 80, 80),
            502: (0, 0, 1200, 800),
            503: (0, 0, 120, 40),
        }[hwnd],
    )

    matches = toolbox.match_windows("网易云", toolbox.list_windows())

    assert [window.hwnd for window in matches] == [502]


def test_blank_capture_is_rejected(tmp_path: Path) -> None:
    black = Image.new("RGB", (20, 20), (0, 0, 0))
    useful = Image.new("RGB", (20, 20), (20, 20, 20))

    assert DesktopToolbox._is_usable_image(black) is False
    assert DesktopToolbox._is_usable_image(useful) is True


def test_partial_window_capture_is_rejected(tmp_path: Path) -> None:
    # A buggy Win32 capture can return only a title-bar strip (for example
    # 160x28) while still containing non-black pixels.  It must not be sent
    # as the complete application screenshot.
    partial = Image.new("RGB", (160, 28), "white")
    full = Image.new("RGB", (900, 670), "white")

    assert DesktopToolbox._is_usable_image(partial, (900, 670)) is False
    assert DesktopToolbox._is_usable_image(full, (900, 670)) is True


def test_activated_capture_waits_for_rendered_frames(
    tmp_path: Path, monkeypatch
) -> None:
    toolbox = DesktopToolbox(tmp_path)
    sleeps: list[float] = []

    class FakeUser32:
        def GetForegroundWindow(self):
            return 7

        def ShowWindow(self, _hwnd, _command):
            return True

        def SetForegroundWindow(self, _hwnd):
            return True

        def IsWindow(self, _hwnd):
            return True

    monkeypatch.setattr(toolbox, "_user32", lambda: FakeUser32())
    monkeypatch.setattr(toolbox, "_grab_screen", lambda _rect: Image.new("RGB", (2, 2), "white"))
    monkeypatch.setattr("agent_bridge.tools.desktop.time.sleep", sleeps.append)

    image = toolbox._capture_activated_screen(9, (0, 0, 2, 2))

    assert image is not None
    assert sleeps == [0.8, 0.2, 0.2, 0.2]


def test_combine_images_creates_labeled_contact_sheet(tmp_path: Path) -> None:
    first = tmp_path / "wechat.png"
    second = tmp_path / "qq.png"
    Image.new("RGB", (120, 80), "#ff0000").save(first)
    Image.new("RGB", (80, 100), "#0000ff").save(second)

    output = DesktopToolbox(tmp_path).combine_images(
        [("微信窗口", first), ("QQ窗口", second)]
    )

    with Image.open(output) as image:
        assert image.width == 120
        assert image.height == (36 + 80) + 12 + (36 + 100)
