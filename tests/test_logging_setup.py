from pathlib import Path

import pytest

from agent_bridge.logging_setup import configure_file_logging, open_redirected_log


def test_redirected_log_reports_open_failure(tmp_path):
    with pytest.raises(OSError):
        open_redirected_log(tmp_path / "missing-directory" / "workbench.log")


def test_redirected_log_preserves_existing_content_and_closes(tmp_path):
    path = tmp_path / "workbench.log"
    path.write_bytes(b"before\n")
    with open_redirected_log(path) as stream:
        stream.write(b"after\n")
    assert stream.closed
    assert path.read_bytes() == b"before\nafter\n"


def test_configure_file_logging_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "manager.log"

    first = configure_file_logging(path, logger_name="test.agent.manager")
    second = configure_file_logging(path, logger_name="test.agent.manager")
    first.info("hello")
    for handler in first.handlers:
        handler.flush()

    assert first is second
    assert len(first.handlers) == 1
    assert "hello" in path.read_text(encoding="utf-8")
