from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

from agent_bridge.companion.theme import LIGHT


class StartupPanel(QWidget):
    """In-place loading content, never a second top-level window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"QProgressBar {{ border: 1px solid {LIGHT.border}; border-radius: 4px; height: 8px; }}"
            f"QProgressBar::chunk {{ background: {LIGHT.primary}; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addStretch(1)
        title = QLabel("正在准备工作台")
        title.setObjectName("conversationTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setWordWrap(True)
        font = title.font()
        font.setPointSize(16)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        self.stage = QLabel("正在连接微信")
        self.stage.setTextFormat(Qt.TextFormat.PlainText)
        self.stage.setWordWrap(True)
        layout.addWidget(self.stage)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(8)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("工作台正在加载")
        layout.addWidget(self.progress)
        self.elapsed = QLabel()
        layout.addWidget(self.elapsed)
        hint = QLabel(
            "正在后台加载，窗口可正常移动和收起。\n加载完成后，会话和消息会显示在这里。"
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        self.started = time.monotonic()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._update_elapsed)
        self._update_elapsed()

    def start(self):
        self.started = time.monotonic()
        self.timer.start()
        self._update_elapsed()

    def finish(self):
        self.timer.stop()

    def fail(self):
        self.finish()
        self.progress.hide()
        self.stage.setText("加载失败，请查看管理面板日志。关闭工作台后可重新启动。")

    def set_stage(self, text: str):
        self.stage.setText(text)

    def _update_elapsed(self):
        self.elapsed.setText(f"已等待 {int(time.monotonic() - self.started)} 秒")

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)
