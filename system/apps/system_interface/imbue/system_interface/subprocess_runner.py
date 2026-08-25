"""The one way the system interface shells out: detached from the workspace's terminal.

Every run-to-completion subprocess spawned from this project's own source goes through
:func:`run_detached_command`, and a ratchet (``test_subprocess_ratchets.py``) keeps it that
way. Two spawns here are isolated by other means: ``AgentManager``'s long-running ``mngr
observe`` child, which asks ``ConcurrencyGroup.run_process_in_background`` for the same
detachment directly, and the PTY auth flows' ``pexpect.spawn``, which puts its child in a new
session on a pty of its own.

Calling into mngr in-process is the hole neither this module nor the ratchet can close: a mngr
function spawns through its own runner with the default (attached) disposition, and the
ratchet's regex only sees calls written here. ``mark_claude_agent_idle`` is one such call, on
the request path. Check any new one by hand for a child that could reach the terminal.

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
signal to travel through. Nothing here needs the terminal, and there is no interactive Ctrl-C
to propagate to a background service.

The cost, which is deliberate: a detached child is out of reach of *any* process-group signal,
supervisord's included, and ``[program:system_interface]`` sets ``stopasgroup``/``killasgroup``.
A ``supervisorctl restart`` therefore no longer takes in-flight children down with the service.
Each child's own timeout is what bounds it instead: a second or two of orphaned ``tmux``/``mngr``
for the request-path commands, up to the ten-minute budget of a ``mngr start --restart`` on the
auth restart thread, which can outlive the restart meant to end it while holding host locks.
The ``mngr observe`` child has no timeout at all, so its bound is the service's own teardown:
``main.py`` turns SIGTERM into a clean exit and the ``atexit`` handler terminates it, but a
service that never gets there -- SIGKILLed for overrunning ``stopwaitsecs``, or OOM-killed --
now leaves it running for good, and the restart starts a second one.
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

    A failure is reported on the result, not raised: the caller reads ``returncode`` and
    ``is_timed_out`` and decides what to tell the user.
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
