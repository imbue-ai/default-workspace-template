#!/usr/bin/env python3
"""Agent-facing helper for inspecting and mutating the workspace layout, over addresses.

Subcommands:
    list                                List every app with its instances (address, title, status, where docked).
    inspect                             Describe the live dock (compact by default; --verbose for the YAML tree).
    where <address>                     Show one panel: its group's tab-mates and the addresses in each direction.
    context                             Show each browser client's recent messages, device kind, and active view.
    views                               List the views (projects + Everything): tab sets and the clients on each.
    load <view>                         Switch the requesting client (or --client / all clients) onto a view.
    open <address>                      Dock an instance next to the caller's chat (a no-op when it is already open).
    focus <address>                     Activate the named panel within its group.
    split <address> [...]               Add a panel relative to another panel; tabs into an adjacent group by default.
    close <address>                     Remove the named panel.
    move <address> --relative-to <address> [...]  Relocate a panel; iframe DOM is preserved.
    rename <address> <title>            Retitle an instance through its app (the title shows in every view).
    delete <address>                    Delete an instance through its app (it leaves every view).
    replace-url <address> <path-or-url> Point an instance at a path under its app, or at a URL for an app that browses.
    maximize <address>                  Maximize the panel's group within the dock.
    restore                             Exit a maximized group.
    refresh <address>                   Reload one iframe; a bare app address reloads every iframe of that app.
    shortcuts                           List a view's rail shortcuts (app, action, mode).
    shortcut set <app> <action> [...]   Add a rail shortcut to a project, or change its mode.
    shortcut remove <app> <action>      Take a rail shortcut off a project.

Every instance is named by one *address* (contracts.md section 1):

- ``app:<name>?instance=<key>`` -- one instance of an app: a chat by its agent id
  (``app:chat?instance=agent-...``), a terminal by its tmux session name
  (``app:terminal?instance=terminal-3``), a browser by its name.
- ``app:<name>`` -- a single-instance app's one tab (an app built without instances, say
  ``app:docs``), or, as an ``open`` / ``split`` target, "a fresh instance of this app" for an
  app that has instances (``open app:terminal``, ``open app:files``).

A bare word is shorthand for ``app:<word>``. The old ``chat:`` / ``terminal:`` /
``service:`` / ``url:`` / ``subagent:`` spellings are refused with the address to use
instead; ``list`` shows every address on the machine.

The workspace shows one *view* at a time: a project, or ``Everything`` (the unfiltered
home). Each connected browser client has one active, and that view is the arrangement the
client saves into. An op with no target goes to the view the connected client is looking
at; pass ``--view <name>`` (a project's name, or ``Everything``) to address a view no client
has in front, and the op then takes effect only when a connected client has it active
(failing with a clear error listing the connected clients otherwise). ``context`` tells you
which client (and view, and device kind) recently messaged each chat.

``--direction`` on ``split`` / ``move`` accepts five values: ``left`` / ``right`` /
``above`` / ``below`` target the *adjacent* group in that direction (tabbing into one that
already lives there unless ``--new-group`` is passed), and ``within`` tabs the panel into
the anchor's *own* group.

Mutating dock ops (``open`` / ``split`` / ``move`` / ``focus`` / ``close`` / ``maximize`` /
``restore`` / ``refresh``) wait for the resulting state to be observable via ``inspect``
before returning; on success they print a concise diff on stderr, on a no-op they print
``no change: ...`` and exit 0. ``maximize`` / ``restore`` / ``refresh`` have no observable
layout-state change, so they confirm the broadcast was sent. ``rename`` / ``delete`` /
``replace-url`` go through the shell's relay to the app that owns the instance and echo
the app's refusal when it gives one.

All dock ops POST one body ``{op, args, agent_id}`` to a loopback-only endpoint on the
shell. The caller's ``MNGR_AGENT_ID`` is sent both in the JSON body and as the
``X-Mngr-Agent-Id`` request header.

Output for ``list`` / ``views`` / ``context`` / ``shortcuts`` is YAML by default; pass
``--json`` for the raw structured object. ``inspect`` and ``where`` default to a compact
rendering; ``--verbose`` prints the full YAML tree.
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
ENV_MNGR_AGENT_ID = "MNGR_AGENT_ID"
MNGR_AGENT_ID_HEADER = "X-Mngr-Agent-Id"
# Escape hatch for environments without a live frontend to apply dock ops (the acceptance
# test that exercises the broadcast pipeline but has no DOM). When set to any non-empty
# value, mutating ops skip the wait-stable poll, the diff print, and no-op detection -- the
# script returns as soon as the HTTP POST succeeds. Production callers never set this.
ENV_NO_WAIT_STABLE = "MINDS_LAYOUT_NO_WAIT_STABLE"

ADDRESS_SCHEME = "app:"
ADDRESS_INSTANCE_PARAMETER = "instance="
EVERYTHING_VIEW_ID = "everything"

# The spellings addresses replaced. Each is refused by name, with the address to use
# instead, so an agent working from an old note gets the new form rather than a five
# second registration wait for an app called ``chat:alice``.
_RETIRED_PREFIXES = (
    "chat-terminal:",
    "chat:",
    "terminal:",
    "service:",
    "url:",
    "subagent:",
)
_EXTERNAL_URL_PREFIXES = ("https://", "http://")
# A bare word that may stand in for ``app:<word>``: the registry's name rule
# (``forward_port.py``'s ``NAME_PATTERN`` and ``MAX_SERVICE_NAME_LENGTH``), so a name the
# registry could never hold is refused here rather than waited for.
_APP_NAME_PATTERN = re.compile(r"^[a-z0-9_]+(?:-[a-z0-9_]+)*$")
_MAX_APP_NAME_LENGTH = 32
_INSTANCE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# How long ``open`` / ``split`` wait for a freshly-registered app to appear before giving
# up. The supervisord-managed forward_port.py call races with the agent invoking this
# script right after build-app, so a brief window where the row is not yet visible is fine.
_REGISTRATION_TIMEOUT_SECONDS = 5.0
_REGISTRATION_POLL_INTERVAL_SECONDS = 0.25

_WITHIN_DIRECTION = "within"
_CARDINAL_DIRECTIONS = ("left", "right", "above", "below")
_DIRECTIONS = (*_CARDINAL_DIRECTIONS, _WITHIN_DIRECTION)

# How long mutating ops wait for the resulting state to show up in ``inspect`` before
# declaring a timeout. The frontend autosaves with a 1.5 s debounce, so a few seconds of
# headroom covers the broadcast -> apply -> debounced save cycle.
_WAIT_STABLE_CAP_SECONDS = 5.0
_WAIT_STABLE_POLL_SECONDS = 0.25

_SHORTCUT_MODES = ("focus", "new")

# Exit codes: 0 / 1 / 3. Agents branch on "did it work"; the one distinct code worth its
# own slot is contention, where retry-with-backoff is the right response. Slot 2 is left
# to argparse's usage exit.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 3

# Sentinel the frontend resolves to the caller's own chat panel. Valid as a
# ``--relative-to`` value for ``split`` / ``move`` and as a target for any dock op.
_SELF_REF = "self"


def _workspace_base_url() -> str:
    return os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL).rstrip("/")


def _mngr_agent_id() -> str:
    return os.environ.get(ENV_MNGR_AGENT_ID, "")


def _apps_file() -> Path:
    """Path to the app registry: ``data/.state/apps.toml`` relative to cwd (the repo root), or ``MINDS_APPS_FILE``."""
    return Path(os.environ.get(ENV_APPS_FILE, DEFAULT_APPS_FILE))


# ---------- Addresses ----------


def _fail(message: str) -> None:
    sys.stderr.write(f"error: {message}\n")
    raise SystemExit(EXIT_ERROR)


def _retired_spelling_message(value: str) -> str:
    prefix = next(
        candidate for candidate in _RETIRED_PREFIXES if value.startswith(candidate)
    )
    remainder = value[len(prefix) :]
    if prefix == "chat:":
        hint = (
            f"a chat is addressed by its agent id, not its name: app:chat?instance=<agent-id> "
            f"(the chat rows of 'layout.py list' carry the id of the one titled {remainder!r})"
        )
    elif prefix == "chat-terminal:":
        hint = "an agent's terminal is the back face of its chat: address the chat as app:chat?instance=<agent-id>"
    elif prefix == "terminal:":
        hint = f"a terminal is addressed by its tmux session name: app:terminal?instance={remainder or '<session>'}"
    elif prefix == "service:":
        name, _, query = remainder.partition("?")
        if query.startswith("instance="):
            hint = f"use app:{name}?{query}"
        elif query.startswith("session="):
            hint = f"a browser is addressed by its name: app:browser?instance={query.removeprefix('session=')}"
        else:
            hint = f"use app:{name}"
    elif prefix == "url:":
        hint = "external URLs land in phase 8 of the workspace app model; until then open them in a browser instance"
    else:
        hint = "a subagent is an instance of the chat app: app:chat?instance=<parent-agent-id>.<session>"
    return (
        f"{value!r} is not an address any more; {hint}. Addresses are app:<name> or "
        f"app:<name>?instance=<key>; run 'layout.py list' to see every one on the machine"
    )


def _normalize_address(value: str) -> str:
    """Expand a bare app name into ``app:<name>``; refuse the retired spellings and external URLs by name."""
    if value == _SELF_REF or value.startswith(ADDRESS_SCHEME):
        return value
    if any(value.startswith(prefix) for prefix in _RETIRED_PREFIXES):
        _fail(_retired_spelling_message(value))
    if any(value.startswith(prefix) for prefix in _EXTERNAL_URL_PREFIXES):
        _fail(
            f"{value!r} is an external URL; opening one lands in phase 8 of the workspace app model. "
            "Until then open it in a browser instance (app:browser?instance=<name>)"
        )
    if _is_app_name(value):
        return f"{ADDRESS_SCHEME}{value}"
    _fail(
        f"{value!r} is not an address: expected app:<name>, app:<name>?instance=<key>, or a bare app name"
    )
    return value


def _is_app_name(value: str) -> bool:
    return (
        len(value) <= _MAX_APP_NAME_LENGTH
        and _APP_NAME_PATTERN.fullmatch(value) is not None
    )


def _address_parts(address: str) -> tuple[str, str | None]:
    """``(app, key)`` of a validated address; ``key`` is None for the bare form."""
    body = address[len(ADDRESS_SCHEME) :]
    name, separator, remainder = body.partition("?")
    if not separator:
        return name, None
    return name, remainder[len(ADDRESS_INSTANCE_PARAMETER) :]


def _validate_address(address: str) -> None:
    """Raise SystemExit unless ``address`` is ``self`` or a well-formed address."""
    if address == _SELF_REF:
        return
    if not address.startswith(ADDRESS_SCHEME):
        _fail(
            f"{address!r} is not an address: expected app:<name> or app:<name>?instance=<key>"
        )
    body = address[len(ADDRESS_SCHEME) :]
    name, separator, remainder = body.partition("?")
    if not _is_app_name(name):
        _fail(f"{address!r} names no app: an address starts with app:<name>")
    if separator:
        if not remainder.startswith(ADDRESS_INSTANCE_PARAMETER):
            _fail(f"{address!r}: the part after '?' must be instance=<key>")
        key = remainder[len(ADDRESS_INSTANCE_PARAMETER) :]
        if not _INSTANCE_KEY_PATTERN.fullmatch(key):
            _fail(f"{address!r}: {key!r} is not an instance key")


def _resolve_address(value: str) -> str:
    address = _normalize_address(value)
    _validate_address(address)
    return address


def _instance_address(address: str, verb: str) -> tuple[str, str]:
    """``(app, key)`` of an address that must name one instance (the relay verbs)."""
    app, key = _address_parts(address)
    if key is None:
        _fail(
            f"{verb} needs an instance address (app:<name>?instance=<key>), not the app {address!r}"
        )
    return app, str(key)


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


def _registry_row(name: str) -> dict[str, Any] | None:
    for row in _read_registry_rows(_apps_file()):
        if row.get("name") == name:
            return row
    return None


def _is_app_registered(name: str) -> bool:
    return _registry_row(name) is not None


def _has_instances(name: str) -> bool:
    row = _registry_row(name)
    return bool(row.get("instances", False)) if row is not None else False


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
    method: str, url: str, body: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any] | str]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        MNGR_AGENT_ID_HEADER: _mngr_agent_id(),
    }
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
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


def _post_layout(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
    """POST {op, args, agent_id} to /api/layout/broadcast and return (status, parsed_or_raw)."""
    return _request_json(
        "POST",
        f"{_workspace_base_url()}/api/layout/broadcast",
        {"op": op, "args": args, "agent_id": _mngr_agent_id()},
    )


def _request_rest_json(
    method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any] | str]:
    """Call one of the shell's REST routes (the relay, the projects) and parse the answer."""
    return _request_json(method, f"{_workspace_base_url()}{path}", body)


def _maybe_parse_json(text: str) -> dict[str, Any] | str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict):
        return parsed
    return text


def _report_failure(op: str, status: int, body: dict[str, Any] | str) -> int:
    """Translate (status, body) into a stderr message + exit code; only mutex contention is distinct."""
    if status == -1:
        sys.stderr.write(f"error: could not reach the workspace shell: {body}\n")
        return EXIT_ERROR
    detail: str = ""
    if isinstance(body, dict):
        detail = str(body.get("detail", body))
        if status == 412:
            sys.stderr.write(
                f"error: {op!r} has no client to apply it (HTTP 412): {detail}\n"
            )
            return EXIT_ERROR
        if status == 409:
            in_flight = body.get("in_flight") or {}
            retry_ms = body.get("retry_after_ms")
            sys.stderr.write(
                f"error: {op!r} rejected (HTTP 409 conflict): {detail}\n"
                f"  in-flight: agent_id={in_flight.get('agent_id')} op={in_flight.get('operation')} "
                f"args={in_flight.get('args')} started_at={in_flight.get('started_at')}\n"
                f"  retry_after_ms={retry_ms}\n"
            )
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
        # ``sort_keys=False`` keeps the server's intentional ordering (panels in tab order).
        yaml.safe_dump(data, sys.stdout, sort_keys=False, default_flow_style=False)


# ---------- Inspect helpers (wait-stable, diff, where, compact view) ----------


def _view_args(view: str | None) -> dict[str, str]:
    return {"view": view} if view else {}


def _fetch_layout(view: str | None = None) -> dict[str, Any] | None:
    """Run ``inspect`` once and return the parsed ``layout`` block, or None when the call failed."""
    status, body = _post_layout("inspect", _view_args(view))
    if status != 200 or not isinstance(body, dict):
        return None
    layout = body.get("layout", {})
    if not isinstance(layout, dict):
        return None
    return layout


def _walk_tree_leaves(node: Any) -> list[dict[str, Any]]:
    """Every leaf node of the inspect tree, depth-first."""
    if not isinstance(node, dict):
        return []
    if node.get("type") == "leaf":
        return [node]
    if node.get("type") == "branch":
        leaves: list[dict[str, Any]] = []
        for child in node.get("children", []) or []:
            leaves.extend(_walk_tree_leaves(child))
        return leaves
    return []


def _address_matches(requested: str, panel_address: Any) -> bool:
    """Whether a live panel's address satisfies the requested one.

    Exact match, plus one widening: a bare ``app:<name>`` is satisfied by any instance of
    that app (``app:<name>?instance=...``), the way the frontend resolves it.
    """
    if not isinstance(panel_address, str):
        return False
    if panel_address == requested:
        return True
    if "?" in requested:
        return False
    return panel_address.startswith(f"{requested}?{ADDRESS_INSTANCE_PARAMETER}")


def _find_leaf_for_address(
    layout: dict[str, Any], address: str
) -> dict[str, Any] | None:
    for leaf in _walk_tree_leaves(layout.get("tree")):
        for panel in leaf.get("panels", []) or []:
            if _address_matches(address, panel.get("address")):
                return leaf
    return None


def _find_panel_summary(layout: dict[str, Any], address: str) -> dict[str, Any] | None:
    for panel in layout.get("panels", []) or []:
        if _address_matches(address, panel.get("address")):
            return panel
    return None


def _panel_addresses(layout: dict[str, Any]) -> set[str]:
    return {
        str(panel["address"])
        for panel in layout.get("panels", []) or []
        if isinstance(panel.get("address"), str)
    }


def _require_open(op: str, *addresses: str, view: str | None = None) -> int | None:
    """Pre-flight: every named address must already be a live panel; ``self`` is trusted.

    Honors ``MINDS_LAYOUT_NO_WAIT_STABLE`` (no live frontend, so ``inspect`` says nothing
    useful). A transient ``inspect`` failure is treated as "can't tell, proceed".
    """
    if os.environ.get(ENV_NO_WAIT_STABLE):
        return None
    layout = _fetch_layout(view)
    if layout is None:
        return None
    missing = [
        address
        for address in addresses
        if address != _SELF_REF and _find_panel_summary(layout, address) is None
    ]
    if not missing:
        return None
    listed = ", ".join(repr(address) for address in missing)
    sys.stderr.write(f"error: {op}: {listed} is not open in the current layout\n")
    return EXIT_ERROR


def _addresses_in_group(leaf: dict[str, Any]) -> list[str]:
    """Tab-mate addresses in order, with the active tab marked by a trailing ``*``."""
    out: list[str] = []
    for panel in leaf.get("panels", []) or []:
        address = panel.get("address")
        if not isinstance(address, str):
            continue
        out.append(f"{address}*" if panel.get("active") else address)
    return out


def _describe_group(leaf: dict[str, Any] | None) -> str:
    if leaf is None:
        return "<absent>"
    return "tabs=[" + ", ".join(_addresses_in_group(leaf)) + "]"


# ---------- Per-op predicates ----------


_Predicate = Callable[[dict[str, Any]], bool]
_NoopMessage = Callable[[dict[str, Any]], str]
_DiffMessage = Callable[[dict[str, Any], dict[str, Any]], str]


def _predicate_present(address: str) -> _Predicate:
    return lambda layout: _find_panel_summary(layout, address) is not None


def _predicate_absent(address: str) -> _Predicate:
    return lambda layout: _find_panel_summary(layout, address) is None


def _predicate_focus(address: str) -> _Predicate:
    def check(layout: dict[str, Any]) -> bool:
        leaf = _find_leaf_for_address(layout, address)
        if leaf is None:
            return False
        for panel in leaf.get("panels", []) or []:
            if _address_matches(address, panel.get("address")):
                return bool(panel.get("active"))
        return False

    return check


def _predicate_share_group(address: str, anchor: str) -> _Predicate:
    def check(layout: dict[str, Any]) -> bool:
        leaf = _find_leaf_for_address(layout, address)
        anchor_leaf = _find_leaf_for_address(layout, anchor)
        return leaf is not None and anchor_leaf is not None and leaf is anchor_leaf

    return check


def _predicate_any_change(before: dict[str, Any]) -> _Predicate:
    before_blob = json.dumps(before, sort_keys=True)
    return lambda layout: json.dumps(layout, sort_keys=True) != before_blob


def _new_instance_address(
    app: str, before: set[str], layout: dict[str, Any]
) -> str | None:
    """An instance address of ``app`` docked now that was not docked before, or None."""
    prefix = f"{ADDRESS_SCHEME}{app}?{ADDRESS_INSTANCE_PARAMETER}"
    for address in sorted(_panel_addresses(layout) - before):
        if address.startswith(prefix):
            return address
    return None


# Marker predicate: "no observable layout-state change to confirm" (maximize / restore / refresh).
_UNOBSERVABLE: _Predicate = lambda _layout: True  # noqa: E731


# ---------- Wait-stable runner ----------


def _wait_stable(
    op: str,
    predicate: _Predicate,
    *,
    view: str | None = None,
    cap: float = _WAIT_STABLE_CAP_SECONDS,
    poll: float = _WAIT_STABLE_POLL_SECONDS,
) -> tuple[str, dict[str, Any] | None]:
    """Poll ``inspect`` until ``predicate(layout)`` holds or ``cap`` elapses: ``changed`` / ``timeout`` / ``unknown``."""
    deadline = time.monotonic() + cap
    last: dict[str, Any] | None = None
    while True:
        layout = _fetch_layout(view)
        if layout is None:
            sys.stderr.write(
                f"warning: inspect failed while waiting for {op!r} to settle\n"
            )
            return "unknown", last
        last = layout
        if predicate(layout):
            return "changed", layout
        if time.monotonic() >= deadline:
            return "timeout", layout
        time.sleep(poll)


def _run_mutating_op(
    op: str,
    args: dict[str, Any],
    predicate: _Predicate,
    *,
    on_success: _DiffMessage,
    on_noop: _NoopMessage,
    skip_pre_op_noop: bool = False,
) -> int:
    """Snapshot, post, wait, diff.

    ``_UNOBSERVABLE`` short-circuits to "post and confirm the broadcast".
    ``MINDS_LAYOUT_NO_WAIT_STABLE`` bypasses the snapshot / wait / diff path entirely.
    ``skip_pre_op_noop`` disables the pre-op no-op check for snapshot-relative predicates.
    """
    if predicate is _UNOBSERVABLE or os.environ.get(ENV_NO_WAIT_STABLE):
        status, body = _post_layout(op, args)
        if status != 200:
            return _report_failure(op, status, body)
        if predicate is _UNOBSERVABLE:
            sys.stderr.write(
                "(broadcast sent; no observable layout-state change to confirm)\n"
            )
        return EXIT_OK

    view = args.get("view")
    before = _fetch_layout(view)
    if not skip_pre_op_noop and before is not None and predicate(before):
        sys.stderr.write(on_noop(before))
        return EXIT_OK

    status, body = _post_layout(op, args)
    if status != 200:
        return _report_failure(op, status, body)

    wait_status, after = _wait_stable(op, predicate, view=view)
    if wait_status == "changed" and after is not None:
        sys.stderr.write(on_success(before or {}, after))
        return EXIT_OK
    if wait_status == "timeout":
        sys.stderr.write(
            f"error: timeout waiting for {op!r} to settle after {_WAIT_STABLE_CAP_SECONDS:.0f}s\n"
        )
        return EXIT_ERROR
    sys.stderr.write("(broadcast sent; could not read inspect to confirm new state)\n")
    return EXIT_OK


def _run_creating_op(op: str, args: dict[str, Any], app: str) -> int:
    """``open`` / ``split`` of a bare ``app:<name>`` for an app with instances: a fresh instance every time.

    The instance's key is minted by the app when the frontend runs the action, so there is
    nothing to predicate against beforehand: post first, then wait for an instance of the
    app that was not docked before, and print its address to stdout for later ops.
    """
    if os.environ.get(ENV_NO_WAIT_STABLE):
        status, body = _post_layout(op, args)
        if status != 200:
            return _report_failure(op, status, body)
        return EXIT_OK
    view = args.get("view")
    before_layout = _fetch_layout(view)
    status, body = _post_layout(op, args)
    if status != 200:
        return _report_failure(op, status, body)
    if before_layout is None:
        # Without the pre-op snapshot an instance docked earlier cannot be told from the new
        # one, and printing the wrong address as created would be worse than printing none.
        sys.stderr.write(
            f"(broadcast sent; inspect could not be read before the {op}, so the new {app} "
            "instance cannot be told apart. Run 'layout.py list' to find it)\n"
        )
        return EXIT_OK
    before = _panel_addresses(before_layout)
    wait_status, after = _wait_stable(
        op,
        lambda layout: _new_instance_address(app, before, layout) is not None,
        view=view,
    )
    if wait_status == "changed" and after is not None:
        created = _new_instance_address(app, before, after)
        sys.stdout.write(f"{created}\n")
        sys.stderr.write(
            f"created {created} in {_describe_group(_find_leaf_for_address(after, str(created)))}\n"
        )
        return EXIT_OK
    if wait_status == "timeout":
        # The client runs the app's action itself; an app that refused the create (no signed-in
        # account, a full fleet) shows up here as nothing new docked, not as an error body.
        sys.stderr.write(
            f"error: timeout waiting for {op!r} to settle after {_WAIT_STABLE_CAP_SECONDS:.0f}s: "
            f"no new {app} instance was docked. The app may have refused the create; run "
            "'layout.py list' to see its instances\n"
        )
        return EXIT_ERROR
    sys.stderr.write("(broadcast sent; could not read inspect to confirm new state)\n")
    return EXIT_OK


# ---------- Compact rendering for inspect / where ----------


def _format_tree_compact(node: Any, indent: int = 0) -> list[str]:
    pad = "  " * indent
    if not isinstance(node, dict):
        return []
    if node.get("type") == "leaf":
        size = node.get("size_ratio")
        size_str = f" size={size}" if size is not None else ""
        return [f"{pad}[{' '.join(_addresses_in_group(node))}]{size_str}"]
    if node.get("type") == "branch":
        size = node.get("size_ratio")
        size_str = f" size={size}" if size is not None else ""
        out = [f"{pad}{node.get('arrangement', '?')}{size_str}"]
        for child in node.get("children", []) or []:
            out.extend(_format_tree_compact(child, indent + 1))
        return out
    return []


def _emit_layout_view(layout: dict[str, Any], *, as_json: bool, verbose: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(layout, indent=2))
        sys.stdout.write("\n")
        return
    if verbose:
        yaml.safe_dump(layout, sys.stdout, sort_keys=False, default_flow_style=False)
        return
    active = layout.get("active_panel")
    if active is not None:
        sys.stdout.write(f"active_panel: {active}\n")
    tree = layout.get("tree")
    if tree is None:
        sys.stdout.write("(no layout)\n")
        return
    for line in _format_tree_compact(tree):
        sys.stdout.write(line + "\n")


# ---------- where: neighbor lookup by tree structure ----------


def _build_leaf_parents(
    node: Any, parent_chain: tuple[dict[str, Any], ...]
) -> dict[int, tuple[dict[str, Any], ...]]:
    out: dict[int, tuple[dict[str, Any], ...]] = {}
    if not isinstance(node, dict):
        return out
    if node.get("type") == "leaf":
        out[id(node)] = parent_chain
        return out
    if node.get("type") == "branch":
        extended = (*parent_chain, node)
        for child in node.get("children", []) or []:
            out.update(_build_leaf_parents(child, extended))
    return out


def _neighbors_in_direction(
    layout: dict[str, Any], leaf: dict[str, Any], direction: str
) -> list[dict[str, Any]]:
    """Leaves adjacent to ``leaf`` in ``direction``: the nearest ancestor of the matching arrangement decides."""
    tree = layout.get("tree")
    if tree is None:
        return []
    chain = _build_leaf_parents(tree, ()).get(id(leaf), ())
    if not chain:
        return []
    target_arrangement = "row" if direction in ("left", "right") else "column"
    side = "before" if direction in ("left", "above") else "after"
    current: dict[str, Any] = leaf
    for ancestor in reversed(chain):
        if ancestor.get("arrangement") != target_arrangement:
            current = ancestor
            continue
        children = ancestor.get("children", []) or []
        try:
            idx = next(i for i, c in enumerate(children) if c is current)
        except StopIteration:
            return []
        if side == "before" and idx > 0:
            return _walk_tree_leaves(children[idx - 1])
        if side == "after" and idx < len(children) - 1:
            return _walk_tree_leaves(children[idx + 1])
        current = ancestor
    return []


# ---------- Read subcommands ----------


def _cmd_list(args: argparse.Namespace) -> int:
    status, body = _post_layout("list", _view_args(args.view))
    if status != 200 or not isinstance(body, dict):
        return _report_failure("list", status, body)
    _emit_structured(body.get("apps", []), args.json)
    return EXIT_OK


def _cmd_inspect(args: argparse.Namespace) -> int:
    status, body = _post_layout("inspect", _view_args(args.view))
    if status != 200 or not isinstance(body, dict):
        return _report_failure("inspect", status, body)
    layout = body.get("layout", {})
    if not isinstance(layout, dict):
        layout = {}
    if not args.json:
        sys.stderr.write(
            f"(view: {body.get('view_id')}, client: {body.get('client_id')})\n"
        )
    _emit_layout_view(layout, as_json=args.json, verbose=args.verbose)
    return EXIT_OK


def _cmd_context(args: argparse.Namespace) -> int:
    status, body = _post_layout("context", {})
    if status != 200 or not isinstance(body, dict):
        return _report_failure("context", status, body)
    _emit_structured(body.get("clients", []), args.json)
    return EXIT_OK


def _cmd_views(args: argparse.Namespace) -> int:
    status, body = _post_layout("views", {})
    if status != 200 or not isinstance(body, dict):
        return _report_failure("views", status, body)
    _emit_structured(body.get("views", []), args.json)
    return EXIT_OK


def _cmd_load(args: argparse.Namespace) -> int:
    load_args: dict[str, Any] = {"view": args.view_name}
    if args.client:
        load_args["client"] = args.client
    status, body = _post_layout("load", load_args)
    if status != 200 or not isinstance(body, dict):
        return _report_failure("load", status, body)
    target = body.get("target_client_id")
    target_text = (
        f"client {target}" if target else "all clients (requesting client unknown)"
    )
    sys.stderr.write(
        f"requested load of view {body.get('view_id')!r} on {target_text}\n"
    )
    return EXIT_OK


def _cmd_where(args: argparse.Namespace) -> int:
    address = _resolve_address(args.address)
    if address == _SELF_REF:
        sys.stderr.write(
            "error: 'self' is not resolvable from the CLI; pass your chat's address "
            "(app:chat?instance=$MNGR_AGENT_ID) or use ``inspect`` to see every address\n"
        )
        return EXIT_ERROR
    layout = _fetch_layout(args.view)
    if layout is None:
        sys.stderr.write("error: inspect failed; could not locate the panel\n")
        return EXIT_ERROR
    leaf = _find_leaf_for_address(layout, address)
    if leaf is None:
        sys.stderr.write(f"error: {address!r} is not currently open\n")
        return EXIT_ERROR
    panel_summary = _find_panel_summary(layout, address) or {}
    view: dict[str, Any] = {
        "address": panel_summary.get("address", address),
        "title": panel_summary.get("title"),
        "tab_id": panel_summary.get("tab_id"),
        "group": {
            "size_ratio": leaf.get("size_ratio"),
            "tabs": _addresses_in_group(leaf),
        },
        "neighbors": {
            direction: [
                tab
                for neighbor in _neighbors_in_direction(layout, leaf, direction)
                for tab in _addresses_in_group(neighbor)
            ]
            for direction in _CARDINAL_DIRECTIONS
        },
    }
    if args.verbose:
        view["full_layout"] = layout
    if args.json:
        sys.stdout.write(json.dumps(view, indent=2))
        sys.stdout.write("\n")
        return EXIT_OK
    if args.verbose:
        yaml.safe_dump(view, sys.stdout, sort_keys=False, default_flow_style=False)
        return EXIT_OK
    sys.stdout.write(f"address: {view['address']}\n")
    if panel_summary.get("title"):
        sys.stdout.write(f"title:   {panel_summary['title']}\n")
    sys.stdout.write(f"group:   [{' '.join(view['group']['tabs'])}]\n")
    for direction in _CARDINAL_DIRECTIONS:
        neighbor_addresses = view["neighbors"][direction]
        rendered = (
            "[" + " ".join(neighbor_addresses) + "]" if neighbor_addresses else "-"
        )
        sys.stdout.write(f"{direction:<8} {rendered}\n")
    return EXIT_OK


# ---------- Dock subcommands ----------


def _cmd_open(args: argparse.Namespace) -> int:
    address = _resolve_address(args.target)
    if address == _SELF_REF:
        _fail("open needs an address, not 'self'")
    app, key = _address_parts(address)
    if (err := _require_registered(app)) is not None:
        return err
    payload: dict[str, Any] = {
        "address": address,
        "new_group": bool(args.new_group),
        "view": args.view,
    }
    if key is None and _has_instances(app):
        return _run_creating_op("open", payload, app)
    return _run_mutating_op(
        "open",
        payload,
        _predicate_present(address),
        on_success=lambda b,
        a: f"opened {address} in {_describe_group(_find_leaf_for_address(a, address))}\n",
        on_noop=lambda b: f"no change: {address} is already open in {_describe_group(_find_leaf_for_address(b, address))}\n",
    )


def _cmd_focus(args: argparse.Namespace) -> int:
    address = _resolve_address(args.address)
    if (err := _require_open("focus", address, view=args.view)) is not None:
        return err
    return _run_mutating_op(
        "focus",
        {"address": address, "view": args.view},
        _predicate_focus(address),
        on_success=lambda b, a: f"focused {address}\n",
        on_noop=lambda b: f"no change: {address} is already the active tab in its group\n",
    )


def _cmd_split(args: argparse.Namespace) -> int:
    if args.direction == _WITHIN_DIRECTION and args.new_group:
        sys.stderr.write(
            f"error: --new-group is meaningless with --direction={_WITHIN_DIRECTION} "
            f"(within tabs into the anchor's own group)\n"
        )
        return EXIT_ERROR
    address = _resolve_address(args.target)
    if address == _SELF_REF:
        _fail("split needs an address to create, not 'self'")
    app, key = _address_parts(address)
    if (err := _require_registered(app)) is not None:
        return err
    relative_to = _resolve_address(args.relative_to)
    if (err := _require_open("split", relative_to, view=args.view)) is not None:
        return err
    payload: dict[str, Any] = {
        "address": address,
        "relative_to": relative_to,
        "direction": args.direction,
        "ratio": args.ratio,
        "new_group": bool(args.new_group),
        "view": args.view,
    }
    if key is None and _has_instances(app):
        return _run_creating_op("split", payload, app)
    return _run_mutating_op(
        "split",
        payload,
        _predicate_present(address),
        on_success=lambda b,
        a: f"split: {address} now in {_describe_group(_find_leaf_for_address(a, address))}\n",
        on_noop=lambda b: f"no change: {address} is already open in {_describe_group(_find_leaf_for_address(b, address))}\n",
    )


def _cmd_close(args: argparse.Namespace) -> int:
    address = _resolve_address(args.address)
    return _run_mutating_op(
        "close",
        {"address": address, "view": args.view},
        _predicate_absent(address),
        on_success=lambda b, a: f"closed {address}\n",
        on_noop=lambda b: f"no change: {address} is already closed\n",
    )


def _cmd_move(args: argparse.Namespace) -> int:
    if args.direction == _WITHIN_DIRECTION and args.new_group:
        sys.stderr.write(
            f"error: --new-group is meaningless with --direction={_WITHIN_DIRECTION} "
            f"(within targets the anchor's own group)\n"
        )
        return EXIT_ERROR
    address = _resolve_address(args.address)
    relative_to = _resolve_address(args.relative_to)
    if (err := _require_open("move", address, relative_to, view=args.view)) is not None:
        return err
    payload: dict[str, Any] = {
        "address": address,
        "relative_to": relative_to,
        "direction": args.direction,
        "new_group": bool(args.new_group),
        "view": args.view,
    }
    # ``within`` with an explicit anchor is a real invariant (the two share a leaf); every
    # other form's end position depends on the live tree, so "something changed" against a
    # snapshot taken right before the post is the honest predicate, and it cannot serve
    # pre-op no-op detection.
    skip_pre_op_noop = False
    if args.direction == _WITHIN_DIRECTION and relative_to != _SELF_REF:
        predicate: _Predicate = _predicate_share_group(address, relative_to)
        on_noop: _NoopMessage = (
            lambda b: f"no change: {address} is already in the same group as {relative_to}\n"
        )
    elif os.environ.get(ENV_NO_WAIT_STABLE):
        predicate = lambda _layout: False  # noqa: E731
        on_noop = lambda b: ""  # noqa: E731
    else:
        before_snapshot = _fetch_layout(args.view)
        if before_snapshot is None:
            sys.stderr.write(
                "warning: inspect failed before move; will not detect a no-op\n"
            )
            before_snapshot = {}
        predicate = _predicate_any_change(before_snapshot)
        skip_pre_op_noop = True
        on_noop = lambda b: ""  # noqa: E731
    return _run_mutating_op(
        "move",
        payload,
        predicate,
        on_success=lambda b,
        a: f"moved {address} into {_describe_group(_find_leaf_for_address(a, address))}\n",
        on_noop=on_noop,
        skip_pre_op_noop=skip_pre_op_noop,
    )


def _cmd_maximize(args: argparse.Namespace) -> int:
    address = _resolve_address(args.address)
    if (err := _require_open("maximize", address, view=args.view)) is not None:
        return err
    return _run_mutating_op(
        "maximize",
        {"address": address, "view": args.view},
        _UNOBSERVABLE,
        on_success=lambda b, a: "",
        on_noop=lambda b: "",
    )


def _cmd_restore(args: argparse.Namespace) -> int:
    return _run_mutating_op(
        "restore",
        {"view": args.view},
        _UNOBSERVABLE,
        on_success=lambda b, a: "",
        on_noop=lambda b: "",
    )


def _cmd_refresh(args: argparse.Namespace) -> int:
    address = _resolve_address(args.target)
    # A bare app address reloads every iframe of that app on every client, so it need not
    # itself be open; an instance address reloads one panel and must be.
    if "?" in address and (err := _require_open("refresh", address)) is not None:
        return err
    return _run_mutating_op(
        "refresh",
        {"address": address},
        _UNOBSERVABLE,
        on_success=lambda b, a: "",
        on_noop=lambda b: "",
    )


# ---------- Relay subcommands (the instance verbs) ----------


def _relay(verb: str, address: str, suffix: str, body: dict[str, Any] | None) -> int:
    """POST one relay verb for the instance ``address`` names; the app's refusal is printed with the address."""
    app, key = _instance_address(address, verb)
    path = f"/api/apps/{urllib.parse.quote(app)}/instances/{urllib.parse.quote(key)}{suffix}"
    status, response = _request_rest_json("POST", path, body)
    if status == -1:
        sys.stderr.write(f"error: could not reach the workspace shell: {response}\n")
        return EXIT_ERROR
    if status >= 400:
        detail = (
            response.get("detail", response) if isinstance(response, dict) else response
        )
        sys.stderr.write(f"error: {verb} {address} refused (HTTP {status}): {detail}\n")
        return EXIT_ERROR
    return EXIT_OK


def _cmd_rename(args: argparse.Namespace) -> int:
    address = _resolve_address(args.address)
    exit_code = _relay("rename", address, "/rename", {"title": args.title})
    if exit_code == EXIT_OK:
        sys.stderr.write(f"renamed {address} to {args.title!r}\n")
    return exit_code


def _cmd_delete(args: argparse.Namespace) -> int:
    address = _resolve_address(args.address)
    exit_code = _relay("delete", address, "/delete", None)
    if exit_code == EXIT_OK:
        sys.stderr.write(f"deleted {address}\n")
    return exit_code


def _cmd_replace_url(args: argparse.Namespace) -> int:
    address = _resolve_address(args.address)
    if not args.path:
        _fail(
            "replace-url needs a path under the instance's app, or an absolute http(s) URL"
        )
    # Which form the app takes (a rooted path for a file viewer, an absolute URL for the
    # browser) is the app's rule, and it answers 400 for the other; nothing is checked here.
    exit_code = _relay("replace-url", address, "/location", {"path": args.path})
    if exit_code == EXIT_OK:
        sys.stderr.write(f"pointed {address} at {args.path}\n")
    return exit_code


# ---------- Shortcuts (per-project rail configuration) ----------


def _fetch_projects() -> list[dict[str, Any]] | None:
    status, body = _request_rest_json("GET", "/api/projects")
    if (
        status != 200
        or not isinstance(body, dict)
        or not isinstance(body.get("projects"), list)
    ):
        sys.stderr.write(f"error: could not list projects (HTTP {status}): {body}\n")
        return None
    return list(body["projects"])


def _active_view_of_a_connected_client() -> str | None:
    """The view the most recently active connected client is on, from ``context``."""
    status, body = _post_layout("context", {})
    if status != 200 or not isinstance(body, dict):
        return None
    for client in body.get("clients", []) or []:
        if (
            isinstance(client, dict)
            and client.get("is_connected")
            and client.get("active_view")
        ):
            return str(client["active_view"])
    return None


def _resolve_project_view(
    view_name: str | None,
) -> tuple[str, dict[str, Any] | None] | None:
    """``(view_id, project)`` for ``--view``; ``project`` is None for Everything. None on error."""
    projects = _fetch_projects()
    if projects is None:
        return None
    if view_name is None:
        view_name = _active_view_of_a_connected_client()
        if view_name is None:
            sys.stderr.write(
                "error: no connected client to take the view from; pass --view <project name>\n"
            )
            return None
    if view_name.strip().lower() == EVERYTHING_VIEW_ID:
        return EVERYTHING_VIEW_ID, None
    wanted = view_name.strip().lower()
    for project in projects:
        if (
            str(project.get("name", "")).strip().lower() == wanted
            or project.get("id") == view_name
        ):
            return str(project.get("id")), project
    known = ", ".join(repr(str(p.get("name", p.get("id", "?")))) for p in projects)
    sys.stderr.write(
        f"error: no project named {view_name!r} (projects: {known or '<none>'}, or 'Everything')\n"
    )
    return None


def _primary_action_id(app: dict[str, Any]) -> str | None:
    """The action an app's rail row runs, as the shell picks it: its ``default_shortcut`` action
    when declared, else its first declared action, else the synthesized ``open`` of a
    single-instance app; None for an app with instances that declares no action."""
    if not bool(app.get("instances", False)):
        return "open"
    actions = app.get("actions")
    action_ids = (
        [str(action.get("id")) for action in actions if hasattr(action, "get")]
        if isinstance(actions, list)
        else []
    )
    default_shortcut = app.get("default_shortcut")
    if hasattr(default_shortcut, "get"):
        default_action = str(default_shortcut.get("action", ""))
        if default_action in action_ids:
            return default_action
    return action_ids[0] if action_ids else None


def _everything_shortcut_rows() -> list[dict[str, Any]]:
    """Everything's rail: every registered app's primary action, in registry order, in focus mode."""
    rows: list[dict[str, Any]] = []
    for app in _read_registry_rows(_apps_file()):
        if bool(app.get("internal", False)):
            continue
        action_id = _primary_action_id(app)
        if action_id is None:
            continue
        rows.append({"app": str(app["name"]), "action": action_id, "mode": "focus"})
    return rows


def _cmd_shortcuts(args: argparse.Namespace) -> int:
    resolved = _resolve_project_view(args.view)
    if resolved is None:
        return EXIT_ERROR
    view_id, project = resolved
    rows = (
        project.get("shortcuts", [])
        if project is not None
        else _everything_shortcut_rows()
    )
    _emit_structured({"view": view_id, "shortcuts": rows}, args.json)
    return EXIT_OK


def _project_for_shortcut_write(view_name: str | None) -> str | None:
    resolved = _resolve_project_view(view_name)
    if resolved is None:
        return None
    project_id, project = resolved
    if project is None:
        sys.stderr.write(
            "error: Everything's rail is fixed (every app's primary action); pass --view <project name>\n"
        )
        return None
    return project_id


def _run_shortcut_write(
    verb: str, path: str, body: dict[str, Any], project_id: str, done_line: str
) -> int:
    """POST one shortcut write to the shell and print the project's rail as it stands after it."""
    status, response = _request_rest_json("POST", path, body)
    if status != 200 or not isinstance(response, dict):
        detail = (
            response.get("detail", response) if isinstance(response, dict) else response
        )
        sys.stderr.write(f"error: shortcut {verb} failed (HTTP {status}): {detail}\n")
        return EXIT_ERROR
    sys.stderr.write(f"{done_line}\n")
    _emit_structured(
        {"project_id": project_id, "shortcuts": response.get("shortcuts", [])}, False
    )
    return EXIT_OK


def _cmd_shortcut_set(args: argparse.Namespace) -> int:
    project_id = _project_for_shortcut_write(args.view)
    if project_id is None:
        return EXIT_ERROR
    return _run_shortcut_write(
        "set",
        f"/api/projects/{project_id}/shortcuts",
        {"app": args.app, "action": args.action, "mode": args.mode},
        project_id,
        f"set {args.app} {args.action} ({args.mode}) on project {project_id!r}",
    )


def _cmd_shortcut_remove(args: argparse.Namespace) -> int:
    project_id = _project_for_shortcut_write(args.view)
    if project_id is None:
        return EXIT_ERROR
    return _run_shortcut_write(
        "remove",
        f"/api/projects/{project_id}/shortcuts/remove",
        {"app": args.app, "action": args.action},
        project_id,
        f"removed {args.app} {args.action} from project {project_id!r}",
    )


# ---------- The parser ----------


def _add_view_argument(subparser: argparse.ArgumentParser, help_text: str) -> None:
    subparser.add_argument(
        "--view", "--layout", dest="view", metavar="VIEW", default=None, help=help_text
    )


_MUTATING_VIEW_HELP = (
    "View to mutate: a project's name, or ``Everything``. Defaults to the view the connected "
    "client is on. Mutating ops only apply on connected clients that have the view active; "
    "use ``context`` to see each client's current view."
)
_READ_VIEW_HELP = "View (a project's name, or ``Everything``) to read; defaults to the view the connected client is on."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_list = subparsers.add_parser("list", help="List every app with its instances")
    p_list.add_argument("--json", action="store_true", help="Emit JSON instead of YAML")
    _add_view_argument(p_list, _READ_VIEW_HELP)
    p_list.set_defaults(func=_cmd_list)

    p_inspect = subparsers.add_parser(
        "inspect", help="Describe the live dock (compact text by default)"
    )
    p_inspect.add_argument(
        "--json", action="store_true", help="Emit JSON (full detail)"
    )
    p_inspect.add_argument(
        "--verbose",
        action="store_true",
        help="Emit the full YAML tree instead of the compact view",
    )
    _add_view_argument(p_inspect, _READ_VIEW_HELP)
    p_inspect.set_defaults(func=_cmd_inspect)

    p_context = subparsers.add_parser(
        "context",
        help="Show each browser client's recent messages, device kind, and active view",
    )
    p_context.add_argument(
        "--json", action="store_true", help="Emit JSON instead of YAML"
    )
    p_context.set_defaults(func=_cmd_context)

    p_views = subparsers.add_parser(
        "views",
        help="List the views: every project plus Everything, their tab sets, and who is on each",
    )
    p_views.add_argument(
        "--json", action="store_true", help="Emit JSON instead of YAML"
    )
    p_views.set_defaults(func=_cmd_views)

    p_load = subparsers.add_parser(
        "load", help="Switch a client onto a view (a project, or ``Everything``)"
    )
    p_load.add_argument(
        "view_name",
        metavar="view",
        help="The view to put in front: a project's name, or ``Everything``",
    )
    p_load.add_argument(
        "--client",
        default=None,
        help="Explicit client id to switch (see ``context``). Defaults to the client that most recently "
        "messaged you; falls back to every connected client when that cannot be determined.",
    )
    p_load.set_defaults(func=_cmd_load)

    p_where = subparsers.add_parser(
        "where", help="Show one panel's group tab-mates and its neighbors"
    )
    p_where.add_argument("address", help="Panel address (bare app name accepted)")
    p_where.add_argument(
        "--json", action="store_true", help="Emit JSON instead of text"
    )
    p_where.add_argument(
        "--verbose",
        action="store_true",
        help="Also include the full inspect layout under ``full_layout``",
    )
    _add_view_argument(p_where, _READ_VIEW_HELP)
    p_where.set_defaults(func=_cmd_where)

    p_open = subparsers.add_parser("open", help="Surface an instance in the UI")
    p_open.add_argument(
        "target",
        help="An address (``app:terminal?instance=terminal-2`` docks that instance; ``app:docs`` docks a "
        "single-instance app's one tab) or a bare app name. A bare name of an app with instances creates a "
        "fresh one and prints its address.",
    )
    p_open.add_argument(
        "--new-group",
        action="store_true",
        help="Force a brand-new dock group instead of tabbing into an existing right-side group.",
    )
    _add_view_argument(p_open, _MUTATING_VIEW_HELP)
    p_open.set_defaults(func=_cmd_open)

    p_focus = subparsers.add_parser(
        "focus", help="Activate the named panel within its group"
    )
    p_focus.add_argument("address", help="Panel address")
    _add_view_argument(p_focus, _MUTATING_VIEW_HELP)
    p_focus.set_defaults(func=_cmd_focus)

    p_split = subparsers.add_parser("split", help="Open a new panel as a split")
    p_split.add_argument(
        "target", help="Address or bare app name to open as the new panel"
    )
    p_split.add_argument(
        "--relative-to",
        default=_SELF_REF,
        help="Address to split relative to. ``self`` (default) resolves to the caller's chat panel.",
    )
    p_split.add_argument(
        "--direction",
        default="right",
        choices=_DIRECTIONS,
        help="Where to place the new panel relative to the anchor; ``within`` tabs it into the anchor's own group.",
    )
    p_split.add_argument(
        "--ratio",
        type=float,
        default=0.6,
        help="Fraction the new panel occupies (0..1); ignored with --direction=within.",
    )
    p_split.add_argument(
        "--new-group",
        action="store_true",
        help="Force a brand-new dock group instead of tabbing into the group in the requested direction.",
    )
    _add_view_argument(p_split, _MUTATING_VIEW_HELP)
    p_split.set_defaults(func=_cmd_split)

    p_close = subparsers.add_parser("close", help="Remove a panel")
    p_close.add_argument("address", help="Panel address")
    _add_view_argument(p_close, _MUTATING_VIEW_HELP)
    p_close.set_defaults(func=_cmd_close)

    p_move = subparsers.add_parser(
        "move", help="Relocate an existing panel (state-preserving)"
    )
    p_move.add_argument("address", help="Panel address to move")
    p_move.add_argument(
        "--relative-to", required=True, help="Address to move relative to"
    )
    p_move.add_argument(
        "--direction",
        required=True,
        choices=_DIRECTIONS,
        help="Where to land the moved panel; ``within`` tabs it into the anchor's own group.",
    )
    p_move.add_argument(
        "--new-group",
        action="store_true",
        help="Force a brand-new dock group instead of moving into an adjacent existing group.",
    )
    _add_view_argument(p_move, _MUTATING_VIEW_HELP)
    p_move.set_defaults(func=_cmd_move)

    p_rename = subparsers.add_parser(
        "rename", help="Retitle an instance through its app"
    )
    p_rename.add_argument(
        "address", help="Instance address (app:<name>?instance=<key>)"
    )
    p_rename.add_argument("title", help="New title, shown in every view")
    p_rename.set_defaults(func=_cmd_rename)

    p_delete = subparsers.add_parser(
        "delete", help="Delete an instance through its app"
    )
    p_delete.add_argument(
        "address", help="Instance address (app:<name>?instance=<key>)"
    )
    p_delete.set_defaults(func=_cmd_delete)

    p_max = subparsers.add_parser("maximize", help="Maximize a panel's group")
    p_max.add_argument("address", help="Panel address")
    _add_view_argument(p_max, _MUTATING_VIEW_HELP)
    p_max.set_defaults(func=_cmd_maximize)

    p_restore = subparsers.add_parser("restore", help="Exit a maximized group")
    _add_view_argument(p_restore, _MUTATING_VIEW_HELP)
    p_restore.set_defaults(func=_cmd_restore)

    p_replace = subparsers.add_parser(
        "replace-url",
        help="Point an instance at a path under its app, or at an absolute http(s) URL for an app that browses to one",
    )
    p_replace.add_argument(
        "address", help="Instance address (app:<name>?instance=<key>)"
    )
    p_replace.add_argument(
        "path",
        help="A path under the app starting with '/', or an absolute http(s) URL (the app refuses the form it does not take)",
    )
    p_replace.set_defaults(func=_cmd_replace_url)

    p_refresh = subparsers.add_parser(
        "refresh", help="Reload an iframe (or every iframe of an app)"
    )
    p_refresh.add_argument(
        "target",
        help="Panel address; a bare app address reloads every iframe of that app.",
    )
    p_refresh.set_defaults(func=_cmd_refresh)

    p_shortcuts = subparsers.add_parser(
        "shortcuts", help="List a view's rail shortcuts: app, action, mode"
    )
    _add_view_argument(
        p_shortcuts,
        "View to read: a project's name, or ``Everything``. Defaults to the connected client's view.",
    )
    p_shortcuts.add_argument(
        "--json", action="store_true", help="Emit JSON instead of YAML"
    )
    p_shortcuts.set_defaults(func=_cmd_shortcuts)

    p_shortcut = subparsers.add_parser(
        "shortcut", help="Configure one rail shortcut on a project"
    )
    shortcut_subparsers = p_shortcut.add_subparsers(
        dest="shortcut_command", required=True
    )
    p_shortcut_set = shortcut_subparsers.add_parser(
        "set", help="Add a shortcut to a project's rail, or change its mode"
    )
    p_shortcut_set.add_argument("app", help="The registered app")
    p_shortcut_set.add_argument(
        "action", help="One of the app's actions (``open`` for a single-instance app)"
    )
    p_shortcut_set.add_argument(
        "--mode",
        choices=_SHORTCUT_MODES,
        default="focus",
        help="What clicking the row does: ``focus`` the app's most recent tab (creating only when it has none), or always create (``new``)",
    )
    _add_view_argument(
        p_shortcut_set,
        "Project to configure, by name. Defaults to the connected client's view; Everything is refused.",
    )
    p_shortcut_set.set_defaults(func=_cmd_shortcut_set)
    p_shortcut_remove = shortcut_subparsers.add_parser(
        "remove", help="Take a shortcut off a project's rail"
    )
    p_shortcut_remove.add_argument("app", help="The registered app")
    p_shortcut_remove.add_argument("action", help="The action the row runs")
    _add_view_argument(
        p_shortcut_remove,
        "Project to configure, by name. Defaults to the connected client's view; Everything is refused.",
    )
    p_shortcut_remove.set_defaults(func=_cmd_shortcut_remove)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
