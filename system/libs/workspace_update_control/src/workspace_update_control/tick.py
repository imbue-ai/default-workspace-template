"""The dispatcher's minute as a program: what the workspace's cron runs, from the workspace root, with no venv.

``main`` is the minute itself. ``install_cron_line_main`` writes the cron line that runs it, which the workspace's
boot does so that the dispatcher runs in a workspace no client has asked anything of.
"""

import sys
import time

from workspace_update_control.arming import tick_self_arming
from workspace_update_control.channel import fetch_channel_file
from workspace_update_control.control import install_cron_entry
from workspace_update_control.dispatcher import (
    AGENT_PIDS_DIR,
    PROC_ROOT,
    AgentEnvironment,
    run_tick,
)
from workspace_update_control.protocol import LIVE_CRON_DIRECTORY


def main() -> None:
    now = time.time()
    agent_environment = AgentEnvironment()
    run_tick(
        now=now,
        proc_root=PROC_ROOT,
        agent_pids_dir=AGENT_PIDS_DIR,
        agent_environment=agent_environment,
    )
    # The dispatcher's minute may have taken minutes; what a client recorded meanwhile is not in the future.
    tick_self_arming(
        now=time.time(),
        live_cron_dir=LIVE_CRON_DIRECTORY,
        agent_environment=agent_environment,
        fetch_channel_file=fetch_channel_file,
    )


def install_cron_line(live_cron_dir: str) -> int:
    """Write the dispatcher's cron line for the workspace at the working directory, its durable copy and its copy
    in ``live_cron_dir``; the exit status says whether cron will run it (1 where the workspace has no cron)."""
    return 0 if install_cron_entry(live_cron_dir) else 1


def install_cron_line_main() -> None:
    """``<entry file> <the directory cron reads its drop-ins from>``."""
    sys.exit(install_cron_line(sys.argv[1]))
