from __future__ import annotations

import asyncio
import ctypes
import logging
import math
import os
import re
import time
from collections.abc import Awaitable, Callable
from ctypes import wintypes
from dataclasses import dataclass, replace

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QEvent,
    QObject,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSignalBlocker,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygonF,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from agent_bridge.channels.wechat import WeChatCompanionSettings
from agent_bridge.companion.avatars import AvatarCache
from agent_bridge.companion.controller import CompanionController
from agent_bridge.companion.follower import (
    Win32WindowMover,
    Win32WindowOwner,
    Win32WindowProbe,
    WindowGeometry,
    WindowRect,
    WindowSnapshot,
    calculate_companion_geometry,
    calculate_launcher_geometry,
)
from agent_bridge.companion.models import (
    CompanionUpdate,
    ConversationItem,
    TimelineEntry,
)
from agent_bridge.companion.qt_widgets import ConversationRow, MessageCard
from agent_bridge.companion.theme import build_stylesheet, resolve_theme

logger = logging.getLogger(__name__)

_GEOMETRY_PATTERN = re.compile(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$")
_COMPACT_SIDEBAR_WIDTH = 80
_EXPANDED_SIDEBAR_WIDTH = 190
_SIDEBAR_EXPANDED_BREAKPOINT = 560
_WM_MOVING = 0x0216
_WM_ENTERSIZEMOVE = 0x0231
_WM_EXITSIZEMOVE = 0x0232
_WM_NCHITTEST = 0x0084
_HTLEFT = 10
_HTRIGHT = 11
_HTTOP = 12
_HTTOPLEFT = 13
_HTTOPRIGHT = 14
_HTBOTTOM = 15
_HTBOTTOMLEFT = 16
_HTBOTTOMRIGHT = 17


def calculate_resize_hit(
    rect: WindowRect, x: int, y: int, border: int
) -> int | None:
    """Return the Win32 resize hit code for a screen point."""
    border = max(1, border)
    left = rect.left <= x < rect.left + border
    right = rect.right - border <= x < rect.right
    top = rect.top <= y < rect.top + border
    bottom = rect.bottom - border <= y < rect.bottom
    if top and left:
        return _HTTOPLEFT
    if top and right:
        return _HTTOPRIGHT
    if bottom and left:
        return _HTBOTTOMLEFT
    if bottom and right:
        return _HTBOTTOMRIGHT
    if left:
        return _HTLEFT
    if right:
        return _HTRIGHT
    if top:
        return _HTTOP
    if bottom:
        return _HTBOTTOM
    return None


def _mix_color(start: QColor, end: QColor, progress: float) -> QColor:
    progress = min(1.0, max(0.0, progress))
    return QColor(
        round(start.red() + (end.red() - start.red()) * progress),
        round(start.green() + (end.green() - start.green()) * progress),
        round(start.blue() + (end.blue() - start.blue()) * progress),
        round(start.alpha() + (end.alpha() - start.alpha()) * progress),
    )


class CompanionSignalBus(QObject):
    updated = Signal(object)
    avatar_updated = Signal(object)


@dataclass(slots=True, frozen=True)
class _GroupMoveOrigin:
    companion: WindowRect
    wechat: WindowRect
    wechat_handle: int


class CompanionLauncher(QPushButton):
    def __init__(self, open_workbench: Callable[[], None]) -> None:
        super().__init__("AI")
        self._open_workbench = open_workbench
        self._allow_close = False
        self.setObjectName("companionLauncher")
        self.setWindowFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(44, 44)
        self.setToolTip("打开 Agent 工作台")
        self.setAccessibleName("打开 Agent 工作台")
        self.clicked.connect(open_workbench)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        event.ignore()
        self._open_workbench()

    def shutdown(self) -> None:
        self._allow_close = True
        self.close()


class AnimatedSwitch(QCheckBox):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        animations_enabled: bool | None = None,
    ) -> None:
        super().__init__("", parent)
        self.setObjectName("headerReplySwitch")
        self.setFixedSize(38, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._knob_position = 0.0
        self._animations_enabled = (
            QApplication.isEffectEnabled(Qt.UIEffect.UI_General)
            if animations_enabled is None
            else animations_enabled
        )
        self._off_color = QColor("#F0F1F7")
        self._on_color = QColor("#17834B")
        self._knob_color = QColor("#FFFFFF")
        self._border_color = QColor("#DCDDE7")
        self._focus_color = QColor("#6D5CE7")
        self.animation = QPropertyAnimation(self, b"knobPosition", self)
        self.animation.setDuration(160)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate_to_checked)

    @property
    def knob_position(self) -> float:
        return self._knob_position

    def _get_knob_position(self) -> float:
        return self._knob_position

    def _set_knob_position(self, value: float) -> None:
        self._knob_position = min(1.0, max(0.0, float(value)))
        self.update()

    knobPosition = Property(float, _get_knob_position, _set_knob_position)

    def set_theme_colors(
        self,
        *,
        off: str,
        on: str,
        knob: str,
        border: str,
        focus: str,
    ) -> None:
        self._off_color = QColor(off)
        self._on_color = QColor(on)
        self._knob_color = QColor(knob)
        self._border_color = QColor(border)
        self._focus_color = QColor(focus)
        self.update()

    def setChecked(self, checked: bool) -> None:
        changed = bool(checked) != self.isChecked()
        super().setChecked(checked)
        if not changed or self.signalsBlocked():
            self.animation.stop()
            self._set_knob_position(1.0 if checked else 0.0)

    def _animate_to_checked(self, checked: bool) -> None:
        target = 1.0 if checked else 0.0
        self.animation.stop()
        if not self._animations_enabled or not self.isVisible():
            self._set_knob_position(target)
            return
        self.animation.setStartValue(self._knob_position)
        self.animation.setEndValue(target)
        self.animation.start()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        position = self._knob_position
        track_color = _mix_color(self._off_color, self._on_color, position)
        border_color = _mix_color(
            self._border_color, self._on_color, position
        )
        knob_color = QColor(self._knob_color)
        if self.underMouse() and self.isEnabled():
            track_color = track_color.lighter(106)
        if not self.isEnabled():
            track_color.setAlpha(120)
            border_color.setAlpha(110)
            knob_color.setAlpha(170)

        track = QRectF(2.0, 6.0, 34.0, 18.0)
        painter.setPen(QPen(border_color, 1.0))
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, 9.0, 9.0)

        knob_x = 4.0 + 16.0 * position
        shadow = QColor(0, 0, 0, 38 if self.isEnabled() else 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow)
        painter.drawEllipse(QRectF(knob_x, 8.7, 14.0, 14.0))
        painter.setBrush(knob_color)
        painter.drawEllipse(QRectF(knob_x, 8.0, 14.0, 14.0))

        if self.hasFocus():
            focus_pen = QPen(self._focus_color, 1.5)
            focus_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(focus_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(0.75, 4.75, 36.5, 20.5), 10.0, 10.0)
        painter.end()

    def enterEvent(self, event: QEvent) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self.update()
        super().leaveEvent(event)

    def hitButton(self, position) -> bool:
        return self.rect().contains(position)


class CompanionTitleBarButton(QPushButton):
    def __init__(
        self,
        role: str,
        tooltip: str,
        callback: Callable[[], None],
        parent: QWidget,
    ) -> None:
        super().__init__("", parent)
        self.setObjectName(f"{role}Button")
        self.setProperty("titlebarRole", role)
        self.setFixedSize(38, 36)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCheckable(role == "settings")
        self.clicked.connect(callback)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self.palette().buttonText().color()
        if not self.isEnabled():
            color.setAlpha(90)
        pen = QPen(color)
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        center = QPointF(self.width() / 2, self.height() / 2)
        role = self.property("titlebarRole")
        if role == "settings":
            points = []
            for index in range(32):
                angle = -math.pi / 2 + math.tau * index / 32
                radius = 8.2 if index % 4 in (1, 2) else 6.3
                points.append(
                    QPointF(
                        center.x() + math.cos(angle) * radius,
                        center.y() + math.sin(angle) * radius,
                    )
                )
            painter.drawPolygon(QPolygonF(points))
            painter.drawEllipse(center, 2.7, 2.7)
        elif role == "minimize":
            painter.drawLine(
                QPointF(center.x() - 6.0, center.y() + 2.5),
                QPointF(center.x() + 6.0, center.y() + 2.5),
            )
        elif role == "close":
            painter.drawLine(
                QPointF(center.x() - 5.0, center.y() - 5.0),
                QPointF(center.x() + 5.0, center.y() + 5.0),
            )
            painter.drawLine(
                QPointF(center.x() + 5.0, center.y() - 5.0),
                QPointF(center.x() - 5.0, center.y() + 5.0),
            )
        painter.end()


class CompanionTitleBar(QFrame):
    def __init__(
        self,
        open_settings: Callable[[], None],
        minimize: Callable[[], None],
        close: Callable[[], None],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("customTitleBar")
        self.setFixedHeight(42)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 3, 8, 3)
        layout.setSpacing(2)
        self.title_label = QLabel("微信 Agent 工作台")
        self.title_label.setObjectName("windowTitleLabel")
        layout.addWidget(self.title_label, 1)
        self.settings_button = CompanionTitleBarButton(
            "settings", "打开会话设置", open_settings, self
        )
        self.minimize_button = CompanionTitleBarButton(
            "minimize", "收起 Agent 工作台", minimize, self
        )
        self.close_button = CompanionTitleBarButton(
            "close", "关闭 Agent 工作台", close, self
        )
        layout.addWidget(self.settings_button)
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.close_button)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()


class WeChatCompanionWindow(QMainWindow):
    visibility_changed = Signal(bool)

    def __init__(
        self,
        settings: WeChatCompanionSettings,
        controller: CompanionController,
        conversations: list[ConversationItem],
        hwnd_provider: Callable[[], int | None],
        load_geometry: Callable[[], str | None],
        save_geometry: Callable[[str], None],
        probe: Win32WindowProbe | None = None,
        mover: Win32WindowMover | None = None,
        owner: Win32WindowOwner | None = None,
        avatar_cache: AvatarCache | None = None,
        conversation_loader: Callable[
            [], Awaitable[list[ConversationItem]]
        ]
        | None = None,
    ) -> None:
        super().__init__()
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("QApplication must exist before creating the companion")

        self.settings = settings
        self.controller = controller
        self.conversations = conversations
        self.hwnd_provider = hwnd_provider
        self.load_geometry = load_geometry
        self.save_geometry = save_geometry
        self.probe = probe or Win32WindowProbe()
        self.mover = mover or Win32WindowMover()
        self.owner = owner or Win32WindowOwner()
        self.avatar_cache = avatar_cache
        self.conversation_loader = conversation_loader
        self._app = app
        self._signal_bus = CompanionSignalBus(self)
        self._signal_bus.updated.connect(self._handle_update)
        self._signal_bus.avatar_updated.connect(self._handle_avatar_updated)
        self._selected_index = 0 if conversations else -1
        self._unread: set[str] = set()
        self._compact_sidebar = False
        self._last_follow_geometry = ""
        self._owner_binding_warning = False
        self._group_move_active = False
        self._group_move_origin: _GroupMoveOrigin | None = None
        self._collapsed = False
        self._last_launcher_geometry = ""
        self._withdrawn = False
        self._closed_event = asyncio.Event()
        self._status_override_until = 0.0
        self._sender_state = "idle"
        self._avatar_tasks: set[asyncio.Task[None]] = set()
        self._profile_refresh_task: asyncio.Task[None] | None = None
        self._message_cards: dict[str, MessageCard] = {}
        self._timeline_entries: dict[str, TimelineEntry] = {}
        self._pending_timeline_updates: dict[str, int] = {}
        self._programmatic_timeline_scroll = False
        self._timeline_force_bottom_pending = False
        self._timeline_follow_bottom = True
        self._timeline_scroll_schedule_generation = 0
        self._show_started_at: float | None = None

        self.setWindowTitle("微信 Agent 工作台")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setMinimumSize(400, 300)
        self._build_ui()
        self.launcher = CompanionLauncher(self._expand_from_launcher)
        self.resize(settings.width, settings.height)
        if settings.mode == "independent":
            self._restore_geometry(load_geometry())

        self._apply_theme()
        style_hints = app.styleHints()
        if hasattr(style_hints, "colorSchemeChanged"):
            style_hints.colorSchemeChanged.connect(self._system_theme_changed)

        self.controller.subscribe(self.post_update)
        self._refresh_conversation_list()
        if conversations:
            self.conversation_list.setCurrentRow(0)
            self._load_conversation_history(conversations[0])
            self._render_selected()
        else:
            self._render_selected()

        self._follow_timer = QTimer(self)
        self._follow_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._follow_timer.setInterval(max(16, int(settings.follow_interval * 1000)))
        self._follow_timer.timeout.connect(self._follow_wechat_window)
        self._profile_refresh_timer = QTimer(self)
        self._profile_refresh_timer.setInterval(10 * 60 * 1000)
        self._profile_refresh_timer.timeout.connect(self._schedule_profile_refresh)
        self._first_show_logged = False

    async def run(self) -> None:
        show_started = time.perf_counter()
        self._show_started_at = show_started
        self.show()
        logger.info("工作台启动计时: window.show returned elapsed=%.3fs", time.perf_counter() - show_started)
        QTimer.singleShot(0, self._log_first_paint)
        self._follow_wechat_window()
        self._schedule_all_avatar_refreshes()
        if self.conversation_loader is not None:
            self._schedule_profile_refresh()
            self._profile_refresh_timer.start()
        if self.settings.mode == "docked":
            self._follow_timer.start()
        await self._closed_event.wait()

    def _log_first_paint(self) -> None:
        if self._first_show_logged:
            return
        self._first_show_logged = True
        elapsed = (
            time.perf_counter() - self._show_started_at
            if self._show_started_at is not None
            else 0.0
        )
        logger.info(
            "工作台启动计时: first Qt event-loop paint completed elapsed=%.3fs",
            elapsed,
        )

    def post_update(self, update: CompanionUpdate) -> None:
        self._signal_bus.updated.emit(update)

    def _schedule_all_avatar_refreshes(self) -> None:
        for item in self.conversations:
            self._schedule_avatar_refresh(item)

    def _schedule_avatar_refresh(self, item: ConversationItem) -> None:
        if self.avatar_cache is None or not item.avatar_url:
            return
        task = asyncio.create_task(self._refresh_avatar(item))
        self._avatar_tasks.add(task)
        task.add_done_callback(self._avatar_tasks.discard)

    async def _refresh_avatar(self, item: ConversationItem) -> None:
        assert self.avatar_cache is not None
        before = self.avatar_cache.cached_path(item)
        before_stamp = _path_stamp(before)
        path = await self.avatar_cache.ensure(item)
        if path is not None and (
            before is None or _path_stamp(path) != before_stamp
        ):
            self._signal_bus.avatar_updated.emit(item.binding_key)

    def _handle_avatar_updated(self, binding_key: tuple[str, str, str]) -> None:
        index = next(
            (
                position
                for position, item in enumerate(self.conversations)
                if item.binding_key == binding_key
            ),
            -1,
        )
        self._refresh_conversation_row(index)

    def _schedule_profile_refresh(self) -> None:
        if self.conversation_loader is None:
            return
        if (
            self._profile_refresh_task is not None
            and not self._profile_refresh_task.done()
        ):
            return
        task = asyncio.create_task(self._reload_conversation_profiles())
        self._profile_refresh_task = task
        task.add_done_callback(self._profile_refresh_finished)

    def _profile_refresh_finished(self, task: asyncio.Task[None]) -> None:
        if self._profile_refresh_task is task:
            self._profile_refresh_task = None

    async def _reload_conversation_profiles(self) -> None:
        if self.conversation_loader is None:
            return
        try:
            profiles = await self.conversation_loader()
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            logger.warning("刷新微信会话头像资料失败", exc_info=True)
            return

        positions = {
            item.binding_key: index for index, item in enumerate(self.conversations)
        }
        selected = self._selected_item()
        selected_key = selected.binding_key if selected is not None else None
        for profile in profiles:
            index = positions.get(profile.binding_key)
            if index is None:
                continue
            if self.conversations[index] != profile:
                self.conversations[index] = profile
                self._refresh_conversation_row(index)
            self._schedule_avatar_refresh(profile)
        if selected_key is not None:
            selected_index = positions.get(selected_key)
            if selected_index is not None:
                self._selected_index = selected_index
                self.conversation_list.setCurrentRow(selected_index)
                self._render_selected()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.launcher.shutdown()
        if self.settings.mode == "independent":
            try:
                self.save_geometry(self._current_geometry())
            except (OSError, RuntimeError, TypeError, ValueError):
                logger.exception("保存伴随窗口位置失败")
        self._follow_timer.stop()
        self._profile_refresh_timer.stop()
        if self._profile_refresh_task is not None:
            self._profile_refresh_task.cancel()
        for task in tuple(self._avatar_tasks):
            task.cancel()
        if not self._closed_event.is_set():
            self._closed_event.set()
        event.accept()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.visibility_changed.emit(not self.isMinimized())

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_responsive_layout()
        QTimer.singleShot(0, self._constrain_message_cards)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.visibility_changed.emit(self.isVisible() and not self.isMinimized())
        settings = getattr(self, "settings", None)
        if (
            event.type() == QEvent.Type.WindowStateChange
            and settings is not None
            and settings.mode == "docked"
            and self.isMinimized()
            and not self._collapsed
        ):
            QTimer.singleShot(0, self._collapse_minimized_to_launcher)

    def _collapse_minimized_to_launcher(self) -> None:
        if (
            self.settings.mode == "docked"
            and self.isMinimized()
            and not self._collapsed
        ):
            self._collapse_to_launcher()

    def nativeEvent(self, event_type, message):
        result = super().nativeEvent(event_type, message)
        settings = getattr(self, "settings", None)
        if os.name != "nt" or settings is None:
            return result
        try:
            message_address = int(message)
            resize_hit = self._windows_resize_hit(message_address)
            if resize_hit is not None:
                return True, resize_hit
            if settings.mode == "docked":
                self._handle_windows_move_message(message_address)
        except (
            AttributeError,
            ctypes.ArgumentError,
            OSError,
            OverflowError,
            TypeError,
            ValueError,
        ):
            logger.debug("忽略无法解析的 Windows 窗口移动消息", exc_info=True)
        return result

    def _windows_resize_hit(self, message_address: int) -> int | None:
        if not message_address:
            return None
        native_message = wintypes.MSG.from_address(message_address)
        if native_message.message != _WM_NCHITTEST:
            return None
        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(
            wintypes.HWND(int(self.winId())), ctypes.byref(rect)
        ):
            return None
        lparam = int(native_message.lParam)
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        border = max(5, round(5 * self.devicePixelRatioF()))
        return calculate_resize_hit(
            WindowRect(rect.left, rect.top, rect.right, rect.bottom),
            x,
            y,
            border,
        )

    def _handle_windows_move_message(self, message_address: int) -> None:
        if not message_address:
            return
        native_message = wintypes.MSG.from_address(message_address)
        if native_message.message == _WM_ENTERSIZEMOVE:
            self._begin_group_move()
        elif native_message.message == _WM_MOVING and native_message.lParam:
            rect = ctypes.cast(
                int(native_message.lParam), ctypes.POINTER(wintypes.RECT)
            ).contents
            self._move_wechat_with_companion(
                WindowRect(rect.left, rect.top, rect.right, rect.bottom)
            )
        elif native_message.message == _WM_EXITSIZEMOVE:
            self._end_group_move()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.title_bar = CompanionTitleBar(
            self._toggle_settings,
            self.showMinimized,
            self.close,
            root,
        )
        self.settings_button = self.title_bar.settings_button
        self.minimize_button = self.title_bar.minimize_button
        self.close_button = self.title_bar.close_button
        root_layout.addWidget(self.title_bar)

        content = QWidget(root)
        content.setObjectName("workbenchContent")
        outer = QHBoxLayout(content)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(_EXPANDED_SIDEBAR_WIDTH)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(10, 12, 10, 10)
        sidebar_layout.setSpacing(8)
        self.app_title = QLabel("Agent 工作台")
        self.app_title.setObjectName("appTitle")
        self.section_label = QLabel("白名单会话")
        self.section_label.setObjectName("sectionLabel")
        self.conversation_list = QListWidget()
        self.conversation_list.setObjectName("conversationList")
        self.conversation_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.conversation_list.currentRowChanged.connect(self._select_conversation)
        sidebar_layout.addWidget(self.app_title)
        sidebar_layout.addWidget(self.section_label)
        sidebar_layout.addWidget(self.conversation_list, 1)
        outer.addWidget(self.sidebar)

        main = QWidget()
        self.main_panel = main
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 11, 12, 11)
        header_layout.setSpacing(10)
        heading = QVBoxLayout()
        heading.setSpacing(3)
        self.title_label = QLabel("未选择会话")
        self.title_label.setObjectName("conversationTitle")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.session_label = QLabel("")
        self.session_label.setObjectName("mutedLabel")
        heading.addWidget(self.title_label)
        heading.addWidget(self.session_label)
        header_layout.addLayout(heading, 1)
        self.clear_header_button = QPushButton("清空")
        self.clear_header_button.setObjectName("headerDangerButton")
        self.clear_header_button.setAccessibleName("清空当前会话本地消息")
        self.clear_header_button.setToolTip("清空当前会话的本地消息")
        self.clear_header_button.clicked.connect(self._clear_local_messages)
        self.provider_badge = QLabel("")
        self.provider_badge.setObjectName("agentBadge")
        self.reply_switch = AnimatedSwitch()
        self.reply_switch.setToolTip("开启或关闭当前会话回复")
        self.reply_switch.setAccessibleName("开启当前会话回复")
        self.reply_switch.toggled.connect(self._header_reply_changed)
        header_layout.addWidget(self.clear_header_button)
        header_layout.addWidget(self.provider_badge)
        header_layout.addWidget(self.reply_switch)
        main_layout.addWidget(header)

        self.status_label = QLabel("等待微信消息")
        self.status_label.setObjectName("statusBanner")
        self.status_label.setProperty("state", "info")
        main_layout.addWidget(self.status_label)

        self._build_queue_panel(main_layout)

        self.timeline_scroll = QScrollArea()
        self.timeline_scroll.setObjectName("timeline")
        self.timeline_scroll.setWidgetResizable(True)
        self.timeline_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.timeline_content = QWidget()
        self.timeline_content.setObjectName("timelineContent")
        self.timeline_content.setMinimumWidth(0)
        self.timeline_content.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.timeline_layout = QVBoxLayout(self.timeline_content)
        self.timeline_layout.setContentsMargins(16, 14, 16, 18)
        self.timeline_layout.setSpacing(10)
        self.timeline_scroll.setWidget(self.timeline_content)
        timeline_bar = self.timeline_scroll.verticalScrollBar()
        timeline_bar.valueChanged.connect(self._timeline_scroll_changed)
        timeline_bar.sliderMoved.connect(self._timeline_user_scrolled)
        timeline_bar.actionTriggered.connect(self._timeline_user_scrolled)
        timeline_bar.rangeChanged.connect(self._timeline_range_changed)
        self.timeline_scroll.viewport().installEventFilter(self)
        self.new_messages_button = QPushButton("")
        self.new_messages_button.setObjectName("newMessagesBanner")
        self.new_messages_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_messages_button.setAccessibleName("加载新消息")
        self.new_messages_button.clicked.connect(self._flush_pending_timeline)
        self.new_messages_button.hide()
        main_layout.addWidget(self.new_messages_button)
        main_layout.addWidget(self.timeline_scroll, 1)
        outer.addWidget(main, 1)

        self.settings_panel = self._build_settings_panel()
        outer.addWidget(self.settings_panel)
        self.settings_panel.hide()
        root_layout.addWidget(content, 1)
        self.setCentralWidget(root)

    def _build_queue_panel(self, parent_layout: QVBoxLayout) -> None:
        """Build the always-visible queue strip for the selected conversation."""
        self.queue_panel = QFrame()
        self.queue_panel.setObjectName("queuePanel")
        self.queue_panel.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        panel_layout = QVBoxLayout(self.queue_panel)
        panel_layout.setContentsMargins(12, 7, 12, 7)
        panel_layout.setSpacing(5)

        heading = QHBoxLayout()
        heading.setSpacing(8)
        self.queue_title = QLabel("等待队列")
        self.queue_title.setObjectName("queueTitle")
        self.queue_count = QLabel("0 条")
        self.queue_count.setObjectName("queueCount")
        self.queue_hint = QLabel("处理中的消息不会出现在这里")
        self.queue_hint.setObjectName("mutedLabel")
        heading.addWidget(self.queue_title)
        heading.addWidget(self.queue_count)
        heading.addWidget(self.queue_hint, 1)
        panel_layout.addLayout(heading)

        self.queue_scroll = QScrollArea()
        self.queue_scroll.setObjectName("queueScroll")
        self.queue_scroll.setWidgetResizable(True)
        self.queue_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.queue_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.queue_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.queue_scroll.setMaximumHeight(112)
        self.queue_items = QWidget()
        self.queue_items.setObjectName("queueItems")
        self.queue_items_layout = QVBoxLayout(self.queue_items)
        self.queue_items_layout.setContentsMargins(0, 0, 0, 0)
        self.queue_items_layout.setSpacing(4)
        self.queue_scroll.setWidget(self.queue_items)
        panel_layout.addWidget(self.queue_scroll)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 1, 0, 0)
        footer.addStretch(1)
        self.queue_clear_button = QPushButton("清空等待中")
        self.queue_clear_button.setObjectName("queueClearButton")
        self.queue_clear_button.setFixedHeight(26)
        self.queue_clear_button.setToolTip("清空当前会话所有尚未开始处理的消息")
        self.queue_clear_button.clicked.connect(self._clear_queued_messages)
        footer.addWidget(self.queue_clear_button)
        panel_layout.addLayout(footer)
        parent_layout.addWidget(self.queue_panel)
        self._update_queue_panel(None)

    def _build_settings_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("settingsPanel")
        panel.setFixedWidth(270)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("会话设置")
        title.setObjectName("conversationTitle")
        close_button = QPushButton("关闭")
        close_button.setAccessibleName("关闭会话设置")
        close_button.clicked.connect(self._close_settings)
        header.addWidget(title, 1)
        header.addWidget(close_button)
        layout.addLayout(header)

        hint = QLabel(
            "会话选项只作用于当前聊天；前台备用发送作用于当前微信账号。"
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.settings_reply_switch = QCheckBox("开启回复")
        self.settings_reply_switch.setAccessibleName("开启当前会话回复")
        self.settings_reply_switch.toggled.connect(self._settings_reply_changed)
        self.settings_image_switch = QCheckBox("允许 Agent 发送图片")
        self.settings_image_switch.setAccessibleName("允许当前会话的 Agent 发送图片")
        self.settings_image_switch.setToolTip(
            "仅允许发送当前 Session 工作目录内的本地图片；需要开启前台备用发送"
        )
        self.settings_image_switch.toggled.connect(self._settings_image_changed)
        self.settings_history_switch = QCheckBox("加载微信历史消息")
        self.settings_history_switch.setAccessibleName("加载微信历史消息")
        self.settings_history_switch.toggled.connect(self._history_changed)
        layout.addWidget(self.settings_reply_switch)
        layout.addWidget(self.settings_image_switch)
        layout.addWidget(self.settings_history_switch)

        limit_label = QLabel("历史消息条数")
        limit_label.setObjectName("sectionLabel")
        self.history_limit = QSpinBox()
        self.history_limit.setRange(1, 500)
        self.history_limit.setAccessibleName("历史消息条数")
        self.history_limit.editingFinished.connect(self._history_limit_changed)
        layout.addWidget(limit_label)
        layout.addWidget(self.history_limit)

        local_history_hint = QLabel(
            "Agent 数据库历史会始终加载，不受微信历史开关影响。"
        )
        local_history_hint.setObjectName("mutedLabel")
        local_history_hint.setWordWrap(True)
        layout.addWidget(local_history_hint)
        self.clear_messages_button = QPushButton("清空本地消息")
        self.clear_messages_button.setObjectName("dangerButton")
        self.clear_messages_button.setAccessibleName("清空当前会话本地消息")
        self.clear_messages_button.setToolTip(
            "删除当前会话在 Agent Bridge 数据库中的消息，不删除微信聊天记录"
        )
        self.clear_messages_button.clicked.connect(self._clear_local_messages)
        layout.addWidget(self.clear_messages_button)

        sender_separator = QFrame()
        sender_separator.setFrameShape(QFrame.Shape.HLine)
        sender_separator.setObjectName("settingsSeparator")
        layout.addWidget(sender_separator)
        self.foreground_fallback_switch = QCheckBox("允许前台备用发送")
        self.foreground_fallback_switch.setAccessibleName("允许微信前台备用发送")
        self.foreground_fallback_switch.setToolTip(
            "后台发送不可用时，允许通过 WeChatMCP 前台通道完成发送"
        )
        self.foreground_fallback_switch.toggled.connect(
            self._foreground_fallback_changed
        )
        layout.addWidget(self.foreground_fallback_switch)
        fallback_hint = QLabel(
            "后台发送不可用时，优先使用 WeChatMCP 前台通道；"
            "可能短暂切换微信并占用键盘或鼠标。"
        )
        fallback_hint.setObjectName("mutedLabel")
        fallback_hint.setWordWrap(True)
        layout.addWidget(fallback_hint)
        layout.addStretch(1)

        safety = QLabel("回复默认关闭。开启后，之后收到的消息才会进入对应 Agent Session。")
        safety.setObjectName("mutedLabel")
        safety.setWordWrap(True)
        layout.addWidget(safety)
        return panel

    def _refresh_conversation_list(self) -> None:
        selected = self._selected_index
        blocker = QSignalBlocker(self.conversation_list)
        self.conversation_list.clear()
        for index in range(len(self.conversations)):
            list_item = QListWidgetItem()
            list_item.setSizeHint(QSize(0, 58))
            self.conversation_list.addItem(list_item)
            self._refresh_conversation_row(index)
        if 0 <= selected < len(self.conversations):
            self.conversation_list.setCurrentRow(selected)
        del blocker

    def _refresh_conversation_row(self, index: int) -> None:
        if not 0 <= index < len(self.conversations):
            return
        list_item = self.conversation_list.item(index)
        if list_item is None:
            return
        conversation = self.conversations[index]
        preferences = self.controller.preferences(conversation)
        row = ConversationRow(
            conversation,
            self.controller.current_provider(conversation),
            preferences.reply_enabled,
            conversation.conversation_id in self._unread,
            avatar_path=(
                self.avatar_cache.cached_path(conversation)
                if self.avatar_cache is not None
                else None
            ),
        )
        row.set_compact(self._compact_sidebar)
        list_item.setSizeHint(QSize(0, 58))
        previous = self.conversation_list.itemWidget(list_item)
        if previous is not None:
            self.conversation_list.removeItemWidget(list_item)
            previous.hide()
            previous.deleteLater()
        self.conversation_list.setItemWidget(list_item, row)

    def _select_conversation(self, index: int) -> None:
        if not 0 <= index < len(self.conversations):
            return
        self._selected_index = index
        item = self.conversations[index]
        self._unread.discard(item.conversation_id)
        self._pending_timeline_updates.pop(item.conversation_id, None)
        self._update_new_messages_banner()
        self._refresh_conversation_row(index)
        self._load_conversation_history(item)
        self._render_selected()

    def _selected_item(self) -> ConversationItem | None:
        if not 0 <= self._selected_index < len(self.conversations):
            return None
        return self.conversations[self._selected_index]

    def _render_selected(self, *, scroll_to_bottom: bool = True) -> None:
        item = self._selected_item()
        if item is None:
            self.title_label.setText("没有白名单会话")
            self.session_label.setText("请在 config.yaml 中配置白名单")
            self.provider_badge.hide()
            self.reply_switch.setEnabled(False)
            self.clear_header_button.setEnabled(False)
            self.settings_button.setEnabled(False)
            self.settings_panel.hide()
            self.settings_button.setChecked(False)
            self._update_queue_panel(None)
            self._render_timeline((), scroll_to_bottom=scroll_to_bottom)
            return

        preferences = self.controller.preferences(item)
        provider = self.controller.current_provider(item)
        session_status = self.controller.current_session_status(item)
        kind = "群聊" if item.conversation_type.value == "group" else "私聊"
        self.title_label.setText(item.display_name)
        self.session_label.setText(f"{kind} · Session {session_status}")
        self.provider_badge.setText(provider.upper())
        available = self.controller.provider_available(item)
        self.provider_badge.setEnabled(available)
        self.provider_badge.setStyleSheet("" if available else "color: #64748B; background: #E2E8F0;")
        self.provider_badge.setToolTip("" if available else
                                      "此 Agent 未启用或本地依赖不可用；原 session 保留，不自动切换。")
        self.provider_badge.show()
        self.reply_switch.setEnabled(available)
        self.settings_reply_switch.setEnabled(available)
        self.clear_header_button.setEnabled(True)
        self._update_queue_panel(item.conversation_id)
        self.settings_button.setEnabled(True)

        blockers = (
            QSignalBlocker(self.reply_switch),
            QSignalBlocker(self.settings_reply_switch),
            QSignalBlocker(self.settings_image_switch),
            QSignalBlocker(self.settings_history_switch),
            QSignalBlocker(self.history_limit),
            QSignalBlocker(self.foreground_fallback_switch),
        )
        self.reply_switch.setChecked(preferences.reply_enabled)
        self.settings_reply_switch.setChecked(preferences.reply_enabled)
        self.settings_image_switch.setChecked(preferences.send_images_enabled)
        self.settings_history_switch.setChecked(preferences.load_history)
        self.history_limit.setValue(preferences.history_limit)
        self.history_limit.setEnabled(preferences.load_history)
        self.foreground_fallback_switch.setChecked(
            self.controller.foreground_fallback_enabled()
        )
        del blockers
        self._render_timeline(
            self.controller.timeline(item.conversation_id),
            scroll_to_bottom=scroll_to_bottom,
        )

    def _update_queue_panel(self, conversation_id: str | None) -> None:
        jobs = (
            self.controller.queued_jobs(conversation_id)
            if conversation_id is not None
            else ()
        )
        _clear_layout(self.queue_items_layout)
        self.queue_count.setText(f"{len(jobs)} 条")
        self.queue_clear_button.setEnabled(bool(jobs))
        # Keep the chat header compact when there is nothing actionable.  The
        # panel is shown again automatically on the queued notification.
        self.queue_panel.setVisible(bool(jobs))
        if conversation_id is None:
            self.queue_panel.setEnabled(False)
            self.queue_hint.setText("选择会话后显示等待中的消息")
            empty = QLabel("暂无会话")
            empty.setObjectName("queueEmpty")
            self.queue_items_layout.addWidget(empty)
            return

        self.queue_panel.setEnabled(True)
        self.queue_hint.setText("处理中的消息不会出现在这里")
        if not jobs:
            empty = QLabel("暂无等待消息")
            empty.setObjectName("queueEmpty")
            self.queue_items_layout.addWidget(empty)
            return

        for job_id, text in jobs:
            row = QWidget()
            row.setObjectName("queueItem")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 3, 6, 3)
            row_layout.setSpacing(7)
            label = QLabel(text or "（无文本消息）")
            label.setObjectName("queueItemText")
            label.setWordWrap(True)
            label.setMaximumHeight(38)
            delete_button = QPushButton("删除")
            delete_button.setObjectName("queueDeleteButton")
            delete_button.setFixedHeight(24)
            delete_button.setToolTip("删除这条等待中的消息")
            delete_button.clicked.connect(
                lambda _checked=False, value=job_id: self._remove_queued_message(value)
            )
            row_layout.addWidget(label, 1)
            row_layout.addWidget(delete_button)
            self.queue_items_layout.addWidget(row)
        self.queue_items_layout.addStretch(1)

    def _queue_conversation_id(self) -> str | None:
        item = self._selected_item()
        return item.conversation_id if item is not None else None

    def _remove_queued_message(self, job_id: str) -> None:
        conversation_id = self._queue_conversation_id()
        if conversation_id is None:
            return
        task = asyncio.create_task(
            self.controller.remove_queued_job(conversation_id, job_id)
        )
        task.add_done_callback(self._queue_operation_finished)

    def _clear_queued_messages(self) -> None:
        conversation_id = self._queue_conversation_id()
        if conversation_id is None:
            return
        task = asyncio.create_task(
            self.controller.clear_queued_jobs(conversation_id)
        )
        task.add_done_callback(self._queue_operation_finished)

    def _queue_operation_finished(self, task: asyncio.Task[object]) -> None:
        try:
            task.result()
        except Exception:
            logger.exception("工作台队列操作失败")
        conversation_id = self._queue_conversation_id()
        self._update_queue_panel(conversation_id)

    def _render_timeline(
        self,
        entries: tuple[TimelineEntry, ...],
        *,
        scroll_to_bottom: bool = True,
    ) -> None:
        if scroll_to_bottom:
            self._timeline_follow_bottom = True
        else:
            self._cancel_timeline_bottom_follow()
        previous_scroll_value = self.timeline_scroll.verticalScrollBar().value()
        _clear_layout(self.timeline_layout)
        self._message_cards.clear()
        self._timeline_entries = {entry.entry_id: entry for entry in entries}

        if not entries:
            self.timeline_layout.addStretch(1)
            empty_title = QLabel("本次运行尚未收到消息")
            empty_title.setObjectName("emptyTitle")
            empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_hint = QLabel("保持窗口打开，白名单会话的新消息会显示在这里。")
            empty_hint.setObjectName("mutedLabel")
            empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_hint.setWordWrap(True)
            self.timeline_layout.addWidget(empty_title)
            self.timeline_layout.addWidget(empty_hint)
            self.timeline_layout.addStretch(1)
            self._update_new_messages_banner()
            return

        had_history = any(entry.history_source is not None for entry in entries)
        previous_source: str | None = None
        for entry in entries:
            source = entry.history_source or "realtime"
            if source != previous_source and (had_history or source != "realtime"):
                self._add_divider(
                    {
                        "local": "Agent 数据库历史",
                        "wechat": "微信历史 · 仅展示",
                        "realtime": "本次运行",
                    }.get(source, "历史消息")
                )
            previous_source = source
            row = QHBoxLayout()
            display_entry = self._display_timeline_entry(entry)
            card = MessageCard(
                display_entry,
                lambda delivery_id: asyncio.create_task(
                    self.controller.retry_delivery(delivery_id)
                ),
                lambda delivery_id: asyncio.create_task(
                    self.controller.cancel_delivery(delivery_id)
                ),
                lambda delivery_id: asyncio.create_task(
                    self.controller.resend_delivery(delivery_id)
                ),
                lambda conversation_id=entry.conversation_id: self._image_gallery(
                    conversation_id
                ),
            )
            card.typing_advanced.connect(
                lambda conversation_id=entry.conversation_id: self._typing_advanced(
                    conversation_id
                )
            )
            card.quote_requested.connect(self._jump_to_quoted_message)
            self._message_cards[entry.entry_id] = card
            if entry.direction == "outbound":
                row.addStretch(1)
                row.addWidget(card)
            elif entry.direction == "inbound":
                row.addWidget(card)
                row.addStretch(1)
            else:
                row.addWidget(card, 1)
            self.timeline_layout.addLayout(row)
        self.timeline_layout.addStretch(1)
        self._constrain_message_cards()
        # A native dock move can resize the top-level window after this render
        # without delivering a Qt resize event before the cards are laid out.
        # Re-constrain once the event loop has applied the new viewport width.
        QTimer.singleShot(0, self._constrain_message_cards)
        if scroll_to_bottom:
            # Move immediately using the current range, then repeat after Qt
            # has recalculated the newly selected conversation's layout.
            self._scroll_to_bottom(complete_schedule=False)
            self._schedule_scroll_to_bottom()
        else:
            QTimer.singleShot(
                0,
                lambda value=previous_scroll_value: self._restore_timeline_scroll(
                    value
                ),
            )
        self._update_new_messages_banner()

    def _image_gallery(self, conversation_id: str) -> tuple[tuple[str, str], ...]:
        """Return all local conversation images for the viewer's prev/next controls."""
        gallery: list[tuple[str, str]] = []
        for entry in self.controller.timeline(conversation_id):
            for attachment in entry.attachments:
                if attachment.kind != "image" or not attachment.path:
                    continue
                label = str(
                    attachment.metadata.get("alt")
                    or attachment.name
                    or "图片"
                )
                gallery.append((label, str(attachment.path)))
        return tuple(gallery)

    def _constrain_message_cards(self) -> None:
        """Keep timeline cards inside the viewport and avoid horizontal overflow."""
        viewport_width = self.timeline_scroll.viewport().width()
        if viewport_width <= 0:
            return
        max_width = max(180, viewport_width - 32)
        for card in self._message_cards.values():
            card.setMinimumWidth(0)
            card.setMaximumWidth(min(560, max_width))
            card.updateGeometry()
        self.timeline_layout.activate()
        self.timeline_content.updateGeometry()

    def _display_timeline_entry(self, entry: TimelineEntry) -> TimelineEntry:
        item = self._selected_item()
        if item is None or entry.quote is None:
            return entry
        sender_name = entry.quote.sender_name
        if sender_name == item.channel_account_id:
            sender_name = "我"
        elif sender_name == item.conversation_id:
            sender_name = item.display_name
        if sender_name == entry.quote.sender_name:
            return entry
        return replace(entry, quote=replace(entry.quote, sender_name=sender_name))

    def _jump_to_quoted_message(self, entry_id: str) -> None:
        source = self._timeline_entries.get(entry_id)
        if source is None or source.quote is None:
            return
        quote = source.quote
        target = None
        if quote.target_source_key:
            target = next(
                (
                    entry
                    for entry in self._timeline_entries.values()
                    if entry.source_key == quote.target_source_key
                ),
                None,
            )
        if target is None and quote.target_created_at is not None:
            candidates = [
                entry
                for entry in self._timeline_entries.values()
                if entry.entry_id != source.entry_id
                and int(entry.created_at.timestamp())
                == int(quote.target_created_at.timestamp())
                and entry.content.strip() == quote.content.strip()
            ]
            if len(candidates) > 1:
                named = [
                    entry
                    for entry in candidates
                    if entry.sender_name == quote.sender_name
                ]
                candidates = named if len(named) == 1 else []
            target = candidates[0] if len(candidates) == 1 else None
        card = self._message_cards.get(target.entry_id) if target is not None else None
        if card is None:
            self._set_status(
                "原消息已不在当前历史中",
                state="warning",
                sticky_seconds=3,
            )
            return
        self._cancel_timeline_bottom_follow()
        self.timeline_scroll.ensureWidgetVisible(card, 0, 32)
        card.flash_quote_target()

    def _add_divider(self, text: str) -> None:
        divider = QLabel(text)
        divider.setObjectName("divider")
        divider.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timeline_layout.addWidget(divider)

    def _scroll_to_bottom(self, *, complete_schedule: bool = True) -> None:
        bar = self.timeline_scroll.verticalScrollBar()
        self._timeline_follow_bottom = True
        if complete_schedule:
            self._timeline_force_bottom_pending = False
        self._programmatic_timeline_scroll = True
        try:
            bar.setValue(bar.maximum())
        finally:
            self._programmatic_timeline_scroll = False

    def _typing_advanced(self, conversation_id: str) -> None:
        if self._timeline_is_at_bottom():
            self._scroll_to_bottom()
        elif conversation_id == (
            self._selected_item().conversation_id if self._selected_item() else None
        ):
            self._mark_pending_timeline(conversation_id)

    def _restore_timeline_scroll(self, value: int) -> None:
        self._cancel_timeline_bottom_follow()
        bar = self.timeline_scroll.verticalScrollBar()
        self._programmatic_timeline_scroll = True
        try:
            bar.setValue(min(max(0, value), bar.maximum()))
        finally:
            self._programmatic_timeline_scroll = False

    def _timeline_is_at_bottom(self) -> bool:
        bar = self.timeline_scroll.verticalScrollBar()
        return bar.maximum() - bar.value() <= 8

    def _timeline_scroll_changed(self, _value: int) -> None:
        if self._programmatic_timeline_scroll:
            return
        if self._timeline_follow_bottom:
            self._scroll_to_bottom(complete_schedule=False)
            return
        if self._timeline_force_bottom_pending:
            return
        if self._timeline_is_at_bottom():
            self._flush_pending_timeline()
        else:
            self._timeline_scroll_schedule_generation += 1

    def _timeline_user_scrolled(self, _value: int) -> None:
        self._cancel_timeline_bottom_follow()

    def _cancel_timeline_bottom_follow(self) -> None:
        self._timeline_follow_bottom = False
        self._timeline_force_bottom_pending = False
        self._timeline_scroll_schedule_generation += 1

    def _timeline_range_changed(self, _minimum: int, _maximum: int) -> None:
        if not self._timeline_follow_bottom:
            return
        self._scroll_to_bottom(complete_schedule=False)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.timeline_scroll.viewport()
            and event.type() == QEvent.Type.Wheel
        ):
            self._timeline_user_scrolled(0)
        return super().eventFilter(watched, event)

    def _schedule_scroll_to_bottom(self) -> None:
        self._timeline_scroll_schedule_generation += 1
        generation = self._timeline_scroll_schedule_generation
        self._timeline_force_bottom_pending = True
        for delay in (0, 50, 150):
            QTimer.singleShot(
                delay,
                lambda generation=generation, delay=delay: self._run_scheduled_bottom_scroll(
                    generation, delay
                ),
            )

    def _run_scheduled_bottom_scroll(self, generation: int, delay: int) -> None:
        if generation != self._timeline_scroll_schedule_generation:
            return
        if not self._timeline_force_bottom_pending:
            return
        self._scroll_to_bottom(complete_schedule=delay == 150)

    def _mark_pending_timeline(self, conversation_id: str) -> None:
        entries = self.controller.timeline(conversation_id)
        visible_ids = set(self._message_cards)
        new_entries = sum(entry.entry_id not in visible_ids for entry in entries)
        current = self._pending_timeline_updates.get(conversation_id, 0)
        self._pending_timeline_updates[conversation_id] = max(
            current, new_entries, 1
        )
        self._update_new_messages_banner()

    def _update_new_messages_banner(self) -> None:
        item = self._selected_item()
        count = (
            self._pending_timeline_updates.get(item.conversation_id, 0)
            if item is not None
            else 0
        )
        if count:
            self.new_messages_button.setText(f"有 {count} 条新消息 · 点击加载")
            self.new_messages_button.show()
        else:
            self.new_messages_button.hide()

    def _flush_pending_timeline(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        if not self._pending_timeline_updates.get(item.conversation_id):
            return
        self._pending_timeline_updates.pop(item.conversation_id, None)
        self._render_selected(scroll_to_bottom=True)

    def _update_timeline_in_place(
        self, entries: tuple[TimelineEntry, ...]
    ) -> bool:
        entry_ids = tuple(entry.entry_id for entry in entries)
        if tuple(self._message_cards) != entry_ids:
            return False
        self._timeline_entries = {entry.entry_id: entry for entry in entries}
        for entry in entries:
            self._message_cards[entry.entry_id].update_entry(
                self._display_timeline_entry(entry)
            )
        self._schedule_scroll_to_bottom()
        return True

    def _header_reply_changed(self, checked: bool) -> None:
        self._update_preferences(reply_enabled=checked)

    def _settings_reply_changed(self, checked: bool) -> None:
        self._update_preferences(reply_enabled=checked)

    def _settings_image_changed(self, checked: bool) -> None:
        self._update_preferences(send_images_enabled=checked)

    def _history_changed(self, checked: bool) -> None:
        updated = self._update_preferences(load_history=checked)
        if updated and updated.load_history:
            item = self._selected_item()
            if item is not None:
                asyncio.create_task(self.controller.load_history(item))

    def _history_limit_changed(self) -> None:
        updated = self._update_preferences(history_limit=self.history_limit.value())
        if updated and updated.load_history:
            item = self._selected_item()
            if item is not None:
                asyncio.create_task(self.controller.load_history(item))

    def _clear_local_messages(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        answer = QMessageBox.question(
            self,
            "清空本地消息",
            f"确定清空“{item.display_name}”在 Agent Bridge 数据库中的消息吗？\n\n"
            "微信聊天记录、会话绑定和 Agent Session 不会被删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = self.controller.clear_local_messages(item)
        except (OSError, RuntimeError, KeyError, TypeError, ValueError) as error:
            self._set_status(
                f"清空失败：{type(error).__name__}",
                state="error",
                sticky_seconds=4,
            )
            return
        self._render_selected()
        self._set_status(
            f"已清空 {removed} 条本地消息",
            state="idle",
            sticky_seconds=4,
        )

    def _foreground_fallback_changed(self, checked: bool) -> None:
        try:
            self.controller.set_foreground_fallback_enabled(checked)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self._set_status(
                f"设置保存失败：{type(error).__name__}",
                state="error",
                sticky_seconds=4,
            )
            blocker = QSignalBlocker(self.foreground_fallback_switch)
            self.foreground_fallback_switch.setChecked(
                self.controller.foreground_fallback_enabled()
            )
            del blocker

    def _update_preferences(
        self,
        *,
        reply_enabled: bool | None = None,
        send_images_enabled: bool | None = None,
        load_history: bool | None = None,
        history_limit: int | None = None,
    ):
        item = self._selected_item()
        if item is None:
            return None
        try:
            updated = self.controller.update_preferences(
                item,
                reply_enabled=reply_enabled,
                send_images_enabled=send_images_enabled,
                load_history=load_history,
                history_limit=history_limit,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self._set_status(
                f"设置保存失败：{type(error).__name__}",
                state="error",
                sticky_seconds=4,
            )
            self._render_selected()
            return None
        return updated

    def _toggle_settings(self) -> None:
        visible = not self.settings_panel.isVisible()
        self.settings_panel.setVisible(visible)
        self.settings_button.setChecked(visible)
        self._update_responsive_layout()

    def _close_settings(self) -> None:
        self.settings_panel.hide()
        self.settings_button.setChecked(False)
        self.main_panel.show()
        self._update_responsive_layout()

    def _load_conversation_history(self, item: ConversationItem) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Some Qt-only callers construct the window without an asyncio loop.
            # Keep those callers synchronous while the bridge uses the async path.
            self.controller.load_local_history(item)
        else:
            loop.create_task(self.controller.load_local_history_async(item))
        if self.controller.preferences(item).load_history:
            asyncio.create_task(self.controller.load_history(item))

    def _handle_update(self, update: CompanionUpdate) -> None:
        if update.kind == "sender":
            self._sender_state = update.detail or "idle"
            self._render_sender_status()
            return
        if update.kind == "sender_settings":
            blocker = QSignalBlocker(self.foreground_fallback_switch)
            self.foreground_fallback_switch.setChecked(
                self.controller.foreground_fallback_enabled()
            )
            del blocker
            return
        if update.kind == "queue":
            if (
                update.conversation_id
                and self._selected_item() is not None
                and update.conversation_id == self._selected_item().conversation_id
            ):
                self._update_queue_panel(update.conversation_id)
            return
        selected = self._selected_item()
        if (
            update.kind == "timeline_stream"
            and selected is not None
            and update.conversation_id == selected.conversation_id
        ):
            entry = next(
                (
                    item
                    for item in self.controller.timeline(selected.conversation_id)
                    if item.entry_id == update.detail
                ),
                None,
            )
            card = self._message_cards.get(update.detail)
            if entry is not None and card is not None:
                if not self._timeline_is_at_bottom():
                    self._mark_pending_timeline(selected.conversation_id)
                    return
                card.update_entry(entry)
                QTimer.singleShot(0, self._scroll_to_bottom)
            return
        if (
            update.kind == "timeline"
            and update.conversation_id
            and (selected is None or update.conversation_id != selected.conversation_id)
        ):
            self._unread.add(update.conversation_id)
        if update.kind in {"preferences", "provider", "timeline"}:
            index = next(
                (
                    position
                    for position, item in enumerate(self.conversations)
                    if item.conversation_id == update.conversation_id
                ),
                -1,
            )
            self._refresh_conversation_row(index)
        if selected and update.conversation_id == selected.conversation_id:
            if update.kind == "timeline" and update.detail != "history":
                if not self._timeline_is_at_bottom():
                    self._mark_pending_timeline(selected.conversation_id)
                    return
            if update.kind == "timeline" and update.detail == "history":
                self._pending_timeline_updates.pop(
                    selected.conversation_id, None
                )
                self._update_new_messages_banner()
            entries = self.controller.timeline(selected.conversation_id)
            if (
                update.kind != "timeline"
                or not self._update_timeline_in_place(entries)
            ):
                self._render_selected(
                    scroll_to_bottom=(
                        update.kind == "timeline"
                        or self._timeline_is_at_bottom()
                    )
                )

    def _update_responsive_layout(self) -> None:
        compact = self.width() < _SIDEBAR_EXPANDED_BREAKPOINT
        if compact != self._compact_sidebar:
            self._compact_sidebar = compact
            self.sidebar.setFixedWidth(
                _COMPACT_SIDEBAR_WIDTH if compact else _EXPANDED_SIDEBAR_WIDTH
            )
            self.app_title.setText("AI" if compact else "Agent 工作台")
            self.app_title.setAlignment(
                Qt.AlignmentFlag.AlignCenter
                if compact
                else Qt.AlignmentFlag.AlignLeft
            )
            self.section_label.setVisible(not compact)
            self._refresh_conversation_list()
        if self.settings_panel.isVisible() and compact:
            self.main_panel.hide()
            self.settings_panel.setFixedWidth(
                max(270, self.width() - self.sidebar.width())
            )
        else:
            self.main_panel.show()
            self.settings_panel.setFixedWidth(270)

    def _apply_theme(self) -> None:
        system_dark = (
            self._app.styleHints().colorScheme() == Qt.ColorScheme.Dark
        )
        palette = resolve_theme(self.settings.theme, system_dark=system_dark)
        self._app.setStyleSheet(build_stylesheet(palette))
        self.reply_switch.set_theme_colors(
            off=palette.surface_alt,
            on=palette.success,
            knob=palette.surface,
            border=palette.border,
            focus=palette.primary,
        )
        self.setProperty("theme", palette.name)

    def _system_theme_changed(self, _scheme: Qt.ColorScheme) -> None:
        if self.settings.theme == "system":
            self._apply_theme()

    def _follow_wechat_window(self) -> None:
        if self.settings.mode == "independent":
            self._set_follow_status("独立窗口 · 正在监听")
            return
        if self._group_move_active:
            return
        wechat_handle = self.hwnd_provider()
        snapshot = self.probe.snapshot(wechat_handle)
        if self._collapsed:
            self._follow_collapsed_launcher(wechat_handle, snapshot)
            return
        if not snapshot.available or snapshot.rect is None:
            if self._withdrawn:
                self.show()
                self._withdrawn = False
            self._set_follow_status("微信不可用 · 等待恢复", state="warning")
            return
        if snapshot.minimized or not snapshot.visible:
            if not self._withdrawn:
                self.hide()
                self._withdrawn = True
            return
        if self._withdrawn:
            self.show()
            self._withdrawn = False
        self._bind_to_wechat(int(self.winId()), int(wechat_handle or 0))
        geometry = calculate_companion_geometry(snapshot.rect, self.settings)
        geometry_key = (
            f"{geometry.width}x{geometry.height}{geometry.x:+d}{geometry.y:+d}"
        )
        if geometry_key != self._last_follow_geometry:
            moved = self.mover.move(int(self.winId()), geometry)
            if not moved:
                self.setGeometry(
                    geometry.x, geometry.y, geometry.width, geometry.height
                )
            self._last_follow_geometry = geometry_key
        side = {
            "left": "左侧",
            "right": "右侧",
            "top": "上方",
            "bottom": "下方",
        }[self.settings.side]
        self._set_follow_status(f"贴靠微信{side} · 正在监听")

    def _follow_collapsed_launcher(
        self, wechat_handle: int | None, snapshot: WindowSnapshot
    ) -> None:
        if (
            not snapshot.available
            or snapshot.rect is None
            or snapshot.minimized
            or not snapshot.visible
        ):
            self.launcher.hide()
            return
        was_hidden = not self.launcher.isVisible()
        if was_hidden:
            self.launcher.show()
        launcher_handle = int(self.launcher.winId())
        self._bind_to_wechat(launcher_handle, int(wechat_handle or 0))
        geometry = calculate_launcher_geometry(
            snapshot.client_rect or snapshot.rect
        )
        geometry_key = (
            f"{geometry.width}x{geometry.height}{geometry.x:+d}{geometry.y:+d}"
        )
        if was_hidden or geometry_key != self._last_launcher_geometry:
            moved = self.mover.move(launcher_handle, geometry)
            if not moved:
                self.launcher.setGeometry(
                    geometry.x, geometry.y, geometry.width, geometry.height
                )
            self._last_launcher_geometry = geometry_key

    def _bind_to_wechat(self, window_handle: int, wechat_handle: int) -> None:
        owner_bound = self.owner.bind(window_handle, wechat_handle)
        if owner_bound:
            self._owner_binding_warning = False
        elif not self._owner_binding_warning:
            logger.warning("Agent 工作台无法绑定微信窗口层级，将继续位置跟随")
            self._owner_binding_warning = True

    def _collapse_to_launcher(self) -> None:
        if self._collapsed:
            return
        self._collapsed = True
        self._last_launcher_geometry = ""
        self.hide()
        self._follow_wechat_window()

    def _expand_from_launcher(self) -> None:
        self.launcher.hide()
        self._collapsed = False
        self._withdrawn = False
        self._last_follow_geometry = ""
        self.showNormal()
        self._follow_wechat_window()
        if self.isVisible():
            self.raise_()
            self.activateWindow()

    def _begin_group_move(self) -> None:
        if self.settings.mode != "docked":
            return
        self._group_move_active = True
        self._group_move_origin = None
        wechat_handle = int(self.hwnd_provider() or 0)
        if not wechat_handle:
            return
        companion_snapshot = self.probe.snapshot(int(self.winId()))
        wechat_snapshot = self.probe.snapshot(wechat_handle)
        if (
            not companion_snapshot.available
            or companion_snapshot.rect is None
            or not wechat_snapshot.available
            or wechat_snapshot.rect is None
            or wechat_snapshot.minimized
            or not wechat_snapshot.visible
        ):
            return
        self._group_move_origin = _GroupMoveOrigin(
            companion_snapshot.rect,
            wechat_snapshot.rect,
            wechat_handle,
        )

    def _move_wechat_with_companion(self, proposed: WindowRect) -> None:
        origin = self._group_move_origin
        if not self._group_move_active or origin is None:
            return
        dx = proposed.left - origin.companion.left
        dy = proposed.top - origin.companion.top
        geometry = WindowGeometry(
            origin.wechat.width,
            origin.wechat.height,
            origin.wechat.left + dx,
            origin.wechat.top + dy,
        )
        if not self.mover.move(origin.wechat_handle, geometry):
            logger.warning("拖动 Agent 工作台时无法同步移动微信")
            self._group_move_origin = None

    def _end_group_move(self) -> None:
        if not self._group_move_active:
            return
        self._group_move_active = False
        self._group_move_origin = None
        self._last_follow_geometry = ""

    def _set_follow_status(self, text: str, *, state: str = "info") -> None:
        if self._sender_state not in {"idle", "sent", "cancelled"}:
            self._render_sender_status()
            return
        if time.monotonic() < self._status_override_until:
            return
        if (
            self.status_label.text() == text
            and self.status_label.property("state") == state
        ):
            return
        self._set_status(text, state=state)

    def _render_sender_status(self) -> None:
        text, state = {
            "idle": ("后台发送空闲", "info"),
            "queued": ("消息已进入后台发送队列", "info"),
            "waiting_for_idle": ("等待你停止操作后自动发送", "warning"),
            "sending": ("正在静默发送", "info"),
            "retrying": ("静默发送失败，正在重试", "warning"),
            "failed": ("后台发送失败", "error"),
            "expired": ("排队消息已过期", "error"),
            "unavailable": ("当前微信不支持静默发送", "error"),
            "foreground_unavailable": ("前台备用发送未授权", "error"),
            "foreground_sending": ("正在通过前台备用通道发送", "warning"),
            "foreground_sent": ("已发送 · 桌面状态已恢复", "info"),
            "delivery_unknown": ("发送结果未知 · 已停止自动重试", "error"),
            "cancelled": ("排队消息已取消", "info"),
        }.get(self._sender_state, ("后台发送空闲", "info"))
        self._set_status(text, state=state)

    def _set_status(
        self,
        text: str,
        *,
        state: str = "info",
        sticky_seconds: float = 0,
    ) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("state", state)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        if sticky_seconds:
            self._status_override_until = time.monotonic() + sticky_seconds

    def _restore_geometry(self, value: str | None) -> None:
        if not value:
            return
        match = _GEOMETRY_PATTERN.fullmatch(value)
        if match is None:
            logger.warning("忽略无效的伴随窗口位置: %s", value)
            return
        width, height, x, y = (int(part) for part in match.groups())
        self.setGeometry(x, y, width, height)

    def _current_geometry(self) -> str:
        geometry = self.geometry()
        return (
            f"{geometry.width()}x{geometry.height()}"
            f"{geometry.x():+d}{geometry.y():+d}"
        )


def _clear_layout(layout) -> None:
    while layout.count():
        layout_item = layout.takeAt(0)
        widget = layout_item.widget()
        if widget is not None:
            widget.hide()
            widget.deleteLater()
            continue
        child_layout = layout_item.layout()
        if child_layout is not None:
            _clear_layout(child_layout)
            child_layout.deleteLater()


def _path_stamp(path) -> tuple[int, int] | None:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size
