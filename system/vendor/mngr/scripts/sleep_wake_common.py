"""Machinery shared by the sleep/wake scripts: clocks, a wake monitor, a black hole, a log.

Imported bare (``from sleep_wake_common import ...``) by ``sleep_wake_probe.py``
and ``sleep_wake_drill.py``, which run as ``uv run --script`` and so have this
directory on ``sys.path``. Tests reach it as ``scripts.sleep_wake_common``.

Nothing here opens a connection or measures a peer. The probe measures the SSH
substrate; the drill watches minds react to a staged outage; both need the same
answers to "did this machine just sleep", "what is on the wire", and "where did
that go in the timeline".
"""

import ctypes
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

# Mirrors imbue_common.suspension: how far the wall clock must outrun the
# monotonic one before the gap counts as a suspension.
SUSPENSION_THRESHOLD_SECONDS: Final[float] = 5.0
# Mirrors minds' SleepTracker heartbeat-gap threshold.
HEARTBEAT_GAP_THRESHOLD_SECONDS: Final[float] = 30.0

IS_DARWIN: Final[bool] = platform.system() == "Darwin"
# time.CLOCK_BOOTTIME, which exists only on Linux, so the attribute cannot be named on macOS.
_LINUX_CLOCK_BOOTTIME: Final[int] = 7

# Nested under com.apple because that is the anchor point /etc/pf.conf evaluates;
# a top-level anchor loaded by name is never consulted.
_PF_ANCHOR: Final[str] = "com.apple/sleep-wake-probe"


# ---------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------


class _MachTimebase(ctypes.Structure):
    _fields_ = (("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32))


class Clocks:
    """Every clock whose behaviour across a sleep matters, sampled together."""

    def __init__(self) -> None:
        self._libc: Any = None
        self._mach_scale = 1.0
        if IS_DARWIN:
            self._libc = ctypes.CDLL(None)
            self._libc.mach_absolute_time.restype = ctypes.c_uint64
            self._libc.mach_continuous_time.restype = ctypes.c_uint64
            timebase = _MachTimebase()
            self._libc.mach_timebase_info(ctypes.byref(timebase))
            self._mach_scale = timebase.numer / timebase.denom / 1e9

    def sample(self) -> dict[str, float]:
        reading: dict[str, float] = {
            "wall": time.time(),
            "py_monotonic": time.monotonic(),
            "clock_monotonic": time.clock_gettime(time.CLOCK_MONOTONIC),
        }
        # sys.platform rather than IS_DARWIN, so a type checker resolving for
        # Linux knows the Darwin-only clock attributes are not reached there.
        if sys.platform == "darwin":
            reading["clock_uptime_raw"] = time.clock_gettime(time.CLOCK_UPTIME_RAW)
            reading["mach_absolute"] = self._libc.mach_absolute_time() * self._mach_scale
            reading["mach_continuous"] = self._libc.mach_continuous_time() * self._mach_scale
        else:
            reading["clock_boottime"] = time.clock_gettime(_LINUX_CLOCK_BOOTTIME)
        return reading

    @staticmethod
    def reference_name() -> str:
        """The clock that keeps counting through a sleep, for a true sleep duration."""
        return "mach_continuous" if IS_DARWIN else "clock_boottime"


def was_suspended_since(earlier: dict[str, float], now: dict[str, float]) -> bool:
    """The rule from imbue_common.suspension, on py_monotonic vs wall."""
    wall_elapsed = now["wall"] - earlier["wall"]
    monotonic_elapsed = now["py_monotonic"] - earlier["py_monotonic"]
    return wall_elapsed - monotonic_elapsed >= SUSPENSION_THRESHOLD_SECONDS


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


class EventLog:
    def __init__(self, path: Path, clocks: Clocks) -> None:
        self._path = path
        self.clocks = clocks
        self._lock = threading.Lock()
        self._file = path.open("a")
        self.started = clocks.sample()

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {"event": event, "clocks": self.clocks.sample(), **fields}
        line = json.dumps(entry, default=str)
        with self._lock:
            self._file.write(line + "\n")
            self._file.flush()
            wall = datetime.fromtimestamp(entry["clocks"]["wall"], tz=timezone.utc).astimezone()
            detail = " ".join(f"{k}={v}" for k, v in fields.items())
            print(f"[{wall:%H:%M:%S}] {event} {detail}", flush=True)
        return entry


# ---------------------------------------------------------------------------
# Wake detection
# ---------------------------------------------------------------------------


@dataclass
class WakeObservation:
    wake_clocks: dict[str, float]
    gaps: dict[str, float]


@dataclass
class HeartbeatMonitor:
    """Ticks once a second and records every gap the way minds' SleepTracker would.

    Records *every* gap over the suspension threshold, not just those over the
    heartbeat threshold, so a sleep broken into dark wakes shows up as the
    several short gaps it is rather than the one long one the code assumes.
    """

    log: EventLog
    clocks: Clocks
    on_wake: Callable[[WakeObservation], None]
    wakes: list[WakeObservation] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)

    def run(self) -> None:
        previous = self.clocks.sample()
        while not self._stop.wait(1.0):
            current = self.clocks.sample()
            gaps = {name: current[name] - previous[name] for name in current}
            previous = current
            if gaps["wall"] < SUSPENSION_THRESHOLD_SECONDS:
                continue
            observation = WakeObservation(wake_clocks=current, gaps=gaps)
            self.wakes.append(observation)
            self.log.record(
                "heartbeat_gap",
                wall_gap=round(gaps["wall"], 2),
                py_monotonic_gap=round(gaps["py_monotonic"], 2),
                all_gaps={k: round(v, 2) for k, v in gaps.items()},
                counts_as_sleep_for_sleep_tracker=gaps["wall"] >= HEARTBEAT_GAP_THRESHOLD_SECONDS,
                suspension_rule_fires=was_suspended_since(
                    {
                        "wall": current["wall"] - gaps["wall"],
                        "py_monotonic": current["py_monotonic"] - gaps["py_monotonic"],
                    },
                    current,
                ),
            )
            self.on_wake(observation)

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Local machine
# ---------------------------------------------------------------------------


def run_local(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=30, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"<{type(e).__name__}: {e}>"


def local_context() -> dict[str, Any]:
    context: dict[str, Any] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
        "py_monotonic_impl": time.get_clock_info("monotonic").implementation,
    }
    if IS_DARWIN:
        context["cpu"] = run_local(["sysctl", "-n", "machdep.cpu.brand_string"])
        pmset = run_local(["pmset", "-g"])
        context["pmset"] = {
            line.split()[0]: line.split()[1]
            for line in pmset.splitlines()
            if len(line.split()) >= 2
            and line.split()[0] in {"tcpkeepalive", "powernap", "standby", "sleep", "hibernatemode", "womp"}
        }
    return context


def pmset_log_since(started_wall: float) -> list[str]:
    if not IS_DARWIN:
        return []
    lines = run_local(["pmset", "-g", "log"]).splitlines()
    since = datetime.fromtimestamp(started_wall).strftime("%Y-%m-%d %H:%M:%S")
    return [line for line in lines if line[:19] >= since and any(k in line for k in ("Sleep", "Wake", "DarkWake"))]


def prevent_idle_sleep(log: EventLog) -> subprocess.Popen[bytes] | None:
    """Hold an idle-sleep assertion for as long as this process lives; None where there is nothing to hold.

    A run measures the minutes after a wake, and a laptop whose display has
    gone dark idles back to sleep partway through them: one run lost its
    post-wake window that way three times over, each re-sleep restarting every
    countdown and moving the wake the report was timed from. The assertion is
    against *idle* sleep only, so a sleep the run itself asks for goes ahead.
    """
    if not IS_DARWIN:
        return None
    process = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    log.record("idle_sleep_prevented", caffeinate_pid=process.pid)
    return process


def schedule_sleep(sleep_minutes: float, log: EventLog) -> None:
    if not IS_DARWIN:
        print("--auto-sleep is only implemented for macOS; put the machine to sleep yourself", flush=True)
        return
    wake_in = int(sleep_minutes * 60)
    scheduled = subprocess.run(["sudo", "pmset", "relative", "wake", str(wake_in)], check=False)
    log.record("auto_sleep_scheduled_wake", seconds=wake_in, pmset_exit=scheduled.returncode)
    if scheduled.returncode != 0:
        print("could not schedule the wake (sudo pmset failed); sleep the machine yourself", flush=True)
        return
    time.sleep(2)
    subprocess.run(["pmset", "sleepnow"], check=False)


# ---------------------------------------------------------------------------
# Black hole: the dead-NAT case, on demand
# ---------------------------------------------------------------------------


def sudo(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sudo", *args], capture_output=True, text=True, check=False)


def can_connect(host: str, port: int, timeout_seconds: float) -> bool:
    """Whether a TCP connection to ``host:port`` completes within ``timeout_seconds``."""
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


@dataclass(frozen=True)
class EstablishedConnection:
    """One TCP connection this machine has open to a peer, as netstat reports it."""

    local_port: int
    peer_ip: str
    peer_port: int
    process: str


# ``netstat -anv -p tcp``: proto, recv-q, send-q, local, foreign, state, then
# counters, then ``process:pid``. Positional because the header's column names
# contain spaces ("Local Address") and cannot be split into indexes.
_NETSTAT_STATE_COLUMN: Final[int] = 5
_NETSTAT_PROCESS_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\S+:\d+$")


def parse_established_connections(netstat_output: str, peer_ips: set[str]) -> list[EstablishedConnection]:
    """Every ESTABLISHED TCP connection in ``netstat -anv -p tcp`` output whose peer is one of ``peer_ips``.

    Addresses come as ``ip.port``, so the port is whatever follows the last dot;
    that also reads the port off a truncated IPv6 address, though a peer given
    as IPv4 will only ever match an untruncated one.
    """
    connections: list[EstablishedConnection] = []
    for line in netstat_output.splitlines():
        columns = line.split()
        if len(columns) <= _NETSTAT_STATE_COLUMN or not columns[0].startswith("tcp"):
            continue
        if columns[_NETSTAT_STATE_COLUMN] != "ESTABLISHED":
            continue
        local_ip, _, local_port = columns[3].rpartition(".")
        peer_ip, _, peer_port = columns[4].rpartition(".")
        if peer_ip not in peer_ips:
            continue
        process = next((c for c in columns[_NETSTAT_STATE_COLUMN + 1 :] if _NETSTAT_PROCESS_PATTERN.match(c)), "?")
        connections.append(
            EstablishedConnection(
                local_port=int(local_port), peer_ip=peer_ip, peer_port=int(peer_port), process=process
            )
        )
    return connections


def list_established_connections(peer_ips: set[str]) -> list[EstablishedConnection]:
    """The connections this machine has open to ``peer_ips`` right now, from the kernel's table."""
    output = subprocess.run(["netstat", "-anv", "-p", "tcp"], capture_output=True, text=True, check=True, timeout=30)
    return parse_established_connections(output.stdout, peer_ips)


class CanaryPort:
    """A local port the drill holds so it can prove a port-scoped rule bites, and later that it is gone.

    A rule scoped to named local ports cannot be checked by connecting to the
    peer: a fresh socket gets a fresh port and goes straight through, which is
    the very property being staged. So one port is held from the start, named in
    the rule beside the connections being killed, and connected *from*: under
    the rule that connect has to hang, and after the rule comes out it has to
    succeed. Only the socket holding a port can connect from it, so each check
    spends the holder and binds a new one to the same port straight after.
    """

    def __init__(self) -> None:
        self._holder = self._bind(0)
        self.port: int = int(self._holder.getsockname()[1])

    @staticmethod
    def _bind(port: int) -> socket.socket:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("0.0.0.0", port))
        return holder

    def can_connect(self, host: str, port: int, timeout_seconds: float) -> bool:
        """Whether a connection from the held port to ``host:port`` completes within ``timeout_seconds``."""
        holder = self._holder
        try:
            holder.settimeout(timeout_seconds)
            holder.connect((host, port))
            return True
        except OSError:
            return False
        finally:
            holder.close()
            self._holder = self._bind(self.port)

    def close(self) -> None:
        self._holder.close()


# The root half of the black hole: run once under ``sudo`` and left running for
# the length of the drill. Its whole reason to exist is that a run sleeps the
# laptop. sudo's credential cache is five minutes and a refresher thread is
# frozen while the machine is suspended, so any sleep worth staging expires it,
# and the next ``sudo pfctl`` blocks on a password prompt with nobody there --
# one run lost twenty-six minutes to exactly that, and measured a post-wake
# window in which the network had never come back. Privileges a process already
# holds survive a suspension, so the process holds them and the drill signals it
# with files.
#
# It also owns the rule's lifetime, which is what makes the drill safe to kill:
# the deadman lifts the block even if nothing ever asks it to, so a run that
# crashes, is interrupted, or is killed cannot leave the laptop unable to reach
# the workspaces.
_BLACK_HOLE_HELPER_SCRIPT: Final[str] = r"""
set -u
anchor=$1
rules_path=$2
control=$3
deadman=$4
shift 4

state() { printf '%s' "$1" > "$control/state.tmp" && mv "$control/state.tmp" "$control/state"; }

enable_output=$(pfctl -E 2>&1)
token=$(printf '%s\n' "$enable_output" |
    awk 'match($0, /[Tt]oken *: *[0-9]+/) { t = substr($0, RSTART, RLENGTH); gsub(/[^0-9]/, "", t); print t; exit }')
if [ -z "$token" ]; then
    printf '%s' "$enable_output" > "$control/error"
    state failed
    exit 1
fi

release_anchor() {
    pfctl -a "$anchor" -F rules >/dev/null 2>&1
    pfctl -X "$token" >/dev/null 2>&1
}
trap 'release_anchor; state released' EXIT
trap 'exit 1' INT TERM HUP

state ready
deadline=$(( $(date +%s) + deadman ))

while :; do
    if [ -f "$control/install" ] && [ ! -f "$control/installed" ]; then
        if pfctl -a "$anchor" -f "$rules_path" 2>"$control/install_error"; then
            for peer in "$@"; do
                pfctl -k 0.0.0.0/0 -k "$peer" >/dev/null 2>&1
                pfctl -k "$peer" >/dev/null 2>&1
            done
            printf '0' > "$control/install_status"
        else
            printf '1' > "$control/install_status"
        fi
        : > "$control/installed"
    fi
    if [ -f "$control/release" ]; then
        if pfctl -a "$anchor" -F rules 2>"$control/release_error"; then
            printf '0' > "$control/release_status"
        else
            printf '1' > "$control/release_status"
        fi
        : > "$control/done"
        exit 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        # Flushed here rather than left to the exit trap, so the status a caller
        # reads describes a pfctl that has already run.
        if pfctl -a "$anchor" -F rules 2>"$control/release_error"; then
            printf '0' > "$control/release_status"
        else
            printf '1' > "$control/release_status"
        fi
        : > "$control/deadman_fired"
        : > "$control/done"
        exit 0
    fi
    sleep 0.2
done
"""

# How often each side looks for the other's signal. The post-wake scenario is
# what sets it: the rule has to be in place before the app's first request after
# the wake reaches the forward, which the incidents put at 24 to 30 seconds, so a
# request costing under half a second leaves that untouched.
_HELPER_POLL_SECONDS: Final[float] = 0.1


def _port_clause(ports: list[int] | None) -> str:
    return "" if ports is None else " port {{ {} }}".format(", ".join(str(port) for port in ports))


def build_black_hole_rules(peer_ips: list[str], peer_ports: list[int] | None, local_ports: list[int] | None) -> str:
    """The pf rules dropping traffic to and from every address in ``peer_ips``.

    Either side can be narrowed to named ports, or left as ``None`` to match
    every one. The probe narrows both, since it owns its sockets and wants
    everything else on the machine left alone. The drill narrows the local side
    to the connections it found open at the wake, so those hang the way a dead
    NAT mapping makes them hang while a fresh connection to the same address
    goes through -- or neither side, when the machine itself is meant to be
    unreachable. The peer side is left open in both of the drill's cases: the
    connections to one box span the container sshd and the VM sshd behind the
    same address.

    Several addresses because one drill run stages the same outage for every
    minds app on the machine at once, and each app's workspace lives behind its
    own box. Naming them in one anchor is what makes it *one* outage: they all
    start and end on the same packet, so the apps can be compared across a
    single sleep rather than across two runs of different networks.
    """
    peers = _port_clause(peer_ports)
    locals_ = _port_clause(local_ports)
    return "".join(
        f"block drop out quick proto tcp from any{locals_} to {peer_ip}{peers}\n"
        f"block drop in quick proto tcp from {peer_ip}{peers} to any{locals_}\n"
        for peer_ip in peer_ips
    )


class BlackHoleError(RuntimeError):
    """The block could not be put in place, or could not be proven to have come out."""


@dataclass
class BlackHole:
    """Drops every packet of the named connections, for as long as the run asks.

    The rule itself lives in a pf anchor, but every ``pfctl`` that touches it runs
    in a root helper started once at :meth:`start` -- see
    ``_BLACK_HOLE_HELPER_SCRIPT`` for why the privilege has to be held rather
    than re-acquired. pf is enabled with a reference token and released with it,
    so a machine that had pf on for something else keeps it on, and existing
    states for each peer are killed, since a state entry passes packets without
    consulting the rules.

    Both :meth:`install` and :meth:`remove` report what actually happened. A
    removal that only *asked* is the one failure that must never be reported as
    success: it leaves the machine unable to reach the workspaces, and a log
    saying the block came out is what stops anyone looking.
    """

    log: EventLog
    peer_ips: list[str]
    peer_ports: list[int] | None
    local_ports: list[int] | None
    # Bounds how long the rule can outlive a run that stopped asking. Sized by
    # the caller from its own longest path, since the helper cannot know it.
    deadman_seconds: float = 3600.0
    # How long to wait for the sudo password before giving up on the run.
    start_timeout_seconds: float = 300.0
    _control: Path | None = None
    _process: subprocess.Popen[bytes] | None = None
    _is_installed: bool = False

    def start(self) -> None:
        """Authenticate and leave the root helper running. Ask while someone is still at the keyboard.

        Separate from :meth:`install` because the two happen at different times:
        in the post-wake scenario the rule goes in seconds after a wake, long
        after any password could be typed.
        """
        if not IS_DARWIN:
            raise BlackHoleError("the black hole is implemented with pf and only on macOS")
        control = Path(tempfile.mkdtemp(prefix="sleep_wake_black_hole_"))
        (control / "helper.sh").write_text(_BLACK_HOLE_HELPER_SCRIPT)
        self._control = control
        self._process = subprocess.Popen(
            [
                "sudo",
                "sh",
                str(control / "helper.sh"),
                _PF_ANCHOR,
                str(control / "rules.conf"),
                str(control),
                str(int(self.deadman_seconds)),
                *self.peer_ips,
            ]
        )
        state = self._await_state({"ready", "failed"}, self.start_timeout_seconds)
        if state != "ready":
            detail = self._read("error") or f"the helper exited {self._process.poll()}"
            raise BlackHoleError(f"could not enable pf: {detail}")
        self.log.record("black_hole_helper_ready", control=str(control), deadman_seconds=self.deadman_seconds)

    def narrow_to_local_ports(self, local_ports: list[int]) -> None:
        """Drop only the connections on ``local_ports`` from here on, leaving the addresses reachable otherwise.

        Called before :meth:`install`, once the connections to kill are known:
        the drill learns them at the wake, long after the helper was started.
        """
        if self._is_installed:
            raise BlackHoleError("the black hole cannot be narrowed while its rule is installed")
        self.local_ports = sorted(local_ports)

    def install(self) -> None:
        """Put the rule in place, raising if the helper could not load it.

        The rules are written now rather than at :meth:`start`, so they say what
        the fields say at the moment the rule goes in.
        """
        control = self._require_control()
        (control / "rules.conf").write_text(build_black_hole_rules(self.peer_ips, self.peer_ports, self.local_ports))
        (control / "install").touch()
        if not self._await_file("installed", timeout_seconds=30.0):
            raise BlackHoleError("the black-hole helper never acknowledged the request to install the rule")
        status = self._read("install_status")
        if status != "0":
            raise BlackHoleError(f"could not load the black-hole rules: {self._read('install_error') or status}")
        self._is_installed = True
        rules = (control / "rules.conf").read_text()
        self.log.record(
            "black_hole_installed",
            peers=[f"{peer_ip}:{self.peer_ports}" for peer_ip in self.peer_ips],
            local_ports=self.local_ports,
            rules=rules,
        )

    def remove(self) -> bool:
        """Take the rule out, and answer whether the helper confirmed it did.

        False means the machine may still be black-holed, which the caller has to
        say out loud rather than record a removal. Idempotent, so the teardown
        path can call it after a normal removal.
        """
        if self._control is None or not self._is_installed:
            return True
        (self._control / "release").touch()
        if not self._await_file("done", timeout_seconds=30.0):
            self.log.record("black_hole_release_unacknowledged")
            return False
        status = self._read("release_status")
        if status != "0":
            # Deliberately still "installed": a caller that tries again must get
            # another attempt and another False, rather than a short-circuit
            # reporting success for a rule that is still in the anchor.
            self.log.record("black_hole_release_failed", status=status, error=self._read("release_error"))
            return False
        self._is_installed = False
        self.log.record("black_hole_removed", is_deadman=(self._control / "deadman_fired").exists())
        return True

    def shutdown(self) -> bool:
        """Stop the helper, having first asked it to take the rule out. Safe to call twice.

        Answers whether the rule is known to be gone, so a teardown that could
        not confirm it says so rather than leaving the machine quietly cut off.
        """
        if self._process is None:
            return True
        is_removed = self.remove()
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
        return is_removed

    @property
    def manual_cleanup_command(self) -> str:
        """What to run by hand if a run ever leaves the rule behind."""
        return f"sudo pfctl -a {_PF_ANCHOR} -F rules"

    def _require_control(self) -> Path:
        if self._control is None:
            raise BlackHoleError("the black hole was used before start() brought its root helper up")
        return self._control

    def _read(self, name: str) -> str | None:
        control = self._require_control()
        try:
            return (control / name).read_text().strip()
        except OSError:
            return None

    def _await_state(self, wanted: set[str], timeout_seconds: float) -> str | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            state = self._read("state")
            if state in wanted:
                return state
            if self._process is not None and self._process.poll() is not None:
                return self._read("state")
            time.sleep(_HELPER_POLL_SECONDS)
        return self._read("state")

    def _await_file(self, name: str, timeout_seconds: float) -> bool:
        control = self._require_control()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if (control / name).exists():
                return True
            time.sleep(_HELPER_POLL_SECONDS)
        return (control / name).exists()
