#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["paramiko>=3.4"]
# ///
"""Measure, across a real laptop sleep, the facts the sleep/wake handling relies on.

Run this on a laptop with an SSH host it can reach, sleep the laptop for several
minutes, wake it, and read the report. Each section states a claim the code
makes (``imbue_common.suspension``, mngr's ``SuspensionWatchdog``, the ``mngr
forward`` tunnel retirement, minds' post-wake grace) alongside what this machine
actually did.

    uv run --script scripts/sleep_wake_probe.py user@host[:port] --sleep-minutes 4
    uv run --script scripts/sleep_wake_probe.py 'ssh -i ~/.minds/... -p 2222 user@host' --sleep-minutes 4

A bare host is resolved through ``~/.ssh/config``; the quoted form is the
"connect over SSH" command from a workspace's recovery card. Prefer a
mngr-launched container sshd (``ClientAliveInterval 30``); a stock sshd never
reaps a silent client. The report measures which it found from the keepalives
the peer actually sends before the sleep, since ``sshd -T`` is blind to the
``-o`` flags mngr passes on the command line. Sleep at least four minutes so the
peer's reap window and the kernel's retransmission timeout can both run.

``--auto-sleep`` schedules the wake (``sudo pmset relative wake``) and sleeps the
machine itself; that is not identical to a lid-closed sleep, so a surprising
result is worth re-running the other way. ``--black-hole`` (macOS, sudo)
reproduces the dead-NAT case of a long sleep, where packets on the old
connections vanish without a reply, with a pf rule scoped to the probe's own
local ports; installed after the pre-sleep window so the peer's keepalives can
still be counted, and lifted at the end of the run. That mode asks for the sudo
password up front and holds the credential open, since the run outlives sudo's
cache and neither installing nor lifting the rule can wait on a prompt nobody is
there to answer. Give it a ``--post-wake-wait`` of ten minutes or so.

Everything observed is also appended to a JSONL file (path printed at the end).
"""

import argparse
import logging
import select
import shlex
import socket
import statistics
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Final

import paramiko
from sleep_wake_common import BlackHole
from sleep_wake_common import BlackHoleError
from sleep_wake_common import Clocks
from sleep_wake_common import EventLog
from sleep_wake_common import HEARTBEAT_GAP_THRESHOLD_SECONDS
from sleep_wake_common import HeartbeatMonitor
from sleep_wake_common import IS_DARWIN
from sleep_wake_common import SUSPENSION_THRESHOLD_SECONDS
from sleep_wake_common import WakeObservation
from sleep_wake_common import local_context
from sleep_wake_common import pmset_log_since
from sleep_wake_common import schedule_sleep
from sleep_wake_common import was_suspended_since

# Mirrors mngr's SuspensionWatchdog check interval.
WATCHDOG_CHECK_INTERVAL_SECONDS: Final[float] = 5.0
# Mirrors mngr's and the forward's paramiko keepalive.
SSH_KEEPALIVE_INTERVAL_SECONDS: Final[int] = 15
# Mirrors the forward's and OuterHost's channel-open bound.
CHANNEL_OPEN_TIMEOUT_SECONDS: Final[float] = 30.0
# Mirrors minds' post-wake grace and shadow, and its stuck threshold. The shadow
# is how late after a wake a failure run may open and still earn the grace; the
# grace is how long such a run must then last. They are separate spans because a
# run opens with the first post-wake traffic, not with the rebuild.
POST_WAKE_GRACE_SECONDS: Final[float] = 20.0
POST_WAKE_SHADOW_SECONDS: Final[float] = 60.0
STUCK_THRESHOLD_SECONDS: Final[float] = 5.0

# Slack on top of a run's own longest path, before the black hole's deadman
# lifts the rule on its own. Generous, since firing it early would end a run
# that was still going; it is a backstop against an abandoned rule, not a
# schedule.
_DEADMAN_MARGIN_SECONDS: Final[float] = 900.0

# Parent of every transport's log channel, so one handler sees them all and the
# channel name says which probe's connection a record came from.
_LOG_CHANNEL_ROOT: Final[str] = "sleep_wake_probe.transport"
# How paramiko logs the request OpenSSH's ClientAliveInterval sends: a global
# request on a connection with no open channel, a channel request otherwise.
# Neither is the "Sending global request" of paramiko's own keepalive.
_PEER_KEEPALIVE_MARKERS: Final[tuple[str, ...]] = (
    'Received global request "keepalive@openssh.com"',
    'Unhandled channel request "keepalive@openssh.com"',
)


class PeerKeepaliveObserver(logging.Handler):
    """Records every keepalive request the peer's sshd sends, per probe connection.

    The only measurement of the running sshd's ``ClientAliveInterval``: the
    interval is the spacing of the requests it sends an idle client, and a
    peer that sends none in the pre-sleep window has it off (or longer than
    the window). Attached to the parent of every transport's log channel.
    """

    def __init__(self, log: EventLog, clocks: Clocks) -> None:
        super().__init__(level=logging.DEBUG)
        self._log = log
        self._clocks = clocks
        self._arrivals_lock = threading.Lock()
        self.arrivals: dict[str, list[float]] = {}

    def install(self) -> None:
        parent = logging.getLogger(_LOG_CHANNEL_ROOT)
        parent.setLevel(logging.DEBUG)
        parent.addHandler(self)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if not any(marker in message for marker in _PEER_KEEPALIVE_MARKERS):
            return
        probe = record.name.removeprefix(f"{_LOG_CHANNEL_ROOT}.")
        wall = self._clocks.sample()["wall"]
        with self._arrivals_lock:
            arrivals = self.arrivals.setdefault(probe, [])
            since_previous = wall - arrivals[-1] if arrivals else None
            arrivals.append(wall)
        self._log.record(
            "peer_keepalive_received",
            probe=probe,
            since_previous=None if since_previous is None else round(since_previous, 1),
        )

    def measured_interval_seconds(self, probe: str, before_wall: float) -> float | None:
        """The peer's keepalive spacing on ``probe``'s connection, from the requests that arrived before ``before_wall``.

        The median of the gaps, so a request the machine was asleep for does
        not pull it; None with fewer than two requests to space.
        """
        with self._arrivals_lock:
            arrivals = [wall for wall in self.arrivals.get(probe, []) if wall < before_wall]
        if len(arrivals) < 2:
            return None
        return statistics.median(b - a for a, b in zip(arrivals, arrivals[1:], strict=False))


# ---------------------------------------------------------------------------
# Timer probes: which deadlines freeze with the machine
# ---------------------------------------------------------------------------


@dataclass
class TimerProbe:
    name: str
    log: EventLog
    clocks: Clocks
    seconds: float
    started: dict[str, float] = field(default_factory=dict)
    fired: dict[str, float] | None = None

    def run(self, wait: Callable[[float], None]) -> None:
        self.started = self.clocks.sample()
        self.log.record("timer_started", timer=self.name, seconds=self.seconds)
        wait(self.seconds)
        self.fired = self.clocks.sample()
        self.log.record(
            "timer_fired",
            timer=self.name,
            wall_elapsed=round(self.fired["wall"] - self.started["wall"], 2),
            py_monotonic_elapsed=round(self.fired["py_monotonic"] - self.started["py_monotonic"], 2),
        )


def _wait_event(seconds: float) -> None:
    threading.Event().wait(seconds)


def _wait_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _wait_socket_timeout(seconds: float) -> None:
    left, right = socket.socketpair()
    try:
        left.settimeout(seconds)
        try:
            left.recv(1)
        except TimeoutError:
            pass
    finally:
        left.close()
        right.close()


def _wait_select(seconds: float) -> None:
    left, right = socket.socketpair()
    try:
        select.select([left], [], [], seconds)
    finally:
        left.close()
        right.close()


def _wait_lock_timeout(seconds: float) -> None:
    lock = threading.Lock()
    lock.acquire()
    lock.acquire(timeout=seconds)


TIMER_WAITS: Final[dict[str, Callable[[float], None]]] = {
    "threading.Event.wait": _wait_event,
    "time.sleep": _wait_sleep,
    "socket.settimeout+recv": _wait_socket_timeout,
    "select.select": _wait_select,
    "threading.Lock.acquire(timeout)": _wait_lock_timeout,
}


# ---------------------------------------------------------------------------
# SSH probes
# ---------------------------------------------------------------------------


@dataclass
class SSHTarget:
    hostname: str
    port: int
    username: str | None
    key_filenames: list[str]
    proxy_command: str | None
    known_hosts_path: str | None = None

    @classmethod
    def resolve(cls, spec: str) -> "SSHTarget":
        """Accept ``[user@]host[:port]`` or a full ``ssh -i KEY -p PORT user@host`` command.

        The latter is what minds' recovery card offers under "connect over
        SSH" (mngr's ``build_ssh_connect_command``), key and host-key pin
        included, so it can be pasted verbatim.
        """
        if spec.startswith("ssh ") or spec == "ssh":
            return cls._from_ssh_command(spec)
        username: str | None = None
        if "@" in spec:
            username, spec = spec.split("@", 1)
        port: int | None = None
        if ":" in spec:
            spec, port_text = spec.rsplit(":", 1)
            port = int(port_text)
        config = paramiko.SSHConfig()
        config_path = Path.home() / ".ssh" / "config"
        if config_path.exists():
            with config_path.open() as f:
                config.parse(f)
        entry = config.lookup(spec)
        return cls(
            hostname=entry.get("hostname", spec),
            port=port or int(entry.get("port", 22)),
            username=username or entry.get("user"),
            key_filenames=list(entry.get("identityfile", [])),
            proxy_command=entry.get("proxycommand"),
        )

    @classmethod
    def _from_ssh_command(cls, command: str) -> "SSHTarget":
        parser = argparse.ArgumentParser(prog="ssh", add_help=False)
        parser.add_argument("-i", dest="identity", action="append", default=[])
        parser.add_argument("-p", dest="port", type=int, default=22)
        parser.add_argument("-o", dest="options", action="append", default=[])
        parser.add_argument("destination")
        args = parser.parse_args(shlex.split(command)[1:])
        username: str | None = None
        hostname = args.destination
        if "@" in hostname:
            username, hostname = hostname.split("@", 1)
        known_hosts: str | None = None
        for option in args.options:
            key, _, value = option.partition("=")
            if key.strip().lower() == "userknownhostsfile":
                known_hosts = str(Path(value.strip()).expanduser())
        return cls(
            hostname=hostname,
            port=args.port,
            username=username,
            key_filenames=[str(Path(k).expanduser()) for k in args.identity],
            proxy_command=None,
            known_hosts_path=known_hosts,
        )

    def connect(self, log_channel: str) -> paramiko.SSHClient:
        """Connect, logging the transport under ``log_channel`` so its records can be told apart."""
        client = paramiko.SSHClient()
        if self.known_hosts_path is not None:
            # Honour the pin the pasted command asked for by leaving paramiko's
            # default RejectPolicy in place: AutoAddPolicy would accept a key the
            # file does not list *and* save it back into the provider's file.
            client.load_host_keys(self.known_hosts_path)
        else:
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        sock = paramiko.ProxyCommand(self.proxy_command) if self.proxy_command else None
        client.connect(
            self.hostname,
            port=self.port,
            username=self.username,
            key_filename=self.key_filenames[0] if self.key_filenames else None,
            sock=sock,
            timeout=20,
            banner_timeout=30,
            auth_timeout=30,
        )
        transport = client.get_transport()
        assert transport is not None
        transport.set_log_channel(f"{_LOG_CHANNEL_ROOT}.{log_channel}")
        transport.set_keepalive(SSH_KEEPALIVE_INTERVAL_SECONDS)
        return client


def _local_port(client: paramiko.SSHClient) -> int:
    transport = client.get_transport()
    assert transport is not None
    sock = transport.sock
    assert isinstance(sock, socket.socket)
    return int(sock.getsockname()[1])


def _run_remote(client: paramiko.SSHClient, command: str) -> str:
    _stdin, stdout, _stderr = client.exec_command(command, timeout=30)
    return stdout.read().decode(errors="replace").strip()


@dataclass
class BlockedReadProbe:
    """A command blocked reading a channel across the sleep -- mngr's `mngr start` case.

    ``watched`` adds the SuspensionWatchdog behaviour: a thread that compares
    the clocks every five seconds and closes the transport when they diverge.
    Without it the read is released only by whatever the kernel and the peer do.
    """

    name: str
    log: EventLog
    clocks: Clocks
    target: SSHTarget
    watched: bool
    established: dict[str, float] = field(default_factory=dict)
    read_released: dict[str, Any] | None = None
    watchdog_closed_at: dict[str, float] | None = None
    inactive_seen_at: dict[str, float] | None = None
    _client: paramiko.SSHClient | None = None
    _stop: threading.Event = field(default_factory=threading.Event)

    def start(self) -> None:
        self._client = self.target.connect(self.name)
        self.established = self.clocks.sample()
        self.log.record("ssh_connected", probe=self.name, local_port=self.local_port)
        threading.Thread(target=self._blocked_read, name=f"{self.name}-read", daemon=True).start()
        threading.Thread(target=self._poll_is_active, name=f"{self.name}-active", daemon=True).start()
        if self.watched:
            threading.Thread(target=self._watchdog, name=f"{self.name}-watchdog", daemon=True).start()

    @property
    def local_port(self) -> int:
        assert self._client is not None
        return _local_port(self._client)

    def _blocked_read(self) -> None:
        assert self._client is not None
        transport = self._client.get_transport()
        assert transport is not None
        channel = transport.open_session()
        channel.exec_command("sleep 86400")
        try:
            data = channel.recv(1)
            outcome = f"recv returned {len(data)} bytes"
        except Exception as e:  # noqa: BLE001 -- the exception class is the measurement
            outcome = f"{type(e).__name__}: {e}"
        self.read_released = {"clocks": self.clocks.sample(), "outcome": outcome}
        self.log.record("blocked_read_released", probe=self.name, outcome=outcome)

    def _poll_is_active(self) -> None:
        assert self._client is not None
        while not self._stop.wait(2.0):
            transport = self._client.get_transport()
            if transport is None or not transport.is_active():
                self.inactive_seen_at = self.clocks.sample()
                self.log.record("transport_inactive", probe=self.name)
                return

    def _watchdog(self) -> None:
        assert self._client is not None
        while not self._stop.wait(WATCHDOG_CHECK_INTERVAL_SECONDS):
            if was_suspended_since(self.established, self.clocks.sample()):
                self.watchdog_closed_at = self.clocks.sample()
                self.log.record("watchdog_closing_transport", probe=self.name)
                transport = self._client.get_transport()
                if transport is not None:
                    transport.close()
                return

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            self._client.close()


@dataclass
class IdleThenOpenProbe:
    """An idle connection (the forward's cached tunnel) asked for a channel just after the wake."""

    name: str
    log: EventLog
    clocks: Clocks
    target: SSHTarget
    delay_after_wake_seconds: float
    established: dict[str, float] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    _client: paramiko.SSHClient | None = None
    _open_started: threading.Event = field(default_factory=threading.Event)

    def start(self) -> None:
        self._client = self.target.connect(self.name)
        self.established = self.clocks.sample()
        self.log.record("ssh_connected", probe=self.name, local_port=self.local_port)

    @property
    def local_port(self) -> int:
        assert self._client is not None
        return _local_port(self._client)

    def on_wake(self, wake: WakeObservation) -> None:
        """Open once, on the first wake: a later dark wake is no longer the first open after the sleep."""
        if self._open_started.is_set():
            return
        self._open_started.set()
        threading.Thread(target=self._open_after_wake, name=f"{self.name}-open", daemon=True).start()

    def _open_after_wake(self) -> None:
        assert self._client is not None
        time.sleep(self.delay_after_wake_seconds)
        transport = self._client.get_transport()
        assert transport is not None
        started = self.clocks.sample()
        self.log.record(
            "idle_open_started",
            probe=self.name,
            is_active_before=transport.is_active(),
            suspension_rule_fires=was_suspended_since(self.established, started),
        )
        try:
            channel = transport.open_session(timeout=CHANNEL_OPEN_TIMEOUT_SECONDS)
            channel.exec_command("true")
            channel.recv_exit_status()
            outcome = "channel opened and command ran"
        except Exception as e:  # noqa: BLE001 -- the exception class is the measurement
            outcome = f"{type(e).__name__}: {e}"
        finished = self.clocks.sample()
        self.result = {"outcome": outcome, "seconds": finished["wall"] - started["wall"]}
        self.log.record(
            "idle_open_finished", probe=self.name, outcome=outcome, seconds=round(self.result["seconds"], 2)
        )

    def stop(self) -> None:
        if self._client is not None:
            self._client.close()


@dataclass
class RawTCPProbe:
    """A bare TCP socket to the SSH port, blocked in recv: the kernel's answer with paramiko out of the way.

    It never authenticates, so the peer's ``LoginGraceTime`` closes it during any
    sleep worth running this for. What it times is therefore how long after the
    wake a close the peer queued while the NIC was down reaches a blocked reader,
    not whether the suspension is what killed the connection.
    """

    log: EventLog
    clocks: Clocks
    target: SSHTarget
    released: dict[str, Any] | None = None
    _sock: socket.socket | None = None

    def start(self) -> None:
        if self.target.proxy_command:
            self.log.record("raw_tcp_skipped", reason="ProxyCommand host")
            return
        self._sock = socket.create_connection((self.target.hostname, self.target.port), timeout=20)
        # Under the connect timeout, since this runs on the main thread before any
        # other probe: a peer that accepts but withholds its banner would otherwise
        # hang the whole run with nothing measuring. Only the recv below is meant
        # to block without a bound.
        self._sock.recv(4096)  # the banner
        self._sock.settimeout(None)
        self.log.record("raw_tcp_connected", local_port=self.local_port)
        threading.Thread(target=self._blocked_recv, name="raw-tcp-read", daemon=True).start()

    @property
    def local_port(self) -> int | None:
        return None if self._sock is None else int(self._sock.getsockname()[1])

    def _blocked_recv(self) -> None:
        assert self._sock is not None
        try:
            data = self._sock.recv(1)
            outcome = f"recv returned {len(data)} bytes" + (" (peer closed)" if not data else "")
        except Exception as e:  # noqa: BLE001 -- the exception class is the measurement
            outcome = f"{type(e).__name__}: {e}"
        self.released = {"clocks": self.clocks.sample(), "outcome": outcome}
        self.log.record("raw_tcp_released", outcome=outcome)

    def stop(self) -> None:
        if self._sock is not None:
            self._sock.close()


@dataclass
class NetworkReturnProbe:
    """After a wake, how long until this laptop can resolve and reach the SSH host again."""

    log: EventLog
    clocks: Clocks
    target: SSHTarget
    results: list[dict[str, Any]] = field(default_factory=list)

    def on_wake(self, wake: WakeObservation) -> None:
        threading.Thread(target=self._poll, args=(wake,), name="network-return", daemon=True).start()

    def _poll(self, wake: WakeObservation) -> None:
        first_dns: float | None = None
        first_tcp: float | None = None
        deadline = time.time() + 180
        while time.time() < deadline and (first_dns is None or first_tcp is None):
            if first_dns is None:
                try:
                    socket.getaddrinfo(self.target.hostname, self.target.port, proto=socket.IPPROTO_TCP)
                    # Stamped after the call, like the connect below: a resolver
                    # still coming back from the wake can sit inside getaddrinfo
                    # for most of the window this is measuring.
                    first_dns = time.time() - wake.wake_clocks["wall"]
                    self.log.record("network_dns_ok", seconds_after_wake=round(first_dns, 2))
                except OSError:
                    pass
            if first_dns is not None and first_tcp is None:
                try:
                    with socket.create_connection((self.target.hostname, self.target.port), timeout=1.5):
                        first_tcp = time.time() - wake.wake_clocks["wall"]
                        self.log.record("network_tcp_ok", seconds_after_wake=round(first_tcp, 2))
                except OSError:
                    pass
            time.sleep(0.5)
        self.results.append({"dns": first_dns, "tcp": first_tcp, "wake_wall": wake.wake_clocks["wall"]})


# ---------------------------------------------------------------------------
# Environment context
# ---------------------------------------------------------------------------


def _remote_context(client: paramiko.SSHClient) -> dict[str, str]:
    """The peer sshd's ``ClientAlive*``/``LoginGraceTime`` from its config file and its command line.

    Printed as context only: ``sshd -T`` is blind to the ``-o`` flags mngr
    starts sshd with, and a newer OpenSSH rewrites its process title, so the
    report's verdict comes from :class:`PeerKeepaliveObserver` instead.
    """
    config_file = _run_remote(
        client,
        "(sshd -T 2>/dev/null || sudo -n sshd -T 2>/dev/null || /usr/sbin/sshd -T 2>/dev/null)"
        " | grep -iE 'clientalive|logingracetime'",
    )
    listener = _run_remote(
        client,
        'p=$(ps -o ppid= -p $$ | tr -d \' \'); while [ -n "$p" ] && [ "$p" -gt 1 ]; do a=$(ps -o args= -p "$p" 2>/dev/null); case "$a" in *sshd*) echo "$a";; esac; p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d \' \'); done',
    )
    return {
        "sshd_config_file": config_file or "<could not read; sshd -T needs root>",
        "sshd_process_chain": listener or "<no sshd ancestor found>",
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _verdict(ok: bool | None) -> str:
    if ok is None:
        return "UNMEASURED"
    return "HOLDS" if ok else "DOES NOT HOLD"


def _seconds_after(clocks: dict[str, float] | None, wake: dict[str, float] | None) -> str:
    if clocks is None or wake is None:
        return "n/a"
    return f"{clocks['wall'] - wake['wall']:+.1f}s after wake"


def build_report(
    *,
    heartbeat: HeartbeatMonitor,
    timers: list[TimerProbe],
    blocked: BlockedReadProbe,
    watched: BlockedReadProbe,
    idle: IdleThenOpenProbe,
    raw_tcp: RawTCPProbe,
    network: NetworkReturnProbe,
    keepalives: PeerKeepaliveObserver,
    black_hole: BlackHole | None,
    reference_clock: str,
    pmset_log: list[str],
    remote_context: dict[str, str],
) -> list[str]:
    out: list[str] = []
    wakes = heartbeat.wakes
    if not wakes:
        return ["No heartbeat gap over 5s was observed: the machine did not sleep, or slept less than 5s."]
    first_wake = wakes[0]
    last_wake = wakes[-1]
    if black_hole is not None:
        out.append(
            f"BLACK-HOLE RUN: packets on the probe connections (local ports {black_hole.local_ports}) were dropped "
            "from before the sleep to the end of the run, so no reset from the peer could reach them and every "
            "release below came from a bound on this side, not from the peer."
        )
    total_wall_gap = sum(w.gaps["wall"] for w in wakes)
    total_mono_gap = sum(w.gaps["py_monotonic"] for w in wakes)
    total_reference_gap = sum(w.gaps[reference_clock] for w in wakes)

    out.append("== 1. time.monotonic() stops while the machine is suspended (imbue_common.suspension) ==")
    out.append(
        f"   heartbeat gaps: wall {total_wall_gap:.1f}s, py_monotonic {total_mono_gap:.1f}s, "
        f"{reference_clock} {total_reference_gap:.1f}s (py_monotonic impl: {time.get_clock_info('monotonic').implementation})"
    )
    for wake in wakes:
        out.append("     gap per clock: " + ", ".join(f"{k}={v:.1f}" for k, v in wake.gaps.items()))
    monotonic_stopped = total_wall_gap - total_mono_gap >= SUSPENSION_THRESHOLD_SECONDS
    out.append(f"   verdict: {_verdict(monotonic_stopped)}")
    if not monotonic_stopped:
        out.append(
            "   !! wall and monotonic advanced together. was_suspended_since() never returns True on this "
            "machine, so the watchdog and the forward's tunnel retirement never fire here."
        )

    out.append("== 2. A sleep is one contiguous gap the SleepTracker sees as one wake (heartbeat gap >= 30s) ==")
    out.append(
        f"   gaps over 5s: {len(wakes)}; gaps over 30s: {sum(1 for w in wakes if w.gaps['wall'] >= HEARTBEAT_GAP_THRESHOLD_SECONDS)}"
    )
    if pmset_log:
        out.append("   pmset -g log entries in the window:")
        out.extend(f"     {line}" for line in pmset_log)
    out.append(
        f"   verdict: {_verdict(len(wakes) == 1)}"
        + ("" if len(wakes) == 1 else "  (dark wakes split the sleep; each one moves the last-wake baseline)")
    )

    out.append("== 3. Deadlines freeze with the machine (the 14-minute 'Reconnecting...' explanation) ==")
    sleep_started_wall = first_wake.wake_clocks["wall"] - first_wake.gaps["wall"]
    for timer in timers:
        if timer.fired is None:
            out.append(f"   {timer.name}: has not fired yet ({timer.seconds}s wait)")
            continue
        wall_elapsed = timer.fired["wall"] - timer.started["wall"]
        monotonic_elapsed = timer.fired["py_monotonic"] - timer.started["py_monotonic"]
        # A frozen deadline runs its full budget on the monotonic clock; one that
        # fired at the wake comes up short there by the length of the sleep.
        frozen = (
            monotonic_elapsed >= timer.seconds - 1.0
            and wall_elapsed - monotonic_elapsed >= SUSPENSION_THRESHOLD_SECONDS
        )
        # Only a deadline that came due while the machine was stopped can tell the
        # two labels apart; one that came due before it stopped simply ran.
        deadline_wall = timer.started["wall"] + timer.seconds
        expired_in_sleep = sleep_started_wall < deadline_wall < first_wake.wake_clocks["wall"]
        if not expired_in_sleep:
            label = "did not span the sleep; inconclusive (increase --timer-seconds or sleep sooner)"
        else:
            label = "froze during sleep (resumed with its budget)" if frozen else "fired at wake (wall-clock deadline)"
        out.append(f"   {timer.name}: asked {timer.seconds:.0f}s, took {wall_elapsed:.1f}s wall -> {label}")

    out.append("== 4. A sleep kills the SSH connection, and nothing above the socket notices promptly ==")
    # Whether this peer reaps a silent client is read from the keepalives it sent
    # before the sleep; the config file and process chain are context only.
    interval = keepalives.measured_interval_seconds(blocked.name, before_wall=sleep_started_wall)
    pre_sleep_seconds = sleep_started_wall - blocked.established["wall"]
    if interval is None:
        out.append(
            f"   peer ClientAliveInterval: no keepalive request from the peer in the {pre_sleep_seconds:.0f}s before "
            f"the sleep. A peer with ClientAliveInterval N sends one every N seconds, so this rules out only an "
            f"interval under {pre_sleep_seconds:.0f}s; read the process line below for the flags it was started with"
        )
    else:
        out.append(
            f"   peer ClientAliveInterval: ~{interval:.0f}s, measured from the keepalive requests the peer's sshd sent "
            "before the sleep"
        )
    for line in remote_context["sshd_config_file"].splitlines():
        out.append(f"   peer sshd config file (sshd -T; blind to -o flags): {line}")
    for line in remote_context["sshd_process_chain"].splitlines():
        out.append(f"   peer sshd process: {line}")
    out.append(
        f"   unwatched blocked read: {'released ' + _seconds_after(blocked.read_released['clocks'], first_wake.wake_clocks) + ' with ' + blocked.read_released['outcome'] if blocked.read_released else 'still blocked at report time'}"
    )
    out.append(
        f"   unwatched is_active() went False: {_seconds_after(blocked.inactive_seen_at, first_wake.wake_clocks) if blocked.inactive_seen_at else 'never (still reports active)'}"
    )
    if raw_tcp.released:
        out.append(
            f"   raw TCP recv: released {_seconds_after(raw_tcp.released['clocks'], first_wake.wake_clocks)} with {raw_tcp.released['outcome']}"
        )
    else:
        out.append("   raw TCP recv: still blocked at report time")
    out.append(
        "     (the raw socket never authenticates, so the peer's LoginGraceTime closes it whatever the laptop did; "
        "its release times when a peer close queued during the sleep surfaces, and is not evidence the sleep killed anything)"
    )
    # Zero bytes is EOF. Asked of the authenticated read alone: the raw socket
    # is closed by LoginGraceTime regardless.
    connection_died = (
        not blocked.read_released["outcome"].startswith("recv returned 1 bytes")
        if blocked.read_released is not None
        else None
    )
    out.append(f"   verdict (connection died): {_verdict(connection_died)}")
    if blocked.read_released is None and black_hole is not None:
        out.append(
            "   !! the authenticated read was still blocked at report time: with the peer's reset dropped, the only "
            "release is the kernel giving up on retransmission, which took longer than the post-wake wait. This is "
            "the case the watchdog exists for; compare section 5"
        )
    elif blocked.read_released is None:
        out.append(
            "   !! the authenticated read was not released before the report: the connection may have survived the "
            "sleep, or this peer does not reap a silent client and the kernel timeout is longer than the post-wake "
            "wait. Re-run against a sshd with ClientAlive* set (see above) to settle it"
        )
    elif black_hole is not None:
        out.append(
            "   -> released with the peer's reset dropped, so this is the kernel's own retransmission bound on a "
            "connection nothing answers: the slow case in the watchdog's docstring, measured"
        )

    out.append("== 5. The watchdog's transport.close() releases a blocked read (mngr SuspensionWatchdog) ==")
    if watched.watchdog_closed_at is None:
        out.append("   watchdog never detected a suspension (see section 1)")
        out.append("   verdict: UNMEASURED")
    else:
        out.append(
            f"   watchdog closed the transport {_seconds_after(watched.watchdog_closed_at, first_wake.wake_clocks)}"
        )
        if watched.read_released is None:
            out.append("   blocked read NOT released by the close (still blocked at report time)")
            out.append("   verdict: DOES NOT HOLD")
        else:
            latency = watched.read_released["clocks"]["wall"] - watched.watchdog_closed_at["wall"]
            out.append(
                f"   blocked read released {latency:.2f}s after the close with {watched.read_released['outcome']}"
            )
            out.append(f"   verdict: {_verdict(latency < 5)}")

    out.append("== 6. A cached idle connection stalls on open after a wake (forward's 30s channel-open bound) ==")
    if idle.result is None:
        out.append("   not measured (no wake, or the open is still running)")
    else:
        out.append(
            f"   open_session {idle.delay_after_wake_seconds:.0f}s after wake: {idle.result['outcome']} in {idle.result['seconds']:.1f}s"
        )
        if idle.result["outcome"].startswith("channel opened"):
            out.append(
                "   -> the pre-sleep connection was still usable; retiring it after this sleep would be a false positive (cost: one reconnect)"
            )
        else:
            out.append(
                f"   -> requests hitting the cached connection wait {idle.result['seconds']:.0f}s before it is retired; compare with the {STUCK_THRESHOLD_SECONDS:.0f}s stuck threshold"
            )
            if black_hole is not None:
                out.append(
                    "   -> with the peer's reset dropped this is what the forward's channel-open bound costs on its own; "
                    "the suspension check in the forward retires the connection before any request pays it"
                )

    out.append(f"== 7. The network is back within the {POST_WAKE_GRACE_SECONDS:.0f}s post-wake grace ==")
    for result in network.results:
        dns = "never" if result["dns"] is None else f"{result['dns']:.1f}s"
        tcp = "never" if result["tcp"] is None else f"{result['tcp']:.1f}s"
        out.append(
            f"   after the wake at {datetime.fromtimestamp(result['wake_wall']):%H:%M:%S}: DNS ok at {dns}, TCP connect ok at {tcp}"
        )
    if network.results:
        within = all(r["tcp"] is not None and r["tcp"] < POST_WAKE_GRACE_SECONDS for r in network.results)
        out.append(f"   verdict: {_verdict(within)}")
        out.append(
            f"   note: a failure run OPENS on the first post-wake request the forward reports a failure for, so "
            f"add the section 6 stall to the TCP time above for roughly when that is. That sum against "
            f"{POST_WAKE_SHADOW_SECONDS:.0f}s says whether the run earns the grace at all; the "
            f"{POST_WAKE_GRACE_SECONDS:.0f}s grace runs from that onset rather than from the wake, so what has to "
            f"fit inside it is the time from the onset until a probe works again"
        )
    else:
        out.append("   verdict: UNMEASURED")
    out.append(f"(last wake observed at {datetime.fromtimestamp(last_wake.wake_clocks['wall']):%H:%M:%S})")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "host",
        help=(
            "[user@]host[:port] (resolved through ~/.ssh/config), or the quoted 'ssh -i KEY -p PORT user@host' "
            "command from minds' recovery card"
        ),
    )
    parser.add_argument(
        "--sleep-minutes", type=float, default=4.0, help="how long you will keep the machine asleep (default 4)"
    )
    parser.add_argument(
        "--auto-sleep", action="store_true", help="schedule the wake with sudo pmset and sleep now (macOS)"
    )
    parser.add_argument(
        "--black-hole",
        action="store_true",
        help=(
            "drop the probe connections' packets with a pf rule (sudo pfctl, macOS) from before the sleep to the "
            "end of the run: the dead-NAT case, in which no reset from the peer ever arrives"
        ),
    )
    parser.add_argument(
        "--timer-seconds", type=float, default=150.0, help="timer probes' wait; must expire during the sleep"
    )
    parser.add_argument(
        "--pre-sleep-seconds",
        type=float,
        default=75.0,
        help="seconds to hold the connections open before the sleep, counting the peer's keepalives (two at 30s)",
    )
    parser.add_argument(
        "--post-wake-wait", type=float, default=240.0, help="seconds to keep observing after the wake before reporting"
    )
    parser.add_argument(
        "--idle-open-delay",
        type=float,
        default=2.0,
        help="seconds after the wake to try opening a channel on the idle connection",
    )
    parser.add_argument(
        "--wait-for-sleep", type=float, default=1800.0, help="give up if no sleep is observed within this many seconds"
    )
    parser.add_argument("--log-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    if args.black_hole and not IS_DARWIN:
        print("--black-hole is implemented with pf and only on macOS", flush=True)
        return 2

    clocks = Clocks()
    log_path = args.log_dir / f"sleep_wake_probe_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    log = EventLog(log_path, clocks)
    log.record("local_context", **local_context())
    keepalives = PeerKeepaliveObserver(log, clocks)
    keepalives.install()

    target = SSHTarget.resolve(args.host)
    if args.black_hole and target.proxy_command:
        print("--black-hole needs a direct TCP connection to the host, not a ProxyCommand", flush=True)
        return 2
    log.record(
        "ssh_target",
        hostname=target.hostname,
        port=target.port,
        username=target.username,
        proxy=bool(target.proxy_command),
    )
    context_client = target.connect("context")
    remote_context = _remote_context(context_client)
    log.record("remote_context", **remote_context)
    context_transport = context_client.get_transport()
    assert context_transport is not None
    peer_ip = str(context_transport.getpeername()[0])
    context_client.close()

    blocked = BlockedReadProbe(name="blocked_unwatched", log=log, clocks=clocks, target=target, watched=False)
    watched = BlockedReadProbe(name="blocked_watched", log=log, clocks=clocks, target=target, watched=True)
    idle = IdleThenOpenProbe(
        name="idle_cached", log=log, clocks=clocks, target=target, delay_after_wake_seconds=args.idle_open_delay
    )
    raw_tcp = RawTCPProbe(log=log, clocks=clocks, target=target)
    for probe in (blocked, watched, idle, raw_tcp):
        probe.start()

    black_hole: BlackHole | None = None
    if args.black_hole:
        raw_port = raw_tcp.local_port
        assert raw_port is not None
        black_hole = BlackHole(
            log=log,
            peer_ips=[peer_ip],
            peer_ports=[target.port],
            local_ports=[blocked.local_port, watched.local_port, idle.local_port, raw_port],
            deadman_seconds=args.pre_sleep_seconds
            + args.wait_for_sleep
            + args.post_wake_wait
            + _DEADMAN_MARGIN_SECONDS,
        )
        # Root is taken now, while someone is still here to type a password: the
        # rule goes in mid-run and comes out minutes after the wake, both of them
        # past sudo's five-minute cache, and the second is on the far side of a
        # suspension that would have expired it anyway.
        try:
            black_hole.start()
        except BlackHoleError as e:
            print(f"\n{e}", flush=True)
            return 2
    try:
        return _observe_and_report(
            args, log, log_path, blocked, watched, idle, raw_tcp, keepalives, black_hole, remote_context
        )
    finally:
        # The rule comes out before the sockets do, so teardown is not itself black-holed.
        if black_hole is not None and not black_hole.shutdown():
            print(
                f"\nWARNING: could not confirm the black hole came out. Check with "
                f"`{black_hole.manual_cleanup_command.replace('-F rules', '-s rules')}` and clear it with "
                f"`{black_hole.manual_cleanup_command}`.",
                flush=True,
            )
        for probe in (blocked, watched, idle, raw_tcp):
            probe.stop()


def _observe_and_report(
    args: argparse.Namespace,
    log: EventLog,
    log_path: Path,
    blocked: BlockedReadProbe,
    watched: BlockedReadProbe,
    idle: IdleThenOpenProbe,
    raw_tcp: RawTCPProbe,
    keepalives: PeerKeepaliveObserver,
    black_hole: BlackHole | None,
    remote_context: dict[str, str],
) -> int:
    clocks = log.clocks
    target = blocked.target
    network = NetworkReturnProbe(log=log, clocks=clocks, target=target)
    first_wake = threading.Event()

    def on_wake(wake: WakeObservation) -> None:
        first_wake.set()
        idle.on_wake(wake)
        network.on_wake(wake)

    heartbeat = HeartbeatMonitor(log=log, clocks=clocks, on_wake=on_wake)
    threading.Thread(target=heartbeat.run, name="heartbeat", daemon=True).start()

    timers = [TimerProbe(name=name, log=log, clocks=clocks, seconds=args.timer_seconds) for name in TIMER_WAITS]
    for timer in timers:
        threading.Thread(target=timer.run, args=(TIMER_WAITS[timer.name],), name=timer.name, daemon=True).start()

    print(
        f"\nREADY. Counting the peer's keepalives for {args.pre_sleep_seconds:.0f}s; do not sleep the laptop yet.\n",
        flush=True,
    )
    time.sleep(args.pre_sleep_seconds)
    if black_hole is not None:
        # After the window: the rule would drop the peer's keepalives too.
        black_hole.install()
    print(
        f"\nSLEEP NOW (within {args.timer_seconds - args.pre_sleep_seconds:.0f}s, so the timers expire while it "
        f"sleeps) and keep the laptop asleep for at least {args.sleep_minutes:.0f} minutes, then wake it and leave "
        "this running.\n",
        flush=True,
    )
    if args.auto_sleep:
        schedule_sleep(args.sleep_minutes, log)

    if not first_wake.wait(timeout=args.wait_for_sleep):
        print(f"no sleep observed; giving up (raw timeline: {log_path})", flush=True)
        return 1
    print(f"\nWake observed; observing for another {args.post_wake_wait:.0f}s before reporting.\n", flush=True)
    time.sleep(args.post_wake_wait)

    heartbeat.stop()
    pmset_log = pmset_log_since(log.started["wall"])
    report = build_report(
        heartbeat=heartbeat,
        timers=timers,
        blocked=blocked,
        watched=watched,
        idle=idle,
        raw_tcp=raw_tcp,
        network=network,
        keepalives=keepalives,
        black_hole=black_hole,
        reference_clock=Clocks.reference_name(),
        pmset_log=pmset_log,
        remote_context=remote_context,
    )
    log.record("report", lines=report)
    print("\n" + "\n".join(report))
    print(f"\nraw timeline: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
