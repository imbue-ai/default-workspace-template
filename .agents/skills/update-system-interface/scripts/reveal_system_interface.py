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
3. Snapshot the built ``static/`` bundle. Both destructive steps below delete
   before they produce (``npm ci`` removes ``node_modules``; the build empties
   the bundle directory), so without a copy taken first, a failure part-way
   leaves nothing to serve and recovery has only the failed build to retry.
4. Refresh dependencies only if a manifest changed (``npm ci``, then the same
   tool installs and ``uv sync`` that ``build_workspace.sh`` does). A plain
   restart does NOT re-resolve an editable install's dependencies, so a backend
   dependency add would otherwise crash the service on restart -- and an
   editable install pins only the source path, so this bites the vendored mngr
   the backend shells out to just as hard as the backend itself.
5. Build the frontend bundle, if the change touched the frontend. A build that
   exits 0 without writing a bundle counts as a failure, not a success. Ahead of
   the pre-flight below, so a change whose build does not compile is rejected
   without also spending a throwaway boot on it; the cost is that a *combined*
   change whose backend then fails to boot has already replaced the bundle, and
   the rollback restores it from the snapshot taken in step 3.
6. For a backend change, *pre-flight* the merged code on a throwaway port before
   touching the live service -- if it cannot boot, the live service is never
   restarted and we go straight to rollback (the UI never went down). The
   throwaway boot's output rides back on the failure, because "it did not boot"
   without the traceback sends whoever reads it guessing at a cause. Then
   restart the backend, so it re-imports the merged code.
7. Probe the live service's loopback endpoint until healthy (with a deadline),
   and confirm the app shell really is the built app and that its module script
   serves as JavaScript. The backend endpoint alone cannot see either failure:
   the placeholder page and an unserved ``/assets`` path are both HTTP 200s to
   it. This is scoped to a *regression* -- the same probe runs before the reveal
   too, and only a frontend that was serving beforehand has to be serving after.
   A workspace that arrived already broken gets the finding reported instead,
   because rolling an unrelated change back would not fix it -- reported both as
   a warning and in place of the closing "confirmed healthy" line, which must
   never be the last word over a UI the user cannot see.
8. Ask every open view of the workspace to reload, via
   ``system/scripts/refresh_workspace_view.py`` -- for a backend-only change
   too, since the restart leaves the open page rendering from what it had
   already fetched. After the probes above, so a reveal that regressed the
   frontend rolls back rather than asking every open view to reload into it.
9. On ANY failure, restore the served tree to the known-good revision (as a
   forward revert commit), put the snapshotted bundle back, and re-probe to
   *confirm* the UI is back. Restoring the snapshot needs neither ``npm`` nor a
   registry, so a broken build environment cannot take the UI down with it; a
   rebuild is attempted only when there is no snapshot to put back -- either
   there was no bundle to copy, or the copy could not be taken. The
   live backend is restarted during recovery only if the failed reveal had
   already restarted it (a failed post-restart health check); when the failure
   happened before the live restart (pre-flight, dependency refresh, frontend
   build) the live service is still serving known-good code and is left
   untouched, so the UI never blips. Only then does the script exit -- reporting
   what happened via its exit code and stderr.

Run via bare ``python3`` (self-contained beyond the stdlib-only ``oom_priority``
package, imported via a ``sys.path`` insert), like ``forward_port.py`` and
``reload_system_interface``'s predecessor -- it orchestrates the environment, so
it must not depend on any particular venv being synced.

Memory bands. Run from an agent's shell, this process and everything it spawns
inherit ``AGENT_SUBPROCESS`` -- the most expendable band there is, above every
chat and every agent. That is right for the build and wrong for the ``reveal``
orchestrator: a shed build is an ordinary failure the rollback below absorbs,
whereas shedding *that* process skips the rollback entirely, leaving a
half-applied tree, an emptied bundle directory and the only surviving copy of the
bundle orphaned in a temporary directory nobody knows the path of. So a ``reveal``
bands itself down to the system interface's own band -- it is that service's
maintenance operation, it holds the only copy of what that service serves, and
shedding it frees a few megabytes -- and bands its hungry children (``npm``,
``uv tool install``, the pre-flight boot) back up to ``AGENT_SUBPROCESS`` on the
way out, so nothing it spawns inherits the protection meant for the orchestrator
alone. Only ``reveal``: ``preview`` detaches a second copy of the whole system
interface that outlives the invocation and would inherit the band with it, and
neither it nor ``unpreview`` has a rollback the protection would be buying. This
is a steer rather than a guarantee (earlyoom folds live memory in on top of the
band), and it only covers earlyoom: a container stop or a kernel OOM kills this
process whatever its band says.

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
       on the known-good revision (the requested change did NOT land). As with
       0, a workspace whose frontend was already broken when the reveal started
       gets a closing line that says so instead, since the rollback was never
       held to a standard it could not have met.
    3  EMERGENCY: even rollback could not restore a healthy UI. When a reveal
       that touched the frontend has a bundle copy, it is kept rather than
       discarded and its path printed: putting it back needs neither npm nor a
       registry, so it is the way out of exactly the failure that gets here.
       There is no such line without a copy to hand over -- a backend-only
       reveal never wrote the bundle directory, and a frontend one has no copy
       when there was no bundle to take one of or the copy itself failed.

Exit codes (``preview`` / ``unpreview``):
    0  Success (preview is up / torn down).
    1  The preview failed to build or boot (and tore itself down), or a bad
       argument / unreadable state file.
"""

from __future__ import annotations

import argparse
import os
import re
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

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[4]
        / "system"
        / "services"
        / "oom_priority"
        / "src"
    ),
)

from oom_priority import bands

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
STATIC_DIR = f"{APP_DIR}/imbue/system_interface/static"
FRONTEND_BUILD_INDEX = f"{STATIC_DIR}/index.html"
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

# Header the backend stamps on the app shell: ``false`` on the "not built"
# placeholder, ``true`` on the real app. Checked rather than string-matching the
# placeholder's markup, so the probe does not silently stop working when that
# page is restyled.
FRONTEND_BUILT_HEADER = "x-frontend-built"
# The hashed module script the built index.html loads. Its presence is what
# distinguishes the real app shell from the placeholder even on a backend too
# old to send the header above.
_ASSET_REFERENCE_PATTERN = re.compile(r"/assets/([A-Za-z0-9._-]+\.js)")

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
# Budget for the frontend probe, which retries only a *non-answer* (see
# :func:`_probe_frontend_until_answered`). Deliberately fewer attempts than the
# health budget: a service that is down refuses immediately, so this rides out a
# blip in about four seconds rather than holding up a decision -- to arm the
# check, to roll back, to escalate -- that the reveal has to reach either way.
_FRONTEND_PROBE_ATTEMPTS = 5
_FRONTEND_PROBE_INTERVAL_SECONDS = 1.0

# How much of a failed pre-flight boot's output rides back on the error. Enough
# for a Python traceback plus the log lines leading into it; the rest is startup
# chatter that pushed the interesting part off the top.
_PREFLIGHT_OUTPUT_TAIL_LINES = 40

# The band this process bands itself into (see the module docstring): the same
# one the service it is maintaining runs in. Not a lower one -- the reveal is
# worth exactly as much as what it is repairing, and the bands below it are the
# two authority paths (owner-exec, the terminal) that are how anything gets
# fixed once this has failed.
_ORCHESTRATOR_BAND = bands.SERVICE_BANDS["system_interface"]

# Tag into the most expendable band, then become the real command, for the
# children that must NOT inherit the protection above. ``sh -c <script> sh
# <argv...>`` puts ``argv`` in ``"$@"``, so ``exec`` replaces the shell with the
# command rather than leaving one wrapping it -- which also keeps a terminate
# aimed at the child itself (see :meth:`Spawned.terminate`).
_EXPENDABLE_TAG_THEN_EXEC = (
    bands.oom_tag_shell_prefix(bands.AGENT_SUBPROCESS) + 'exec "$@"'
)


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

    ``detail`` is captured output explaining the failure (the pre-flight boot's
    own log). It is kept apart from the message because the two readers want
    different amounts of it: stderr gets all of it, while the rollback commit
    gets only :meth:`headline` -- a commit body is read by everyone who ever
    looks at this file's history, and a backend's startup log is a poor thing to
    write into git and push, since it holds whatever the process chose to print.
    """

    def __init__(
        self,
        message: str,
        *,
        live_service_restarted: bool = False,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.live_service_restarted = live_service_restarted
        self.detail = detail

    def headline(self) -> str:
        """The message plus only the last line of ``detail``.

        That last line is the payload -- a traceback ends on the exception, which
        is the part that names the cause -- while everything above it is the
        startup chatter that led there, already on stderr for whoever is looking.
        """
        last = next(
            (
                line
                for line in reversed(self.detail.strip().splitlines())
                if line.strip()
            ),
            "",
        )
        return f"{self}: {last}" if last else str(self)


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


@dataclass(frozen=True)
class FetchedPage:
    """A fetched response body plus the headers the frontend probe reads."""

    status: int
    body: str
    # Lower-cased header names, so callers need not care about the wire casing.
    headers: dict[str, str]

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


@dataclass(frozen=True)
class FrontendProbe:
    """What the live UI said when asked whether it serves a working frontend."""

    # Why it is not serving one, or ``None`` when it is.
    failure: str | None
    # False when the service did not answer at all, so ``failure`` records a
    # non-answer rather than a verdict about the frontend.
    is_answered: bool


class HttpClient:
    """Indirection over the loopback probes: the health checks (live service +
    pre-flight boot) and the frontend probe's page fetches."""

    def get_status(self, url: str, timeout: float) -> int | None:
        """Return the HTTP status for a GET, or ``None`` if the host is unreachable."""
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (urllib.error.URLError, OSError):
            return None

    def get_page(self, url: str, timeout: float) -> FetchedPage | None:
        """Fetch a GET with its body and headers, or ``None`` if unreachable.

        Used by the frontend probe, which has to look at what came back rather
        than only whether something did: the "frontend not built" placeholder is
        a perfectly healthy HTTP 200.
        """
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                return FetchedPage(
                    status=int(response.status), body=body, headers=headers
                )
        except urllib.error.HTTPError as exc:
            return FetchedPage(status=int(exc.code), body="", headers={})
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
        elif path.startswith(f"{FRONTEND_DIR}/"):
            # Everything else under frontend/ counts, not just src/: index.html,
            # vite.config.ts, tsconfig.json and the public assets all change the
            # emitted bundle, so anything narrower misses a change and leaves the
            # merged tree serving a stale bundle. A tooling-only config (eslint,
            # prettier) costs a redundant rebuild here, which is far cheaper than
            # a missed one.
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


def as_expendable(argv: Sequence[str]) -> list[str]:
    """Wrap ``argv`` so it runs in the most expendable band rather than this one.

    For the steps that actually hold memory -- the npm commands, the editable
    tool reinstall, the pre-flight boot. Losing one of those is a failure this
    script recovers from; losing this script is not (see the module docstring),
    so the protection it gives itself must not reach them by inheritance.
    """
    return ["sh", "-c", _EXPENDABLE_TAG_THEN_EXEC, "sh", *argv]


def _is_shed_protected_command(argv: Sequence[str]) -> bool:
    """Whether ``argv`` names the subcommand that must survive a memory shed.

    Only ``reveal`` does. It is the one that can be interrupted half-way through
    replacing what the service serves, and the one holding the sole copy of the
    bundle. ``preview`` hands the shared serve script a *detached* second copy of
    the system interface that outlives this invocation, so banding here would
    leave that throwaway server protected ahead of every chat and every agent for
    as long as it runs; ``unpreview`` only tears one down.
    """
    return bool(argv) and argv[0] == "reveal"


def _protect_from_memory_shed() -> None:
    """Band this process out of the agent-subprocess range it was launched in.

    Best-effort by construction: ``bands.set_oom_score_adj`` reports a refusal
    rather than raising, and a reveal that cannot be protected is still a reveal
    worth running. Called from ``__main__`` rather than from :func:`main` so that
    exercising the entry point in a test cannot re-band the test runner.
    """
    if not bands.set_oom_score_adj(os.getpid(), _ORCHESTRATOR_BAND):
        sys.stderr.write(
            "warning: could not lower this process's memory-shed priority; a shed "
            "during the reveal would skip the rollback.\n"
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


def _detail_block(detail: str) -> str:
    """Render captured failure output for stderr, or nothing when there is none."""
    return f"--- pre-flight boot output ---\n{detail}\n" if detail else ""


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
            as_expendable([TOOL_NAME]),
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


def snapshot_bundle(repo_root: Path) -> Path | None:
    """Copy the built bundle somewhere safe before anything can destroy it.

    Both destructive steps of a reveal delete before they produce -- ``npm ci``
    removes ``node_modules`` first and the build empties the bundle directory
    first -- so a failure part-way leaves the workspace with no UI at all. A
    *recipe* for the known-good bundle is not a substitute for a copy of it,
    because the recipe is re-running the very build that just failed.

    Returns ``None`` when there is no bundle yet, which is not an error: a
    workspace that never built one has nothing to lose. A copy that cannot be
    taken (no space, no permission) returns ``None`` for the same reason rather
    than raising: this is a precaution, and refusing to reveal because the
    precaution failed is worse than revealing without one -- recovery then falls
    back to rebuilding.
    """
    bundle = repo_root / STATIC_DIR
    if not (bundle / "index.html").exists():
        # Said out loud for the same reason the copy failure below is: the
        # emergency path tells its reader to check the stderr for why there is
        # no snapshot to hand over, so both reasons have to leave a line here.
        sys.stderr.write(
            "note: no built bundle to copy aside (none has been built yet); a "
            "failed reveal will have to build one to recover.\n"
        )
        return None
    # Deliberately outside the repo: a stray directory inside it would dirty the
    # tree and trip the next reveal's clean-tree precondition.
    snapshot_root: Path | None = None
    try:
        snapshot_root = Path(tempfile.mkdtemp(prefix="system-interface-bundle-"))
        saved = snapshot_root / "static"
        shutil.copytree(bundle, saved)
    except OSError as exc:
        # A half-written copy has to go with it. The documented reasons to land
        # here are a full or read-only disk, so leaving megabytes of partial
        # bundle behind on every attempt would make the next one likelier to
        # fail for the same reason.
        if snapshot_root is not None:
            shutil.rmtree(snapshot_root, ignore_errors=True)
        sys.stderr.write(
            f"warning: could not copy the built bundle aside ({type(exc).__name__}: {exc}); "
            "a failed reveal will have to rebuild it to recover.\n"
        )
        return None
    return saved


def restore_bundle(saved: Path, repo_root: Path) -> None:
    """Put a snapshotted bundle back, replacing whatever is there now."""
    bundle = repo_root / STATIC_DIR
    if bundle.exists():
        shutil.rmtree(bundle)
    shutil.copytree(saved, bundle)


def _discard_snapshot(saved: Path | None) -> None:
    if saved is not None:
        shutil.rmtree(saved.parent, ignore_errors=True)


def _assert_bundle_built(repo_root: Path, *, live_service_restarted: bool) -> None:
    """Raise unless the build actually left a servable bundle behind.

    A build tool that empties its output directory and then exits 0 without
    writing (killed mid-run, a plugin that swallowed its own error) passes an
    exit-code check while leaving nothing to serve. Treating that as success is
    what turns a bad build into a blank UI.
    """
    index = repo_root / FRONTEND_BUILD_INDEX
    if not index.exists():
        raise RevealFailed(
            f"the frontend build reported success but wrote no bundle ({index} is missing)",
            live_service_restarted=live_service_restarted,
        )


def probe_frontend(http: HttpClient, base_url: str) -> FrontendProbe:
    """Ask the live UI whether it is serving a working frontend.

    The backend health endpoint cannot answer this: ``/api/agents`` is happy
    while the bundle is missing, and the placeholder page the user gets instead
    is itself an HTTP 200. So this asks the two questions a browser would --
    is this the real app shell, and does its module script actually load as
    JavaScript -- which together cover both the missing-bundle state and the
    blank screen an unserved ``/assets`` path produces.

    ``is_answered`` separates "the service told us the frontend is broken" from
    "the service told us nothing", which is what makes the second worth retrying
    (see :func:`_probe_frontend_until_answered`). This is the single-shot probe;
    every decision in the reveal goes through the retrying one.
    """
    shell = http.get_page(f"{base_url}{SERVE_PATH}", timeout=10.0)
    if shell is None:
        return FrontendProbe(
            "the live service did not answer a request for the app shell",
            is_answered=False,
        )
    if shell.status != 200:
        return FrontendProbe(
            f"the app shell returned HTTP {shell.status}", is_answered=True
        )
    if shell.headers.get(FRONTEND_BUILT_HEADER) == "false":
        return FrontendProbe(
            "the live service is serving the 'frontend not built' placeholder -- the compiled bundle is missing",
            is_answered=True,
        )
    match = _ASSET_REFERENCE_PATTERN.search(shell.body)
    if match is None:
        return FrontendProbe(
            "the app shell loads no bundled script, so it is not the built app",
            is_answered=True,
        )
    asset_url = f"{base_url}/assets/{match.group(1)}"
    asset = http.get_page(asset_url, timeout=10.0)
    if asset is None:
        return FrontendProbe(
            f"the live service did not answer a request for the bundled script {asset_url}",
            is_answered=False,
        )
    if asset.status != 200:
        return FrontendProbe(
            f"the bundled script {asset_url} returned HTTP {asset.status}",
            is_answered=True,
        )
    if "javascript" not in asset.content_type:
        return FrontendProbe(
            f"the bundled script {asset_url} came back as '{asset.content_type}' rather than JavaScript, "
            "so the browser will refuse it and render a blank page",
            is_answered=True,
        )
    return FrontendProbe(None, is_answered=True)


def _probe_frontend_until_answered(
    http: HttpClient, base_url: str, sleeper: Callable[[float], None]
) -> FrontendProbe:
    """:func:`probe_frontend`, retrying until the service actually answers.

    Every frontend verdict the reveal reaches is consequential and every one of
    them is wrong if a single unlucky request decides it. Beforehand, a false
    "not serving" silently disarms the regression check for the whole run, so a
    reveal that then breaks the UI is reported rather than rolled back.
    Afterwards, a false "not serving" reverts a change that landed fine -- and
    on a frontend-only reveal this is the only liveness check there is, since
    the health poll runs only when the backend restarted. During recovery it
    turns a rollback that worked into an emergency.

    Only a *non-answer* is retried. A verdict -- the placeholder, a bad status,
    a script served as HTML -- is the service telling us the frontend really is
    broken; asking again reaches the same conclusion more slowly. Exhausting the
    budget returns the last non-answer, which reads as a failure: a UI that will
    not answer is one the user cannot see either.
    """
    probe = probe_frontend(http, base_url)
    for _ in range(_FRONTEND_PROBE_ATTEMPTS - 1):
        if probe.is_answered:
            return probe
        sleeper(_FRONTEND_PROBE_INTERVAL_SECONDS)
        probe = probe_frontend(http, base_url)
    return probe


def describe_frontend_failure(
    http: HttpClient, base_url: str, sleeper: Callable[[float], None]
) -> str | None:
    """Return why the live UI is not serving a working frontend, or ``None``."""
    return _probe_frontend_until_answered(http, base_url, sleeper).failure


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
        as_expendable(
            [
                "uv",
                "tool",
                "install",
                "-e",
                source_dir,
                *_tool_extras(tool_name, repo_root, runner, env),
                "--reinstall",
            ]
        ),
        repo_root,
        f"uv tool install {tool_name} --reinstall",
        env=env,
    )


def _refresh_backend_dependencies(repo_root: Path, runner: Runner) -> None:
    """Re-resolve the three backend environments from the current tree, mirroring
    ``build_workspace.sh``: the vendored ``mngr`` tool, the ``system-interface``
    tool, and the workspace venv (``uv sync``).

    Its own function because the rollback recovery needs exactly this backend
    half of the dependency refresh and none of the frontend half (see
    :func:`_recover_running_state`), and the reveal and the recovery must not
    drift on how the environments are rebuilt.
    """
    _reinstall_tool(MNGR_TOOL_NAME, MNGR_EXECUTABLE, MNGR_DIR, repo_root, runner)
    _reinstall_tool(TOOL_NAME, TOOL_NAME, APP_DIR, repo_root, runner)
    _run_checked(
        runner,
        as_expendable(["uv", "sync", "--all-packages", "--frozen"]),
        repo_root,
        "uv sync --all-packages --frozen",
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
        _run_checked(
            runner, as_expendable(["npm", "ci"]), repo_root / FRONTEND_DIR, "npm ci"
        )
    if changes.backend_manifest:
        _refresh_backend_dependencies(repo_root, runner)


def _apply_reveal(
    changes: ChangeSet,
    repo_root: Path,
    base_url: str,
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None],
    is_frontend_expected: bool,
) -> str | None:
    """Refresh deps, build, restart, and reload as applicable. Raises
    :class:`RevealFailed` the moment any step does not end healthy.

    Returns the frontend failure this reveal decided *not* to roll back for --
    one the workspace arrived with -- so the caller can report it instead of
    signing off on a UI it knows the user cannot see. ``None`` when the live UI
    is serving."""
    _refresh_dependencies(changes, repo_root, runner)
    if changes.frontend:
        _run_checked(
            runner,
            as_expendable(["npm", "run", "build"]),
            repo_root / FRONTEND_DIR,
            "npm run build",
        )
        _assert_bundle_built(repo_root, live_service_restarted=False)
    if changes.backend:
        preflight_output = _preflight(repo_root, http, spawner, sleeper)
        if preflight_output is not None:
            # Live service was never restarted, so it is still serving known-good
            # code -- recovery must not restart it (live_service_restarted=False).
            raise RevealFailed(
                "merged backend failed to boot in a pre-flight check; live service "
                "not restarted",
                detail=preflight_output or "(the pre-flight boot wrote nothing at all)",
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
    # Confirm the user would actually see the app, not just that the backend
    # answers. Scoped to a *regression*: only a frontend that was serving before
    # this reveal has to be serving after it. A workspace that arrived already
    # broken gets the finding reported rather than a rollback, because rolling
    # an unrelated change back would not fix it and would lose the change.
    #
    # Ahead of the refresh below, so a reveal that regressed the frontend rolls
    # back instead of asking every open view to reload into it.
    frontend_failure = describe_frontend_failure(http, base_url, sleeper)
    if frontend_failure is not None:
        if is_frontend_expected:
            raise RevealFailed(
                f"the live UI stopped serving a working frontend: {frontend_failure}",
                live_service_restarted=changes.backend,
            )
        sys.stderr.write(
            f"warning: the live UI is not serving a working frontend, and was not before this "
            f"reveal either, so it was not rolled back for it: {frontend_failure}\n"
        )
    # Unconditional: this runs only when something changed (``reveal`` returns
    # early otherwise), and a BACKEND-only change needs the reload just as much
    # as a frontend one. The restart bounces the API underneath a page that
    # keeps rendering from whatever it had already fetched, and a restart quick
    # enough not to look unreachable never triggers a reload from anywhere else.
    _refresh_workspace_view(repo_root, runner)
    return frontend_failure


def _restore_tree(
    name_status: Sequence[tuple[str, str]],
    rollback_to: str,
    repo_root: Path,
    runner: Runner,
) -> None:
    """Restore every changed path to its ``rollback_to`` state, staged for commit.

    Added-since paths are removed; modified/deleted paths are checked out from
    the known-good revision. Build output is gitignored so it never appears here;
    :func:`restore_bundle` puts that back from the pre-reveal snapshot.
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
    saved_bundle: Path | None,
    is_frontend_expected: bool,
) -> bool:
    """After the tree is restored to known-good, rebuild/restart from it as
    needed and confirm the live UI is healthy. Returns True iff confirmed healthy.

    ``live_service_restarted`` says whether the failed reveal had already
    restarted the live backend. When it did not (pre-flight / dependency-refresh
    / frontend-build failures), the live service is still running known-good code
    in memory and the on-disk tree has just been restored to match it, so we must
    NOT restart -- doing so would needlessly blip a healthy UI. We only restart
    when the failed reveal had actually restarted the service into broken code.

    ``saved_bundle`` is the pre-reveal copy of the built frontend, and it is what
    makes this recoverable at all. Restoring it needs neither ``npm`` nor a
    working registry, so a broken build environment -- the class of failure that
    motivates the snapshot -- cannot take the UI down with it, whereas recovering
    by rebuilding just re-runs the command that has already failed once. A
    rebuild is only attempted when there is no snapshot -- either because there
    was no bundle to lose, or because the copy could not be taken (see
    :func:`snapshot_bundle`, which degrades to this rather than refusing to
    reveal).

    ``npm ci`` runs here only on the rebuild branch, and only for a manifest
    change. Restoring a snapshot needs no dependencies at all, so refusing to run
    the destructive step there is what lets a broken build environment be
    survived -- at the known cost that rolling back a *manifest* change leaves
    ``node_modules`` out of step with the restored lockfile. It holds whatever
    the failed reveal's ``npm ci`` installed, or -- when that ``npm ci`` is the
    step that failed, one of the two failures this whole mechanism exists for --
    nothing at all, since it deletes before it installs. What is served is
    unaffected either way (the restored bundle is already compiled), but the next
    reveal that touches only frontend source builds against whichever of those it
    finds, since no manifest changed for it to notice: against the wrong
    dependency tree in the first case, and against no dependency tree in the
    second. The rebuild branch has no such trade to make: it is already going to
    compile from source, so it needs ``node_modules`` to match the restored
    lockfile, and there is no bundle for ``npm ci`` to endanger.

    Unlike :func:`_apply_reveal`, nothing escapes here -- this is the last line
    of defense, so a failed step (a command that exits non-zero, or a filesystem
    error putting the snapshot back) just means "not recovered" (exit 3). It must
    never propagate: the rollback commit has already landed by this point, and
    the exit code is all the caller has to go on."""
    try:
        if changes.backend_manifest:
            _refresh_backend_dependencies(repo_root, runner)
        if changes.frontend:
            if saved_bundle is not None:
                # Already-compiled output: it needs neither node_modules nor a
                # registry, so ``npm ci`` is skipped rather than run.
                restore_bundle(saved_bundle, repo_root)
            else:
                # Compiling from source, so node_modules has to match the
                # restored lockfile, and right now it holds whatever the failed
                # reveal left it as -- the packages it installed, or nothing at
                # all when its own ``npm ci`` deleted them and then failed.
                if changes.frontend_manifest:
                    _run_checked(
                        runner,
                        as_expendable(["npm", "ci"]),
                        repo_root / FRONTEND_DIR,
                        "npm ci",
                    )
                _run_checked(
                    runner,
                    as_expendable(["npm", "run", "build"]),
                    repo_root / FRONTEND_DIR,
                    "npm run build",
                )
                _assert_bundle_built(repo_root, live_service_restarted=False)
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
    except (RevealFailed, OSError) as exc:
        # ``OSError`` covers putting the snapshot back: rmtree/copytree can fail
        # on a full or read-only filesystem, and letting that escape would kill
        # the process with a traceback and exit 1 -- the code that means "nothing
        # was changed", after the rollback commit has already landed.
        sys.stderr.write(f"recovery step failed: {exc}\n")
        return False
    # "Recovered" has to mean the user can see their UI again, so hold the
    # rollback to the same frontend standard the reveal itself is held to --
    # otherwise a rollback that restored the tree but left nothing to serve
    # would report success and exit 2.
    if healthy and is_frontend_expected:
        frontend_failure = describe_frontend_failure(http, base_url, sleeper)
        if frontend_failure is not None:
            sys.stderr.write(f"recovery left the frontend broken: {frontend_failure}\n")
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

    # Taken before anything destructive runs, and kept until the reveal has
    # either succeeded or finished recovering -- or, on the emergency path,
    # handed to the operator instead of discarded (see below).
    saved_bundle = snapshot_bundle(repo_root)
    is_snapshot_left_for_operator = False
    # Whether a working frontend is owed afterwards is decided by what was being
    # served *before* -- the reveal is answerable for regressions, not for a
    # workspace that was already broken when it arrived.
    is_frontend_expected = (
        describe_frontend_failure(http, resolved_base, sleeper) is None
    )

    try:
        try:
            unresolved_frontend_failure = _apply_reveal(
                changes,
                repo_root,
                resolved_base,
                runner,
                http,
                spawner,
                sleeper,
                is_frontend_expected,
            )
        except RevealFailed as exc:
            sys.stderr.write(
                f"reveal failed: {exc}\n{_detail_block(exc.detail)}"
                f"rolling back to {rollback_to[:12]} and restoring the live UI...\n"
            )
            # The rollback's own git steps run with ``check=True``. Letting one
            # escape would leave the process reporting exit 1 -- "nothing was
            # changed" -- over a part-restored tree with the bundle already
            # destroyed, and the ``finally`` below would discard the copy on the
            # way out. Not recovering is exactly what the emergency path below
            # is for, so route it there and keep the copy.
            try:
                _restore_tree(name_status, rollback_to, repo_root, runner)
                _commit_rollback(
                    repo_root,
                    runner,
                    rollback_to,
                    f"Reveal failed and was auto-reverted: {exc.headline()}",
                )
                is_recovered = _recover_running_state(
                    changes,
                    repo_root,
                    resolved_base,
                    runner,
                    http,
                    sleeper,
                    live_service_restarted=exc.live_service_restarted,
                    saved_bundle=saved_bundle,
                    is_frontend_expected=is_frontend_expected,
                )
            except subprocess.CalledProcessError as rollback_exc:
                sys.stderr.write(f"the rollback itself failed: {rollback_exc}\n")
                is_recovered = False
            if is_recovered:
                if is_frontend_expected:
                    sys.stderr.write(
                        "rolled back to last-known-good; the live UI is confirmed healthy. "
                        "The requested change did NOT land -- diagnose it before retrying.\n"
                    )
                else:
                    # The rollback was never held to the frontend standard,
                    # because the frontend was not serving when the reveal
                    # started -- so "confirmed healthy" would be a sign-off over
                    # a UI we already know the user cannot see. Say what was
                    # actually established and what was not.
                    sys.stderr.write(
                        "rolled back to last-known-good and the backend is healthy, but the live UI "
                        "was not serving a working frontend before this reveal either, so the "
                        "rollback was not held to that standard and cannot confirm it. The requested "
                        "change did NOT land -- diagnose both before retrying.\n"
                    )
                return 2
            sys.stderr.write(
                "EMERGENCY: rollback did not restore a healthy UI. The system interface may be down; "
                "manual intervention is required.\n"
            )
            # The copy outlives this failure on purpose. Recovery could not put a
            # working frontend back, and in the very failure this mechanism
            # exists for -- a build environment that cannot compile one -- this
            # is the last known-good bundle anywhere: the tree's was destroyed
            # before the failed build, and rebuilding is what just failed.
            # Discarding it here would take the operator's only way back with it.
            #
            # Only when the reveal touched the frontend, though. Nothing else
            # here writes the bundle directory -- the build is the one step that
            # empties it, and the rollback restores tracked files while it is
            # untracked output -- so after a backend-only reveal the copy is
            # byte-identical to what is already being served. Offering it there
            # would send someone whose UI is down for a backend reason to copy a
            # bundle over itself.
            if saved_bundle is not None and changes.frontend:
                is_snapshot_left_for_operator = True
                sys.stderr.write(
                    f"the pre-reveal frontend bundle was kept at {saved_bundle} -- copying it over "
                    f"{repo_root / STATIC_DIR} restores the UI without needing npm or a registry. "
                    "Delete it once you have.\n"
                )
            return 3
    finally:
        if not is_snapshot_left_for_operator:
            _discard_snapshot(saved_bundle)

    if unresolved_frontend_failure is not None:
        # The change landed and there is nothing here to roll back, so this is
        # still a 0 -- but the last line the caller reads must not sign off on a
        # UI we just established the user cannot see.
        sys.stderr.write(
            "revealed: the change landed and the backend is healthy, but the live UI is still "
            f"not serving a working frontend: {unresolved_frontend_failure}. That was already "
            "true before this reveal, so it was not rolled back for it -- report it and "
            "diagnose it separately.\n"
        )
        return 0
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
    if _is_shed_protected_command(sys.argv[1:]):
        _protect_from_memory_shed()
    sys.exit(main())
