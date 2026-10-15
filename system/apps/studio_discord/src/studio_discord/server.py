"""The Studio Discord window: join the Imbue Studio Discord, and read it from inside Studio.

Runs from the workspace root (supervisord's working directory). Settings, all overridable so a throwaway instance
can run beside the live one:

- ``STUDIO_DISCORD_DATA_DIR`` -- persistent state; defaults to ``data/.apps/studio-discord`` (the workspace's
  per-app data convention).
- ``STUDIO_DISCORD_PORT`` / ``STUDIO_DISCORD_HOST`` -- where to listen; loopback by default.
"""

import json
import os
from html import escape
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify
from werkzeug.serving import run_simple

from studio_discord import config, discord, invite, studio, views
from studio_discord.store import Store, now_iso
from studio_discord.web import HttpError, UnreachableError

DATA_DIR = Path(os.environ.get("STUDIO_DISCORD_DATA_DIR", "data/.apps/studio-discord"))
PORT = int(os.environ.get("STUDIO_DISCORD_PORT", "8086"))
HOST = os.environ.get("STUDIO_DISCORD_HOST", "127.0.0.1")
ASSETS = Path(__file__).parent / "assets"

# Every page carries this: it connects the page to the shell framing it (so the shell reopens the window where
# it was) and installs the element context menu. Outside the shell it runs harmlessly.
SHELL_PAGE_SCRIPT = """<script type="module">
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
</script>"""

app = Flask("studio_discord", static_folder=None)
store = Store(DATA_DIR)


def _widget() -> dict[str, Any] | None:
    try:
        return discord.widget()
    except (HttpError, UnreachableError, ValueError):
        return None


def _channels_view() -> list[dict[str, Any]]:
    return [views.channel_view(c, store.channel(c.id), store.messages(c.id)) for c in config.CHANNELS]


def _view_name(state: dict[str, Any], granted: bool) -> str:
    if state["uninstalled"]:
        return "uninstalled"
    if granted and not state["disconnected"]:
        return "connected"
    if state["pending_request"]:
        return "waiting"
    return "offer"


@app.get("/api/state")
def api_state() -> Response:
    state = store.state()
    widget = _widget()
    try:
        granted = studio.discord_access_granted()
    except (HttpError, UnreachableError, ValueError, AttributeError):
        granted = False
    if granted and state["pending_request"]:
        state = store.update_state(pending_request=None, disconnected=False)
    view = _view_name(state, granted)
    account = None
    if view == "connected":
        try:
            account = views.account(discord.me())
        except (HttpError, UnreachableError):
            account = None
    return jsonify(
        view=view,
        invite=invite.join_link(widget),
        server=views.widget_view(widget),
        channels=_channels_view(),
        account=account,
        access_granted_but_disconnected=granted and state["disconnected"],
    )


def _read_channel(channel: config.Channel) -> None:
    """Read one Studio channel from Discord and keep the raw records. Raises what the read raised."""
    raw = discord.channel(channel.id)
    store.save_channel(channel.id, raw)
    if channel.kind == "forum":
        found = discord.latest_posts(channel.id, config.MESSAGES_PER_CHANNEL)
        records: list[dict[str, Any]] = [
            {"id": f"thread:{t['id']}", "kind": "thread", "thread": t} for t in found.get("threads", [])
        ]
        records += [
            {"id": f"first:{m['channel_id']}", "kind": "first_message", "channel_id": m["channel_id"], "message": m}
            for m in found.get("first_messages", [])
        ]
    else:
        records = discord.latest_messages(channel.id, config.MESSAGES_PER_CHANNEL)
    store.save_messages(channel.id, records)


@app.get("/api/messages")
def api_messages() -> Response:
    """Refresh every Studio channel from Discord, then return what is kept. Never reads anything else."""
    problem = None
    for channel in config.CHANNELS:
        try:
            _read_channel(channel)
        except HttpError as error:
            if discord.is_not_a_member(error):
                return jsonify(joined=False, channels=[])
            problem = "unreachable"
            break
        except UnreachableError:
            problem = "unreachable"
            break
    loaded = [store.messages(c.id).get("loaded_at") for c in config.CHANNELS]
    return jsonify(
        joined=True,
        problem=problem,
        loaded_at=min((t for t in loaded if t), default=None),
        channels=_channels_view(),
    )


@app.post("/api/connect")
def api_connect() -> Response | tuple[Response, int]:
    try:
        if studio.discord_access_granted():
            store.update_state(disconnected=False, pending_request=None)
            return jsonify(view="connected")
        request_id = studio.request_discord_access()
    except (HttpError, UnreachableError) as error:
        return jsonify(error=str(error)), 502
    store.update_state(disconnected=False, pending_request={"id": request_id, "at": now_iso()})
    return jsonify(view="waiting")


@app.post("/api/cancel")
def api_cancel() -> Response:
    store.update_state(pending_request=None)
    return jsonify(view="offer")


@app.post("/api/disconnect")
def api_disconnect() -> Response:
    store.update_state(disconnected=True, pending_request=None)
    return jsonify(view="offer")


@app.post("/api/uninstall")
def api_uninstall() -> Response | tuple[Response, int]:
    try:
        removed = studio.remove_desktop_icons()
    except (HttpError, UnreachableError) as error:
        return jsonify(error=str(error)), 502
    previous = store.state()["removed_icons"]
    store.update_state(uninstalled=True, disconnected=True, pending_request=None, removed_icons=previous + removed)
    return jsonify(view="uninstalled")


@app.post("/api/undo-uninstall")
def api_undo_uninstall() -> Response | tuple[Response, int]:
    try:
        studio.restore_desktop_icons(store.state()["removed_icons"])
    except (HttpError, UnreachableError) as error:
        return jsonify(error=str(error)), 502
    store.update_state(uninstalled=False, removed_icons=[])
    return jsonify(view="offer")


@app.get("/raw/<channel_id>")
def raw(channel_id: str) -> Response:
    """Everything kept for one Studio channel, exactly as Discord returned it."""
    if channel_id not in config.CHANNEL_IDS:
        abort(404)
    record = {"channel": store.channel(channel_id), "messages": store.messages(channel_id)}
    name = next(c.name for c in config.CHANNELS if c.id == channel_id)
    body = escape(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False))
    page = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>#{escape(name)} raw</title>"
        "<meta name='color-scheme' content='light dark'></head>"
        f"<body style='margin:0;padding:16px;font:13px/1.45 ui-monospace,Menlo,monospace'>"
        f"<p><a href='/'>Back</a> &middot; <a href='{config.channel_url(channel_id)}' target='_blank' "
        f"rel='noopener'>Open in Discord</a></p><pre style='white-space:pre-wrap;overflow-wrap:anywhere'>{body}</pre>"
        f"{SHELL_PAGE_SCRIPT}</body></html>"
    )
    return Response(page, mimetype="text/html")


@app.route("/")
def index() -> Response:
    page = (ASSETS / "index.html").read_text().replace("__SHELL_PAGE_SCRIPT__", SHELL_PAGE_SCRIPT)
    return Response(page, mimetype="text/html")


@app.route("/app.js")
def app_js() -> Response:
    return Response((ASSETS / "app.js").read_text(), mimetype="text/javascript")


@app.route("/_static/<name>")
def shell_module(name: str) -> Response:
    if name not in studio.SHELL_MODULE_NAMES:
        abort(404)
    try:
        body = studio.fetch_shell_module(name)
    except studio.ShellUnavailableError:
        abort(502)
    return Response(body, mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
