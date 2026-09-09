from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
import importlib.util
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_bridge.agents.claude import ClaudeAdapter
from agent_bridge.agents.codex import CodexAdapter
from agent_bridge.agents.factory import AgentFactory
from agent_bridge.channels.wechat import WeChatChannelAdapter
from agent_bridge.companion.controller import CompanionController
from agent_bridge.config import AppConfig, load_config
from agent_bridge.parsers.claude import ClaudeParser
from agent_bridge.parsers.codex import CodexParser
from agent_bridge.runtime.dispatcher import Dispatcher, DispatcherSettings
from agent_bridge.runtime.queue import SessionTaskQueue
from agent_bridge.sessions.context import ContextBuilder
from agent_bridge.sessions.repository import SQLiteRepository
from agent_bridge.sessions.resolver import SessionResolver

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent_bridge.lifecycle import WorkbenchLifecycleServer


class _BridgeInstanceLock:
    """Keep one bridge listener and reclaim a stale/older bridge on startup."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        if self._try_acquire(self._handle):
            self._write_owner()
            return self
        self._handle.close()
        self._handle = None

        owner_pid, owner_executable = self._read_owner()
        if not self._is_reclaimable_owner(owner_pid, owner_executable):
            raise RuntimeError(
                f"Agent bridge 已在运行（锁文件：{self.path}），且无法安全确认旧进程"
            )
        self._terminate_owner(owner_pid)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            self._handle = self.path.open("a+", encoding="utf-8")
            if self._try_acquire(self._handle):
                self._write_owner()
                return self
            self._handle.close()
            self._handle = None
            time.sleep(0.05)
        raise RuntimeError(f"无法结束旧 Agent bridge 进程（PID {owner_pid}）")

    @staticmethod
    def _try_acquire(handle) -> bool:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write("0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, PermissionError):
            return False
        return True

    def _write_owner(self) -> None:
        assert self._handle is not None
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"{os.getpid()}\t{Path(sys.executable).resolve()}\n")
        self._handle.flush()

    def _read_owner(self) -> tuple[int, str | None]:
        try:
            value = self.path.read_text(encoding="utf-8").strip()
            pid_text, _, executable = value.partition("\t")
            return int(pid_text), executable or None
        except (OSError, TypeError, ValueError):
            return 0, None

    @staticmethod
    def _is_reclaimable_owner(pid: int, executable: str | None) -> bool:
        if pid <= 0 or pid == os.getpid():
            return False
        expected = Path(sys.executable).resolve()
        actual = _process_executable(pid)
        if actual is None:
            return False
        if executable:
            try:
                return Path(executable).resolve() == actual == expected
            except (OSError, RuntimeError, ValueError):
                return False
        return actual == expected

    @staticmethod
    def _terminate_owner(pid: int) -> None:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0001 | 0x1000, False, pid)
            if not handle:
                return
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _process_executable(pid: int) -> Path | None:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint32(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return None
            return Path(buffer.value).resolve()
        finally:
            kernel32.CloseHandle(handle)
    try:
        return Path(os.readlink(f"/proc/{pid}/exe")).resolve()
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return None


def _reclaim_legacy_bridge_processes(config_path: str | Path | None = None) -> None:
    """Stop pre-lock bridge processes from this checkout.

    Older releases did not write a lock file, so they cannot be reclaimed via
    the owner PID.  Restrict the scan to the current working directory and the
    same config basename before terminating anything.
    """
    try:
        import psutil
    except ImportError:
        return
    expected_cwd = Path.cwd().resolve()
    expected_config = Path(config_path).resolve() if config_path else None
    protected = _current_process_tree_ids(psutil)
    matches = []
    for process in psutil.process_iter(["pid", "cmdline", "cwd"]):
        try:
            if process.pid in protected:
                continue
            command = process.info.get("cmdline") or []
            if "agent_bridge" not in command or "run" not in command:
                continue
            cwd = process.info.get("cwd")
            if not cwd or Path(cwd).resolve() != expected_cwd:
                continue
            if expected_config is not None and "--config" in command:
                index = command.index("--config")
                if index + 1 < len(command):
                    supplied = Path(command[index + 1])
                    if not supplied.is_absolute():
                        supplied = expected_cwd / supplied
                    if supplied.resolve() != expected_config:
                        continue
            matches.append(process)
        except (OSError, psutil.Error, TypeError, ValueError):
            continue
    for process in matches:
        try:
            process.terminate()
        except (OSError, psutil.Error):
            continue
    if matches:
        gone, alive = psutil.wait_procs(matches, timeout=3)
        for process in alive:
            try:
                process.kill()
            except (OSError, psutil.Error):
                pass


def _current_process_tree_ids(psutil_module) -> set[int]:
    """Return this interpreter's process tree so legacy cleanup cannot self-kill."""
    protected = {os.getpid()}
    try:
        current = psutil_module.Process(os.getpid())
        protected.update(child.pid for child in current.children(recursive=True))
        parent = current.parent()
        while parent is not None:
            command = parent.cmdline()
            if "agent_bridge" not in command:
                break
            protected.add(parent.pid)
            parent = parent.parent()
    except (OSError, psutil_module.Error, TypeError, ValueError):
        pass
    return protected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-bridge")
    parser.add_argument("--config", default="config.yaml", help="YAML configuration file")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor", help="Run non-invasive configuration checks")
    subcommands.add_parser("run", help="Run the configured channel bridge")
    sessions = subcommands.add_parser("sessions", help="Inspect logical sessions")
    sessions.add_subparsers(dest="sessions_command", required=True).add_parser("list")
    return parser


def build_agent_factory(config: AppConfig) -> AgentFactory:
    from agent_bridge.agents.availability import (
        configured_availability,
        effective_default,
    )

    states = configured_availability(config)
    effective_default(config, states)  # Fail before opening databases or touching WeChat.
    factory = AgentFactory()
    codex_config = config.agents.get("codex")
    if codex_config and states["codex"].available:
        factory.register(
            "codex",
            lambda: CodexAdapter(codex_config.codex),
            CodexParser,
        )
    claude_config = config.agents.get("claude")
    if claude_config and states["claude"].available:
        factory.register(
            "claude",
            lambda: ClaudeAdapter(claude_config.claude),
            ClaudeParser,
        )
    return factory


async def run_bridge(
    config: AppConfig,
    lifecycle: "WorkbenchLifecycleServer | None" = None,
) -> None:
    if not config.wechat_enabled:
        raise RuntimeError("No enabled channel is configured")
    factory = build_agent_factory(config)
    default_provider = config.runtime.default_provider
    if default_provider not in factory.providers():
        default_provider = factory.providers()[0]
        logger.warning("默认 Agent 不可用，新会话使用 %s；已有 session 不迁移。", default_provider)
    repository = SQLiteRepository(config.runtime.database)
    repository.interrupt_running_jobs()
    # Reply queues are process-local.  Do not let persistent outbound rows
    # resurrect stale replies after a service restart; conversation history
    # remains stored in the messages table.
    repository.discard_pending_outbound_deliveries()
    channel = WeChatChannelAdapter(config.wechat, repository)
    resolver = SessionResolver(
        repository,
        default_provider,
        config.runtime.default_working_directory,
        config.runtime.allowed_roots,
    )
    dispatcher = Dispatcher(
        channel,
        repository,
        resolver,
        factory,
        ContextBuilder(repository, config.runtime.recent_messages),
        SessionTaskQueue(config.runtime.concurrency),
        DispatcherSettings(
            timeout_seconds=config.runtime.timeout_seconds,
            acknowledgement=config.runtime.acknowledgement,
            max_reply_chars=config.runtime.max_reply_chars,
            group_controllers=config.wechat.group_controllers,
            message_batch_window_seconds=config.wechat.message_batch_window_seconds,
            reply_prefix=config.wechat.reply_prefix,
        ),
    )
    channel.set_desktop_window_selector(dispatcher.select_desktop_window)
    controller = CompanionController(
        repository,
        dispatcher.handle,
        channel.load_history,
        default_provider,
        channel.retry_delivery,
        channel.cancel_delivery,
        channel.resend_delivery,
        channel.foreground_fallback_enabled,
        channel.set_foreground_fallback_enabled,
        dispatcher.queued_jobs,
        dispatcher.remove_queued_job,
        dispatcher.clear_queued_jobs,
        dispatcher.observe,
        available_providers=factory.providers(),
    )
    dispatcher.subscribe_progress(controller.handle_agent_update)
    channel.subscribe_sender(controller.handle_sender_update)
    companion = None
    startup_started = time.perf_counter()
    try:
        stage_started = time.perf_counter()
        await channel.start(controller.handle)
        logger.info(
            "工作台启动计时: 微信通道 ready elapsed=%.3fs",
            time.perf_counter() - stage_started,
        )
        resolver.configure_session_bindings(channel.resolved_session_bindings)
        stage_started = time.perf_counter()
        conversations = await channel.list_allowlisted_conversations_initial()
        logger.info(
            "工作台启动计时: 会话列表 loaded count=%d elapsed=%.3fs",
            len(conversations),
            time.perf_counter() - stage_started,
        )
        from agent_bridge.companion.avatars import AvatarCache

        avatar_cache = AvatarCache(Path(config.runtime.database).parent / "avatars")

        def load_geometry() -> str | None:
            value = repository.get_channel_state(
                channel.name, channel.account_id, "companion_geometry"
            )
            return value if isinstance(value, str) else None

        def save_geometry(value: str) -> None:
            repository.set_channel_state(
                channel.name, channel.account_id, "companion_geometry", value
            )

        try:
            from agent_bridge.companion.qt_window import WeChatCompanionWindow

            stage_started = time.perf_counter()
            companion = WeChatCompanionWindow(
                config.wechat.companion,
                controller,
                conversations,
                lambda: channel.main_window_handle,
                load_geometry,
                save_geometry,
                avatar_cache=avatar_cache,
                conversation_loader=channel.list_allowlisted_conversations,
            )
            if lifecycle is not None:
                lifecycle.show_requested.connect(companion._expand_from_launcher)
                lifecycle.hide_requested.connect(companion._collapse_to_launcher)
                companion.visibility_changed.connect(lifecycle.set_window_visible)
                lifecycle.set_window_visible(companion.isVisible() and not companion.isMinimized())
            logger.info(
                "工作台启动计时: UI constructed elapsed=%.3fs total=%.3fs",
                time.perf_counter() - stage_started,
                time.perf_counter() - startup_started,
            )
        except (ImportError, RuntimeError) as error:
            raise RuntimeError(
                f"Unable to create WeChat companion window: {error}"
            ) from error
        print("Agent bridge is running. Close the companion window or press Ctrl+C to stop.")
        await companion.run()
    finally:
        if lifecycle is not None:
            lifecycle.begin_shutdown()
        if companion is not None:
            companion.close()
        await channel.stop()
        await dispatcher.close()
        repository.close()


def doctor(config: AppConfig) -> int:
    checks: list[tuple[str, bool, str]] = []
    runtime = config.runtime
    checks.append(("python", sys.version_info >= (3, 10), sys.version.split()[0]))
    working_directory = Path(runtime.default_working_directory)
    checks.append(("working_directory", working_directory.is_dir(), str(working_directory)))
    for root in runtime.allowed_roots:
        checks.append(("allowed_root", Path(root).is_dir(), root))
    package_map = {
        "wechat": "wechatauto",
        "codex": "openai_codex",
        "claude": "claude_agent_sdk",
    }
    if config.wechat_enabled:
        checks.append(
            ("wechat_allowlist", bool(config.wechat.allowed_private_ids or config.wechat.allowed_group_ids), "configured")
        )
        checks.append(("dependency_wechat", _module_exists(package_map["wechat"]), package_map["wechat"]))
        checks.append(("dependency_pyside6", _module_exists("PySide6"), "PySide6"))
        checks.append(("dependency_qasync", _module_exists("qasync"), "qasync"))
        checks.append(
            (
                "wechat_companion",
                True,
                f"{config.wechat.companion.mode}:{config.wechat.companion.side}",
            )
        )
        if config.wechat.sender.mode == "idle_uia" and _module_exists("wechatauto"):
            from agent_bridge.senders.uia_driver import SilentWeChatUiaDriver

            sender_ok, sender_detail = SilentWeChatUiaDriver().probe_capabilities()
            checks.append(("wechat_idle_uia", sender_ok, sender_detail))
            checks.append(
                (
                    "wechat_foreground_fallback",
                    True,
                    config.wechat.sender.foreground_driver
                    + "; available; default="
                    + (
                        "on"
                        if config.wechat.sender.foreground_fallback_default
                        else "off"
                    ),
                )
            )
        else:
            checks.append(("wechat_sender", True, config.wechat.sender.mode))
        hook_quote = config.wechat.hook_quote
        if not hook_quote.enabled:
            checks.append(("wechat_hook_quote", True, "disabled"))
        else:
            token = os.environ.get(hook_quote.token_env, "").strip()
            if not token:
                checks.append(
                    (
                        "wechat_hook_quote",
                        False,
                        f"missing token environment variable: {hook_quote.token_env}",
                    )
                )
            else:
                from agent_bridge.senders.wechat_hook_driver import (
                    WeChatHookError,
                    WeChatHookQuoteDriver,
                )

                try:
                    status = WeChatHookQuoteDriver(
                        hook_quote.endpoint,
                        token,
                        timeout_seconds=hook_quote.timeout_seconds,
                    ).probe_status()
                    capabilities = status.get("capabilities") or {}
                    version_ok = (
                        str(status.get("client_version") or "")
                        == hook_quote.expected_version
                    )
                    fingerprint_ok = status.get("fingerprint_supported") is True
                    quote_ok = (
                        isinstance(capabilities, dict)
                        and capabilities.get("quote") is True
                    )
                    hook_ok = version_ok and fingerprint_ok and quote_ok
                    detail = (
                        f"version={status.get('client_version') or 'unknown'}; "
                        f"fingerprint={'ok' if fingerprint_ok else 'unsupported'}; "
                        f"quote={'available' if quote_ok else 'unavailable'}"
                    )
                    checks.append(("wechat_hook_quote", hook_ok, detail))
                except WeChatHookError as error:
                    checks.append(("wechat_hook_quote", False, str(error)))
    from agent_bridge.agents.availability import configured_availability

    states = configured_availability(config)
    for provider, state in states.items():
        print(f"[{'OK' if state.available else 'WARN'}] {provider}: {state.reason}")
    checks.append(("agent_available", any(s.available for s in states.values()),
                   "Codex 或 Claude 至少一个可用"))
    for name, ok, detail in checks:
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def list_sessions(config: AppConfig) -> None:
    repository = SQLiteRepository(config.runtime.database)
    try:
        for session in repository.list_sessions():
            print(
                json.dumps(
                    {
                        "id": session.id,
                        "provider": session.current_provider,
                        "working_directory": session.working_directory,
                        "status": session.status,
                        "updated_at": session.updated_at.isoformat(),
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        repository.close()


def _module_exists(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def run_bridge_with_qt(
    config: AppConfig, config_path: str | Path | None = None
) -> None:
    try:
        from PySide6.QtWidgets import QApplication
        from qasync import QEventLoop
    except ImportError as error:
        raise RuntimeError(
            "WeChat companion requires: pip install -e .[wechat]"
        ) from error

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    event_loop = QEventLoop(app)
    asyncio.set_event_loop(event_loop)
    bridge_task = None
    lifecycle = None
    shutdown_requested = False

    def request_shutdown(_signum, _frame) -> None:
        nonlocal shutdown_requested
        if shutdown_requested:
            return
        shutdown_requested = True
        if lifecycle is not None:
            lifecycle.begin_shutdown()
        if bridge_task is not None:
            bridge_task.cancel()

    previous_sigint = signal.signal(signal.SIGINT, request_shutdown)
    runtime = getattr(config, "runtime", None)
    lock_context = (
        _BridgeInstanceLock(Path(runtime.database).with_suffix(".lock"))
        if runtime is not None
        else nullcontext()
    )
    try:
        with lock_context:
            _reclaim_legacy_bridge_processes(config_path)
            if config_path is not None:
                from agent_bridge.lifecycle import WorkbenchLifecycleServer
                from PySide6.QtCore import Qt

                lifecycle = WorkbenchLifecycleServer(config_path)
                lifecycle.shutdown_requested.connect(
                    lambda: request_shutdown(signal.SIGINT, None),
                    Qt.ConnectionType.QueuedConnection,
                )
                lifecycle.start()
            bridge_task = event_loop.create_task(run_bridge(config, lifecycle))
            with event_loop:
                try:
                    event_loop.run_until_complete(bridge_task)
                except asyncio.CancelledError:
                    if shutdown_requested:
                        raise KeyboardInterrupt from None
                    raise
    finally:
        if lifecycle is not None:
            lifecycle.close()
        signal.signal(signal.SIGINT, previous_sigint)
        app.quit()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [agent-bridge] [%(levelname)s] %(message)s",
    )
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            return doctor(config)
        if args.command == "sessions":
            list_sessions(config)
            return 0
        if args.command == "run":
            try:
                run_bridge_with_qt(config, args.config)
            except KeyboardInterrupt:
                print("Agent bridge stopped.")
            return 0
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0
