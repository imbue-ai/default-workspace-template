#!/usr/bin/env python3
"""Reveal a merged system-interface change to the live UI -- and auto-recover if it breaks.

This is the reveal step of the ``update-system-interface`` flow. The lead agent
merges a verified worker branch into the served working tree, then runs this
script. It owns the *entire* reveal sequence as a single deterministic motion,
because the failure mode is catastrophic: if the ``system-interface`` backend
fails to start, the user loses their whole chat UI and there is nowhere left to
surface an error message. So detection is not enough -- this script must always
leave the served UI in a working state, on its own, without the agent.

What it does, given the pre-merge revision (``--rollback-to``):

1. Refuse to run on a dirty tree (so a rollback can never clobber unrelated work).
2. Classify what changed since the known-good revision (frontend src / frontend
   manifest / backend src / backend manifest).
3. Refresh dependencies only if a manifest changed (``npm ci``, then the same
   tool installs and ``uv sync`` that ``build_workspace.sh`` does). A plain
   restart does NOT re-resolve an editable install's dependencies, so a
   dependency add would otherwise crash the service on restart -- and an
   editable install pins only the source path, so this bites the vendored mngr
   the backend shells out to just as hard as the backend itself.
4. For a backend change, *pre-flight* the merged code on a throwaway port before
   touching the live service -- if it cannot boot, the live service is never
   restarted and we go straight to rollback (the UI never went down). The
   throwaway boot's output rides back on the failure, because "it did not boot"
   without the traceback sends whoever reads it guessing at a cause.
5. Build the frontend bundle and restart the backend, as applicable, then ask
   every open view of the workspace to reload, via
   ``system/scripts/refresh_workspace_view.py`` -- for a backend-only change
   too, since the restart leaves the open page rendering from what it had
   already fetched.
6. Probe the live service's loopback endpoint until healthy (with a deadline).
7. On ANY failure, restore the served tree to the known-good revision (as a
   forward revert commit) and re-probe to *confirm* the UI is back. The live
   backend is restarted during recovery only if the failed reveal had already
   restarted it (a failed post-restart health check); when the failure happened
   before the live restart (pre-flight, dependency refresh, frontend build) the
   live service is still serving known-good code and is left untouched, so the
   UI never blips. Only then does the script exit -- reporting what happened via
   its exit code and stderr.

Run via bare ``python3`` (standard library only), like ``forward_port.py`` and
``reload_system_interface``'s predecessor -- it orchestrates the environment, so
it must not depend on any particular venv being synced.

The ``preview`` / ``unpreview`` subcommands are thin system-interface adapters
over the shared ``serve_isolated_instance.py`` motion (the previewable-instance
substrate every service flow shares). They hand it the system-interface
specifics -- boot ``uv run system-interface`` from the worker's already-built
``--work-dir`` on a free port, with layout persistence neutered (drop
MNGR_AGENT_ID so it can't clobber the live ``layout.json``) but agent discovery
kept, probe ``/api/agents``, and register the inner app plus the labeled
"preview" wrapper frame the user opens. The shared script owns the ports, the
process/service teardown, and the state file; no fetch, checkout, or rebuild
happens, and the served tree and the worker's folder are never touched. The
worker is a local git-worktree sub-agent whose work_dir is a folder it has
already built, and it must still exist at preview time.

The non-deterministic part -- opening the tab and gating on the user's judgment
-- stays with the agent.

Usage:
    python3 reveal_system_interface.py reveal --rollback-to <pre-merge-sha> [--repo-root PATH]
    python3 reveal_system_interface.py preview --slug <name> --work-dir <worker-work-dir> [--repo-root PATH]
    python3 reveal_system_interface.py unpreview --slug <name> [--repo-root PATH]

Environment:
    MINDS_WORKSPACE_SERVER_URL  Base URL of the live workspace server
                                (default http://127.0.0.1:8000).
    MNGR_AGENT_ID               Dropped for the preview boot so it cannot
                                clobber the live layout. The refresh helper
                                reads it (and the latchkey gateway vars) itself.

Exit codes (``reveal``):
    0  Revealed successfully; live UI is healthy.
    1  Precondition error (dirty tree, bad arguments) -- nothing was changed.
    2  The change was bad and was rolled back; the live UI is confirmed healthy
       on the known-good revision (the requested change did NOT land).
    3  EMERGENCY: even rollback could not restore a healthy UI.

Exit codes (``preview`` / ``unpreview``):
    0  Success (preview is up / torn down).
    1  The preview failed to build or boot (and tore itself down), or a bad
       argument / unreadable state file.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"
ENV_MNGR_AGENT_ID = "MNGR_AGENT_ID"

# The served app, the editable tool the live service runs from, and the build
# surfaces. These mirror system/scripts/build_workspace.sh -- the source of truth for
# how the served environment is constructed.
APP_DIR = "system/apps/system_interface"
FRONTEND_DIR = f"{APP_DIR}/frontend"
# The vendored mngr the workspace runs on, and the uv tool built from it. An
# editable install pins the *source path*, not the dependency closure -- so the
# moment a merge advances this tree, the ``mngr`` CLI starts running new code
# against whatever was resolved for the old code. build_workspace.sh re-resolves
# it; a reveal that did not would leave ``mngr`` broken (and with it ``mngr
# start --restart``, and the ``mngr observe`` the backend spawns).
MNGR_VENDOR_DIR = "system/vendor/mngr"
MNGR_DIR = f"{MNGR_VENDOR_DIR}/libs/mngr"
MNGR_TOOL_NAME = "imbue-mngr"
# The console script that tool installs; the reveal resolves it on PATH to find
# which of the possibly-several installations is the one actually being run.
MNGR_EXECUTABLE = "mngr"
# uv records how a tool was installed here, inside the tool's own directory.
_RECEIPT = "uv-receipt.toml"
# The frontend build output the backend serves at ``/``. Both ``node_modules``
# and this ``static/`` bundle are gitignored, so a fresh worktree has neither
# until the worker builds it. The preview serves the worker's app dir as-is and
# will not build for it (see ``preview``): a work_dir without this bundle is a
# worker that skipped its build, and the preview refuses it rather than boot the
# backend's "Frontend not built" placeholder.
FRONTEND_BUILD_INDEX = f"{APP_DIR}/imbue/system_interface/static/index.html"
TOOL_NAME = "system-interface"

# The shared post-change refresh motion, repo-relative. Owns *how* a changed
# interface is revealed to whoever is looking (which channels, in what order,
# what is fatal); this script only decides *when*. Shared with the other flows
# that restart the services agent (``update-app``, ``update-self``), so they
# cannot drift on that policy. Stdlib-only, so it runs under our interpreter.
_REFRESH_SCRIPT = "system/scripts/refresh_workspace_view.py"
# The helper budgets its own calls to ~50s in total, so this is a backstop for a
# child that ignores those budgets (a wedged ``mngr`` that does not answer a
# SIGTERM), not the normal bound. A reveal that has already landed must not hang
# on a courtesy reload; TimeoutExpired is a SubprocessError, so overrunning it
# takes the same reported-and-continue path as a helper we cannot spawn.
_REFRESH_TIMEOUT_SECONDS = 120.0

# Pre-merge preview: the deterministic boot + teardown of a previewable instance
# is the shared ``serve_isolated_instance.py`` motion that every service flow
# reuses. ``preview`` / ``unpreview`` below are thin adapters that hand it the
# system-interface specifics; the shared script owns the ports, the
# process/service teardown, and the state file. It lives two levels up under
# ``.agents/shared/scripts/`` and is stdlib-only, so it runs under the same
# interpreter as this script.
_SHARED_SERVE_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "shared"
    / "scripts"
    / "serve_isolated_instance.py"
)
# The service names the preview registers: the inner booted app and the
# outer wrapper the user actually opens. Fixed because the flow runs one preview
# at a time -- enforced by the guard in ``preview`` (a different slug's live
# preview refuses to boot); a re-run of the *same* slug is fine because the
# shared script clears its own stale instance first.
PREVIEW_INNER_SERVICE_NAME = "si-preview-app"
PREVIEW_SERVICE_NAME = "si-preview"
# Where the shared script files each instance's state (mirrors its STATE_ROOT /
# STATE_FILENAME). Used only to detect a different slug's live preview: because
# the service names above are fixed, a second concurrent preview would silently
# hijack the tab of the one already up, and its teardown would later deregister
# the service out from under it.
_INSTANCES_ROOT = "data/.state/isolated-instances"
_INSTANCE_STATE_FILENAME = "instance.json"
# The system interface reads its bind host/port from the environment; the shared
# script injects the free port into PORT and 127.0.0.1 into HOST.
PREVIEW_PORT_ENV = "SYSTEM_INTERFACE_PORT"
PREVIEW_HOST_ENV = "SYSTEM_INTERFACE_HOST"

# Endpoints used to probe liveness. ``/api/agents`` exercises the mngr plugin
# discovery path -- exactly what a missing backend dependency or a broken
# plugin-config parse would take down -- so a 200 there is a strong "the backend
# actually works" signal, not just "the server is listening". It is also handed
# to the shared preview script as its ``--health-path``.
HEALTH_PATH = "/api/agents"
SERVE_PATH = "/"

# Poll budget for "did the service come back up". Restart is fire-and-forget, so
# we poll rather than assume.
_HEALTH_ATTEMPTS = 30
_HEALTH_INTERVAL_SECONDS = 1.0
# Pre-flight boot is a fresh process on a throwaway port; give it the same grace.
_PREFLIGHT_ATTEMPTS = 30
_PREFLIGHT_INTERVAL_SECONDS = 1.0
# How much of a failed pre-flight boot's output rides back on the error. Enough
# for a Python traceback plus the log lines leading into it; the rest is startup
# chatter that pushed the interesting part off the top.
_PREFLIGHT_OUTPUT_TAIL_LINES = 40


class RevealError(Exception):
    """Base class for reveal failures (avoids raising built-in exceptions)."""


class PreconditionError(RevealError):
    """A precondition was not met; nothing was changed, do not roll back."""


class RevealFailed(RevealError):
    """The reveal of the merged change failed; the caller must roll back.

    ``live_service_restarted`` records whether the live service was already
    (re)started before the failure. It is ``False`` for failures that happen
    before the live restart (pre-flight, dependency refresh, frontend build) --
    in which case the live service is untouched and still serving known-good
    code, so recovery must NOT restart it -- and ``True`` once the restart has
    been attempted, where recovery must restart to reload known-good code.
    """

    def __init__(self, message: str, *, live_service_restarted: bool = False) -> None:
        super().__init__(message)
        self.live_service_restarted = live_service_restarted


@dataclass(frozen=True)
class ChangeSet:
    """Which kinds of system-interface change a diff contains."""

    frontend_src: bool
    frontend_manifest: bool
    backend_src: bool
    backend_manifest: bool

    @property
    def frontend(self) -> bool:
        return self.frontend_src or self.frontend_manifest

    @property
    def backend(self) -> bool:
        return self.backend_src or self.backend_manifest

    @property
    def any(self) -> bool:
        return self.frontend or self.backend


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands.

    The default implementation calls ``subprocess.run`` directly; tests inject a
    recording stub instead.
    """

    def run(self, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), **kwargs)

    def which(self, executable: str) -> str | None:
        """Resolve ``executable`` on PATH, as the shell running us would."""
        return shutil.which(executable)


class HttpClient:
    """Indirection over the loopback health probes (live service + pre-flight boot)."""

    def get_status(self, url: str, timeout: float) -> int | None:
        """Return the HTTP status for a GET, or ``None`` if the host is unreachable."""
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (urllib.error.URLError, OSError):
            return None


@dataclass
class Spawned:
    """A handle to a spawned throwaway server process."""

    _process: subprocess.Popen
    _output_path: Path

    def terminate(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            self._process.kill()

    def has_exited(self) -> bool:
        """True once the child is gone, and so can never answer another probe."""
        return self._process.poll() is not None

    def read_output(self) -> str:
        """Return what the child wrote to stdout and stderr, interleaved.

        Read after ``terminate``, so a child that died on its own has already
        flushed. An unreadable file yields ``""``: the caller is already
        reporting a failure and must not fail again while explaining it.
        """
        try:
            return self._output_path.read_text(errors="replace")
        except OSError:
            return ""


class Spawner:
    """Indirection over ``subprocess.Popen`` for the pre-flight throwaway boot.

    ``spawn`` returns a managed child (terminated in a ``finally``) used to boot
    the merged backend on a throwaway port before touching the live service.

    The child's stdout and stderr go to ``output_path`` rather than a pipe: it is
    a chatty server nobody is reading from while we poll, and a pipe whose buffer
    filled would block the very boot we are timing -- turning a healthy backend
    into a pre-flight failure. A file has no such backpressure.
    """

    def spawn(
        self, argv: Sequence[str], cwd: str, env: dict, output_path: Path
    ) -> Spawned:
        # The child dups the fd, so closing our handle right after the spawn
        # leaves it writing to the still-open file.
        with output_path.open("wb") as output_file:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=env,
                stdout=output_file,
                stderr=subprocess.STDOUT,
            )
        return Spawned(_process=process, _output_path=output_path)


def classify_changes(paths: Sequence[str]) -> ChangeSet:
    """Classify repo-relative changed ``paths`` into a :class:`ChangeSet`.

    The frontend build output (``.../static/``) is gitignored and so never
    appears in a diff; we do not need to special-case it here.
    """
    frontend_src = False
    frontend_manifest = False
    backend_src = False
    backend_manifest = False
    for path in paths:
        if path in (
            f"{FRONTEND_DIR}/package.json",
            f"{FRONTEND_DIR}/package-lock.json",
        ):
            frontend_manifest = True
        elif path.startswith(f"{FRONTEND_DIR}/src/"):
            frontend_src = True
        elif _is_backend_manifest(path):
            backend_manifest = True
        elif (
            path.startswith(f"{APP_DIR}/imbue/")
            and path.endswith(".py")
            and not _is_test_file(path)
        ):
            backend_src = True
    return ChangeSet(
        frontend_src=frontend_src,
        frontend_manifest=frontend_manifest,
        backend_src=backend_src,
        backend_manifest=backend_manifest,
    )


def _is_backend_manifest(path: str) -> bool:
    """Whether ``path`` can change what the backend's environment resolves to.

    Not just the app's own manifest: the backend imports the vendored mngr and
    shells out to it, both as editable installs, so a vendored package's
    ``pyproject.toml`` moves their dependency closure exactly as the app's own
    does -- and is the change that actually ships in a release. Every
    ``system/vendor/mngr: refresh`` commit in this repo's history leaves
    ``uv.lock`` untouched, so keying only off the lock would miss the case this
    refresh exists for.

    Both workspace roots count, because each holds the ``[tool.uv.sources]`` and
    resolver settings its own installs go through. The repo root governs ``uv
    sync``. The *vendored* root is the one ``uv tool install -e
    system/vendor/mngr/libs/mngr`` walks up to (it declares the ``libs/*``
    workspace that package belongs to), and nothing else in this set stands in
    for it: it maps ``imbue-common`` and ``overlay`` -- which ``libs/mngr`` pins
    exactly and which resolve nowhere else -- and carries the ``exclude-newer``
    cooldown that mngr advances before each release plus its dependency
    overrides, any of which moves what the tool resolves to.
    """
    if path in (
        f"{APP_DIR}/pyproject.toml",
        "uv.lock",
        "pyproject.toml",
        f"{MNGR_VENDOR_DIR}/pyproject.toml",
    ):
        return True
    parts = path.split("/")
    return (
        len(parts) == 6
        and parts[:3] == ["system", "vendor", "mngr"]
        and parts[3] == "libs"
        and parts[5] == "pyproject.toml"
    )


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith("_test.py") or name.startswith("test_")


def find_free_port() -> int:
    """Bind to an ephemeral port, then release it for the throwaway server to take."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _diff_name_status(
    repo_root: Path, rollback_to: str, runner: Runner
) -> list[tuple[str, str]]:
    """Return ``(status, path)`` pairs for ``rollback_to..HEAD``.

    ``--no-renames`` makes a rename surface as a delete + add pair, which keeps
    the rollback logic simple (restore the deletes, remove the adds).
    """
    result = runner.run(
        ["git", "diff", "--no-renames", "--name-status", rollback_to, "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    pairs: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        pairs.append((fields[0].strip(), fields[-1].strip()))
    return pairs


def _assert_clean_tree(repo_root: Path, runner: Runner) -> None:
    result = runner.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout.strip():
        raise PreconditionError(
            "working tree has uncommitted changes; refusing to reveal so a rollback "
            "can never clobber unrelated work. Commit or stash, then re-run."
        )


def _run_checked(
    runner: Runner,
    argv: Sequence[str],
    cwd: Path,
    what: str,
    *,
    live_service_restarted: bool = False,
    env: dict | None = None,
) -> None:
    """Run a reveal command; raise :class:`RevealFailed` on a non-zero exit.

    ``live_service_restarted`` is forwarded onto the raised exception so callers
    that run the live restart can record that recovery must restart (see
    :class:`RevealFailed`)."""
    result = runner.run(
        list(argv), cwd=str(cwd), capture_output=True, text=True, check=False, env=env
    )
    if getattr(result, "returncode", 0) != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise RevealFailed(
            f"{what} failed (exit {result.returncode}): {stderr}",
            live_service_restarted=live_service_restarted,
        )


def wait_healthy(
    http: HttpClient,
    url: str,
    attempts: int,
    interval: float,
    sleeper: Callable[[float], None],
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """Poll ``url`` until it returns HTTP 200, up to ``attempts`` times.

    ``should_stop`` (the pre-flight passes its child's liveness) ends the poll
    early once there is nothing left that could turn healthy. Sitting out the
    remaining deadline would not change the verdict, and on a failed reveal every
    second of it is a second before the rollback starts.
    """
    for index in range(attempts):
        if http.get_status(url, timeout=5.0) == 200:
            return True
        if should_stop is not None and should_stop():
            return False
        if index < attempts - 1:
            sleeper(interval)
    return False


def _tail(text: str, limit: int) -> str:
    """Return the last ``limit`` lines of ``text``, noting anything dropped."""
    lines = text.strip().splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    dropped = len(lines) - limit
    return "\n".join([f"[{dropped} earlier line(s) omitted]", *lines[-limit:]])


def _preflight(
    repo_root: Path,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None],
) -> str | None:
    """Boot the merged backend on a throwaway port and probe it, without touching
    the live service.

    Returns ``None`` iff it serves a healthy response. Otherwise returns the tail
    of what the throwaway boot wrote -- the traceback for a backend that died on
    import, or how far it got before the deadline. That output is the only record
    of *why* the merged code did not come up, and the process and its scratch
    file are both gone by the time anyone reads the failure, so it has to travel
    back with the error rather than be left somewhere to look up.
    """
    port = find_free_port()
    env = dict(os.environ)
    env["SYSTEM_INTERFACE_HOST"] = "127.0.0.1"
    env["SYSTEM_INTERFACE_PORT"] = str(port)
    with tempfile.TemporaryDirectory() as scratch:
        output_path = Path(scratch) / "preflight-boot.log"
        spawned = spawner.spawn(
            [TOOL_NAME],
            cwd=str(repo_root / APP_DIR),
            env=env,
            output_path=output_path,
        )
        try:
            if wait_healthy(
                http,
                f"http://127.0.0.1:{port}{HEALTH_PATH}",
                _PREFLIGHT_ATTEMPTS,
                _PREFLIGHT_INTERVAL_SECONDS,
                sleeper,
                should_stop=spawned.has_exited,
            ):
                return None
        finally:
            spawned.terminate()
        return _tail(spawned.read_output(), _PREFLIGHT_OUTPUT_TAIL_LINES)


def _refresh_workspace_view(repo_root: Path, runner: Runner) -> None:
    """Ask every open view of this workspace to reload the changed interface.

    Delegates to the shared ``refresh_workspace_view.py`` helper, which fires
    both the in-workspace reload broadcast (reaching browsers we cannot address
    directly, including shared tunnel viewers) and the Minds app's refresh
    endpoint (which works when the frontend's WebSocket never came back from the
    restart, because it does not go through the workspace server at all).

    Best-effort and never fatal: the helper always exits 0 and names any channel
    that did not land on stderr, which we pass through. The change is already on
    disk and will load on the next visit regardless. A helper we cannot even
    spawn (no memory to fork right after the restart) is caught here for the
    same reason: both callers run this once the reveal -- or the rollback
    recovery -- has already succeeded, and neither treats it as a step that can fail.

    ``UnicodeDecodeError`` is in that group because capturing text output decodes
    it, and output the stdio encoding cannot decode is a ``ValueError`` rather
    than a ``SubprocessError`` -- the same escape the helper's own ``_run_channel``
    guards against on its side of the boundary.
    """
    try:
        completed = runner.run(
            [sys.executable, str(repo_root / _REFRESH_SCRIPT)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_REFRESH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        sys.stderr.write(
            f"refresh: could not run {_REFRESH_SCRIPT} ({type(exc).__name__}: {exc}); "
            "an open view may still be showing the previous build until reloaded.\n"
        )
        return
    if completed.stderr:
        sys.stderr.write(completed.stderr)


def _tool_location(script: Path, tool_name: str) -> tuple[Path, Path] | None:
    """Return ``(tool_dir, bin_dir)`` for the tool that owns console ``script``.

    We have to ask rather than let uv default, because uv's tool directory
    follows ``$HOME`` and the workspace runs with a ``$HOME`` that is not the one
    ``build_workspace.sh`` installed under (it runs as root at image build; the
    workspace then runs as root with ``HOME=/home/user``). Defaulting therefore
    rebuilds a *second*, shadow installation that nothing on PATH executes --
    leaving the tool everyone actually runs exactly as stale as before, while
    every command reports success.

    uv writes each console script with a shebang naming the interpreter inside
    that tool's own environment
    (``#!/root/.local/share/uv/tools/imbue-mngr/bin/python3``), so the script we
    are about to re-resolve tells us precisely which installation it belongs to.
    ``None`` means we could not confirm one, and the caller lets uv default.
    Confirming matters as much as finding: these names also exist in the
    workspace venv (both are ``uv sync --all-packages`` members), and a venv
    console script would yield a "tool directory" inside the repo tree -- so we
    would build a tool environment into the served checkout, dirty the tree that
    the next reveal refuses to run on, and overwrite the venv's own entrypoint.
    The receipt is what makes it a uv tool, so we require it.
    """
    try:
        shebang = script.read_text(errors="replace").split("\n", 1)[0]
    except OSError:
        return None
    if not shebang.startswith("#!"):
        return None
    interpreter = shebang[2:].strip().split(" ", 1)[0]
    if not interpreter:
        return None
    # <tool_dir>/<tool_name>/bin/python -> <tool_dir>
    parents = Path(interpreter).parents
    if len(parents) < 3:
        return None
    tool_dir = parents[2]
    if not (tool_dir / tool_name / _RECEIPT).is_file():
        return None
    return tool_dir, script.parent


def _uv_tool_env(executable: str, tool_name: str, runner: Runner) -> dict:
    """The environment for a ``uv tool`` call, aimed at ``executable``'s own
    installation when we can confirm which that is.

    Falling back to uv's default is the safe answer but not a good outcome --
    it is how the refresh came to rebuild a copy nothing runs -- so say so
    rather than let it pass silently.
    """
    env = dict(os.environ)
    found = runner.which(executable)
    location = _tool_location(Path(found), tool_name) if found is not None else None
    if location is None:
        sys.stderr.write(
            f"refresh: could not identify the uv tool behind '{executable}'"
            f" ({found or 'not on PATH'}); letting uv choose the tool directory,"
            " which may rebuild a copy that is not the one being run.\n"
        )
        return env
    env["UV_TOOL_DIR"] = str(location[0])
    env["UV_TOOL_BIN_DIR"] = str(location[1])
    return env


def _tool_extras(
    tool_name: str, repo_root: Path, runner: Runner, env: dict
) -> list[str]:
    """Return the ``--with``/``--with-editable`` args a tool was installed with.

    A ``uv tool install --reinstall`` rebuilds the environment from the base
    package alone, dropping every extra -- for the mngr tool those extras *are*
    its plugins, so dropping them leaves a CLI that cannot parse its own plugin
    config. uv records them in the tool's ``uv-receipt.toml``, so we read them
    back and pass them through rather than keeping a second copy of the plugin
    list here for build_workspace.sh's to drift away from.

    A tool with no receipt at all contributes no extras: it is not installed (or
    predates the receipt), and the reinstall below is then the plain install it
    would have gotten anyway. A receipt we cannot *read* is a different story --
    we had a tool and lost the record of it -- so that degrades to the same empty
    answer but says so, because the silent version of it hands back exactly the
    plugin-less CLI this refresh exists to prevent, while reporting success.
    """
    tool_dir = env.get("UV_TOOL_DIR")
    if tool_dir is None:
        result = runner.run(
            ["uv", "tool", "dir"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if getattr(result, "returncode", 0) != 0:
            _warn_extras_lost(tool_name, f"'uv tool dir' exited {result.returncode}")
            return []
        tool_dir = (getattr(result, "stdout", "") or "").strip()
    receipt = Path(tool_dir) / tool_name / _RECEIPT
    try:
        parsed = tomllib.loads(receipt.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        if receipt.is_file():
            _warn_extras_lost(tool_name, f"{receipt} is unreadable ({exc})")
        return []
    extras: list[str] = []
    for requirement in parsed.get("tool", {}).get("requirements", []):
        name = requirement.get("name", "")
        if _canonical(name) == _canonical(tool_name):
            continue  # the base package, which we re-pin to its in-tree source
        editable = requirement.get("editable") or requirement.get("directory")
        if editable:
            extras.extend(["--with-editable", editable])
        elif requirement.get("git"):
            extras.extend(["--with", f"{name} @ git+{requirement['git']}"])
        elif requirement.get("specifier"):
            extras.extend(["--with", f"{name}{requirement['specifier']}"])
        else:
            extras.extend(["--with", name])
    return extras


def _warn_extras_lost(tool_name: str, why: str) -> None:
    """Report that ``tool_name`` is about to be rebuilt without its extras."""
    sys.stderr.write(
        f"refresh: cannot read what '{tool_name}' was installed with ({why}); "
        "reinstalling from the base package alone, which drops any plugins it "
        "had registered.\n"
    )


def _canonical(name: str) -> str:
    """Normalize a package name the way packaging does, for comparison."""
    return name.replace("_", "-").lower()


def _reinstall_tool(
    tool_name: str, executable: str, source_dir: str, repo_root: Path, runner: Runner
) -> None:
    """Re-resolve the installed ``executable``'s tool from its in-tree source,
    keeping the extras it was installed with.

    The base is re-pinned to ``source_dir`` rather than left to the receipt: a
    receipt that has lost its editable marker would otherwise resolve the base
    from the index, quietly swapping the workspace's own vendored code for a
    published release.
    """
    env = _uv_tool_env(executable, tool_name, runner)
    _run_checked(
        runner,
        [
            "uv",
            "tool",
            "install",
            "-e",
            source_dir,
            *_tool_extras(tool_name, repo_root, runner, env),
            "--reinstall",
        ],
        repo_root,
        f"uv tool install {tool_name} --reinstall",
        env=env,
    )


def _refresh_dependencies(changes: ChangeSet, repo_root: Path, runner: Runner) -> None:
    """Rebuild the environments the served backend runs from, mirroring
    ``system/scripts/build_workspace.sh`` -- which is the source of truth for how
    they are constructed, and which refreshes all three.

    All three, because ``supervisord`` starts the backend as a bare
    ``system-interface`` off PATH: which of the tool install and the workspace
    venv answers that is a PATH question, and the backend shells out to ``mngr``
    besides. Refreshing only one of them leaves a merge half-applied in a way
    that surfaces as a crash somewhere else entirely.
    """
    if changes.frontend_manifest:
        _run_checked(runner, ["npm", "ci"], repo_root / FRONTEND_DIR, "npm ci")
    if changes.backend_manifest:
        _reinstall_tool(MNGR_TOOL_NAME, MNGR_EXECUTABLE, MNGR_DIR, repo_root, runner)
        _reinstall_tool(TOOL_NAME, TOOL_NAME, APP_DIR, repo_root, runner)
        _run_checked(
            runner,
            ["uv", "sync", "--all-packages", "--frozen"],
            repo_root,
            "uv sync --all-packages --frozen",
        )


def _apply_reveal(
    changes: ChangeSet,
    repo_root: Path,
    base_url: str,
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None],
) -> None:
    """Refresh deps, build, restart, and reload as applicable. Raises
    :class:`RevealFailed` the moment any step does not end healthy."""
    _refresh_dependencies(changes, repo_root, runner)
    if changes.frontend:
        _run_checked(
            runner, ["npm", "run", "build"], repo_root / FRONTEND_DIR, "npm run build"
        )
    if changes.backend:
        preflight_output = _preflight(repo_root, http, spawner, sleeper)
        if preflight_output is not None:
            # Live service was never restarted, so it is still serving known-good
            # code -- recovery must not restart it (live_service_restarted=False).
            detail = (
                f"\n--- pre-flight boot output ---\n{preflight_output}"
                if preflight_output
                else "\n(the pre-flight boot wrote nothing at all)"
            )
            raise RevealFailed(
                "merged backend failed to boot in a pre-flight check; live service "
                f"not restarted{detail}"
            )
        # From here on the live service has been (or is being) restarted, so any
        # failure leaves it potentially running broken code: recovery must restart.
        _run_checked(
            runner,
            ["mngr", "start", "--restart", "system-services"],
            repo_root,
            "mngr start --restart",
            live_service_restarted=True,
        )
        if not wait_healthy(
            http,
            f"{base_url}{HEALTH_PATH}",
            _HEALTH_ATTEMPTS,
            _HEALTH_INTERVAL_SECONDS,
            sleeper,
        ):
            raise RevealFailed(
                "backend did not become healthy after restart",
                live_service_restarted=True,
            )
    # Unconditional: this runs only when something changed (``reveal`` returns
    # early otherwise), and a BACKEND-only change needs the reload just as much
    # as a frontend one. The restart bounces the API underneath a page that
    # keeps rendering from whatever it had already fetched, and a restart quick
    # enough not to look unreachable never triggers a reload from anywhere else.
    _refresh_workspace_view(repo_root, runner)


def _restore_tree(
    name_status: Sequence[tuple[str, str]],
    rollback_to: str,
    repo_root: Path,
    runner: Runner,
) -> None:
    """Restore every changed path to its ``rollback_to`` state, staged for commit.

    Added-since paths are removed; modified/deleted paths are checked out from
    the known-good revision. Build output is gitignored and untouched here -- the
    recovery rebuild regenerates it.
    """
    for status, path in name_status:
        if status.startswith("A"):
            runner.run(
                ["git", "rm", "--force", "--ignore-unmatch", path],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
            )
        else:
            runner.run(
                ["git", "checkout", rollback_to, "--", path],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
            )


def _commit_rollback(
    repo_root: Path, runner: Runner, rollback_to: str, reason: str
) -> None:
    message = (
        f"Roll back system-interface reveal (restore to {rollback_to[:12]})\n\n{reason}"
    )
    runner.run(
        ["git", "commit", "--no-verify", "-m", message],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )


def _recover_running_state(
    changes: ChangeSet,
    repo_root: Path,
    base_url: str,
    runner: Runner,
    http: HttpClient,
    sleeper: Callable[[float], None],
    live_service_restarted: bool,
) -> bool:
    """After the tree is restored to known-good, rebuild/restart from it as
    needed and confirm the live UI is healthy. Returns True iff confirmed healthy.

    ``live_service_restarted`` says whether the failed reveal had already
    restarted the live backend. When it did not (pre-flight / dependency-refresh
    / frontend-build failures), the live service is still running known-good code
    in memory and the on-disk tree has just been restored to match it, so we must
    NOT restart -- doing so would needlessly blip a healthy UI. We only restart
    when the failed reveal had actually restarted the service into broken code.

    Unlike :func:`_apply_reveal`, nothing here raises -- this is the last line of
    defense, so a failed step just means "not recovered" (exit 3)."""
    try:
        _refresh_dependencies(changes, repo_root, runner)
        if changes.frontend:
            _run_checked(
                runner,
                ["npm", "run", "build"],
                repo_root / FRONTEND_DIR,
                "npm run build",
            )
        if changes.backend:
            if live_service_restarted:
                _run_checked(
                    runner,
                    ["mngr", "start", "--restart", "system-services"],
                    repo_root,
                    "mngr start --restart",
                )
            # Probe the backend health endpoint either way: after a restart to
            # confirm known-good booted, or (no restart) to confirm the untouched
            # service is still serving.
            healthy = wait_healthy(
                http,
                f"{base_url}{HEALTH_PATH}",
                _HEALTH_ATTEMPTS,
                _HEALTH_INTERVAL_SECONDS,
                sleeper,
            )
        else:
            # Frontend-only: the server was never restarted; confirm it still serves.
            healthy = wait_healthy(
                http,
                f"{base_url}{SERVE_PATH}",
                _HEALTH_ATTEMPTS,
                _HEALTH_INTERVAL_SECONDS,
                sleeper,
            )
    except RevealFailed as exc:
        sys.stderr.write(f"recovery step failed: {exc}\n")
        return False
    # Same reasoning as the reveal path: the rolled-back tree is a change to
    # whatever the open view is currently rendering, whichever side it touched.
    if healthy:
        _refresh_workspace_view(repo_root, runner)
    return healthy


def reveal(
    rollback_to: str,
    repo_root: Path,
    *,
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None] = time.sleep,
    base_url: str | None = None,
) -> int:
    """Run the full reveal-and-recover sequence. Returns the process exit code."""
    resolved_base = (
        base_url or os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL)
    ).rstrip("/")
    _assert_clean_tree(repo_root, runner)
    name_status = _diff_name_status(repo_root, rollback_to, runner)
    changes = classify_changes([path for _, path in name_status])
    if not changes.any:
        sys.stderr.write(
            f"no system-interface changes since {rollback_to[:12]}; nothing to reveal.\n"
        )
        return 0

    try:
        _apply_reveal(changes, repo_root, resolved_base, runner, http, spawner, sleeper)
    except RevealFailed as exc:
        sys.stderr.write(
            f"reveal failed: {exc}\nrolling back to {rollback_to[:12]} and restoring the live UI...\n"
        )
        _restore_tree(name_status, rollback_to, repo_root, runner)
        _commit_rollback(
            repo_root,
            runner,
            rollback_to,
            f"Reveal failed and was auto-reverted: {exc}",
        )
        if _recover_running_state(
            changes,
            repo_root,
            resolved_base,
            runner,
            http,
            sleeper,
            live_service_restarted=exc.live_service_restarted,
        ):
            sys.stderr.write(
                "rolled back to last-known-good; the live UI is confirmed healthy. "
                "The requested change did NOT land -- diagnose it before retrying.\n"
            )
            return 2
        sys.stderr.write(
            "EMERGENCY: rollback did not restore a healthy UI. The system interface may be down; "
            "manual intervention is required.\n"
        )
        return 3

    sys.stderr.write(
        "revealed: the live system interface is updated and confirmed healthy.\n"
    )
    return 0


def _preview_instance_name(slug: str) -> str:
    """The name the shared script files this preview's instance under (its state
    dir + the stable id ``unpreview`` tears down). One preview per slug."""
    return f"{PREVIEW_SERVICE_NAME}-{slug}"


def _find_other_preview(repo_root: Path, slug: str) -> str | None:
    """Return another slug's live preview instance name, or ``None``.

    Only a *different* slug's preview blocks: both would register the same fixed
    service names, so booting a second one hijacks the first's tab. Re-running
    the same slug stays allowed -- the shared script clears its own stale
    instance, which is the normal retry path.
    """
    instances_root = repo_root / _INSTANCES_ROOT
    if not instances_root.is_dir():
        return None
    own_name = _preview_instance_name(slug)
    prefix = f"{PREVIEW_SERVICE_NAME}-"
    for state_dir in sorted(instances_root.iterdir()):
        if not state_dir.name.startswith(prefix) or state_dir.name == own_name:
            continue
        if (state_dir / _INSTANCE_STATE_FILENAME).exists():
            return state_dir.name
    return None


def preview(slug: str, work_dir: str, repo_root: Path, *, runner: Runner) -> int:
    """Stand up a pre-merge preview of the worker's ``work_dir``.

    Thin system-interface adapter over the shared ``serve_isolated_instance.py``
    ``up`` motion: validate the worker's app dir, require that the worker built its
    frontend bundle, then hand the shared script the system-interface specifics --
    boot ``uv run system-interface`` from the worker's already-built app dir on a
    free port; neuter layout persistence by dropping MNGR_AGENT_ID (so the preview
    can't clobber the live ``layout.json``) while keeping discovery, so the real
    conversations still render; probe ``/api/agents``; register the inner app and
    the labeled wrapper frame. The shared script owns the ports, the
    process/service teardown, and the state file. ``work_dir`` must still exist --
    run this before the worker is destroyed.
    """
    # Sanity-check the work_dir before disturbing anything: a wrong --work-dir
    # should fail fast rather than reaching the shared script.
    worker_app_dir = Path(work_dir) / APP_DIR
    if not worker_app_dir.is_dir():
        sys.stderr.write(
            f"preview: {worker_app_dir} is not a directory; is --work-dir correct "
            "and is the worker still alive (not destroyed)?\n"
        )
        return 1
    # The preview serves the worker's app dir as-is; it does not build for the
    # worker. A work_dir without a frontend bundle means the worker reported done
    # without building it (a fresh worktree has no gitignored static/ until built),
    # so booting would only serve the backend's "Frontend not built" placeholder --
    # a dead preview that reads as working. Refuse loudly and point at the fix: the
    # worker must build before it is previewable.
    if not (Path(work_dir) / FRONTEND_BUILD_INDEX).exists():
        sys.stderr.write(
            f"preview: no frontend build in {work_dir} "
            f"({FRONTEND_BUILD_INDEX} is missing), so the preview would serve the "
            "'Frontend not built' placeholder. The worker must build the frontend "
            "(cd system/apps/system_interface/frontend && npm ci && npm run build) before "
            "its work_dir can be previewed -- re-brief it to build, then retry.\n"
        )
        return 1
    other = _find_other_preview(repo_root, slug)
    if other is not None:
        other_slug = other.removeprefix(f"{PREVIEW_SERVICE_NAME}-")
        sys.stderr.write(
            f"preview: another pass's preview is already up ({other}); the "
            f"'{PREVIEW_SERVICE_NAME}' tab can only show one at a time, so booting "
            "this one would hijack it. Surface this to the user and coordinate "
            "with that pass -- or, if it is abandoned, tear it down first with "
            f"'unpreview --slug {other_slug}'.\n"
        )
        return 1
    result = runner.run(
        [
            sys.executable,
            str(_SHARED_SERVE_SCRIPT),
            "up",
            "--name",
            _preview_instance_name(slug),
            "--cwd",
            str(worker_app_dir),
            "--port-env",
            PREVIEW_PORT_ENV,
            "--host-env",
            PREVIEW_HOST_ENV,
            "--unset-env",
            ENV_MNGR_AGENT_ID,
            "--health-path",
            HEALTH_PATH,
            "--service-name",
            PREVIEW_INNER_SERVICE_NAME,
            "--preview-service-name",
            PREVIEW_SERVICE_NAME,
            "--preview-title",
            slug,
            "--repo-root",
            str(repo_root),
            "--",
            "uv",
            "run",
            TOOL_NAME,
        ],
        cwd=str(repo_root),
        check=False,
    )
    return int(getattr(result, "returncode", 0))


def unpreview(slug: str, repo_root: Path, *, runner: Runner) -> int:
    """Tear down the preview for ``slug`` via the shared script. Idempotent: a
    missing instance is a no-op success, so this is safe on reject, after a
    successful reveal, or to recover from a half-set-up preview."""
    result = runner.run(
        [
            sys.executable,
            str(_SHARED_SERVE_SCRIPT),
            "down",
            "--name",
            _preview_instance_name(slug),
            "--repo-root",
            str(repo_root),
        ],
        cwd=str(repo_root),
        check=False,
    )
    return int(getattr(result, "returncode", 0))


def _add_repo_root_arg(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root (default: current directory).",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Manage the system-interface update lifecycle: preview a worker "
            "branch before merging, reveal a merged change with auto-recovery, "
            "and tear the preview down."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    reveal_parser = subparsers.add_parser(
        "reveal", help="Reveal a merged change to the live UI, with auto-recovery."
    )
    reveal_parser.add_argument(
        "--rollback-to",
        required=True,
        help="The known-good revision to restore to if the reveal fails (the pre-merge HEAD).",
    )
    _add_repo_root_arg(reveal_parser)

    preview_parser = subparsers.add_parser(
        "preview",
        help="Boot the worker's already-built work_dir and serve it as a "
        "previewable tab, before any merge.",
    )
    preview_parser.add_argument(
        "--slug",
        required=True,
        help="Short kebab-case id for this preview (names the service/state dir).",
    )
    preview_parser.add_argument(
        "--work-dir",
        required=True,
        help="The worker's work_dir (from `mngr ls --include 'name==\"<worker>\"' "
        "--format json` -> agent.work_dir). The worker must still exist.",
    )
    _add_repo_root_arg(preview_parser)

    unpreview_parser = subparsers.add_parser(
        "unpreview",
        help="Tear down a preview (kill the server, deregister the service). Idempotent.",
    )
    unpreview_parser.add_argument(
        "--slug", required=True, help="The slug passed to 'preview'."
    )
    _add_repo_root_arg(unpreview_parser)

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    try:
        if args.command == "reveal":
            return reveal(
                args.rollback_to,
                repo_root,
                runner=Runner(),
                http=HttpClient(),
                spawner=Spawner(),
            )
        if args.command == "preview":
            return preview(
                args.slug,
                args.work_dir,
                repo_root,
                runner=Runner(),
            )
        if args.command == "unpreview":
            return unpreview(args.slug, repo_root, runner=Runner())
        parser.error(f"unknown command: {args.command}")
        return 1
    except PreconditionError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"error: git command failed: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
