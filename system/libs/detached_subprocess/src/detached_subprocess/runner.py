"""The one way a workspace service shells out: detached from the workspace's terminal.

A service that adopts this spawns every subprocess written in its own source through one of
three entry points -- :func:`run_detached_command` for a command that runs to completion in a
service built on ``ConcurrencyGroup``, :func:`run_detached_subprocess` for the same in a service
that is not, :func:`spawn_detached_process` for a child that outlives the call -- and a ratchet
that keeps it that way (``subprocess_ratchets_test.py``, or ``test_subprocess_ratchets.py`` in
the chat app and the system interface). Every supervisord program whose Python source is in this
repo does.

Two things sit outside that. ``bootstrap`` is the foreground process group that owns the
terminal rather than a child of it, so its children are foreground and cannot be stopped this
way; and the ``oom_priority`` launchers ``exec`` into the service rather than spawning it, so
there is no child to detach. The rules also scan Python only, so a supervisord program with a
shell body -- ``owner-exec``, ``vm-exec-register``, and anything ``cron`` drives -- is not
covered by them, whatever its exposure.

Detaching at the top instead, once, is the alternative worth having considered: it would need no
call-site changes and would keep process groups intact so supervisord still reaped everything.
It is not reachable. Leaving a session requires forking, and every candidate is already a
leader -- bootstrap ``exec``s supervisord from the tmux pane, and supervisord ``setpgrp``s each
program before exec, so ``setsid`` returns EPERM in both places. Getting there means forking
supervisord away from the pane, which breaks the contract that keeps the bootstrap window alive.

Workspace services are started by supervisord, which puts each one into its own process group
with ``setpgrp`` -- a new *group*, but the same session, so the service and everything it
spawns keep the session's controlling terminal. In a workspace that terminal is the tmux pane
running ``uv run bootstrap``, whose foreground process group is bootstrap's, not the service's.
So every subprocess a service starts begins life in a *background* process group on a real
terminal.

That is only a problem when a child reaches the terminal, but children do. Redirecting stdio
does not stop them: the ``claude`` CLI opens ``/dev/tty`` directly even with stdin on
``DEVNULL`` and stdout/stderr on pipes, and it reads that terminal and restores its modes while
handling the SIGTERM the runner sends when a command overruns its timeout. Both are stopping
operations from a background process group -- the kernel answers them with SIGTTIN / SIGTTOU
addressed to the whole group -- so the child takes the service down with it, stopped mid-call.
The listening socket still accepts connections and nothing ever answers them, which is exactly
how the app goes blank.

Running each child in its own session removes the exposure at the source: it inherits no
controlling terminal, so ``/dev/tty`` is not openable and there is no group for a terminal
signal to travel through. No service child here needs the terminal, and there is no interactive
Ctrl-C to propagate to a background service.

The cost, which is deliberate: a detached child is out of reach of *any* process-group signal,
supervisord's included, and every ``[program:*]`` that consumes this sets
``stopasgroup``/``killasgroup``. A ``supervisorctl restart`` therefore no longer takes in-flight
children down with the service. Each child's own timeout is what bounds it instead, so a caller
that passes no timeout is choosing to let its child outlive a restart. A service that exits
cleanly can still reap those itself -- an ``atexit`` handler reaches a detached child, since
that is a process handle rather than a group signal -- but one that never gets there, SIGKILLed
for overrunning ``stopwaitsecs`` or OOM-killed, leaves them running for good.

Calling into a library that spawns for you is the hole neither this module nor a ratchet can
close: the library spawns through its own runner with the default (attached) disposition, and a
ratchet's regex only sees calls written in the app. Check any new one by hand for a child that
could reach the terminal.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO

from imbue.concurrency_group.event_utils import MutableEvent
from imbue.concurrency_group.subprocess_utils import (
    FinishedProcess,
    run_local_command_modern_version,
)


def run_detached_command(
    command: Sequence[str],
    timeout: float | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    trace_output: bool = False,
    trace_on_line_callback: Callable[[str, bool], None] | None = None,
    shutdown_event: MutableEvent | None = None,
    shutdown_timeout_sec: float = 30.0,
    name: str | None = None,
) -> FinishedProcess:
    """Run ``command`` to completion in its own session, returning how it went.

    A command that ran and failed is reported on the result, not raised: the caller reads
    ``returncode`` and ``is_timed_out`` and decides what to tell the user. One that could not be
    spawned at all -- binary missing, ``cwd`` unusable -- raises ``ProcessSetupError``, so a
    caller that has to survive that needs its own guard.
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
        shutdown_timeout_sec=shutdown_timeout_sec,
        name=name,
        is_detached_from_terminal=True,
    )


def run_detached_subprocess(
    command: Sequence[str],
    timeout: float | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """:func:`subprocess.run` with the child in its own session, capturing text output.

    The stdlib-shaped counterpart to :func:`run_detached_command`, for a service that does not
    otherwise use ``ConcurrencyGroup``. It keeps :class:`subprocess.CompletedProcess` as the
    caller's currency and the stdlib's exceptions -- :class:`subprocess.TimeoutExpired` on a
    timeout, :class:`OSError` when the binary is missing -- so adopting it changes only where
    the child's session comes from.

    Prefer :func:`run_detached_command` in a service already built on ``ConcurrencyGroup``: it
    reports a timeout on the result rather than raising, and carries the trace callbacks and
    shutdown-event plumbing this one has no way to express.
    """
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=dict(env) if env is not None else None,
        cwd=cwd,
        start_new_session=True,
    )


def spawn_detached_process(
    command: Sequence[str],
    stdout: int | IO[bytes] | None = None,
    stderr: int | IO[bytes] | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """Start a long-lived child in its own session, returning its handle.

    The counterpart to :func:`run_detached_command` for a process that outlives the call -- a
    wrapped server, a display, a browser -- which cannot go through a runner that waits for the
    command to finish.

    Raises :class:`OSError` when the child cannot be spawned, exactly as :func:`subprocess.Popen`
    does, so an existing handler keeps working.

    The caller owns the child's death, and owes two things for it. Terminate it explicitly
    (``terminate``/``kill``/``send_signal``): detaching puts it out of reach of the service's
    process group, so supervisord's ``stopasgroup``/``killasgroup`` no longer reaps it and the
    handle returned here is the only thing that does. And if the child holds a fixed port, sweep
    for an orphan at startup -- a parent SIGKILLed for overrunning ``stopwaitsecs`` never runs its
    own teardown, and the orphan then holds the port against the restarted service
    (``chrome_launcher.reap_orphan`` is the worked example).
    """
    return subprocess.Popen(
        list(command),
        stdout=stdout,
        stderr=stderr,
        env=dict(env) if env is not None else None,
        cwd=cwd,
        start_new_session=True,
    )
