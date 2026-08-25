"""The one way the system interface shells out: detached from the workspace's terminal.

Every subprocess the system interface runs goes through :func:`run_detached_command`, and a
ratchet (``test_subprocess_ratchets.py``) keeps it that way.

The system interface is started by supervisord, which puts each service it launches into its
own process group with ``setpgrp`` -- a new *group*, but the same session, so the service and
everything it spawns keep the session's controlling terminal. In a workspace that terminal is
the tmux pane running ``uv run bootstrap``, whose foreground process group is bootstrap's, not
ours. So every subprocess started here begins life in a *background* process group on a real
terminal.

That is only a problem when a child reaches the terminal, but children do. Redirecting stdio
does not stop them: the ``claude`` CLI opens ``/dev/tty`` directly even with stdin on
``DEVNULL`` and stdout/stderr on pipes, and it reads that terminal and restores its modes while
handling the SIGTERM the runner sends when a command overruns its timeout. Both are stopping
operations from a background process group -- the kernel answers them with SIGTTIN / SIGTTOU
addressed to the whole group -- so the child takes the system interface down with it, stopped
mid-call. The listening socket still accepts connections and nothing ever answers them, which
is exactly how the app goes blank.

Running each child in its own session removes the exposure at the source: it inherits no
controlling terminal, so ``/dev/tty`` is not openable and there is no group for a terminal
signal to travel through. Nothing here needs the terminal, and nothing here needs to receive
the terminal's signals -- there is no interactive Ctrl-C to propagate to a background service.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

from imbue.concurrency_group.event_utils import MutableEvent
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version


def run_detached_command(
    command: Sequence[str],
    timeout: float | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    trace_output: bool = False,
    trace_on_line_callback: Callable[[str, bool], None] | None = None,
    shutdown_event: MutableEvent | None = None,
    name: str | None = None,
) -> FinishedProcess:
    """Run ``command`` to completion in its own session, returning how it went.

    A non-zero exit is reported on the result rather than raised: callers here turn a failed
    command into a response for the user, so every one of them reads ``returncode`` itself.
    """
    return run_local_command_modern_version(
        command=command,
        is_checked=False,
        timeout=timeout,
        cwd=cwd,
        env=env,
        trace_output=trace_output,
        trace_on_line_callback=trace_on_line_callback,
        shutdown_event=shutdown_event,
        name=name,
        is_detached_from_terminal=True,
    )
