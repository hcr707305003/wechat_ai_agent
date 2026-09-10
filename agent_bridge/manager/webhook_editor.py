from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import asdict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from agent_bridge.manager.header_editor import HeaderEditor
from agent_bridge.webhooks import HTTP_METHODS, WebhookSettings


class WebhookEditor(QWidget):
    """One endpoint at a time: no wide configuration table or hidden columns."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self._invalid_values = None
        self._loading = False
        layout = QVBoxLayout(self)
        hint = QLabel("仅推送白名单内的新消息；每个地址独立筛选，满足全部条件才推送。保存并重启工作台后生效。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        columns = QHBoxLayout()
        sidebar = QVBoxLayout()
        self.list = QListWidget()
        self.list.setObjectName("webhookEndpointList")
        self.list.setStyleSheet("""
            QListWidget#webhookEndpointList::item { padding: 8px 6px; }
            QListWidget#webhookEndpointList::item:selected { background: #CCFBF1; color: #115E59; }
        """)
        self.list.setAccessibleName("Webhook 地址列表")
        self.list.setMinimumWidth(140)
        self.list.setMaximumWidth(210)
        self.list.setMinimumHeight(270)
        sidebar.addWidget(self.list)
        actions = QHBoxLayout()
        self.add_button = QPushButton("添加")
        self.remove_button = QPushButton("删除")
        actions.addWidget(self.add_button)
        actions.addWidget(self.remove_button)
        sidebar.addLayout(actions)
        columns.addLayout(sidebar)
        self.form_widget = QWidget()
        form = QFormLayout(self.form_widget)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.name = QLineEdit()
        self.name.setMaxLength(80)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://your-server.example/webhook")
        self.method = QComboBox()
        self.method.addItems(HTTP_METHODS)
        self.headers = HeaderEditor()
        self.enabled = QCheckBox("启用此地址")
        self.conversation_type = QComboBox()
        for text, value in (("私聊和群聊", "all"), ("仅私聊", "private"), ("仅群聊", "group")):
            self.conversation_type.addItem(text, value)
        self.sender = QComboBox()
        for text, value in (("仅其他人", "others"), ("仅本人", "self"), ("本人和其他人", "all")):
            self.sender.addItem(text, value)
        self.include_ai = QCheckBox("允许推送 AI / 本程序回复")
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(1, 60)
        self.timeout.setDecimals(1)
        self.attempts = QSpinBox()
        self.attempts.setRange(1, 10)
        for label, widget in (("名称", self.name), ("推送 URL", self.url),
                              ("请求方式", self.method), ("自定义请求头", self.headers), ("状态", self.enabled),
                              ("会话类型", self.conversation_type), ("消息来源", self.sender),
                              ("AI 回复", self.include_ai), ("超时（秒）", self.timeout),
                              ("最多尝试（含首次）", self.attempts)):
            form.addRow(label, widget)
        note = QLabel("本人 = 当前登录微信账号。AI 回复也属于本人；“仅其他人”不会推送 AI 回复。\n"
                      "固定发送 JSON；图片仅提供类型信息，不上传文件。不补历史，重启不补发旧队列。\n"
                      "头值与配置文件/备份均为明文，请勿分享含密钥的截图或配置。")
        note.setWordWrap(True)
        note.setObjectName("mutedText")
        form.addRow(note)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setStyleSheet("color: #B91C1C;")
        self.error.setWordWrap(True)
        columns.addWidget(self.form_widget, 1)
        layout.addLayout(columns)
        layout.addWidget(self.error)
        self.list.currentRowChanged.connect(self._select)
        self.add_button.clicked.connect(self._add)
        self.remove_button.clicked.connect(self._remove)
        for field in (self.name, self.url):
            field.textChanged.connect(self._edited)
        for field in (self.enabled, self.include_ai):
            field.toggled.connect(self._edited)
        for field in (self.conversation_type, self.sender, self.method):
            field.currentIndexChanged.connect(self._edited)
        self.headers.changed.connect(self._edited)
        self.timeout.valueChanged.connect(self._edited)
        self.attempts.valueChanged.connect(self._edited)
        self._select(-1)

    def values(self):
        return deepcopy(self._invalid_values if self._invalid_values is not None else self._items)

    def set_values(self, values):
        self._loading = True
        self._invalid_values = None
        if values is not None and (not isinstance(values, list) or any(not isinstance(v, dict) for v in values)):
            self._invalid_values = deepcopy(values)
            self._items = []
        else:
            self._items = deepcopy(values or [])
        self.list.clear()
        for index in range(len(self._items)):
            self.list.addItem(self._label(index))
        self._loading = False
        self.list.setCurrentRow(0 if self._items else -1)
        self._select(self.list.currentRow())

    def _label(self, index):
        item = self._items[index]
        return f"{index + 1}. {item.get('name', 'Webhook')} · {'启用' if item.get('enabled') else '停用'}"

    def _select(self, row):
        if self._loading:
            return
        if self._invalid_values is not None:
            self.headers.set_values({})
            self.form_widget.setEnabled(False)
            self.remove_button.setEnabled(False)
            self.add_button.setEnabled(False)
            self.error.setText("Webhook 配置必须是对象列表，请在高级 YAML 中修正；原配置已保留。")
            return
        valid = 0 <= row < len(self._items)
        self.form_widget.setEnabled(valid)
        self.remove_button.setEnabled(valid)
        self.add_button.setEnabled(len(self._items) < 32)
        if not valid:
            self.headers.set_values({})
            self.error.clear()
            return
        self._loading = True
        item = {**asdict(WebhookSettings()), **self._items[row]}
        try:
            self.name.setText(str(item["name"]))
            self.url.setText(str(item["url"]))
            self.method.setCurrentIndex(self.method.findText(str(item["method"])))
            self.headers.set_values(item["headers"])
            self.enabled.setChecked(item["enabled"] is True)
            self.conversation_type.setCurrentIndex(self.conversation_type.findData(item["conversation_type"]))
            self.sender.setCurrentIndex(self.sender.findData(item["sender"]))
            self.include_ai.setChecked(item["include_ai_replies"] is True)
            timeout = item["timeout_seconds"]
            self.timeout.setValue(timeout if type(timeout) in (int, float) and math.isfinite(timeout) else 5)
            attempts = item["max_attempts"]
            self.attempts.setValue(max(1, min(10, attempts)) if type(attempts) is int else 3)
        finally:
            self._loading = False
        self._validate(item)

    def _edited(self):
        row = self.list.currentRow()
        if self._loading or row < 0:
            return
        self._items[row].update(
            name=self.name.text(), url=self.url.text().strip(), enabled=self.enabled.isChecked(),
            conversation_type=self.conversation_type.currentData(), sender=self.sender.currentData(),
            include_ai_replies=self.include_ai.isChecked(), timeout_seconds=self.timeout.value(),
            max_attempts=self.attempts.value(),
            method=self.method.currentText(), headers=self.headers.values(),
        )
        self.list.item(row).setText(self._label(row))
        self._validate(self._items[row])

    def _validate(self, item):
        try:
            WebhookSettings(**item)
        except (ValueError, TypeError) as error:
            self.error.setText(str(error))
        else:
            self.error.clear()

    def _add(self):
        if len(self._items) >= 32:
            return
        self._items.append(asdict(WebhookSettings(name=f"Webhook {len(self._items) + 1}")))
        self.list.addItem(self._label(len(self._items) - 1))
        self.list.setCurrentRow(len(self._items) - 1)

    def _remove(self):
        row = self.list.currentRow()
        if row < 0:
            return
        items = self.values()
        del items[row]
        self.set_values(items)
        self.list.setCurrentRow(min(row, len(items) - 1))
