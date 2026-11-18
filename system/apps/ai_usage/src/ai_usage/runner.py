"""Shows how much of your AI tool usage limits (for each AI the chats here run on) you have used and when each limit resets..

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/ai-usage/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/ai-usage/`` but
  honors the ``AI_USAGE_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``AI_USAGE_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://ai-usage.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from app_schema.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from app_schema.routes import model_response
from flask import Flask, Response, abort, request, send_file
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from loguru import logger
from pydantic import Field
from werkzeug.serving import run_simple

from ai_usage.usage import (
    OUTSIDE,
    AgentInfo,
    Attribution,
    Source,
    UsageStore,
    chats_to_show,
    source_key_for,
    start_background_poller,
)

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/ai-usage/`` but is overridable via the ``AI_USAGE_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/ai-usage/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("AI_USAGE_DATA_DIR", "data/.apps/ai-usage"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``AI_USAGE_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("AI_USAGE_PORT", "8080"))

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


class PointOut(FrozenModel):
    """One point on a chat's running total."""

    t: str = Field(description="When the increase was seen (ISO 8601)")
    value: float = Field(
        description="The chat's running total of credited percent at that time"
    )


class PastTurnOut(FrozenModel):
    """One stretch of a chat's work credited in a past window."""

    started_at: str = Field(description="When the stretch began (ISO 8601)")
    label: str = Field(description="The stretch's label")
    prompt: str | None = Field(description="The message that started it, verbatim, if known")
    summarised: bool = Field(description="Whether the label is an AI-written summary")
    credit: float = Field(description="Percent of the limit credited to it")


class PastChatOut(FrozenModel):
    """One chat's share of a past window."""

    chat_id: str = Field(description="The chat's id")
    name: str = Field(description="The chat's title, or a stand-in for a chat no longer here")
    total: float = Field(description="Percent credited to it in the window")
    points: list[PointOut] = Field(description="Its running total over the window")
    turns: list[PastTurnOut] = Field(description="Its credited stretches, newest first")


class PastWindowOut(FrozenModel):
    """A window of one limit that has since reset."""

    started_at: str = Field(description="When the window began, or when tracking or reporting began (ISO 8601)")
    ended_at: str = Field(description="When it reset (ISO 8601)")
    level: float = Field(description="The highest percent the AI reported for the limit in the window")
    chats: list[PastChatOut] = Field(description="The chats credited in the window, most first")


class LimitOut(FrozenModel):
    """One usage limit as of the latest reading."""

    key: str = Field(description="Stable id of the limit, e.g. 'seven_day'")
    label: str = Field(description="What the AI's usage page calls the limit")
    percent: float = Field(description="How much of the limit is used, 0-100")
    resets_at: str | None = Field(
        description="When the limit resets (ISO 8601), if the AI says"
    )
    window_started_at: str | None = Field(
        description="When the limit last reset (ISO 8601), which is where each chat's running total for it starts over; null if it has not reset since tracking began"
    )
    past_windows: list[PastWindowOut] = Field(
        default_factory=list,
        description="The limit's windows that reset since tracking began, newest first",
    )
    first_read_at: str | None = Field(
        default=None,
        description="When the AI started reporting this limit (ISO 8601), if that was after tracking began; its graph starts here when it has not reset since",
    )


class TurnOut(FrozenModel):
    """One stretch of work by a chat and the usage credited to it."""

    started_at: str = Field(description="When the stretch began (ISO 8601)")
    label: str = Field(
        description="An AI-written summary of the message that started it; the message itself, shortened, until one is written"
    )
    prompt: str | None = Field(
        description="The message that started it, verbatim, if known"
    )
    summarised: bool = Field(description="Whether the label is an AI-written summary")
    credits: dict[str, float] = Field(description="Percent credited per limit key")


class ChatOut(FrozenModel):
    """A chat connected to Studio and the usage credited to it."""

    chat_id: str = Field(description="The chat's id")
    name: str = Field(description="The chat's title")
    totals: dict[str, float] = Field(
        description="Total percent credited per limit key since that limit last reset"
    )
    series: dict[str, list[PointOut]] = Field(
        description="Running total over time per limit key, starting over at each reset of that limit"
    )
    turns: list[TurnOut] = Field(
        description="Stretches of work credited since a limit last reset, newest first"
    )


class SourceOut(FrozenModel):
    """One AI account's usage and how it splits across the chats on it."""

    key: str = Field(description="'<agent type>:<account>'")
    label: str = Field(description="What the user calls this AI, e.g. 'Claude'")
    account_label: str = Field(description="What the account is called")
    checked_at: str | None = Field(
        description="When usage was last read successfully (ISO 8601)"
    )
    error: str | None = Field(description="Why the latest check failed, if it did")
    plan: str | None = Field(description="The plan, e.g. 'Max 20x'")
    limits: list[LimitOut] = Field(description="Each limit as of the latest reading")
    chats: list[ChatOut] = Field(description="Chats on this account, most usage first")
    tracking_since: str | None = Field(
        description="When the first reading was taken (ISO 8601)"
    )
    raw_reading: dict[str, Any] | None = Field(
        description="The latest reading exactly as the AI returned it"
    )
    usage_page: str = Field(
        description="The page in the AI's own settings with the same numbers"
    )


class UntrackedChatOut(FrozenModel):
    """A chat connected to Studio whose kind of AI has no known usage source."""

    chat_id: str = Field(description="The chat's id")
    name: str = Field(description="The chat's title")
    agent_type: str = Field(description="The kind of AI running it")


class UsageResponse(FrozenModel):
    """Usage of every AI account the chats here use, and how it splits across them."""

    sources: list[SourceOut] = Field(
        description="One entry per (kind of AI, account) in use"
    )
    untracked_chats: list[UntrackedChatOut] = Field(
        description="Chats whose AI has no known usage source"
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
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
    ("GET", "/assets/{name}"): SchemalessRoute(reason="the page's script and styles"),
    ("GET", "/api/usage"): RouteModels(response_model=UsageResponse),
    ("POST", "/api/check"): RouteModels(response_model=UsageResponse),
    ("POST", "/api/activity"): SchemalessRoute(
        reason="a chat hook's raw event, forwarded as the hook received it"
    ),
    ("POST", "/_csp-report"): SchemalessRoute(
        reason="the browser's report of what the page's policy blocked"
    ),
}

app = Flask("ai_usage", static_folder=None)
# The app describes its own API: an OpenAPI document of every route below, served at the
# manifest's api_schema_path.
register_api_schema_route(
    app,
    DEFAULT_API_SCHEMA_PATH,
    title="AI Usage Limits",
    description="Shows how much of your AI tool usage limits (for each AI the chats here run on) you have used and when each limit resets.",
    route_table=ROUTE_TABLE,
)


@app.after_request
def add_content_security_policy(response: Response) -> Response:
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


ASSETS_DIR = Path(__file__).parent / "assets"
ASSET_TYPES = {"app.js": "text/javascript", "app.css": "text/css"}
SCHEDULED_READING_SECONDS = 300

store = UsageStore(DATA_DIR)


@app.route("/")
def index() -> Response:
    """The app's page: each AI account's usage limits, and how much of them each chat here used over time."""
    page = (
        (ASSETS_DIR / "index.html")
        .read_text()
        .replace("__SHELL_PAGE_SCRIPT__", SHELL_PAGE_SCRIPT)
    )
    return Response(page, mimetype="text/html")


@app.route("/assets/<name>")
def asset(name: str) -> Response:
    """The page's script or stylesheet."""
    if name not in ASSET_TYPES:
        abort(404)
    return send_file((ASSETS_DIR / name).absolute(), mimetype=ASSET_TYPES[name])


def chat_out(agent: AgentInfo, attribution: Attribution) -> ChatOut:
    chat_id = agent.chat_id
    return ChatOut(
        chat_id=chat_id,
        name=agent.name,
        totals={k: round(v, 3) for k, v in attribution.totals.get(chat_id, {}).items()},
        series={
            key: [PointOut(t=t, value=v) for t, v in points]
            for key, points in attribution.series.get(chat_id, {}).items()
        },
        turns=[
            TurnOut(
                started_at=turn.started_at,
                label=turn.label,
                prompt=turn.prompt,
                summarised=turn.summarised,
                credits={k: round(v, 3) for k, v in turn.credits.items()},
            )
            for turn in attribution.turns.get(chat_id, [])
        ],
    )


def past_windows_out(attribution: Attribution, key: str, agents: list[AgentInfo]) -> list[PastWindowOut]:
    names = {a.chat_id: a.name for a in agents}
    out: list[PastWindowOut] = []
    for window in reversed(attribution.past_windows.get(key, [])):
        chats = [
            PastChatOut(
                chat_id=chat_id,
                name=names.get(chat_id, "A chat no longer here"),
                total=round(total, 3),
                points=[PointOut(t=t, value=v) for t, v in window.series.get(chat_id, [])],
                turns=[
                    PastTurnOut(
                        started_at=turn.started_at,
                        label=turn.label,
                        prompt=turn.prompt,
                        summarised=turn.summarised,
                        credit=round(turn.credit, 3),
                    )
                    for turn in window.turns
                    if turn.chat_id == chat_id
                ],
            )
            for chat_id, total in sorted(window.totals.items(), key=lambda item: -item[1])
            if chat_id != OUTSIDE
        ]
        out.append(
            PastWindowOut(started_at=window.started_at, ended_at=window.ended_at, level=window.level, chats=chats)
        )
    return out


def source_out(source: Source, agents: list[AgentInfo]) -> SourceOut:
    latest = store.latest_reading(source.key)
    readings = store.source_readings(source.key)
    attribution = store.attribute(source)
    members = [a for a in agents if source_key_for(a) == source.key]
    chats = sorted(
        (chat_out(a, attribution) for a in chats_to_show(members, attribution)),
        key=lambda c: -sum(c.totals.values()),
    )
    limits = (
        [
            LimitOut(
                key=l.key,
                label=l.label,
                percent=l.percent,
                resets_at=l.resets_at,
                window_started_at=attribution.window_starts.get(l.key),
                first_read_at=attribution.first_read.get(l.key),
                past_windows=past_windows_out(attribution, l.key, agents),
            )
            for l in source.provider.parse_limits(latest["raw"])
        ]
        if latest is not None
        else []
    )
    return SourceOut(
        key=source.key,
        label=source.provider.label,
        account_label=source.account_label,
        checked_at=latest["taken_at"] if latest else None,
        error=store.last_error.get(source.key),
        plan=source.provider.plan(source.account),
        limits=limits,
        chats=chats,
        tracking_since=readings[0]["t"] if readings else None,
        raw_reading=latest["raw"] if latest else None,
        usage_page=source.provider.usage_page,
    )


def build_usage() -> UsageResponse:
    agents = store.agents()
    tracked_chats = {a.chat_id for a in agents if source_key_for(a) is not None}
    untracked: dict[str, UntrackedChatOut] = {}
    for a in agents:
        if a.chat_id not in tracked_chats and a.chat_id not in untracked:
            untracked[a.chat_id] = UntrackedChatOut(
                chat_id=a.chat_id, name=a.name, agent_type=a.agent_type
            )
    return UsageResponse(
        sources=[source_out(s, agents) for s in store.sources()],
        untracked_chats=list(untracked.values()),
    )


@app.route("/api/usage")
def usage() -> Response:
    """Current usage of every AI account the chats here use, and each chat's share of it over time."""
    return model_response(build_usage())


@app.route("/api/check", methods=["POST"])
def check_now() -> Response:
    """Look up the chats and their names again and read every account's usage right now, then answer like /api/usage."""
    store.refresh_agents_now()
    store.take_all_readings(trigger={"chat_id": None, "event": "check-now"}, wait=True)
    return model_response(build_usage())


@app.route("/api/activity", methods=["POST"])
def activity() -> Response:
    """A chat reports that it just did something; usage is read and credited to the chats working."""
    chat_id = request.headers.get("X-Chat-Id", "").strip()
    event = request.headers.get("X-Event", "activity").strip()[:20]
    if not chat_id:
        return Response(status=204)
    try:
        hook_input = json.loads(request.get_data(as_text=True) or "{}")
    except json.JSONDecodeError:
        hook_input = {}
    store.record_activity(
        chat_id,
        event,
        hook_input if isinstance(hook_input, dict) else {},
    )
    return Response(status=204)


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


def main() -> None:
    setup_logging()
    start_background_poller(store, SCHEDULED_READING_SECONDS)
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
