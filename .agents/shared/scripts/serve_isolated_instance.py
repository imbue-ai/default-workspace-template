#!/usr/bin/env python3
"""Spin up an isolated, throwaway instance of a service on a spare port.

This is the shared substrate under two service flows:

- ``update-app`` boots a copy of a service against a *copy* of its data
  (``DATA_DIR`` pointed at a scratch dir) so an edit can be exercised -- writes,
  deletes, migrations -- without ever touching the user's live store. The agent
  reaches the instance directly on its loopback port.
- ``update-app``'s careful flow for a critical app boots an already-built
  worktree (the lead's editing worktree during its live loop, or a worker's
  work_dir for a final pre-merge check) as a *preview* the user clicks around,
  through ``preview_app.py``, and ``refresh``-es it in place as the lead edits.

Both are the same motion: launch the service on a free port, with environment
overrides that isolate its writable state, wait until it is healthy, and
(optionally) surface it to the user as a labeled "preview" tab. The only thing
that differs is *what* is launched and *how* its state is isolated -- so this
script is deliberately unopinionated and takes all of that as parameters. The
calling skill supplies the specifics.

The two shapes:

- **Bare instance (own testing).** Given just ``--name``, ``--cwd``,
  ``--port-env`` and the launch argv, it picks a free port, injects it into the
  named env var, boots the service, waits for health, and prints the loopback URL
  to stdout. Nothing is registered with the workspace UI; the agent curls /
  drives the port directly. ``down`` kills it.
- **Preview (surface to the user).** Add ``--service-name`` to also register the
  instance as a service (served raw at its own browser origin), and
  ``--preview-service-name`` + ``--preview-title`` to wrap it in a labeled
  "preview" frame (``preview_wrapper_server.py``) the user opens as a tab.
  Registered names become hostname labels, so they must be DNS-safe: lowercase
  letters/digits with single hyphens (e.g. ``preview-1``, not ``preview_1``),
  not ``localhost``, and not starting with ``host-`` or ``agent-``. ``down``
  kills both servers and deregisters both services.

The service must read its port (and, when relevant, its data dir) from the
environment -- that is what ``--port-env`` / ``--env`` inject. Scaffolded Flask
services do this out of the box (``<PKG>_PORT`` / ``<PKG>_DATA_DIR``); an older
service is retrofitted with the same one-liner when it is edited.

Run via bare ``python3`` (standard library only) -- like ``forward_port.py``, it
orchestrates the environment, so it must not depend on any particular venv
being synced.

Usage:
    python3 serve_isolated_instance.py up --name <slug> --cwd <dir> \\
        (--port-env <ENVVAR> | --port main) [--port-env <name>=<ENVVAR> | --port <name> ...] \\
        [--host-env <ENVVAR>] \\
        [--env NAME=VALUE ...] [--unset-env NAME ...] [--copy KEY=SOURCE ...] \\
        [--health-path /path] [--service-name <name>] \\
        [--preview-service-name <name> --preview-title <label> [--inner-path /path]] \\
        [--repo-root PATH] -- <launch argv...>
    python3 serve_isolated_instance.py refresh --name <slug> [--repo-root PATH]
    python3 serve_isolated_instance.py down --name <slug> [--repo-root PATH]

``refresh`` re-boots the inner server (only) on its existing port so a rebuild or
edit is picked up in place -- the port, the wrapper frame, the service
registrations, and the user's tab all stay put.

Ports, copies, and placeholders: every ``--port-env`` names a free port the
instance is given (a bare ``ENVVAR`` is the ``main`` port, the one probed for
health; ``sidecar=MYSVC_API_PORT`` adds a second), and ``--port <name>`` gives it
one that no env var carries, reachable only by placeholder (what a manifest's
``[preview]`` table declares). ``--copy KEY=SOURCE`` copies
a directory (repo-relative or absolute) into the instance's scratch space before
boot, so the instance can write to it freely. The launch argv and every ``--env``
value may carry ``{port:<name>}``, ``{copy:<key>}``, ``{scratch}`` (a fresh
directory of the instance's own), and ``{host}`` (the loopback host); they are
filled once the ports are allocated and the copies made. These are the
placeholders an app's manifest ``[preview]`` table uses (``app_manifest``'s
``PreviewSpec`` mirrors this pattern); the preview script resolves the ones only
it knows before calling here.

Exit codes:
    0  Success (instance is up and healthy / rebooted in place / torn down).
    1  Failure to boot (partial state torn down); or, for ``refresh``, there was
       no refreshable instance, the old server would not exit in time, or the
       rebooted server never became healthy; or, for ``down``, a recorded process
       survived SIGKILL (its state dir is kept so it stays findable); or a bad
       argument / unreadable state file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple, Sequence

# State (detached pids, ports, registered service names) lives under the caller's
# ``data/`` so it is gitignored and survives between the separate ``up`` and
# ``down`` invocations. One instance per ``--name``; ``up`` tears down any stale
# instance for the name first.
STATE_ROOT = "data/.state/isolated-instances"
STATE_FILENAME = "instance.json"
INNER_LOG_FILENAME = "instance.log"
WRAPPER_LOG_FILENAME = "wrapper.log"
# Where an instance's copies and its own scratch directory live, inside its state
# dir so ``down`` removes them with everything else.
COPIES_DIRNAME = "copies"
SCRATCH_DIRNAME = "scratch"
# The port the health probe reaches and the wrapper frames; every instance has it.
MAIN_PORT_NAME = "main"
LOOPBACK_HOST = "127.0.0.1"
# The placeholders this script fills in the launch argv and ``--env`` values. The
# same pattern as ``app_manifest.manifest.PREVIEW_PLACEHOLDER_PATTERN``: a kind,
# optionally a name. Only the kinds below are this script's; any other is left as
# written, for the caller that knows it.
_PLACEHOLDER_PATTERN = re.compile(r"\{(?P<kind>[a-z_]+)(?::(?P<name>[a-z0-9_-]+))?\}")
_PORT_PLACEHOLDER_KIND = "port"
_COPY_PLACEHOLDER_KIND = "copy"
_SCRATCH_PLACEHOLDER_KIND = "scratch"
_HOST_PLACEHOLDER_KIND = "host"
# A failed ``up`` deletes the whole state directory, taking the log its own
# failure message just pointed at with it. The log is copied here first -- outside
# the state dir, one per name, overwritten each time -- so the path in the message
# still exists when the agent goes to read more of it.
FAILED_LOG_SUFFIX = "-failed.log"

# forward_port.py is stdlib-only (the supervisord program lines that register an
# app run it under a plain python3), so no venv is needed here either.
FORWARD_PORT_CMD = ("python3", "system/scripts/forward_port.py")

# How to spell this script in a message an agent will copy: repo-root-relative,
# like every other command it prints. The follow-up commands it names
# (``refresh`` / ``down``) are sub-commands of *this* script, and the flows that
# reach them arrive through an adapter (e.g. ``preview_app.py up``), so a bare
# verb would not be runnable.
_SELF_HINT = ".agents/shared/scripts/serve_isolated_instance.py"

# The wrapper server ships beside this script and is stdlib-only, so it runs under
# the same bare ``python3`` that runs this script -- no venv resolution.
WRAPPER_SCRIPT = "preview_wrapper_server.py"
_WRAPPER_SCRIPT_PATH = Path(__file__).resolve().parent / WRAPPER_SCRIPT

# Boot budget: a fresh instance (first import + startup) runs alongside whatever
# else is on the box, so give it a generous grace before declaring it dead.
_HEALTH_ATTEMPTS = 60
_HEALTH_INTERVAL_SECONDS = 1.0

# When ``refresh`` reboots the inner server, how long to wait for the old process
# to exit (and release its listening socket) before rebinding the same port. A
# teardown gives a process the same grace to exit on SIGTERM.
_STOP_ATTEMPTS = 30
_STOP_INTERVAL_SECONDS = 0.5

# How long to keep watching after SIGKILL before calling a process unkillable.
# SIGKILL cannot be caught, so anything still here is stuck in the kernel (an
# uninterruptible I/O wait) and no further wait would help.
_KILL_ATTEMPTS = 10
_KILL_INTERVAL_SECONDS = 0.5

# How much of a failed boot's log to quote back on stderr. Enough to carry the
# traceback or the refusal message that explains the failure, short enough not to
# bury it.
_LOG_EXCERPT_LINES = 40

# How much of a failing health response's body to quote. The body is where the
# diagnosis lives -- a system interface answering 503 names in it exactly which
# precondition failed -- and that one sentence is the difference between an agent
# that knows what to fix and one reading a wall of discovery DEBUG chatter.
_HEALTH_BODY_EXCERPT_BYTES = 400

# Written into the (append-only, refresh-reused) inner log before every spawn, so
# an excerpt can be scoped to the boot that just failed. Without it, a boot that
# hangs at import and writes nothing shows the *previous* boot's traceback, and an
# agent reading that hunts a cause that no longer exists in the source.
_BOOT_MARKER_PREFIX = "===== boot "

# Where the shared service-band tagger lives, relative to the repo root. An
# isolated instance is launched directly rather than as a supervisord program, so
# nothing else bands it: it inherits the launching shell's band, and every Claude
# bash command self-tags AGENT_SUBPROCESS (900). That makes the surface the user is
# actually looking at the first non-browser thing shed under memory pressure, with
# nothing re-polling health afterwards to tell them their tab went dead.
_OOM_TAG_SCRIPT = "system/services/oom_priority/bin/oom_tag_service.py"
# The band every user-created service shares. A served instance is exactly that.
# The tradeoff is deliberate: at 200 the instance outlives every agent, including
# the lead driving it. The alternative kills the surface under the user's eyes
# first, which is the one loss that stays invisible until they stare at a dead tab.
_OOM_SERVICE_KEY = "user"


class InstanceError(Exception):
    """A throwaway instance failed to boot (avoids raising built-in exceptions).

    Carries the log the failing server was writing, when there is one, so the
    handler can quote the right boot's tail and preserve the file before the
    state directory it lives in is removed.
    """

    def __init__(self, message: str, log_path: Path | None = None) -> None:
        super().__init__(message)
        self.log_path = log_path


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands."""

    def run(self, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), **kwargs)

    def kill_process_group(self, pid: int, sig: int = signal.SIGTERM) -> None:
        """Send ``sig`` to the whole process group led by ``pid``; a no-op if the
        group is already gone.

        Uses ``os.killpg`` directly rather than shelling out to ``kill -<sig>
        -<pid>``: the external procps-ng ``kill`` mis-parses a bare negative-pid
        argument and can signal PID 1 / unrelated groups (procps-ng issue #65),
        which inside a container whose PID 1 traps SIGTERM restarts the whole
        container. ``os.killpg`` targets exactly the intended group.
        """
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    def process_group_alive(self, pid: int) -> bool:
        """Whether the process group led by ``pid`` still exists.

        Used by ``refresh`` to wait for a just-killed inner server to actually
        exit before rebinding its port: a live listening socket cannot be
        rebound (even with SO_REUSEADDR), so we must not respawn until the old
        process is gone. Signal 0 probes existence without delivering a signal.
        """
        try:
            os.killpg(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # The group exists but is not ours to signal -- still "alive".
            return True


class ProbeResult(NamedTuple):
    """One health probe: the status (None when unreachable) and the response body."""

    status: int | None
    body: str

    @property
    def is_healthy(self) -> bool:
        return self.status == 200

    def describe(self) -> str:
        """One unterminated stderr line saying what the probe actually got back.

        The whole point of keeping the body: a health endpoint that refuses states
        *why* in it, and discarding that leaves the caller with a bare status code
        and a log to go read.
        """
        if self.status is None:
            return "  last probe: no response (connection refused, or timed out)"
        if not self.body:
            return f"  last probe: HTTP {self.status}, empty body"
        return f"  last probe: HTTP {self.status} {self.body}"


def _read_body_excerpt(response) -> str:
    """Read at most ``_HEALTH_BODY_EXCERPT_BYTES`` of a response body, as text."""
    try:
        raw = response.read(_HEALTH_BODY_EXCERPT_BYTES + 1)
    except OSError:
        return ""
    # Truncation is judged on the raw bytes, before decoding: the read cap is in
    # bytes, and a multi-byte body can be over the byte budget while under it in
    # characters -- which would silently drop the "..." marker and pass off a
    # partial body as the whole diagnosis. Stripping bytes is UTF-8-safe (no
    # multi-byte sequence contains an ASCII whitespace byte), and slicing may
    # split a trailing multi-byte character, which ``errors="replace"`` absorbs.
    stripped = raw.strip()
    is_truncated = len(stripped) > _HEALTH_BODY_EXCERPT_BYTES
    text = stripped[:_HEALTH_BODY_EXCERPT_BYTES].decode("utf-8", errors="replace")
    return text + "..." if is_truncated else text


class HttpClient:
    """Indirection over the loopback health probe."""

    def get(self, url: str, timeout: float) -> ProbeResult:
        """GET ``url``, returning its status and a truncated body."""
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return ProbeResult(int(response.status), _read_body_excerpt(response))
        except urllib.error.HTTPError as exc:
            # An HTTPError *is* the response, so the refusal's body is readable
            # here -- and a refusal is exactly the case whose body is worth having.
            return ProbeResult(int(exc.code), _read_body_excerpt(exc))
        except (urllib.error.URLError, OSError):
            return ProbeResult(None, "")


class Spawner:
    """Indirection over ``subprocess.Popen`` for detached servers.

    Every server this script starts must outlive the ``up`` invocation (so the
    user can explore the tab / the agent can drive the port), so all spawns are
    detached and later killed by ``down`` via the recorded pid.
    """

    def spawn_detached(
        self, argv: Sequence[str], cwd: str, env: dict, log_path: str
    ) -> int:
        """Start a long-lived process in its own session; return its pid.

        ``start_new_session=True`` makes the child a session/process-group leader
        so it survives this script exiting and so ``down`` can signal the whole
        group, reaping any grandchildren ``uv run`` spawns. Output is appended to
        ``log_path`` so a failed boot is diagnosable.
        """
        with open(log_path, "ab") as log_file:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return int(process.pid)


def find_free_port() -> int:
    """Bind to an ephemeral port, then release it for the server to take."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_healthy(
    http: HttpClient,
    url: str,
    attempts: int,
    interval: float,
    sleeper: Callable[[float], None],
) -> ProbeResult:
    """Poll ``url`` until it returns HTTP 200, up to ``attempts`` times.

    Returns the *last* probe rather than a bare bool, so a caller reporting the
    failure can say what the final attempt actually got back.
    """
    result = ProbeResult(None, "")
    for index in range(attempts):
        result = http.get(url, timeout=5.0)
        if result.is_healthy:
            return result
        if index < attempts - 1:
            sleeper(interval)
    return result


def _wait_process_gone(
    runner: Runner,
    pid: int,
    attempts: int,
    interval: float,
    sleeper: Callable[[float], None],
) -> bool:
    """Poll until the process group led by ``pid`` is gone, up to ``attempts``."""
    for index in range(attempts):
        if not runner.process_group_alive(pid):
            return True
        if index < attempts - 1:
            sleeper(interval)
    return False


def substitute_placeholders(
    text: str,
    port_by_name: dict[str, int],
    copy_by_key: dict[str, str],
    scratch_dir: str,
) -> str:
    """Fill this script's placeholders in one argv element or env value.

    A ``{port:<name>}`` or ``{copy:<key>}`` naming something the instance was not
    given is an error rather than a literal left in a command line: it would boot
    the instance against a path that does not exist and fail somewhere far less
    legible. Other placeholders are left as written.
    """

    def fill(match: re.Match[str]) -> str:
        kind = match.group("kind")
        name = match.group("name")
        if kind == _PORT_PLACEHOLDER_KIND:
            if name not in port_by_name:
                raise InstanceError(
                    f"{match.group(0)} names a port this instance was not given "
                    f"(have: {', '.join(sorted(port_by_name)) or 'none'})"
                )
            return str(port_by_name[name])
        if kind == _COPY_PLACEHOLDER_KIND:
            if name not in copy_by_key:
                raise InstanceError(
                    f"{match.group(0)} names a copy this instance was not given "
                    f"(have: {', '.join(sorted(copy_by_key)) or 'none'})"
                )
            return copy_by_key[name]
        if kind == _SCRATCH_PLACEHOLDER_KIND and name is None:
            return scratch_dir
        if kind == _HOST_PLACEHOLDER_KIND and name is None:
            return LOOPBACK_HOST
        return match.group(0)

    return _PLACEHOLDER_PATTERN.sub(fill, text)


def _inner_env(
    port_env_by_name: dict[str, str | None],
    port_by_name: dict[str, int],
    host_env: str | None,
    env_overrides: dict[str, str] | None,
    unset_env: Sequence[str],
    copy_by_key: dict[str, str],
    scratch_dir: str,
) -> dict[str, str]:
    """Build the child environment for the inner server: the ambient env with the
    isolating overrides applied (placeholders filled) and every named port
    injected. Shared by ``up`` (fresh boot) and ``refresh`` (re-boot on the same
    ports)."""
    env = dict(os.environ)
    for key in unset_env:
        env.pop(key, None)
    for key, value in (env_overrides or {}).items():
        env[key] = substitute_placeholders(
            value, port_by_name, copy_by_key, scratch_dir
        )
    for name, port_env in port_env_by_name.items():
        if port_env is not None:
            env[port_env] = str(port_by_name[name])
    if host_env is not None:
        env[host_env] = LOOPBACK_HOST
    return env


def parse_env_assignments(assignments: Sequence[str]) -> dict[str, str]:
    """Parse ``NAME=VALUE`` strings into a dict. Raises on a missing ``=``."""
    parsed: dict[str, str] = {}
    for item in assignments:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise InstanceError(f"--env expects NAME=VALUE, got {item!r}")
        parsed[name] = value
    return parsed


def parse_port_envs(
    port_envs: Sequence[str], port_names: Sequence[str] = ()
) -> dict[str, str | None]:
    """Parse the ``--port-env`` and ``--port`` values into port name -> env var (None for a bare port).

    A bare ``ENVVAR`` is the ``main`` port; ``<name>=ENVVAR`` names another; a ``--port``
    name is a port carried by no env var. There must be exactly one ``main``: it is the
    port the health probe reaches.
    """
    parsed: dict[str, str | None] = {}
    for item in port_envs:
        name, sep, env_var = item.partition("=")
        if not sep:
            name, env_var = MAIN_PORT_NAME, item
        if not name or not env_var:
            raise InstanceError(
                f"--port-env expects ENVVAR or <name>=ENVVAR, got {item!r}"
            )
        if name in parsed:
            raise InstanceError(f"--port-env names the port {name!r} twice")
        parsed[name] = env_var
    for name in port_names:
        if not name:
            raise InstanceError("--port expects a port name")
        if name in parsed:
            raise InstanceError(f"--port names the port {name!r} twice")
        parsed[name] = None
    if MAIN_PORT_NAME not in parsed:
        raise InstanceError(
            f"--port-env or --port must name the {MAIN_PORT_NAME!r} port"
        )
    return parsed


def parse_copy_assignments(assignments: Sequence[str]) -> dict[str, str]:
    """Parse ``KEY=SOURCE`` strings into copy key -> source directory."""
    parsed: dict[str, str] = {}
    for item in assignments:
        key, sep, source = item.partition("=")
        if not sep or not key or not source:
            raise InstanceError(f"--copy expects KEY=SOURCE, got {item!r}")
        if key in parsed:
            raise InstanceError(f"--copy names the key {key!r} twice")
        parsed[key] = source
    return parsed


def _make_copies(
    repo_root: Path, state_dir: Path, copy_sources: dict[str, str]
) -> dict[str, str]:
    """Copy each source directory into the instance's scratch space; return key -> copy path.

    A source that does not exist yet (an app that has never written its store) becomes
    an empty directory rather than an error: the instance creates what it needs there,
    exactly as the live app would on its first run.
    """
    copy_by_key: dict[str, str] = {}
    for key, source in copy_sources.items():
        source_path = Path(source)
        if not source_path.is_absolute():
            source_path = repo_root / source_path
        destination = state_dir / COPIES_DIRNAME / key
        if source_path.is_dir():
            shutil.copytree(source_path, destination, symlinks=True)
        else:
            sys.stderr.write(
                f"note: --copy {key}={source}: {source_path} is not a directory; "
                f"the instance gets an empty {destination} instead.\n"
            )
            destination.mkdir(parents=True, exist_ok=True)
        copy_by_key[key] = str(destination)
    return copy_by_key


def _append_boot_marker(log_path: Path, name: str) -> None:
    """Record a boot boundary in the shared log, immediately before a spawn.

    Best-effort: an unwritable log must not stop the instance from booting; the
    excerpt then simply falls back to the whole file, as it did before.
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as handle:
            handle.write(f"{_BOOT_MARKER_PREFIX}{name} {stamp} =====\n")
    except OSError:
        pass


def _lines_since_last_boot(lines: Sequence[str]) -> list[str]:
    """Only the lines written since the most recent boot marker.

    Every line before it belongs to a process that has already exited, and quoting
    those is worse than quoting nothing: an agent cannot tell the two apart, so it
    reads a stale traceback as the current failure.
    """
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith(_BOOT_MARKER_PREFIX):
            return list(lines[index + 1 :])
    return list(lines)


def _log_excerpt(
    log_path: Path,
    display_path: Path | None = None,
    max_lines: int = _LOG_EXCERPT_LINES,
) -> str:
    """Return the last ``max_lines`` this boot wrote, formatted for stderr.

    A failed boot's cause is usually in this log, and the caller is an agent
    reading our stderr -- quoting the tail inline turns "it did not become
    healthy" into an actionable message instead of a pointer to a file it has to
    remember to go read. An empty excerpt is itself the diagnosis when the boot
    hung: the new process wrote nothing at all.

    ``display_path`` is what the message points the reader at, which is not always
    where the lines came from: a failed ``up`` deletes the instance's state
    directory, so it names the copy kept outside it. Best-effort: an unreadable
    log just yields a note.
    """
    named = display_path if display_path is not None else log_path
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError as exc:
        return f"  (could not read {log_path}: {exc})\n"
    tail = _lines_since_last_boot(lines)[-max_lines:]
    if not tail:
        return f"  ({named} holds no output from this boot)\n"
    body = "".join(f"  | {line}\n" for line in tail)
    return f"  last {len(tail)} line(s) of {named}:\n{body}"


def _preserve_failed_log(repo_root: Path, name: str, log_path: Path) -> Path | None:
    """Copy a failed boot's log out of the state dir before that dir is removed.

    Returns where it landed, or None if there was nothing to copy -- in which case
    the caller falls back to naming the original path, which is all it ever had.
    """
    destination = repo_root / STATE_ROOT / f"{name}{FAILED_LOG_SUFFIX}"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(log_path, destination)
    except OSError:
        return None
    return destination


def _banded(repo_root: Path, command: Sequence[str]) -> list[str]:
    """Prefix ``command`` with the service-band tagger, when the repo ships one.

    The tagger sets its own ``oom_score_adj`` and then ``exec``s, so the band is
    inherited by everything the command spawns and the argv is otherwise
    untouched. When the tagger is not there (a caller whose ``--repo-root`` is not
    a workspace checkout), the command launches exactly as it did before -- an
    untagged instance is a far smaller problem than one that cannot start.
    """
    tagger = repo_root / _OOM_TAG_SCRIPT
    if not tagger.is_file():
        return list(command)
    return [sys.executable, str(tagger), _OOM_SERVICE_KEY, *command]


def _state_dir(repo_root: Path, name: str) -> Path:
    return repo_root / STATE_ROOT / name


def _state_path(repo_root: Path, name: str) -> Path:
    return _state_dir(repo_root, name) / STATE_FILENAME


def _register_service(
    runner: Runner, repo_root: Path, service_name: str, port: int, what: str
) -> None:
    result = runner.run(
        [
            *FORWARD_PORT_CMD,
            "--name",
            service_name,
            "--url",
            f"http://localhost:{port}",
            "--no-icon",  # short-lived preview tabs; the generic monogram is fine
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(result, "returncode", 0) != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise InstanceError(f"{what} failed (exit {result.returncode}): {stderr}")


def _kill_process_group_and_wait(
    runner: Runner, pid: int, sleeper: Callable[[float], None]
) -> bool:
    """SIGTERM, wait, then SIGKILL and wait again. True once the group is gone.

    The situation a teardown is reached in is often precisely a wedged instance, so
    "asked it to stop" is not the same as "it stopped": a server that traps SIGTERM
    keeps its port bound and keeps serving, and the state file naming its pid is
    about to be deleted. Only a process that survives SIGKILL is genuinely beyond
    reach (stuck in an uninterruptible kernel wait), and that is what False means.
    """
    runner.kill_process_group(pid, signal.SIGTERM)
    if _wait_process_gone(runner, pid, _STOP_ATTEMPTS, _STOP_INTERVAL_SECONDS, sleeper):
        return True
    runner.kill_process_group(pid, signal.SIGKILL)
    return _wait_process_gone(
        runner, pid, _KILL_ATTEMPTS, _KILL_INTERVAL_SECONDS, sleeper
    )


def _teardown(
    repo_root: Path,
    runner: Runner,
    *,
    pids: Sequence[int],
    services: Sequence[str],
    sleeper: Callable[[float], None] = time.sleep,
) -> list[int]:
    """Tear down whatever ``up`` set up; return the pids that would not die.

    Order: kill every detached server (by process group, escalating to SIGKILL),
    then deregister every registered service so the workspace stops routing at a
    dead port. Deregistration is unchecked and runs for every service regardless,
    so partial state still fully unwinds and re-runs are no-ops -- but a surviving
    process is reported rather than swallowed, because the caller must not delete
    the record of a pid and port that are still in use.
    """
    survivors = [
        pid for pid in pids if not _kill_process_group_and_wait(runner, pid, sleeper)
    ]
    for service in services:
        runner.run(
            [*FORWARD_PORT_CMD, "--remove", "--name", service],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    return survivors


def up(
    name: str,
    command: Sequence[str],
    cwd: str,
    repo_root: Path,
    *,
    port_env_by_name: dict[str, str | None],
    host_env: str | None = None,
    env_overrides: dict[str, str] | None = None,
    unset_env: Sequence[str] = (),
    copy_sources: dict[str, str] | None = None,
    health_path: str = "/",
    service_name: str | None = None,
    preview_service_name: str | None = None,
    preview_title: str | None = None,
    inner_path: str = "/",
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Boot an isolated instance of a service; optionally register + wrap it.

    Picks a free port per name in ``port_env_by_name`` and injects each into its
    env var (and ``host_env`` when given) on top of ``env_overrides`` /
    ``unset_env``, copies every ``copy_sources`` directory into the instance's
    scratch space, fills the placeholders in ``command`` and the env values,
    launches ``command`` from ``cwd`` detached, and waits for ``health_path`` on
    the ``main`` port to serve 200. With ``service_name`` it also registers the
    instance as a service (served at its own origin); with the preview names +
    title it additionally boots the labeled wrapper frame, opening the inner
    service at ``inner_path``.

    On any failure the partial state is torn down and 1 is returned. On success a
    state file records the servers + services so ``down`` can find them later.
    """
    if not command:
        sys.stderr.write("up: no launch command given (pass it after `--`).\n")
        return 1
    if not Path(cwd).is_dir():
        sys.stderr.write(f"up: --cwd {cwd} is not a directory.\n")
        return 1
    preview_requested = preview_title is not None or preview_service_name is not None
    if preview_requested and not (
        preview_title and preview_service_name and service_name
    ):
        sys.stderr.write(
            "up: a preview needs --service-name, --preview-service-name, and "
            "--preview-title together.\n"
        )
        return 1

    # Clear any stale instance for this name first so a re-run is clean. Booting
    # over one that could not be cleared would leave the old server holding its
    # port with nothing left recording its pid, so refuse instead.
    if down(name, repo_root, runner=runner, sleeper=sleeper) != 0:
        sys.stderr.write(
            f"up: refusing to boot '{name}' over an instance that could not be "
            f"cleared (see above). Resolve it, or remove "
            f"{_state_dir(repo_root, name)} to start fresh.\n"
        )
        return 1

    state_dir = _state_dir(repo_root, name)
    inner_log_path = state_dir / INNER_LOG_FILENAME
    wrapper_log_path = state_dir / WRAPPER_LOG_FILENAME
    state_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir = state_dir / SCRATCH_DIRNAME
    scratch_dir.mkdir(parents=True, exist_ok=True)

    # Track what has been stood up so teardown unwinds exactly the partial state
    # on any failure (each server/service is appended right after it is created).
    pids: list[int] = []
    services: list[str] = []
    try:
        # 1. Give the instance its ports and copies, fill the placeholders, and boot
        #    it with the isolating env overrides.
        port_by_name = {port_name: find_free_port() for port_name in port_env_by_name}
        inner_port = port_by_name[MAIN_PORT_NAME]
        copy_by_key = _make_copies(repo_root, state_dir, copy_sources or {})
        resolved_command = [
            substitute_placeholders(part, port_by_name, copy_by_key, str(scratch_dir))
            for part in command
        ]
        env = _inner_env(
            port_env_by_name,
            port_by_name,
            host_env,
            env_overrides,
            unset_env,
            copy_by_key,
            str(scratch_dir),
        )
        _append_boot_marker(inner_log_path, name)
        pids.append(
            spawner.spawn_detached(
                _banded(repo_root, resolved_command),
                cwd=cwd,
                env=env,
                log_path=str(inner_log_path),
            )
        )
        inner_url = f"http://{LOOPBACK_HOST}:{inner_port}"
        probe = wait_healthy(
            http,
            f"{inner_url}{health_path}",
            _HEALTH_ATTEMPTS,
            _HEALTH_INTERVAL_SECONDS,
            sleeper,
        )
        if not probe.is_healthy:
            raise InstanceError(
                f"instance did not become healthy on port {inner_port}\n"
                f"{probe.describe()}",
                log_path=inner_log_path,
            )

        # 2. Register it as a service (own browser origin), if asked.
        if service_name is not None:
            _register_service(
                runner, repo_root, service_name, inner_port, "forward_port register"
            )
            services.append(service_name)

        # 3. Wrap it in a labeled preview frame, if asked.
        wrapper_port: int | None = None
        if preview_requested:
            # Validated up front: a preview implies all three names are set. Re-assert
            # so the invariant is explicit (and the types narrow from ``str | None``).
            assert (
                service_name is not None
                and preview_service_name is not None
                and preview_title is not None
            )
            wrapper_port = find_free_port()
            _append_boot_marker(wrapper_log_path, name)
            pids.append(
                spawner.spawn_detached(
                    _banded(
                        repo_root,
                        [
                            sys.executable,
                            str(_WRAPPER_SCRIPT_PATH),
                            "--port",
                            str(wrapper_port),
                            "--inner-service",
                            service_name,
                            "--title",
                            preview_title,
                            "--inner-path",
                            inner_path,
                        ],
                    ),
                    cwd=str(repo_root),
                    env=dict(os.environ),
                    log_path=str(wrapper_log_path),
                )
            )
            wrapper_probe = wait_healthy(
                http,
                f"http://127.0.0.1:{wrapper_port}/",
                _HEALTH_ATTEMPTS,
                _HEALTH_INTERVAL_SECONDS,
                sleeper,
            )
            if not wrapper_probe.is_healthy:
                raise InstanceError(
                    f"preview wrapper did not become healthy on port {wrapper_port}\n"
                    f"{wrapper_probe.describe()}",
                    log_path=wrapper_log_path,
                )
            _register_service(
                runner,
                repo_root,
                preview_service_name,
                wrapper_port,
                "forward_port register (wrapper)",
            )
            services.append(preview_service_name)

        state = {
            "name": name,
            "cwd": str(cwd),
            "inner_port": inner_port,
            "ports": port_by_name,
            "copies": copy_by_key,
            "scratch": str(scratch_dir),
            "wrapper_port": wrapper_port,
            "inner_path": inner_path if preview_requested else None,
            "pids": pids,
            "services": services,
            "inner_log": str(inner_log_path),
            "wrapper_log": str(wrapper_log_path) if preview_requested else None,
            # The rest of the recipe to re-boot the inner server (pids[0]) on the
            # same ports, so ``refresh`` can replay it without re-passing any of
            # it. Only what the keys above do not already record -- refresh reads
            # ``cwd`` / ``ports`` / ``copies`` / ``scratch`` / ``inner_log`` from
            # the top level, so there is one spelling of each fact. The command
            # is recorded as resolved: the ports and copies it names do not move
            # between boots. The inner server is always the first spawn, so
            # pids[0] is the one refresh cycles; the wrapper (pids[1], if any) is
            # left running.
            "inner": {
                "command": resolved_command,
                "port_env_by_name": dict(port_env_by_name),
                "host_env": host_env,
                "env_overrides": dict(env_overrides or {}),
                "unset_env": list(unset_env),
                "health_path": health_path,
            },
        }
        _state_path(repo_root, name).write_text(json.dumps(state, indent=2))
    except (InstanceError, OSError) as exc:
        # OSError too: a boot can fail by raising rather than exiting non-zero -- a
        # missing ``uv`` binary surfaces as FileNotFoundError, and find_free_port
        # can raise a socket OSError. Either way a server may already be running,
        # so teardown must run.
        #
        # The log is copied out and quoted *before* the teardown below removes the
        # state directory it lives in, so the path this message names still exists
        # when the agent goes to read the rest of it.
        failed_log = getattr(exc, "log_path", None)
        excerpt = ""
        if failed_log is not None:
            preserved = _preserve_failed_log(repo_root, name, failed_log)
            excerpt = _log_excerpt(failed_log, preserved)
        sys.stderr.write(
            f"up failed: {exc}\n{excerpt}tearing down partial instance...\n"
        )
        survivors = _teardown(
            repo_root, runner, pids=pids, services=services, sleeper=sleeper
        )
        if survivors:
            sys.stderr.write(
                f"warning: pid(s) {', '.join(str(pid) for pid in survivors)} survived "
                "SIGKILL and may still hold their port.\n"
            )
        shutil.rmtree(state_dir, ignore_errors=True)
        return 1

    # What the caller needs on stdout: the preview's service name when
    # previewing (its browser origin is derived from the workspace host, which
    # is not knowable server-side -- open the tab by service name, e.g. via
    # layout.py open), else the instance's own loopback URL.
    if preview_requested:
        sys.stdout.write(f"{preview_service_name}\n")
        sys.stderr.write(
            f"preview up: open the '{preview_service_name}' service tab, e.g. "
            f"`python3 system/scripts/layout.py open "
            f"{preview_service_name}` (serving {cwd} "
            f"on port {inner_port}, wrapped on port {wrapper_port}). Opening it "
            "puts it on the user's screen. Run "
            f"`python3 {_SELF_HINT} refresh --name {name}` to pick up a later "
            f"edit on this same port, and `python3 {_SELF_HINT} down --name "
            f"{name}` to tear it down.\n"
        )
    else:
        sys.stdout.write(f"{inner_url}\n")
        sys.stderr.write(
            f"instance up: reach it at {inner_url} (serving {cwd}). Run "
            f"`python3 {_SELF_HINT} down --name {name}` to tear it down.\n"
        )
    return 0


def down(
    name: str,
    repo_root: Path,
    *,
    runner: Runner,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Tear down the instance for ``name``: kill the server(s), deregister the
    service(s), delete the state directory.

    Idempotent: a missing state file is a no-op success, so this is safe to run to
    clean up after a successful test, a rejected preview, or a half-set-up
    instance.

    Returns 1 -- and keeps the state directory -- when the state file is
    unreadable, or when a recorded process is still alive after SIGKILL. Deleting
    the state on a survivor would be the worst outcome available: the file naming
    its pid and port is the only way anything could find it again, and the port
    stays bound either way, so the next ``up`` would silently take a different one.
    """
    state_path = _state_path(repo_root, name)
    if not state_path.exists():
        sys.stderr.write(f"no active instance for '{name}'; nothing to tear down.\n")
        return 0
    try:
        state = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(f"error: could not read instance state {state_path}: {exc}\n")
        return 1
    pids = state.get("pids") or []
    services = state.get("services") or []
    survivors = _teardown(
        repo_root, runner, pids=pids, services=services, sleeper=sleeper
    )
    if survivors:
        sys.stderr.write(
            f"error: instance '{name}' is NOT torn down: pid(s) "
            f"{', '.join(str(pid) for pid in survivors)} are still alive after "
            f"SIGKILL and their port(s) are still bound. Keeping {state_path} so "
            "they stay findable; investigate those pids before re-running.\n"
        )
        return 1
    shutil.rmtree(_state_dir(repo_root, name), ignore_errors=True)
    sys.stderr.write(f"instance for '{name}' torn down.\n")
    return 0


def refresh(
    name: str,
    repo_root: Path,
    *,
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Re-boot the inner server of instance ``name`` on its existing port.

    This is the in-place update motion: the caller has rebuilt/edited the code the
    inner server runs from, and wants the live instance to pick it up *without*
    changing its port, tearing down the wrapper, or moving the user's tab. We stop
    the inner server (``pids[0]``), wait for it to release its port, relaunch the
    exact command recorded at ``up`` time on the same port, and re-probe health.
    The wrapper (``pids[1]``, if any) and both service registrations are left
    untouched -- since the port is unchanged, they keep routing to the new
    process. The caller reloads the tab's iframe itself (this never touches it).

    Returns 0 once the rebooted inner server is healthy; 1 if there is no
    refreshable instance, the old server would not exit, or the new one did not
    come up (in which case the preview tab shows an error until the underlying
    build is fixed and refresh is retried -- but nothing else was disturbed).
    """
    state_path = _state_path(repo_root, name)
    if not state_path.exists():
        sys.stderr.write(
            f"no active instance for '{name}'; nothing to refresh (run `up` first).\n"
        )
        return 1
    try:
        state = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(f"error: could not read instance state {state_path}: {exc}\n")
        return 1
    inner = state.get("inner")
    pids = list(state.get("pids") or [])
    if not inner or not pids:
        sys.stderr.write(
            f"instance '{name}' has no recorded inner-boot recipe; cannot refresh "
            "(tear it down and re-create it with `up`).\n"
        )
        return 1

    old_pid = int(pids[0])
    port = int(state["inner_port"])
    port_by_name = {
        str(port_name): int(value)
        for port_name, value in (state.get("ports") or {}).items()
    }
    copy_by_key = {
        str(key): str(value) for key, value in (state.get("copies") or {}).items()
    }
    scratch_dir = str(
        state.get("scratch") or _state_dir(repo_root, name) / SCRATCH_DIRNAME
    )
    inner_log = str(state["inner_log"])
    # 1. Stop the old inner server and wait for it to release the port. A live
    #    listening socket cannot be rebound, so we must not respawn until it is
    #    gone -- otherwise the new process fails to bind.
    runner.kill_process_group(old_pid)
    if not _wait_process_gone(
        runner, old_pid, _STOP_ATTEMPTS, _STOP_INTERVAL_SECONDS, sleeper
    ):
        sys.stderr.write(
            f"refresh: inner server pid {old_pid} did not exit in time; its port "
            f"{port} may still be held. Aborting rather than risk a bind clash.\n"
        )
        return 1

    # 2. Relaunch the recorded inner command on the SAME ports.
    try:
        env = _inner_env(
            inner["port_env_by_name"],
            port_by_name,
            inner.get("host_env"),
            inner.get("env_overrides") or {},
            inner.get("unset_env") or [],
            copy_by_key,
            scratch_dir,
        )
    except InstanceError as exc:
        sys.stderr.write(
            f"refresh: the recorded recipe for '{name}' cannot be replayed: {exc}\n"
        )
        return 1
    _append_boot_marker(Path(inner_log), name)
    try:
        new_pid = spawner.spawn_detached(
            _banded(repo_root, inner["command"]),
            cwd=str(state["cwd"]),
            env=env,
            log_path=inner_log,
        )
    except OSError as exc:
        sys.stderr.write(f"refresh: failed to relaunch the inner server: {exc}\n")
        return 1
    # Record the new pid *before* the health wait so ``down`` can still kill it
    # even if it never becomes healthy (no leaked server).
    pids[0] = new_pid
    state["pids"] = pids
    state_path.write_text(json.dumps(state, indent=2))

    probe = wait_healthy(
        http,
        f"http://127.0.0.1:{port}{inner['health_path']}",
        _HEALTH_ATTEMPTS,
        _HEALTH_INTERVAL_SECONDS,
        sleeper,
    )
    if not probe.is_healthy:
        sys.stderr.write(
            f"refresh: inner server did not become healthy on port {port} after "
            "reboot. The preview tab will show an error until the underlying "
            "build boots; fix it and refresh again.\n"
            f"{probe.describe()}\n"
            f"{_log_excerpt(Path(inner_log))}"
        )
        return 1
    sys.stderr.write(
        f"refresh: inner server for '{name}' rebooted on port {port}; reload the "
        "tab to see the current build.\n"
    )
    return 0


def _add_repo_root_arg(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root (default: current directory).",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Spin up an isolated, throwaway instance of a service on a spare port "
            "-- for the agent's own testing, or surfaced to the user as a preview."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    up_parser = subparsers.add_parser(
        "up", help="Boot an isolated instance (optionally as a previewable tab)."
    )
    up_parser.add_argument(
        "--name",
        required=True,
        help="Short slug identifying this instance (names the state dir).",
    )
    up_parser.add_argument(
        "--cwd", required=True, help="Directory to launch the service from."
    )
    up_parser.add_argument(
        "--port-env",
        action="append",
        default=[],
        metavar="[NAME=]ENVVAR",
        help="Env var the service reads its port from; a free port is chosen and "
        "injected into it (e.g. SYSTEM_INTERFACE_PORT, MYSVC_PORT). A bare env var "
        "is the 'main' port (the one probed for health); repeat with NAME=ENVVAR "
        "for another named port, reachable as {port:NAME} in --env values and the "
        "launch argv.",
    )
    up_parser.add_argument(
        "--port",
        action="append",
        default=[],
        metavar="NAME",
        help="A named free port carried by no env var, reachable as {port:NAME} "
        "(repeatable); 'main' when no --port-env names it.",
    )
    up_parser.add_argument(
        "--host-env",
        default=None,
        help="Optional env var to set to 127.0.0.1 (for services that bind a "
        "configurable host, e.g. SYSTEM_INTERFACE_HOST).",
    )
    up_parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Env override for the instance (repeatable); e.g. point a *_DATA_DIR "
        "at a scratch copy of the data.",
    )
    up_parser.add_argument(
        "--unset-env",
        action="append",
        default=[],
        metavar="NAME",
        help="Env var to remove for the instance (repeatable).",
    )
    up_parser.add_argument(
        "--copy",
        action="append",
        default=[],
        metavar="KEY=SOURCE",
        help="Copy a directory (repo-relative or absolute) into the instance's "
        "scratch space before boot (repeatable); reachable as {copy:KEY} in --env "
        "values and the launch argv. {scratch} names a fresh directory of the "
        "instance's own, and {host} the loopback host.",
    )
    up_parser.add_argument(
        "--health-path",
        default="/",
        help="Path polled for a 200 to decide the instance is up (default: /).",
    )
    up_parser.add_argument(
        "--service-name",
        default=None,
        help="Register the instance as a service under this name (needed to "
        "surface it as a tab; the name becomes a hostname label, so it must be "
        "DNS-safe: lowercase letters/digits and single hyphens, no underscores, "
        "not starting with 'host-' or 'agent-'). Omit for a bare instance "
        "reached directly on its port.",
    )
    up_parser.add_argument(
        "--preview-service-name",
        default=None,
        help="Register the labeled preview-frame wrapper as this service (the tab "
        "the user opens). Requires --service-name and --preview-title.",
    )
    up_parser.add_argument(
        "--preview-title",
        default=None,
        help="Human-readable label shown in the preview frame banner.",
    )
    up_parser.add_argument(
        "--inner-path",
        default="/",
        help="The path the preview frame opens the inner service on (default: /).",
    )
    _add_repo_root_arg(up_parser)
    up_parser.add_argument(
        "launch",
        nargs=argparse.REMAINDER,
        help="The launch argv, after `--` (e.g. `-- uv run my-service`).",
    )

    down_parser = subparsers.add_parser(
        "down",
        help="Tear down an instance (kill the server(s), deregister service(s)). "
        "Idempotent.",
    )
    down_parser.add_argument("--name", required=True, help="The name passed to 'up'.")
    _add_repo_root_arg(down_parser)

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Re-boot the inner server on its existing port (to pick up a rebuild "
        "/ edit) without changing the port, wrapper, or the user's tab.",
    )
    refresh_parser.add_argument(
        "--name", required=True, help="The name passed to 'up'."
    )
    _add_repo_root_arg(refresh_parser)

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    if args.command == "up":
        # argparse.REMAINDER keeps a leading `--`; drop it so ``command`` is the
        # bare launch argv.
        launch = list(args.launch)
        if launch and launch[0] == "--":
            launch = launch[1:]
        try:
            env_overrides = parse_env_assignments(args.env)
            port_env_by_name = parse_port_envs(args.port_env, args.port)
            copy_sources = parse_copy_assignments(args.copy)
        except InstanceError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 1
        return up(
            args.name,
            launch,
            args.cwd,
            repo_root,
            port_env_by_name=port_env_by_name,
            host_env=args.host_env,
            env_overrides=env_overrides,
            unset_env=args.unset_env,
            copy_sources=copy_sources,
            health_path=args.health_path,
            service_name=args.service_name,
            preview_service_name=args.preview_service_name,
            preview_title=args.preview_title,
            inner_path=args.inner_path,
            runner=Runner(),
            http=HttpClient(),
            spawner=Spawner(),
        )
    if args.command == "down":
        return down(args.name, repo_root, runner=Runner())
    if args.command == "refresh":
        return refresh(
            args.name,
            repo_root,
            runner=Runner(),
            http=HttpClient(),
            spawner=Spawner(),
        )
    parser.error(f"unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
