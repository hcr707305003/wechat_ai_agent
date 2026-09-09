from __future__ import annotations

import asyncio
import ctypes
import os
from ctypes import wintypes
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QAbstractAnimation, QSignalBlocker, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

from agent_bridge.channels.wechat import WeChatCompanionSettings
from agent_bridge.companion.controller import CompanionController
from agent_bridge.companion.follower import WindowRect, WindowSnapshot
from agent_bridge.companion.models import (
    ConversationItem,
    QuotePreview,
    TimelineEntry,
)
from agent_bridge.companion.qt_widgets import ConversationRow, ImageViewerDialog
from agent_bridge.companion.qt_window import (
    AnimatedSwitch,
    CompanionLauncher,
    MessageCard,
    WeChatCompanionWindow,
    calculate_resize_hit,
)
from agent_bridge.models import (
    AgentProgressUpdate,
    Attachment,
    ChannelTarget,
    ContentType,
    ConversationType,
    UnifiedMessage,
)
from agent_bridge.senders.wechat import SenderUpdate
from agent_bridge.sessions.repository import SQLiteRepository


@pytest.fixture(scope="session")
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


def conversation(
    conversation_id: str = "friend",
    display_name: str = "工藤新一",
) -> ConversationItem:
    return ConversationItem(
        "wechat",
        "bot",
        conversation_id,
        ConversationType.PRIVATE,
        display_name,
    )


def incoming(message_id: str, conversation_id: str = "friend") -> UnifiedMessage:
    return UnifiedMessage(
        channel="wechat",
        channel_account_id="bot",
        conversation_id=conversation_id,
        conversation_type=ConversationType.PRIVATE,
        sender_id=conversation_id,
        sender_name="工藤新一",
        message_id=message_id,
        content=f"message-{message_id}",
        metadata={"is_self": False},
    )


def make_controller(repository: SQLiteRepository):
    history_calls: list[tuple[str, int]] = []

    async def dispatch(_message: UnifiedMessage) -> None:
        return None

    async def load_history(conversation_id: str, limit: int):
        history_calls.append((conversation_id, limit))
        return [incoming("history", conversation_id)]

    return CompanionController(repository, dispatch, load_history, "codex"), history_calls


def make_window(
    controller: CompanionController,
    conversations: list[ConversationItem],
    *,
    settings: WeChatCompanionSettings | None = None,
    probe=None,
    mover=None,
    owner=None,
    hwnd_provider=None,
    avatar_cache=None,
    conversation_loader=None,
) -> WeChatCompanionWindow:
    return WeChatCompanionWindow(
        settings or WeChatCompanionSettings(mode="independent"),
        controller,
        conversations,
        hwnd_provider or (lambda: 101),
        lambda: None,
        lambda _value: None,
        probe=probe,
        mover=mover,
        owner=owner,
        avatar_cache=avatar_cache,
        conversation_loader=conversation_loader,
    )


@pytest.mark.asyncio
async def test_renders_messages_and_tracks_unread(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(
        controller,
        [conversation("friend", "工藤新一"), conversation("other", "灰原哀")],
    )
    window.show()
    qt_app.processEvents()

    await controller.handle(incoming("one", "friend"))
    await controller.handle(incoming("three", "friend"))
    await controller.handle(incoming("two", "other"))
    qt_app.processEvents()

    visible_cards = [card for card in window.findChildren(MessageCard) if card.isVisible()]
    assert len(visible_cards) == 2
    assert any("message-one" in label.text() for label in visible_cards[0].findChildren(type(window.title_label)))
    visible_rows = [row for row in window.findChildren(ConversationRow) if row.isVisible()]
    assert len(visible_rows) == 2
    assert "other" in window._unread

    window.conversation_list.setCurrentRow(1)
    qt_app.processEvents()
    assert "other" not in window._unread
    assert window.title_label.text() == "灰原哀"
    window.close()
    repository.close()


@pytest.mark.asyncio
async def test_switching_conversation_reflows_narrow_timeline_cards(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(
        controller,
        [conversation("friend"), conversation("other", "灰原哀")],
        settings=WeChatCompanionSettings(mode="independent", width=440, height=600),
    )
    window.show()
    qt_app.processEvents()

    long_path = r"C:\Users\Administrator\Desktop\enterprise\wechat_ai_agent\项目下。"
    await controller.handle(replace(incoming("friend-long"), content=long_path))
    await controller.handle(
        replace(incoming("other-long", "other"), content=long_path)
    )
    qt_app.processEvents()

    window.conversation_list.setCurrentRow(1)
    await asyncio.sleep(0.2)
    qt_app.processEvents()

    card = next(iter(window._message_cards.values()))
    viewport_width = window.timeline_scroll.viewport().width()
    assert card.width() <= viewport_width - 32
    assert window.timeline_scroll.horizontalScrollBar().maximum() == 0
    window.close()
    repository.close()


@pytest.mark.asyncio
async def test_timeline_updates_wait_until_scrolled_to_bottom(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])
    window.resize(700, 500)
    window.show()
    qt_app.processEvents()

    for index in range(20):
        await controller.handle(incoming(str(index)))
    window._render_selected()
    window.timeline_content.setMinimumHeight(2000)
    qt_app.processEvents()
    bar = window.timeline_scroll.verticalScrollBar()
    assert bar.maximum() > 0

    window._timeline_user_scrolled(0)
    bar.setValue(0)
    qt_app.processEvents()
    rendered_count = len(window._message_cards)
    await controller.handle(incoming("new"))
    qt_app.processEvents()

    assert len(window._message_cards) == rendered_count
    assert window.new_messages_button.isVisible()
    assert "1 条新消息" in window.new_messages_button.text()

    bar.setValue(bar.maximum())
    qt_app.processEvents()
    assert len(window._message_cards) == rendered_count + 1
    assert not window.new_messages_button.isVisible()
    window.close()
    repository.close()


@pytest.mark.asyncio
async def test_clicking_new_messages_stays_at_bottom_after_delayed_layout(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])
    window.resize(700, 500)
    window.show()
    qt_app.processEvents()

    for index in range(20):
        await controller.handle(incoming(str(index)))
    window._render_selected()
    window.timeline_content.setMinimumHeight(2000)
    qt_app.processEvents()
    bar = window.timeline_scroll.verticalScrollBar()
    window._timeline_user_scrolled(0)
    bar.setValue(0)
    await controller.handle(incoming("new"))
    qt_app.processEvents()

    QTest.mouseClick(window.new_messages_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()
    await asyncio.sleep(0.2)
    assert window._timeline_follow_bottom is True
    previous_maximum = bar.maximum()
    # A late relayout can reset the current value before publishing its larger
    # scroll range. The bottom-follow state must survive that programmatic jump.
    bar.setValue(0)
    bar.setMaximum(previous_maximum + 600)
    qt_app.processEvents()

    assert bar.value() == bar.maximum()
    window._timeline_user_scrolled(0)
    assert window._timeline_follow_bottom is False
    window.close()
    repository.close()


@pytest.mark.asyncio
async def test_settings_are_persisted_and_history_is_loaded(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, history_calls = make_controller(repository)
    item = conversation()
    window = make_window(controller, [item])
    window.show()
    qt_app.processEvents()

    window.settings_button.click()
    window.settings_reply_switch.click()
    window.settings_image_switch.click()
    window.settings_history_switch.click()
    await asyncio.sleep(0)
    qt_app.processEvents()

    preferences = controller.preferences(item)
    assert window.settings_panel.isVisible()
    assert preferences.reply_enabled is True
    assert preferences.send_images_enabled is True
    assert preferences.load_history is True
    assert window.history_limit.isEnabled()
    assert history_calls == [("friend", 50)]
    assert any("message-history" in label.text() for label in window.findChildren(type(window.title_label)))
    window.close()
    repository.close()


def test_message_card_previews_local_image(
    qt_app: QApplication, tmp_path: Path
) -> None:
    image_path = tmp_path / "result.png"
    image = QImage(40, 30, QImage.Format.Format_ARGB32)
    image.fill(QColor("#6d5dfc"))
    assert image.save(str(image_path))
    entry = TimelineEntry(
        "friend",
        "我",
        "[图片] 结果图",
        "outbound",
        attachments=(
            Attachment(
                "image",
                "result.png",
                str(image_path),
                mime_type="image/png",
                metadata={"alt": "结果图"},
            ),
        ),
    )

    card = MessageCard(entry)
    card.show()
    qt_app.processEvents()

    assert card.image_preview.isVisible()
    assert card.image_preview.pixmap() is not None
    assert not card.image_preview.pixmap().isNull()
    assert card.image_preview.accessibleName() == "结果图"
    assert "点击查看大图" in card.image_preview.toolTip()
    card._open_image_viewer()
    qt_app.processEvents()
    assert card._image_viewer is not None
    assert card._image_viewer.isVisible()
    card._image_viewer.close()
    card.close()


def test_image_viewer_supports_zoom_rotation_and_actual_size(
    qt_app: QApplication, tmp_path: Path
) -> None:
    image_path = tmp_path / "viewer.png"
    second_path = tmp_path / "viewer-2.png"
    image = QImage(80, 40, QImage.Format.Format_ARGB32)
    image.fill(QColor("#6d5dfc"))
    assert image.save(str(image_path))
    second = QImage(40, 80, QImage.Format.Format_ARGB32)
    second.fill(QColor("#ff6d8d"))
    assert second.save(str(second_path))

    viewer = ImageViewerDialog(
        QPixmap(str(image_path)),
        "结果图",
        str(image_path),
        gallery=(("结果图", str(image_path)), ("第二张", str(second_path))),
    )
    viewer.show()
    qt_app.processEvents()
    assert viewer.isModal() is False
    assert viewer.windowModality() == Qt.WindowModality.NonModal
    assert "80 × 40" in viewer._info_label.text()
    assert viewer._previous_button.isEnabled() is False
    assert viewer._next_button.isEnabled() is True
    assert viewer._previous_button.text() == ""
    assert viewer._next_button.text() == ""
    assert viewer._previous_button.parentWidget() is viewer._scroll_area.viewport()
    menu = viewer._build_image_context_menu()
    assert [action.text() for action in menu.actions() if not action.isSeparator()] == [
        "复制图片",
        "图片另存为",
        "打开图片所在目录",
        "适应窗口",
        "原始尺寸",
    ]
    menu.deleteLater()

    viewer._navigate(1)
    assert viewer._source_pixmap.size() == second.size()
    assert "1/2" not in viewer._info_label.text()
    assert "2/2" in viewer._info_label.text()
    assert viewer._previous_button.isEnabled() is True
    assert viewer._next_button.isEnabled() is False

    viewer._show_actual_size()
    assert "100%" in viewer._info_label.text()
    viewer._rotate(90)
    assert "90°" in viewer._info_label.text()
    viewer._image_label.wheel_zoom.emit(1)
    assert "125%" in viewer._info_label.text()
    assert viewer._scroll_area.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert viewer._scroll_area.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    viewer.close()


def test_window_loads_local_history_by_default_and_can_clear_it(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    stored = incoming("stored")
    session = repository.create_session("codex", str(tmp_path))
    repository.bind_channel(stored, session.id)
    repository.add_inbound_message(session.id, stored)
    controller, history_calls = make_controller(repository)

    window = make_window(controller, [conversation()])
    window.show()
    qt_app.processEvents()

    assert [entry.content for entry in controller.timeline("friend")] == [
        "message-stored"
    ]
    assert history_calls == []
    dividers = [
        label.text()
        for label in window.findChildren(QLabel)
        if label.objectName() == "divider"
    ]
    assert "Agent 数据库历史" in dividers

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    assert window.clear_header_button.text() == "清空"
    assert window.clear_header_button.accessibleName() == "清空当前会话本地消息"
    assert window.clear_messages_button.text() == "清空本地消息"
    window.clear_header_button.click()
    qt_app.processEvents()

    assert repository.session_messages(session.id) == []
    assert controller.timeline("friend") == ()
    window.close()
    repository.close()


def test_settings_panel_has_one_global_foreground_fallback_switch(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    state = {"enabled": False}

    async def dispatch(_message: UnifiedMessage) -> None:
        return None

    async def load_history(_conversation_id: str, _limit: int):
        return []

    controller = CompanionController(
        repository,
        dispatch,
        load_history,
        "codex",
        foreground_fallback_getter=lambda: state["enabled"],
        foreground_fallback_setter=lambda enabled: state.__setitem__(
            "enabled", enabled
        ),
    )
    window = make_window(controller, [conversation()])
    window.show()
    qt_app.processEvents()

    assert window.foreground_fallback_switch.text() == "允许前台备用发送"
    assert window.foreground_fallback_switch.isChecked() is False

    window.foreground_fallback_switch.click()
    qt_app.processEvents()

    assert state["enabled"] is True
    assert window.foreground_fallback_switch.isChecked() is True
    window.close()
    repository.close()


def test_header_reply_control_is_switch_only_and_accessible(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])

    assert isinstance(window.reply_switch, AnimatedSwitch)
    assert window.reply_switch.text() == ""
    assert window.reply_switch.toolTip() == "开启或关闭当前会话回复"
    assert window.reply_switch.accessibleName() == "开启当前会话回复"
    assert window.settings_reply_switch.text() == "开启回复"

    window.close()
    repository.close()


def test_animated_switch_moves_knob_between_states(qt_app: QApplication) -> None:
    switch = AnimatedSwitch(animations_enabled=True)
    switch.show()
    qt_app.processEvents()

    assert switch.knob_position == 0.0
    switch.click()
    assert switch.animation.duration() == 160
    assert switch.animation.endValue() == 1.0
    assert switch.animation.state() == QAbstractAnimation.State.Running

    switch.animation.setCurrentTime(80)
    midpoint = switch.knob_position
    assert 0.0 < midpoint < 1.0
    switch.click()
    assert switch.animation.startValue() == pytest.approx(midpoint)
    assert switch.animation.endValue() == 0.0
    switch.animation.setCurrentTime(160)
    assert switch.knob_position == pytest.approx(0.0)

    switch.click()
    assert switch.animation.endValue() == 1.0
    switch.animation.setCurrentTime(160)
    assert switch.knob_position == pytest.approx(1.0)

    blocker = QSignalBlocker(switch)
    switch.setChecked(False)
    del blocker
    assert switch.knob_position == pytest.approx(0.0)
    switch.close()


def test_sidebar_collapses_at_narrow_width(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])
    window.show()

    window.resize(440, 600)
    qt_app.processEvents()
    assert window.sidebar.width() == 80
    assert window.section_label.isHidden()
    compact_row = window.conversation_list.itemWidget(
        window.conversation_list.item(0)
    )
    assert isinstance(compact_row, ConversationRow)
    assert compact_row.details.isHidden()
    compact_rect = window.conversation_list.visualItemRect(
        window.conversation_list.item(0)
    )
    assert compact_rect.right() <= window.conversation_list.viewport().rect().right()

    window.resize(500, 600)
    qt_app.processEvents()
    assert window.sidebar.width() == 80
    assert window.section_label.isHidden()

    window.settings_button.click()
    qt_app.processEvents()
    assert window.main_panel.isHidden()
    assert window.settings_panel.width() == 420
    window._close_settings()
    assert not window.main_panel.isHidden()

    window.resize(559, 600)
    qt_app.processEvents()
    assert window.sidebar.width() == 80

    window.resize(560, 600)
    qt_app.processEvents()
    assert window.sidebar.width() == 190
    assert not window.section_label.isHidden()
    expanded_row = window.conversation_list.itemWidget(
        window.conversation_list.item(0)
    )
    assert isinstance(expanded_row, ConversationRow)
    assert not expanded_row.details.isHidden()
    expanded_rect = window.conversation_list.visualItemRect(
        window.conversation_list.item(0)
    )
    assert expanded_rect.right() <= window.conversation_list.viewport().rect().right()
    window.close()
    repository.close()


def test_compact_conversation_row_centers_avatar_and_exposes_name(
    qt_app: QApplication,
) -> None:
    row = ConversationRow(conversation(display_name="工藤新一"), "codex", False, False)
    row.set_compact(True)
    row.resize(54, 58)
    row.show()
    qt_app.processEvents()

    avatar_parent = row.avatar.parentWidget()
    assert row.avatar.toolTip() == "工藤新一"
    assert row.avatar.accessibleName() == "工藤新一"
    assert row.avatar.geometry().left() >= 0
    assert row.avatar.geometry().right() < avatar_parent.width()
    assert (
        abs(row.avatar.geometry().center().x() - avatar_parent.rect().center().x())
        <= 1
    )
    row.close()


def test_conversation_row_renders_real_avatar(
    qt_app: QApplication, tmp_path: Path
) -> None:
    avatar_path = tmp_path / "avatar.png"
    image = QImage(12, 8, QImage.Format.Format_ARGB32)
    image.fill(QColor("#17834B"))
    assert image.save(str(avatar_path))

    row = ConversationRow(
        conversation(display_name="工藤新一"),
        "codex",
        False,
        False,
        avatar_path=avatar_path,
    )
    row.show()
    qt_app.processEvents()

    pixmap = row.avatar.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    assert pixmap.size().width() == 36
    assert pixmap.size().height() == 36
    assert row.avatar.text() == ""
    assert row.avatar.toolTip() == "工藤新一"
    row.close()


def test_conversation_row_keeps_initial_when_avatar_is_invalid(
    qt_app: QApplication, tmp_path: Path
) -> None:
    row = ConversationRow(
        conversation(display_name="工藤新一"),
        "codex",
        False,
        False,
        avatar_path=tmp_path / "missing.png",
    )
    row.show()
    qt_app.processEvents()

    assert row.avatar.text() == "工"
    assert row.avatar.pixmap().isNull()
    row.close()


@pytest.mark.asyncio
async def test_avatar_completion_refreshes_only_matching_conversation_row(
    qt_app: QApplication, tmp_path: Path
) -> None:
    avatar_path = tmp_path / "avatar.png"
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(QColor("#6D5CE7"))
    assert image.save(str(avatar_path))

    class Cache:
        def __init__(self) -> None:
            self.ready: set[str] = set()

        def cached_path(self, item: ConversationItem):
            return avatar_path if item.conversation_id in self.ready else None

        async def ensure(self, item: ConversationItem):
            await asyncio.sleep(0)
            self.ready.add(item.conversation_id)
            return avatar_path

    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    items = [
        replace(conversation("friend"), avatar_url="https://wx.qlogo.cn/one"),
        replace(conversation("other"), avatar_url="https://wx.qlogo.cn/two"),
    ]
    window = make_window(controller, items, avatar_cache=Cache())
    window.show()
    qt_app.processEvents()
    previous_first = window.conversation_list.itemWidget(
        window.conversation_list.item(0)
    )
    previous_second = window.conversation_list.itemWidget(
        window.conversation_list.item(1)
    )

    window._schedule_avatar_refresh(items[0])
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    qt_app.processEvents()

    refreshed_first = window.conversation_list.itemWidget(
        window.conversation_list.item(0)
    )
    refreshed_second = window.conversation_list.itemWidget(
        window.conversation_list.item(1)
    )
    assert refreshed_first is not previous_first
    assert refreshed_second is previous_second
    assert not refreshed_first.avatar.pixmap().isNull()
    window.close()
    repository.close()


@pytest.mark.asyncio
async def test_profile_reload_updates_avatar_url_without_losing_ui_state(
    qt_app: QApplication, tmp_path: Path
) -> None:
    original = [
        replace(conversation("friend"), avatar_url="https://wx.qlogo.cn/old"),
        replace(conversation("other"), avatar_url="https://wx.qlogo.cn/other"),
    ]
    updated = [
        replace(original[0], avatar_url="https://wx.qlogo.cn/new"),
        original[1],
    ]
    loader_calls = 0

    async def load_profiles() -> list[ConversationItem]:
        nonlocal loader_calls
        loader_calls += 1
        return updated

    class Cache:
        def __init__(self) -> None:
            self.requests: list[str] = []

        def cached_path(self, _item: ConversationItem):
            return None

        async def ensure(self, item: ConversationItem):
            self.requests.append(item.conversation_id)
            return None

    cache = Cache()
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(
        controller,
        original,
        avatar_cache=cache,
        conversation_loader=load_profiles,
    )
    window.conversation_list.setCurrentRow(1)
    window._unread.add("friend")

    await window._reload_conversation_profiles()
    await asyncio.sleep(0)

    assert loader_calls == 1
    assert window.conversations[0].avatar_url == "https://wx.qlogo.cn/new"
    assert window.conversation_list.currentRow() == 1
    assert window._selected_item().conversation_id == "other"
    assert window._unread == {"friend"}
    assert cache.requests == ["friend", "other"]
    assert window._profile_refresh_timer.interval() == 10 * 60 * 1000
    window.close()
    repository.close()


def test_launcher_has_compact_accessible_tool_window_behavior(
    qt_app: QApplication,
) -> None:
    opened = []
    launcher = CompanionLauncher(lambda: opened.append(True))

    assert launcher.size().width() == 44
    assert launcher.size().height() == 44
    assert launcher.text() == "AI"
    assert launcher.toolTip() == "打开 Agent 工作台"
    assert launcher.accessibleName() == "打开 Agent 工作台"
    assert launcher.windowFlags() & Qt.WindowType.Tool
    assert launcher.windowFlags() & Qt.WindowType.FramelessWindowHint

    launcher.click()
    assert opened == [True]
    launcher.shutdown()


def test_custom_titlebar_has_only_accessible_settings_minimize_and_close(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])
    window.show()
    qt_app.processEvents()

    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.title_bar.title_label.text() == "微信 Agent 工作台"
    controls = window.title_bar.findChildren(QPushButton)
    assert [button.property("titlebarRole") for button in controls] == [
        "settings",
        "minimize",
        "close",
    ]
    assert all(button.text() == "" for button in controls)
    assert [button.toolTip() for button in controls] == [
        "打开会话设置",
        "收起 Agent 工作台",
        "关闭 Agent 工作台",
    ]
    assert [button.accessibleName() for button in controls] == [
        "打开会话设置",
        "收起 Agent 工作台",
        "关闭 Agent 工作台",
    ]
    assert window.title_bar.findChildren(QPushButton, "maximizeButton") == []

    window.settings_button.click()
    assert window.settings_panel.isVisible()
    assert window.settings_button.isChecked()
    window._close_settings()
    assert not window.settings_button.isChecked()
    window.close_button.click()
    assert window._closed_event.is_set()
    repository.close()


@pytest.mark.parametrize(
    ("x", "y", "expected"),
    [
        (103, 103, 13),
        (497, 103, 14),
        (103, 397, 16),
        (497, 397, 17),
        (103, 200, 10),
        (497, 200, 11),
        (250, 103, 12),
        (250, 397, 15),
        (250, 200, None),
    ],
)
def test_calculates_each_windows_resize_hit(
    x: int, y: int, expected: int | None
) -> None:
    assert (
        calculate_resize_hit(WindowRect(100, 100, 500, 400), x, y, 8)
        == expected
    )


def test_independent_titlebar_minimize_does_not_show_launcher(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    independent = make_window(
        controller,
        [conversation()],
        settings=WeChatCompanionSettings(mode="independent"),
    )
    independent.show()
    qt_app.processEvents()

    independent.minimize_button.click()
    qt_app.processEvents()

    assert independent._collapsed is False
    assert independent.isMinimized()
    assert independent.launcher.isHidden()

    independent.close()
    repository.close()


def test_collapsed_launcher_follows_wechat_and_restores_workbench(
    qt_app: QApplication, tmp_path: Path
) -> None:
    class Probe:
        minimized = False
        client_rect = WindowRect(-1592, 138, -708, 892)

        def snapshot(self, _hwnd: int) -> WindowSnapshot:
            return WindowSnapshot(
                available=True,
                visible=True,
                minimized=self.minimized,
                rect=WindowRect(-1600, 100, -700, 900),
                client_rect=self.client_rect,
            )

    class Mover:
        def __init__(self) -> None:
            self.calls = []
            self.succeed = True
            self.window = None
            self.launcher_visibility = []

        def move(self, hwnd, geometry) -> bool:
            self.calls.append((hwnd, geometry))
            if (
                self.window is not None
                and hwnd == int(self.window.launcher.winId())
            ):
                self.launcher_visibility.append(
                    self.window.launcher.isVisible()
                )
            return self.succeed

    class Owner:
        def __init__(self) -> None:
            self.calls = []

        def bind(self, hwnd, owner_hwnd) -> bool:
            self.calls.append((hwnd, owner_hwnd))
            return True

    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    probe = Probe()
    mover = Mover()
    owner = Owner()
    window = make_window(
        controller,
        [conversation()],
        settings=WeChatCompanionSettings(mode="docked", side="left", width=440),
        probe=probe,
        mover=mover,
        owner=owner,
    )
    mover.window = window
    window.show()
    qt_app.processEvents()

    window.minimize_button.click()
    qt_app.processEvents()

    assert window._collapsed is True
    assert window.isHidden()
    assert window.launcher.isVisible()
    assert owner.calls[-1] == (int(window.launcher.winId()), 101)
    assert mover.launcher_visibility[-1] is True
    launcher_geometry = mover.calls[-1][1]
    assert (launcher_geometry.x, launcher_geometry.y) == (-1580, 150)
    assert (launcher_geometry.width, launcher_geometry.height) == (44, 44)

    probe.minimized = True
    window._follow_wechat_window()
    assert window.launcher.isHidden()
    moves_before_restore = len(mover.launcher_visibility)
    probe.minimized = False
    window._follow_wechat_window()
    assert window.launcher.isVisible()
    assert len(mover.launcher_visibility) == moves_before_restore + 1
    assert mover.launcher_visibility[-1] is True

    probe.client_rect = WindowRect(-1392, 238, -508, 992)
    mover.succeed = False
    window._follow_wechat_window()
    assert (window.launcher.geometry().x(), window.launcher.geometry().y()) == (
        -1380,
        250,
    )

    window.launcher.click()
    qt_app.processEvents()

    assert window._collapsed is False
    assert window.launcher.isHidden()
    assert window.isVisible()
    assert owner.calls[-1] == (int(window.winId()), 101)

    window.minimize_button.click()
    qt_app.processEvents()
    window.launcher.close()
    qt_app.processEvents()
    assert window._collapsed is False
    assert window.isVisible()

    window.minimize_button.click()
    qt_app.processEvents()
    assert window.launcher.isVisible()
    window.close()
    assert window._closed_event.is_set()
    assert window.launcher.isHidden()
    repository.close()


def test_lifecycle_can_hide_and_restore_independent_workbench(qt_app, tmp_path):
    from agent_bridge.lifecycle import WorkbenchLifecycleServer, send_lifecycle_command

    repository = SQLiteRepository(tmp_path / "visibility.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])
    server = WorkbenchLifecycleServer(tmp_path / "config.yaml")
    server.show_requested.connect(window._expand_from_launcher)
    server.hide_requested.connect(window._collapse_to_launcher)
    window.visibility_changed.connect(server.set_window_visible)
    server.start()
    try:
        window.show()
        qt_app.processEvents()
        assert send_lifecycle_command(server.config_path, "status")["visible"] is True
        assert send_lifecycle_command(server.config_path, "hide")["ok"]
        qt_app.processEvents()
        assert window.isHidden()
        assert not window._closed_event.is_set()
        assert send_lifecycle_command(server.config_path, "status") == {
            "ok": True, "state": "running", "visible": False,
        }
        assert send_lifecycle_command(server.config_path, "show")["ok"]
        qt_app.processEvents()
        assert window.isVisible()
        assert send_lifecycle_command(server.config_path, "status")["visible"] is True
        # Native minimization is also reported, and show restores it even if not collapsed.
        window.showMinimized()
        qt_app.processEvents()
        assert send_lifecycle_command(server.config_path, "status")["visible"] is False
        send_lifecycle_command(server.config_path, "show")
        qt_app.processEvents()
        assert window.isVisible() and not window.isMinimized()
    finally:
        server.close()
        window.close()
        repository.close()


def test_timeline_aligns_other_messages_left_and_self_messages_right(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])

    window._render_timeline(
        (
            TimelineEntry("friend", "工藤新一", "对方消息", "inbound"),
            TimelineEntry("friend", "我", "我的消息", "outbound"),
        )
    )

    inbound_row = window.timeline_layout.itemAt(0).layout()
    outbound_row = window.timeline_layout.itemAt(1).layout()
    assert isinstance(inbound_row.itemAt(0).widget(), MessageCard)
    assert inbound_row.itemAt(1).spacerItem() is not None
    assert outbound_row.itemAt(0).spacerItem() is not None
    assert isinstance(outbound_row.itemAt(1).widget(), MessageCard)
    window.close()
    repository.close()


def test_quote_card_click_jumps_to_exact_history_message(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])
    target = TimelineEntry(
        "friend",
        "工藤新一",
        "在吗",
        "inbound",
        source_key="wechat:friend:255",
    )
    reply = TimelineEntry(
        "friend",
        "我",
        "爸爸，在。",
        "outbound",
        quote=QuotePreview(
            "friend",
            "在吗",
            ContentType.TEXT,
            target_source_key="wechat:friend:255",
        ),
    )
    window._render_timeline((target, reply))

    reply_card = window._message_cards[reply.entry_id]
    target_card = window._message_cards[target.entry_id]
    QTest.mouseClick(reply_card.quote_preview, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    assert reply_card.quote_preview.isHidden() is False
    assert reply_card.quote_preview.sender_label.text() == "工藤新一"
    assert reply_card.quote_preview.content_label.text() == "在吗"
    assert target_card.property("quoteTarget") is True
    window.close()
    repository.close()


def test_quote_card_missing_target_does_not_jump(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])
    reply = TimelineEntry(
        "friend",
        "我",
        "爸爸，在。",
        "outbound",
        quote=QuotePreview(
            "工藤新一",
            "在吗",
            ContentType.TEXT,
            target_source_key="wechat:friend:cleared",
        ),
    )
    window._render_timeline((reply,))
    before = window.timeline_scroll.verticalScrollBar().value()

    QTest.mouseClick(
        window._message_cards[reply.entry_id].quote_preview,
        Qt.MouseButton.LeftButton,
    )
    qt_app.processEvents()

    assert window.timeline_scroll.verticalScrollBar().value() == before
    assert window.status_label.text() == "原消息已不在当前历史中"
    window.close()
    repository.close()


def test_quote_card_fallback_requires_timestamp_and_content(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])
    created_at = datetime(2026, 9, 4, 0, 30, 39, tzinfo=timezone.utc)
    wrong = TimelineEntry(
        "friend",
        "工藤新一",
        "在吗",
        "inbound",
        created_at=created_at.replace(second=38),
    )
    target = TimelineEntry(
        "friend", "工藤新一", "在吗", "inbound", created_at=created_at
    )
    reply = TimelineEntry(
        "friend",
        "我",
        "爸爸，在。",
        "outbound",
        quote=QuotePreview(
            "工藤新一",
            "在吗",
            ContentType.TEXT,
            target_created_at=created_at,
        ),
    )
    window._render_timeline((wrong, target, reply))

    QTest.mouseClick(
        window._message_cards[reply.entry_id].quote_preview,
        Qt.MouseButton.LeftButton,
    )
    qt_app.processEvents()

    assert window._message_cards[target.entry_id].property("quoteTarget") is True
    assert window._message_cards[wrong.entry_id].property("quoteTarget") is not True
    window.close()
    repository.close()


def test_agent_stream_updates_existing_message_card_in_place(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])
    window.show()

    controller.handle_agent_update(
        AgentProgressUpdate("job-1", "friend", "codex", "started")
    )
    qt_app.processEvents()
    entry = controller.timeline("friend")[0]
    card = window._message_cards[entry.entry_id]

    controller.handle_agent_update(
        AgentProgressUpdate(
            "job-1", "friend", "codex", "streaming", "正在流式输出"
        )
    )
    qt_app.processEvents()

    assert window._message_cards[entry.entry_id] is card
    assert card.content_label.text().rstrip("▍") != "正在流式输出"
    card._advance_typewriter()
    assert card.content_label.text().startswith("正")
    assert "正在生成" in card.status_label.text()
    assert "正在生成" not in card.meta_label.text()

    controller.handle_agent_update(
        AgentProgressUpdate(
            "job-1",
            "friend",
            "codex",
            "completed",
            "正在流式输出完成",
            ("正在流式输出完成",),
        )
    )
    qt_app.processEvents()

    assert window._message_cards[entry.entry_id] is card
    while card.typewriter_active:
        card._advance_typewriter()
    assert card.content_label.text() == "正在流式输出完成"
    assert "▍" not in card.content_label.text()
    window.close()
    repository.close()


def test_typewriter_copy_uses_complete_target_without_cursor(
    qt_app: QApplication,
) -> None:
    card = MessageCard(
        TimelineEntry(
            "friend",
            "我",
            "",
            "outbound",
            delivery_status="generating",
        )
    )
    card.update_entry(
        TimelineEntry(
            "friend",
            "我",
            "一段完整回复",
            "outbound",
            delivery_status="generating",
        )
    )
    card._advance_typewriter()

    assert card.content_label.text().startswith("一")
    assert card.content_label.text() != "一段完整回复"
    card.copy_button.click()
    assert qt_app.clipboard().text() == "一段完整回复"
    card.close()


def test_typewriter_accelerates_when_stream_backlog_is_large() -> None:
    card = MessageCard(
        TimelineEntry(
            "friend",
            "我",
            "",
            "outbound",
            delivery_status="generating",
        )
    )
    card.update_entry(
        TimelineEntry(
            "friend",
            "我",
            "字" * 130,
            "outbound",
            delivery_status="generating",
        )
    )

    card._advance_typewriter()

    assert len(card.content_label.text().rstrip("▍")) == 6
    card.close()


def test_message_actions_follow_in_place_delivery_status() -> None:
    resent = []
    card = MessageCard(
        TimelineEntry(
            "friend",
            "我",
            "",
            "outbound",
            delivery_status="generating",
        ),
        resend_callback=resent.append,
    )
    card.update_entry(
        TimelineEntry(
            "friend",
            "我",
            "agent reply",
            "outbound",
            delivery_id="delivery-1",
            delivery_status="sent",
        )
    )

    assert card.resend_button is not None
    assert card.resend_button.isHidden() is False
    card.resend_button.click()
    assert resent == ["delivery-1"]
    card.close()


def test_message_status_keeps_error_detail_out_of_footer() -> None:
    detail = "Another application is active; atomic foreground delivery is required"
    card = MessageCard(
        TimelineEntry(
            "friend",
            "我",
            "agent reply",
            "outbound",
            delivery_status="foreground_unavailable",
            status_detail=detail,
        )
    )

    assert card.status_label.text() == "前台备用未授权"
    assert detail not in card.status_label.text()
    assert card.status_label.toolTip() == detail
    assert card.status_dot.property("statusState") == "error"
    card.close()


def test_sent_message_card_offers_send_again(qt_app: QApplication) -> None:
    resent = []
    card = MessageCard(
        TimelineEntry(
            "friend",
            "我",
            "agent reply",
            "outbound",
            delivery_id="delivery-1",
            delivery_status="sent",
        ),
        resend_callback=resent.append,
    )

    button = next(
        item for item in card.findChildren(QPushButton) if item.text() == "再次发送"
    )
    button.click()

    assert resent == ["delivery-1"]
    assert card.status_label.text() == "已发送"
    copy_button = next(
        item for item in card.findChildren(QPushButton) if item.text() == "复制"
    )
    copy_button.click()
    assert qt_app.clipboard().text() == "agent reply"
    card.close()


def test_chunked_sent_card_resends_every_delivery(qt_app: QApplication) -> None:
    resent = []
    card = MessageCard(
        TimelineEntry(
            "friend",
            "我",
            "long agent reply",
            "outbound",
            delivery_id="delivery-1",
            delivery_ids=("delivery-1", "delivery-2"),
            delivery_status="sent",
            expected_deliveries=2,
        ),
        resend_callback=resent.append,
    )

    button = next(
        item for item in card.findChildren(QPushButton) if item.text() == "再次发送"
    )
    button.click()

    assert resent == ["delivery-1", "delivery-2"]
    card.close()


def test_failed_setting_rolls_back_control(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])
    window.show()
    qt_app.processEvents()

    def fail_update(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(controller, "update_preferences", fail_update)
    window.reply_switch.click()
    qt_app.processEvents()

    assert not window.reply_switch.isChecked()
    assert "设置保存失败" in window.status_label.text()
    window.close()
    repository.close()


@pytest.mark.asyncio
async def test_window_run_finishes_when_closed(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])

    running = asyncio.create_task(window.run())
    await asyncio.sleep(0)
    qt_app.processEvents()
    assert not window._follow_timer.isActive()
    window.close()

    await running
    repository.close()


@pytest.mark.asyncio
async def test_message_content_is_rendered_as_plain_text(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation("friend", "<b>联系人</b>")])
    window.show()

    message = replace(incoming("markup"), content="<b>不是富文本</b>")
    await controller.handle(message)
    qt_app.processEvents()

    labels = window.findChildren(QLabel)
    content_label = next(label for label in labels if label.text() == "<b>不是富文本</b>")
    assert content_label.textFormat() == Qt.TextFormat.PlainText
    assert window.title_label.textFormat() == Qt.TextFormat.PlainText
    window.close()
    repository.close()


def test_docked_window_uses_qt_handle_and_absolute_geometry(
    qt_app: QApplication, tmp_path: Path
) -> None:
    class Probe:
        def snapshot(self, _hwnd: int) -> WindowSnapshot:
            return WindowSnapshot(
                available=True,
                visible=True,
                rect=WindowRect(-1600, 100, -700, 900),
            )

    class Mover:
        def __init__(self) -> None:
            self.calls = []

        def move(self, hwnd, geometry) -> bool:
            self.calls.append((hwnd, geometry))
            return True

    class Owner:
        def __init__(self) -> None:
            self.calls = []

        def bind(self, hwnd, owner_hwnd) -> bool:
            self.calls.append((hwnd, owner_hwnd))
            return False

    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    mover = Mover()
    owner = Owner()
    window = make_window(
        controller,
        [conversation()],
        settings=WeChatCompanionSettings(side="left", width=440),
        probe=Probe(),
        mover=mover,
        owner=owner,
    )
    window.show()
    qt_app.processEvents()

    window._follow_wechat_window()
    window._follow_wechat_window()

    assert len(mover.calls) == 1
    assert mover.calls[0][0] == int(window.winId())
    assert mover.calls[0][1].x == -2040
    assert mover.calls[0][1].y == 100
    assert owner.calls[0] == (int(window.winId()), 101)
    window.close()
    repository.close()


def test_independent_window_does_not_bind_to_wechat_owner(
    qt_app: QApplication, tmp_path: Path
) -> None:
    class Owner:
        def __init__(self) -> None:
            self.calls = []

        def bind(self, hwnd, owner_hwnd) -> bool:
            self.calls.append((hwnd, owner_hwnd))
            return True

    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    owner = Owner()
    window = make_window(
        controller,
        [conversation()],
        settings=WeChatCompanionSettings(mode="independent"),
        owner=owner,
    )

    window._follow_wechat_window()

    assert owner.calls == []
    window.close()
    repository.close()


def test_dragging_companion_moves_wechat_by_the_same_delta(
    qt_app: QApplication, tmp_path: Path
) -> None:
    wechat_rect = WindowRect(-1600, 100, -700, 900)
    companion_rect = WindowRect(-2040, 100, -1600, 900)

    class Probe:
        def __init__(self) -> None:
            self.companion_hwnd = 0
            self.calls = []

        def snapshot(self, hwnd: int) -> WindowSnapshot:
            self.calls.append(hwnd)
            rect = companion_rect if hwnd == self.companion_hwnd else wechat_rect
            return WindowSnapshot(available=True, visible=True, rect=rect)

    class Mover:
        def __init__(self) -> None:
            self.calls = []

        def move(self, hwnd, geometry) -> bool:
            self.calls.append((hwnd, geometry))
            return True

    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    probe = Probe()
    mover = Mover()
    window = make_window(
        controller,
        [conversation()],
        settings=WeChatCompanionSettings(side="left", width=440),
        probe=probe,
        mover=mover,
    )
    probe.companion_hwnd = int(window.winId())

    window._begin_group_move()
    window._move_wechat_with_companion(
        WindowRect(-1940, 250, -1500, 1050)
    )

    assert mover.calls[-1][0] == 101
    geometry = mover.calls[-1][1]
    assert (geometry.x, geometry.y) == (-1500, 250)
    assert (geometry.width, geometry.height) == (900, 800)

    calls_before_follow = len(probe.calls)
    window._follow_wechat_window()
    assert len(probe.calls) == calls_before_follow

    window._end_group_move()
    assert window._group_move_active is False
    assert window._group_move_origin is None
    window.close()
    repository.close()


def test_failed_group_move_stops_linkage_until_drag_ends(
    qt_app: QApplication, tmp_path: Path
) -> None:
    class Probe:
        def snapshot(self, hwnd: int) -> WindowSnapshot:
            rect = (
                WindowRect(100, 100, 500, 700)
                if hwnd != 101
                else WindowRect(500, 100, 1300, 700)
            )
            return WindowSnapshot(available=True, visible=True, rect=rect)

    class Mover:
        def __init__(self) -> None:
            self.calls = []

        def move(self, hwnd, geometry) -> bool:
            self.calls.append((hwnd, geometry))
            return False

    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    mover = Mover()
    window = make_window(
        controller,
        [conversation()],
        settings=WeChatCompanionSettings(side="left", width=400),
        probe=Probe(),
        mover=mover,
    )

    window._begin_group_move()
    window._move_wechat_with_companion(WindowRect(150, 150, 550, 750))
    window._move_wechat_with_companion(WindowRect(200, 200, 600, 800))

    assert len(mover.calls) == 1
    assert window._group_move_origin is None
    assert window._group_move_active is True
    window._end_group_move()
    window.close()
    repository.close()


def test_independent_window_does_not_begin_group_move(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(
        controller,
        [conversation()],
        settings=WeChatCompanionSettings(mode="independent"),
    )

    window._begin_group_move()

    assert window._group_move_active is False
    assert window._group_move_origin is None
    window.close()
    repository.close()


def test_windows_move_messages_route_group_drag(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(
        controller,
        [conversation()],
        settings=WeChatCompanionSettings(side="left"),
    )
    events = []
    monkeypatch.setattr(window, "_begin_group_move", lambda: events.append("begin"))
    monkeypatch.setattr(window, "_end_group_move", lambda: events.append("end"))
    monkeypatch.setattr(
        window,
        "_move_wechat_with_companion",
        lambda rect: events.append(rect),
    )
    message = wintypes.MSG()

    message.message = 0x0231
    window._handle_windows_move_message(ctypes.addressof(message))
    proposed = wintypes.RECT(-500, 120, -60, 920)
    message.message = 0x0216
    message.lParam = ctypes.addressof(proposed)
    window._handle_windows_move_message(ctypes.addressof(message))
    message.message = 0x0232
    message.lParam = 0
    window._handle_windows_move_message(ctypes.addressof(message))

    assert events == [
        "begin",
        WindowRect(-500, 120, -60, 920),
        "end",
    ]
    window.close()
    repository.close()


def test_follow_timer_uses_precise_sixty_fps(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    settings = WeChatCompanionSettings()
    window = make_window(
        controller,
        [conversation()],
        settings=settings,
    )

    assert settings.follow_interval == pytest.approx(0.016)
    assert window._follow_timer.interval() == 16
    assert window._follow_timer.timerType() == Qt.TimerType.PreciseTimer
    window.close()
    repository.close()


def test_unchanged_follow_status_skips_repaint(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])
    window.status_label.setText("贴靠微信左侧 · 正在监听")
    window.status_label.setProperty("state", "info")
    updates = []
    monkeypatch.setattr(
        window,
        "_set_status",
        lambda text, **kwargs: updates.append((text, kwargs)),
    )

    window._set_follow_status("贴靠微信左侧 · 正在监听")
    window._set_follow_status("微信不可用 · 等待恢复", state="warning")

    assert updates == [
        ("微信不可用 · 等待恢复", {"state": "warning"}),
    ]
    window.close()
    repository.close()


def test_sender_waiting_status_and_delivery_badge_are_rendered(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.db")
    controller, _ = make_controller(repository)
    window = make_window(controller, [conversation()])
    window.show()
    delivery = repository.enqueue_outbound_delivery(
        ChannelTarget("bot", "friend", ConversationType.PRIVATE),
        "agent reply",
        "job:reply",
        600,
    )

    controller.handle_sender_update(SenderUpdate("waiting_for_idle", delivery))
    qt_app.processEvents()

    assert "等待你停止操作" in window.status_label.text()
    assert any(
        "等待你停止操作" in label.text()
        for label in window.findChildren(QLabel)
    )
    window.close()
    repository.close()
