"""Turns email, Slack and the calendar into a simple todo list, kept up to date as new items land.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (the items and their statuses, the theme) is read and written under
  ``DATA_DIR`` (defined below), never a hardcoded ``data/.apps/todo-list/`` at the call site.
  ``DATA_DIR`` defaults to ``data/.apps/todo-list/`` but honors the ``TODO_LIST_DATA_DIR`` env
  var, so an editing agent can point a throwaway instance at a *copy* of the data instead of
  the live store (see the update-app skill).
- Static assets shipped alongside this file (the page, the seed): ``Path(__file__).parent / "assets/..."``.
- Listen port: bind ``PORT`` (defined below), which defaults to this app's assigned port but
  honors the ``TODO_LIST_PORT`` env var.

Every open window holds a WebSocket to ``/ws``; an item acted on in one window, items another
process adds (``POST /api/items``, the calendar sync) and a theme change (``POST /api/theme``)
reach every window live.
"""

import json
import os
import threading
from datetime import date
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask
from flask import Response
from flask import abort
from flask import jsonify
from flask import request
from flask import send_file
from flask_sock import Sock  # ty: ignore[unresolved-import]
from simple_websocket import ConnectionClosed
from werkzeug.serving import run_simple

DATA_DIR = Path(os.environ.get("TODO_LIST_DATA_DIR", "data/.apps/todo-list"))
PORT = int(os.environ.get("TODO_LIST_PORT", "8082"))

# The browser-side modules the workspace shell builds and every app serves from its own
# origin: the app contract (how a page talks to the shell framing it) and the element context
# menu (the right-click menu whose last rows hand the clicked element to a chat).
SHELL_STATIC_MODULES_DIR = Path("system/apps/system_interface/imbue/system_interface/static/_static")
SHELL_STATIC_MODULE_NAMES = ("app_contract.js", "context_menu.js")

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

app = Flask("todo_list", static_folder=None)
sock = Sock(app)

ASSETS_DIR = Path(__file__).parent / "assets"
STATE_FILE = DATA_DIR / "state.json"
THEME_FILE = DATA_DIR / "theme.json"
VALID_ACTIONS = ("done", "drop", "open")
VALID_SECTIONS = ("promise", "request", "know")
# The page's colors as shipped (the main blue, the page behind the notes, the group headings' tape) and its
# style (empty for the scrapbook, "bullet-journal" for one pen on dotted paper): the theme route overrides
# these. An empty heading keeps the three stepped blues the page ships with.
DEFAULT_THEME = {"accent": "#003f91", "background": "#eef1f5", "heading": "", "style": ""}

# One shared list for every window. Each item's status is "open", "done" or "drop".
_lock = threading.Lock()
_clients: set[Any] = set()


def _seed_state() -> dict[str, Any]:
    seed = json.loads((ASSETS_DIR / "seed.json").read_text())
    today = date.today()
    for item in seed["items"]:
        item["status"] = "open"
        # The sample items' timing is relative to today, so the demo list never goes stale.
        if "due_in_days" in item:
            due = (today + timedelta(days=item.pop("due_in_days"))).isoformat()
            item["due"] = f"{due}T{item.pop('due_time')}" if "due_time" in item else due
        if "asked_days_ago" in item:
            item["asked"] = (today - timedelta(days=item.pop("asked_days_ago"))).isoformat()
    return seed


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        return _seed_state()
    state = json.loads(STATE_FILE.read_text())
    # Keep saved statuses, but pick up any wording or fields changed in the seed since.
    seed_by_id = {item["id"]: item for item in _seed_state()["items"]}
    for item in state["items"]:
        status = item["status"]
        item.update(seed_by_id.pop(item["id"], {}))
        item["status"] = status
    # Seed items added since the state was first saved join the list as new.
    state["items"].extend(seed_by_id.values())
    return state


def _save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


def _load_theme() -> dict[str, str]:
    theme = dict(DEFAULT_THEME)
    if THEME_FILE.is_file():
        theme.update(json.loads(THEME_FILE.read_text()))
    return theme


def _save_theme(theme: dict[str, str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THEME_FILE.write_text(json.dumps(theme, indent=1))


def _broadcast(message: dict[str, Any]) -> None:
    text = json.dumps(message)
    for ws in list(_clients):
        try:
            ws.send(text)
        except ConnectionClosed:
            _clients.discard(ws)


@app.route("/")
def index() -> Response:
    page = (ASSETS_DIR / "index.html").read_text()
    return Response(page.replace("</body>", SHELL_PAGE_SCRIPT + "\n</body>"), mimetype="text/html")


@app.route("/api/state")
def get_state() -> Response:
    with _lock:
        state = _load_state()
    return jsonify({"items": state["items"], "theme": _load_theme()})


@app.route("/api/items/<item_id>", methods=["POST"])
def act_on_item(item_id: str) -> Response:
    action = (request.get_json(silent=True) or {}).get("action")
    if action not in VALID_ACTIONS:
        abort(400)
    with _lock:
        state = _load_state()
        item = next((it for it in state["items"] if it["id"] == item_id), None)
        if item is None:
            abort(404)
        if item["status"] != action:
            item["status"] = action
            _save_state(state)
            _broadcast({"type": "status", "id": item_id, "status": action})
    return jsonify({"ok": True})


@app.route("/api/items", methods=["POST"])
def add_items() -> Response:
    """Append items another process found (a calendar sync); an item whose id is already listed is left as it is."""
    body = request.get_json(silent=True) or {}
    added = [dict(item) for item in body.get("items", []) if isinstance(item, dict)]
    for item in added:
        if not item.get("id") or item.get("section") not in VALID_SECTIONS or not item.get("summary"):
            abort(400)
    with _lock:
        state = _load_state()
        known_ids = {item["id"] for item in state["items"]}
        new_items = [item for item in added if item["id"] not in known_ids]
        for item in new_items:
            item.setdefault("src", "calendar")
            item.setdefault("where", "")
            item.setdefault("raw", "")
            item["status"] = "open"
        state["items"].extend(new_items)
        _save_state(state)
        if new_items:
            _broadcast({"type": "added", "items": new_items})
    return jsonify({"ok": True, "added": len(new_items)})


@app.route("/api/theme", methods=["POST"])
def set_theme() -> Response:
    body = request.get_json(silent=True) or {}
    with _lock:
        theme = _load_theme()
        theme.update({key: str(value) for key, value in body.items() if key in DEFAULT_THEME})
        _save_theme(theme)
        _broadcast({"type": "theme", "theme": theme})
    return jsonify({"ok": True, "theme": theme})


@app.route("/api/reset", methods=["POST"])
def reset() -> Response:
    with _lock:
        _save_state(_seed_state())
        _broadcast({"type": "reset"})
    return jsonify({"ok": True})


@sock.route("/ws")
def live_updates(ws: Any) -> None:
    _clients.add(ws)
    try:
        # The page sends nothing; the loop holds the socket open until it closes (receive raises then).
        for _message in iter(ws.receive, None):
            pass
    finally:
        _clients.discard(ws)


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    return send_file(module_path.absolute(), mimetype="text/javascript")


FONT_NAMES = ("Caveat.woff2", "Shrikhand.woff2")


@app.route("/fonts/<name>")
def font(name: str) -> Response:
    """The handwritten and display fonts, shipped with the app so it looks the same offline."""
    if name not in FONT_NAMES:
        abort(404)
    response = send_file((ASSETS_DIR / "fonts" / name).absolute(), mimetype="font/woff2")
    response.headers["Cache-Control"] = "public, max-age=604800"
    return response


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    run_simple("127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
