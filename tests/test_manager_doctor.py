from pathlib import Path

from agent_bridge.manager.doctor import (
    CheckTask,
    ManagerCheckSession,
    run_manager_checks,
)


def test_manager_checks_report_invalid_config(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("- not-a-mapping\n", encoding="utf-8")

    checks = run_manager_checks(config)

    assert len(checks) == 1
    assert checks[0].name == "配置文件"
    assert checks[0].ok is False


def test_manager_checks_accept_example_configuration() -> None:
    root = Path(__file__).resolve().parents[1]

    checks = run_manager_checks(root / "config.example.yaml")

    assert checks[0].ok is True
    assert any(check.name == "微信白名单" and check.ok for check in checks)


def test_checks_are_lazy_and_configuration_expands_remaining_tasks(monkeypatch) -> None:
    from agent_bridge.manager import doctor

    root = Path(__file__).resolve().parents[1]
    calls = []
    monkeypatch.setattr(
        doctor.importlib.util, "find_spec", lambda module: calls.append(module)
    )
    session = ManagerCheckSession(root / "config.example.yaml")
    assert [task.name for task in session.tasks] == ["配置文件"]
    assert session.tasks[0].run().ok
    assert len(session.tasks) > 1
    assert calls == []
    dependency = next(task for task in session.tasks if task.name == "微信组件")
    assert dependency.run().ok is False
    assert calls == ["wechatauto"]


def test_configuration_failure_does_not_expand_tasks(tmp_path) -> None:
    session = ManagerCheckSession(tmp_path / "missing.yaml")
    result = session.tasks[0].run()
    assert not result.ok
    assert "未执行" in result.suggestion
    assert len(session.tasks) == 1


def test_check_exception_is_a_failure_result() -> None:
    def broken():
        raise RuntimeError("dependency probe failed")

    result = CheckTask("组件", broken).run()
    assert not result.ok
    assert result.name == "组件"
    assert result.detail == "dependency probe failed"
