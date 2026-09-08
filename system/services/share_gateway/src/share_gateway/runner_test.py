"""How the gateway takes its detached children down.

Detaching caddy and frpc removed them from supervisord's group signals, so the runner's own
teardown is the only thing that stops them, and the program's stopwaitsecs is the only thing
bounding it. What that budget has to survive is a child that ignores SIGTERM.
"""

import subprocess
import sys
import time

from share_gateway.runner import _stop_children

# Installs SIG_IGN for SIGTERM, announces that it is ready, and then blocks until it is killed:
# an ignored signal never wakes signal.pause(), so only SIGKILL ends this child.
_IGNORES_SIGTERM = (
    "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('ready', flush=True); signal.pause()"
)


def _spawn_sigterm_ignoring_child() -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [sys.executable, "-c", _IGNORES_SIGTERM], stdout=subprocess.PIPE
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == b"ready"
    return process


def test_one_child_ignoring_sigterm_does_not_consume_every_child_s_grace() -> None:
    """The grace period is shared, not spent per child.

    Waiting on each child before signalling the next would give four stubborn children four
    grace periods, which overruns stopwaitsecs and leaves the ones never reached holding their
    ports (caddy's 443 among them) when supervisord SIGKILLs the gateway.
    """
    children = [_spawn_sigterm_ignoring_child() for _ in range(4)]
    grace_seconds = 0.5
    try:
        started_at = time.monotonic()
        _stop_children(
            [(f"child[{index}]", process) for index, process in enumerate(children)],
            grace_seconds=grace_seconds,
        )
        elapsed = time.monotonic() - started_at
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
                process.wait()
            assert process.stdout is not None
            process.stdout.close()

    assert all(process.poll() is not None for process in children)
    # Sits between the one shared period this must take and the four a per-child wait would.
    assert elapsed < 2 * grace_seconds, (
        f"stopping {len(children)} children took {elapsed:.2f}s, which is a per-child grace "
        f"period rather than the shared {grace_seconds}s one"
    )
