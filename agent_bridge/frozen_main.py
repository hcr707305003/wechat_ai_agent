from __future__ import annotations

import multiprocessing
import os
import sys


def main() -> int:
    multiprocessing.freeze_support()
    if len(sys.argv) == 1 or sys.argv[1] == "manager":
        if len(sys.argv) > 1:
            del sys.argv[1]
        from agent_bridge.manager_main import main as manager_main

        return manager_main()
    from agent_bridge.application_paths import ApplicationPaths
    from agent_bridge.logging_setup import configure_file_logging

    paths = ApplicationPaths.discover()
    paths.initialize_user_data()
    configure_file_logging(paths.workbench_log)
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    from agent_bridge.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
