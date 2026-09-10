from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)


class HeaderEditor(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._invalid = None
        self._has_invalid = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["头名称", "头值", "操作"])
        self.table.setAccessibleName("自定义请求头")
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 165)
        self.table.setColumnWidth(2, 76)
        self.table.setMinimumHeight(145)
        self.table.setMaximumHeight(190)
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        self.add_button = QPushButton("添加请求头")
        actions.addWidget(self.add_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.add_button.clicked.connect(self._add)

    def values(self):
        if self._has_invalid:
            return deepcopy(self._invalid)
        rows = [{"name": self.table.cellWidget(i, 0).text(), "value": self.table.cellWidget(i, 1).text()}
                for i in range(self.table.rowCount())]
        # Retain duplicate drafts for inline validation and failed-save feedback.
        if len({row["name"] for row in rows}) != len(rows):
            return rows
        return {row["name"]: row["value"] for row in rows}

    def set_values(self, value):
        self._invalid = None
        self._has_invalid = False
        self.table.setRowCount(0)
        if isinstance(value, dict):
            rows = list(value.items())
        elif isinstance(value, list) and all(isinstance(r, dict) and set(r) == {"name", "value"} for r in value):
            rows = [(r["name"], r["value"]) for r in value]
        else:
            rows = []
            self._invalid = deepcopy(value)
            self._has_invalid = True
        # Preserve malformed YAML rather than coercing values into valid credentials.
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in rows):
            self._invalid = deepcopy(value)
            rows = []
            self._has_invalid = True
        for name, content in rows:
            self._append(name, content)
        self.table.setEnabled(not self._has_invalid)
        self.add_button.setEnabled(not self._has_invalid and len(rows) < 32)

    def _append(self, name="", content=""):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, 42)
        key, value = QLineEdit(name), QLineEdit(content)
        key.setAccessibleName("请求头名称")
        value.setAccessibleName("请求头值")
        key.setPlaceholderText("Authorization")
        value.setPlaceholderText("Bearer …")
        value.setEchoMode(QLineEdit.EchoMode.Normal)
        remove = QPushButton("删除")
        remove.setAccessibleName("删除此请求头")
        for column, widget in enumerate((key, value, remove)):
            self.table.setCellWidget(row, column, widget)
        key.textChanged.connect(lambda _: self.changed.emit())
        value.textChanged.connect(lambda _: self.changed.emit())
        remove.clicked.connect(lambda: self._remove(value))

    def _add(self):
        if self._has_invalid or self.table.rowCount() >= 32:
            return
        self._append()
        self.add_button.setEnabled(self.table.rowCount() < 32)
        self.changed.emit()

    def _remove(self, value_widget):
        for row in range(self.table.rowCount()):
            if self.table.cellWidget(row, 1) is value_widget:
                self.table.removeRow(row)
                self.add_button.setEnabled(True)
                self.changed.emit()
                return
