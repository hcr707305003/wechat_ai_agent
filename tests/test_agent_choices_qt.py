import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox

from agent_bridge.agents.availability import AgentAvailability
from agent_bridge.manager.agent_debug_panel import AgentDebugPanel
from agent_bridge.manager.widgets import SessionBindingsEditor, set_agent_choices


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def states():
    return {"codex": AgentAvailability(False, "未安装 Codex"),
            "claude": AgentAvailability(True, "就绪")}


def test_disabled_choice_rejects_keyboard_and_mouse(app):
    combo = QComboBox()
    combo.addItems(["codex", "claude"])
    set_agent_choices(combo, states(), select_available=True)
    combo.show()
    app.processEvents()
    try:
        assert combo.currentText() == "claude"
        assert combo.itemData(0, Qt.ItemDataRole.ForegroundRole).name() == "#94a3b8"
        QTest.keyClick(combo, Qt.Key.Key_Up)
        assert combo.currentText() == "claude"
        combo.showPopup()
        app.processEvents()
        index = combo.model().index(0, 0)
        QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton,
                         pos=combo.view().visualRect(index).center())
        assert combo.currentText() == "claude"
        assert "未安装" in combo.itemData(0, Qt.ItemDataRole.ToolTipRole)
    finally:
        combo.hidePopup()
        combo.close()


def test_new_binding_prefers_available_but_saved_binding_is_preserved(app):
    editor = SessionBindingsEditor()
    try:
        editor.set_agent_availability(states())
        editor.add_row()
        assert editor.table.cellWidget(0, 2).currentText() == "claude"
        editor.add_row({"conversation_id": "old", "session_id": "old-session", "provider": "codex"})
        assert editor.table.cellWidget(1, 2).currentText() == "codex"
        assert not editor.table.cellWidget(1, 2).model().item(0).isEnabled()
    finally:
        editor.close()


def test_debug_shortcut_cannot_bypass_disabled_provider(app):
    panel = AgentDebugPanel(lambda: pytest.fail("must not load config or call SDK"))
    try:
        panel.set_agent_availability(states())
        # Programmatic selection can bypass Qt flags; the send path still guards it.
        panel.provider.setCurrentIndex(0)
        assert not panel._send_prompt("test")
        assert "不可用" in panel.status.text()
    finally:
        panel.shutdown()
        panel.close()
