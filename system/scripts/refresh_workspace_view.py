#!/usr/bin/env python3
"""Rebuild the user's view of this workspace after its interface changed.

Run this after anything that makes the running UI stale -- above all
``mngr start --restart system-services``, which bounces the system-interface
backend underneath whatever the user currently has open. Nothing else reloads
that view: the Minds app only intervenes when a workspace looks *unreachable*
for a sustained stretch, and a services restart that comes back quickly never
crosses that bar.

Two channels, because neither reaches every viewer:

``reload_system_interface`` broadcast (the workspace's own WebSocket)
    Reaches every *browser* attached to the system interface, including anyone
    the user shared the workspace with over a Cloudflare tunnel. Best-effort by
    nature: it is a live fan-out with no replay, so it reaches whoever is
    connected at that instant and nobody else. After a services restart that is
    often nobody -- browsers reconnect on exponential backoff -- so treat it as
    a bonus rather than the guarantee.

``POST /api/v1/agents/<primary>/refresh`` (the Minds app, via the gateway)
    Reaches only the desktop app, but does not go through the workspace server
    at all, so it works while that server is still coming back. This is the
    channel that actually covers the common case: a user looking at the
    workspace in the Minds app.

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
    LATCHKEY_GATEWAY,           Gateway address + password mngr injects into the
    LATCHKEY_GATEWAY_PASSWORD   agent environment. Both must be present to reach
                                the Minds app; the broadcast still runs without
                                them.
    LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE
                                The per-agent authorization JWT, forwarded when
                                set. Only a desktop-hosted gateway injects it, so
                                its absence is normal and not a reason to skip
                                the app refresh.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Callable, Sequence

DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"
ENV_MNGR_AGENT_ID = "MNGR_AGENT_ID"
MNGR_AGENT_ID_HEADER = "X-Mngr-Agent-Id"

ENV_GATEWAY = "LATCHKEY_GATEWAY"
ENV_GATEWAY_PASSWORD = "LATCHKEY_GATEWAY_PASSWORD"
ENV_GATEWAY_PERMISSIONS = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"

RELOAD_OP = "reload_system_interface"

# The two refresh POSTs are courtesies on a path the caller is blocking on, so
# they get a short leash: a wedged desktop app or frontend must not stall a
# reveal.
_TIMEOUT_SECONDS = 10.0

# The lookup is a ``mngr ls`` subprocess -- an interpreter start plus provider
# discovery, which is mostly page faults, so it tracks how much memory the host
# has to spare (~1.5s idle, 35s measured on a host swapping hard). It gets a
# budget of its own, larger than the POSTs', because those are one round trip and
# this is not. The budget still sits under the swapping-host figure on purpose:
# a caller is blocked on this, and overrunning it costs a reported skip of the
# app channel, not a wrong window.
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


def resolve_primary_agent_id(runner: Runner) -> str:
    """Return this workspace's primary agent id, or ``""`` if it cannot be found.

    Reported on stderr rather than guessed at: the caller's own id is the
    tempting fallback, but for a sub-agent it names a window the app is not
    showing, so the refresh would be a silent no-op that reads as a success.
    """

    def unresolved(reason: str) -> str:
        sys.stderr.write(
            f"refresh: could not resolve this workspace's primary agent id ({reason}); "
            "skipping the Minds app refresh.\n"
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
    # ``--ids`` prints one id per line; a workspace has exactly one primary, but
    # take the first line rather than assuming the output is a single token.
    #
    # Read stdout whatever the exit code says. ``mngr ls`` prints every agent it
    # did list, then exits non-zero if *any* provider errored -- with the error
    # block on stderr, never here. An unconfigured cloud provider is routine, and
    # gating on the exit code would discard the id sitting right here and disable
    # the app channel on hosts where nothing is actually wrong.
    for line in (completed.stdout or "").splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    if completed.returncode != 0:
        return unresolved(f"mngr ls exited {completed.returncode} and listed no primary agent")
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
        f"refresh: reload broadcast to the workspace server {_describe(status)}; any "
        "attached browser (including a shared tunnel viewer) may still be showing "
        "the previous build.\n"
    )
    return False


def request_app_refresh(http: HttpClient, runner: Runner) -> bool:
    """Ask the Minds app to rebuild its view of this workspace.

    Returns whether the app accepted the request. No gateway URL or password
    means we are not running under a Minds desktop app at all (a bare ``mngr``
    workspace, a test harness), which is not a failure -- the broadcast alone is
    the whole story there.

    The primary-agent lookup happens after that env check, not before: it is a
    subprocess with a 30s budget, and there is nothing to address it to when no
    app is attached.
    """
    gateway = os.environ.get(ENV_GATEWAY, "")
    password = os.environ.get(ENV_GATEWAY_PASSWORD, "")
    if not gateway or not password:
        sys.stderr.write(
            "refresh: latchkey gateway env not set; skipping the Minds app refresh.\n"
        )
        return False
    primary_agent_id = resolve_primary_agent_id(runner)
    if not primary_agent_id:
        return False
    headers = {
        "Content-Type": "application/json",
        "X-Latchkey-Gateway-Password": password,
    }
    # Forwarded only when set, like every other gateway caller here
    # (``bootstrap/manager.py``, ``github_sync/wiring.py``, the ``latchkey``
    # skill's ``${VAR:+...}`` guard). Both directions bite: a desktop-hosted
    # gateway needs it as the per-agent authorization JWT -- without it the
    # gateway resolves the request against a deny-all default -- while a
    # VPS-hosted one never injects it, because its forwarding extension
    # substitutes a desktop-target JWT of its own. Requiring it would disable
    # this channel on every remote workspace.
    permissions = os.environ.get(ENV_GATEWAY_PERMISSIONS, "")
    if permissions:
        headers["X-Latchkey-Gateway-Permissions-Override"] = permissions
    status = http.post_json(
        f"{gateway.rstrip('/')}/minds-api-proxy/api/v1/agents/{primary_agent_id}/refresh",
        {},
        headers,
        timeout=_TIMEOUT_SECONDS,
    )
    if status == 200:
        return True
    sys.stderr.write(
        f"refresh: Minds app refresh {_describe(status)}; if the app has this "
        "workspace open it may still be showing the previous build.\n"
    )
    return False


def _run_channel(name: str, call: Callable[[], bool]) -> bool:
    """Run one channel, reporting an escape it does not catch itself.

    Each channel handles the errors it expects; this covers the ones it does not.
    Both have escapes outside those groups: a malformed ``LATCHKEY_GATEWAY`` (or
    ``MINDS_WORKSPACE_SERVER_URL``) raises ``http.client.InvalidURL``, which is
    not an ``OSError``, and captured ``mngr ls`` output that is not UTF-8 raises
    ``UnicodeDecodeError``, which is not a ``SubprocessError``. A traceback would
    read to an agent mid-update as a failed reveal.

    Per channel rather than around both: the channels are independent, so an
    escape from the one that happens to run first must not cancel the other.
    """
    try:
        return call()
    except Exception as exc:
        sys.stderr.write(
            f"refresh: {name} failed unexpectedly ({type(exc).__name__}: {exc}).\n"
        )
        return False


def refresh(
    *,
    runner: Runner,
    http: HttpClient,
    base_url: str | None = None,
) -> int:
    """Fire both channels. Always returns 0 -- see the module docstring."""
    resolved_base = (
        base_url or os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL)
    ).rstrip("/")
    # Independent and unconditional: each reaches viewers the other cannot, and
    # a failure of one says nothing about the other.
    broadcast_ok = _run_channel(
        "the reload broadcast", lambda: broadcast_reload(http, resolved_base)
    )
    app_ok = _run_channel(
        "the Minds app refresh", lambda: request_app_refresh(http, runner)
    )
    if broadcast_ok or app_ok:
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
