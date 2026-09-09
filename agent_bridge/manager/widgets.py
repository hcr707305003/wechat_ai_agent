from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QListWidget,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from agent_bridge.agents.availability import AgentAvailability


def set_agent_choices(combo: QComboBox, states: dict[str, AgentAvailability], *,
                      select_available: bool = False) -> None:
    """Disable native items (mouse AND keyboard); preserve saved binding values."""
    # The native Windows combo menu delegate ignores item foreground colors.
    if not isinstance(combo.itemDelegate(), QStyledItemDelegate):
        combo.setItemDelegate(QStyledItemDelegate(combo))
    combo.view().setStyleSheet("QAbstractItemView::item:disabled { color: #94A3B8; }")
    for index in range(combo.count()):
        provider = combo.itemData(index) or combo.itemText(index).lower()
        state = states.get(provider, AgentAvailability(False, "等待环境检查"))
        item = combo.model().item(index)
        item.setEnabled(state.available)
        item.setData(None if state.available else QColor("#94A3B8"), Qt.ItemDataRole.ForegroundRole)
        item.setToolTip(state.reason)
    current = combo.model().item(combo.currentIndex())
    if select_available and (current is None or not current.isEnabled()):
        combo.setCurrentIndex(next((i for i in range(combo.count())
                                   if combo.model().item(i).isEnabled()), -1))
    current = combo.model().item(combo.currentIndex())
    combo.setToolTip(current.toolTip() if current else "没有可用 Agent")


class StringListEditor(QWidget):
    def __init__(self, placeholder: str = "输入内容后添加", parent=None) -> None:
        super().__init__(parent)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.input = QLineEdit()
        self.input.setPlaceholderText(placeholder)
        self.input.returnPressed.connect(self._add)
        add = QPushButton("添加")
        remove = QPushButton("删除")
        up = QPushButton("上移")
        down = QPushButton("下移")
        add.clicked.connect(self._add)
        remove.clicked.connect(self._remove)
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(self.input, 1)
        controls.addWidget(add)
        controls.addWidget(remove)
        controls.addWidget(up)
        controls.addWidget(down)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list)
        layout.addLayout(controls)

    def values(self) -> list[str]:
        return [self.list.item(index).text() for index in range(self.list.count())]

    def set_values(self, values: list[str] | tuple[str, ...] | None) -> None:
        self.list.clear()
        for value in values or ():
            text = str(value).strip()
            if text:
                self.list.addItem(text)

    def _add(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        if text not in self.values():
            self.list.addItem(text)
        self.input.clear()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)

    def _move(self, offset: int) -> None:
        row = self.list.currentRow()
        target = row + offset
        if row < 0 or target < 0 or target >= self.list.count():
            return
        item = self.list.takeItem(row)
        self.list.insertItem(target, item)
        self.list.setCurrentRow(target)


class SessionBindingsEditor(QWidget):
    HEADERS = ("会话名称或 ID", "类型", "Agent", "Session ID")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._agent_states: dict[str, AgentAvailability] | None = None
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        add = QPushButton("新增绑定")
        remove = QPushButton("删除")
        up = QPushButton("上移")
        down = QPushButton("下移")
        add.clicked.connect(self.add_row)
        remove.clicked.connect(self._remove)
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(add)
        controls.addWidget(remove)
        controls.addWidget(up)
        controls.addWidget(down)
        controls.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)
        layout.addLayout(controls)

    def add_row(self, value: dict[str, Any] | None = None) -> None:
        value = value or {}
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(
            row, 0, QTableWidgetItem(str(value.get("conversation_id", "")))
        )
        type_combo = QComboBox()
        type_combo.addItems(["private", "group"])
        type_combo.setCurrentText(str(value.get("conversation_type") or "private"))
        self.table.setCellWidget(row, 1, type_combo)
        provider = QComboBox()
        provider.addItems(["codex", "claude"])
        provider.setCurrentText(str(value.get("provider") or "codex"))
        if self._agent_states is not None:
            set_agent_choices(provider, self._agent_states, select_available=not value)
        self.table.setCellWidget(row, 2, provider)
        self.table.setItem(row, 3, QTableWidgetItem(str(value.get("session_id", ""))))
        self.table.setCurrentCell(row, 0)

    def set_agent_availability(self, states: dict[str, AgentAvailability]) -> None:
        self._agent_states = states
        for row in range(self.table.rowCount()):
            set_agent_choices(self.table.cellWidget(row, 2), states)

    def values(self) -> list[dict[str, str]]:
        result = []
        for row in range(self.table.rowCount()):
            conversation = self.table.item(row, 0)
            session = self.table.item(row, 3)
            type_combo = self.table.cellWidget(row, 1)
            provider = self.table.cellWidget(row, 2)
            conversation_id = conversation.text().strip() if conversation else ""
            session_id = session.text().strip() if session else ""
            if not conversation_id and not session_id:
                continue
            result.append(
                {
                    "conversation_id": conversation_id,
                    "conversation_type": type_combo.currentText(),
                    "provider": provider.currentText(),
                    "session_id": session_id,
                }
            )
        return result

    def set_values(self, values: list[dict[str, Any]] | None) -> None:
        self.table.setRowCount(0)
        for value in values or ():
            self.add_row(value)

    def _remove(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _move(self, offset: int) -> None:
        row = self.table.currentRow()
        target = row + offset
        if row < 0 or target < 0 or target >= self.table.rowCount():
            return
        values = self.values()
        values[row], values[target] = values[target], values[row]
        self.set_values(values)
        self.table.setCurrentCell(target, 0)
