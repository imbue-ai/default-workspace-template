"""The dispatcher's minute as a program: what the workspace's cron runs, from the workspace root, with no venv."""

import time

from workspace_update_control.dispatcher import (
    AGENT_PIDS_DIR,
    PROC_ROOT,
    AgentEnvironment,
    run_tick,
)


def main() -> None:
    run_tick(
        now=time.time(),
        proc_root=PROC_ROOT,
        agent_pids_dir=AGENT_PIDS_DIR,
        agent_environment=AgentEnvironment(),
    )
