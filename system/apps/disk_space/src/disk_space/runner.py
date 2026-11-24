"""Shows what is filling the workspace's disk as a treemap, sorts it into safe to delete, ask first and keep, and helps free space when the disk passes 90%..

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/disk-space/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/disk-space/`` but
  honors the ``DISK_SPACE_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``DISK_SPACE_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://disk-space.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from app_schema.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from app_schema.request_guard import install_request_guard
from app_schema.routes import error_response, model_response, parse_request_body
from flask import Flask, Response, abort, request, send_file
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from loguru import logger
from pydantic import Field
from werkzeug.serving import run_simple
from workspace_layout.identity import is_owner_participant

from disk_space import cleanup, cli, helper, identity, scan, status, store

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/disk-space/`` but is overridable via the ``DISK_SPACE_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/disk-space/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = store.DATA_DIR

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``DISK_SPACE_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("DISK_SPACE_PORT", "8099"))

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path(
    "system/apps/system_interface/imbue/system_interface/static/_static"
)
SHELL_STATIC_MODULE_NAMES = ("app_contract.js", "context_menu.js")

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT_BODY = """
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
"""
SHELL_PAGE_SCRIPT = f'<script type="module">{SHELL_PAGE_SCRIPT_BODY}</script>'

# The page, its script and its stylesheet, shipped beside this file.
STATIC_DIR = Path(__file__).parent / "static"
PAGE_ASSET_NAMES = ("app.js", "app.css")


def inline_script_source(body: str) -> str:
    """The Content-Security-Policy source admitting one inline <script> whose body is exactly ``body``."""
    return (
        "'sha256-"
        + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        + "'"
    )


# Every HTML page is served under this policy (the app is reachable from the
# internet once the workspace is shared): scripts only from this origin plus the
# inline blocks listed here by hash, fetches, sockets and images only from this
# origin, inline styles, frames of any http(s) page, no plugins. Anything else a
# page pulls in (a CDN script, Google Fonts, an image hosted elsewhere, a
# third-party API called from the browser) is blocked until its origin is added
# to the matching directive; a new inline <script> needs its
# ``inline_script_source`` added.
# The browser posts every block to ``/_csp-report`` below, which logs it on
# stderr (the supervisor log), so a blocked resource is never silent; the
# ``'report-sample'`` makes the browser include where a blocked inline script
# is and how it starts, since the report otherwise names it only as "inline".
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    f"script-src 'self' 'report-sample' {inline_script_source(SHELL_PAGE_SCRIPT_BODY)}; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src http: https:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; "
    "report-uri /_csp-report"
)
BROWSER_EXTENSION_SCHEMES = (
    "chrome-extension:",
    "moz-extension:",
    "safari-web-extension:",
)


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


class DiskFigures(FrozenModel):
    """The disk right now."""

    total: int = Field(description="The disk's size in bytes")
    used: int = Field(description="Bytes in use")
    free: int = Field(description="Bytes free")
    percent: int = Field(description="How full the disk is, as a whole percent")


class StateResponse(FrozenModel):
    """Everything the page shows."""

    disk: DiskFigures = Field(description="The disk right now")
    threshold: int = Field(description="The alert line, in percent full")
    scanning: bool = Field(description="Whether a measurement is running now")
    scan_started_at: float | None = Field(
        description="When the running measurement started (epoch seconds)"
    )
    snapshot: dict[str, Any] | None = Field(
        description="The latest measurement: id, taken_at, took_seconds, disk, missing, and nodes (a flat tree; "
        "each node has id, parent, name, path, bytes, verdict safe/ask/keep, why, item, deletable)"
    )
    history: list[dict[str, Any]] = Field(
        description="Recent alerts, deletions and helper starts, newest first"
    )
    last_check_at: float | None = Field(
        description="When the routine last checked the disk"
    )
    helper_chat_id: str | None = Field(
        description="The cleanup helper's chat, once one was started"
    )
    is_owner: bool = Field(
        description="Whether this request comes from the workspace's owner: only the owner can delete files, start "
        "the cleanup helper or move the alert line, and the page greys those controls out for anyone else"
    )


class StatusItem(FrozenModel):
    """One thing that could be deleted."""

    name: str = Field(description="What it is, in plain words")
    path: str = Field(description="Where it is on disk")
    bytes: int = Field(description="Its size when last measured")
    why: str = Field(description="Why it is in its group")
    deletable: bool = Field(description="Whether POST /api/delete can delete it")


class StatusResponse(FrozenModel):
    """How full the disk is and what can go: the one answer another agent (the Caretaker) needs."""

    percent_used: int = Field(description="How full the disk is, as a whole percent")
    used_bytes: int = Field(description="Bytes in use")
    free_bytes: int = Field(description="Bytes free")
    total_bytes: int = Field(description="The disk's size")
    threshold_percent: int = Field(description="The alert line")
    is_over_threshold: bool = Field(
        description="Whether the disk is at or over the alert line"
    )
    measured_at: float | None = Field(
        description="When the breakdown was measured (epoch seconds)"
    )
    measured_minutes_ago: int | None = Field(description="How old the breakdown is")
    safe_to_delete_bytes: int = Field(description="Total of everything safe to delete")
    ask_first_bytes: int = Field(
        description="Total of everything that needs the user's yes"
    )
    safe_to_delete: list[StatusItem] = Field(
        description="The largest safe-to-delete items"
    )
    ask_first: list[StatusItem] = Field(description="The largest ask-first items")
    last_check_at: float | None = Field(
        description="When the routine last checked the disk"
    )
    helper_chat_id: str | None = Field(
        description="The cleanup helper's chat, once one was started"
    )
    how_to_free_space: str = Field(description="What to call to free space")


class ScanResponse(FrozenModel):
    """Whether a measurement was started."""

    started: bool = Field(description="False when one was already running")


class DeleteRequest(FrozenModel):
    """What to delete. Owner only: a visitor's request is refused with 403."""

    paths: list[str] = Field(description="Paths of items from the latest measurement")
    user_approved: bool = Field(
        default=False,
        description="True only when the user said yes to these ask-first items in so many words",
    )


class DeleteResponse(FrozenModel):
    """What was deleted and what was not."""

    deleted: list[dict[str, Any]] = Field(
        description="Each deleted item: path, name, bytes, verdict"
    )
    refused: list[dict[str, Any]] = Field(
        description="Each item left alone: path and the reason"
    )
    freed_bytes: int = Field(description="How much the free space grew")


class HelperRequest(FrozenModel):
    """Start the cleanup helper. Owner only: a visitor's request is refused with 403."""

    item: str | None = Field(
        default=None, description="A path the user wants to ask about first"
    )


class HelperResponse(FrozenModel):
    """The cleanup helper's chat."""

    chat_id: str = Field(description="The chat's id; empty when it could not start")
    created: bool = Field(
        description="False when the request went to a helper chat already there"
    )
    detail: str = Field(description="Why it could not start, when it could not")


class SettingsRequest(FrozenModel):
    """New settings. Owner only: a visitor's request is refused with 403."""

    threshold: int = Field(description="The alert line, 50 to 99 percent full")


class SettingsResponse(FrozenModel):
    """The settings now."""

    threshold: int = Field(description="The alert line, in percent full")
    renotify_hours: float = Field(
        description="How long before nudging again while the disk stays over the line"
    )


# What every route exchanges, which the app's API document publishes and every parse and answer
# is checked against (build-app skill, "The app describes its own API"): a route that reads or
# answers JSON names its pydantic models here and uses ``parse_request_body`` and
# ``model_response``; a page, a file or a stream is declared schemaless with the reason.
ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the app's page"),
    ("GET", "/_static/{basename}"): SchemalessRoute(
        reason="a browser-side module the shell builds"
    ),
    ("GET", "/static/{basename}"): SchemalessRoute(
        reason="the page's own script and stylesheet"
    ),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
    ("GET", "/api/state"): RouteModels(response_model=StateResponse),
    ("GET", "/api/status"): RouteModels(response_model=StatusResponse),
    ("POST", "/api/scan"): RouteModels(response_model=ScanResponse),
    ("POST", "/api/delete"): RouteModels(
        request_model=DeleteRequest, response_model=DeleteResponse
    ),
    ("POST", "/api/helper"): RouteModels(
        request_model=HelperRequest, response_model=HelperResponse
    ),
    ("POST", "/api/settings"): RouteModels(
        request_model=SettingsRequest, response_model=SettingsResponse
    ),
    ("GET", "/raw/history/{entry_id}"): SchemalessRoute(
        reason="one stored history record, pretty-printed"
    ),
    ("GET", "/raw/snapshot/{snapshot_id}"): SchemalessRoute(
        reason="one stored measurement, pretty-printed"
    ),
    ("GET", "/raw/snapshot/{snapshot_id}/{group}"): SchemalessRoute(
        reason="the raw du output a measurement was built from"
    ),
    ("POST", "/_csp-report"): SchemalessRoute(
        reason="the browser's report of what the page's policy blocked"
    ),
}

app = Flask("disk_space", static_folder=None)
# Ahead of every route: a page of another origin's request is refused, and a request with no identity header is
# the owner's only from loopback under a loopback Host. Installed before anything else registers a hook, so nothing
# runs for a request it refuses.
install_request_guard(app, exempt_rules=frozenset())
# The app describes its own API: an OpenAPI document of every route below, served at the
# manifest's api_schema_path.
register_api_schema_route(
    app,
    DEFAULT_API_SCHEMA_PATH,
    title="Disk Space",
    description="Shows what is filling the workspace's disk as a treemap, sorts it into safe to delete, ask first and keep, and helps free space when the disk passes 90%.",
    route_table=ROUTE_TABLE,
)


@app.after_request
def add_content_security_policy(response: Response) -> Response:
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


@app.route("/")
def index() -> Response:
    """The app's page."""
    page = (
        (STATIC_DIR / "index.html")
        .read_text()
        .replace("SHELL_PAGE_SCRIPT", SHELL_PAGE_SCRIPT)
    )
    return Response(page, mimetype="text/html")


@app.route("/static/<basename>")
def page_asset(basename: str) -> Response:
    """The page's own script or stylesheet."""
    if basename not in PAGE_ASSET_NAMES:
        abort(404)
    return send_file((STATIC_DIR / basename).absolute())


@app.route("/_csp-report", methods=["POST"])
def content_security_policy_report() -> Response:
    """Where the browser reports what the page's security policy blocked."""
    body = request.get_json(force=True, silent=True)
    report = body.get("csp-report") if isinstance(body, dict) else None
    if not isinstance(report, dict):
        return Response(status=204)
    blocked, page, source, directive, line, sample = (
        str(report.get(key, ""))[:500]
        for key in (
            "blocked-uri",
            "document-uri",
            "source-file",
            "effective-directive",
            "line-number",
            "script-sample",
        )
    )
    # A block caused by a browser extension is the policy doing its job and is not logged.
    if blocked.startswith(BROWSER_EXTENSION_SCHEMES) or source.startswith(
        BROWSER_EXTENSION_SCHEMES
    ):
        return Response(status=204)
    # An inline script or style is reported as "inline": say where it is and how it starts instead.
    where = f" at {source}:{line}" if source and line else ""
    starting = f" starting {sample!r}" if sample else ""
    remedy = (
        "add its inline_script_source to CONTENT_SECURITY_POLICY if the page needs it"
        if blocked == "inline"
        else "add its origin to CONTENT_SECURITY_POLICY if the page needs it"
    )
    logger.error(
        "Content-Security-Policy blocked {!r} on {!r} ({}){}{}; {}",
        blocked,
        page,
        directive,
        where,
        starting,
        remedy,
    )
    return Response(status=204)


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    """One of the two browser-side modules the workspace shell builds and every app serves from its own origin."""
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    """Whether the app is up."""
    return model_response(HealthResponse(status="ok"))


@app.route("/api/state")
def api_state() -> Response:
    """Everything the page shows: the disk now, the latest measurement, settings and history."""
    watch = store.read_watch()
    scanning = store.is_locked("scan")
    return model_response(
        StateResponse(
            disk=DiskFigures(**scan.disk_usage()),
            threshold=store.read_settings()["threshold"],
            scanning=scanning,
            scan_started_at=watch.get("scan_started_at") if scanning else None,
            snapshot=store.latest_snapshot(),
            history=store.read_history()[: store.HISTORY_SHOWN],
            last_check_at=watch.get("last_check_at"),
            helper_chat_id=watch.get("helper_chat_id") or None,
            is_owner=is_owner_participant(identity.request_identity()),
        )
    )


@app.route("/api/status")
def api_status() -> Response:
    """How full the disk is and what can go, in one answer: what the Caretaker or any other agent asks."""
    return model_response(StatusResponse(**status.build_status()))


@app.route("/api/scan", methods=["POST"])
def api_scan() -> Response:
    """Measure the disk again in the background (a minute or two)."""
    return model_response(ScanResponse(started=scan.scan_in_background()))


@app.route("/api/delete", methods=["POST"])
def api_delete() -> Response:
    """Delete items of the latest measurement; ask-first items only with the user's explicit yes."""
    refusal = identity.forbid_unless_owner()
    if refusal is not None:
        return refusal
    body = parse_request_body(DeleteRequest)
    if not body.paths:
        return error_response("Name at least one item to delete.", 400)
    result = cleanup.delete(
        body.paths,
        user_approved=body.user_approved,
        by="page",
        requested_by=identity.request_participant(),
    )
    return model_response(DeleteResponse(**result))


@app.route("/api/helper", methods=["POST"])
def api_helper() -> Response:
    """Start the cleanup helper chat (or hand the request to the one already open) and show it."""
    refusal = identity.forbid_unless_owner()
    if refusal is not None:
        return refusal
    body = parse_request_body(HelperRequest)
    reason = "The user asked for help freeing disk space from the Disk Space page."
    return model_response(HelperResponse(**helper.start(reason, "page", body.item)))


@app.route("/api/settings", methods=["POST"])
def api_settings() -> Response:
    """Change the alert line."""
    refusal = identity.forbid_unless_owner()
    if refusal is not None:
        return refusal
    body = parse_request_body(SettingsRequest)
    return model_response(SettingsResponse(**store.write_settings(body.threshold)))


def _pretty(value: object) -> Response:
    return Response(json.dumps(value, indent=2), mimetype="text/plain")


@app.route("/raw/history/<entry_id>")
def raw_history(entry_id: str) -> Response:
    """One history record exactly as stored."""
    entry = store.find_history(entry_id)
    if entry is None:
        abort(404)
    return _pretty(entry)


@app.route("/raw/snapshot/<snapshot_id>")
def raw_snapshot(snapshot_id: str) -> Response:
    """One measurement exactly as stored."""
    snapshot = store.read_snapshot(snapshot_id)
    if snapshot is None:
        abort(404)
    return _pretty(snapshot)


@app.route("/raw/snapshot/<snapshot_id>/<group>")
def raw_du(snapshot_id: str, group: str) -> Response:
    """The raw du output one group of a measurement was built from."""
    if snapshot_id.startswith(".") or not group.replace("-", "").isalnum():
        abort(404)
    path = store.snapshots_dir() / snapshot_id / f"du-{group}.txt"
    if not path.is_file():
        abort(404)
    return Response(path.read_text(), mimetype="text/plain")


def main() -> None:
    # With a subcommand (check, scan, status, delete, helper) it is the command line, not the server.
    if len(sys.argv) > 1:
        raise SystemExit(cli.main(sys.argv[1:]))
    setup_logging()
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
