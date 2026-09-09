from agent_bridge.senders.activity import ActivitySnapshot, WindowsActivityMonitor


class FakeUser32:
    def __init__(self) -> None:
        self.last_input = 1_000
        self.foreground = 42
        self.cursor = (10, 20)

    def GetLastInputInfo(self, pointer) -> int:
        pointer._obj.dwTime = self.last_input
        return 1

    def GetCursorPos(self, pointer) -> int:
        pointer._obj.x, pointer._obj.y = self.cursor
        return 1

    def GetForegroundWindow(self) -> int:
        return self.foreground


class FakeKernel32:
    def __init__(self, tick: int) -> None:
        self.tick = tick

    def GetTickCount(self) -> int:
        return self.tick


def test_activity_monitor_detects_idle_and_unchanged_state() -> None:
    user32 = FakeUser32()
    monitor = WindowsActivityMonitor(user32, FakeKernel32(3_000))
    snapshot = monitor.snapshot()

    assert snapshot == ActivitySnapshot(1_000, 42, (10, 20), True)
    assert monitor.is_idle(1.5) is True
    assert monitor.unchanged(snapshot) is True


def test_activity_monitor_detects_new_input_or_foreground_change() -> None:
    user32 = FakeUser32()
    monitor = WindowsActivityMonitor(user32, FakeKernel32(3_000))
    snapshot = monitor.snapshot()

    user32.last_input += 1
    assert monitor.unchanged(snapshot) is False

    user32.last_input = snapshot.last_input_tick
    user32.foreground = 99
    assert monitor.unchanged(snapshot) is False


def test_activity_monitor_requires_an_available_desktop() -> None:
    user32 = FakeUser32()
    user32.foreground = 0
    monitor = WindowsActivityMonitor(user32, FakeKernel32(3_000))

    assert monitor.is_idle(1.5) is False


def test_activity_monitor_handles_tick_counter_wraparound() -> None:
    user32 = FakeUser32()
    user32.last_input = 0xFFFFFF00
    monitor = WindowsActivityMonitor(user32, FakeKernel32(0x00000300))

    assert monitor.idle_seconds() == 1.024
