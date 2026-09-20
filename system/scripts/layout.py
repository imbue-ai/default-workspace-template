#!/usr/bin/env python3
"""Agent-facing helper for the workspace desktop: read what is open, open and arrange windows, edit a desktop.

Subcommands:
    context                             Show each browser client: its active desktop, connection state, recent messages.
    desktops                            List every desktop with its windows and shortcuts, and every client.
    list                                List every app (launch paths, running or not) with its windows, plus the desktops.
    load <desktop>                      Switch the target client onto a desktop.
    open <app|url> [--path P | --launch ID --param k=v ...] [--if-present focus|new]
                                        Open a window of an app (a bare https:// URL opens a new browser on that page).
    focus <window>                      Restore and raise a window.
    minimize <window>                   Put a window out of sight (its frame is kept).
    restore <window>                    Bring a minimized or maximized window back to its frame.
    maximize <window>                   Fill the backdrop with a window.
    place <window> --zone Z | --frame x,y,w,h
                                        Snap a window to a half (left, right) or maximize it, or set its frame.
    close <window>                      Close a window for everyone.
    navigate <window> <path>            Point a window at another path under its app.
    refresh <window> | --app <name>     Reload one window's page, or every page of an app.
    shortcuts                           List a desktop's backdrop shortcuts.
    shortcut set <app> <launch> [--mode] [--cell c,r]
                                        Add a shortcut to a desktop, or change its mode or cell.
    shortcut move <app> <launch> --cell c,r
                                        Move a shortcut to another cell.
    shortcut remove <app> <launch>      Take a shortcut off a desktop.
    wallpaper <kind> <name> | none      Set a desktop's wallpaper (a bundled image, a file), or clear it.

A *desktop* is a named, shared collection of *windows*: each window is one page of an app,
named by its app and the path under the app's origin it is at (``chat`` at ``/?chat=<id>``,
``terminal`` at ``/?session=<name>``, ``files`` at ``/notes/``). Windows and desktops are shared
by everyone; where each window sits on a screen (its frame, whether it is minimized or
maximized) is one client's own *placement*. Every browser *client* has one active desktop.

A window is named by its id (``win-<hex>``, from ``desktops`` or the ``open`` that made it), by
``self`` (the caller's own chat window), or by an app name (that app's most recently focused
window on the target client's active desktop).

Every op targets exactly one client: ``--client <id>`` (from ``context``), else the client
that most recently messaged you, else the one connected client; with several clients and no
way to tell, the op is refused and lists them. The shell edits that client's placements
itself, so an op lands whether or not a browser is connected, and a connected window shows it
within a redraw. An op edits the client's active desktop; ``--desktop <name>`` edits that
desktop and switches the client to it.

``open`` opens a window at ``--path``, or at one of the app's *launch paths* (``--launch <id>``
with ``--param name=value`` for its parameters; with neither, the app's default launch path).
A window of the app already at that path is focused rather than duplicated unless
``--if-present new`` is passed. The window's id (the new one's, or the focused one's) is
printed to stdout. To open a folder in the file viewer, ``open files --path /notes/`` (the ``path`` launch parameter is the same:
``open files --param path=/notes/``).

Every op POSTs one body ``{op, args, requester}`` to a loopback-only endpoint on the shell:
``requester`` is the caller's own chat, ``{"app": "chat", "marker": $MINDS_CHAT_ID}`` (the chat
app sets ``MINDS_CHAT_ID`` on every agent it creates; ``MNGR_AGENT_ID`` stands in for an agent
that is its own chat), which is what ``self`` means and how the shell attributes the op to a
client (the one that last messaged that chat). ``desktops`` and ``list`` read ``GET
/api/inventory`` instead.

Output for the read commands is YAML by default; pass ``--json`` for the raw structured
object. Descriptions of what an op did go to stderr; stdout carries only the window id of an
``open``, the structured output of the read commands, and the desktop's shortcuts as they stand
after a ``shortcut`` write.

Retired verbs (``split``, ``move``, ``rename``, ``delete``, ``stop``, ``start``,
``replace-url``, ``inspect``, ``where``, ``views``) and the old ``app:``, ``chat:``,
``chat-terminal:``, ``terminal:``, ``service:``, ``url:``, and ``subagent:`` spellings are
refused with the verb or form to use instead.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import tomlkit
import yaml

DEFAULT_APPS_FILE = "data/.state/apps.toml"
ENV_APPS_FILE = "MINDS_APPS_FILE"
DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"
ENV_MINDS_CHAT_ID = "MINDS_CHAT_ID"
ENV_MNGR_AGENT_ID = "MNGR_AGENT_ID"

# The requester every op carries is the caller's own chat: the chat app's name, and the chat
# id its window's path carries.
REQUESTER_APP = "chat"

# A bare URL opens a new browser on that page: the browser app's ``new`` launch path with the
# URL as its ``url`` parameter (system/apps/browser/app.toml).
_BROWSER_APP_NAME = "browser"
_BROWSER_NEW_LAUNCH = "new"
_BROWSER_URL_PARAM = "url"
_EXTERNAL_URL_PREFIXES = ("https://", "http://")

# The spellings of the tabbed shell, refused by name with the form to use instead, so an agent
# working from an old note is told what to type rather than waiting on a registration that
# never comes.
_RETIRED_PREFIXES = (
    "app:",
    "chat-terminal:",
    "chat:",
    "terminal:",
    "service:",
    "url:",
    "subagent:",
)
# The verbs of the tabbed shell, each with its replacement.
_RETIRED_VERBS = {
    "split": "'place' sets where a window sits (--zone left|right|maximized, or --frame x,y,w,h); 'open' puts a new window on the desktop",
    "move": "'place' sets where a window sits (--zone left|right|maximized, or --frame x,y,w,h)",
    "rename": "a title belongs to the app that owns the page: the chat's POST /api/chats/<id>/rename, a terminal's own rename route",
    "delete": "close the window with 'close'; what backs the page is the app's to end (the chat's destroy route, the terminal's delete route, the browser's DELETE /browsers/<name>)",
    "stop": "the app's own route stops what backs a page (the chat's stop route, the browser's POST /browsers/<name>/stop)",
    "start": "the app's own route starts what backs a page (the browser's POST /browsers/<name>/start)",
    "replace-url": "'navigate <window> <path>' points a window at another path under its app",
    "inspect": "'desktops' lists every desktop with its windows; 'list' adds every app",
    "where": "'desktops' lists every desktop with its windows and their ids",
    "views": "'desktops' lists every desktop and which clients are on each",
}

# A bare word that may name an app: the registry's name rule (``forward_port.py``'s
# ``NAME_PATTERN``, ``MAX_SERVICE_NAME_LENGTH``, ``RESERVED_NAMES``, and
# ``RESERVED_NAME_PREFIXES``), so a name the registry could never hold is refused here rather
# than waited for.
_APP_NAME_PATTERN = re.compile(r"^[a-z0-9_]+(?:-[a-z0-9_]+)*$")
_MAX_APP_NAME_LENGTH = 32
_RESERVED_APP_NAMES = frozenset({"localhost", "auth"})
_RESERVED_APP_NAME_PREFIXES = ("host-", "agent-")
# A window id as the shell mints it.
_WINDOW_ID_PATTERN = re.compile(r"^win-[0-9a-f]{16}$")

# How long ``open`` waits for a freshly-registered app to appear before giving up. The
# supervisord-managed forward_port.py call races with the agent invoking this script right
# after build-app, so a brief window where the row is not yet visible is fine.
_REGISTRATION_TIMEOUT_SECONDS = 5.0
_REGISTRATION_POLL_INTERVAL_SECONDS = 0.25

_ZONES = ("left", "right", "maximized")
_SHORTCUT_MODES = ("focus", "new")
_IF_PRESENT_CHOICES = ("focus", "new")
_WALLPAPER_KINDS = ("bundled", "file")
_NO_WALLPAPER = "none"

# A read answers from memory and the state files; an ``open`` waits on nothing slower than a
# file write either, but keeps a wider bound for a shell busy with a client's save.
_READ_TIMEOUT_SECONDS = 10.0
_OP_TIMEOUT_SECONDS = 30.0

# Exit codes: 0 / 1 / 3. Agents branch on "did it work"; the one distinct code worth its own
# slot is a shell or app that cannot act right now (a 409 or a 503), where retry-with-backoff
# is the right response. Slot 2 is left to argparse's usage exit.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 3

# The caller's own chat window. Valid wherever a window is named; the shell resolves it from
# the op's ``requester`` (and refuses it with a 400 when the op carries none).
_SELF_REF = "self"


def _workspace_base_url() -> str:
    return os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL).rstrip("/")


def _own_chat_id() -> str:
    """The caller's chat id: ``MINDS_CHAT_ID`` from the chat app that created the agent, else the
    agent's own id (an agent created any other way is its own chat), else "" outside an agent."""
    return os.environ.get(ENV_MINDS_CHAT_ID, "") or os.environ.get(ENV_MNGR_AGENT_ID, "")


def _requester() -> dict[str, str] | None:
    """The caller's own chat as the op route's requester, or None outside an agent: what ``self``
    names and what every op carries so the shell knows who asked."""
    chat_id = _own_chat_id()
    if not chat_id:
        return None
    return {"app": REQUESTER_APP, "marker": chat_id}


def _apps_file() -> Path:
    """Path to the app registry: ``data/.state/apps.toml`` relative to cwd (the repo root), or ``MINDS_APPS_FILE``."""
    return Path(os.environ.get(ENV_APPS_FILE, DEFAULT_APPS_FILE))


# ---------- Names ----------


def _fail(message: str) -> None:
    sys.stderr.write(f"error: {message}\n")
    raise SystemExit(EXIT_ERROR)


def _retired_spelling_message(value: str) -> str:
    prefix = next(candidate for candidate in _RETIRED_PREFIXES if value.startswith(candidate))
    remainder = value[len(prefix) :]
    if prefix == "app:":
        name, _, query = remainder.partition("?")
        if query.startswith("instance="):
            hint = (
                f"a window is named by its id (see 'desktops'); to open one, give the app and the path of its page: "
                f"'layout.py open {name or '<app>'} --path <path>'"
            )
        else:
            hint = f"give the app name on its own: 'layout.py open {name or '<app>'}'"
    elif prefix == "chat:":
        hint = "a chat's window is the chat app at its page: 'layout.py open chat --path \"/?chat=<chat-id>\"'"
    elif prefix == "chat-terminal:":
        hint = "an agent's terminal is the back face of its chat: open the chat's page, 'layout.py open chat --path \"/?chat=<chat-id>\"'"
    elif prefix == "terminal:":
        hint = f"a terminal's window is the terminal app at its page: 'layout.py open terminal --path \"/?session={remainder or '<session>'}\"'"
    elif prefix == "service:":
        name, _, query = remainder.partition("?")
        hint = f"give the app name on its own: 'layout.py open {name or '<app>'}'" + (
            f" (with --path \"/?{query}\" for one page of it)" if query else ""
        )
    elif prefix == "url:":
        hint = "pass the URL itself: 'layout.py open https://...' opens it in a new browser"
    else:
        hint = "a subagent's view is a page of the chat app: 'layout.py open chat --path <the subagent view's path>'"
    return (
        f"{value!r} is not how the desktop names things: give an app name and a path. {hint}; "
        f"'layout.py desktops' lists every window with its id"
    )


def _is_external_url(value: str) -> bool:
    return any(value.startswith(prefix) for prefix in _EXTERNAL_URL_PREFIXES)


def _is_app_name(value: str) -> bool:
    return (
        len(value) <= _MAX_APP_NAME_LENGTH
        and _APP_NAME_PATTERN.fullmatch(value) is not None
        and value not in _RESERVED_APP_NAMES
        and not value.startswith(_RESERVED_APP_NAME_PREFIXES)
    )


def _refuse_retired_spelling(value: str) -> None:
    if any(value.startswith(prefix) for prefix in _RETIRED_PREFIXES):
        _fail(_retired_spelling_message(value))


def _app_name(value: str) -> str:
    """An argument that must name an app; the retired spellings and a URL are refused by name."""
    _refuse_retired_spelling(value)
    if _is_external_url(value):
        _fail(f"{value!r} is a URL: only 'open' takes one (it opens the page in a new browser)")
    if not _is_app_name(value):
        _fail(f"{value!r} is not an app name (lowercase words joined by single dashes, at most {_MAX_APP_NAME_LENGTH} characters)")
    return value


def _window_ref(value: str) -> str:
    """An argument that names a window: a window id, ``self``, or an app name."""
    if value == _SELF_REF or _WINDOW_ID_PATTERN.fullmatch(value):
        return value
    _refuse_retired_spelling(value)
    if not _is_app_name(value):
        _fail(f"{value!r} is not a window: give a window id (win-<hex>, from 'desktops'), 'self', or an app name")
    return value


# ---------- The registry ----------


def _read_registry_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        doc = tomlkit.load(f)
    rows: list[dict[str, Any]] = []
    for app in doc.get("apps", []):
        if hasattr(app, "get") and isinstance(app.get("name"), str) and app.get("name"):
            rows.append(dict(app))
    return rows


def _is_app_registered(name: str) -> bool:
    return any(row.get("name") == name for row in _read_registry_rows(_apps_file()))


def _wait_for_registration(name: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _is_app_registered(name):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_REGISTRATION_POLL_INTERVAL_SECONDS)


def _require_registered(app: str) -> int | None:
    if _wait_for_registration(app, _REGISTRATION_TIMEOUT_SECONDS):
        return None
    sys.stderr.write(
        f"error: app {app!r} is not registered in {_apps_file()} after waiting "
        f"{_REGISTRATION_TIMEOUT_SECONDS:.0f}s. Did you forward_port.py / start the app?\n"
    )
    return EXIT_ERROR


# ---------- Transport ----------


def _request_json(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    timeout: float = _READ_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any] | str]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, _maybe_parse_json(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return e.code, _maybe_parse_json(raw)
    except urllib.error.URLError as e:
        return -1, str(e.reason)
    except OSError as e:
        # A read timeout is a TimeoutError rather than a URLError; the shell is as gone either way.
        return -1, str(e)


def _post_layout(
    op: str, args: dict[str, Any], timeout: float = _READ_TIMEOUT_SECONDS
) -> tuple[int, dict[str, Any] | str]:
    """POST {op, args, requester} to /api/layout/broadcast and return (status, parsed_or_raw)."""
    return _request_json(
        "POST",
        f"{_workspace_base_url()}/api/layout/broadcast",
        {"op": op, "args": args, "requester": _requester()},
        timeout=timeout,
    )


def _maybe_parse_json(text: str) -> dict[str, Any] | str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict):
        return parsed
    return text


def _report_failure(op: str, status: int, body: dict[str, Any] | str) -> int:
    """Translate (status, body) into a stderr message + exit code; only a 409 or a 503 (the shell or an app cannot do it right now) has its own code."""
    if status == -1:
        sys.stderr.write(f"error: could not reach the workspace shell: {body}\n")
        return EXIT_ERROR
    detail: str = ""
    if isinstance(body, dict):
        detail = str(body.get("detail", body))
        if status == 412:
            sys.stderr.write(f"error: {op!r} has no client to apply it to (HTTP 412): {detail}\n")
            return EXIT_ERROR
        if status in (409, 503):
            # The shell or the app cannot do it right now (a save in flight, an app still starting up): retry later.
            sys.stderr.write(f"error: {op!r} rejected (HTTP {status}): {detail}\n")
            return EXIT_CONFLICT
        if status == 404:
            sys.stderr.write(f"error: {op!r} target not found (HTTP 404): {detail}\n")
            return EXIT_ERROR
        if status == 400:
            sys.stderr.write(f"error: {op!r} rejected (HTTP 400): {detail}\n")
            return EXIT_ERROR
    else:
        detail = body
    sys.stderr.write(f"error: {op!r} failed (HTTP {status}): {detail}\n")
    return EXIT_ERROR


def _emit_structured(data: Any, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(data, indent=2))
        sys.stdout.write("\n")
    else:
        # ``sort_keys=False`` keeps the server's intentional ordering (windows in opening order).
        yaml.safe_dump(data, sys.stdout, sort_keys=False, default_flow_style=False)


# ---------- Targeting and answers ----------


def _target_args(desktop: str | None, client: str | None) -> dict[str, str]:
    """The ``desktop`` and ``client`` an op names, when it names them."""
    args: dict[str, str] = {}
    if desktop:
        args["desktop"] = desktop
    if client:
        args["client"] = client
    return args


def _window_in(answer: dict[str, Any], window_id: str | None) -> dict[str, Any] | None:
    desktop = answer.get("desktop")
    windows = desktop.get("windows", []) if isinstance(desktop, dict) else []
    for window in windows:
        if isinstance(window, dict) and window.get("id") == window_id:
            return window
    return None


def _describe_window(answer: dict[str, Any], window_id: str | None) -> str:
    """``<id> (<app> at <path>)`` for the window an answer names, or just the id when it is gone (a close)."""
    window = _window_in(answer, window_id)
    if window is None:
        return str(window_id)
    return f"{window_id} ({window.get('app')} at {window.get('path')})"


def _describe_target(answer: dict[str, Any]) -> str:
    return f"desktop {answer.get('desktop_id')} for client {answer.get('client_id')}"


def _run_desktop_op(
    op: str, args: dict[str, Any], describe: Callable[[dict[str, Any]], str]
) -> int:
    """Post one op the shell applies to the files; it answers with the desktop and the client's placements.

    ``describe`` renders the one-line stderr summary from the answer.
    """
    status, body = _post_layout(op, args, timeout=_OP_TIMEOUT_SECONDS)
    if status != 200 or not isinstance(body, dict):
        return _report_failure(op, status, body)
    sys.stderr.write(describe(body) + "\n")
    return EXIT_OK


def _run_transient_op(op: str, args: dict[str, Any]) -> int:
    """Post one of the verbs with nothing to store (refresh); the target client's windows apply it."""
    status, body = _post_layout(op, args)
    if status != 200:
        return _report_failure(op, status, body)
    target = body.get("target_client_id") if isinstance(body, dict) else None
    where = f"client {target}" if target else "every client"
    sys.stderr.write(f"(sent {op} to {where})\n")
    return EXIT_OK


# ---------- The read commands ----------


def _fetch_inventory() -> dict[str, Any] | None:
    status, body = _request_json("GET", f"{_workspace_base_url()}/api/inventory")
    if status != 200 or not isinstance(body, dict):
        sys.stderr.write(f"error: could not read the inventory (HTTP {status}): {body}\n")
        return None
    return body


def _listed_window(window: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": window.get("id"),
        "app": window.get("app"),
        "path": window.get("path"),
        "title": window.get("title"),
        "is_settling": window.get("is_settling"),
    }


def _listed_desktops(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    listed: list[dict[str, Any]] = []
    for desktop in inventory.get("desktops", []) or []:
        if not isinstance(desktop, dict):
            continue
        listed.append(
            {
                "id": desktop.get("id"),
                "name": desktop.get("name"),
                "wallpaper": desktop.get("wallpaper"),
                "shortcuts": desktop.get("shortcuts", []),
                "windows": [_listed_window(window) for window in desktop.get("windows", []) or [] if isinstance(window, dict)],
            }
        )
    return listed


def _listed_clients(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": client.get("id"),
            "active_desktop": client.get("active_desktop"),
            "is_connected": client.get("is_connected"),
            "shown": client.get("shown", []),
            "last_seen": client.get("last_seen"),
        }
        for client in inventory.get("clients", []) or []
        if isinstance(client, dict)
    ]


def _listed_apps(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    """Every app a user can open, with where its windows are."""
    windows_by_app: dict[str, list[dict[str, Any]]] = {}
    for desktop in inventory.get("desktops", []) or []:
        if not isinstance(desktop, dict):
            continue
        for window in desktop.get("windows", []) or []:
            if isinstance(window, dict):
                windows_by_app.setdefault(str(window.get("app")), []).append(
                    {**_listed_window(window), "desktop": desktop.get("id")}
                )
    listed: list[dict[str, Any]] = []
    for app in inventory.get("apps", []) or []:
        if not isinstance(app, dict) or bool(app.get("internal", False)):
            continue
        name = str(app.get("name"))
        listed.append(
            {
                "name": name,
                "display_name": app.get("display_name", name),
                "is_running": app.get("is_running"),
                "launch_paths": app.get("launch_paths", []),
                "default_shortcut": app.get("default_shortcut"),
                "windows": windows_by_app.get(name, []),
            }
        )
    return listed


def _cmd_context(args: argparse.Namespace) -> int:
    status, body = _post_layout("context", {})
    if status != 200 or not isinstance(body, dict):
        return _report_failure("context", status, body)
    _emit_structured(body.get("clients", []), args.json)
    return EXIT_OK


def _cmd_desktops(args: argparse.Namespace) -> int:
    inventory = _fetch_inventory()
    if inventory is None:
        return EXIT_ERROR
    _emit_structured(
        {"desktops": _listed_desktops(inventory), "clients": _listed_clients(inventory)}, args.json
    )
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    inventory = _fetch_inventory()
    if inventory is None:
        return EXIT_ERROR
    _emit_structured(
        {
            "apps": _listed_apps(inventory),
            "desktops": _listed_desktops(inventory),
            "clients": _listed_clients(inventory),
        },
        args.json,
    )
    return EXIT_OK


def _cmd_load(args: argparse.Namespace) -> int:
    return _run_desktop_op(
        "load",
        _target_args(args.desktop, args.client),
        lambda answer: f"switched client {answer.get('client_id')} onto desktop {answer.get('desktop_id')}",
    )


# ---------- open ----------


def _parse_params(raw_params: list[str] | None) -> dict[str, str]:
    """``--param name=value`` pairs as a launch path's query parameters."""
    params: dict[str, str] = {}
    for raw in raw_params or []:
        name, separator, value = raw.partition("=")
        if not separator or not name:
            _fail(f"--param takes name=value, not {raw!r}")
        params[name] = value
    return params


def _open_arguments(args: argparse.Namespace) -> dict[str, Any]:
    """The ``open`` op's arguments: the app, and its path or launch path with parameters.

    A bare URL is the browser app's ``new`` launch path with the URL as its ``url`` parameter.
    """
    params = _parse_params(args.param)
    target: str = args.target
    if _is_external_url(target):
        if args.path or args.launch or params:
            _fail("a URL is opened in a new browser; --path, --launch, and --param do not apply to it")
        return {"app": _BROWSER_APP_NAME, "launch": _BROWSER_NEW_LAUNCH, "params": {_BROWSER_URL_PARAM: target}}
    op_args: dict[str, Any] = {"app": _app_name(target)}
    if args.path:
        if args.launch or params:
            _fail("--path names the page to open; --launch and --param choose a launch path instead. Pass one or the other")
        if not args.path.startswith("/"):
            _fail(f"--path takes a path under the app's origin, starting with '/', not {args.path!r}")
        op_args["path"] = args.path
    if args.launch:
        op_args["launch"] = args.launch
    if params:
        op_args["params"] = params
    return op_args


def _cmd_open(args: argparse.Namespace) -> int:
    op_args = _open_arguments(args)
    if (err := _require_registered(op_args["app"])) is not None:
        return err
    if args.if_present:
        op_args["if_present"] = args.if_present
    op_args.update(_target_args(args.desktop, args.client))
    status, body = _post_layout("open", op_args, timeout=_OP_TIMEOUT_SECONDS)
    if status != 200 or not isinstance(body, dict):
        return _report_failure("open", status, body)
    window_id = body.get("window_id")
    if isinstance(window_id, str) and window_id:
        sys.stdout.write(f"{window_id}\n")
    sys.stderr.write(f"opened window {_describe_window(body, window_id)} on {_describe_target(body)}\n")
    return EXIT_OK


# ---------- The window verbs ----------


def _window_op(op: str, past_tense: str) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        window = _window_ref(args.window)
        return _run_desktop_op(
            op,
            {"window": window, **_target_args(args.desktop, args.client)},
            lambda answer: f"{past_tense} window {_describe_window(answer, answer.get('window_id'))} on {_describe_target(answer)}",
        )

    return run


def _cmd_place(args: argparse.Namespace) -> int:
    window = _window_ref(args.window)
    if bool(args.zone) == bool(args.frame):
        _fail("place takes exactly one of --zone (left, right, maximized) or --frame x,y,width,height")
    op_args: dict[str, Any] = {"window": window, **_target_args(args.desktop, args.client)}
    if args.zone:
        op_args["zone"] = args.zone
        what = f"in the {args.zone} zone"
    else:
        op_args["frame"] = args.frame
        what = f"at frame {args.frame}"
    return _run_desktop_op(
        "place",
        op_args,
        lambda answer: f"placed window {_describe_window(answer, answer.get('window_id'))} {what} on {_describe_target(answer)}",
    )


def _cmd_navigate(args: argparse.Namespace) -> int:
    window = _window_ref(args.window)
    if not args.path.startswith("/"):
        _fail(f"navigate takes a path under the window's app, starting with '/', not {args.path!r}")
    return _run_desktop_op(
        "navigate",
        {"window": window, "path": args.path, **_target_args(args.desktop, args.client)},
        lambda answer: f"pointed window {_describe_window(answer, answer.get('window_id'))} at {args.path} on {_describe_target(answer)}",
    )


def _cmd_refresh(args: argparse.Namespace) -> int:
    if bool(args.window) == bool(args.app):
        _fail("refresh takes a window (a window id, 'self', or an app name) or --app <name> for every page of an app")
    if args.app:
        if args.client or args.desktop:
            _fail("refresh --app reloads every page of the app on every client; --client and --desktop do not apply to it")
        return _run_transient_op("refresh", {"app": _app_name(args.app)})
    return _run_transient_op(
        "refresh", {"window": _window_ref(args.window), **_target_args(args.desktop, args.client)}
    )


# ---------- Shortcuts and the wallpaper ----------


def _shortcuts_of(answer: dict[str, Any]) -> list[Any]:
    desktop = answer.get("desktop")
    return list(desktop.get("shortcuts", [])) if isinstance(desktop, dict) else []


def _run_shortcut_write(op: str, op_args: dict[str, Any], done: str) -> int:
    """Post one shortcut or wallpaper write and print the desktop's shortcuts as they stand after it."""
    status, body = _post_layout(op, op_args, timeout=_OP_TIMEOUT_SECONDS)
    if status != 200 or not isinstance(body, dict):
        return _report_failure(op, status, body)
    sys.stderr.write(f"{done} on {_describe_target(body)}\n")
    _emit_structured({"desktop": body.get("desktop_id"), "shortcuts": _shortcuts_of(body)}, False)
    return EXIT_OK


def _cmd_shortcuts(args: argparse.Namespace) -> int:
    status, body = _post_layout("shortcuts", _target_args(args.desktop, args.client))
    if status != 200 or not isinstance(body, dict):
        return _report_failure("shortcuts", status, body)
    _emit_structured({"desktop": body.get("desktop_id"), "shortcuts": _shortcuts_of(body)}, args.json)
    return EXIT_OK


def _cmd_shortcut_set(args: argparse.Namespace) -> int:
    app = _app_name(args.app)
    op_args: dict[str, Any] = {"app": app, "launch": args.launch, "mode": args.mode}
    if args.cell:
        op_args["cell"] = args.cell
    op_args.update(_target_args(args.desktop, args.client))
    return _run_shortcut_write("shortcut_set", op_args, f"set shortcut {app} {args.launch} ({args.mode})")


def _cmd_shortcut_move(args: argparse.Namespace) -> int:
    app = _app_name(args.app)
    op_args: dict[str, Any] = {"app": app, "launch": args.launch, "cell": args.cell}
    op_args.update(_target_args(args.desktop, args.client))
    return _run_shortcut_write("shortcut_move", op_args, f"moved shortcut {app} {args.launch} to cell {args.cell}")


def _cmd_shortcut_remove(args: argparse.Namespace) -> int:
    app = _app_name(args.app)
    op_args: dict[str, Any] = {"app": app, "launch": args.launch}
    op_args.update(_target_args(args.desktop, args.client))
    return _run_shortcut_write("shortcut_remove", op_args, f"removed shortcut {app} {args.launch}")


def _cmd_wallpaper(args: argparse.Namespace) -> int:
    if args.kind == _NO_WALLPAPER:
        if args.name:
            _fail("'wallpaper none' takes no name")
        wallpaper = None
        done = "cleared the wallpaper"
    else:
        if args.kind not in _WALLPAPER_KINDS or not args.name:
            _fail(f"wallpaper takes '<kind> <name>' with kind one of {list(_WALLPAPER_KINDS)}, or 'none'")
        wallpaper = {"kind": args.kind, "name": args.name}
        done = f"set the wallpaper to {args.kind} {args.name}"
    op_args: dict[str, Any] = {"wallpaper": wallpaper, **_target_args(args.desktop, args.client)}
    status, body = _post_layout("wallpaper", op_args, timeout=_OP_TIMEOUT_SECONDS)
    if status != 200 or not isinstance(body, dict):
        return _report_failure("wallpaper", status, body)
    sys.stderr.write(f"{done} on {_describe_target(body)}\n")
    return EXIT_OK


# ---------- The retired verbs ----------


def _cmd_retired(args: argparse.Namespace) -> int:
    _fail(f"'{args.verb}' is not a desktop verb: {_RETIRED_VERBS[args.verb]}")
    return EXIT_ERROR


# ---------- The parser ----------


_CLIENT_HELP = (
    "The client whose desktop the op targets (an id from ``context``). Defaults to the client that "
    "most recently messaged you, else the one connected client; refused when that settles nothing."
)
_DESKTOP_HELP = (
    "The desktop to edit, by name or id. Defaults to the target client's active desktop; naming "
    "another edits that one and switches the client to it."
)


def _add_target_arguments(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--client", default=None, help=_CLIENT_HELP)
    subparser.add_argument("--desktop", default=None, help=_DESKTOP_HELP)


def _add_json_argument(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--json", action="store_true", help="Emit JSON instead of YAML")


def _add_window_verb(subparsers: Any, verb: str, help_text: str, past_tense: str) -> None:
    subparser = subparsers.add_parser(verb, help=help_text)
    subparser.add_argument("window", help="A window id (win-<hex>), 'self', or an app name")
    _add_target_arguments(subparser)
    subparser.set_defaults(func=_window_op(verb, past_tense))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_context = subparsers.add_parser(
        "context", help="Show each client: its active desktop, connection state, and recent messages"
    )
    _add_json_argument(p_context)
    p_context.set_defaults(func=_cmd_context)

    p_desktops = subparsers.add_parser("desktops", help="List every desktop with its windows and shortcuts, and every client")
    _add_json_argument(p_desktops)
    p_desktops.set_defaults(func=_cmd_desktops)

    p_list = subparsers.add_parser("list", help="List every app with its launch paths and windows, plus the desktops")
    _add_json_argument(p_list)
    p_list.set_defaults(func=_cmd_list)

    p_load = subparsers.add_parser("load", help="Switch the target client onto a desktop")
    p_load.add_argument("desktop", help="The desktop's name or id")
    p_load.add_argument("--client", default=None, help=_CLIENT_HELP)
    p_load.set_defaults(func=_cmd_load)

    p_open = subparsers.add_parser("open", help="Open a window of an app (or a URL in a new browser)")
    p_open.add_argument(
        "target",
        help="An app name (open a window of it), or a bare https:// URL (open it in a new browser)",
    )
    p_open.add_argument(
        "--path",
        default=None,
        help="The page to open, a path under the app's origin: 'open files --path /notes/' opens a folder, "
        "'open chat --path \"/?chat=<id>\"' a chat. Without it, a launch path is used.",
    )
    p_open.add_argument(
        "--launch",
        default=None,
        help="The launch path to open (an id from 'list'); defaults to the app's default launch path.",
    )
    p_open.add_argument(
        "--param",
        action="append",
        default=None,
        metavar="NAME=VALUE",
        help="A launch path parameter (repeatable), e.g. --param workdir=/data, --param path=/notes/.",
    )
    p_open.add_argument(
        "--if-present",
        dest="if_present",
        choices=_IF_PRESENT_CHOICES,
        default=None,
        help="What to do about a window of the app already at the path: focus it (the default) or open another.",
    )
    _add_target_arguments(p_open)
    p_open.set_defaults(func=_cmd_open)

    _add_window_verb(subparsers, "focus", "Restore and raise a window", "focused")
    _add_window_verb(subparsers, "minimize", "Put a window out of sight", "minimized")
    _add_window_verb(subparsers, "restore", "Bring a window back to its frame", "restored")
    _add_window_verb(subparsers, "maximize", "Fill the backdrop with a window", "maximized")
    _add_window_verb(subparsers, "close", "Close a window for everyone", "closed")

    p_place = subparsers.add_parser("place", help="Snap a window to a zone or set its frame")
    p_place.add_argument("window", help="A window id (win-<hex>), 'self', or an app name")
    p_place.add_argument("--zone", choices=_ZONES, default=None, help="Snap to the left or right half, or maximize")
    p_place.add_argument(
        "--frame", default=None, metavar="X,Y,WIDTH,HEIGHT", help="The frame in fractions of the backdrop (0..1)"
    )
    _add_target_arguments(p_place)
    p_place.set_defaults(func=_cmd_place)

    p_navigate = subparsers.add_parser("navigate", help="Point a window at another path under its app")
    p_navigate.add_argument("window", help="A window id (win-<hex>), 'self', or an app name")
    p_navigate.add_argument("path", help="The path under the app's origin, starting with '/'")
    _add_target_arguments(p_navigate)
    p_navigate.set_defaults(func=_cmd_navigate)

    p_refresh = subparsers.add_parser("refresh", help="Reload one window's page, or every page of an app")
    p_refresh.add_argument("window", nargs="?", default=None, help="A window id (win-<hex>), 'self', or an app name")
    p_refresh.add_argument("--app", default=None, help="Reload every page of this app, on every client")
    _add_target_arguments(p_refresh)
    p_refresh.set_defaults(func=_cmd_refresh)

    p_shortcuts = subparsers.add_parser("shortcuts", help="List a desktop's backdrop shortcuts")
    _add_json_argument(p_shortcuts)
    _add_target_arguments(p_shortcuts)
    p_shortcuts.set_defaults(func=_cmd_shortcuts)

    p_shortcut = subparsers.add_parser("shortcut", help="Add, move, or remove a desktop's shortcuts")
    shortcut_subparsers = p_shortcut.add_subparsers(dest="shortcut_command", required=True)
    p_shortcut_set = shortcut_subparsers.add_parser("set", help="Add a shortcut, or change its mode or cell")
    p_shortcut_set.add_argument("app", help="The registered app")
    p_shortcut_set.add_argument("launch", help="The launch path the shortcut runs (an id from 'list')")
    p_shortcut_set.add_argument(
        "--mode",
        choices=_SHORTCUT_MODES,
        default="focus",
        help="focus: raise the app's most recent window, opening one only when it has none; new: always open one",
    )
    p_shortcut_set.add_argument("--cell", default=None, metavar="COLUMN,ROW", help="The grid cell; the next free one by default")
    _add_target_arguments(p_shortcut_set)
    p_shortcut_set.set_defaults(func=_cmd_shortcut_set)
    p_shortcut_move = shortcut_subparsers.add_parser("move", help="Move a shortcut to another cell")
    p_shortcut_move.add_argument("app", help="The registered app")
    p_shortcut_move.add_argument("launch", help="The launch path the shortcut runs")
    p_shortcut_move.add_argument("--cell", required=True, metavar="COLUMN,ROW", help="The grid cell to move to")
    _add_target_arguments(p_shortcut_move)
    p_shortcut_move.set_defaults(func=_cmd_shortcut_move)
    p_shortcut_remove = shortcut_subparsers.add_parser("remove", help="Take a shortcut off the desktop")
    p_shortcut_remove.add_argument("app", help="The registered app")
    p_shortcut_remove.add_argument("launch", help="The launch path the shortcut runs")
    _add_target_arguments(p_shortcut_remove)
    p_shortcut_remove.set_defaults(func=_cmd_shortcut_remove)

    p_wallpaper = subparsers.add_parser("wallpaper", help="Set or clear a desktop's wallpaper")
    p_wallpaper.add_argument("kind", help="'bundled' or 'file', or 'none' to clear it")
    p_wallpaper.add_argument("name", nargs="?", default=None, help="The image's file name without its extension")
    _add_target_arguments(p_wallpaper)
    p_wallpaper.set_defaults(func=_cmd_wallpaper)

    for verb in _RETIRED_VERBS:
        p_retired = subparsers.add_parser(verb)
        p_retired.add_argument("rest", nargs=argparse.REMAINDER)
        p_retired.set_defaults(func=_cmd_retired, verb=verb)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
