from pathlib import Path

from agent_bridge.logging_setup import configure_file_logging


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
