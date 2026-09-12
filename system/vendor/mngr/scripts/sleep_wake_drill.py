#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Stage the sleep/wake recovery incident against every running minds app, and read back what each did.

Where ``sleep_wake_probe.py`` measures the substrate (clocks, deadlines, what a
``transport.close()`` releases), this drives the assembled system: it black-holes
each app's workspace machine while the apps are running, waits for them to
convict those machines and dispatch unattended starts, sleeps the laptop across
those starts, and then watches what each app does with it on the wake.

    uv run --script scripts/sleep_wake_drill.py --auto-sleep

That is the whole invocation. Every running minds backend is found in the
process table, and its log file, data directory and mngr come from its own argv
-- so a laptop running a build under test beside a build from main is drilled as
one run, on one outage and one sleep, and the report ends in a table of what
each app did against that shared wake. Two runs could not answer the same
question: they would be two different networks and two different sleeps.

There are two scenarios, because a sleep does two different things to minds.

``--scenario post-wake-tunnel`` (the default) is the incident this branch is
named for. Nothing is staged before the sleep: both apps go under with healthy
machines and healthy tunnels. At the wake, every connection open to the
machines is killed by its local port and nothing else is, which is what the
incident's network did: the NAT had dropped the mappings for the connections
that predated the sleep, so packets on them vanished without a reply, while a
fresh connection to the same address went straight through. The machines
answer throughout, so a machine called stuck was called stuck wrongly, and an
unattended start dispatched for it restarted a machine that was running.
Blocking the whole address instead stages a different outage -- a machine
unreachable for as long as the block lasts -- which both builds are right to
convict, and which cannot tell a tunnel rebuilt before use from one handed back
half-open. The block goes in on the first gap rather than the settled wake,
since the app's first request after a wake reaches the forward within seconds
and it is that request hanging -- rather than being refused -- that produces
the failure run under test. It stays in for the rest of the run: the
connections it names never came back in the incident either, and macOS hands
out local ports in sequence, so a fresh connection cannot land on one of them.

``--scenario recovery-across-sleep`` is the other half: the machines are taken
off the network first, the apps are left to convict them and dispatch restarts,
and the laptop sleeps across those restarts. There the conviction is correct and
what is under test is what the sleep does to a ``mngr start`` whose every
deadline is measured on a clock the sleep stops.

What is taken off the network is a *machine*, not an agent: every agent on a
host shares one address, so blocking any of them blocks all of them, and the
markers below are matched for any agent on it. Which agent the app enrols,
convicts and recovers is not knowable in advance -- on a plain minds laptop the
machine has one agent, minds treats it as the workspace, and it is called
``system-services``.

Root is taken once, at the start, and held by a helper process for the length of
the run. Nothing here can rely on sudo's credential cache: it lasts five minutes,
a refresher is frozen while the laptop is suspended, and the one pfctl that
matters runs on the far side of the sleep. That helper also owns the rule's
lifetime and lifts it on a deadman, so a run that crashes or is killed cannot
leave the laptop unable to reach the workspaces. Every removal is confirmed by
reconnecting rather than by pfctl's exit code.

Run this on a spare machine with spare workspaces: it blocks every packet to
those machines' addresses for the length of the run, and it sleeps the laptop.
``--dry-run`` resolves everything and checks each machine is reachable without
installing a rule or sleeping. ``--data-dir`` restricts the run to named apps,
and ``--workspace`` picks a machine for an app running more than one -- prefix
it to address one app (``--workspace .minds-staging=other-box``), since the apps
being compared do not share machines. None of them is needed for the ordinary
case of one machine per app.

The outage stays scoped to those addresses rather than cutting the whole network,
and that is not a convenience. Minds withholds an unattended start when it reads
the *device* as offline -- the reading is "does any of github.com, gitlab.com,
bitbucket.org answer on 443" -- and records the start as merely owed. Blocking
everything would trip that gate, so nothing would be in flight for the sleep to
land in and the run would measure the offline path instead of this one.

Two things have to be true of each app or its column measures nothing. It must be
running the code you meant to test, and a window must be open on its workspace
for the whole run: the health probe loop only polls agents that a failed request
has enrolled as suspect, and those requests come from a page loading the
workspace. A workspace nobody is looking at is never probed, never convicted,
and never recovered.

What is observed comes from the apps' own logs, so the drill needs no session
cookie and asserts on the same records a bug report would carry. The sequence it
expects of each app, in order:

    HEALTHY -> STUCK -> unattended START dispatched -> [sleep] -> the wake hands
    the in-flight START back to the probe loop -> a probe finds the machine
    answering -> if the start then errors, that failure is declined rather than
    shown as a recovery-failed card.

In the post-wake scenario the expected sequence is that none of it happens. A
build that retires its tunnels at the wake never logs a failure at all; one that
hands the old tunnel back logs failures for as long as a channel open takes to
give up. Neither may convict, and the drill's own checks -- connections found
open at the wake, a held port that hangs under the rule, a fresh connect that
answers -- are what say the outage was real, since the app's silence cannot.

Black-holing is not optional and there is no flag for skipping it: with the
tunnel reset that a wake triggers, the first post-wake open fails in
hundredths of a second, so a plain lid-close exercises none of this.

On battery, expect several sleep gaps rather than one: macOS dark-wakes partway
through, and each of those ends the sleep interval as far as the app is
concerned. That is the harder and more realistic condition -- it is what the
overnight incident actually had -- but a first run on AC gives one wake edge and
a timeline that reads straight through. The drill waits ``--wake-settle`` awake
seconds past the last gap before calling it the wake, so it does not lift the
block during a dark wake. It also holds an idle-sleep assertion for the run: a
laptop whose display has gone dark idles back to sleep a couple of minutes after
the wake, and every re-sleep restarts the countdowns and moves the wake the
report is timed from. The assertion is against idle sleep only, so the sleep
the run asks for goes ahead.

The drill lifts the block at that wake, which is what the incident's own network
did -- the laptop woke onto a working network and only the old connections were
dead. mngr's ``SuspensionWatchdog`` then closes the start's suspension-outlived
transport within about five seconds and its retry reconnects, so the *expected*
ending is a start that completes rather than one that fails. That makes the
declined-failure step conditional, and the report says so rather than calling an
unexercised path a pass.

Everything observed is also appended to a JSONL file (path printed at the end).
"""

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
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

from sleep_wake_common import BlackHole
from sleep_wake_common import BlackHoleError
from sleep_wake_common import CanaryPort
from sleep_wake_common import Clocks
from sleep_wake_common import EstablishedConnection
from sleep_wake_common import EventLog
from sleep_wake_common import HeartbeatMonitor
from sleep_wake_common import IS_DARWIN
from sleep_wake_common import WakeObservation
from sleep_wake_common import can_connect
from sleep_wake_common import list_established_connections
from sleep_wake_common import local_context
from sleep_wake_common import pmset_log_since
from sleep_wake_common import prevent_idle_sleep
from sleep_wake_common import schedule_sleep

# Mirrors minds' backend_resolver: the label carrying a workspace's display name,
# and the name of the agent a host recovery actually addresses.
_WORKSPACE_DISPLAY_NAME_LABEL: Final[str] = "workspace_display_name"
_SYSTEM_SERVICES_AGENT_NAME: Final[str] = "system-services"
# The one host state that has a workspace running on it to interrupt.
_RUNNING_HOST_STATE: Final[str] = "RUNNING"

# The app's own prefix for tmux session names; running mngr against its host dir
# without this addresses different sessions and can start a duplicate agent.
_DEFAULT_MNGR_PREFIX: Final[str] = "minds-"

# How often the log follower looks for new records. Well under the 2s health
# probe interval, so the ordering the drill reports is the app's, not the
# follower's.
_FOLLOW_INTERVAL_SECONDS: Final[float] = 0.25

# Slack on top of a run's own longest path, before the black hole's deadman lifts
# the rule on its own. Generous, since firing it early would end a run that was
# still going; it is a backstop against an abandoned rule, not a schedule.
_DEADMAN_MARGIN_SECONDS: Final[float] = 900.0

# How long to keep asking for the machine after the rule comes out. Covers the
# tunnel rebuild and the machine's own reaction, and is what turns "pfctl said
# it worked" into evidence that packets are flowing again.
_RELEASE_CONFIRM_SECONDS: Final[float] = 60.0

# The two things a laptop sleep does to minds, staged one at a time.
_SCENARIO_POST_WAKE: Final[str] = "post-wake-tunnel"
_SCENARIO_RECOVERY: Final[str] = "recovery-across-sleep"


# ---------------------------------------------------------------------------
# Finding the running apps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MindsApp:
    """One running minds backend, and the three paths that belong to it.

    Everything here is read off the process itself rather than assumed, because
    the point of a run is comparing two apps and they share none of it: a build
    under test and a build from main keep separate data directories, separate
    logs and separate mngr installs, and defaults that suit one are wrong for
    the other.
    """

    pid: int
    data_dir: Path
    log_file: Path
    host_dir: Path
    mngr_command: list[str]

    @property
    def label(self) -> str:
        """How this app is named in output: its data directory, which is what distinguishes it."""
        return self.data_dir.name


_PID_AND_COMMAND_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s*(\d+)\s+(.*)$")

# -ww so the argv is not truncated to the terminal width: --log-file is what the
# whole discovery hangs on, and it sits well past the first 80 columns.
_PS_ARGV: Final[tuple[str, ...]] = ("ps", "-Awwo", "pid=,command=")


def parse_minds_apps(ps_output: str) -> list[MindsApp]:
    """Every running minds backend in ``ps_output``, as the paths the drill needs.

    Discovered rather than configured, since the alternative is naming a data
    dir, a log file and an mngr install per app on the command line -- six flags
    to compare two apps, each of which silently produces a meaningless run if it
    is wrong.

    The ``minds`` entry point is looked for anywhere in the argv rather than at
    the front, because that is not where it is: a console script is exec'd
    through its interpreter, so the running app reads ``<venv>/bin/python3
    <venv>/bin/minds ... run ...`` and matching the first token would find
    nothing at all. It is the ``minds`` token, not the interpreter, that locates
    the app: mngr is its sibling, which is the app's *own* mngr for an installed
    app and the checkout's for a dev app.

    It has to be a *path* to that entry point. Each app also has a launcher
    process above the backend -- ``uv run --project <bundle> minds ... run ...``
    for the packaged app, ``uv run --package minds minds ... run ...`` for a dev
    one -- carrying the same ``run`` and the same ``--log-file``. Taking those
    too would put every app in the run twice, and their bare ``minds`` argument
    has no virtualenv around it, so the mngr "beside" it is whatever happens to
    be on PATH: another app's, on the machine this is for.

    The log file is the one the app was told to write (``--log-file``), so an app
    logging somewhere unusual is still followed, and the data dir is that log's
    grandparent (``<data-dir>/logs/minds-events.jsonl``). Both ``run`` and
    ``--log-file`` are required after the entry point: without them this is not
    the backend writing the log the drill reads, and adopting it would mean
    watching a file nothing writes and reporting the app as having ignored the
    outage.
    """
    apps: list[MindsApp] = []
    for line in ps_output.splitlines():
        match = _PID_AND_COMMAND_PATTERN.match(line)
        if match is None:
            continue
        try:
            argv = shlex.split(match.group(2))
        # A command line this cannot tokenise is some other process's quoting,
        # not a minds app: they are spawned with plain absolute paths.
        except ValueError:
            continue
        entry_point = next(
            (index for index, arg in enumerate(argv) if Path(arg).name == "minds" and Path(arg).parent != Path(".")),
            None,
        )
        if entry_point is None:
            continue
        rest = argv[entry_point + 1 :]
        if "run" not in rest or "--log-file" not in rest:
            continue
        log_file = Path(rest[rest.index("--log-file") + 1])
        data_dir = log_file.parent.parent
        apps.append(
            MindsApp(
                pid=int(match.group(1)),
                data_dir=data_dir,
                log_file=log_file,
                host_dir=data_dir / "mngr",
                mngr_command=[str(Path(argv[entry_point]).parent / "mngr")],
            )
        )
    return sorted(apps, key=lambda app: app.label)


# ---------------------------------------------------------------------------
# Resolving the machine to take off the network
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostTarget:
    """The one machine this run stages an outage for, and every agent minds might name it by.

    A host rather than an agent, because a host is what an outage happens to:
    every agent on it shares one address, so blocking any of them blocks all of
    them. Choosing an agent instead was a choice the drill had no way to make --
    the agent minds treats as the workspace is the machine's only agent on a
    plain minds laptop, and it is called ``system-services``, which a rule
    written for hosts running several chats went out of its way to exclude.

    ``agent_ids`` is every agent on the machine, and the log markers match any of
    them, so the run does not have to know in advance which one the app enrolls,
    convicts and recovers. That also covers an agent discovery has not yet
    named, whose name comes back as its own id.
    """

    host_id: str
    host_name: str
    display_name: str
    ssh_host: str
    ssh_port: int
    agent_ids: frozenset[str]
    services_agent_id: str


class DrillError(Exception):
    """A condition that makes the run meaningless, raised before anything is installed."""


def _agent_display_name(agent: dict[str, Any]) -> str:
    labels = agent.get("labels") or {}
    host = agent.get("host") or {}
    return str(labels.get(_WORKSPACE_DISPLAY_NAME_LABEL) or host.get("name") or agent.get("id") or "")


def _coordinates(agent: dict[str, Any]) -> tuple[str, ...]:
    """Every handle that names this agent's machine: agent id, agent name, display name, host name."""
    host = agent.get("host") or {}
    return (
        str(agent.get("id") or ""),
        str(agent.get("name") or ""),
        _agent_display_name(agent),
        str(host.get("name") or ""),
    )


def _hosts_by_id(agents: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for agent in agents:
        host_id = str((agent.get("host") or {}).get("id") or "")
        if host_id:
            grouped.setdefault(host_id, []).append(agent)
    return grouped


def _describe(host_id: str, on_host: list[dict[str, Any]]) -> str:
    return f"{_agent_display_name(on_host[0]) or host_id} ({host_id})"


def workspace_query_for(specs: list[str] | None, app_label: str) -> str | None:
    """The ``--workspace`` value that applies to ``app_label``, or None to take its only machine.

    A value may name the app it is for (``.minds-staging=other-box``) or stand
    bare, in which case it applies to every app that has no value of its own.
    Per-app because the apps being compared do not share machines: a build under
    test and a build from main each keep their own workspace, so one query
    cannot resolve in both -- and a query that matched nothing would otherwise
    read as the machine being gone.
    """
    if not specs:
        return None
    for spec in specs:
        label, separator, query = spec.partition("=")
        if separator and label == app_label:
            return query
    bare = [spec for spec in specs if "=" not in spec]
    if len(bare) > 1:
        raise DrillError(
            f"several --workspace values are not addressed to an app ({', '.join(bare)}), so there is no telling "
            f"which one is meant for {app_label}. Prefix them, as --workspace {app_label}=<name>"
        )
    return bare[0] if bare else None


def select_host(agents: list[dict[str, Any]], query: str | None) -> HostTarget:
    """Resolve ``query`` to exactly one running machine, or take the only one when it is ``None``.

    ``query`` may be any handle a user would reach for -- an agent id, an agent
    name, a workspace display name, or a host name -- and names the machine that
    agent lives on. Matching is exact first and case-insensitive second. Anything
    that matches zero or several machines raises rather than guessing: this run
    is about to take whatever it picks off the network.

    ``None`` takes the app's only running machine. An app with one is the
    ordinary case for a laptop kept for drills, and naming it buys nothing --
    while a name has to be supplied once per app, which is what makes comparing
    two apps tedious enough to skip.
    """
    running = {
        host_id: on_host
        for host_id, on_host in _hosts_by_id(agents).items()
        # An outage can only be staged on a machine that is up and reachable. A
        # stale record whose address something else now answers on passes the
        # reachability check and still has no live tunnel, no open window and no
        # traffic to interrupt -- which reads afterwards as the app ignoring an
        # outage rather than as there having been nothing to ignore.
        if (on_host[0].get("host") or {}).get("ssh")
        and str((on_host[0].get("host") or {}).get("state") or "UNKNOWN") == _RUNNING_HOST_STATE
    }
    if not running:
        raise DrillError(
            "this app has no running remote machine to drill. The drill needs one that is up and reachable over "
            "SSH; start a workspace, let discovery report it, and re-run."
        )
    if query is None:
        if len(running) > 1:
            several = ", ".join(sorted(_describe(host_id, on_host) for host_id, on_host in running.items()))
            raise DrillError(
                f"this app has several running machines ({several}); pass --workspace to say which one to take off "
                "the network"
            )
        host_id, on_host = next(iter(running.items()))
        return _target_from(host_id, on_host)
    exact = {
        host_id: on_host
        for host_id, on_host in running.items()
        if any(query in _coordinates(agent) for agent in on_host)
    }
    folded = {
        host_id: on_host
        for host_id, on_host in running.items()
        if any(query.casefold() in {value.casefold() for value in _coordinates(agent)} for agent in on_host)
    }
    matches = exact or folded
    if not matches:
        known = sorted(_describe(host_id, on_host) for host_id, on_host in running.items())
        raise DrillError(f"no running machine matches {query!r}. Running machines: {', '.join(known)}")
    if len(matches) > 1:
        ambiguous = ", ".join(sorted(_describe(host_id, on_host) for host_id, on_host in matches.items()))
        raise DrillError(f"{query!r} matches several machines ({ambiguous}); name one by host name or agent id")
    host_id, on_host = next(iter(matches.items()))
    return _target_from(host_id, on_host)


def _target_from(host_id: str, on_host: list[dict[str, Any]]) -> HostTarget:
    """``on_host`` as a target, or raise saying why that machine cannot be drilled."""
    host = on_host[0].get("host") or {}
    ssh = host["ssh"]
    services = [agent for agent in on_host if str(agent.get("name") or "") == _SYSTEM_SERVICES_AGENT_NAME]
    if not services:
        # The recovery under test dispatches `mngr start` against the
        # system-services agent, and reports "Could not locate the
        # system-services agent for this machine" when it cannot find one. So a
        # host without one is a host whose recovery fails before it starts,
        # whatever this drill stages for it. On a machine whose agents discovery
        # has not named yet, every name comes back as an id and this is what
        # says so.
        found = ", ".join(sorted(f"{agent.get('name')} ({agent.get('id')})" for agent in on_host))
        raise DrillError(
            f"no system-services agent on {host.get('name') or host_id}, so the recovery this drill stages could not "
            f"run even if it fired. Agents discovered there: {found}. If those are bare ids, discovery has not named "
            "them yet -- let the app finish and re-run."
        )
    return HostTarget(
        host_id=host_id,
        host_name=str(host.get("name") or ""),
        display_name=_agent_display_name(on_host[0]),
        ssh_host=str(ssh["host"]),
        ssh_port=int(ssh["port"]),
        agent_ids=frozenset(str(agent.get("id")) for agent in on_host),
        services_agent_id=str(services[0].get("id")),
    )


def resolve_app_mngr(app: MindsApp, path_lookup: Callable[[str], str | None]) -> list[str]:
    """The argv prefix for running ``app``'s own mngr, or raise naming what was tried.

    The app's own is the one beside the ``minds`` it is running: the same
    version, reading the app's host dir the way the app reads it. That holds for
    an installed app and for one run out of a checkout's virtualenv alike, which
    is why nothing has to be passed for either. Anything on PATH is a fallback,
    and on a machine running two apps it is the wrong mngr for at least one of
    them -- so it is used only when an app ships none of its own.
    """
    bundled = Path(app.mngr_command[0])
    if bundled.exists():
        return [str(bundled)]
    on_path = path_lookup("mngr")
    if on_path is not None:
        return [on_path]
    raise DrillError(
        f"no mngr for the app at {app.data_dir}: {bundled} does not exist and 'mngr' is not on PATH. That app is "
        "running a `minds` whose virtualenv has no `mngr` beside it, which should not happen for either an "
        "installed app or a checkout."
    )


def _list_agents(mngr_command: list[str], host_dir: Path, mngr_prefix: str) -> list[dict[str, Any]]:
    env = dict(os.environ, MNGR_HOST_DIR=str(host_dir), MNGR_PREFIX=mngr_prefix)
    argv = [*mngr_command, "list", "--format", "json"]
    # From the home directory, because mngr layers in the project config of
    # whatever directory it is run from. A checkout's .mngr/settings.toml
    # declares providers whose backends an app-bundled mngr does not ship, and
    # an enabled block for a missing backend is a hard parse error -- so running
    # this from a checkout reads a config the app never reads and fails on it.
    # The app runs its own mngr from home for the same reason.
    result = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=Path.home(), check=False, timeout=300)
    # The exit code is non-zero when any provider errored, with the surviving
    # providers' agents still on stdout, so parse first and only complain if
    # there is nothing to parse.
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise DrillError(
            f"could not read `{shlex.join(argv)}` against {host_dir} ({e}); "
            f"exit {result.returncode}, stderr: {result.stderr.strip()[-500:]}"
        ) from e
    return list(data.get("agents", []))


# ---------------------------------------------------------------------------
# Watching minds' log
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Marker:
    """One log record the drill is waiting for, and what its arrival means."""

    key: str
    claim: str
    module: str
    template: str

    def matched_agent(self, module: str, message: str, agent_ids: frozenset[str]) -> str | None:
        """Which of ``agent_ids`` this record is about, or None if it is not this marker at all.

        Any agent on the drilled machine counts. Which one the app enrolls,
        convicts and recovers is not knowable before the run -- on a plain minds
        laptop it is the machine's only agent, which is called
        ``system-services`` -- and it is the same outage whichever it turns out
        to be.
        """
        if module != self.module:
            return None
        return next((agent_id for agent_id in agent_ids if self.template.format(agent=agent_id) in message), None)


# Every message here is a literal from the code that emits it: the first three
# from SystemInterfaceHealthTracker, the rest from workspace_recovery.
MARKERS: Final[tuple[Marker, ...]] = (
    Marker(
        key="connection_failure",
        claim="a request to the machine failed, so the outage reached the app",
        module="system_interface_health",
        template="System-interface connection failure for {agent} classified as",
    ),
    Marker(
        key="enrolled",
        claim="the failure enrolled the machine, so the probe loop started polling it",
        module="system_interface_health",
        template="Enrolled {agent} as a system-interface probe suspect",
    ),
    Marker(
        key="stuck",
        claim="the outage convicted the machine",
        module="system_interface_health",
        template="System-interface health for {agent}: HEALTHY -> STUCK",
    ),
    Marker(
        key="run_restarted",
        claim="a probe-failure run was restarted by the sleep signal",
        module="system_interface_health",
        template="Probe-failure run for {agent} restarted",
    ),
    Marker(
        key="probe_recovered",
        claim="a probe found the machine answering",
        module="system_interface_health",
        # The prior state is interpolated as ``AgentHealth.RECOVERING.value``, so
        # it reaches the log lowercase while the literal HEALTHY beside it does not.
        template="System-interface health for {agent}: recovering -> HEALTHY (probe succeeded)",
    ),
    Marker(
        key="failure_declined",
        claim="the start's failure was declined in favour of the probe",
        module="system_interface_health",
        template="Recovery failure for {agent} not shown",
    ),
    Marker(
        key="invalidated",
        claim="the wake handed the in-flight START back to the probe loop",
        module="system_interface_health",
        template="Recovery of {agent} was in flight across a sleep",
    ),
    Marker(
        key="dispatched",
        claim="an unattended recovery was dispatched",
        module="workspace_recovery",
        template="Unattended recovery for {agent}:",
    ),
    Marker(
        key="start_only",
        claim="the recovery was a start, not a restart",
        module="workspace_recovery",
        template="Start-only recovery for {agent}: skipping the stop step",
    ),
    Marker(
        key="step_failed",
        claim="a recovery step failed",
        module="workspace_recovery",
        template="step of host recovery for {agent} failed",
    ),
    Marker(
        key="recovery_failed",
        claim="the recovery ended in a failure",
        module="workspace_recovery",
        template="Host recovery of {agent} failed",
    ),
)

_STUCK_AFTER_PATTERN: Final[re.Pattern[str]] = re.compile(r"STUCK after ([\d.]+)s")
_DISPATCH_OUTCOME_PATTERN: Final[re.Pattern[str]] = re.compile(r"Unattended recovery for \S+: (\S+)")


def classify_record(record: dict[str, Any], agent_ids: frozenset[str]) -> tuple[Marker, str] | None:
    """The marker ``record`` satisfies and the agent it is about, or None for every other line in the log."""
    module = str(record.get("module") or "")
    message = str(record.get("message") or "")
    for marker in MARKERS:
        agent_id = marker.matched_agent(module, message, agent_ids)
        if agent_id is not None:
            return marker, agent_id
    return None


@dataclass
class Sighting:
    """A matched log record, stamped with the drill's own clocks."""

    key: str
    message: str
    clocks: dict[str, float]
    agent_id: str = ""


def first_sighting(sightings: list[Sighting], key: str) -> Sighting | None:
    return next((sighting for sighting in sightings if sighting.key == key), None)


def all_sightings(sightings: list[Sighting], key: str) -> list[Sighting]:
    return [sighting for sighting in sightings if sighting.key == key]


def _inode_or_none(path: Path) -> int | None:
    """``path``'s inode, or None while it does not exist."""
    try:
        return path.stat().st_ino
    except FileNotFoundError:
        return None


@dataclass
class _LineAssembler:
    """Turns the chunks a follower reads back into whole lines.

    A read of a file another process is appending to lands wherever it lands,
    so a record whose trailing newline had not been written yet arrives in two
    pieces. Neither piece parses, and a follower that handed each straight on
    would drop the record entirely -- which for this drill means reporting that
    a step did not hold when it did.
    """

    _partial: str = ""

    def feed(self, chunk: str) -> list[str]:
        """The complete lines in ``chunk``, with any trailing remainder carried to the next call."""
        self._partial += chunk
        complete, _, self._partial = self._partial.rpartition("\n")
        # split, not splitlines: the framing above knows only "\n", and splitlines
        # would additionally break a record on any of the several other characters
        # Python counts as a line boundary, losing it the way this class prevents.
        return complete.split("\n") if complete else []

    def reset(self) -> None:
        """Forget the carried remainder, which belongs to a file no longer being read."""
        self._partial = ""


@dataclass
class LogWatcher:
    """Follows minds' JSONL log from its current end, keeping the records that matter.

    Reopens the file when it is rotated (minds rotates this log, and a run
    spanning a rotation would otherwise go silent). Records are matched by
    module *and* message, so a marker cannot be satisfied by an unrelated line
    that happens to quote the same text.
    """

    path: Path
    agent_ids: frozenset[str]
    log: EventLog
    clocks: Clocks
    sightings: list[Sighting] = field(default_factory=list)
    # Every record naming this agent, marker or not. A workspace nothing is
    # loading is never mentioned, which is the difference between an app that
    # ignored the outage and an app that never saw the workspace.
    mentions: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> list[Sighting]:
        with self._lock:
            return list(self.sightings)

    def first(self, key: str) -> Sighting | None:
        return first_sighting(self.snapshot(), key)

    def wait_for(self, key: str, timeout_seconds: float) -> Sighting | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            sighting = self.first(key)
            if sighting is not None:
                return sighting
            if self._stop.wait(_FOLLOW_INTERVAL_SECONDS):
                return None
        return self.first(key)

    def run(self) -> None:
        handle = self.path.open("r", errors="replace")
        handle.seek(0, os.SEEK_END)
        inode = os.fstat(handle.fileno()).st_ino
        assembler = _LineAssembler()
        try:
            while not self._stop.wait(_FOLLOW_INTERVAL_SECONDS):
                for line in assembler.feed(handle.read()):
                    self._consider(line)
                # Read rather than probed with exists(): the sink rotates by
                # renaming and reopening, so the path is briefly absent, and a
                # stat() landing in that gap would raise out of this thread and
                # take the rest of the run's markers with it.
                current_inode = _inode_or_none(self.path)
                if current_inode is not None and current_inode != inode:
                    handle.close()
                    handle = self.path.open("r", errors="replace")
                    inode = os.fstat(handle.fileno()).st_ino
                    assembler.reset()
        finally:
            handle.close()

    def stop(self) -> None:
        self._stop.set()

    def _consider(self, line: str) -> None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return
        message = str(record.get("message") or "")
        if any(agent_id in message for agent_id in self.agent_ids):
            with self._lock:
                self.mentions += 1
        classified = classify_record(record, self.agent_ids)
        if classified is None:
            return
        marker, agent_id = classified
        sighting = Sighting(key=marker.key, message=message, clocks=self.clocks.sample(), agent_id=agent_id)
        with self._lock:
            self.sightings.append(sighting)
        self.log.record("minds_log", marker=marker.key, message=message)


@dataclass
class WakeSettler:
    """Tells the wake that ends the sleep from a dark wake partway through it.

    On battery macOS takes short dark wakes mid-sleep; each ends the sleep
    interval and fires the app's wake callbacks, so the first gap the heartbeat
    records is often not the lid opening. Lifting the black hole on one of those
    would put the network back while the machine is still mostly asleep, and
    would time every post-wake measurement from the wrong edge.

    A wake counts as settled once the machine has stayed awake ``settle_seconds``
    past the last gap. Measured on the monotonic clock, which does not advance
    during a suspension -- so the countdown only spends awake seconds, and a dark
    wake's few seconds of wakefulness never reach it.
    """

    settle_seconds: float
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_gap_monotonic: float | None = None
    _any_gap: threading.Event = field(default_factory=threading.Event)

    def on_wake(self, _wake: WakeObservation) -> None:
        with self._lock:
            self._last_gap_monotonic = time.monotonic()
        self._any_gap.set()

    def wait_for_any_gap(self, timeout_seconds: float) -> bool:
        """Block until the machine comes back from *any* gap, dark wake included.

        The post-wake scenario acts here rather than at the settled wake: the
        rule has to be in place before the app's first request after the wake
        reaches the forward, and a dark wake that puts it in early is not a
        problem -- the rule simply stays in until the machine is properly awake.
        """
        return self._any_gap.wait(timeout_seconds)

    def awake_seconds_since_last_gap(self, now_monotonic: float) -> float | None:
        """Awake seconds since the most recent gap, or None before any gap."""
        with self._lock:
            if self._last_gap_monotonic is None:
                return None
            return now_monotonic - self._last_gap_monotonic

    def is_settled(self, now_monotonic: float) -> bool:
        awake_for = self.awake_seconds_since_last_gap(now_monotonic)
        return awake_for is not None and awake_for >= self.settle_seconds

    def wait_for_a_settled_wake(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.is_settled(time.monotonic()):
                return True
            time.sleep(1.0)
        return self.is_settled(time.monotonic())


def log_age_seconds(path: Path, now_wall: float) -> float | None:
    """How long ago ``path`` was last written, or None if it does not exist.

    The pre-flight check for "an app is running and writing here". Freshness
    rather than growth: a healthy app with nothing happening writes nothing for
    minutes at a time, so waiting to see the file grow would refuse a working
    setup as often as a broken one.
    """
    if not path.exists():
        return None
    return now_wall - path.stat().st_mtime


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrillTarget:
    """One app, the workspace this run takes off the network for it, and the follower watching its log."""

    app: MindsApp
    host: HostTarget
    peer_ip: str
    watcher: LogWatcher


def _verdict(ok: bool | None) -> str:
    if ok is None:
        return "UNMEASURED"
    return "HOLDS" if ok else "DOES NOT HOLD"


def _relative_to_wake(sighting: Sighting | None, wake: WakeObservation | None) -> str:
    if sighting is None:
        return "-"
    if wake is None:
        return "no wake"
    return f"{sighting.clocks['wall'] - wake.wake_clocks['wall']:+.0f}s"


def build_comparison(targets: list[DrillTarget], wakes: list[WakeObservation]) -> list[str]:
    """The one table this run exists for: what each app did, on the same outage and the same wake.

    Every time is relative to the wake, because that is the only instant the
    apps share -- they convict on their own probe cadences and their workspaces
    live behind different boxes, so wall-clock columns would differ for reasons
    that are not the difference being looked for.
    """
    wake = wakes[-1] if wakes else None
    rows = [
        ("app", "STUCK", "dispatch", "wake handoff", "probe back", "card"),
    ]
    for target in targets:
        sightings = target.watcher.snapshot()
        failures = all_sightings(sightings, "step_failed") + all_sightings(sightings, "recovery_failed")
        declined = first_sighting(sightings, "failure_declined")
        if not failures:
            card = "none"
        elif declined is not None:
            card = "declined"
        else:
            card = "RECOVERY_FAILED"
        rows.append(
            (
                target.app.label,
                _relative_to_wake(first_sighting(sightings, "stuck"), wake),
                _relative_to_wake(first_sighting(sightings, "dispatched"), wake),
                _relative_to_wake(first_sighting(sightings, "invalidated"), wake),
                _relative_to_wake(first_sighting(sightings, "probe_recovered"), wake),
                card,
            )
        )
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    out = ["=" * 78, "SIDE BY SIDE (times relative to the wake; '-' is never observed)", "=" * 78]
    out.extend(
        "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip() for row in rows
    )
    out.append("")
    out.append("STUCK before the wake is the outage being convicted, which is the drill working.")
    out.append("'probe back' is the reconnecting notice clearing. 'card' is what the user was left")
    out.append("looking at: RECOVERY_FAILED is the regression, declined is the backstop catching it.")
    return out


def _after_wake(sighting: Sighting | None, wake: WakeObservation | None) -> str:
    if sighting is None or wake is None:
        return "n/a"
    return f"{sighting.clocks['wall'] - wake.wake_clocks['wall']:+.1f}s after the wake"


def build_post_wake_report(
    *,
    app_label: str,
    target: HostTarget,
    sightings: list[Sighting],
    mentions: int,
    wakes: list[WakeObservation],
    black_hole_installed_wall: float | None,
    black_hole_removed_wall: float | None,
    blocked_connections: list[EstablishedConnection],
) -> list[str]:
    """What one app did with a wake whose connections were dead and whose machine was not.

    The verdict is the opposite way round from the recovery scenario: convicting
    is the defect. The machine answered a fresh connection throughout, so a
    machine called stuck was called stuck wrongly, and an unattended start
    dispatched for it restarted a machine that was running.

    Whether the outage was real is read off the drill's own evidence -- the
    connections it found open at the wake and killed -- rather than off the
    app's log. A build that retires its tunnels at the wake never sends a byte
    on a dead connection, so its log carries no failure, and that silence is the
    fix rather than a sign nothing was staged. What the app's log *can* say is
    whether anything was looking at the workspace at all.
    """
    wake = wakes[-1] if wakes else None
    stuck = first_sighting(sightings, "stuck")
    dispatched = first_sighting(sightings, "dispatched")
    held = first_sighting(sightings, "run_restarted")

    out: list[str] = []
    out.append("=" * 78)
    out.append(f"APP {app_label}: {target.display_name} ({target.host_id})")
    out.append("=" * 78)
    out.append(f"machine: {target.host_name} at {target.ssh_host}:{target.ssh_port}")
    out.append(f"agents watched on it: {', '.join(sorted(target.agent_ids))}")
    if black_hole_installed_wall is not None and black_hole_removed_wall is not None:
        out.append(
            f"connections dead for {black_hole_removed_wall - black_hole_installed_wall:.0f}s from the wake, with "
            "the machine answering a fresh connection throughout"
        )
    out.append(f"sleep gaps observed: {len(wakes)}")

    out.append("")
    out.append("0. The outage was real: connections to the machine were open at the wake and were killed.")
    if blocked_connections:
        named = ", ".join(f"{c.process} :{c.local_port}->{c.peer_port}" for c in blocked_connections)
        out.append(f"   observed: {len(blocked_connections)} dead from the wake ({named})")
    else:
        out.append("   observed: nothing was open to this machine at the wake, so nothing was killed for this app.")
        out.append("   Check a window was open on the workspace before the sleep and re-run.")
    out.append(f"   verdict: {_verdict(bool(blocked_connections))}")

    # Without a killed connection the steps below are describing an app that was
    # never asked the question, which is the one reading that must not come out
    # as a pass. The same goes for an app whose log never named the agents: nothing
    # was loading the workspace, so the probe loop never looked at it.
    is_measured = bool(blocked_connections) and mentions > 0

    out.append("")
    out.append("1. The app touched the machine after the wake.")
    failure = first_sighting(sightings, "connection_failure")
    enrolled = first_sighting(sightings, "enrolled")
    out.append(f"   requests failed: {'none' if failure is None else failure.message}")
    if failure is not None:
        out.append(f"   {_after_wake(failure, wake)}")
    out.append(f"   enrolled for probing: {'no' if enrolled is None else _after_wake(enrolled, wake)}")
    out.append(f"   the app's log named these agents {mentions} times during the run")
    if failure is None and mentions > 0:
        out.append("   No request failed on a connection the drill had killed, so the app's tunnels were rebuilt")
        out.append("   before anything was sent on them.")
    if mentions == 0:
        out.append("   Never, so nothing was loading the workspace and the probe loop never looked at it.")
        out.append("   Open a window on it and re-run.")
    out.append(f"   verdict: {_verdict(mentions > 0)}")

    out.append("")
    out.append("2. The machine was not called stuck while its tunnel was rebuilding.")
    if stuck is None:
        out.append("   observed: no STUCK edge")
    else:
        out.append(f"   observed: {stuck.message}")
        out.append(f"   {_after_wake(stuck, wake)}")
    out.append(f"   verdict: {_verdict(stuck is None if is_measured else None)}")

    out.append("")
    out.append("3. No restart was started for a machine that was running.")
    if dispatched is None:
        out.append("   observed: no unattended recovery was dispatched.")
    else:
        out.append(f"   observed: {dispatched.message}")
        out.append(f"   {_after_wake(dispatched, wake)}")
        out.append(
            "   The machine answered a fresh connection the whole time, so this restarted a machine that was fine."
        )
    out.append(f"   verdict: {_verdict(dispatched is None if is_measured else None)}")

    out.append("")
    out.append("4. The sleep signal restarted a failure run that straddled the sleep.")
    out.append(f"   observed: {'no run was restarted at the wake' if held is None else held.message}")
    out.append("   Only fires for a run that began before the sleep, so absence here is not a failure.")
    out.append(f"   verdict: {_verdict(True if held is not None else None)}")
    return out


def build_report(
    *,
    app_label: str,
    target: HostTarget,
    sightings: list[Sighting],
    mentions: int,
    wakes: list[WakeObservation],
    black_hole_installed_wall: float | None,
    black_hole_removed_wall: float | None,
) -> list[str]:
    """The drill's verdict per step of the chain, in the order the app runs them."""
    wake = wakes[-1] if wakes else None
    stuck = first_sighting(sightings, "stuck")
    dispatched = first_sighting(sightings, "dispatched")
    start_only = first_sighting(sightings, "start_only")
    invalidated = first_sighting(sightings, "invalidated")
    recovered = first_sighting(sightings, "probe_recovered")
    declined = first_sighting(sightings, "failure_declined")
    failures = all_sightings(sightings, "step_failed") + all_sightings(sightings, "recovery_failed")

    out: list[str] = []
    out.append("=" * 78)
    out.append(f"APP {app_label}: {target.display_name} ({target.host_id})")
    out.append("=" * 78)
    out.append(f"machine: {target.host_name} at {target.ssh_host}:{target.ssh_port} (host {target.host_id})")
    out.append(f"system-services agent: {target.services_agent_id}")
    out.append(f"agents watched on it: {', '.join(sorted(target.agent_ids))}")
    if stuck is not None and stuck.agent_id:
        out.append(f"the app convicted: {stuck.agent_id}")
    if black_hole_installed_wall is not None:
        window = (
            "still in place"
            if black_hole_removed_wall is None
            else f"{black_hole_removed_wall - black_hole_installed_wall:.0f}s"
        )
        out.append(
            f"black hole: every packet to {target.ssh_host} dropped for {window}, after a connect to "
            f"{target.ssh_host}:{target.ssh_port} succeeded before it and failed under it"
        )
    out.append(f"sleep gaps observed: {len(wakes)}")
    if len(wakes) > 1:
        out.append(
            "   More than one: macOS dark-woke partway through (usual on battery). Each of those ends the sleep "
            "interval, so the app re-armed its post-wake grace and re-marked the in-flight start every time. The "
            "timings below are measured from the last gap."
        )

    out.append("")
    out.append("1. The outage convicted the machine.")
    stuck_after = None if stuck is None else _STUCK_AFTER_PATTERN.search(stuck.message)
    out.append(f"   observed: {'no STUCK edge' if stuck is None else stuck.message}")
    if stuck is None:
        out.append(f"   the app's log named this workspace {mentions} times during the run")
        if mentions == 0:
            out.append("   Never, so nothing was loading it: no request failed, nothing enrolled it as a probe")
            out.append("   suspect, and the probe loop never looked at it. Open a window on this workspace and")
            out.append("   leave it open for the whole run. The outage itself was verified, so this is not it.")
    if stuck_after is not None:
        out.append(f"   convicted after {stuck_after.group(1)}s of continuous probe failures")
    out.append(f"   verdict: {_verdict(stuck is not None)}")

    out.append("")
    out.append("2. The unattended start was dispatched, not withheld.")
    outcome = None if dispatched is None else _DISPATCH_OUTCOME_PATTERN.search(dispatched.message)
    dispatched_start = dispatched is not None and outcome is not None and outcome.group(1) == "DISPATCHED"
    if dispatched is None:
        out.append("   observed: no unattended dispatch. Nothing was in flight for the sleep to land in.")
        out.append("   If the app read this device as offline or SSH-blocked, the start was withheld as owed")
        out.append("   instead -- which would mean the black hole caught the connectivity quorum too.")
    else:
        out.append(f"   observed: {dispatched.message}")
        if dispatched_start:
            out.append(f"   start-only: {'yes' if start_only is not None else 'no (a RESTART, not a START)'}")
        else:
            # The absent start-only marker says nothing about the recovery kind
            # here: this outcome spawned no worker to write one.
            out.append("   No worker was spawned for this outcome, so no recovery of either kind ran.")
    # A start the sleep can land in, rather than merely a dispatch line: the app
    # logs one for every outcome, and only DISPATCHED spawns a worker. The
    # start-only marker is that worker running, and a START rather than a RESTART
    # -- the only recovery the wake marks.
    is_a_start_in_flight = dispatched_start and start_only is not None
    out.append(f"   verdict: {_verdict(is_a_start_in_flight)}")

    out.append("")
    out.append("3. The wake handed the in-flight START back to the probe loop.")
    out.append(
        f"   observed: {'the recovery was not marked at the wake' if invalidated is None else invalidated.message}"
    )
    out.append(f"   {_after_wake(invalidated, wake)}")
    if invalidated is None and not is_a_start_in_flight:
        out.append("   Nothing to mark: no START was in flight. Step 2 is what failed, not this.")
    out.append(f"   verdict: {_verdict(invalidated is not None if is_a_start_in_flight else None)}")

    out.append("")
    out.append("4. A probe found the machine answering after the wake.")
    out.append(f"   observed: {'no probe success recorded' if recovered is None else recovered.message}")
    out.append(f"   {_after_wake(recovered, wake)}")
    out.append("   This is the card clearing: the workspace stops saying it is reconnecting.")
    out.append(f"   verdict: {_verdict(recovered is not None)}")

    out.append("")
    out.append("5. No recovery-failed card was raised.")
    if not failures:
        out.append("   observed: the start never reported a failure.")
        out.append("   Expected: mngr's own suspension watchdog closes the start's stale transport within")
        out.append("   ~5s of the wake and its retry reconnects, so the start completes. The decline path")
        out.append("   below is the backstop for when it does not, and this run did not reach it.")
        out.append(f"   verdict: {_verdict(True)}")
    else:
        for failure in failures:
            out.append(f"   observed: {failure.message}")
        if declined is None:
            out.append("   The failure was NOT declined, so the card flipped to recovery-failed.")
            out.append("   Check whether the probe success in step 4 landed before or after it.")
        else:
            out.append(f"   declined: {declined.message}")
            out.append(f"   {_after_wake(declined, wake)}")
            out.append("   The operation ends as a completion carrying the error as a caveat.")
        out.append(f"   verdict: {_verdict(declined is not None)}")

    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--workspace",
        action="append",
        dest="workspaces",
        default=None,
        help=(
            "which machine to take off the network, as an agent name, workspace display name, host name, or agent "
            "id. Repeatable, and may be prefixed with an app to apply to that app alone "
            "(`--workspace .minds-staging=other-box`). Only needed for an app running more than one machine; an app "
            "with a single one is taken without asking"
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        action="append",
        dest="data_dirs",
        default=None,
        help=(
            "restrict the run to the app with this data directory (e.g. ~/.minds). Repeatable. Every running minds "
            "app is drilled when this is not given"
        ),
    )
    parser.add_argument("--mngr-prefix", default=_DEFAULT_MNGR_PREFIX, help="the apps' MNGR_PREFIX")
    parser.add_argument(
        "--max-log-age",
        type=float,
        default=600.0,
        help="refuse to start if an app's log has not been written this recently (default 600s)",
    )
    parser.add_argument(
        "--scenario",
        choices=(_SCENARIO_POST_WAKE, _SCENARIO_RECOVERY),
        default=_SCENARIO_POST_WAKE,
        help=(
            f"{_SCENARIO_POST_WAKE} (default) sleeps with everything healthy and kills the connections at the wake, "
            f"so a machine called stuck was called stuck wrongly. {_SCENARIO_RECOVERY} convicts the machines first "
            "and sleeps across the restarts that provokes"
        ),
    )
    parser.add_argument(
        "--sleep-minutes", type=float, default=5.0, help="how long the machine stays asleep (default 5)"
    )
    parser.add_argument(
        "--auto-sleep", action="store_true", help="schedule the wake with sudo pmset and sleep now (macOS)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "resolve every app and machine and check each one is reachable, then report what a real run would block "
            "and watch. Installs no pf rule and does not sleep the laptop"
        ),
    )
    parser.add_argument(
        "--wait-for-stuck", type=float, default=180.0, help="seconds to wait for the STUCK edge before giving up"
    )
    parser.add_argument(
        "--wait-for-dispatch", type=float, default=180.0, help="seconds to wait for the unattended dispatch"
    )
    parser.add_argument(
        "--post-wake-wait",
        type=float,
        default=300.0,
        help=(
            "seconds to keep watching after the wake before reporting. In the post-wake scenario the connections "
            "killed at the wake stay dead for all of it"
        ),
    )
    parser.add_argument(
        "--wait-for-sleep", type=float, default=1800.0, help="give up if no sleep is observed within this many seconds"
    )
    parser.add_argument(
        "--wake-settle",
        type=float,
        default=60.0,
        help="awake seconds after a sleep gap before it counts as the wake rather than a dark wake (default 60)",
    )
    parser.add_argument(
        "--reachability-timeout",
        type=float,
        default=5.0,
        help="seconds a TCP connect to the workspace may take when checking the block took effect (default 5)",
    )
    parser.add_argument("--log-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    if not IS_DARWIN:
        print("the drill's black hole is implemented with pf and only on macOS", flush=True)
        return 2

    clocks = Clocks()
    log_path = args.log_dir / f"sleep_wake_drill_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    log = EventLog(log_path, clocks)
    log.record("invocation", argv=sys.argv[1:], settings=vars(args))
    log.record("local_context", **local_context())

    try:
        targets = _resolve_targets(args, log, clocks)
    except (DrillError, OSError, subprocess.TimeoutExpired) as e:
        print(f"\n{e}", flush=True)
        return 2

    # Before the sudo prompt: a dry run installs no pf rule, so asking for a
    # password would be asking for something it has no use for.
    if args.dry_run:
        report = _describe_planned_run(targets, args.reachability_timeout, args.scenario)
        log.record("dry_run", lines=report)
        print("\n" + "\n".join(report))
        print(f"\nraw timeline: {log_path}")
        return 0

    for target in targets:
        threading.Thread(target=target.watcher.run, name=f"minds-log-{target.app.label}", daemon=True).start()
    # One anchor over every app's box, so all of them lose the network on the
    # same packet and get it back on the same one.
    black_hole = BlackHole(
        log=log,
        peer_ips=sorted({target.peer_ip for target in targets}),
        peer_ports=None,
        local_ports=None,
        deadman_seconds=_deadman_seconds(args),
    )
    # Root is taken now, while someone is still at the keyboard. Every pfctl the
    # run makes after this goes through the helper, which is the only way the one
    # that matters -- the rule going in seconds after a wake -- can happen at all.
    try:
        black_hole.start()
    except BlackHoleError as e:
        print(f"\n{e}", flush=True)
        return 2
    try:
        return _run_drill(args, log, log_path, targets, black_hole)
    except (DrillError, BlackHoleError) as e:
        print(f"\n{e}", flush=True)
        return 2
    finally:
        # The rule comes out before anything else, so a crashed run does not
        # leave the workspaces unreachable -- and if that cannot be confirmed it
        # is said here rather than left in a log nobody reads.
        if not black_hole.shutdown():
            print(
                "\nWARNING: could not confirm the black hole came out, so this machine may still be unable to reach "
                f"those workspaces. Check with `{black_hole.manual_cleanup_command.replace('-F rules', '-s rules')}` "
                f"and clear it with `{black_hole.manual_cleanup_command}`.",
                flush=True,
            )
        for target in targets:
            target.watcher.stop()


def _deadman_seconds(args: argparse.Namespace) -> float:
    """How long the rule may outlive a run that stopped asking for it.

    The sum of every wait a run can spend with the rule in place, plus slack. It
    is a backstop against a run that died holding the network down, not a
    schedule, so it errs long: firing it early would end a run that was still
    going.
    """
    return (
        args.wait_for_stuck
        + args.wait_for_dispatch
        + args.wait_for_sleep
        + args.post_wake_wait
        + _DEADMAN_MARGIN_SECONDS
    )


def _pids_by_data_dir(apps: list[MindsApp]) -> dict[Path, list[int]]:
    grouped: dict[Path, list[int]] = {}
    for app in apps:
        grouped.setdefault(app.data_dir, []).append(app.pid)
    return grouped


def _resolve_targets(args: argparse.Namespace, log: EventLog, clocks: Clocks) -> list[DrillTarget]:
    """Every app this run drills, with its workspace resolved and its log proved live.

    Raises rather than dropping an app it cannot resolve. A run that quietly
    drilled one of the two apps asked for would produce a comparison with one
    column, which is the shape of an answer without being one.
    """
    apps = parse_minds_apps(subprocess.run(_PS_ARGV, capture_output=True, text=True, check=True, timeout=60).stdout)
    if args.data_dirs is not None:
        wanted = {path.expanduser().resolve() for path in args.data_dirs}
        found = {app.data_dir.resolve(): app for app in apps}
        missing = sorted(str(path) for path in wanted - set(found))
        if missing:
            running = ", ".join(str(app.data_dir) for app in apps) or "<none>"
            raise DrillError(f"no running minds app has data dir {', '.join(missing)}. Running apps: {running}")
        apps = [found[path] for path in sorted(wanted)]
    if not apps:
        raise DrillError(
            "no running minds app found. The drill reads what the apps themselves are doing, so at least one has to "
            "be running; start it and re-run."
        )
    for data_dir, pids in _pids_by_data_dir(apps).items():
        if len(pids) > 1:
            # Two backends on one data dir is a broken machine rather than two
            # apps: they share a log, so the run could not tell their records
            # apart, and the comparison would carry the same app twice.
            raise DrillError(
                f"two minds backends are running on {data_dir} (pids {', '.join(str(pid) for pid in pids)}). They "
                "write the same log, so there is no telling their records apart. Stop one and re-run."
            )

    targets: list[DrillTarget] = []
    for app in apps:
        age = log_age_seconds(app.log_file, clocks.sample()["wall"])
        if age is None or age > args.max_log_age:
            staleness = "does not exist" if age is None else f"was last written {age / 60:.0f} minutes ago"
            raise DrillError(
                f"the app at {app.data_dir} (pid {app.pid}) has a log that {staleness}: {app.log_file}. With nothing "
                "writing it there is nothing to observe for that app."
            )
        agents = _list_agents(resolve_app_mngr(app, shutil.which), app.host_dir, args.mngr_prefix)
        host = select_host(agents, workspace_query_for(args.workspaces, app.label))
        targets.append(
            DrillTarget(
                app=app,
                host=host,
                peer_ip=socket.gethostbyname(host.ssh_host),
                watcher=LogWatcher(path=app.log_file, agent_ids=host.agent_ids, log=log, clocks=clocks),
            )
        )
    for target in targets:
        log.record(
            "target",
            app=target.app.label,
            data_dir=str(target.app.data_dir),
            pid=target.app.pid,
            agent_ids=sorted(target.host.agent_ids),
            display_name=target.host.display_name,
            host=f"{target.host.ssh_host}:{target.host.ssh_port}",
            peer_ip=target.peer_ip,
            services_agent_id=target.host.services_agent_id,
        )
    return targets


def _describe_planned_run(targets: list[DrillTarget], timeout_seconds: float, scenario: str) -> list[str]:
    """What a real run would block, watch and compare -- and whether each machine can be reached now.

    Everything a run depends on except the outage itself: that the apps were
    found, that each resolved to one running machine, that the log the drill
    will follow is the one that app writes, and that the address about to be
    black-holed answers today. A run whose first act is to sleep the laptop is
    worth checking before it starts.
    """
    out = ["=" * 78, "DRY RUN -- nothing blocked, nothing slept", "=" * 78]
    for target in targets:
        endpoint = f"{target.host.ssh_host}:{target.host.ssh_port}"
        reachable = can_connect(target.host.ssh_host, target.host.ssh_port, timeout_seconds)
        out.append("")
        out.append(f"app {target.app.label} (pid {target.app.pid})")
        out.append(f"   log to follow:  {target.app.log_file}")
        out.append(f"   mngr:           {' '.join(target.app.mngr_command)}")
        out.append(f"   machine:        {target.host.display_name} ({target.host.host_id})")
        if scenario == _SCENARIO_POST_WAKE:
            open_now = list_established_connections({target.peer_ip})
            named = ", ".join(f"{c.process} :{c.local_port}->{c.peer_port}" for c in open_now) or "none open now"
            out.append(f"   would block:    every connection open to {target.peer_ip} at the wake, by local port")
            out.append(f"                   ({named}); a fresh connection would still go through  [{endpoint}]")
        else:
            out.append(f"   would block:    {target.peer_ip}, every port, both directions  [{endpoint}]")
        out.append(f"   reachable now:  {'yes' if reachable else 'NO -- a real run would abort here'}")
        out.append(f"   agents watched: {', '.join(sorted(target.host.agent_ids))}")
        out.append(f"   services agent: {target.host.services_agent_id}")
    out.append("")
    blocked = ", ".join(sorted({target.peer_ip for target in targets}))
    if scenario == _SCENARIO_POST_WAKE:
        out.append("A real run would sleep the laptop with everything healthy, then at the wake kill")
        out.append(f"every connection open to {blocked} in one pf anchor -- and nothing else, so")
        out.append("the machines keep answering -- and compare what each app made of that.")
    else:
        out.append(f"A real run would black-hole {blocked} in one pf anchor, so every app above")
        out.append("loses the network on the same packet, then sleep the laptop across the starts")
        out.append("that outage provokes and compare the apps against the one wake they share.")
    out.append("")
    out.append("Keep a window open on each machine above for the whole run: the health probe")
    out.append("loop only polls agents that a failed request has enrolled as suspect.")
    return out


def _require_reachable(targets: list[DrillTarget], log: EventLog, timeout_seconds: float) -> None:
    """Refuse to stage anything until every machine answers, so a dead one is not read as a staged outage."""
    for target in targets:
        endpoint = f"{target.host.ssh_host}:{target.host.ssh_port}"
        if not can_connect(target.host.ssh_host, target.host.ssh_port, timeout_seconds):
            raise DrillError(
                f"{endpoint} ({target.app.label}) is not reachable, so this run could not tell a staged outage from "
                "the machine already being down. Check the workspace is up."
            )
        log.record("reachable_before_black_hole", app=target.app.label, endpoint=endpoint)


def _install_a_verified_black_hole(
    black_hole: BlackHole, targets: list[DrillTarget], log: EventLog, timeout_seconds: float
) -> None:
    """Put the rule in and prove it took, for every app.

    ``pfctl`` exiting 0 is not evidence that packets are being dropped: the
    anchor may not be evaluated, or the address may not be the one the app talks
    to. Without this, a run where nothing broke is indistinguishable from a run
    where the outage never happened -- which is exactly the run that has to be
    thrown away rather than read.

    Every app's endpoint is checked, not just the first: two apps behind two
    boxes is the case this exists for, and an anchor that reaches one of them is
    a comparison between an app that saw an outage and an app that did not.
    """
    black_hole.install()
    # pf drops the packets rather than answering, so a still-open path shows up
    # as a connect that succeeds; a blocked one spends the whole timeout.
    for target in targets:
        endpoint = f"{target.host.ssh_host}:{target.host.ssh_port}"
        if can_connect(target.host.ssh_host, target.host.ssh_port, timeout_seconds):
            raise DrillError(
                f"the black hole is loaded but {endpoint} ({target.app.label}) still answers, so that app's outage "
                "was not staged. pf may not be evaluating the anchor on this machine."
            )
        log.record("black_hole_verified", app=target.app.label, endpoint=endpoint)


def _install_a_verified_connection_black_hole(
    black_hole: BlackHole,
    targets: list[DrillTarget],
    log: EventLog,
    canary: CanaryPort,
    timeout_seconds: float,
) -> None:
    """Put the port-scoped rule in and prove both halves of it, for every app.

    A rule naming local ports cannot be checked the way a whole-address rule is,
    since a fresh connect gets a fresh port and goes through -- which is the
    other half of what is being staged. So the check is made from the port the
    drill holds and named in the rule, which has to hang, and then from a fresh
    one, which has to answer: a machine that refuses the fresh one is dead
    rather than staged, and the run would be measuring the wrong incident.
    """
    black_hole.install()
    for target in targets:
        endpoint = f"{target.host.ssh_host}:{target.host.ssh_port}"
        if canary.can_connect(target.host.ssh_host, target.host.ssh_port, timeout_seconds):
            raise DrillError(
                f"the black hole is loaded but a connection from a port it names still reaches {endpoint} "
                f"({target.app.label}), so the connections to that machine were not killed. pf may not be "
                "evaluating the anchor on this machine."
            )
        if not can_connect(target.host.ssh_host, target.host.ssh_port, timeout_seconds):
            raise DrillError(
                f"{endpoint} ({target.app.label}) does not answer a fresh connection under a rule that names only "
                "the old ones, so that machine is down rather than staged: this would measure a dead machine, "
                "not the incident."
            )
        log.record("black_hole_verified", app=target.app.label, endpoint=endpoint, is_fresh_connection_answered=True)


def _release_and_confirm(
    black_hole: BlackHole,
    targets: list[DrillTarget],
    log: EventLog,
    timeout_seconds: float,
    connect: Callable[[str, int, float], bool] = can_connect,
) -> list[DrillTarget]:
    """Lift the rule and answer with the apps whose machine did not come back.

    The removal is confirmed on the wire rather than taken from ``pfctl``'s exit
    code, because the consequence of being wrong is a laptop that silently cannot
    reach its workspaces, and because every post-wake measurement after this
    point is worthless if the network never returned. ``connect`` is how: a
    whole-address rule is confirmed gone by any connect, a port-scoped one only
    by a connect from a port it named.
    """
    is_removed = black_hole.remove()
    unreachable: list[DrillTarget] = []
    for target in targets:
        endpoint = f"{target.host.ssh_host}:{target.host.ssh_port}"
        # The machine itself may take a moment; retry across the timeout rather
        # than reading one refused connect as the rule still being in place.
        deadline = time.monotonic() + _RELEASE_CONFIRM_SECONDS
        is_back = False
        while not is_back and time.monotonic() < deadline:
            is_back = connect(target.host.ssh_host, target.host.ssh_port, timeout_seconds)
        log.record("black_hole_release_confirmed", app=target.app.label, endpoint=endpoint, is_reachable=is_back)
        if not is_back:
            unreachable.append(target)
    if unreachable or not is_removed:
        endpoints = ", ".join(f"{t.host.ssh_host}:{t.host.ssh_port} ({t.app.label})" for t in unreachable)
        print(
            f"\nWARNING: the block was lifted but {endpoints or 'the helper could not confirm it'} did not come "
            f"back within {_RELEASE_CONFIRM_SECONDS:.0f}s. Everything measured after this point is against a network "
            f"that never returned. Check with `{black_hole.manual_cleanup_command.replace('-F rules', '-s rules')}`.",
            flush=True,
        )
    return unreachable


def _await_across_apps(targets: list[DrillTarget], key: str, timeout_seconds: float) -> list[DrillTarget]:
    """Wait up to ``timeout_seconds`` in total for every app to log ``key``; answer with those that did not.

    One shared deadline rather than one per app, because the apps are watching
    the same outage at the same time: waiting for them in turn would charge the
    second app's budget for the first app's wait and could push the sleep past
    the point where the first app's recovery is still in flight.
    """
    deadline = time.monotonic() + timeout_seconds
    for target in targets:
        target.watcher.wait_for(key, max(0.0, deadline - time.monotonic()))
    return [target for target in targets if target.watcher.first(key) is None]


def _run_drill(
    args: argparse.Namespace,
    log: EventLog,
    log_path: Path,
    targets: list[DrillTarget],
    black_hole: BlackHole,
) -> int:
    clocks = log.clocks
    settler = WakeSettler(settle_seconds=args.wake_settle)
    heartbeat = HeartbeatMonitor(log=log, clocks=clocks, on_wake=settler.on_wake)
    threading.Thread(target=heartbeat.run, name="heartbeat", daemon=True).start()
    idle_sleep_assertion = prevent_idle_sleep(log)
    try:
        _require_reachable(targets, log, args.reachability_timeout)
        if args.scenario == _SCENARIO_POST_WAKE:
            window = _stage_post_wake_outage(args, log, log_path, targets, black_hole, settler)
        else:
            window = _stage_recovery_across_sleep(args, log, log_path, targets, black_hole, settler, heartbeat)
    finally:
        if idle_sleep_assertion is not None:
            idle_sleep_assertion.terminate()
    if window is None:
        return 1

    heartbeat.stop()
    for target in targets:
        target.watcher.stop()
    return _print_report(args, log, log_path, targets, heartbeat.wakes, window)


@dataclass(frozen=True)
class BlackHoleWindow:
    """When the rule went in and came out, on the wall clock the report reads, and what it named."""

    installed_wall: float
    removed_wall: float | None
    # Per app label, the connections killed at the wake. Empty for the recovery
    # scenario, whose rule names an address rather than connections.
    blocked_connections_by_app: dict[str, list[EstablishedConnection]]


def _stage_post_wake_outage(
    args: argparse.Namespace,
    log: EventLog,
    log_path: Path,
    targets: list[DrillTarget],
    black_hole: BlackHole,
    settler: WakeSettler,
) -> BlackHoleWindow | None:
    """The incident: a laptop that wakes onto a working network to find the connections it had are dead.

    Nothing is staged before the sleep, so both apps go under with healthy
    machines and healthy tunnels. At the wake, every connection then open to the
    machines is killed by its local port and nothing else is: a request on one
    of them hangs the way it did behind the NAT that dropped its mapping, while
    the fresh connection a rebuilt tunnel makes goes straight through. That is
    what makes this the *false* conviction the post-wake grace exists for: the
    machines answer throughout, so a restart either app starts was started
    against a machine that was fine.

    It has to be the first gap rather than the settled wake. The app's first
    request after a wake reaches the forward within seconds, and it is that
    request hanging -- rather than being refused -- that produces the failure run
    under test; a rule installed a minute later would arrive after the tunnel had
    already been rebuilt and would measure nothing. The connections are read off
    the kernel at that same moment, since they are whatever the app had open
    when it went under.
    """
    print(
        f"\nNothing is blocked yet. SLEEP NOW and keep the laptop asleep for at least {args.sleep_minutes:.0f} "
        "minutes. Keep a window open on each workspace. Every connection open to the machines is killed the "
        "moment the laptop wakes.\n",
        flush=True,
    )
    canary = CanaryPort()
    try:
        if args.auto_sleep:
            schedule_sleep(args.sleep_minutes, log)

        if not settler.wait_for_any_gap(args.wait_for_sleep):
            print(
                f"\nno sleep observed within {args.wait_for_sleep:.0f}s; giving up (raw timeline: {log_path})",
                flush=True,
            )
            return None
        connections = list_established_connections({target.peer_ip for target in targets})
        blocked_by_app = {
            target.app.label: [c for c in connections if c.peer_ip == target.peer_ip] for target in targets
        }
        log.record(
            "connections_at_wake",
            canary_port=canary.port,
            **{label: [vars(c) for c in blocked] for label, blocked in blocked_by_app.items()},
        )
        black_hole.narrow_to_local_ports([c.local_port for c in connections] + [canary.port])
        installed_wall = log.clocks.sample()["wall"]
        _install_a_verified_connection_black_hole(black_hole, targets, log, canary, args.reachability_timeout)
        for label, blocked in blocked_by_app.items():
            if not blocked:
                print(
                    f"\nWARNING: nothing was open to {label}'s machine at the wake, so no connection of that app's "
                    "was killed and its column will measure nothing.",
                    flush=True,
                )
        print(
            f"\nWake seen and {len(connections)} connections black-holed, with the machines still answering fresh "
            f"ones. Holding that for the rest of the run ({args.post_wake_wait:.0f}s).\n",
            flush=True,
        )
        time.sleep(args.post_wake_wait)
        _release_and_confirm(black_hole, targets, log, args.reachability_timeout, connect=canary.can_connect)
        removed_wall = log.clocks.sample()["wall"]
    finally:
        canary.close()
    return BlackHoleWindow(
        installed_wall=installed_wall, removed_wall=removed_wall, blocked_connections_by_app=blocked_by_app
    )


def _stage_recovery_across_sleep(
    args: argparse.Namespace,
    log: EventLog,
    log_path: Path,
    targets: list[DrillTarget],
    black_hole: BlackHole,
    settler: WakeSettler,
    heartbeat: HeartbeatMonitor,
) -> BlackHoleWindow | None:
    """The other half: a recovery already running when the laptop sleeps.

    Here the machines really are unreachable before the sleep and the apps are
    right to convict them. What is under test is what the sleep does to the
    ``mngr start`` that conviction dispatched -- every deadline on it measured on
    a clock the sleep stops.
    """
    staged = ", ".join(f"{target.app.label}: {target.host.display_name} ({target.peer_ip})" for target in targets)
    print(
        f"\nBlack-holing every packet to {staged}. Leave the apps running, keep a window open on each of those "
        "workspaces, and do not sleep the laptop yet.\n",
        flush=True,
    )
    installed_wall = log.clocks.sample()["wall"]
    _install_a_verified_black_hole(black_hole, targets, log, args.reachability_timeout)

    unconvicted = _await_across_apps(targets, "stuck", args.wait_for_stuck)
    for target in unconvicted:
        print(
            f"\nNo STUCK edge in {target.app.label} for any agent on {target.host.display_name} within "
            f"{args.wait_for_stuck:.0f}s. That machine is provably unreachable, so nothing was loading the "
            "workspace: the probe loop only polls agents a failed request has enrolled as suspect, and those "
            "requests come from a window open on the workspace. Open one in that app and re-run.",
            flush=True,
        )
    convicted = [target for target in targets if target not in unconvicted]
    undispatched = _await_across_apps(convicted, "dispatched", args.wait_for_dispatch)
    for target in undispatched:
        print(
            f"\nNo unattended dispatch in {target.app.label} within {args.wait_for_dispatch:.0f}s of its STUCK edge. "
            "Sleeping anyway; the report will say what was and was not staged.",
            flush=True,
        )

    print(
        f"\nSLEEP NOW and keep the laptop asleep for at least {args.sleep_minutes:.0f} minutes, then wake it and "
        "leave this running.\n",
        flush=True,
    )
    if args.auto_sleep:
        schedule_sleep(args.sleep_minutes, log)

    is_settled = settler.wait_for_a_settled_wake(args.wait_for_sleep)
    if not heartbeat.wakes:
        print(f"\nno sleep observed; giving up (raw timeline: {log_path})", flush=True)
        return None
    if not is_settled:
        print(
            f"\nThe machine kept going back to sleep, so no wake settled within {args.wait_for_sleep:.0f}s. Lifting "
            "the block and reporting what there is.",
            flush=True,
        )

    # The incident's own network: the laptop wakes onto a working connection and
    # only the connections that predate the sleep are dead.
    _release_and_confirm(black_hole, targets, log, args.reachability_timeout)
    removed_wall = log.clocks.sample()["wall"]
    print(f"\nWake observed and the black hole lifted; watching for another {args.post_wake_wait:.0f}s.\n", flush=True)
    time.sleep(args.post_wake_wait)
    return BlackHoleWindow(installed_wall=installed_wall, removed_wall=removed_wall, blocked_connections_by_app={})


def _print_report(
    args: argparse.Namespace,
    log: EventLog,
    log_path: Path,
    targets: list[DrillTarget],
    wakes: list[WakeObservation],
    window: BlackHoleWindow,
) -> int:
    report: list[str] = []
    for target in targets:
        if args.scenario == _SCENARIO_POST_WAKE:
            report.extend(
                build_post_wake_report(
                    app_label=target.app.label,
                    target=target.host,
                    sightings=target.watcher.snapshot(),
                    mentions=target.watcher.mentions,
                    wakes=wakes,
                    black_hole_installed_wall=window.installed_wall,
                    black_hole_removed_wall=window.removed_wall,
                    blocked_connections=window.blocked_connections_by_app.get(target.app.label, []),
                )
            )
        else:
            report.extend(
                build_report(
                    app_label=target.app.label,
                    target=target.host,
                    sightings=target.watcher.snapshot(),
                    mentions=target.watcher.mentions,
                    wakes=wakes,
                    black_hole_installed_wall=window.installed_wall,
                    black_hole_removed_wall=window.removed_wall,
                )
            )
        report.append("")
    report.extend(build_comparison(targets, wakes))
    pmset = pmset_log_since(log.started["wall"])
    if pmset:
        report.append("")
        report.append("pmset sleep/wake log for the run:")
        report.extend(f"   {line}" for line in pmset)
    log.record("report", lines=report)
    print("\n" + "\n".join(report))
    print(f"\nraw timeline: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
