from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from time import monotonic
from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from agent_bridge.agents.availability import AgentAvailability
from agent_bridge.application_paths import ApplicationPaths
from agent_bridge.config import AppConfig
from agent_bridge.manager.agent_debug_panel import AgentDebugPanel
from agent_bridge.manager.check_widgets import CheckRow
from agent_bridge.manager.config_document import ConfigDocument
from agent_bridge.manager.doctor import CheckResult, ManagerCheckSession
from agent_bridge.manager.process_controller import (
    WorkbenchProcessController,
    WorkbenchState,
    WorkbenchStatus,
)
from agent_bridge.manager.webhook_editor import WebhookEditor
from agent_bridge.manager.widgets import (
    SessionBindingsEditor,
    StringListEditor,
    set_agent_choices,
)


def decode_workbench_log(data: bytes) -> str:
    decoded_lines = []
    for line in data.splitlines(keepends=True):
        try:
            decoded_lines.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            decoded_lines.append(line.decode("gb18030", errors="replace"))
    return "".join(decoded_lines)


class AgentBridgeManagerWindow(QMainWindow):
    def __init__(
        self,
        paths: ApplicationPaths,
        document: ConfigDocument,
        controller: WorkbenchProcessController,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.paths = paths
        self.document = document
        self.controller = controller
        self._bindings: list[tuple[str, Callable[[], Any], Callable[[Any], None]]] = []
        self._syncing_tabs = False
        self._yaml_dirty = False
        self._allow_close = False
        self._previous_tab = 0
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="manager")
        self._operation: Future[WorkbenchStatus] | None = None
        self._status_future: Future[WorkbenchStatus] | None = None
        self._visibility_future: Future[bool] | None = None
        self._visibility_action = None
        self._check_session: ManagerCheckSession | None = None
        self._check_future: Future[CheckResult] | None = None
        self._check_rows: list[CheckRow] = []
        self._check_outcomes: list[CheckResult] = []
        self._checked_config: AppConfig | None = None
        self._saved_config: AppConfig | None = None
        self._agent_states: dict[str, AgentAvailability] = {}
        self._check_started = 0.0
        self._start_after_checks = False
        self._initial_checks_pending = True
        self._last_status = WorkbenchStatus(WorkbenchState.STOPPED)
        self._tray: QSystemTrayIcon | None = None

        self.setWindowTitle("Agent Bridge 管理面板")
        self.setMinimumSize(820, 620)
        self.resize(980, 720)
        self._build_ui()
        self._apply_style()
        self._populate_form()
        self.codex_enabled.toggled.connect(self._refresh_agent_choices)
        self.claude_enabled.toggled.connect(self._refresh_agent_choices)
        self._refresh_yaml()
        self._apply_status(WorkbenchStatus(WorkbenchState.STOPPED))

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(300)
        self._poll_timer.timeout.connect(self._poll_background_work)
        self._poll_timer.start()
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1200)
        self._status_timer.timeout.connect(self._schedule_status_check)
        self._status_timer.start()
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(1500)
        self._log_timer.timeout.connect(self._refresh_log)
        self._log_timer.start()
        self._schedule_status_check()
        self._refresh_log()

    def attach_tray(self, tray: QSystemTrayIcon) -> None:
        self._tray = tray

    def attach_visibility_action(self, action) -> None:
        self._visibility_action = action
        self._apply_status(self._last_status)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._initial_checks_pending:
            # Let the first frame appear before starting background diagnostics.
            QTimer.singleShot(0, self._run_initial_checks)

    def _run_initial_checks(self) -> None:
        if not self._initial_checks_pending or self._allow_close:
            return
        self._initial_checks_pending = False
        if self._check_session is None and not self._check_outcomes:
            self._run_checks(automatic=True)

    def request_exit(self) -> None:
        if self._allow_close:
            return
        self._allow_close = True
        self.agent_debug.shutdown()
        self._poll_timer.stop()
        self._status_timer.stop()
        self._log_timer.stop()
        if self._check_future is not None:
            self._check_future.cancel()
        self._check_session = None
        for row in self._check_rows:
            row.icon._timer.stop()
        if self._tray is not None:
            self._tray.hide()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        self.hide()
        if self._tray is not None and self._tray.isVisible():
            self._tray.showMessage(
                "Agent Bridge",
                "管理面板已隐藏，工作台将继续运行。",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )
        event.ignore()

    def show_manager(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Agent Bridge")
        title.setObjectName("managerTitle")
        subtitle = QLabel("配置与工作台控制中心")
        subtitle.setObjectName("mutedText")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        self.status_label = QLabel("已关闭")
        self.status_label.setObjectName("statusPill")
        header.addWidget(self.status_label)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("managerTabs")
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)
        self.tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self.tabs.setUsesScrollButtons(True)
        self.overview_tab = self._build_overview_tab()
        self.basic_tab = self._build_basic_tab()
        self.wechat_tab = self._build_wechat_tab()
        self.delivery_tab = self._build_delivery_tab()
        self.agents_tab = self._build_agents_tab()
        self.yaml_tab = self._build_yaml_tab()
        self.logs_tab = self._build_logs_tab()
        for label, page in (
            ("概览", self.overview_tab),
            ("基础运行", self.basic_tab),
            ("微信与会话", self.wechat_tab),
            ("发送与外观", self.delivery_tab),
            ("Agent", self.agents_tab),
            ("高级 YAML", self.yaml_tab),
            ("日志", self.logs_tab),
        ):
            self.tabs.addTab(page, label)
        self._advanced_index = self.tabs.indexOf(self.yaml_tab)
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs, 1)

        footer = QHBoxLayout()
        self.config_path_label = QLabel(str(self.paths.config_file))
        self.config_path_label.setObjectName("mutedText")
        self.config_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        footer.addWidget(self.config_path_label, 1)
        open_folder = QPushButton("打开数据目录")
        open_folder.clicked.connect(self._open_data_directory)
        self.save_button = QPushButton("保存配置")
        self.save_button.setObjectName("primaryButton")
        self.save_button.clicked.connect(self.save_config)
        footer.addWidget(open_folder)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)
        self.setCentralWidget(root)

    def _build_overview_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        heading = QLabel("微信 Agent 工作台")
        heading.setObjectName("sectionTitle")
        self.status_detail = QLabel("正在检查运行状态…")
        self.status_detail.setWordWrap(True)
        self.status_detail.setObjectName("mutedText")
        buttons = QHBoxLayout()
        self.start_button = QPushButton("启动工作台")
        self.start_button.setObjectName("primaryButton")
        self.show_button = QPushButton("打开工作台")
        self.stop_button = QPushButton("关闭工作台")
        self.start_button.clicked.connect(self._start_workbench)
        self.show_button.clicked.connect(self._show_workbench)
        self.stop_button.clicked.connect(self._stop_workbench)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.show_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        card_layout.addWidget(heading)
        card_layout.addWidget(self.status_detail)
        card_layout.addLayout(buttons)
        layout.addWidget(card)
        check_header = QHBoxLayout()
        check_title = QLabel("环境检查")
        check_title.setObjectName("sectionTitle")
        self.check_button = QPushButton("重新检查")
        self.check_button.clicked.connect(self._run_checks)
        check_header.addWidget(check_title)
        check_header.addStretch(1)
        check_header.addWidget(self.check_button)
        layout.addLayout(check_header)
        self.check_summary = QLabel("即将自动检查环境…")
        self.check_summary.setObjectName("mutedText")
        self.check_summary.setWordWrap(True)
        layout.addWidget(self.check_summary)
        self.check_progress = QProgressBar()
        self.check_progress.setObjectName("checkProgress")
        self.check_progress.setTextVisible(False)
        self.check_progress.setFixedHeight(6)
        self.check_progress.setRange(0, 1)
        self.check_progress.setValue(0)
        layout.addWidget(self.check_progress)
        self.check_results = QScrollArea()
        self.check_results.setWidgetResizable(True)
        self.check_results.setFrameShape(QFrame.Shape.NoFrame)
        self.check_results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._check_content = QWidget()
        self._check_layout = QVBoxLayout(self._check_content)
        self._check_layout.setContentsMargins(0, 0, 4, 0)
        self._check_layout.setSpacing(8)
        self._check_layout.addStretch(1)
        self.check_results.setWidget(self._check_content)
        layout.addWidget(self.check_results, 1)
        return page

    def _build_basic_tab(self) -> QWidget:
        page, content = self._scroll_page()
        runtime = self._form_group("运行设置")
        self.default_provider = self._combo(
            runtime, "默认 Agent", "runtime.default_provider", ["codex", "claude"]
        )
        self.database_path = self._line(runtime, "数据库", "runtime.database")
        self.working_directory = self._line(
            runtime, "默认工作目录", "runtime.default_working_directory"
        )
        self.allowed_roots = self._list(
            runtime, "允许的工作目录", "runtime.allowed_roots", "输入目录"
        )
        self.concurrency = self._spin(
            runtime, "并发会话数", "runtime.concurrency", 1, 32
        )
        self.timeout_seconds = self._double(
            runtime, "Agent 超时（秒）", "runtime.timeout_seconds", 1, 86400
        )
        self.recent_messages = self._spin(
            runtime, "上下文消息数", "runtime.recent_messages", 1, 1000
        )
        self.max_reply_chars = self._spin(
            runtime, "单次回复字数", "runtime.max_reply_chars", 100, 20000
        )
        self.acknowledgement = self._line(
            runtime, "处理中提示", "runtime.acknowledgement"
        )
        content.addWidget(runtime.parentWidget())
        content.addStretch(1)
        return page

    def _build_wechat_tab(self) -> QWidget:
        page, content = self._scroll_page()
        access = self._form_group("微信账号与白名单")
        self.wechat_enabled = self._check(
            access, "启用微信通道", "channels.wechat.enabled"
        )
        self.wechat_account = self._line(
            access, "微信账号（可留空）", "channels.wechat.account", optional=True
        )
        self.private_ids = self._list(
            access, "私聊白名单", "channels.wechat.allowed_private_ids", "输入昵称或 ID"
        )
        self.group_ids = self._list(
            access, "群聊白名单", "channels.wechat.allowed_group_ids", "输入群名或 ID"
        )
        self.group_controllers = self._list(
            access, "群控制人", "channels.wechat.group_controllers", "输入成员昵称或 ID"
        )
        content.addWidget(access.parentWidget())

        behavior = self._form_group("触发与回复")
        self.group_prefixes = self._list(
            behavior, "群触发词", "channels.wechat.group_prefixes", "例如 /ai"
        )
        self.prefix_rule = self._combo(
            behavior,
            "触发词匹配",
            "channels.wechat.group_prefixes_rule",
            ["prefix", "contains", "suffix"],
        )
        self.reply_prefix = self._line(
            behavior, "AI 回复前缀", "channels.wechat.reply_prefix"
        )
        self.quote_private = self._check(
            behavior, "私聊引用原消息", "channels.wechat.quote_private_replies"
        )
        self.quote_group = self._check(
            behavior, "群聊引用原消息", "channels.wechat.quote_group_replies"
        )
        self.batch_window = self._double(
            behavior,
            "图文合并窗口（秒）",
            "channels.wechat.message_batch_window_seconds",
            0,
            60,
        )
        self.listener_interval = self._double(
            behavior,
            "监听间隔（秒）",
            "channels.wechat.listener_interval",
            0.01,
            60,
            decimals=3,
        )
        content.addWidget(behavior.parentWidget())

        bindings_group = QGroupBox("会话 Session 绑定")
        bindings_layout = QVBoxLayout(bindings_group)
        self.session_bindings = SessionBindingsEditor()
        bindings_layout.addWidget(self.session_bindings)
        self._bindings.append(
            (
                "channels.wechat.session_bindings",
                self.session_bindings.values,
                self.session_bindings.set_values,
            )
        )
        content.addWidget(bindings_group)
        webhook_group = QGroupBox("消息 Webhook 推送")
        webhook_layout = QVBoxLayout(webhook_group)
        self.webhooks = WebhookEditor()
        webhook_layout.addWidget(self.webhooks)
        self._bindings.append(("channels.wechat.webhooks", self.webhooks.values, self.webhooks.set_values))
        content.addWidget(webhook_group)
        content.addStretch(1)
        return page

    def _build_delivery_tab(self) -> QWidget:
        page, content = self._scroll_page()
        sender = self._form_group("发送控制")
        self.sender_mode = self._combo(
            sender,
            "发送模式",
            "channels.wechat.sender.mode",
            ["idle_uia", "legacy_gui"],
        )
        self.idle_seconds = self._double(
            sender, "空闲等待（秒）", "channels.wechat.sender.idle_seconds", 0.1, 600
        )
        self.queue_age = self._double(
            sender,
            "队列最长等待（秒）",
            "channels.wechat.sender.max_queue_age_seconds",
            1,
            86400,
        )
        self.max_attempts = self._spin(
            sender, "最多发送次数", "channels.wechat.sender.max_attempts", 1, 20
        )
        self.verify_sends = self._check(
            sender, "验证发送结果", "channels.wechat.sender.verify_sends"
        )
        self.foreground_fallback = self._check(
            sender,
            "默认允许前台备用发送",
            "channels.wechat.sender.foreground_fallback_default",
        )
        self.foreground_driver = self._combo(
            sender,
            "前台驱动",
            "channels.wechat.sender.foreground_driver",
            ["wechat_mcp", "uia"],
        )
        self.operation_timeout = self._double(
            sender,
            "普通发送超时（秒）",
            "channels.wechat.sender.foreground_operation_timeout_seconds",
            1,
            600,
        )
        self.quote_timeout = self._double(
            sender,
            "引用发送超时（秒）",
            "channels.wechat.sender.foreground_quote_timeout_seconds",
            1,
            600,
        )
        self.cooldown = self._double(
            sender,
            "前台熔断（秒）",
            "channels.wechat.sender.foreground_cooldown_seconds",
            0,
            3600,
        )
        content.addWidget(sender.parentWidget())

        hook = self._form_group("实验性引用 Hook")
        self.hook_enabled = self._check(
            hook, "启用 Hook", "channels.wechat.hook_quote.enabled"
        )
        self.hook_endpoint = self._line(
            hook, "本地端点", "channels.wechat.hook_quote.endpoint"
        )
        self.hook_token_env = self._line(
            hook, "令牌环境变量", "channels.wechat.hook_quote.token_env"
        )
        self.hook_timeout = self._double(
            hook,
            "请求超时（秒）",
            "channels.wechat.hook_quote.timeout_seconds",
            0.1,
            60,
        )
        self.hook_version = self._line(
            hook, "微信版本", "channels.wechat.hook_quote.expected_version"
        )
        content.addWidget(hook.parentWidget())

        companion = self._form_group("工作台外观")
        self.companion_mode = self._combo(
            companion,
            "窗口模式",
            "channels.wechat.companion.mode",
            ["docked", "independent"],
        )
        self.companion_side = self._combo(
            companion,
            "停靠方向",
            "channels.wechat.companion.side",
            ["left", "right", "top", "bottom"],
        )
        self.companion_width = self._spin(
            companion, "宽度", "channels.wechat.companion.width", 320, 4000
        )
        self.companion_height = self._spin(
            companion, "高度", "channels.wechat.companion.height", 240, 4000
        )
        self.follow_interval = self._double(
            companion,
            "跟随间隔（秒）",
            "channels.wechat.companion.follow_interval",
            0.001,
            10,
            decimals=3,
        )
        self.companion_theme = self._combo(
            companion,
            "主题",
            "channels.wechat.companion.theme",
            ["system", "light", "dark"],
        )
        content.addWidget(companion.parentWidget())
        content.addStretch(1)
        return page

    def _build_agents_tab(self) -> QWidget:
        page, content = self._scroll_page()
        codex = self._form_group("Codex")
        self.codex_enabled = self._check(codex, "启用", "agents.codex.enabled")
        self.codex_model = self._line(
            codex, "模型（可留空）", "agents.codex.model", optional=True
        )
        self.codex_fallbacks = self._list(
            codex, "备用模型", "agents.codex.fallback_models", "输入模型名称"
        )
        self.codex_sandbox = self._combo(
            codex,
            "沙箱",
            "agents.codex.sandbox",
            ["read-only", "workspace-write", "full-access"],
        )
        self.codex_approval = self._combo(
            codex,
            "审批模式",
            "agents.codex.approval_mode",
            ["deny_all", "auto_review"],
        )
        self.codex_instructions = self._multiline(
            codex, "基础指令", "agents.codex.base_instructions", optional=True
        )
        content.addWidget(codex.parentWidget())

        claude = self._form_group("Claude")
        self.claude_enabled = self._check(claude, "启用", "agents.claude.enabled")
        self.claude_model = self._line(
            claude, "模型（可留空）", "agents.claude.model", optional=True
        )
        self.claude_permission = self._combo(
            claude,
            "权限模式",
            "agents.claude.permission_mode",
            ["default", "plan", "dontAsk"],
        )
        self.claude_prompt = self._multiline(
            claude, "系统提示", "agents.claude.system_prompt", optional=True
        )
        self.claude_bash = self._check(claude, "允许 Bash", "agents.claude.allow_bash")
        self.claude_network = self._check(
            claude, "允许网络", "agents.claude.allow_network"
        )
        content.addWidget(claude.parentWidget())
        content.addStretch(1)
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(12, 12, 12, 12)
        self.agent_pages = QTabWidget()
        self.agent_pages.setObjectName("agentTabs")
        self.agent_pages.tabBar().setDrawBase(False)
        self.agent_pages.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)
        self.agent_pages.addTab(page, "参数配置")
        self.agent_debug = AgentDebugPanel(self._load_debug_config)
        self.agent_pages.addTab(self.agent_debug, "聊天调试")
        layout.addWidget(self.agent_pages)
        return wrapper

    def _load_debug_config(self) -> AppConfig:
        self._sync_form_to_document()
        return self.document.validate()

    def _build_yaml_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        info = QLabel(
            "高级编辑会保留所有配置字段。离开此页或保存时会先解析并校验 YAML。"
        )
        info.setWordWrap(True)
        info.setObjectName("mutedText")
        self.yaml_editor = QPlainTextEdit()
        self.yaml_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.yaml_editor.setStyleSheet("font-family: Consolas, 'Microsoft YaHei UI';")
        self.yaml_editor.textChanged.connect(self._mark_yaml_dirty)
        layout.addWidget(info)
        layout.addWidget(self.yaml_editor, 1)
        return page

    def _build_logs_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QHBoxLayout()
        label = QLabel("工作台日志")
        label.setObjectName("sectionTitle")
        clear = QPushButton("清空日志")
        clear.setToolTip("清空当前工作台日志文件和显示，无法恢复；不影响聊天记录和其他日志。")
        open_dir = QPushButton("打开日志目录")
        clear.clicked.connect(self._clear_workbench_log)
        open_dir.clicked.connect(self._open_log_directory)
        header.addWidget(label)
        header.addStretch(1)
        header.addWidget(clear)
        header.addWidget(open_dir)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setStyleSheet("font-family: Consolas, 'Microsoft YaHei UI';")
        self._log_follow_tail = True
        self._log_updating = False
        self.log_view.verticalScrollBar().valueChanged.connect(self._update_log_follow_mode)
        self.log_view.verticalScrollBar().rangeChanged.connect(self._follow_log_range)
        layout.addLayout(header)
        layout.addWidget(self.log_view, 1)
        return page

    def _scroll_page(self) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content_widget = QWidget()
        content = QVBoxLayout(content_widget)
        content.setContentsMargins(12, 16, 12, 16)
        content.setSpacing(16)
        scroll.setWidget(content_widget)
        return scroll, content

    @staticmethod
    def _form_group(title: str) -> QFormLayout:
        group = QGroupBox(title)
        form = QFormLayout(group)
        # Keep the Python wrapper alive until the group is inserted into its page.
        form._owner = group
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setSpacing(10)
        return form

    def _line(
        self, form: QFormLayout, label: str, path: str, *, optional: bool = False
    ) -> QLineEdit:
        widget = QLineEdit()
        form.addRow(label, widget)
        self._bindings.append(
            (
                path,
                lambda current=widget: (
                    current.text().strip() or None if optional else current.text()
                ),
                lambda value, current=widget: current.setText(
                    "" if value is None else str(value)
                ),
            )
        )
        return widget

    def _multiline(
        self, form: QFormLayout, label: str, path: str, *, optional: bool = False
    ) -> QPlainTextEdit:
        widget = QPlainTextEdit()
        widget.setMaximumHeight(110)
        form.addRow(label, widget)
        self._bindings.append(
            (
                path,
                lambda current=widget: (
                    current.toPlainText().strip() or None
                    if optional
                    else current.toPlainText()
                ),
                lambda value, current=widget: current.setPlainText(
                    "" if value is None else str(value)
                ),
            )
        )
        return widget

    def _combo(
        self, form: QFormLayout, label: str, path: str, choices: list[str]
    ) -> QComboBox:
        widget = QComboBox()
        widget.addItems(choices)
        form.addRow(label, widget)
        self._bindings.append(
            (
                path,
                widget.currentText,
                lambda value, current=widget: current.setCurrentText(str(value)),
            )
        )
        return widget

    def _check(self, form: QFormLayout, label: str, path: str) -> QCheckBox:
        widget = QCheckBox(label)
        form.addRow("", widget)
        self._bindings.append((path, widget.isChecked, widget.setChecked))
        return widget

    def _spin(
        self,
        form: QFormLayout,
        label: str,
        path: str,
        minimum: int,
        maximum: int,
    ) -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        form.addRow(label, widget)
        self._bindings.append((path, widget.value, widget.setValue))
        return widget

    def _double(
        self,
        form: QFormLayout,
        label: str,
        path: str,
        minimum: float,
        maximum: float,
        *,
        decimals: int = 1,
    ) -> QDoubleSpinBox:
        widget = QDoubleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setDecimals(decimals)
        form.addRow(label, widget)
        self._bindings.append((path, widget.value, widget.setValue))
        return widget

    def _list(
        self,
        form: QFormLayout,
        label: str,
        path: str,
        placeholder: str,
    ) -> StringListEditor:
        widget = StringListEditor(placeholder)
        widget.setMinimumHeight(150)
        form.addRow(label, widget)
        self._bindings.append((path, widget.values, widget.set_values))
        return widget

    def _populate_form(self) -> None:
        for path, _reader, writer in self._bindings:
            writer(self.document.value(path))
        self._refresh_agent_choices()

    def _refresh_agent_choices(self) -> None:
        states = {
            provider: self._agent_states.get(provider, AgentAvailability(False, "等待环境检查"))
            if checkbox.isChecked() else AgentAvailability(False, "配置未启用")
            for provider, checkbox in (("codex", self.codex_enabled), ("claude", self.claude_enabled))
        }
        set_agent_choices(self.default_provider, states)
        self.session_bindings.set_agent_availability(states)
        self.agent_debug.set_agent_availability(states)

    def _sync_form_to_document(self) -> None:
        for path, reader, _writer in self._bindings:
            self.document.set_value(path, reader())

    def _refresh_yaml(self) -> None:
        blocker = QSignalBlocker(self.yaml_editor)
        self.yaml_editor.setPlainText(self.document.to_yaml())
        del blocker
        self._yaml_dirty = False

    def _mark_yaml_dirty(self) -> None:
        if not self._syncing_tabs:
            self._yaml_dirty = True

    def _tab_changed(self, index: int) -> None:
        if self._syncing_tabs:
            return
        if self._previous_tab == self._advanced_index and self._yaml_dirty:
            try:
                self.document.replace_from_yaml(self.yaml_editor.toPlainText())
                self._populate_form()
                self._yaml_dirty = False
            except (TypeError, ValueError) as error:
                self._syncing_tabs = True
                self.tabs.setCurrentIndex(self._advanced_index)
                self._syncing_tabs = False
                QMessageBox.warning(self, "YAML 无法解析", str(error))
                return
        if index == self._advanced_index:
            self._sync_form_to_document()
            self._refresh_yaml()
        self._previous_tab = index

    def save_config(self, *, show_success: bool = True) -> bool:
        try:
            if self.tabs.currentIndex() == self._advanced_index and self._yaml_dirty:
                self.document.replace_from_yaml(self.yaml_editor.toPlainText())
                self._populate_form()
            else:
                self._sync_form_to_document()
            self._saved_config = self.document.save()
            self._refresh_yaml()
        except (OSError, TypeError, ValueError) as error:
            QMessageBox.critical(self, "配置保存失败", str(error))
            return False
        if show_success:
            self.status_detail.setText("配置已保存。工作台下次启动时使用新配置。")
        return True

    def _run_checks(
        self, *, start_after: bool = False, automatic: bool = False,
        config_saved: bool = False,
    ) -> None:
        if self._check_session is not None or self._allow_close:
            return
        if not automatic and (self._operation is not None or self._last_status.state in {
            WorkbenchState.STARTING, WorkbenchState.STOPPING,
        }):
            return
        if not automatic and not config_saved and not self.save_config(show_success=False):
            return
        self._initial_checks_pending = False
        self._start_after_checks = start_after
        self._checked_config = None
        self._check_outcomes = []
        for row in self._check_rows:
            self._check_layout.removeWidget(row)
            row.deleteLater()
        self._check_rows = []
        self._check_session = ManagerCheckSession(self.paths.config_file)
        self.check_progress.setRange(0, 0)
        self.check_progress.setProperty("failed", False)
        self._repolish(self.check_progress)
        if not automatic:
            self.tabs.setCurrentWidget(self.overview_tab)
        self._set_config_editing_enabled(False)
        self._apply_status(self._last_status)
        self._submit_next_check()

    def _set_config_editing_enabled(self, enabled: bool) -> None:
        self.save_button.setEnabled(enabled)
        for page in (self.basic_tab, self.wechat_tab, self.delivery_tab,
                     self.agents_tab, self.yaml_tab):
            page.setEnabled(enabled)

    def _submit_next_check(self) -> None:
        session = self._check_session
        if session is None or self._allow_close:
            return
        for task in session.tasks[len(self._check_rows):]:
            row = CheckRow(task.name)
            self._check_rows.append(row)
            self._check_layout.insertWidget(self._check_layout.count() - 1, row)
        index = len(self._check_outcomes)
        if index >= len(session.tasks):
            self._finish_checks()
            return
        self._check_rows[index].set_state("running")
        total = str(len(session.tasks)) if index else "…"
        self.check_summary.setText(f"正在检测 {index + 1}/{total} · {session.tasks[index].name}")
        self._check_started = monotonic()
        self._check_future = self._executor.submit(session.tasks[index].run)
        QTimer.singleShot(0, self._reveal_check_result)

    def _reveal_check_result(self) -> None:
        if self._allow_close or not self._check_rows:
            return
        if self._check_session is not None:
            index = min(len(self._check_outcomes), len(self._check_rows) - 1)
        else:
            index = next((i for i, result in enumerate(self._check_outcomes)
                          if not result.ok), len(self._check_rows) - 1)
        self.check_results.ensureWidgetVisible(self._check_rows[index], 0, 16)

    def _poll_checks(self) -> None:
        future = self._check_future
        if (self._check_session is None or future is None or not future.done()
                or monotonic() - self._check_started < 0.15):
            return
        self._check_future = None
        index = len(self._check_outcomes)
        try:
            result = future.result()
        except Exception as error:  # noqa: BLE001 - UI worker boundary
            result = CheckResult(self._check_rows[index].name.text(), False, str(error))
        self._check_outcomes.append(result)
        self._check_rows[index].set_state(
            "success" if result.ok else "failed" if result.required else "unavailable", result)
        self.check_progress.setRange(0, len(self._check_session.tasks))
        self.check_progress.setValue(len(self._check_outcomes))
        self._submit_next_check()

    def _finish_checks(self) -> None:
        failed = sum(not result.ok and result.required for result in self._check_outcomes)
        optional = sum(not result.ok and not result.required for result in self._check_outcomes)
        passed = sum(result.ok for result in self._check_outcomes)
        self._agent_states = getattr(self._check_session, "agent_states", self._agent_states)
        self._refresh_agent_choices()
        if passed and not failed:
            self._checked_config = getattr(self._check_session, "config", None)
        self._check_session = None
        start_after = self._start_after_checks
        self._start_after_checks = False
        self.check_summary.setText(f"检测完成：{passed} 项通过，{failed} 项失败。"
                                   + (f"{optional} 个可选 Agent 不可用。" if optional else "")
                                   + ("请修复失败项后重新检查。" if failed else "环境已就绪。"))
        self.check_progress.setProperty("failed", bool(failed))
        self._repolish(self.check_progress)
        self._set_config_editing_enabled(True)
        self._apply_status(self._last_status)
        QTimer.singleShot(0, self._reveal_check_result)
        if start_after:
            if failed or not self._check_outcomes:
                self.status_detail.setText("环境检查未通过，工作台没有启动。")
            elif self._last_status.state not in {
                WorkbenchState.RUNNING, WorkbenchState.STARTING, WorkbenchState.STOPPING,
            }:
                self._submit_operation(self.controller.start, WorkbenchState.STARTING)

    def _start_workbench(self) -> None:
        if self._allow_close or self._operation is not None:
            return
        if self._last_status.state in {
            WorkbenchState.RUNNING, WorkbenchState.STARTING, WorkbenchState.STOPPING,
        }:
            return
        if self._check_session is not None:
            self._start_after_checks = True
            return
        if not self.save_config(show_success=False):
            return
        if self._checked_config is not None and self._saved_config == self._checked_config:
            self._submit_operation(self.controller.start, WorkbenchState.STARTING)
            return
        self._run_checks(start_after=True, config_saved=True)

    def _show_workbench(self) -> None:
        if (self._allow_close or self._visibility_future is not None
                or self._last_status.state != WorkbenchState.RUNNING):
            return
        operation = self.controller.hide if self._last_status.window_visible else self.controller.show
        if self._status_future is not None:
            self._status_future.cancel()
            self._status_future = None
        self._visibility_future = self._executor.submit(operation)
        self._apply_status(self._last_status)

    def _stop_workbench(self) -> None:
        self._submit_operation(self.controller.stop, WorkbenchState.STOPPING)

    def _submit_operation(
        self,
        operation: Callable[[], WorkbenchStatus],
        pending_state: WorkbenchState,
    ) -> None:
        if self._allow_close or self._check_session is not None or self._operation is not None:
            return
        # A query issued before this operation must not overwrite its new state.
        if self._status_future is not None:
            self._status_future.cancel()
            self._status_future = None
        self._apply_status(WorkbenchStatus(pending_state))
        self._operation = self._executor.submit(operation)

    def _schedule_status_check(self) -> None:
        if self._allow_close or self._operation is not None or self._visibility_future is not None:
            return
        if self._status_future is None:
            self._status_future = self._executor.submit(self.controller.status)

    def _poll_background_work(self) -> None:
        if self._allow_close:
            return
        self._poll_checks()
        if self._visibility_future is not None and self._visibility_future.done():
            future = self._visibility_future
            self._visibility_future = None
            try:
                changed = future.result()
            except Exception:  # noqa: BLE001 - UI worker boundary
                changed = False
            self._apply_status(self._last_status)
            if not changed:
                self.status_detail.setText("工作台显示状态切换失败，请稍后重试。")
            self._schedule_status_check()
        if self._operation is not None and self._operation.done():
            future = self._operation
            self._operation = None
            try:
                self._apply_status(future.result())
            except Exception as error:  # noqa: BLE001 - UI worker boundary
                self._apply_status(
                    WorkbenchStatus(WorkbenchState.UNKNOWN, detail=str(error))
                )
        if self._status_future is not None and self._status_future.done():
            future = self._status_future
            self._status_future = None
            try:
                self._apply_status(future.result())
            except Exception as error:  # noqa: BLE001 - UI worker boundary
                self._apply_status(
                    WorkbenchStatus(WorkbenchState.UNKNOWN, detail=str(error))
                )

    def _apply_status(self, status: WorkbenchStatus) -> None:
        self._last_status = status
        labels = {
            WorkbenchState.STOPPED: "已关闭",
            WorkbenchState.STARTING: "开启中",
            WorkbenchState.RUNNING: "已开启",
            WorkbenchState.STOPPING: "关闭中",
            WorkbenchState.EXITED: "已关闭",
            WorkbenchState.UNKNOWN: "已关闭",
        }
        self.status_label.setText("●  " + labels[status.state])
        self.status_label.setAccessibleName(labels[status.state])
        self.status_label.setProperty("closed", status.state in {
            WorkbenchState.STOPPED, WorkbenchState.EXITED, WorkbenchState.UNKNOWN,
        })
        self._repolish(self.status_label)
        detail = status.detail
        if status.state == WorkbenchState.RUNNING:
            detail = "工作台正在监听消息。关闭管理面板不会中断工作台。"
        elif status.state == WorkbenchState.STOPPED:
            detail = "工作台当前未运行。"
        elif status.state == WorkbenchState.EXITED:
            detail = ("工作台已正常关闭。" if status.exit_code == 0
                      else f"工作台已退出，退出码：{status.exit_code}。请查看日志。")
        self.status_detail.setText(detail or labels[status.state])
        running = status.state == WorkbenchState.RUNNING
        busy = (status.state in {WorkbenchState.STARTING, WorkbenchState.STOPPING}
                or self._check_session is not None)
        self.start_button.setEnabled(not running and not busy)
        visibility_text = "收起工作台" if running and status.window_visible else "打开工作台"
        self.show_button.setText(visibility_text)
        self.show_button.setToolTip("仅收起或显示窗口，工作台继续监听和回复消息。")
        self.show_button.setEnabled(running and self._visibility_future is None)
        if self._visibility_action is not None:
            self._visibility_action.setText(visibility_text)
            self._visibility_action.setEnabled(self.show_button.isEnabled())
        self.stop_button.setEnabled(running and not busy)
        self.check_button.setEnabled(not busy)

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _update_log_follow_mode(self, value: int) -> None:
        if not self._log_updating:
            self._log_follow_tail = value == self.log_view.verticalScrollBar().maximum()

    def _follow_log_range(self, _minimum: int, maximum: int) -> None:
        scrollbar = self.log_view.verticalScrollBar()
        # Qt may update the vertical range again after showing a horizontal
        # scrollbar. Retain tail-following across that deferred layout pass.
        if self._log_follow_tail and not scrollbar.isSliderDown():
            scrollbar.setValue(maximum)

    def _refresh_log(self) -> None:
        scrollbar = self.log_view.verticalScrollBar()
        # Keep the displayed snapshot intact while reading older lines, even
        # when the on-disk 128 KiB tail rolls forward. Resume at the next tick
        # only after the user returns to the bottom and releases the scrollbar.
        if scrollbar.isSliderDown() or not self._log_follow_tail:
            return
        if not self.paths.workbench_log.exists():
            return
        try:
            with self.paths.workbench_log.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                offset = max(0, size - 128 * 1024)
                handle.seek(offset)
                if offset:
                    handle.readline()
                text = decode_workbench_log(handle.read())
        except OSError:
            return
        if text != self.log_view.toPlainText():
            self._log_updating = True
            try:
                self.log_view.setPlainText(text)
                scrollbar.setValue(scrollbar.maximum())
            finally:
                self._log_updating = False

    def _clear_workbench_log(self) -> None:
        try:
            # Truncate in place; do not unlink/replace the file held by writers.
            with self.paths.workbench_log.open("r+b") as handle:
                handle.truncate(0)
        except FileNotFoundError:
            pass
        except OSError as error:
            QMessageBox.critical(self, "日志清空失败", str(error))
            return
        self._log_follow_tail = True
        self.log_view.clear()

    def _open_data_directory(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.user_root)))

    def _open_log_directory(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.logs_dir)))

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #F8FAFC; color: #0F172A; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
            QLabel { background: transparent; }
            QTabWidget#managerTabs::pane { border: 1px solid #E2E8F0; border-radius: 12px; background: #F8FAFC; margin-top: 12px; }
            QTabWidget#managerTabs::tab-bar { left: 0; }
            QTabWidget#agentTabs::pane { border: none; margin-top: 8px; }
            QTabBar { background: #E9EEF4; border: none; border-radius: 10px; }
            QTabBar::tab { background: transparent; border: 1px solid transparent; border-radius: 7px; padding: 10px 16px; margin: 4px 3px; color: #475569; font-weight: 500; }
            QTabBar::tab:selected { background: #FFFFFF; border-color: #C8DDD8; color: #0F766E; font-weight: 600; }
            QTabBar::tab:hover:!selected { background: #DCE6ED; color: #0F172A; }
            QTabBar::tab:selected:focus { border-color: #0F766E; }
            QTabBar QToolButton { background: #E9EEF4; border: none; border-radius: 6px; color: #475569; }
            QGroupBox { background: #FFFFFF; border: 1px solid #D7E0E8; border-radius: 8px; margin-top: 12px; padding: 14px 12px 12px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
            QLineEdit, QPlainTextEdit, QTextBrowser, QListWidget, QTableWidget, QComboBox, QSpinBox, QDoubleSpinBox {
                background: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 5px; padding: 6px; selection-background-color: #99F6E4;
            }
            QLineEdit:focus, QPlainTextEdit:focus, QListWidget:focus, QTableWidget:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border: 2px solid #0D9488; }
            QPushButton { min-height: 32px; padding: 0 14px; border: 1px solid #CBD5E1; border-radius: 6px; background: #FFFFFF; }
            QPushButton:hover { background: #F1F5F9; border-color: #94A3B8; }
            QPushButton:disabled { color: #94A3B8; background: #F1F5F9; }
            QPushButton#primaryButton { color: #FFFFFF; background: #0F766E; border-color: #0F766E; font-weight: 600; }
            QPushButton#primaryButton:hover { background: #115E59; }
            QPushButton#primaryButton:disabled { background: #F1F5F9; color: #94A3B8; border-color: #CBD5E1; }
            QLabel#managerTitle { font-size: 24px; font-weight: 700; }
            QLabel#sectionTitle { font-size: 16px; font-weight: 600; }
            QLabel#mutedText { color: #64748B; }
            QLabel#statusPill { background: #DCFCE7; color: #166534; border-radius: 12px; padding: 8px 14px; font-weight: 600; }
            QLabel#statusPill[closed="true"] { background: #FEE2E2; color: #B91C1C; }
            QFrame#checkRow { background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; }
            QFrame#checkRow QLabel, QFrame#checkRow QWidget { background: transparent; }
            QLabel#checkDetail { color: #64748B; font-size: 12px; }
            QProgressBar#checkProgress { background: #E2E8F0; border: none; border-radius: 3px; }
            QProgressBar#checkProgress::chunk { background: #16A34A; border-radius: 3px; }
            QProgressBar#checkProgress[failed="true"]::chunk { background: #DC2626; }
            QFrame#card { background: #FFFFFF; border: 1px solid #D7E0E8; border-radius: 8px; padding: 12px; }
            """
        )
