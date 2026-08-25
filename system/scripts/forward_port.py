#!/usr/bin/env python3
"""Register or remove an app port in data/.state/apps.toml.

Uses file locking to safely upsert or remove entries. Called by services
on startup to declare the ports they expose.

Usage:
    python3 system/scripts/forward_port.py --icon-file system/apps/foo/icon.svg --name foo --url http://localhost:8090
    python3 system/scripts/forward_port.py --no-icon --name terminal --url http://localhost:7681
    python3 system/scripts/forward_port.py --remove --name terminal

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
``--icon-file`` is only an input convenience: the file is read once here, at
registration time, and its contents (not its path) are what gets persisted.

The markup is validated before it is stored, because it is eventually inlined
into the workspace DOM. ``validate_icon`` rejects anything that is not exactly
one well-formed ``<svg>`` element and anything that could execute or reach off
the page (see its docstring). It also caps the length at
``MAX_ICON_LENGTH`` so one app cannot bloat the registry that every consumer
re-reads on every change.

An icon is REQUIRED when a registration would create a new entry (unless the
entry is ``--internal``, which never renders anywhere): without one the
workspace can only draw a generic letter-in-a-box monogram, and a wall of
those makes apps indistinguishable. The requirement bites at first
registration -- exactly the moment the authoring agent is present and can
draw a proper glyph (see the build-app skill for the house style). It does
NOT apply to entries that already exist, so pre-existing apps registered
before the requirement keep restarting (and keep their monogram) untouched.
``--no-icon`` opts out explicitly, for machinery whose entry is hidden from
the app pickers (previews, wrappers) and keeps the generic glyph on purpose.
"""

import argparse
import fcntl
import os
import re
import secrets
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import tomlkit

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
# keeps that rule a subset of this one).
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
    """Read icon markup from ``path``, returning ``(markup, error_message)``.

    Only one of the two is ever set. The file's *contents* are what gets
    persisted; the path is not recorded anywhere.

    Only ``.svg`` files are accepted. The contents are validated as SVG markup
    anyway, but checking the extension up front turns "I passed a PNG" into a
    clear error naming the one supported format instead of an XML parse
    failure -- icons are vector-only so every app's glyph stays in the same
    house style.
    """
    if path.suffix.lower() != ".svg":
        return None, (
            f"icon file {str(path)!r} must be an .svg file: app icons are "
            "stored as SVG markup so every glyph stays in the same vector "
            "house style (PNG, JPEG, and other raster formats are not "
            "supported)"
        )
    if not path.is_file():
        return None, f"icon file {str(path)!r} does not exist"
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as error:
        return None, f"icon file {str(path)!r} could not be read as UTF-8 text: {error}"


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


def _load_apps(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        doc = tomlkit.document()
        doc.add("apps", tomlkit.aot())
        return doc
    with open(path, "rb") as f:
        return tomlkit.load(f)


def _save_apps(path: Path, doc: tomlkit.TOMLDocument) -> None:
    # Atomic write: write to a temp file in the same directory, then os.replace()
    # into place. This guarantees that readers (like app-watcher) never observe
    # a truncated/partial file during the write window.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            tomlkit.dump(doc, f)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _upsert(
    path: Path,
    name: str,
    url: str,
    icon: str | None = None,
    internal: bool = False,
    program: str | None = None,
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
    """
    doc = _load_apps(path)
    apps = doc.get("apps", [])

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
            if internal:
                app["internal"] = True
            elif "internal" in app:
                del app["internal"]
            if program is not None:
                app["program"] = program
            elif "program" in app:
                del app["program"]
            _save_apps(path, doc)
            return

    # No existing entry -- append with a freshly-minted label. The ``icon``,
    # ``internal``, and ``program`` keys are omitted entirely when there is
    # nothing to say, so the common row keeps the shape it has always had (a
    # missing key reads as "no icon" / "not internal" / "not supervised").
    entry = tomlkit.table()
    entry.add("name", name)
    entry.add("url", url)
    entry.add("label", mint_service_label(name))
    if icon is not None:
        entry.add("icon", icon)
    if internal:
        entry.add("internal", True)
    if program is not None:
        entry.add("program", program)
    apps.append(entry)
    _save_apps(path, doc)


def _has_entry(path: Path, name: str) -> bool:
    """Whether the registry already holds an entry for ``name``."""
    doc = _load_apps(path)
    return any(app.get("name") == name for app in doc.get("apps", []))


def missing_icon_error(name: str) -> str:
    """The error for registering a brand-new pickable app without an icon."""
    return (
        f"app {name!r} is not registered yet and no icon was given: a new app "
        "must register an icon, or the workspace can only draw a generic "
        "letter-in-a-box glyph for it. Draw one in the house style -- a single "
        '<svg viewBox="0 0 24 24"> of monochrome line art, '
        "stroke='currentColor', fill='none' (see the build-app skill) -- and "
        "pass it via --icon or --icon-file. Pass --no-icon only for machinery "
        "that is hidden from the app pickers and keeps the generic glyph on "
        "purpose"
    )


def _remove(path: Path, name: str) -> None:
    if not path.exists():
        return
    doc = _load_apps(path)
    apps = doc.get("apps", [])
    original_len = len(apps)

    # Remove matching entries
    to_remove = [i for i, app in enumerate(apps) if app.get("name") == name]
    for i in reversed(to_remove):
        del apps[i]

    if len(apps) != original_len:
        _save_apps(path, doc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Register or remove an app port")
    parser.add_argument(
        "--name", required=True, help="App name (e.g. 'terminal', 'browser')"
    )
    parser.add_argument(
        "--url",
        help="Full URL where the app is accessible (e.g. http://localhost:7681)",
    )
    parser.add_argument(
        "--icon",
        help=(
            "SVG markup for the app's icon, stored verbatim on the entry. Must "
            "be a single <svg> element. House style: monochrome line art -- "
            "stroke='currentColor', fill='none', transparent background -- like "
            "the workspace's built-in glyphs. Required (or --icon-file, or an "
            "explicit --no-icon) when the registration would create a new "
            "non-internal entry; omit on re-registration to leave the stored "
            "icon untouched."
        ),
    )
    parser.add_argument(
        "--icon-file",
        help=(
            "Path to an .svg file holding the SVG markup for the app's icon "
            "(SVG only -- no raster formats). The file is read now and its "
            "contents are stored; the path is not recorded. Mutually "
            "exclusive with --icon."
        ),
    )
    parser.add_argument(
        "--no-icon",
        action="store_true",
        help=(
            "Register a brand-new entry without an icon, on purpose. Only for "
            "machinery whose entry is hidden from the app pickers (previews, "
            "wrappers, the built-in services with their own UI glyphs); a "
            "user-facing app must pass --icon or --icon-file instead. Like "
            "omitting the icon flags, this never touches an icon already "
            "stored on the entry."
        ),
    )
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

    if not args.remove and not args.url:
        parser.error("--url is required when not using --remove")

    if args.icon is not None and args.icon_file is not None:
        parser.error("--icon and --icon-file are mutually exclusive")

    if args.no_icon and (args.icon is not None or args.icon_file is not None):
        parser.error("--no-icon is mutually exclusive with --icon and --icon-file")

    if args.remove and (args.icon is not None or args.icon_file is not None or args.no_icon):
        parser.error("--icon, --icon-file, and --no-icon cannot be combined with --remove")

    if args.remove and args.program is not None:
        parser.error("--program cannot be combined with --remove")

    if args.program is not None and not args.program.strip():
        parser.error("--program must not be empty")

    name_error = validate_service_name(args.name)
    if name_error is not None:
        parser.error(name_error)

    icon: str | None = args.icon
    if args.icon_file is not None:
        icon, read_error = read_icon_file(Path(args.icon_file))
        if read_error is not None:
            parser.error(read_error)
    if icon is not None:
        icon_error = validate_icon(icon)
        if icon_error is not None:
            parser.error(icon_error)
        icon = icon.strip()

    apps_file = _apps_file()
    lock_path = apps_file.parent / ".apps.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if args.remove:
                _remove(apps_file, args.name)
            else:
                # Checked under the lock so the existence answer cannot go
                # stale between the check and the upsert.
                is_new_pickable_entry = (
                    not args.internal and not _has_entry(apps_file, args.name)
                )
                if icon is None and not args.no_icon and is_new_pickable_entry:
                    parser.error(missing_icon_error(args.name))
                _upsert(
                    apps_file,
                    args.name,
                    args.url,
                    icon,
                    internal=args.internal,
                    program=args.program.strip() if args.program is not None else None,
                )
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
