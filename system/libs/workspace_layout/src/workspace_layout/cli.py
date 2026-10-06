import argparse
import json
import sys
import tomllib
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final
from typing import NoReturn
from typing import TypeVar

import click
from app_manifest.manifest import ShortcutMode
from app_manifest.manifest import describe_validation_error
from app_manifest.primitives import MAX_APP_NAME_LENGTH
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from app_manifest.primitives import describe_app_name_problem
from app_manifest.registry import registry_path
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field
from pydantic import ValidationError
from tenacity import Retrying
from tenacity import retry_if_result
from tenacity import stop_after_delay
from tenacity import wait_fixed

from workspace_layout.answers import ContextAnswer
from workspace_layout.answers import DesktopOpAnswer
from workspace_layout.answers import InventoryDocument
from workspace_layout.answers import OpenAnswer
from workspace_layout.answers import ShowAnswer
from workspace_layout.answers import TransientOpAnswer
from workspace_layout.client import ShellLayoutClient
from workspace_layout.client import requester_from_environment
from workspace_layout.errors import ShellAnswerMalformedError
from workspace_layout.errors import ShellOpError
from workspace_layout.errors import ShellRefusedOpError
from workspace_layout.errors import ShellUnreachableError
from workspace_layout.ops import PLACEABLE_STATES
from workspace_layout.ops import POPPED_OUT_REFUSAL_STATUS
from workspace_layout.ops import ContextBody
from workspace_layout.ops import LoadArgs
from workspace_layout.ops import LoadBody
from workspace_layout.ops import NavigateArgs
from workspace_layout.ops import NavigateBody
from workspace_layout.ops import OpBody
from workspace_layout.ops import OpenArgs
from workspace_layout.ops import OpenBody
from workspace_layout.ops import OpRequester
from workspace_layout.ops import PlaceArgs
from workspace_layout.ops import PlaceBody
from workspace_layout.ops import RefreshAppArgs
from workspace_layout.ops import RefreshAppBody
from workspace_layout.ops import RefreshWindowArgs
from workspace_layout.ops import RefreshWindowBody
from workspace_layout.ops import ShortcutArgs
from workspace_layout.ops import ShortcutMoveArgs
from workspace_layout.ops import ShortcutMoveBody
from workspace_layout.ops import ShortcutRemoveBody
from workspace_layout.ops import ShortcutsArgs
from workspace_layout.ops import ShortcutsBody
from workspace_layout.ops import ShortcutSetArgs
from workspace_layout.ops import ShortcutSetBody
from workspace_layout.ops import ShowArgs
from workspace_layout.ops import ShowBody
from workspace_layout.ops import WallpaperArgs
from workspace_layout.ops import WallpaperBody
from workspace_layout.ops import WindowArgs
from workspace_layout.ops import WindowOp
from workspace_layout.ops import WindowOpBody
from workspace_layout.ops import parse_window_reference
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import IfPresent
from workspace_layout.primitives import LayoutOp
from workspace_layout.primitives import SpecialWindow
from workspace_layout.primitives import WallpaperKind
from workspace_layout.primitives import WallpaperName
from workspace_layout.primitives import WindowId
from workspace_layout.primitives import WindowPage
from workspace_layout.primitives import WindowPath
from workspace_layout.primitives import WindowState
from workspace_layout.records import Wallpaper
from workspace_layout.records import WindowView
from workspace_layout.records import parse_cell
from workspace_layout.records import parse_frame
from workspace_layout.shell_url import shell_base_url

_CLI_DESCRIPTION: Final[str] = """\
Agent-facing helper for the workspace desktop: read what is open, open and arrange windows, edit a desktop.

Subcommands:
    context                             Show each browser client: its active desktop, connection state, recent messages.
    desktops                            List every desktop with its windows and shortcuts, and every client.
    list                                List every app (launch paths, running or not) with its windows, plus the desktops.
    load <desktop>                      Switch the target client onto a desktop.
    open <app|url> [--path P | --launch ID --param k=v ...] [--if-present focus|new] [--minimized]
                                        Open a window of an app (a bare https:// URL opens a new browser on that page).
    open ... --beside [window]          ... and lay it beside that window (your own chat by default): that one
                                        snapped to the left half, the opened one to the right half and on top.
    show <app> --path P [--showing P ...] [--repoint PAGE ...]
                                        Put a page of an app on screen: raise a window already showing it, else point
                                        an on-screen window at one of the --repoint pages, else the app's pinned
                                        window, else open a new one.
    focus <window>                      Restore and raise a window.
    minimize <window>                   Put a window out of sight (its frame is kept).
    restore <window>                    Bring a minimized or maximized window back to its frame.
    maximize <window>                   Fill the backdrop with a window.
    place <window> --state S | --frame x,y,w,h
                                        Snap a window to a half (snapped-left, snapped-right) or maximize it
                                        (maximized), or set its frame.
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
``terminal`` at ``/?session=<name>``, ``files`` at ``/home/user/workspace/data/notes/``). Windows
and desktops are shared by everyone; where each window sits on a screen (its frame, whether it
is minimized or maximized) is one client's own *placement*. Every browser *client* has one
active desktop.

A window is named by its id (``win-<hex>``, from ``desktops`` or the ``open`` that made it), by
``self`` (the caller's own chat window), by ``pinned`` (the caller's app's pinned window), or
by an app name (that app's most recently focused window on the target client's active desktop).

A window the user *popped out* into its own Imbue Studio window (``desktops`` lists each
client's under ``popped_out``) is their arrangement: ``minimize``, ``restore``, ``maximize``,
and ``place`` refuse it (exit 4, changing nothing) unless ``--force`` is passed, which brings it
back onto the desktop and closes its own window. ``focus``, and an ``open`` that finds it,
raise its own window instead; ``open --beside`` a popped-out window opens unpaired. Every
mutating verb takes ``--force``; it is ignored where nothing can be refused.

Every op targets exactly one client: ``--client <id>`` (from ``context``), else the client
that most recently messaged you while it is connected, else the one connected client (when
a client messaged you, only if it is the same user's), else that messaging client though it
is not connected; with several clients and no way to tell, the op is refused and lists them.
The shell edits that client's placements itself, so an op lands whether or not a browser is
connected, and a connected window shows it within a redraw. An op edits the client's active
desktop; ``--desktop <name>`` edits that desktop and switches the client to it.

``open`` opens a window at ``--path``, or at one of the app's *launch paths* (``--launch <id>``
with ``--param name=value`` for its parameters; with neither, the app's default launch path).
A GET launch path is the page itself with the params as its query; a POST launch path is posted
the params by the shell and answers the page to open (the terminal's and the browser's ``new``,
the chat's ``new``, ``send``, and ``draft``), so the window opens at the page the app answered.
A window of the app already at that path is focused rather than duplicated unless
``--if-present new`` is passed. The window's id (the new one's, or the focused one's) is
printed to stdout. To open a folder in the file viewer, ``open files --path
/home/user/workspace/data/notes/`` (the viewer serves the filesystem root, so the path is
absolute); the ``path`` launch parameter (``open files --param path=/home/user/workspace/data/notes/``)
lands in the same folder but as a window at ``/home/user/workspace/?path=...``, and a window is
focused only when its path matches exactly, so use one form per folder.

``show`` chooses the window itself and never points a window at another page of the app unless
that page is named with ``--repoint``: ``--showing`` names the app's other paths that count as
already showing the page. The window's id is printed to stdout, and how it was shown (raised,
navigated, pinned, opened) to stderr.

Every op POSTs one body ``{op, args, requester}`` to a loopback-only endpoint on the shell:
``requester`` is the caller's own chat, ``{"app": "chat", "marker": $MINDS_CHAT_ID}`` (the chat
app sets ``MINDS_CHAT_ID`` on every agent it creates; ``MNGR_AGENT_ID`` stands in for an agent
that is its own chat), which is what ``self`` means and how the shell attributes the op to a
client (the one that last messaged that chat). ``desktops`` and ``list`` read ``GET
/api/inventory`` instead.

The read commands print JSON, indented, in the shell's own order (windows in opening
order). Descriptions of what an op did go to stderr; stdout carries only the window id of an
``open`` or a ``show``, the JSON of the read commands, and the desktop's shortcuts as they stand
after a ``shortcut`` write.

Retired verbs (``split``, ``move``, ``rename``, ``delete``, ``stop``, ``start``,
``replace-url``, ``inspect``, ``where``, ``views``) and the old ``app:``, ``chat:``,
``chat-terminal:``, ``terminal:``, ``service:``, ``url:``, and ``subagent:`` spellings are
refused with the verb or form to use instead.
"""

# How the hints name this command: agents run it from the repo root through the root venv.
_COMMAND: Final[str] = "uv run workspace-layout"

# A bare URL opens a new browser on that page: the browser app's ``new`` launch path with the
# URL as its ``url`` parameter (system/apps/browser/app.toml).
_BROWSER_APP_NAME: Final[str] = "browser"
_BROWSER_NEW_LAUNCH: Final[str] = "new"
_BROWSER_URL_PARAM: Final[str] = "url"
_EXTERNAL_URL_PREFIXES: Final[tuple[str, ...]] = ("https://", "http://")

# The retired spellings, refused by name with the form to use instead, so an agent
# working from an old note is told what to type rather than waiting on a registration that
# never comes.
_RETIRED_PREFIXES: Final[tuple[str, ...]] = (
    "app:",
    "chat-terminal:",
    "chat:",
    "terminal:",
    "service:",
    "url:",
    "subagent:",
)
# The retired verbs, each with its replacement.
RETIRED_VERBS: Final[Mapping[str, str]] = {
    "split": "'place' sets where a window sits (--state snapped-left|snapped-right|maximized, or --frame x,y,w,h); 'open' puts a new window on the desktop",
    "move": "'place' sets where a window sits (--state snapped-left|snapped-right|maximized, or --frame x,y,w,h)",
    "rename": "a title belongs to the app that owns the page: the chat's POST /api/chats/<id>/rename (the terminal offers no rename route yet)",
    "delete": "close the window with 'close'; a terminal or the browser is ended by its app once no window shows it, and a chat only by the chat's own destroy route",
    "stop": "the app's own route stops what backs a page (the chat's stop route, the browser's POST /browsers/<name>/stop)",
    "start": "the app's own route starts what backs a page (the browser's POST /browsers/<name>/start)",
    "replace-url": "'navigate <window> <path>' points a window at another path under its app",
    "inspect": "'desktops' lists every desktop with its windows; 'list' adds every app",
    "where": "'desktops' lists every desktop with its windows and their ids",
    "views": "'desktops' lists every desktop and which clients are on each",
}

# How long ``open`` waits for a freshly-registered app to appear before giving up. The
# supervisord-managed forward_port.py call races with the agent invoking this command right
# after build-app, so a brief window where the row is not yet visible is fine.
REGISTRATION_TIMEOUT_SECONDS: Final[float] = 5.0
_REGISTRATION_POLL_INTERVAL_SECONDS: Final[float] = 0.25

_NO_WALLPAPER: Final[str] = "none"

# What ``desktops`` and ``list`` print of each window, client, and app.
_LISTED_WINDOW_FIELDS: Final[tuple[str, ...]] = ("id", "app", "path", "title", "is_pinned", "scope", "client_paths")
_LISTED_CLIENT_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "active_desktop",
    "is_connected",
    "shown",
    "popped_out",
    "shown_history",
    "last_seen",
)
_LISTED_APP_FIELDS: Final[tuple[str, ...]] = ("name", "display_name", "is_running", "launch_paths", "default_shortcut")

_Answer = TypeVar("_Answer", bound=FrozenModel)
_DesktopAnswer = TypeVar("_DesktopAnswer", bound=DesktopOpAnswer)
_Args = TypeVar("_Args", bound=FrozenModel)
_Parsed = TypeVar("_Parsed")

# A read answers from memory and the state files; an ``open`` waits on nothing slower than a
# file write either, but keeps a wider bound for a shell busy with a client's save.
READ_TIMEOUT_SECONDS: Final[float] = 10.0
OP_TIMEOUT_SECONDS: Final[float] = 30.0

# Agents branch on "did it work"; the distinct exit codes worth their own slot are a shell or app
# that cannot act right now (a 409 or a 503), where retry-with-backoff is the right response, and a
# window the user popped out into its own window, where the answer is --force or telling the user,
# never a retry. Slot 2 is left to argparse's usage exit.
EXIT_OK: Final[int] = 0
EXIT_ERROR: Final[int] = 1
EXIT_CONFLICT: Final[int] = 3
EXIT_POPPED_OUT: Final[int] = 4


class LayoutCliContext(FrozenModel):
    """Where the command reaches the shell and the registry, who it says is asking, and how long it waits."""

    shell_url: str = Field(description="The shell's base URL, without a trailing slash")
    apps_file: Path = Field(description="The app registry (data/.state/apps.toml)")
    requester: OpRequester | None = Field(description="The caller's own chat, or None outside an agent")
    registration_timeout_seconds: float = Field(description="How long an open waits for its app's registry row")
    read_timeout_seconds: float = Field(description="How long a read or a transient op may take")
    op_timeout_seconds: float = Field(description="How long an op the shell writes files for may take")


def context_from_environment() -> LayoutCliContext:
    return LayoutCliContext(
        shell_url=shell_base_url(),
        apps_file=registry_path(),
        requester=requester_from_environment(),
        registration_timeout_seconds=REGISTRATION_TIMEOUT_SECONDS,
        read_timeout_seconds=READ_TIMEOUT_SECONDS,
        op_timeout_seconds=OP_TIMEOUT_SECONDS,
    )


def _write_stdout(text: str) -> None:
    click.echo(text, nl=False)


def _write_stderr(text: str) -> None:
    click.echo(text, nl=False, err=True)


# Names


def _fail(message: str) -> NoReturn:
    _write_stderr(f"error: {message}\n")
    raise SystemExit(EXIT_ERROR)


def _retired_spelling_message(value: str) -> str:
    prefix = next(candidate for candidate in _RETIRED_PREFIXES if value.startswith(candidate))
    remainder = value[len(prefix) :]
    if prefix == "app:":
        name, _, query = remainder.partition("?")
        if query.startswith("instance="):
            hint = (
                f"a window is named by its id (see 'desktops'); to open one, give the app and the path of its page: "
                f"'{_COMMAND} open {name or '<app>'} --path <path>'"
            )
        else:
            hint = f"give the app name on its own: '{_COMMAND} open {name or '<app>'}'"
    elif prefix == "chat:":
        hint = f"a chat's window is the chat app at its page: '{_COMMAND} open chat --path \"/?chat=<chat-id>\"'"
    elif prefix == "chat-terminal:":
        hint = (
            f"an agent's terminal is the back face of its chat: open the chat's page, "
            f"'{_COMMAND} open chat --path \"/?chat=<chat-id>\"'"
        )
    elif prefix == "terminal:":
        hint = (
            f"a terminal's window is the terminal app at its page: "
            f"'{_COMMAND} open terminal --path \"/?session={remainder or '<session>'}\"'"
        )
    elif prefix == "service:":
        name, _, query = remainder.partition("?")
        hint = f"give the app name on its own: '{_COMMAND} open {name or '<app>'}'" + (
            f' (with --path "/?{query}" for one page of it)' if query else ""
        )
    elif prefix == "url:":
        hint = f"pass the URL itself: '{_COMMAND} open https://...' opens it in a new browser"
    else:
        hint = f"a subagent's view is a page of the chat app: '{_COMMAND} open chat --path <the subagent view's path>'"
    return (
        f"{value!r} is not how the desktop names things: give an app name and a path. {hint}; "
        f"'{_COMMAND} desktops' lists every window with its id"
    )


def _is_external_url(value: str) -> bool:
    return any(value.startswith(prefix) for prefix in _EXTERNAL_URL_PREFIXES)


def _refuse_retired_spelling(value: str) -> None:
    if any(value.startswith(prefix) for prefix in _RETIRED_PREFIXES):
        _fail(_retired_spelling_message(value))


def app_name_argument(value: str) -> AppName:
    """An argument that must name an app; the retired spellings and a URL are refused by name."""
    _refuse_retired_spelling(value)
    if _is_external_url(value):
        _fail(f"{value!r} is a URL: only 'open' takes one (it opens the page in a new browser)")
    if describe_app_name_problem(value) is not None:
        _fail(
            f"{value!r} is not an app name (lowercase words joined by single dashes, at most {MAX_APP_NAME_LENGTH} characters)"
        )
    return AppName(value)


def window_argument(value: str) -> str:
    """An argument that names a window: a window id, ``self``, ``pinned`` (your app's pinned window), or an app name."""
    _refuse_retired_spelling(value)
    return str(_flag_value("the window", parse_window_reference, value))


def _flag_value(flag: str, parse: Callable[[str], _Parsed], value: str) -> _Parsed:
    """``value`` read as ``flag`` takes it; a value off its rule fails the command with the rule it broke."""
    try:
        return parse(value)
    except ValueError as e:
        _fail(f"{flag}: {e}")


# The registry


def _read_registry_names(path: Path) -> list[str]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        document = tomllib.load(f)
    return [
        app["name"]
        for app in document.get("apps", [])
        if isinstance(app, dict) and isinstance(app.get("name"), str) and app.get("name")
    ]


def _is_app_registered(name: str, apps_file: Path) -> bool:
    return name in _read_registry_names(apps_file)


def _wait_for_registration(name: str, apps_file: Path, timeout_seconds: float) -> bool:
    retrying = Retrying(
        stop=stop_after_delay(timeout_seconds),
        wait=wait_fixed(_REGISTRATION_POLL_INTERVAL_SECONDS),
        retry=retry_if_result(lambda is_registered: not is_registered),
        retry_error_callback=lambda retry_state: False,
    )
    return retrying(_is_app_registered, name, apps_file)


def _require_registered(context: LayoutCliContext, app: str) -> int | None:
    if _wait_for_registration(app, context.apps_file, context.registration_timeout_seconds):
        return None
    _write_stderr(
        f"error: app {app!r} is not registered in {context.apps_file} after waiting "
        f"{context.registration_timeout_seconds:.0f}s. Did you forward_port.py / start the app?\n"
    )
    return EXIT_ERROR


# Transport


def _client(context: LayoutCliContext) -> ShellLayoutClient:
    return ShellLayoutClient(
        shell_url=context.shell_url, requester=context.requester, timeout_seconds=context.op_timeout_seconds
    )


def _report_refusal(op: LayoutOp, status: int, detail: str) -> int:
    """A refused op as a stderr message and an exit code; a 409 or a 503 (the shell or an app cannot do it right now)
    and a refusal to move a popped-out window each have their own code."""
    if status == POPPED_OUT_REFUSAL_STATUS:
        _write_stderr(
            f"error: '{op}' refused (HTTP {status}): {detail}. The user popped the window out into its own window; "
            "--force overrides it\n"
        )
        return EXIT_POPPED_OUT
    if status == 412:
        _write_stderr(f"error: '{op}' has no client to apply it to (HTTP 412): {detail}\n")
        return EXIT_ERROR
    if status in (409, 503):
        _write_stderr(f"error: '{op}' rejected (HTTP {status}): {detail}\n")
        return EXIT_CONFLICT
    if status == 404:
        _write_stderr(f"error: '{op}' target not found (HTTP 404): {detail}\n")
        return EXIT_ERROR
    if status == 400:
        _write_stderr(f"error: '{op}' rejected (HTTP 400): {detail}\n")
        return EXIT_ERROR
    _write_stderr(f"error: '{op}' failed (HTTP {status}): {detail}\n")
    return EXIT_ERROR


def _post(context: LayoutCliContext, body: OpBody, model: type[_Answer], timeout_seconds: float) -> _Answer | int:
    """The shell's answer to one op, or the exit code its failure is reported with."""
    try:
        return _client(context).post_op(body, model, timeout_seconds)
    except ShellUnreachableError as e:
        _write_stderr(f"error: could not reach the workspace shell: {e}\n")
        return EXIT_ERROR
    except ShellRefusedOpError as e:
        return _report_refusal(body.op, e.status_code, e.detail)
    except ShellAnswerMalformedError as e:
        _write_stderr(f"error: {e}\n")
        return EXIT_ERROR


def _emit_structured(data: Any) -> None:
    _write_stdout(json.dumps(data, indent=2))
    _write_stdout("\n")


# Targeting and answers


def _target_fields(args: argparse.Namespace) -> dict[str, Any]:
    """The ``--client`` and ``--desktop`` an op names, as its arguments' target keys."""
    fields: dict[str, Any] = {}
    if args.client:
        fields["client"] = _flag_value("--client", ClientId, args.client)
    if args.desktop:
        fields["desktop"] = args.desktop
    return fields


def _arguments(model: type[_Args], fields: Mapping[str, Any]) -> _Args:
    """An op's arguments; a combination the op does not take fails the command with the rule it broke."""
    try:
        return model.model_validate(dict(fields))
    except ValidationError as e:
        _fail(describe_validation_error(e))


def _window_in(answer: DesktopOpAnswer, window_id: WindowId | None) -> WindowView | None:
    return next((window for window in answer.desktop.windows if window.id == window_id), None)


def _describe_window(answer: DesktopOpAnswer, window_id: WindowId | None) -> str:
    """``<id> (<app> at <path>)`` for the window an answer names, or just the id when it is gone (a close)."""
    window = _window_in(answer, window_id)
    if window is None:
        return str(window_id)
    return f"{window_id} ({window.app} at {window.path})"


def _describe_target(answer: DesktopOpAnswer) -> str:
    if answer.client_id is None:
        return f"desktop {answer.desktop_id} for no client (minimized everywhere)"
    return f"desktop {answer.desktop_id} for client {answer.client_id}"


def _describe_pop_out_notes(answer: DesktopOpAnswer) -> str:
    """What an answer says about the client's popped-out windows, as notes after the summary line ("" for none)."""
    notes: list[str] = []
    if answer.is_raised_in_own_window:
        notes.append("raised in its own window: the user popped it out, so it stays there")
    if answer.is_brought_back:
        notes.append("brought back from its own window")
    if answer.unpaired_beside is not None:
        notes.append(
            f"not paired beside {answer.unpaired_beside}: the user popped it out into its own window, so the window "
            "opened where a plain open puts it; --force brings it back and pairs the two"
        )
    if answer.has_no_desktop_window:
        notes.append(
            f"client {answer.client_id} has no desktop window open, only popped-out ones; the window is there when "
            "one opens"
        )
    return "".join(f" ({note})" for note in notes)


def _force_fields(args: argparse.Namespace) -> dict[str, Any]:
    """``force`` when the verb was given ``--force``, else nothing, so the body carries it only when asked."""
    return {"force": True} if args.force else {}


def _run_desktop_op(
    context: LayoutCliContext,
    body: OpBody,
    model: type[_DesktopAnswer],
    describe: Callable[[_DesktopAnswer], str],
    emit: Callable[[_DesktopAnswer], None] | None,
) -> int:
    """Post one op the shell applies to the files; it answers with the desktop and the client's placements.

    ``describe`` renders the one-line stderr summary from the answer; ``emit``, when given, writes what the
    verb prints on stdout from it.
    """
    answer = _post(context, body, model, context.op_timeout_seconds)
    if isinstance(answer, int):
        return answer
    _write_stderr(describe(answer) + _describe_pop_out_notes(answer) + "\n")
    if emit is not None:
        emit(answer)
    return EXIT_OK


def _run_transient_op(context: LayoutCliContext, body: RefreshWindowBody | RefreshAppBody) -> int:
    """Post one of the verbs with nothing to store (refresh); the target client's windows apply it."""
    answer = _post(context, body, TransientOpAnswer, context.read_timeout_seconds)
    if isinstance(answer, int):
        return answer
    where = f"client {answer.target_client_id}" if answer.target_client_id is not None else "every client"
    _write_stderr(f"(sent {body.op} to {where})\n")
    return EXIT_OK


# The read commands


def _fetch_inventory(context: LayoutCliContext) -> InventoryDocument | None:
    try:
        return _client(context).inventory(context.read_timeout_seconds)
    except ShellOpError as e:
        _write_stderr(f"error: could not read the inventory: {e}\n")
        return None


@pure
def _listed(model: FrozenModel, fields: Sequence[str]) -> dict[str, Any]:
    """The named fields of ``model`` as the read commands print them, in that order."""
    dumped = model.model_dump(mode="json")
    return {field: dumped[field] for field in fields}


def _listed_window(window: WindowView) -> dict[str, Any]:
    return _listed(window, _LISTED_WINDOW_FIELDS)


def _listed_desktops(inventory: InventoryDocument) -> list[dict[str, Any]]:
    return [
        {
            "id": str(desktop.id),
            "name": desktop.name,
            "wallpaper": desktop.wallpaper.model_dump(mode="json") if desktop.wallpaper is not None else None,
            "shortcuts": [shortcut.model_dump(mode="json") for shortcut in desktop.shortcuts],
            "windows": [_listed_window(window) for window in desktop.windows],
        }
        for desktop in inventory.desktops
    ]


def _listed_clients(inventory: InventoryDocument) -> list[dict[str, Any]]:
    return [_listed(client, _LISTED_CLIENT_FIELDS) for client in inventory.clients]


def _listed_apps(inventory: InventoryDocument) -> list[dict[str, Any]]:
    """Every app a user can open, with where its windows are."""
    windows_by_app: dict[str, list[dict[str, Any]]] = {}
    for desktop in inventory.desktops:
        for window in desktop.windows:
            windows_by_app.setdefault(str(window.app), []).append(
                {**_listed_window(window), "desktop": str(desktop.id)}
            )
    return [
        {
            **_listed(app, _LISTED_APP_FIELDS),
            "windows": windows_by_app.get(str(app.name), []),
        }
        for app in inventory.apps
        if not app.internal
    ]


def _cmd_context(args: argparse.Namespace, context: LayoutCliContext) -> int:
    answer = _post(context, ContextBody(), ContextAnswer, context.read_timeout_seconds)
    if isinstance(answer, int):
        return answer
    _emit_structured([client.model_dump(mode="json") for client in answer.clients])
    return EXIT_OK


def _cmd_desktops(args: argparse.Namespace, context: LayoutCliContext) -> int:
    inventory = _fetch_inventory(context)
    if inventory is None:
        return EXIT_ERROR
    _emit_structured({"desktops": _listed_desktops(inventory), "clients": _listed_clients(inventory)})
    return EXIT_OK


def _cmd_list(args: argparse.Namespace, context: LayoutCliContext) -> int:
    inventory = _fetch_inventory(context)
    if inventory is None:
        return EXIT_ERROR
    _emit_structured(
        {
            "apps": _listed_apps(inventory),
            "desktops": _listed_desktops(inventory),
            "clients": _listed_clients(inventory),
        }
    )
    return EXIT_OK


def _cmd_load(args: argparse.Namespace, context: LayoutCliContext) -> int:
    return _run_desktop_op(
        context,
        LoadBody(args=_arguments(LoadArgs, _target_fields(args))),
        DesktopOpAnswer,
        lambda answer: f"switched client {answer.client_id} onto desktop {answer.desktop_id}",
        None,
    )


# open and show


def _parse_params(raw_params: Sequence[str] | None) -> dict[str, str]:
    """``--param name=value`` pairs as a launch path's query parameters."""
    params: dict[str, str] = {}
    for raw in raw_params or []:
        name, separator, value = raw.partition("=")
        if not separator or not name:
            _fail(f"--param takes name=value, not {raw!r}")
        params[name] = value
    return params


def _open_arguments(args: argparse.Namespace) -> OpenArgs:
    """The ``open`` op's arguments: the app, its path or launch path with parameters, and how the window lands.

    A bare URL is the browser app's ``new`` launch path with the URL as its ``url`` parameter.
    """
    params = _parse_params(args.param)
    target: str = args.target
    fields: dict[str, Any] = {}
    if _is_external_url(target):
        if args.path or args.launch or params:
            _fail("a URL is opened in a new browser; --path, --launch, and --param do not apply to it")
        fields = {
            "app": AppName(_BROWSER_APP_NAME),
            "launch": LaunchPathId(_BROWSER_NEW_LAUNCH),
            "params": {_BROWSER_URL_PARAM: target},
        }
    else:
        fields = {"app": app_name_argument(target)}
        if args.path:
            if args.launch or params:
                _fail(
                    "--path names the page to open; --launch and --param choose a launch path instead. "
                    "Pass one or the other"
                )
            fields["path"] = _flag_value("--path", WindowPath, args.path)
        if args.launch:
            fields["launch"] = _flag_value("--launch", LaunchPathId, args.launch)
        if params:
            fields["params"] = params
    if args.if_present:
        fields["if_present"] = IfPresent(args.if_present)
    if args.minimized and args.beside is not None:
        _fail(
            "--minimized puts the window out of sight and --beside puts it on half the screen; pass one or the other"
        )
    if args.minimized:
        fields["minimized"] = True
    if args.beside is not None:
        fields["beside"] = window_argument(args.beside)
    return _arguments(OpenArgs, {**fields, **_target_fields(args), **_force_fields(args)})


def _cmd_open(args: argparse.Namespace, context: LayoutCliContext) -> int:
    arguments = _open_arguments(args)
    if (err := _require_registered(context, str(arguments.app))) is not None:
        return err
    return _run_desktop_op(
        context,
        OpenBody(args=arguments),
        OpenAnswer,
        lambda answer: (
            f"opened window {_describe_window(answer, answer.window_id)}"
            f"{f' beside {arguments.beside}' if arguments.beside and answer.unpaired_beside is None else ''} "
            f"on {_describe_target(answer)}"
        ),
        _print_window_id,
    )


def _print_window_id(answer: OpenAnswer) -> None:
    _write_stdout(f"{answer.window_id}\n")


def _show_arguments(args: argparse.Namespace) -> ShowArgs:
    """The ``show`` op's arguments: the app, its page, the other paths that count as showing it, and the pages whose
    windows the shell may point at it."""
    fields: dict[str, Any] = {
        "app": app_name_argument(args.app),
        "path": _flag_value("--path", WindowPath, args.path),
    }
    if args.showing:
        fields["showing"] = tuple(_flag_value("--showing", WindowPath, path) for path in args.showing)
    if args.repoint:
        fields["repoint"] = tuple(_flag_value("--repoint", WindowPage, page) for page in args.repoint)
    return _arguments(ShowArgs, {**fields, **_target_fields(args)})


def _cmd_show(args: argparse.Namespace, context: LayoutCliContext) -> int:
    arguments = _show_arguments(args)
    if (err := _require_registered(context, str(arguments.app))) is not None:
        return err
    return _run_desktop_op(
        context,
        ShowBody(args=arguments),
        ShowAnswer,
        lambda answer: f"{answer.shown} window {_describe_window(answer, answer.window_id)} on {_describe_target(answer)}",
        _print_window_id,
    )


# The window verbs


def _window_op(op: WindowOp, past_tense: str) -> Callable[[argparse.Namespace, LayoutCliContext], int]:
    def run(args: argparse.Namespace, context: LayoutCliContext) -> int:
        arguments = _arguments(
            WindowArgs, {"window": window_argument(args.window), **_target_fields(args), **_force_fields(args)}
        )
        return _run_desktop_op(
            context,
            WindowOpBody(op=op, args=arguments),
            DesktopOpAnswer,
            lambda answer: (
                f"{past_tense} window {_describe_window(answer, answer.window_id)} on {_describe_target(answer)}"
            ),
            None,
        )

    return run


@pure
def _state_flag(state: WindowState) -> str:
    """How ``--state`` spells a state: its name in lowercase, words joined by dashes."""
    return state.value.lower().replace("_", "-")


def _parse_state_flag(value: str) -> WindowState:
    return WindowState(value.upper().replace("-", "_"))


_STATE_FLAGS: Final[tuple[str, ...]] = tuple(_state_flag(state) for state in PLACEABLE_STATES)


def _cmd_place(args: argparse.Namespace, context: LayoutCliContext) -> int:
    if args.zone is not None:
        _fail(f"--zone is retired: 'place' takes --state ({', '.join(_STATE_FLAGS)}) or --frame x,y,width,height")
    window = window_argument(args.window)
    if bool(args.state) == bool(args.frame):
        _fail(f"place takes exactly one of --state ({', '.join(_STATE_FLAGS)}) or --frame x,y,width,height")
    if args.state:
        fields: dict[str, Any] = {"window": window, "state": _parse_state_flag(args.state)}
        what = f"as {args.state}"
    else:
        fields = {"window": window, "frame": _flag_value("--frame", parse_frame, args.frame)}
        what = f"at frame {args.frame}"
    return _run_desktop_op(
        context,
        PlaceBody(args=_arguments(PlaceArgs, {**fields, **_target_fields(args), **_force_fields(args)})),
        DesktopOpAnswer,
        lambda answer: f"placed window {_describe_window(answer, answer.window_id)} {what} on {_describe_target(answer)}",
        None,
    )


def _cmd_navigate(args: argparse.Namespace, context: LayoutCliContext) -> int:
    window = window_argument(args.window)
    path = _flag_value("navigate's path", WindowPath, args.path)
    return _run_desktop_op(
        context,
        NavigateBody(args=_arguments(NavigateArgs, {"window": window, "path": path, **_target_fields(args)})),
        DesktopOpAnswer,
        lambda answer: (
            f"pointed window {_describe_window(answer, answer.window_id)} at {path} on {_describe_target(answer)}"
        ),
        None,
    )


def _cmd_refresh(args: argparse.Namespace, context: LayoutCliContext) -> int:
    if bool(args.window) == bool(args.app):
        _fail(
            "refresh takes a window (a window id, 'self', 'pinned', or an app name) or --app <name> for every page of an app"
        )
    if args.app:
        if args.client or args.desktop:
            _fail(
                "refresh --app reloads every page of the app on every client; --client and --desktop do not apply to it"
            )
        return _run_transient_op(
            context, RefreshAppBody(args=_arguments(RefreshAppArgs, {"app": app_name_argument(args.app)}))
        )
    arguments = _arguments(RefreshWindowArgs, {"window": window_argument(args.window), **_target_fields(args)})
    return _run_transient_op(context, RefreshWindowBody(args=arguments))


# Shortcuts and the wallpaper


def _shortcuts_document(answer: DesktopOpAnswer) -> dict[str, Any]:
    """The desktop's shortcuts as the read and write verbs print them: ``{desktop, shortcuts}``."""
    return {
        "desktop": str(answer.desktop_id),
        "shortcuts": [shortcut.model_dump(mode="json") for shortcut in answer.desktop.shortcuts],
    }


def _run_shortcut_write(
    context: LayoutCliContext, body: ShortcutSetBody | ShortcutMoveBody | ShortcutRemoveBody, done: str
) -> int:
    """Post one shortcut write and print the desktop's shortcuts as they stand after it."""
    return _run_desktop_op(
        context,
        body,
        DesktopOpAnswer,
        lambda answer: f"{done} on {_describe_target(answer)}",
        lambda answer: _emit_structured(_shortcuts_document(answer)),
    )


def _cmd_shortcuts(args: argparse.Namespace, context: LayoutCliContext) -> int:
    body = ShortcutsBody(args=_arguments(ShortcutsArgs, _target_fields(args)))
    answer = _post(context, body, DesktopOpAnswer, context.read_timeout_seconds)
    if isinstance(answer, int):
        return answer
    _emit_structured(_shortcuts_document(answer))
    return EXIT_OK


def _cmd_shortcut_set(args: argparse.Namespace, context: LayoutCliContext) -> int:
    app = app_name_argument(args.app)
    launch = _flag_value("launch", LaunchPathId, args.launch)
    mode = ShortcutMode(args.mode)
    fields: dict[str, Any] = {"app": app, "launch": launch, "mode": mode}
    if args.cell:
        fields["cell"] = _flag_value("--cell", parse_cell, args.cell)
    return _run_shortcut_write(
        context,
        ShortcutSetBody(args=_arguments(ShortcutSetArgs, {**fields, **_target_fields(args)})),
        f"set shortcut {app} {launch} ({mode})",
    )


def _cmd_shortcut_move(args: argparse.Namespace, context: LayoutCliContext) -> int:
    app = app_name_argument(args.app)
    launch = _flag_value("launch", LaunchPathId, args.launch)
    cell = _flag_value("--cell", parse_cell, args.cell)
    arguments = _arguments(ShortcutMoveArgs, {"app": app, "launch": launch, "cell": cell, **_target_fields(args)})
    return _run_shortcut_write(
        context, ShortcutMoveBody(args=arguments), f"moved shortcut {app} {launch} to cell {args.cell}"
    )


def _cmd_shortcut_remove(args: argparse.Namespace, context: LayoutCliContext) -> int:
    app = app_name_argument(args.app)
    launch = _flag_value("launch", LaunchPathId, args.launch)
    arguments = _arguments(ShortcutArgs, {"app": app, "launch": launch, **_target_fields(args)})
    return _run_shortcut_write(context, ShortcutRemoveBody(args=arguments), f"removed shortcut {app} {launch}")


def _cmd_wallpaper(args: argparse.Namespace, context: LayoutCliContext) -> int:
    wallpaper_kinds = [kind.value for kind in WallpaperKind]
    if args.kind == _NO_WALLPAPER:
        if args.name:
            _fail("'wallpaper none' takes no name")
        wallpaper = None
        done = "cleared the wallpaper"
    else:
        if args.kind not in wallpaper_kinds or not args.name:
            _fail(f"wallpaper takes '<kind> <name>' with kind one of {wallpaper_kinds}, or 'none'")
        wallpaper = Wallpaper(kind=WallpaperKind(args.kind), name=_flag_value("name", WallpaperName, args.name))
        done = f"set the wallpaper to {args.kind} {args.name}"
    return _run_desktop_op(
        context,
        WallpaperBody(args=_arguments(WallpaperArgs, {"wallpaper": wallpaper, **_target_fields(args)})),
        DesktopOpAnswer,
        lambda answer: f"{done} on {_describe_target(answer)}",
        None,
    )


# The retired verbs


def _cmd_retired(args: argparse.Namespace, context: LayoutCliContext) -> int:
    _fail(f"'{args.verb}' is not a desktop verb: {RETIRED_VERBS[args.verb]}")


# The parser


_CLIENT_HELP: Final[str] = (
    "The client whose desktop the op targets (an id from ``context``). Defaults to the client that "
    "most recently messaged you while it is connected, else the one connected client (when a client "
    "messaged you, only if it is the same user's), else that messaging client though it is not "
    "connected; refused when that settles nothing."
)
_DESKTOP_HELP: Final[str] = (
    "The desktop to edit, by name or id. Defaults to the target client's active desktop; naming "
    "another edits that one and switches the client to it."
)


def _add_target_arguments(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--client", default=None, help=_CLIENT_HELP)
    subparser.add_argument("--desktop", default=None, help=_DESKTOP_HELP)


_FORCE_HELP: Final[str] = (
    "Apply the op even to a window the user popped out into its own window, bringing it back onto the desktop "
    "(minimize, restore, maximize, place, open --beside); ignored where nothing is refused."
)


def _add_force_argument(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--force", action="store_true", help=_FORCE_HELP)


def _add_json_argument(subparser: argparse.ArgumentParser) -> None:
    # CLEANUP: drop --json once every workspace runs a release where JSON is the only output
    # (it became so in September 2026); it is accepted so instructions written for the YAML
    # default keep working.
    subparser.add_argument(
        "--json", action="store_true", help="Accepted for compatibility: the output is JSON either way"
    )


def _add_window_verb(subparsers: Any, verb: WindowOp, help_text: str, past_tense: str) -> None:
    subparser = subparsers.add_parser(verb, help=help_text)
    subparser.add_argument("window", help="A window id (win-<hex>), 'self', 'pinned', or an app name")
    _add_target_arguments(subparser)
    _add_force_argument(subparser)
    subparser.set_defaults(func=_window_op(verb, past_tense))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=_CLI_DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_context = subparsers.add_parser(
        "context", help="Show each client: its active desktop, connection state, and recent messages"
    )
    _add_json_argument(p_context)
    p_context.set_defaults(func=_cmd_context)

    p_desktops = subparsers.add_parser(
        "desktops", help="List every desktop with its windows and shortcuts, and every client"
    )
    _add_json_argument(p_desktops)
    p_desktops.set_defaults(func=_cmd_desktops)

    p_list = subparsers.add_parser("list", help="List every app with its launch paths and windows, plus the desktops")
    _add_json_argument(p_list)
    p_list.set_defaults(func=_cmd_list)

    p_load = subparsers.add_parser("load", help="Switch the target client onto a desktop")
    p_load.add_argument("desktop", help="The desktop's name or id")
    p_load.add_argument("--client", default=None, help=_CLIENT_HELP)
    _add_force_argument(p_load)
    p_load.set_defaults(func=_cmd_load)

    p_open = subparsers.add_parser("open", help="Open a window of an app (or a URL in a new browser)")
    p_open.add_argument(
        "target",
        help="An app name (open a window of it), or a bare https:// URL (open it in a new browser)",
    )
    p_open.add_argument(
        "--path",
        default=None,
        help="The page to open, a path under the app's origin: 'open files --path /home/user/workspace/data/notes/' "
        "opens a folder, "
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
        help="A launch path parameter (repeatable), e.g. --param workdir=/data, --param path=/home/user/workspace/data/notes/.",
    )
    p_open.add_argument(
        "--if-present",
        dest="if_present",
        choices=[choice.value for choice in IfPresent],
        default=None,
        help="What to do about a window of the app already at the path: focus it (the default) or open another.",
    )
    p_open.add_argument(
        "--minimized",
        action="store_true",
        help="Place a window this open creates minimized, so it does not land over what the user is doing; "
        "a window found already at the path is left as it is.",
    )
    p_open.add_argument(
        "--beside",
        nargs="?",
        const=SpecialWindow.SELF.value,
        default=None,
        metavar="WINDOW",
        help="Lay the opened window beside this one (bare, your own chat): that window snapped to the left half, "
        "the opened one to the right half and on top. Ignored when the named window is not on the desktop.",
    )
    _add_target_arguments(p_open)
    _add_force_argument(p_open)
    p_open.set_defaults(func=_cmd_open)

    p_show = subparsers.add_parser(
        "show", help="Put a page of an app on screen, raising a window already showing it rather than opening another"
    )
    p_show.add_argument("app", help="The app whose page to show")
    p_show.add_argument("--path", required=True, help="The page to show, a path under the app's origin")
    p_show.add_argument(
        "--showing",
        action="append",
        default=None,
        metavar="PATH",
        help="Another path of the app that counts as already showing the page (repeatable)",
    )
    p_show.add_argument(
        "--repoint",
        action="append",
        default=None,
        metavar="PAGE",
        help="A page of the app (a path with no query string) whose on-screen window may be pointed at the path "
        "(repeatable); without it no window is pointed elsewhere",
    )
    _add_target_arguments(p_show)
    _add_force_argument(p_show)
    p_show.set_defaults(func=_cmd_show)

    _add_window_verb(subparsers, LayoutOp.FOCUS, "Restore and raise a window", "focused")
    _add_window_verb(subparsers, LayoutOp.MINIMIZE, "Put a window out of sight", "minimized")
    _add_window_verb(subparsers, LayoutOp.RESTORE, "Bring a window back to its frame", "restored")
    _add_window_verb(subparsers, LayoutOp.MAXIMIZE, "Fill the backdrop with a window", "maximized")
    _add_window_verb(subparsers, LayoutOp.CLOSE, "Close a window for everyone", "closed")

    p_place = subparsers.add_parser("place", help="Snap a window to a half or maximize it, or set its frame")
    p_place.add_argument("window", help="A window id (win-<hex>), 'self', 'pinned', or an app name")
    p_place.add_argument(
        "--state", choices=_STATE_FLAGS, default=None, help="Snap to the left or right half, or maximize"
    )
    p_place.add_argument("--zone", default=None, help=argparse.SUPPRESS)
    p_place.add_argument(
        "--frame", default=None, metavar="X,Y,WIDTH,HEIGHT", help="The frame in fractions of the backdrop (0..1)"
    )
    _add_target_arguments(p_place)
    _add_force_argument(p_place)
    p_place.set_defaults(func=_cmd_place)

    p_navigate = subparsers.add_parser("navigate", help="Point a window at another path under its app")
    p_navigate.add_argument("window", help="A window id (win-<hex>), 'self', 'pinned', or an app name")
    p_navigate.add_argument("path", help="The path under the app's origin, starting with '/'")
    _add_target_arguments(p_navigate)
    _add_force_argument(p_navigate)
    p_navigate.set_defaults(func=_cmd_navigate)

    p_refresh = subparsers.add_parser("refresh", help="Reload one window's page, or every page of an app")
    p_refresh.add_argument(
        "window", nargs="?", default=None, help="A window id (win-<hex>), 'self', 'pinned', or an app name"
    )
    p_refresh.add_argument("--app", default=None, help="Reload every page of this app, on every client")
    _add_target_arguments(p_refresh)
    _add_force_argument(p_refresh)
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
        choices=[mode.value for mode in ShortcutMode],
        default=ShortcutMode.FOCUS.value,
        help="focus: raise the app's most recent window, opening one only when it has none; new: always open one",
    )
    p_shortcut_set.add_argument(
        "--cell", default=None, metavar="COLUMN,ROW", help="The grid cell; the next free one by default"
    )
    _add_target_arguments(p_shortcut_set)
    _add_force_argument(p_shortcut_set)
    p_shortcut_set.set_defaults(func=_cmd_shortcut_set)
    p_shortcut_move = shortcut_subparsers.add_parser("move", help="Move a shortcut to another cell")
    p_shortcut_move.add_argument("app", help="The registered app")
    p_shortcut_move.add_argument("launch", help="The launch path the shortcut runs")
    p_shortcut_move.add_argument("--cell", required=True, metavar="COLUMN,ROW", help="The grid cell to move to")
    _add_target_arguments(p_shortcut_move)
    _add_force_argument(p_shortcut_move)
    p_shortcut_move.set_defaults(func=_cmd_shortcut_move)
    p_shortcut_remove = shortcut_subparsers.add_parser("remove", help="Take a shortcut off the desktop")
    p_shortcut_remove.add_argument("app", help="The registered app")
    p_shortcut_remove.add_argument("launch", help="The launch path the shortcut runs")
    _add_target_arguments(p_shortcut_remove)
    _add_force_argument(p_shortcut_remove)
    p_shortcut_remove.set_defaults(func=_cmd_shortcut_remove)

    p_wallpaper = subparsers.add_parser("wallpaper", help="Set or clear a desktop's wallpaper")
    p_wallpaper.add_argument("kind", help="'bundled' or 'file', or 'none' to clear it")
    p_wallpaper.add_argument("name", nargs="?", default=None, help="The image's file name without its extension")
    _add_target_arguments(p_wallpaper)
    _add_force_argument(p_wallpaper)
    p_wallpaper.set_defaults(func=_cmd_wallpaper)

    for verb in RETIRED_VERBS:
        p_retired = subparsers.add_parser(verb)
        p_retired.add_argument("rest", nargs=argparse.REMAINDER)
        p_retired.set_defaults(func=_cmd_retired, verb=verb)

    return parser


def run_layout_cli(argv: Sequence[str], context: LayoutCliContext) -> int:
    """Run one subcommand against the shell and registry ``context`` names; the exit code."""
    args = _build_parser().parse_args(list(argv))
    return args.func(args, context)


def main() -> int:
    # The command reports everything it has to say itself, on the lines its callers parse; the library's own logs
    # would add lines to stderr that no caller expects.
    logger.remove()
    return run_layout_cli(sys.argv[1:], context_from_environment())
