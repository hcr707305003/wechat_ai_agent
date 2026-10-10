from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSpinBox,
    QWidget,
)

from agent_bridge.manager.header_editor import HeaderEditor
from agent_bridge.webhook_uploads import UploadSettings, parse_upload
from agent_bridge.webhooks import HTTP_METHODS


class UploadEditor(QWidget):
    """Independent nested config, retaining invalid YAML instead of silently repairing it."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        self._value = {}
        self._invalid = False
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.url = QLineEdit()
        self.protocol = QComboBox()
        self.protocol.addItem("通用 multipart", "multipart")
        self.protocol.addItem("Evolutionary 原始文件 / 分片", "evolutionary")
        self.chunk_url = QLineEdit()
        self.chunk_url.setPlaceholderText("http://your-server:8000/v1/uploads")
        self.url.setPlaceholderText("留空：发送 [图片] / [语音] / [视频]")
        self.method = QComboBox()
        self.method.addItems(HTTP_METHODS)
        self.headers = HeaderEditor()
        self.headers.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.file_field = QLineEdit()
        self.file_id_path = QLineEdit()
        self.file_id_path.setPlaceholderText("field / data->field / data[0]->field")
        self.threshold, self.chunk_size, self.max_file = (QSpinBox() for _ in range(3))
        self.timeout = QDoubleSpinBox()
        self.timeout.setDecimals(2)
        for widget, maximum in ((self.threshold, 100), (self.chunk_size, 32),
                                (self.max_file, 1024), (self.timeout, 120)):
            widget.setRange(1, maximum)
        for label, widget in (("上传协议", self.protocol), ("普通上传 URL", self.url),
                              ("分片初始化 URL", self.chunk_url), ("上传请求方式", self.method),
                              ("上传请求头（明文）", self.headers), ("文件表单字段", self.file_field),
                              ("返回文件 ID 路径", self.file_id_path), ("分片阈值（MiB）", self.threshold),
                              ("分片大小（MiB）", self.chunk_size), ("最大文件（MiB）", self.max_file),
                              ("上传超时（秒）", self.timeout)):
            widget.setAccessibleName(label)
            form.addRow(label, widget)
        note = QLabel("仅当前 Webhook 使用这些配置；请求头不继承推送接口。\n"
                      "通用协议仅支持小文件 multipart。Evolutionary 协议支持原始二进制、"
                      "分片与合并，文件表单字段不参与请求；同时选择 Webhook 的记忆服务正文格式。\n"
                      "上传失败不推送该媒体消息；不会重复上传以免创建重复文件。")
        note.setWordWrap(True)
        form.addRow(note)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #B91C1C;")
        form.addRow(self.error)
        for widget in (self.url, self.chunk_url, self.file_field, self.file_id_path):
            widget.textChanged.connect(self._edited)
        self.method.currentIndexChanged.connect(self._edited)
        self.protocol.currentIndexChanged.connect(self._protocol_changed)
        self.headers.changed.connect(self._edited)
        for widget in (self.threshold, self.chunk_size, self.max_file, self.timeout):
            widget.valueChanged.connect(self._edited)
        self.set_values({})

    def values(self):
        return deepcopy(self._value)

    def set_values(self, value):
        self._loading = True
        self._value = deepcopy(value)
        try:
            settings = parse_upload(value)
            self._invalid = False
            self.error.clear()
        except (ValueError, TypeError) as error:
            settings = UploadSettings()
            self._invalid = True
            self.error.setText(f"{error}；原值已保留，请在高级 YAML 中修正。")
        self.url.setText(settings.url)
        self.protocol.setCurrentIndex(self.protocol.findData(settings.protocol))
        self.chunk_url.setText(settings.chunk_url)
        self.method.setCurrentText(settings.method)
        self.headers.set_values(settings.headers)
        self.file_field.setText(settings.file_field)
        self.file_id_path.setText(settings.file_id_path)
        self.threshold.setValue(settings.chunk_threshold_mb)
        self.chunk_size.setValue(settings.chunk_size_mb)
        self.max_file.setValue(settings.max_file_mb)
        self.timeout.setValue(settings.timeout_seconds)
        for widget in (self.url, self.protocol, self.chunk_url, self.method, self.headers, self.file_field, self.file_id_path,
                       self.threshold, self.chunk_size, self.max_file, self.timeout):
            widget.setEnabled(not self._invalid)
        self._loading = False
        self._sync_controls()

    def _sync_controls(self):
        native = self.protocol.currentData() == "evolutionary"
        self.chunk_url.setEnabled(not self._invalid and native)
        self.file_field.setEnabled(not self._invalid and not native)
        self.method.setEnabled(not self._invalid and not native)

    def _protocol_changed(self):
        if self._loading or self._invalid:
            return
        if self.protocol.currentData() == "evolutionary":
            self.method.setCurrentText("POST")
            if self.file_id_path.text() == "data->file_id":
                self.file_id_path.setText("file_id")
        self._sync_controls()
        self._edited()

    def _edited(self):
        if self._loading or self._invalid:
            return
        self._value = {**asdict(UploadSettings()), **self._value,
                       "protocol": self.protocol.currentData(), "chunk_url": self.chunk_url.text().strip(),
                       "url": self.url.text().strip(), "method": self.method.currentText(),
                       "headers": self.headers.values(), "file_field": self.file_field.text(),
                       "file_id_path": self.file_id_path.text(), "chunk_threshold_mb": self.threshold.value(),
                       "chunk_size_mb": self.chunk_size.value(), "max_file_mb": self.max_file.value(),
                       "timeout_seconds": self.timeout.value()}
        try:
            parse_upload(self._value)
            self.error.clear()
        except (ValueError, TypeError) as error:
            self.error.setText(str(error))
        self.changed.emit()
