from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QDesktopServices,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from agent_bridge.companion.models import (
    ConversationItem,
    QuotePreview,
    TimelineEntry,
)
from agent_bridge.models import ContentType


class ConversationRow(QWidget):
    def __init__(
        self,
        item: ConversationItem,
        provider: str,
        reply_enabled: bool,
        unread: bool,
        avatar_path: str | Path | None = None,
    ) -> None:
        super().__init__()
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(8, 7, 8, 7)
        self._layout.setSpacing(9)

        avatar_container = QWidget()
        avatar_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        avatar_layout = QHBoxLayout(avatar_container)
        avatar_layout.setContentsMargins(0, 0, 0, 0)
        self.avatar = QLabel(_avatar_text(item.display_name))
        self.avatar.setObjectName("avatar")
        self.avatar.setTextFormat(Qt.TextFormat.PlainText)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar.setFixedSize(36, 36)
        self.avatar.setToolTip(item.display_name)
        self.avatar.setAccessibleName(item.display_name)
        avatar_pixmap = _load_avatar_pixmap(avatar_path, 36)
        if avatar_pixmap is not None:
            self.avatar.setPixmap(avatar_pixmap)
        avatar_layout.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignCenter)
        self._layout.addWidget(avatar_container)

        self.details = QWidget()
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(3)

        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_label = QLabel(item.display_name)
        name_label.setTextFormat(Qt.TextFormat.PlainText)
        name_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        unread_label = QLabel("新消息" if unread else "")
        unread_label.setObjectName("replyBadge")
        unread_label.setProperty("enabled", True)
        name_row.addWidget(name_label, 1)
        name_row.addWidget(unread_label)
        details_layout.addLayout(name_row)

        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 0, 0, 0)
        meta_row.setSpacing(6)
        provider_label = QLabel(provider.upper())
        provider_label.setObjectName("agentBadge")
        state = "开启" if reply_enabled else "监听"
        state_label = QLabel(state)
        state_label.setObjectName("replyBadge")
        state_label.setProperty("enabled", reply_enabled)
        meta_row.addWidget(provider_label)
        meta_row.addWidget(state_label)
        meta_row.addStretch(1)
        details_layout.addLayout(meta_row)
        self._layout.addWidget(self.details, 1)

        kind = "群聊" if item.conversation_type.value == "group" else "私聊"
        self.setToolTip(f"{item.display_name}\n{kind} · {provider} · {state}")

    def set_compact(self, compact: bool) -> None:
        margin = 3 if compact else 8
        self._layout.setContentsMargins(margin, 7, margin, 7)
        self._layout.setSpacing(0 if compact else 9)
        self.details.setVisible(not compact)


class MessageActionButton(QPushButton):
    """Compact text action with a theme-aware, code-drawn outline icon."""

    def __init__(self, text: str, icon_name: str, accessible_name: str) -> None:
        super().__init__(text)
        self._icon_name = icon_name
        self.setProperty("messageAction", True)
        self.setAccessibleName(accessible_name)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self.palette().color(QPalette.ColorRole.ButtonText)
        pen = QPen(color, 1.35)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        top = (self.height() - 14) / 2
        left = 8.0
        if self._icon_name == "copy":
            painter.drawRoundedRect(QRectF(left + 3, top, 8, 9), 1.5, 1.5)
            painter.drawRoundedRect(QRectF(left, top + 3, 8, 9), 1.5, 1.5)
        elif self._icon_name == "cancel":
            painter.drawLine(QPointF(left + 2, top + 2), QPointF(left + 11, top + 11))
            painter.drawLine(QPointF(left + 11, top + 2), QPointF(left + 2, top + 11))
        else:
            painter.drawArc(QRectF(left + 1, top + 1, 11, 11), 35 * 16, 285 * 16)
            painter.drawLine(QPointF(left + 9, top + 1), QPointF(left + 12, top + 1))
            painter.drawLine(QPointF(left + 12, top + 1), QPointF(left + 12, top + 4))
        painter.end()


class ClickableImageLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, event) -> None:
        pixmap = self.pixmap()
        if (
            event.button() == Qt.MouseButton.LeftButton
            and pixmap is not None
            and not pixmap.isNull()
        ):
            self.clicked.emit()
        super().mousePressEvent(event)


class _ImageCanvas(QLabel):
    wheel_zoom = Signal(int)

    def __init__(self, scroll_area: QScrollArea):
        super().__init__()
        self._scroll_area = scroll_area
        self._drag_origin = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is not None:
            current = event.position().toPoint()
            delta = current - self._drag_origin
            self._drag_origin = current
            horizontal = self._scroll_area.horizontalScrollBar()
            vertical = self._scroll_area.verticalScrollBar()
            horizontal.setValue(horizontal.value() - delta.x())
            vertical.setValue(vertical.value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_origin = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta:
            self.wheel_zoom.emit(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)


class _ImageNavButton(QPushButton):
    def __init__(self, direction: int, accessible_name: str):
        super().__init__()
        self._direction = -1 if direction < 0 else 1
        self.setObjectName("imageViewerNav")
        self.setAccessibleName(accessible_name)
        self.setToolTip(accessible_name)
        self.setFixedSize(44, 72)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self.palette().color(QPalette.ColorRole.ButtonText), 2.4))
        center_x = self.width() / 2
        center_y = self.height() / 2
        direction = self._direction
        painter.drawLine(
            QPointF(center_x - direction * 6, center_y - 11),
            QPointF(center_x + direction * 6, center_y),
        )
        painter.drawLine(
            QPointF(center_x + direction * 6, center_y),
            QPointF(center_x - direction * 6, center_y + 11),
        )
        painter.end()


class ImageViewerDialog(QDialog):
    """Full-featured in-workbench image viewer with zoom and pan controls."""

    _shared_viewer = None

    def __init__(
        self,
        pixmap: QPixmap,
        title: str,
        image_path: str | None = None,
        parent: QWidget | None = None,
        gallery: tuple[tuple[str, str], ...] = (),
        gallery_index: int = 0,
    ):
        super().__init__(parent)
        self.setWindowTitle(title or "图片预览")
        # Keep the workbench usable while the viewer is open.  A modeless
        # dialog also follows normal z-order rules instead of staying above
        # every other application window.
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setObjectName("imageViewerDialog")
        self.setStyleSheet(
            """
            QDialog#imageViewerDialog { background: #1f1f1f; color: #f2f2f2; }
            QScrollArea#imageViewerArea { background: #1f1f1f; border: 0; }
            QLabel#imageViewerInfo { color: #b8b8b8; padding: 0 8px; }
            QPushButton#imageViewerTool {
                color: #eeeeee; background: #303030; border: 1px solid #4a4a4a;
                border-radius: 5px; padding: 5px 10px; min-height: 28px;
            }
            QPushButton#imageViewerTool:hover { background: #414141; }
            QPushButton#imageViewerTool:pressed { background: #505050; }
            QPushButton#imageViewerClose {
                color: #ffffff; background: #3a3a3a; border: 1px solid #5a5a5a;
                border-radius: 5px; padding: 5px 14px; min-height: 28px;
            }
            QPushButton#imageViewerClose:hover { background: #c94b4b; }
            QPushButton#imageViewerNav {
                color: #ffffff; background: rgba(35, 35, 35, 190);
                border: 1px solid rgba(255, 255, 255, 70); border-radius: 22px;
                font-size: 30px; font-weight: 300; padding: 0;
            }
            QPushButton#imageViewerNav:hover { background: rgba(75, 75, 75, 225); }
            QPushButton#imageViewerNav:disabled { color: rgba(255, 255, 255, 55); }
            QMenu { color: #eeeeee; background: #303030; border: 1px solid #505050; }
            QMenu::item { padding: 7px 24px 7px 12px; }
            QMenu::item:selected { background: #4a4a4a; }
            """
        )
        self._source_pixmap = pixmap
        self._image_path = image_path
        self._gallery = gallery or ((title or "图片", image_path or ""),)
        self._gallery_index = max(0, min(gallery_index, len(self._gallery) - 1))
        self._rotation = 0
        self._scale = 1.0
        self._fit_mode = True

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(5)
        self._previous_button = _ImageNavButton(-1, "查看上一张图片")
        self._next_button = _ImageNavButton(1, "查看下一张图片")
        self._zoom_out = self._tool_button("−", "缩小")
        self._zoom_in = self._tool_button("＋", "放大")
        self._fit_button = self._tool_button("适应", "适应窗口")
        self._actual_button = self._tool_button("100%", "原始尺寸")
        self._rotate_left = self._tool_button("↺", "向左旋转")
        self._rotate_right = self._tool_button("↻", "向右旋转")
        self._copy_button = self._tool_button("复制", "复制图片")
        self._save_button = self._tool_button("另存为", "图片另存为")
        self._folder_button = self._tool_button("打开目录", "打开图片所在目录")
        self._fullscreen_button = self._tool_button("全屏", "切换全屏")
        self._close_button = self._tool_button("关闭", "关闭图片查看器")
        self._close_button.setObjectName("imageViewerClose")
        for button in (
            self._zoom_out,
            self._zoom_in,
            self._fit_button,
            self._actual_button,
            self._rotate_left,
            self._rotate_right,
            self._copy_button,
            self._save_button,
            self._folder_button,
            self._fullscreen_button,
        ):
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        self._info_label = QLabel()
        self._info_label.setObjectName("imageViewerInfo")
        toolbar.addWidget(self._info_label)
        toolbar.addWidget(self._close_button)

        self._scroll_area = QScrollArea()
        self._scroll_area.setObjectName("imageViewerArea")
        self._scroll_area.setWidgetResizable(False)
        self._scroll_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll_area.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._image_label = _ImageCanvas(self._scroll_area)
        self._image_label.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._image_label.customContextMenuRequested.connect(
            self._show_image_context_menu
        )
        self._scroll_area.setWidget(self._image_label)
        self._previous_button.setParent(self._scroll_area.viewport())
        self._next_button.setParent(self._scroll_area.viewport())
        self._previous_button.show()
        self._next_button.show()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._scroll_area, 1)
        layout.addLayout(toolbar)

        self._zoom_out.clicked.connect(lambda: self._change_zoom(-1))
        self._zoom_in.clicked.connect(lambda: self._change_zoom(1))
        self._previous_button.clicked.connect(lambda: self._navigate(-1))
        self._next_button.clicked.connect(lambda: self._navigate(1))
        self._fit_button.clicked.connect(self._fit_to_window)
        self._actual_button.clicked.connect(self._show_actual_size)
        self._rotate_left.clicked.connect(lambda: self._rotate(-90))
        self._rotate_right.clicked.connect(lambda: self._rotate(90))
        self._copy_button.clicked.connect(self._copy_image)
        self._save_button.clicked.connect(self._save_image)
        self._folder_button.clicked.connect(self._open_image_folder)
        self._fullscreen_button.clicked.connect(self._toggle_fullscreen)
        self._close_button.clicked.connect(self.close)
        self._image_label.wheel_zoom.connect(self._change_zoom)

        width = min(max(640, pixmap.width() + 48), 1400)
        height = min(max(480, pixmap.height() + 120), 1000)
        self.resize(width, height)
        self._render()
        self._update_navigation()

    @classmethod
    def show_shared(
        cls,
        pixmap: QPixmap,
        title: str,
        image_path: str | None,
        gallery: tuple[tuple[str, str], ...],
        gallery_index: int,
    ) -> "ImageViewerDialog":
        """Show one process-wide viewer and replace its current image."""
        viewer = cls._shared_viewer
        if viewer is None:
            viewer = cls(pixmap, title, image_path, None, gallery, gallery_index)
            cls._shared_viewer = viewer
        else:
            viewer._gallery = gallery or ((title or "图片", image_path or ""),)
            viewer._gallery_index = max(
                0, min(gallery_index, len(viewer._gallery) - 1)
            )
            viewer._source_pixmap = pixmap
            viewer._image_path = image_path
            viewer._rotation = 0
            viewer._scale = 1.0
            viewer._fit_mode = True
            viewer.setWindowTitle(title or "图片预览")
            viewer._render()
            viewer._update_navigation()
        viewer.show()
        return viewer

    @staticmethod
    def _tool_button(text: str, accessible_name: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("imageViewerTool")
        button.setAccessibleName(accessible_name)
        button.setToolTip(accessible_name)
        button.setMinimumHeight(30)
        return button

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit_mode:
            self._render()
        self._position_navigation_buttons()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._position_navigation_buttons()

    def _position_navigation_buttons(self) -> None:
        viewport = self._scroll_area.viewport()
        if viewport is None:
            return
        y = max(0, (viewport.height() - self._previous_button.height()) // 2)
        margin = 16
        self._previous_button.move(margin, y)
        self._next_button.move(
            max(margin, viewport.width() - self._next_button.width() - margin), y
        )
        self._previous_button.raise_()
        self._next_button.raise_()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        modifiers = event.modifiers()
        if key == Qt.Key.Key_Escape:
            self.close()
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._change_zoom(1)
        elif key == Qt.Key.Key_Minus:
            self._change_zoom(-1)
        elif key == Qt.Key.Key_0:
            self._show_actual_size()
        elif key == Qt.Key.Key_F:
            self._fit_to_window()
        elif key == Qt.Key.Key_Left:
            self._navigate(-1)
        elif key == Qt.Key.Key_Right:
            self._navigate(1)
        elif key == Qt.Key.Key_R:
            self._rotate(90)
        elif key == Qt.Key.Key_F11:
            self._toggle_fullscreen()
        elif key == Qt.Key.Key_C and modifiers & Qt.KeyboardModifier.ControlModifier:
            self._copy_image()
        elif key == Qt.Key.Key_S and modifiers & Qt.KeyboardModifier.ControlModifier:
            self._save_image()
        else:
            super().keyPressEvent(event)

    def _transformed_source(self) -> QPixmap:
        if not self._rotation:
            return self._source_pixmap
        return self._source_pixmap.transformed(
            QTransform().rotate(self._rotation), Qt.TransformationMode.SmoothTransformation
        )

    def _render(self) -> None:
        source = self._transformed_source()
        if source.isNull():
            self._image_label.clear()
            return
        scale = self._scale
        if self._fit_mode:
            scale = self._fit_scale(source)
        size = source.size() * max(0.05, min(scale, 8.0))
        rendered = source.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(rendered)
        self._image_label.setFixedSize(rendered.size())
        self._info_label.setText(
            f"{self._source_pixmap.width()} × {self._source_pixmap.height()}  ·  {scale * 100:.0f}%  ·  {self._rotation % 360}°"
            + (f"  ·  {self._gallery_index + 1}/{len(self._gallery)}" if len(self._gallery) > 1 else "")
        )

    def _fit_scale(self, source: QPixmap | None = None) -> float:
        source = source or self._transformed_source()
        available = self._scroll_area.viewport().size()
        if source.isNull() or available.width() <= 0 or available.height() <= 0:
            return 1.0
        return min(
            available.width() / source.width(),
            available.height() / source.height(),
            1.0,
        )

    def _update_navigation(self) -> None:
        has_previous = self._gallery_index > 0
        has_next = self._gallery_index < len(self._gallery) - 1
        self._previous_button.setEnabled(has_previous)
        self._next_button.setEnabled(has_next)
        self._previous_button.setVisible(len(self._gallery) > 1)
        self._next_button.setVisible(len(self._gallery) > 1)

    def _navigate(self, offset: int) -> None:
        target = self._gallery_index + offset
        if not 0 <= target < len(self._gallery):
            return
        label, path = self._gallery[target]
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        self._gallery_index = target
        self._source_pixmap = pixmap
        self._image_path = str(path)
        self._rotation = 0
        self._fit_mode = True
        self._scale = 1.0
        self.setWindowTitle(label or "图片预览")
        self._render()
        self._update_navigation()

    def _change_zoom(self, direction: int) -> None:
        if direction == 0:
            return
        if self._fit_mode:
            self._scale = self._fit_scale()
        self._fit_mode = False
        self._scale = max(0.05, min(8.0, self._scale * (1.25 if direction > 0 else 0.8)))
        self._render()

    def _fit_to_window(self) -> None:
        self._fit_mode = True
        self._render()

    def _show_actual_size(self) -> None:
        self._fit_mode = False
        self._scale = 1.0
        self._render()

    def _rotate(self, degrees: int) -> None:
        self._rotation = (self._rotation + degrees) % 360
        self._render()

    def _copy_image(self) -> None:
        QApplication.clipboard().setPixmap(self._source_pixmap)

    def _show_image_context_menu(self, position) -> None:
        menu = self._build_image_context_menu()
        menu.exec(self._image_label.mapToGlobal(position))

    def _build_image_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("复制图片", self._copy_image)
        menu.addAction("图片另存为", self._save_image)
        menu.addAction("打开图片所在目录", self._open_image_folder)
        menu.addSeparator()
        menu.addAction("适应窗口", self._fit_to_window)
        menu.addAction("原始尺寸", self._show_actual_size)
        return menu

    def _save_image(self) -> None:
        default_name = Path(self._image_path).name if self._image_path else "image.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "图片另存为", default_name, "Images (*.png *.jpg *.jpeg *.webp);;All files (*)"
        )
        if path:
            image_format = Path(path).suffix.lstrip(".").upper() or "PNG"
            if image_format == "JPG":
                image_format = "JPEG"
            self._source_pixmap.save(path, image_format)

    def _open_image_folder(self) -> None:
        if self._image_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self._image_path).parent)))

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._fullscreen_button.setText("全屏")
        else:
            self.showFullScreen()
            self._fullscreen_button.setText("退出全屏")


class QuotePreviewCard(QFrame):
    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("quotePreview")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("引用的原消息")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 6, 8, 6)
        layout.setSpacing(8)

        accent = QFrame()
        accent.setObjectName("quoteAccent")
        accent.setFixedWidth(3)
        accent.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(accent)

        labels = QVBoxLayout()
        labels.setContentsMargins(0, 0, 0, 0)
        labels.setSpacing(2)
        self.sender_label = QLabel()
        self.sender_label.setObjectName("quoteSender")
        self.sender_label.setTextFormat(Qt.TextFormat.PlainText)
        self.sender_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.content_label = QLabel()
        self.content_label.setObjectName("quoteContent")
        self.content_label.setTextFormat(Qt.TextFormat.PlainText)
        self.content_label.setWordWrap(True)
        self.content_label.setMaximumHeight(38)
        self.content_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        labels.addWidget(self.sender_label)
        labels.addWidget(self.content_label)
        layout.addLayout(labels, 1)

    def set_preview(self, preview: QuotePreview) -> None:
        self.sender_label.setText(preview.sender_name or "原消息")
        summary = {
            ContentType.IMAGE: "[图片]",
            ContentType.VOICE: "[语音]",
            ContentType.VIDEO: "[视频]",
            ContentType.FILE: "[文件/链接/卡片]",
        }.get(preview.content_type)
        if summary is None:
            summary = " ".join(preview.content.split()) or "[原消息]"
            if len(summary) > 96:
                summary = f"{summary[:95]}…"
        self.content_label.setText(summary)
        self.setToolTip("点击定位到原消息")
        self.setAccessibleName(
            f"引用 {preview.sender_name or '原消息'} 的消息：{summary}"
        )

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class MessageCard(QFrame):
    typing_advanced = Signal()
    quote_requested = Signal(str)

    def __init__(
        self,
        entry: TimelineEntry,
        retry_callback=None,
        cancel_callback=None,
        resend_callback=None,
        image_gallery_provider=None,
    ) -> None:
        super().__init__()
        role = entry.direction if entry.direction in {"inbound", "outbound"} else "system"
        self.setProperty("messageRole", role)
        # Let the timeline decide the card width when the companion is narrow.
        # Without an explicit zero minimum, a long unbroken path/URL can make a
        # card wider than the viewport after switching conversations.
        self.setMinimumWidth(0)
        self.setMaximumWidth(560)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        self._retry_callback = retry_callback
        self._cancel_callback = cancel_callback
        self._resend_callback = resend_callback
        self._delivery_ids: tuple[str, ...] = ()
        self._copy_text = ""
        self._image_path: str | None = None
        self._image_gallery_provider = image_gallery_provider
        self._image_viewer: ImageViewerDialog | None = None
        self._target_content = ""
        self._displayed_content = ""
        self._typewriter_live = False
        self._typing_status: str | None = None
        self._entry_id = entry.entry_id
        self._cursor_visible = True
        self._cursor_ticks = 0
        self._typewriter_timer = QTimer(self)
        self._typewriter_timer.setInterval(24)
        self._typewriter_timer.timeout.connect(self._advance_typewriter)
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.setInterval(1200)
        self._highlight_timer.timeout.connect(self._clear_quote_target_highlight)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(6)

        self.meta_label = QLabel()
        self.meta_label.setObjectName("messageMeta")
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.quote_preview = QuotePreviewCard()
        self.quote_preview.clicked.connect(
            lambda: self.quote_requested.emit(self._entry_id)
        )
        self.quote_preview.hide()
        self.content_label = QLabel()
        self.content_label.setTextFormat(Qt.TextFormat.PlainText)
        self.content_label.setWordWrap(True)
        self.content_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.content_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.content_label.setMinimumWidth(0)
        layout.addWidget(self.meta_label)
        layout.addWidget(self.quote_preview)
        layout.addWidget(self.content_label)
        self.image_preview = ClickableImageLabel()
        self.image_preview.setObjectName("messageImagePreview")
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setMinimumSize(120, 80)
        self.image_preview.setMaximumSize(320, 220)
        self.image_preview.setScaledContents(False)
        self.image_preview.setCursor(Qt.CursorShape.PointingHandCursor)
        self.image_preview.clicked.connect(self._open_image_viewer)
        self.image_preview.hide()
        layout.addWidget(self.image_preview)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 3, 0, 0)
        footer.setSpacing(6)
        self.status_dot = QFrame()
        self.status_dot.setObjectName("messageStatusDot")
        self.status_dot.setFixedSize(6, 6)
        self.status_label = QLabel()
        self.status_label.setObjectName("messageStatus")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        footer.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        footer.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignVCenter)
        footer.addStretch(1)

        self.copy_button = None
        self.resend_button = None
        self.retry_button = None
        self.cancel_button = None
        if entry.direction == "outbound":
            self.copy_button = self._action_button("复制", "copy", "复制这条消息")
            self.copy_button.clicked.connect(self._copy_content)
            footer.addWidget(self.copy_button)
            if resend_callback is not None:
                self.resend_button = self._action_button(
                    "再次发送", "resend", "再次发送这条微信消息"
                )
                self.resend_button.clicked.connect(self._resend_current)
                footer.addWidget(self.resend_button)
            if retry_callback is not None:
                self.retry_button = self._action_button(
                    "重新发送", "retry", "重新发送失败的微信消息"
                )
                self.retry_button.clicked.connect(self._retry_current)
                footer.addWidget(self.retry_button)
            if cancel_callback is not None:
                self.cancel_button = self._action_button(
                    "取消发送", "cancel", "取消排队中的微信消息"
                )
                self.cancel_button.clicked.connect(self._cancel_current)
                footer.addWidget(self.cancel_button)
        layout.addLayout(footer)
        self.update_entry(entry)

    @staticmethod
    def _action_button(
        text: str, icon_name: str, accessible_name: str
    ) -> MessageActionButton:
        return MessageActionButton(text, icon_name, accessible_name)

    def _copy_content(self) -> None:
        QApplication.clipboard().setText(self._copy_text)

    def _resend_current(self) -> None:
        if self._resend_callback is not None:
            self._invoke_each(self._resend_callback, self._delivery_ids)

    def _retry_current(self) -> None:
        if self._retry_callback is not None:
            self._invoke_each(self._retry_callback, self._delivery_ids)

    def _cancel_current(self) -> None:
        if self._cancel_callback is not None:
            self._invoke_each(self._cancel_callback, self._delivery_ids)

    def _open_image_viewer(self) -> None:
        if not self._image_path:
            return
        pixmap = QPixmap(self._image_path)
        if pixmap.isNull():
            return
        gallery = (
            tuple(self._image_gallery_provider())
            if self._image_gallery_provider is not None
            else ()
        )
        if not gallery:
            gallery = ((self.image_preview.accessibleName() or "图片", self._image_path),)
        gallery_index = next(
            (index for index, (_label, path) in enumerate(gallery) if str(path) == self._image_path),
            0,
        )
        self._image_viewer = ImageViewerDialog.show_shared(
            pixmap,
            self.image_preview.accessibleName() or "图片预览",
            self._image_path,
            gallery,
            gallery_index,
        )

    @staticmethod
    def _invoke_each(callback, delivery_ids: tuple[str, ...]) -> None:
        for delivery_id in delivery_ids:
            callback(delivery_id)

    def update_entry(self, entry: TimelineEntry) -> None:
        self._entry_id = entry.entry_id
        timestamp = entry.created_at.astimezone().strftime("%H:%M:%S")
        sender = "我" if entry.direction == "outbound" else entry.sender_name
        delivery_status = {
            "generating": "正在生成",
            "generated": "等待发送",
            "generation_failed": "生成失败",
            "queued": "等待发送",
            "waiting_for_idle": "等待你停止操作",
            "sending": "发送中",
            "retrying": "重试中",
            "sent": "已发送",
            "failed": "发送失败",
            "expired": "已过期",
            "unavailable": "静默发送不可用",
            "foreground_unavailable": "前台备用未授权",
            "foreground_sending": "前台发送中",
            "foreground_sent": "已发送",
            "delivery_unknown": "发送结果未知",
            "cancelled": "已取消",
        }.get(entry.delivery_status or "")
        self.meta_label.setText(f"{sender}  ·  {timestamp}")
        if entry.quote is None:
            self.quote_preview.hide()
        else:
            self.quote_preview.set_preview(entry.quote)
            self.quote_preview.show()
        self._update_image_preview(entry)
        placeholder = {
            "generation_failed": "Agent 生成失败",
            "cancelled": "任务已取消",
        }.get(entry.delivery_status or "", "正在生成回复…")
        self._copy_text = entry.content
        self._typing_status = entry.delivery_status
        if (
            entry.direction == "outbound"
            and not entry.historical
            and (self._typewriter_live or entry.delivery_status == "generating")
        ):
            self._typewriter_live = True
            self._set_typewriter_target(entry.content)
        else:
            self._typewriter_timer.stop()
            self._target_content = entry.content or placeholder
            self._displayed_content = self._target_content
            self._cursor_visible = False
            self.content_label.setText(self._displayed_content)
        if self.copy_button is not None:
            self.copy_button.setEnabled(bool(entry.content))
        self._delivery_ids = entry.delivery_ids or (
            (entry.delivery_id,) if entry.delivery_id else ()
        )
        status_text = delivery_status or ""
        self.status_label.setText(status_text)
        self.status_label.setVisible(bool(status_text))
        self.status_dot.setVisible(bool(status_text))
        status_state = self._status_state(entry.delivery_status)
        self.status_label.setProperty("statusState", status_state)
        self.status_dot.setProperty("statusState", status_state)
        self.status_label.setToolTip(entry.status_detail)
        self.status_dot.setToolTip(entry.status_detail)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)
        if self.resend_button is not None:
            self.resend_button.setVisible(
                bool(self._delivery_ids)
                and entry.delivery_status in {"sent", "foreground_sent"}
            )
        if self.retry_button is not None:
            self.retry_button.setVisible(
                bool(self._delivery_ids)
                and entry.delivery_status
                in {
                    "failed",
                    "expired",
                    "unavailable",
                    "foreground_unavailable",
                }
            )
        if self.cancel_button is not None:
            self.cancel_button.setVisible(
                bool(self._delivery_ids)
                and entry.delivery_status
                in {"queued", "waiting_for_idle", "retrying"}
            )

    def flash_quote_target(self) -> None:
        self._highlight_timer.start()
        self.setProperty("quoteTarget", True)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _clear_quote_target_highlight(self) -> None:
        self.setProperty("quoteTarget", False)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _update_image_preview(self, entry: TimelineEntry) -> None:
        attachment = next(
            (item for item in entry.attachments if item.kind == "image"), None
        )
        if attachment is None:
            self._image_path = None
            self.image_preview.clear()
            self.image_preview.hide()
            return
        self._image_path = str(attachment.path or "")
        alt = str(attachment.metadata.get("alt") or attachment.name or "图片")
        self.image_preview.setAccessibleName(alt)
        self.image_preview.setToolTip(f"{alt}（点击查看大图）")
        source = QPixmap(str(attachment.path or ""))
        if source.isNull():
            self.image_preview.setPixmap(QPixmap())
            self.image_preview.setText("图片预览不可用")
        else:
            self.image_preview.setText("")
            self.image_preview.setPixmap(
                source.scaled(
                    320,
                    220,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        self.image_preview.show()

    @property
    def typewriter_active(self) -> bool:
        return self._typewriter_timer.isActive()

    def _set_typewriter_target(self, content: str) -> None:
        target = content or ""
        if not target.startswith(self._displayed_content):
            self._displayed_content = ""
        self._target_content = target
        self._cursor_visible = True
        self._cursor_ticks = 0
        if (
            len(self._displayed_content) < len(self._target_content)
            or self._typing_status == "generating"
        ):
            self._typewriter_timer.start()
        else:
            self._typewriter_timer.stop()
            self._cursor_visible = False
        self._render_typewriter()

    def _advance_typewriter(self) -> None:
        backlog = len(self._target_content) - len(self._displayed_content)
        changed = False
        if backlog > 0:
            step = 6 if backlog > 120 else 3 if backlog > 48 else 1
            end = min(len(self._target_content), len(self._displayed_content) + step)
            self._displayed_content = self._target_content[:end]
            self._cursor_visible = True
            self._cursor_ticks = 0
            changed = True
        elif self._typing_status == "generating":
            self._cursor_ticks += 1
            if self._cursor_ticks >= 15:
                self._cursor_ticks = 0
                self._cursor_visible = not self._cursor_visible
                changed = True
        else:
            self._typewriter_timer.stop()
            if self._cursor_visible:
                self._cursor_visible = False
                changed = True
        self._render_typewriter()
        if changed:
            self.typing_advanced.emit()

    def _render_typewriter(self) -> None:
        cursor = "▍" if self._cursor_visible and self._typewriter_timer.isActive() else ""
        self.content_label.setText(f"{self._displayed_content}{cursor}")

    @staticmethod
    def _status_state(status: str | None) -> str:
        if status in {"sent", "foreground_sent"}:
            return "success"
        if status in {
            "generation_failed",
            "failed",
            "expired",
            "unavailable",
            "foreground_unavailable",
            "delivery_unknown",
        }:
            return "error"
        if status in {"waiting_for_idle", "retrying", "cancelled"}:
            return "warning"
        return "info"


def _avatar_text(display_name: str) -> str:
    stripped = display_name.strip()
    return stripped[:1].upper() if stripped else "?"


def _load_avatar_pixmap(path: str | Path | None, size: int) -> QPixmap | None:
    if path is None:
        return None
    source = QPixmap(str(path))
    if source.isNull():
        return None
    scaled = source.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    left = max(0, (scaled.width() - size) // 2)
    top = max(0, (scaled.height() - size) // 2)
    cropped = scaled.copy(left, top, size, size)
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(0, 0, size, size)
    painter.setClipPath(clip)
    painter.drawPixmap(0, 0, cropped)
    painter.end()
    return result
