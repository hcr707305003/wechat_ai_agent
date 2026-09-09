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
