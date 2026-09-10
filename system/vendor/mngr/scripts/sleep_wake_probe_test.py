import logging
import sys
from pathlib import Path
from typing import Any

import pytest

# scripts/sleep_wake_probe.py imports its sibling module bare (matching how it is
# invoked, `uv run --script scripts/sleep_wake_probe.py`). Make that resolvable
# for pytest by adding scripts/ to sys.path before importing it.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from scripts.sleep_wake_common import Clocks  # noqa: E402
from scripts.sleep_wake_common import EventLog  # noqa: E402
from scripts.sleep_wake_common import HeartbeatMonitor  # noqa: E402
from scripts.sleep_wake_common import WakeObservation  # noqa: E402
from scripts.sleep_wake_probe import BlockedReadProbe  # noqa: E402
from scripts.sleep_wake_probe import IdleThenOpenProbe  # noqa: E402
from scripts.sleep_wake_probe import NetworkReturnProbe  # noqa: E402
from scripts.sleep_wake_probe import PeerKeepaliveObserver  # noqa: E402
from scripts.sleep_wake_probe import RawTCPProbe  # noqa: E402
from scripts.sleep_wake_probe import SSHTarget  # noqa: E402
from scripts.sleep_wake_probe import _LOG_CHANNEL_ROOT  # noqa: E402
from scripts.sleep_wake_probe import build_report  # noqa: E402

_PEER_SSHD_CONFIG = "clientaliveinterval 0\nclientalivecountmax 3\nlogingracetime 120"

# What each blocked read can come back with, as the probes record it.
_PEER_CLOSED: dict[str, Any] = {
    "clocks": {"wall": 1002.0, "py_monotonic": 102.0},
    "outcome": "recv returned 0 bytes (peer closed)",
}
_CARRIED_A_BYTE: dict[str, Any] = {
    "clocks": {"wall": 1002.0, "py_monotonic": 102.0},
    "outcome": "recv returned 1 bytes",
}


def _report_after_a_four_minute_sleep(
    tmp_path: Path,
    *,
    authenticated_read: dict[str, Any] | None,
    raw_socket_read: dict[str, Any] | None,
) -> str:
    """The report for a run whose only measured facts are which reads were released."""
    clocks = Clocks()
    log = EventLog(tmp_path / "probe.jsonl", clocks)
    target = SSHTarget(hostname="host.example", port=22, username="root", key_filenames=[], proxy_command=None)
    reference = Clocks.reference_name()
    keepalives = PeerKeepaliveObserver(log, clocks)
    # The peer sent its keepalive every 30s while the laptop was up, so the
    # running sshd has ClientAliveInterval 30 whatever its config file says.
    keepalives.arrivals["blocked_unwatched"] = [700.0, 730.0, 760.0]
    wake = WakeObservation(
        wake_clocks={"wall": 1000.0, "py_monotonic": 100.0, reference: 340.0},
        gaps={"wall": 240.0, "py_monotonic": 1.0, reference: 240.0},
    )
    lines = build_report(
        heartbeat=HeartbeatMonitor(log=log, clocks=clocks, on_wake=lambda observation: None, wakes=[wake]),
        timers=[],
        blocked=BlockedReadProbe(
            name="blocked_unwatched",
            log=log,
            clocks=clocks,
            target=target,
            watched=False,
            established={"wall": 690.0, "py_monotonic": 0.0},
            read_released=authenticated_read,
        ),
        watched=BlockedReadProbe(name="blocked_watched", log=log, clocks=clocks, target=target, watched=True),
        idle=IdleThenOpenProbe(
            name="idle_cached", log=log, clocks=clocks, target=target, delay_after_wake_seconds=2.0
        ),
        raw_tcp=RawTCPProbe(log=log, clocks=clocks, target=target, released=raw_socket_read),
        network=NetworkReturnProbe(log=log, clocks=clocks, target=target),
        keepalives=keepalives,
        black_hole=None,
        reference_clock=reference,
        pmset_log=[],
        remote_context={"sshd_config_file": _PEER_SSHD_CONFIG, "sshd_process_chain": "/usr/sbin/sshd -D"},
    )
    return "\n".join(lines)


@pytest.mark.parametrize(
    "authenticated_read, raw_socket_read, expected_verdict",
    [
        # The peer's LoginGraceTime closes the unauthenticated socket during any
        # sleep this probe asks for, so on its own it settles nothing.
        pytest.param(None, _PEER_CLOSED, "UNMEASURED", id="only-the-raw-socket-released"),
        pytest.param(_PEER_CLOSED, None, "HOLDS", id="authenticated-read-saw-the-close"),
        pytest.param(_CARRIED_A_BYTE, _PEER_CLOSED, "DOES NOT HOLD", id="authenticated-read-outlived-the-sleep"),
        pytest.param(None, None, "UNMEASURED", id="neither-released"),
    ],
)
def test_the_dead_connection_verdict_comes_from_the_authenticated_read(
    tmp_path: Path,
    authenticated_read: dict[str, Any] | None,
    raw_socket_read: dict[str, Any] | None,
    expected_verdict: str,
) -> None:
    report = _report_after_a_four_minute_sleep(
        tmp_path, authenticated_read=authenticated_read, raw_socket_read=raw_socket_read
    )
    assert f"verdict (connection died): {expected_verdict}" in report


def test_the_report_reads_the_peer_reap_interval_from_its_keepalives_not_its_config_file(tmp_path: Path) -> None:
    report = _report_after_a_four_minute_sleep(tmp_path, authenticated_read=_PEER_CLOSED, raw_socket_read=None)
    assert "peer ClientAliveInterval: ~30s, measured from the keepalive requests" in report
    assert "(sshd -T; blind to -o flags): clientaliveinterval 0" in report


def _transport_log_record(probe_name: str, message: str) -> logging.LogRecord:
    """A paramiko transport log record as it reaches the observer, on ``probe_name``'s channel."""
    return logging.LogRecord(
        name=f"{_LOG_CHANNEL_ROOT}.{probe_name}",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=0,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_only_the_peers_keepalive_requests_are_counted_and_against_their_own_connection(tmp_path: Path) -> None:
    clocks = Clocks()
    observer = PeerKeepaliveObserver(EventLog(tmp_path / "probe.jsonl", clocks), clocks)
    for probe_name, message in (
        # The peer's request: a channel request where a session is open, a global one where none is.
        ("blocked_unwatched", 'Unhandled channel request "keepalive@openssh.com"'),
        ("idle_cached", 'Received global request "keepalive@openssh.com"'),
        # Our own keepalive, and a request that is not one at all.
        ("idle_cached", 'Sending global request "keepalive@openssh.com"'),
        ("idle_cached", 'Received global request "hostkeys-00@openssh.com"'),
    ):
        observer.emit(_transport_log_record(probe_name, message))

    assert {name: len(arrivals) for name, arrivals in observer.arrivals.items()} == {
        "blocked_unwatched": 1,
        "idle_cached": 1,
    }
    # One request spaces nothing, so the report says the window ruled nothing out
    # rather than claiming an interval.
    assert observer.measured_interval_seconds("blocked_unwatched", before_wall=float("inf")) is None


@pytest.mark.parametrize(
    "known_hosts_argument, expected_known_hosts",
    [
        pytest.param("/keys/known_hosts", "/keys/known_hosts", id="absolute"),
        pytest.param("~/.minds/known_hosts", str(Path("~/.minds/known_hosts").expanduser()), id="tilde"),
    ],
)
def test_resolve_reads_the_recovery_card_command(known_hosts_argument: str, expected_known_hosts: str) -> None:
    target = SSHTarget.resolve(
        f'ssh -i ~/.minds/id -o "UserKnownHostsFile={known_hosts_argument}" '
        "-o StrictHostKeyChecking=yes -p 2222 root@203.0.113.5"
    )
    assert target.hostname == "203.0.113.5"
    assert target.port == 2222
    assert target.username == "root"
    assert target.key_filenames == [str(Path("~/.minds/id").expanduser())]
    assert target.known_hosts_path == expected_known_hosts
    assert target.proxy_command is None
