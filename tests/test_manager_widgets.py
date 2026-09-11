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


def test_upload_editor_independent_config_and_id_path(qt_app):
    from agent_bridge.manager.webhook_editor import WebhookEditor
    from agent_bridge.webhooks import parse_webhooks

    editor = WebhookEditor()
    editor.set_values([{"name": "one", "upload": {"file_id_path": "field", "timeout_seconds": 12.5}},
                       {"name": "two", "upload": {"file_id_path": "data[0]->field"}}])
    editor.upload.url.setText("https://one.example/upload")
    editor.upload.headers.set_values({"Authorization": "Bearer TEST_PLAIN"})
    editor.upload.file_field.setText("media")
    editor.list.setCurrentRow(1)
    assert editor.upload.file_id_path.text() == "data[0]->field"
    assert editor.upload.url.text() == ""
    editor.upload.url.setText("https://two.example/upload")
    editor.list.setCurrentRow(0)
    assert editor.upload.url.text() == "https://one.example/upload"
    assert editor.upload.timeout.value() == 12.5
    hooks = parse_webhooks(editor.values())
    assert hooks[0].upload.headers == {"Authorization": "Bearer TEST_PLAIN"}
    assert hooks[1].upload.headers == {}
    assert hooks[0].upload.file_field == "media"
    editor.close()


@pytest.mark.parametrize("invalid", [None, [], {"file_id_path": "data[-1]"}, {"unknown": 2}])
def test_upload_invalid_yaml_preserved_on_unrelated_edit(qt_app, invalid):
    from agent_bridge.manager.webhook_editor import WebhookEditor

    editor = WebhookEditor()
    editor.set_values([{"name": "one", "upload": invalid}])
    editor.name.setText("renamed")
    assert editor.values()[0]["upload"] == invalid
    assert editor.upload.error.text()
    assert not editor.upload.url.isEnabled()
    editor.close()


def test_upload_path_inline_validation(qt_app):
    from agent_bridge.manager.upload_editor import UploadEditor

    editor = UploadEditor()
    editor.file_id_path.setText("data->")
    assert "file_id_path" in editor.error.text()
    editor.file_id_path.setText("data[0]->field")
    assert editor.error.text() == ""
    editor.close()


def test_evolutionary_protocol_controls_and_roundtrip(qt_app):
    from agent_bridge.manager.webhook_editor import WebhookEditor
    from agent_bridge.webhooks import parse_webhooks

    editor = WebhookEditor()
    editor.set_values([{"name": "memory"}, {"name": "generic"}])
    editor.payload_format.setCurrentIndex(editor.payload_format.findData("memory"))
    upload = editor.upload
    upload.protocol.setCurrentIndex(upload.protocol.findData("evolutionary"))
    upload.url.setText("http://localhost:8000/v1/files")
    upload.chunk_url.setText("http://localhost:8000/v1/uploads")
    assert not upload.method.isEnabled()
    assert not upload.file_field.isEnabled()
    assert upload.chunk_url.isEnabled()
    assert upload.file_id_path.text() == "file_id"
    editor.list.setCurrentRow(1)
    assert upload.protocol.currentData() == "multipart"
    assert editor.payload_format.currentData() == "basic"
    editor.list.setCurrentRow(0)
    hooks = parse_webhooks(editor.values())
    assert hooks[0].payload_format == "memory"
    assert hooks[0].upload.protocol == "evolutionary"
    assert hooks[0].upload.chunk_url == "http://localhost:8000/v1/uploads"
    assert hooks[1].upload.protocol == "multipart"
    editor.close()


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
