from __future__ import annotations

import sys

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from agent_bridge.manager.doctor import CheckResult


def animations_enabled() -> bool:
    if sys.platform == "win32":
        import ctypes

        enabled = ctypes.c_int(1)
        if ctypes.windll.user32.SystemParametersInfoW(
            0x1042, 0, ctypes.byref(enabled), 0
        ):
            return bool(enabled.value)
    return True


class CheckIcon(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(26, 26)
        self.state = "waiting"
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._advance)

    def _advance(self) -> None:
        self._angle = (self._angle + 18) % 360
        self.update()

    def set_state(self, state: str) -> None:
        self.state = state
        if state == "running" and animations_enabled():
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = {
            "waiting": "#94A3B8",
            "running": "#15803D",
            "success": "#15803D",
            "failed": "#B91C1C",
        }[self.state]
        painter.setPen(
            QPen(QColor(color), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        )
        rect = QRectF(4, 4, 18, 18)
        if self.state == "running":
            painter.drawArc(rect, -self._angle * 16, 260 * 16)
        else:
            painter.drawEllipse(rect)
            if self.state == "success":
                painter.drawLine(QPointF(8, 13), QPointF(11, 16))
                painter.drawLine(QPointF(11, 16), QPointF(18, 9))
            elif self.state == "failed":
                painter.drawLine(QPointF(10, 10), QPointF(16, 16))
                painter.drawLine(QPointF(16, 10), QPointF(10, 16))
            else:
                painter.drawLine(QPointF(13, 8), QPointF(13, 13))
                painter.drawLine(QPointF(13, 13), QPointF(16, 15))


class CheckRow(QFrame):
    def __init__(self, name: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("checkRow")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.state = "waiting"
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(12)
        self.icon = CheckIcon(self)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(5)
        heading = QHBoxLayout()
        self.name = QLabel(name)
        self.name.setTextFormat(Qt.TextFormat.PlainText)
        self.name.setWordWrap(True)
        self.status = QLabel()
        heading.addWidget(self.name, 1)
        heading.addWidget(self.status)
        text.addLayout(heading)
        self.detail = QLabel()
        self.detail.setObjectName("checkDetail")
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setWordWrap(True)
        self.detail.setMinimumWidth(0)
        self.detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        text.addWidget(self.detail)
        layout.addLayout(text, 1)
        self.set_state("waiting")

    def set_state(self, state: str, result: CheckResult | None = None) -> None:
        self.state = state
        labels = {
            "waiting": "等待检测",
            "running": "正在检查…",
            "success": "通过",
            "failed": "失败",
        }
        self.status.setText(labels[state])
        color = {
            "waiting": "#64748B",
            "running": "#15803D",
            "success": "#15803D",
            "failed": "#B91C1C",
        }[state]
        self.status.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.icon.set_state(state)
        detail = ""
        if result is not None:
            detail = result.detail
            if result.suggestion:
                detail += "\n" + result.suggestion
        # Long Windows paths can contain no natural word-break opportunities.
        self.detail.setText(detail.replace("\\", "\\\u200b").replace("/", "/\u200b"))
        self.detail.setVisible(bool(detail))
        self.setAccessibleName(f"{self.name.text()}：{labels[state]}")
