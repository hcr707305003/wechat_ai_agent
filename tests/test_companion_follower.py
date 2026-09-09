import pytest

from agent_bridge.channels.wechat import WeChatCompanionSettings
from agent_bridge.companion.follower import (
    Win32WindowOwner,
    Win32WindowProbe,
    WindowRect,
    calculate_companion_geometry,
    calculate_launcher_geometry,
)


def _handle_value(value) -> int:
    return int(getattr(value, "value", value) or 0)


class FakeUser32:
    def __init__(self) -> None:
        self.ancestors = {11: 111, 22: 222, 23: 223}
        self.owners: dict[int, int] = {}
        self.set_calls: list[tuple[int, int, int]] = []
        self.accept_owner = True
        self.raise_on_set = False

    def IsWindow(self, handle) -> bool:
        return _handle_value(handle) in self.ancestors.values()

    def GetAncestor(self, handle, _flag: int) -> int:
        value = _handle_value(handle)
        return self.ancestors.get(value, value)

    def SetWindowLongPtrW(self, handle, index: int, owner: int) -> int:
        if self.raise_on_set:
            raise OSError("Win32 owner binding failed")
        window = _handle_value(handle)
        owner_value = _handle_value(owner)
        self.set_calls.append((window, index, owner_value))
        if self.accept_owner:
            self.owners[window] = owner_value
        return 0

    def GetWindowLongPtrW(self, handle, _index: int) -> int:
        return self.owners.get(_handle_value(handle), 0)


def test_binds_companion_as_owned_window_and_caches_the_pair() -> None:
    user32 = FakeUser32()
    owner = Win32WindowOwner(user32=user32)

    assert owner.bind(11, 22) is True
    assert owner.bind(11, 22) is True

    assert user32.set_calls == [(111, -8, 222)]


def test_rebinds_when_wechat_handle_changes() -> None:
    user32 = FakeUser32()
    owner = Win32WindowOwner(user32=user32)

    assert owner.bind(11, 22) is True
    assert owner.bind(11, 23) is True

    assert user32.set_calls == [(111, -8, 222), (111, -8, 223)]


def test_failed_owner_binding_is_not_cached() -> None:
    user32 = FakeUser32()
    user32.accept_owner = False
    owner = Win32WindowOwner(user32=user32)

    assert owner.bind(11, 22) is False
    assert owner.bind(11, 22) is False

    assert user32.set_calls == [(111, -8, 222), (111, -8, 222)]


def test_owner_api_error_returns_failure() -> None:
    user32 = FakeUser32()
    user32.raise_on_set = True
    owner = Win32WindowOwner(user32=user32)

    assert owner.bind(11, 22) is False


def test_reads_client_rect_in_absolute_screen_coordinates() -> None:
    class User32:
        def IsWindow(self, _handle) -> bool:
            return True

        def GetWindowRect(self, _handle, rect) -> bool:
            rect._obj.left = -1600
            rect._obj.top = 100
            rect._obj.right = -700
            rect._obj.bottom = 900
            return True

        def IsWindowVisible(self, _handle) -> bool:
            return True

        def IsIconic(self, _handle) -> bool:
            return False

        def GetClientRect(self, _handle, rect) -> bool:
            rect._obj.left = 0
            rect._obj.top = 0
            rect._obj.right = 884
            rect._obj.bottom = 754
            return True

        def ClientToScreen(self, _handle, point) -> bool:
            point._obj.x = -1592
            point._obj.y = 138
            return True

    user32 = User32()
    probe = Win32WindowProbe(user32=user32)
    snapshot = probe.snapshot(101)

    assert snapshot.rect == WindowRect(-1600, 100, -700, 900)
    assert snapshot.client_rect == WindowRect(-1592, 138, -708, 892)

    user32.GetClientRect = lambda _handle, _rect: False
    assert probe.snapshot(101).client_rect is None


def test_calculates_launcher_geometry_from_negative_client_rect() -> None:
    client = WindowRect(-1592, 138, -708, 892)

    geometry = calculate_launcher_geometry(client)

    assert (geometry.x, geometry.y) == (-1580, 150)
    assert (geometry.width, geometry.height) == (44, 44)


@pytest.mark.parametrize(
    ("side", "expected"),
    [
        ("left", "320x240+680+200"),
        ("right", "320x240+1800+200"),
        ("top", "800x240+1000-40"),
        ("bottom", "800x240+1000+800"),
    ],
)
def test_calculates_each_dock_side(side: str, expected: str) -> None:
    settings = WeChatCompanionSettings(
        side=side,
        width=320,
        height=240,
    )
    wechat = WindowRect(1000, 200, 1800, 800)

    geometry = calculate_companion_geometry(wechat, settings)

    assert geometry.as_tk_geometry() == expected


def test_left_monitor_keeps_negative_absolute_coordinate() -> None:
    settings = WeChatCompanionSettings(side="left", width=440, height=620)
    wechat = WindowRect(-1600, 100, -700, 900)

    geometry = calculate_companion_geometry(wechat, settings)

    assert geometry.x == -2040
    assert geometry.y == 100


def test_left_right_dock_height_is_clamped_to_wechat_window() -> None:
    settings = WeChatCompanionSettings(side="right", width=440, height=900)
    wechat = WindowRect(100, 200, 1000, 800)

    geometry = calculate_companion_geometry(wechat, settings)

    assert geometry.width == 440
    assert geometry.height == 600
