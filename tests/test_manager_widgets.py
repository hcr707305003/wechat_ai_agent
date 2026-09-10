import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from agent_bridge.manager.widgets import SessionBindingsEditor, StringListEditor


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


def test_string_list_editor_round_trips_values(qt_app) -> None:
    editor = StringListEditor()
    editor.set_values(["one", "two"])

    assert editor.values() == ["one", "two"]


def test_session_bindings_editor_round_trips_rows(qt_app) -> None:
    editor = SessionBindingsEditor()
    values = [
        {
            "conversation_id": "friend",
            "conversation_type": "private",
            "provider": "codex",
            "session_id": "thread-1",
        }
    ]
    editor.set_values(values)

    assert editor.values() == values


def test_header_editor_plaintext_duplicates_and_delete(qt_app):
    from PySide6.QtWidgets import QLineEdit

    from agent_bridge.manager.header_editor import HeaderEditor
    from agent_bridge.webhooks import validated_headers

    editor = HeaderEditor()
    editor.set_values({"Authorization": "Bearer TEST_TOKEN"})
    assert editor.values() == {"Authorization": "Bearer TEST_TOKEN"}
    assert editor.table.cellWidget(0, 1).echoMode() == QLineEdit.EchoMode.Normal
    assert editor.table.cellWidget(0, 1).displayText() == "Bearer TEST_TOKEN"
    editor.add_button.click()
    editor.table.cellWidget(1, 0).setText("Authorization")
    editor.table.cellWidget(1, 1).setText("duplicate")
    assert editor.table.cellWidget(1, 1).displayText() == "duplicate"
    with pytest.raises(ValueError, match="重复"):
        validated_headers(editor.values())
    editor.table.cellWidget(0, 2).click()
    assert editor.values() == {"Authorization": "duplicate"}
    editor.set_values({"X-API-Key": "TEST_KEY"})
    assert editor.table.cellWidget(0, 1).echoMode() == QLineEdit.EchoMode.Normal
    assert editor.table.cellWidget(0, 1).displayText() == "TEST_KEY"
    editor.close()


@pytest.mark.parametrize("invalid", [None, "bad", {"X-Key": 123}, [{"invalid": "TEST_SECRET"}]])
def test_header_editor_preserves_invalid_yaml_for_correction(qt_app, invalid):
    from agent_bridge.manager.header_editor import HeaderEditor

    editor = HeaderEditor()
    editor.set_values(invalid)
    assert editor.values() == invalid
    assert not editor.add_button.isEnabled()
    editor.set_values({})
    assert editor.values() == {} and editor.add_button.isEnabled()
    editor.close()
