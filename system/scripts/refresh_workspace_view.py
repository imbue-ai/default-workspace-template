#!/usr/bin/env python3
"""Rebuild the user's view of this workspace after its interface changed.

Run this after anything that makes the running UI stale -- above all
``mngr start --restart system-services``, which bounces the system-interface
backend underneath whatever the user currently has open. Nothing else reloads
that view: the Minds app only intervenes when a workspace looks *unreachable*
for a sustained stretch, and a services restart that comes back quickly never
crosses that bar.

Three channels, because no one of them reaches every viewer:

view epoch (``data/.state/view_epoch``)
    Bumped on disk, so it does not care whether anything is up yet. The system
    interface serves it into the app shell and sends it on every WebSocket
    connect; a page that reconnects carrying an older epoch reloads itself.
    This is what covers the browser whose socket was down for the restart --
    or shut entirely -- and it is why nothing here waits for the server.

``reload_system_interface`` broadcast (the workspace's own WebSocket)
    Reloads every browser attached *right now*, including anyone the user
    shared the workspace with over a Cloudflare tunnel. A live fan-out with no
    replay: fired at a server that is down, or at one whose clients are still
    on reconnect backoff, it simply reaches nobody. That is not a loss -- the
    epoch above catches those clients when they reconnect -- it just makes the
    reload immediate for those already there.

``POST /api/v1/agents/<primary>/refresh`` (the Minds app, via the gateway)
    Reaches only the desktop app, but does what neither of the above can: it
    drops the workspace session's HTTP cache before reloading.

Every outcome is reported on stderr and the exit code is always 0. The change
has already landed on disk; failing a reveal because the user had the window
shut would be worse than a stale tab.

Run via bare ``python3`` (standard library only), like ``forward_port.py`` -- it
runs from restart paths that must not depend on a synced venv.

Usage:
    python3 system/scripts/refresh_workspace_view.py

Environment:
    MINDS_WORKSPACE_SERVER_URL  Base URL of the live workspace server
                                (default http://127.0.0.1:8000).
    MNGR_AGENT_ID               This agent's id. Sent for telemetry on the
                                broadcast.
    LATCHKEY_GATEWAY,           Gateway address + credentials mngr injects into
    LATCHKEY_GATEWAY_PASSWORD,  the agent environment. All three must be present
    LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE
                                to reach the Minds app; the other two channels
                                still run without them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Sequence

DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"
ENV_MNGR_AGENT_ID = "MNGR_AGENT_ID"
MNGR_AGENT_ID_HEADER = "X-Mngr-Agent-Id"

ENV_GATEWAY = "LATCHKEY_GATEWAY"
ENV_GATEWAY_PASSWORD = "LATCHKEY_GATEWAY_PASSWORD"
ENV_GATEWAY_PERMISSIONS = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"

RELOAD_OP = "reload_system_interface"

# Resolved from this file rather than the cwd: callers run us straight after a
# restart, from wherever they happen to be. Read by ``server.py``, which resolves
# the same path from its own location; ``test_view_epoch_path_matches_the_writer``
# pins the two together.
#
# This therefore names the checkout *this copy of the script* lives in, which is
# the served one for every caller that can reach here -- the reveal script and
# the update flows all edit and restart the live service, which is only coherent
# from the workspace's own checkout. Same handoff, and same assumption, as
# ``forward_port.py`` writing ``data/.state/apps.toml`` for the server to read.
_REPO_ROOT = Path(__file__).resolve().parents[2]
VIEW_EPOCH_PATH = _REPO_ROOT / "data/.state/view_epoch"

# The two refresh POSTs are courtesies on a path the caller is blocking on, so
# they get a short leash: a wedged desktop app or frontend must not stall a
# reveal.
_TIMEOUT_SECONDS = 10.0

# The lookup is a ``mngr ls`` subprocess -- an interpreter start plus provider
# discovery, which is mostly page faults, so it tracks how much memory the host
# has to spare (~1.5s idle, 35s measured on a host swapping hard). It gets its
# own budget because timing out here used to mean addressing the wrong window.
_PRIMARY_LOOKUP_TIMEOUT_SECONDS = 30.0

# The Minds app identifies a *workspace* by its primary agent id, not by whoever
# is calling: a sub-agent (an /assist chat, a launch-task worker) has its own
# MNGR_AGENT_ID, and refreshing under that addresses no window at all. Only this
# workspace's agents are visible from here, so exactly one carries ``is_primary``.
# Mirrors the resolution the ``assist`` skill uses for bug reports.
_PRIMARY_AGENT_QUERY = "has(labels.is_primary)"


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands."""

    def run(self, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), **kwargs)


class HttpClient:
    """Indirection over the outbound HTTP so tests can intercept it."""

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> int | None:
        """POST a JSON body; return the HTTP status or ``None`` if unreachable."""
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (urllib.error.URLError, OSError):
            return None


def _describe(status: int | None) -> str:
    """Render a POST outcome for a human reading stderr.

    ``None`` means the host never answered, which is a different situation from
    any status code and should not be reported as one.
    """
    return "was unreachable" if status is None else f"returned {status}"


def bump_view_epoch(epoch_path: Path) -> bool:
    """Record that the interface on disk has changed. Returns whether it stuck.

    Written via a temporary file and ``os.replace`` so a reader never sees a
    half-written epoch: the system interface reads this on every WebSocket
    connect, including while we are writing it.
    """
    epoch = uuid.uuid4().hex
    temporary_path = epoch_path.with_name(f"{epoch_path.name}.{os.getpid()}.tmp")
    try:
        epoch_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(f"{epoch}\n")
        os.replace(temporary_path, epoch_path)
    except OSError as exc:
        sys.stderr.write(
            f"refresh: could not record the new interface epoch at {epoch_path} "
            f"({type(exc).__name__}: {exc}); a browser that reconnects later will "
            "not know to reload itself.\n"
        )
        return False
    return True


def resolve_primary_agent_id(runner: Runner) -> str:
    """Return this workspace's primary agent id, or ``""`` if it cannot be found.

    Reported on stderr rather than guessed at: the caller's own id is the
    tempting fallback, but for a sub-agent it names a window the app is not
    showing, so the refresh would be a silent no-op that reads as a success.
    """

    def unresolved(reason: str) -> str:
        sys.stderr.write(
            f"refresh: could not resolve this workspace's primary agent id ({reason}); "
            "skipping the Minds app refresh (the other channels still ran).\n"
        )
        return ""

    try:
        completed = runner.run(
            ["mngr", "ls", "--include", _PRIMARY_AGENT_QUERY, "--ids"],
            capture_output=True,
            text=True,
            timeout=_PRIMARY_LOOKUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return unresolved(f"{type(exc).__name__}: {exc}")
    if completed.returncode != 0:
        return unresolved(f"mngr ls exited {completed.returncode}")
    # ``--ids`` prints one id per line; a workspace has exactly one primary, but
    # take the first line rather than assuming the output is a single token.
    for line in (completed.stdout or "").splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return unresolved("mngr ls listed no primary agent")


def broadcast_reload(http: HttpClient, base_url: str) -> bool:
    """Tell every attached browser to reload the whole interface.

    Returns whether the broadcast was accepted. A non-200 does not mean no
    browser reloaded -- only that the workspace server did not take the op.
    """
    agent_id = os.environ.get(ENV_MNGR_AGENT_ID, "")
    status = http.post_json(
        f"{base_url}/api/layout/broadcast",
        {"op": RELOAD_OP, "args": {}, "agent_id": agent_id},
        {"Content-Type": "application/json", MNGR_AGENT_ID_HEADER: agent_id},
        timeout=_TIMEOUT_SECONDS,
    )
    if status == 200:
        return True
    sys.stderr.write(
        f"refresh: reload broadcast to the workspace server {_describe(status)}; an "
        "attached browser will reload when it reconnects rather than right now.\n"
    )
    return False


def request_app_refresh(http: HttpClient, runner: Runner) -> bool:
    """Ask the Minds app to drop its cache and reload its view of this workspace.

    Returns whether the app accepted the request. Absent gateway env means we are
    not running under a Minds desktop app at all (a bare ``mngr`` workspace, a
    test harness), which is not a failure. The primary-agent lookup happens after
    that check: it is a subprocess with a 30s budget, and there is nothing to
    address it to when no app is attached.
    """
    gateway = os.environ.get(ENV_GATEWAY, "")
    password = os.environ.get(ENV_GATEWAY_PASSWORD, "")
    permissions = os.environ.get(ENV_GATEWAY_PERMISSIONS, "")
    if not gateway or not password or not permissions:
        sys.stderr.write(
            "refresh: latchkey gateway env not set; skipping the Minds app refresh "
            "(the other channels still ran).\n"
        )
        return False
    primary_agent_id = resolve_primary_agent_id(runner)
    if not primary_agent_id:
        return False
    status = http.post_json(
        f"{gateway.rstrip('/')}/minds-api-proxy/api/v1/agents/{primary_agent_id}/refresh",
        {},
        {
            "Content-Type": "application/json",
            "X-Latchkey-Gateway-Password": password,
            "X-Latchkey-Gateway-Permissions-Override": permissions,
        },
        timeout=_TIMEOUT_SECONDS,
    )
    if status == 200:
        return True
    sys.stderr.write(
        f"refresh: Minds app refresh {_describe(status)}; if the app has this "
        "workspace open it may still be showing the previous build.\n"
    )
    return False


def refresh(
    *,
    runner: Runner,
    http: HttpClient,
    base_url: str | None = None,
    epoch_path: Path = VIEW_EPOCH_PATH,
) -> int:
    """Fire all three channels. Always returns 0 -- see the module docstring."""
    resolved_base = (
        base_url or os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL)
    ).rstrip("/")
    # First, and unconditionally: this is the only channel that does not need
    # anything to be up, and the only one that reaches a viewer who is not
    # looking right now.
    epoch_ok = bump_view_epoch(epoch_path)
    # Independent of each other and of the epoch: each reaches viewers the
    # others cannot, and a failure of one says nothing about the rest.
    broadcast_ok = broadcast_reload(http, resolved_base)
    app_ok = request_app_refresh(http, runner)
    if epoch_ok or broadcast_ok or app_ok:
        sys.stderr.write("refresh: requested a reload of this workspace's view.\n")
    else:
        sys.stderr.write(
            "refresh: no refresh channel succeeded; the change is on disk but an "
            "open view may still be showing the previous build until reloaded.\n"
        )
    return 0


def main() -> int:
    return refresh(runner=Runner(), http=HttpClient())


if __name__ == "__main__":
    sys.exit(main())
