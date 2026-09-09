from __future__ import annotations

from queue import Empty
from time import monotonic
from weakref import WeakMethod

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from agent_bridge.manager.agent_debug import AgentDebugRunner, DebugEvent


class AgentDebugPanel(QWidget):
    def __init__(self, config_loader, parent=None, *, runner=None) -> None:
        super().__init__(parent)
        # Avoid a child -> bound parent method -> child ownership cycle in PySide.
        self._config_loader = (
            WeakMethod(config_loader)
            if getattr(config_loader, "__self__", None) is not None
            else lambda: config_loader
        )
        self.runner = runner or AgentDebugRunner()
        self._active = False
        self._target = ""
        self._typed = ""
        self._terminal = None
        self._started = 0.0
        self._reply_start = 0
        self._stop_requested = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        title = QLabel("Agent 聊天调试")
        title.setObjectName("sectionTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.provider = QComboBox()
        self.provider.addItem("Codex", "codex")
        self.provider.addItem("Claude", "claude")
        self.provider.setMinimumWidth(110)
        self.probe_button = QPushButton("测试连接")
        self.clear_button = QPushButton("新建对话")
        heading.addWidget(self.provider)
        heading.addWidget(self.probe_button)
        heading.addWidget(self.clear_button)
        layout.addLayout(heading)
        hint = QLabel("独立调试 · 使用当前表单配置 · 不发送微信消息")
        hint.setToolTip("使用当前模型、权限和工作目录，不修改 config.yaml。")
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.status = QLabel("未测试 · 点击测试连接，或直接发送一条消息。")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setObjectName("checkProgress")
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("回复会在这里逐字显示，支持连续对话。")
        self.transcript.setMinimumHeight(80)
        layout.addWidget(self.transcript, 1)
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("输入调试消息…  Ctrl+Enter 发送")
        self.input.setFixedHeight(64)
        layout.addWidget(self.input)
        actions = QHBoxLayout()
        actions.addStretch()
        self.stop_button = QPushButton("停止生成")
        self.stop_button.setEnabled(False)
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("primaryButton")
        actions.addWidget(self.stop_button)
        actions.addWidget(self.send_button)
        layout.addLayout(actions)
        self.send_button.clicked.connect(self._send)
        self.probe_button.clicked.connect(self._probe)
        self.stop_button.clicked.connect(self._stop)
        self.clear_button.clicked.connect(self._clear)
        self.provider.currentIndexChanged.connect(self._clear)
        self._shortcut = QShortcut(QKeySequence("Ctrl+Return"), self.input)
        self._shortcut.activated.connect(self._send)
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._tick)

    def _set_status(self, text, color="#64748B") -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")

    def _set_busy(self, busy) -> None:
        self._active = busy
        for widget in (
            self.send_button,
            self.probe_button,
            self.clear_button,
            self.provider,
            self.input,
        ):
            widget.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.progress.setVisible(busy)

    def _probe(self) -> None:
        self._send_prompt(
            "这是一条连接测试。请只回复：连接测试通过。不要调用工具或修改文件。"
        )

    def _send(self) -> None:
        prompt = self.input.toPlainText().strip()
        if prompt and self._send_prompt(prompt):
            self.input.clear()

    def _send_prompt(self, prompt) -> bool:
        if self._active:
            return False
        try:
            loader = self._config_loader()
            if loader is None:
                return False
            config = loader()
            self.runner.start(config, self.provider.currentData(), prompt)
        except Exception as error:  # noqa: BLE001 - display configuration / worker errors inline
            self._set_status(f"无法开始调试：{error}", "#B91C1C")
            return False
        self.transcript.appendPlainText(
            f"你\n{prompt}\n\n{self.provider.currentText()}\n"
        )
        self._reply_start = self.transcript.document().characterCount() - 1
        self._target = self._typed = ""
        self._terminal = None
        self._stop_requested = False
        self._started = monotonic()
        self._set_busy(True)
        self._set_status("连接中…", "#0F766E")
        self._timer.start()
        return True

    def _stop(self) -> None:
        self._stop_requested = True
        self._target = self._typed
        self.runner.cancel()
        if not self.runner.busy:
            self._terminal = DebugEvent("cancelled", "已停止生成。")
        self.stop_button.setEnabled(False)
        self._set_status("正在停止…")

    def _clear(self) -> None:
        if self._active:
            return
        self.runner.reset()
        self.transcript.clear()
        self._set_status("新对话 · 可测试连接或发送消息。")

    def _tick(self) -> None:
        for _ in range(200):
            try:
                event = self.runner.events.get_nowait()
            except Empty:
                break
            if event.kind == "delta":
                if self._stop_requested:
                    continue
                self._target += event.text
                self._set_status("正在生成…", "#0F766E")
            elif event.kind == "status":
                if self._stop_requested:
                    continue
                self._set_status(event.text, "#0F766E")
            else:
                if self._stop_requested:
                    event = DebugEvent(
                        "cancelled", "已停止生成；下次发送会创建新的调试会话。"
                    )
                self._terminal = event
                if event.kind == "done":
                    if not event.text.startswith(self._typed):
                        cursor = QTextCursor(self.transcript.document())
                        cursor.setPosition(self._reply_start)
                        cursor.movePosition(
                            QTextCursor.MoveOperation.End,
                            QTextCursor.MoveMode.KeepAnchor,
                        )
                        cursor.removeSelectedText()
                        self._typed = ""
                    self._target = event.text
        remaining = self._target[len(self._typed) :]
        if remaining:
            chunk = remaining[: max(1, min(64, len(remaining) // 8 + 1))]
            scrollbar = self.transcript.verticalScrollBar()
            follow = scrollbar.value() >= scrollbar.maximum() - 4
            old_scroll = scrollbar.value()
            cursor = QTextCursor(self.transcript.document())
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(chunk)
            scrollbar.setValue(scrollbar.maximum() if follow else old_scroll)
            self._typed += chunk
        if (
            self._terminal is not None
            and self._typed == self._target
            and not self.runner.busy
        ):
            event = self._terminal
            elapsed = monotonic() - self._started
            if event.kind == "done":
                self._set_status(f"调试通过 · {elapsed:.1f} 秒", "#15803D")
            else:
                if self._stop_requested:
                    self.runner.reset()
                self.transcript.appendPlainText(f"\n[{event.text}]")
                self._set_status(
                    event.text, "#B91C1C" if event.kind == "error" else "#64748B"
                )
            self._set_busy(False)
            self._timer.stop()

    def shutdown(self) -> None:
        self._timer.stop()
        self.runner.cancel()
