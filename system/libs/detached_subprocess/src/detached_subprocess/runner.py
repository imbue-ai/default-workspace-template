"""The one way a workspace service shells out: detached from the workspace's terminal.

A service that adopts this spawns every subprocess written in its own source through one of
three entry points -- :func:`run_detached_command` for a command that runs to completion in a
service built on ``ConcurrencyGroup``, :func:`run_detached_subprocess` for the same in a service
that is not, :func:`spawn_detached_process` for a child that outlives the call -- and a ratchet
(``test_subprocess_ratchets.py``) that keeps it that way. Every supervisord program whose source
is in this repo does. What remains attached is the code that is not one: ``bootstrap``, which is
the foreground process group that owns the terminal rather than a child of it, and the CLI tools
under ``system/scripts/``, which a person runs and which may legitimately want a terminal.

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


class DetachedSpawnError(Exception):
    """Raised when a long-lived child could not be started at all."""


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
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
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
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.Popen[bytes]:
    """Start a long-lived child in its own session, returning its handle.

    The counterpart to :func:`run_detached_command` for a process that outlives the call -- a
    wrapped server, a display, a browser -- which cannot go through a runner that waits for the
    command to finish.

    The caller owns the child's death. Detaching puts it out of reach of the service's process
    group, so supervisord's ``stopasgroup``/``killasgroup`` no longer reaps it: only the handle
    returned here does. Terminate it explicitly (``terminate``/``kill``/``send_signal``), which
    addresses the process rather than the group and so still works. A child that nothing
    terminates will survive the service that started it -- pass it to
    :func:`subprocess.Popen` directly instead, and say why in the app's ratchet allowlist.
    """
    try:
        return subprocess.Popen(
            list(command),
            stdout=stdout,
            stderr=stderr,
            env=dict(env) if env is not None else None,
            cwd=cwd,
            start_new_session=True,
        )
    except OSError as e:
        raise DetachedSpawnError(f"cannot start {list(command)}: {e}") from e
