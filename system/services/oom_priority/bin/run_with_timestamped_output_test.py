"""earlyoom's supervisord wrapper timestamps every line it writes, as it writes it.

earlyoom's log is read to tell when it acted relative to the rest of the
workspace, so a line must reach the log with its own time while earlyoom is still
running, not when a buffer fills or the process exits.
"""

import os
import re
import select
import subprocess
import time
from pathlib import Path

_WRAPPER = Path(__file__).with_name("run_with_timestamped_output.sh")
_STAMPED_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z (.*)$")


def _read_lines(fd: int, count: int, timeout_seconds: float) -> list[str]:
    received = b""
    deadline = time.monotonic() + timeout_seconds
    while received.count(b"\n") < count:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"only {received!r} arrived within {timeout_seconds}s"
        ready, _, _ = select.select([fd], [], [], remaining)
        if ready:
            chunk = os.read(fd, 4096)
            assert chunk, f"the stream closed after {received!r}"
            received += chunk
    return received.decode().splitlines()[:count]


def test_both_streams_arrive_timestamped_on_stderr_while_the_command_still_runs() -> None:
    process = subprocess.Popen(
        [str(_WRAPPER), "sh", "-c", "echo to-stdout; echo to-stderr >&2; echo pid=$$; exec sleep 38211"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    try:
        lines = _read_lines(process.stderr.fileno(), 3, timeout_seconds=5)
        assert process.poll() is None
        messages = []
        for line in lines:
            match = _STAMPED_LINE.match(line)
            assert match is not None, line
            messages.append(match.group(1))
        assert messages == ["to-stdout", "to-stderr", f"pid={process.pid}"]
    finally:
        process.kill()
        stdout, _ = process.communicate(timeout=10)
    assert stdout == b""
