#!/usr/bin/env python3
"""Register or remove an app port in data/.state/apps.toml.

Uses file locking to safely upsert or remove entries. Called by services
on startup to declare the ports they expose.

Usage:
    python3 system/scripts/forward_port.py --manifest system/apps/files/app.toml --url http://localhost:8300
    python3 system/scripts/forward_port.py --name terminal --url http://localhost:7681
    python3 system/scripts/forward_port.py --icon-file system/apps/foo/icon.svg --name foo --url http://localhost:8090
    python3 system/scripts/forward_port.py --remove --name terminal

This script is deliberately standard-library only: every supervisord program
line runs it under a plain ``python3`` before the program's own command, so
registration must never depend on the root venv being intact. It reads TOML
with ``tomllib`` and writes the registry's flat ``[[apps]]`` shape with the
private writer below.

Manifests
---------
An app with a directory ships ``system/apps/<package>/app.toml`` (see
``system/libs/app_manifest`` for the schema). ``--manifest <path>`` reads it
and copies its static fields onto the row: ``display_name``, ``instances``,
``instances_url``, ``critical``, ``priority``, ``program`` (default: the name),
``internal``, ``default_shortcut``, and ``actions`` (id and label only); the
icon is read from the file the manifest names, relative to the manifest. Every
manifest field is authoritative on every call, so a re-registration with a
changed manifest updates the row. Only what is copied from files is checked
here (the name rule, the icon markup, the value types); the manifest's other
rules are the ``app_manifest`` library's job, applied by ``validate-manifest``
and by every reader of the registry. ``--name --url`` without a manifest keeps
registering rows for things with no app directory (owner-exec, the VM exec
service, previews, isolated test servers).

Icons
-----
An app may register an icon so the workspace UI can draw it instead of a
generic glyph. **The registry stores the SVG markup itself**, as a plain TOML
string on the entry, not a path to a file. A path would have to be readable by
every consumer -- the system-interface server, the desktop client, and anything
reading the registry off a shared host -- at whatever moment it renders, and
those do not share a filesystem view with the service that registered. The
markup travels with the entry through the existing apps.toml -> app-watcher
event -> WebSocket path with no extra plumbing and no file access at all.
``--icon-file`` (and the manifest's ``icon``) is only an input convenience: the
file is read once here, at registration time, and its contents (not its path)
are what gets persisted.

The markup is validated before it is stored, because it is eventually inlined
into the workspace DOM: ``validate_icon`` accepts exactly one well-formed
``<svg>`` element with nothing that executes or reaches off the page, capped
at ``MAX_ICON_LENGTH`` (see its docstring). An icon is REQUIRED when a
registration would create a new, non-``--internal`` entry; existing entries
re-register untouched. ``--no-icon`` skips the requirement, keeping the
generic letter monogram -- it does not hide the entry (that is
``--internal``'s job).
"""

import argparse
import fcntl
import os
import re
import secrets
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ElementTree
from pathlib import Path

DEFAULT_APPS_FILE = "data/.state/apps.toml"
ENV_APPS_FILE = "MINDS_APPS_FILE"

# Each service's public origin uses an UNGUESSABLE hostname label,
# ``<name>-<rand>`` (e.g. ``terminal-x7k9q2w1``), both locally
# (``<label>.host-<hex>.localhost``) and on a share
# (``<label>.<ws-domain>``). On a share the workspace's frpc claims these
# explicit labels instead of the wildcard, so the relay drops any SNI it was
# not told about -- CT only ever exposes the bare ``*.<ws-domain>`` cert name,
# which no longer routes. The random suffix is the one hostname component CT
# never sees. Minted once per service and persisted (stable across re-share so
# bookmarks/layouts keep working); see blueprint/random-service-labels/.
_LABEL_RANDOM_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_LABEL_RANDOM_LENGTH = 8

# Cap the service name so ``<name>-<rand>`` always fits a 63-char DNS label
# (name + 1 hyphen + 8 random chars), with generous headroom.
MAX_SERVICE_NAME_LENGTH = 32

# Service-name rule: lowercase alphanumeric/underscore runs separated by
# single hyphens (no uppercase, no leading/trailing/consecutive hyphens).
# The registered name becomes the leading label of the service's origin
# hostname (http://<name>.host-<hex>.localhost:8421/ locally; share
# hostnames follow the same prefix rule on a longer base), so it must work
# as a hostname label. Underscores are tolerated for legacy names --
# ``system_interface`` predates this scheme and underscore labels resolve
# fine in browsers -- but new apps should stick to kebab-case (the build-app
# scaffold enforces the stricter rule; a drift test in forward_port_test.py
# keeps that rule a subset of this one, and keeps the app_manifest library's
# copy of this rule identical).
NAME_PATTERN = re.compile(r"^[a-z0-9_]+(?:-[a-z0-9_]+)*$")

# Workspace hostnames carry their coordinate as a ``host-<hex>`` label (and
# ``agent-`` is the legacy spelling of the same coordinate). A service whose
# name starts with either prefix could collide with the coordinate label when
# hostnames are parsed, so both prefixes are reserved.
RESERVED_NAME_PREFIXES = ("host-", "agent-")

# ``localhost`` is the local origin's root domain; a service by that name
# would produce the nonsense hostname ``localhost.host-<hex>.localhost``.
# ``auth`` is reserved for the share stack's dedicated ``auth-<rand>`` label
# (the sole public ``/_auth/*`` origin), so no app may claim it.
RESERVED_NAMES = frozenset({"localhost", "auth"})

# Cap on the stored SVG markup. Generous for a hand-drawn or exported glyph
# (icons in this repo run a few hundred bytes) while keeping apps.toml small:
# every consumer re-reads the whole registry on every change, and the markup
# is broadcast to every connected client.
MAX_ICON_LENGTH = 16384

# The SVG namespace, as ElementTree spells it in a parsed tag.
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"

# Elements that must not appear in stored markup, because inlining them would
# run code (``script``), leak rules into the host document (``style``), or
# embed arbitrary HTML (``foreignObject``).
_FORBIDDEN_ICON_ELEMENTS = frozenset({"script", "style", "foreignobject"})

# Attributes that name a resource. Only same-document references (``#id``) are
# allowed, so an icon can never fetch, track, or embed anything remote.
_ICON_REFERENCE_ATTRIBUTES = frozenset({"href", "src"})

# Control characters have no business in markup and would have to be escaped to
# survive a TOML round trip; tab/newline/carriage return are ordinary
# whitespace in XML and are kept.
_ALLOWED_CONTROL_CHARACTERS = frozenset({"\t", "\n", "\r"})

# The manifest keys copied verbatim onto the row, with the type each must have.
# ``name`` (validated separately), ``icon`` (read from the named file), and the
# two structured keys (``default_shortcut``, ``actions``) are handled on their
# own. ``program`` defaults to the name when the manifest omits it.
_MANIFEST_STRING_KEYS = ("display_name", "instances_url", "priority", "program")
_MANIFEST_BOOL_KEYS = ("instances", "critical", "internal")

# The registry keys a manifest owns. A manifest registration rewrites every one
# of them (absent in the manifest means absent on the row), so a stale value
# from an earlier manifest never lingers.
_MANIFEST_OWNED_KEYS = (
    "display_name",
    "instances",
    "instances_url",
    "critical",
    "priority",
    "program",
    "internal",
    "default_shortcut",
    "actions",
)

# The TOML basic-string escapes for the characters that have a short form;
# every other control character is written as ``\uXXXX``.
_TOML_SHORT_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _local_name(tag: str) -> str:
    """Strip ElementTree's ``{namespace}`` prefix from a tag or attribute name."""
    if tag.startswith("{"):
        return tag.rpartition("}")[2]
    return tag


def _validate_icon_element(element: ElementTree.Element) -> str | None:
    """Return an error message when ``element`` or any descendant is unsafe to inline."""
    for node in element.iter():
        if not isinstance(node.tag, str):
            # A comment or processing instruction survived the raw-markup
            # check; treat it the same way.
            return "invalid icon: comments and processing instructions are not allowed"
        tag = _local_name(node.tag).lower()
        if tag in _FORBIDDEN_ICON_ELEMENTS:
            return f"invalid icon: <{tag}> elements are not allowed"
        for attribute_name, attribute_value in node.attrib.items():
            name = _local_name(attribute_name).lower()
            if name.startswith("on"):
                return f"invalid icon: event-handler attribute {name!r} is not allowed"
            if "javascript:" in attribute_value.lower().replace(" ", ""):
                return f"invalid icon: attribute {name!r} contains a javascript: URL"
            if name in _ICON_REFERENCE_ATTRIBUTES and not attribute_value.startswith("#"):
                return (
                    f"invalid icon: attribute {name!r} must reference the icon "
                    "itself (a '#id' fragment); icons may not point at external "
                    "or data: resources"
                )
    return None


def validate_icon(icon: str) -> str | None:
    """Return an error message when ``icon`` cannot be stored as an app icon.

    Returns None for usable markup. The markup is stored verbatim and later
    inlined into the workspace DOM, so this is the only gate: it accepts
    exactly one well-formed ``<svg>`` element containing nothing that executes
    (``<script>``, ``on*`` handlers, ``javascript:`` URLs), nothing that styles
    the host document (``<style>``), nothing that embeds foreign content
    (``<foreignObject>``), and no reference to anything outside the icon
    itself. Prologue syntax (``<?xml ...?>``, ``<!DOCTYPE ...>``, comments,
    CDATA) is rejected outright rather than tolerated, so what is stored is a
    single element and nothing else.
    """
    markup = icon.strip()
    if not markup:
        return "invalid icon: the icon is empty"
    if len(markup) > MAX_ICON_LENGTH:
        return (
            f"invalid icon: the markup is {len(markup)} characters, over the "
            f"{MAX_ICON_LENGTH}-character limit"
        )
    for character in markup:
        if character < " " and character not in _ALLOWED_CONTROL_CHARACTERS:
            return "invalid icon: the markup contains control characters"
    if "<?" in markup or "<!" in markup:
        return (
            "invalid icon: the markup must be a bare <svg> element with no XML "
            "declaration, doctype, comments, or CDATA"
        )
    try:
        root = ElementTree.fromstring(markup)
    except ElementTree.ParseError as error:
        return f"invalid icon: the markup is not well-formed XML ({error})"
    root_tag = root.tag if isinstance(root.tag, str) else ""
    if root_tag not in ("svg", f"{{{_SVG_NAMESPACE}}}svg"):
        return (
            f"invalid icon: the root element is <{_local_name(root_tag) or '?'}>, "
            "but an icon must be a single <svg> element"
        )
    return _validate_icon_element(root)


def read_icon_file(path: Path) -> tuple[str | None, str | None]:
    """Read and validate icon markup from ``path``, returning ``(markup, error_message)``.

    Only one of the two is ever set. The file's *contents* (validated,
    stripped) are what gets persisted; the path is not recorded anywhere.
    """
    if path.suffix.lower() != ".svg":
        return None, f"icon file {str(path)!r} must be an .svg file (icons are SVG-only so every glyph stays in the same vector style)"
    if not path.is_file():
        return None, f"icon file {str(path)!r} does not exist"
    try:
        markup = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return None, f"icon file {str(path)!r} could not be read as UTF-8 text: {error}"
    icon_error = validate_icon(markup)
    if icon_error is not None:
        return None, icon_error
    return markup.strip(), None


def mint_service_label(name: str) -> str:
    """Mint an unguessable ``<name>-<rand>`` origin label for a freshly-registered service."""
    suffix = "".join(
        secrets.choice(_LABEL_RANDOM_ALPHABET) for _ in range(_LABEL_RANDOM_LENGTH)
    )
    return f"{name}-{suffix}"


def validate_service_name(name: str) -> str | None:
    """Return an error message when ``name`` cannot be a service origin label.

    Returns None for a usable name. Applied to both upsert and remove so a
    bad name fails loudly instead of silently registering an unroutable (or
    unremovable) entry.
    """
    if not NAME_PATTERN.match(name):
        return (
            f"invalid app name {name!r}: names must be lowercase "
            "alphanumeric/underscore runs separated by single hyphens (no "
            "leading, trailing, or consecutive hyphens) because the name "
            "becomes the leading label of the service's origin hostname"
        )
    if len(name) > MAX_SERVICE_NAME_LENGTH:
        return (
            f"invalid app name {name!r}: names must be at most "
            f"{MAX_SERVICE_NAME_LENGTH} characters so the '<name>-<random>' "
            "origin label fits a 63-character DNS label"
        )
    for prefix in RESERVED_NAME_PREFIXES:
        if name.startswith(prefix):
            return (
                f"invalid app name {name!r}: the {prefix!r} prefix is "
                "reserved for the workspace coordinate in service origin "
                "hostnames"
            )
    if name in RESERVED_NAMES:
        return f"invalid app name {name!r}: this name is reserved"
    return None


def _apps_file() -> Path:
    """Path to the agent's apps.toml registry.

    Defaults to ``data/.state/apps.toml`` relative to cwd. Override
    via ``MINDS_APPS_FILE`` -- used by tests and by callers that
    need to point at a non-default registry (e.g. when running outside
    the agent's repo root). Mirrors ``system/scripts/layout.py``.
    """
    return Path(os.environ.get(ENV_APPS_FILE, DEFAULT_APPS_FILE))


def _toml_string(value: str) -> str:
    """``value`` as a TOML basic string: ``\\``, ``"``, and control characters escaped."""
    escaped: list[str] = []
    for character in value:
        short = _TOML_SHORT_ESCAPES.get(character)
        if short is not None:
            escaped.append(short)
        elif character < " " or character == "\x7f":
            escaped.append(f"\\u{ord(character):04X}")
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'


def _toml_inline_table(table: dict[str, object]) -> str:
    return "{" + ", ".join(f"{key} = {_toml_scalar(value)}" for key, value in table.items()) + "}"


def _toml_scalar(value: object) -> str:
    """A string or boolean as TOML; the registry's tables and arrays hold nothing else."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_string(value)
    raise TypeError(f"the registry cannot hold a value of type {type(value).__name__}: {value!r}")


def _toml_value(value: object) -> str:
    """A registry row value as TOML: a scalar, an inline table of scalars, or an array of such tables."""
    if isinstance(value, dict):
        return _toml_inline_table(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_inline_table(item) for item in value) + "]"
    return _toml_scalar(value)


def dump_registry(apps: list[dict[str, object]]) -> str:
    """Render the registry as ``[[apps]]`` tables, one key per line, in the order given.

    The output is what ``tomllib`` reads back byte-for-byte equal in every
    value (icons carry newlines and quotes; both survive the escaping). Keys are
    bare, which every registry key is.
    """
    chunks: list[str] = []
    for app in apps:
        lines = ["[[apps]]"]
        for key, value in app.items():
            lines.append(f"{key} = {_toml_value(value)}")
        chunks.append("\n".join(lines) + "\n")
    return "\n".join(chunks)


def _load_apps(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        doc = tomllib.load(f)
    apps = doc.get("apps", [])
    if not isinstance(apps, list):
        raise ValueError(f"registry {path} has an 'apps' key that is not an array of tables")
    return [dict(app) for app in apps]


def _save_apps(path: Path, apps: list[dict[str, object]]) -> None:
    # Atomic write: write to a temp file in the same directory, then os.replace()
    # into place. This guarantees that readers (like app-watcher) never observe
    # a truncated/partial file during the write window.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(dump_registry(apps))
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _read_manifest(path: Path, name_from_flag: str | None) -> tuple[dict[str, object], Path | None, str | None]:
    """Read ``path`` and return ``(row_fields, icon_path, error)``.

    ``row_fields`` holds every manifest-owned registry key the manifest sets
    (plus ``name``), with each value checked to be the type the registry
    stores. ``icon_path`` is the icon file resolved against the manifest's
    directory, or None for a manifest without one. Only one of ``row_fields``
    and ``error`` is meaningful.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        return {}, None, f"manifest {str(path)!r} could not be read: {error}"
    except tomllib.TOMLDecodeError as error:
        return {}, None, f"manifest {str(path)!r} is not valid TOML: {error}"

    name = raw.get("name")
    if not isinstance(name, str):
        return {}, None, f"manifest {str(path)!r} has no string 'name'"
    name_error = validate_service_name(name)
    if name_error is not None:
        return {}, None, f"manifest {str(path)!r}: {name_error}"
    if name_from_flag is not None and name_from_flag != name:
        return {}, None, f"--name {name_from_flag!r} does not match the manifest's name {name!r}"

    fields: dict[str, object] = {"name": name}
    for key in _MANIFEST_STRING_KEYS:
        if key in raw:
            if not isinstance(raw[key], str):
                return {}, None, f"manifest {str(path)!r}: {key} must be a string"
            fields[key] = raw[key]
    for key in _MANIFEST_BOOL_KEYS:
        if key in raw:
            if not isinstance(raw[key], bool):
                return {}, None, f"manifest {str(path)!r}: {key} must be a boolean"
            fields[key] = raw[key]
    if "program" not in fields:
        fields["program"] = name

    shortcut = raw.get("default_shortcut")
    if shortcut is not None:
        if not (isinstance(shortcut, dict) and isinstance(shortcut.get("action"), str) and isinstance(shortcut.get("mode"), str)):
            return {}, None, f"manifest {str(path)!r}: default_shortcut must be a table with string 'action' and 'mode'"
        fields["default_shortcut"] = {"action": shortcut["action"], "mode": shortcut["mode"]}

    actions = raw.get("actions")
    if actions is not None:
        if not isinstance(actions, list):
            return {}, None, f"manifest {str(path)!r}: actions must be an array of tables"
        copied_actions: list[dict[str, object]] = []
        for action in actions:
            if not (isinstance(action, dict) and isinstance(action.get("id"), str) and isinstance(action.get("label"), str)):
                return {}, None, f"manifest {str(path)!r}: every action needs a string 'id' and 'label'"
            copied_actions.append({"id": action["id"], "label": action["label"]})
        fields["actions"] = copied_actions

    icon = raw.get("icon")
    if icon is not None and not isinstance(icon, str):
        return {}, None, f"manifest {str(path)!r}: icon must be a string path"
    icon_path = path.parent / icon if icon is not None else None
    return fields, icon_path, None


def _upsert(
    path: Path,
    name: str,
    url: str,
    icon: str | None = None,
    internal: bool = False,
    program: str | None = None,
    manifest_fields: dict[str, object] | None = None,
) -> None:
    """Register ``name`` at ``url``, optionally setting its icon markup.

    ``icon`` is None when the caller said nothing about an icon, which leaves
    any icon already on the entry alone: a service that re-registers on every
    restart (the normal supervisord case) must not silently lose the icon it
    registered earlier, or the workspace would flip back to a generic glyph.

    ``internal`` has no such tri-state: it is a plain flag a service's own
    registration call either always passes or always omits, so every call is
    authoritative and simply sets it -- unlike the icon, there is no "leave it
    as it was" case to preserve.

    ``program`` names the supervisord program that runs this app, and its
    presence is the capability grant "this app can be stopped and started
    through supervisord". Like ``internal``, every call is authoritative:
    passing it sets the field and omitting it clears it, so a registration
    that stops passing it cannot leave a stale capability behind.

    ``manifest_fields`` (a ``--manifest`` registration) is authoritative for
    every manifest-owned key the same way: each is set to the manifest's value
    or removed when the manifest omits it.
    """
    apps = _load_apps(path)
    manifest_owned = _manifest_owned_values(manifest_fields, internal=internal, program=program)

    # Update an existing entry's URL in place, minting a label only if one was
    # never assigned (a legacy row, or a row written before labels existed).
    # The label is stable: re-registration must not change a service's origin.
    for app in apps:
        if app.get("name") == name:
            app["url"] = url
            if not app.get("label"):
                app["label"] = mint_service_label(name)
            if icon is not None:
                app["icon"] = icon
            for key in _MANIFEST_OWNED_KEYS:
                if key in manifest_owned:
                    app[key] = manifest_owned[key]
                elif key in app:
                    del app[key]
            _save_apps(path, apps)
            return

    # No existing entry -- append with a freshly-minted label. The ``icon``,
    # ``internal``, ``program``, and manifest keys are omitted entirely when
    # there is nothing to say, so the common row keeps the shape it has always
    # had (a missing key reads as "no icon" / "not internal" / "not supervised").
    entry: dict[str, object] = {"name": name, "url": url, "label": mint_service_label(name)}
    if icon is not None:
        entry["icon"] = icon
    for key in _MANIFEST_OWNED_KEYS:
        if key in manifest_owned:
            entry[key] = manifest_owned[key]
    apps.append(entry)
    _save_apps(path, apps)


def _manifest_owned_values(
    manifest_fields: dict[str, object] | None, internal: bool, program: str | None
) -> dict[str, object]:
    """The manifest-owned keys a registration sets, from the manifest or from the plain flags."""
    if manifest_fields is not None:
        values = {key: value for key, value in manifest_fields.items() if key in _MANIFEST_OWNED_KEYS}
        # ``internal`` keeps its flag shape on the row (present only when true).
        if not values.get("internal", False):
            values.pop("internal", None)
        return values
    values = {}
    if internal:
        values["internal"] = True
    if program is not None:
        values["program"] = program
    return values


def _has_entry(path: Path, name: str) -> bool:
    return any(app.get("name") == name for app in _load_apps(path))


def _remove(path: Path, name: str) -> None:
    if not path.exists():
        return
    apps = _load_apps(path)
    remaining = [app for app in apps if app.get("name") != name]
    if len(remaining) != len(apps):
        _save_apps(path, remaining)


def main() -> None:
    parser = argparse.ArgumentParser(description="Register or remove an app port")
    parser.add_argument(
        "--name", help="App name (e.g. 'terminal', 'browser'). Required without --manifest; must match the manifest's name with it."
    )
    parser.add_argument(
        "--manifest",
        help=(
            "Path to the app's app.toml. Its name, icon, and static fields (display_name, "
            "instances, instances_url, critical, priority, program, internal, default_shortcut, "
            "actions) are copied onto the row on every call."
        ),
    )
    parser.add_argument(
        "--url",
        help="Full URL where the app is accessible (e.g. http://localhost:7681)",
    )
    parser.add_argument("--icon-file", help="Path to the app's .svg icon; its contents are read now, validated, and stored (the path is not recorded). Omit to leave any stored icon untouched.")
    parser.add_argument("--no-icon", action="store_true", help="Register a brand-new entry without an icon, keeping the generic letter monogram. Does NOT hide the entry (that is --internal's job). Prefer --icon-file; use only when an icon was explicitly declined or would never be rendered.")
    parser.add_argument(
        "--program",
        help=(
            "Name of the supervisord program that runs this app. Its presence "
            "on the entry is the capability grant 'this app can be stopped and "
            "started through supervisord'. Authoritative per call: passing it "
            "sets the field, omitting it clears any previously-stored value. "
            "Never set it for unsupervised instances (previews, isolated "
            "test servers), which own their own teardown."
        ),
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove the named app instead of adding it",
    )
    parser.add_argument(
        "--internal",
        action="store_true",
        help=(
            "Register without offering this as an app to open: no row in the "
            "New Tab launcher's machine table, the rail's All apps popover, or "
            "its shortcuts. For machinery with a port to forward (share/embed "
            "routing) but no page of its own to show -- a name with nothing "
            "behind it would otherwise open blank."
        ),
    )
    args = parser.parse_args()

    if args.manifest is None and args.name is None:
        parser.error("--name is required without --manifest")

    if not args.remove and not args.url:
        parser.error("--url is required when not using --remove")

    if args.no_icon and args.icon_file is not None:
        parser.error("--no-icon is mutually exclusive with --icon-file")

    if args.remove and (args.icon_file is not None or args.no_icon):
        parser.error("--icon-file and --no-icon cannot be combined with --remove")

    if args.remove and args.program is not None:
        parser.error("--program cannot be combined with --remove")

    if args.manifest is not None and (
        args.remove or args.icon_file is not None or args.no_icon or args.program is not None or args.internal
    ):
        parser.error("--manifest cannot be combined with --remove, --icon-file, --no-icon, --program, or --internal")

    if args.program is not None and not args.program.strip():
        parser.error("--program must not be empty")

    manifest_fields: dict[str, object] | None = None
    icon_path: Path | None = Path(args.icon_file) if args.icon_file is not None else None
    if args.manifest is not None:
        manifest_fields, icon_path, manifest_error = _read_manifest(Path(args.manifest), args.name)
        if manifest_error is not None:
            parser.error(manifest_error)
        name = str(manifest_fields["name"])
    else:
        name = args.name

    name_error = validate_service_name(name)
    if name_error is not None:
        parser.error(name_error)

    icon: str | None = None
    icon_error: str | None = None
    if icon_path is not None:
        icon, icon_error = read_icon_file(icon_path)

    is_internal = args.internal or bool(manifest_fields is not None and manifest_fields.get("internal", False))

    apps_file = _apps_file()
    lock_path = apps_file.parent / ".apps.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if args.remove:
                _remove(apps_file, name)
            else:
                is_new_pickable = not is_internal and not _has_entry(apps_file, name)
                # A bad icon file fails a NEW registration (the author is present to
                # fix it) but must not brick an existing app's restart: warn and
                # register without it, keeping any already-stored icon (the UI
                # falls back to the letter monogram when none is stored).
                if icon_error is not None:
                    if is_new_pickable:
                        parser.error(icon_error)
                    sys.stderr.write(f"warning: {icon_error}; registering without an icon\n")
                if icon is None and not args.no_icon and is_new_pickable:
                    parser.error(
                        f"app {name!r} is new and has no icon: pass --icon-file with a house-style "
                        "SVG (see the build-app skill), name one in the manifest, or pass --no-icon "
                        "to keep the generic letter monogram"
                    )
                _upsert(
                    apps_file,
                    name,
                    args.url,
                    icon,
                    internal=args.internal,
                    program=args.program.strip() if args.program is not None else None,
                    manifest_fields=manifest_fields,
                )
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
