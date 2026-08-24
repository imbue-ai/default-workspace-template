"""App instances, derived from what references them.

A plain registered app is multi-instance the way terminals and browsers are:
each open pane is a numbered, named, filed object (``files-1``, ``files-2``)
rather than an anonymous view of one service. Unlike those kinds, an instance
has no backing state of its own -- no tmux session, no profile directory -- so
its existence is **derived**: an instance exists while any project's member
list or any view's saved layout references it, and ceases to exist when
nothing does. There is deliberately no instance registry file; removing the
last reference IS deletion.

Refs ride the browser fleet's existing ``service:<name>?<query>`` grammar,
with the FULL canonical instance name in the query:
``service:files?instance=files-2``. The canonical name is minted here, by the
same lowest-free-number rule the terminal allocator uses, machine-wide, under
a lock plus an in-flight reservation set (allocation and the first reference
land asynchronously, so two rapid mints would otherwise both see the same
free number).
"""

import json
import re
import threading
import urllib.parse
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

from imbue.imbue_common.pure import pure
from imbue.system_interface import projects

# Query parameter carrying an app pane's instance name in its member ref,
# mirroring the browser fleet's ``session`` key. The value is the FULL
# canonical instance name (``files-2``), not the bare number, so a later
# rename scheme can address instances without re-deriving names.
INSTANCE_QUERY_KEY: Final[str] = "instance"

_SERVICE_REF_PREFIX: Final[str] = "service:"

# ``<service-name>-<N>``: the canonical instance name is always the full
# registered service name plus a dash and a 1-based number, so stripping the
# final ``-<digits>`` group always recovers the service name -- even for a
# service whose own name ends in digits.
_INSTANCE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?P<service>.+)-(?P<number>[1-9][0-9]*)$")

# Serializes instance-name allocation and tracks names handed out but not yet
# referenced by any member list or saved layout. Mirrors the module-level
# ``_terminal_allocate_lock`` convention in ``server.py``, keyed by layout dir
# so nothing bleeds between workspaces (or between tests' sandboxes).
_instance_allocate_lock = threading.Lock()
_recently_allocated_names_by_layout_dir: dict[Path, set[str]] = {}


@pure
def instance_ref(service_name: str, instance_name: str) -> str:
    """The member ref one app instance is filed under.

    Built without URL-encoding on purpose: the allocator is the only writer,
    and a canonical name is a registered service name (a DNS label) plus
    ``-<N>``, which carries nothing the query-decoding parsers would
    transform.
    """
    return f"{_SERVICE_REF_PREFIX}{service_name}?{INSTANCE_QUERY_KEY}={instance_name}"


@pure
def parse_instance_ref(ref: str) -> tuple[str, str] | None:
    """``(service_name, instance_name)`` for an instance ref, else None.

    A bare ``service:<name>`` ref (an app's pin) and the browser fleet's
    ``?session=`` refs both answer None: neither names an instance.
    """
    if not ref.startswith(_SERVICE_REF_PREFIX):
        return None
    body = ref[len(_SERVICE_REF_PREFIX) :]
    name, separator, query = body.partition("?")
    if not separator or not name:
        return None
    instance_values = urllib.parse.parse_qs(query).get(INSTANCE_QUERY_KEY, [])
    if not instance_values or not instance_values[0]:
        return None
    return name, instance_values[0]


@pure
def parse_instance_name(instance_name: str) -> tuple[str, int] | None:
    """``(service_name, number)`` for a canonical ``<service>-<N>`` name, else None."""
    match = _INSTANCE_NAME_PATTERN.match(instance_name)
    if match is None:
        return None
    return match.group("service"), int(match.group("number"))


def _instance_refs_in_saved_layouts(layout_dir: Path) -> set[str]:
    """Every instance ref any view's saved arrangement references.

    Reads the content files straight off disk (they are written atomically, so
    a read sees old or new content in full) and resolves each saved panel to
    its member ref through the one shared grammar
    (``projects.member_refs_from_content``). Unreadable files are skipped with
    a warning rather than hiding every other view's references.
    """
    projects_dir = layout_dir / "projects"
    if not projects_dir.is_dir():
        return set()
    refs: set[str] = set()
    for content_path in projects_dir.iterdir():
        if not content_path.is_file() or not content_path.name.endswith(".json"):
            continue
        try:
            content = json.loads(content_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _loguru_logger.opt(exception=e).warning("Skipped unreadable view content at {}", content_path)
            continue
        if not isinstance(content, dict):
            continue
        for ref in projects.member_refs_from_content(content):
            if parse_instance_ref(ref) is not None:
                refs.add(ref)
    return refs


def _referenced_instance_refs(layout_dir: Path) -> set[str]:
    """Every instance ref any project's member list or any view's layout holds."""
    refs = _instance_refs_in_saved_layouts(layout_dir)
    for info in projects.list_projects(layout_dir):
        for ref in info.members:
            if parse_instance_ref(ref) is not None:
                refs.add(ref)
    return refs


def list_app_instances(layout_dir: Path) -> dict[str, list[str]]:
    """Every app instance the machine holds, by service name.

    An instance exists while something references it, so this is exactly the
    union of every project's member list and every view's saved layout.
    Instance names are ordered by their number; a name that does not parse as
    ``<service>-<N>`` (a hand-edited ref) is kept, ordered last, rather than
    hidden -- it is still a referenced object with panes to sweep.
    """
    names_by_service: dict[str, list[str]] = {}
    for ref in _referenced_instance_refs(layout_dir):
        parsed = parse_instance_ref(ref)
        if parsed is None:
            continue
        service_name, instance_name = parsed
        names_by_service.setdefault(service_name, []).append(instance_name)
    for service_name, names in names_by_service.items():
        names.sort(key=lambda name: (parse_instance_name(name) is None, (parse_instance_name(name) or ("", 0))[1]))
    return names_by_service


def allocate_app_instance(layout_dir: Path, service_name: str) -> str:
    """Mint the lowest free ``<service>-<N>`` instance name, machine-wide.

    The lock plus the in-memory reservation set make consecutive allocations
    return distinct names even before the first reference to the new instance
    (a member entry, a saved pane) has landed -- allocation is answered
    synchronously while filing is asynchronous, so two rapid mints would
    otherwise both see the same free number. Reservations are dropped once
    they show up as real references, so the set cannot grow without bound.
    """
    with _instance_allocate_lock:
        used_names = {
            instance_name
            for names in list_app_instances(layout_dir).values()
            for instance_name in names
        }
        reserved_names = _recently_allocated_names_by_layout_dir.setdefault(layout_dir.resolve(), set())
        # Drop reservations that have since become real references so the set
        # cannot grow without bound.
        reserved_names.difference_update(used_names)
        taken = used_names | reserved_names
        index = 1
        while f"{service_name}-{index}" in taken:
            index += 1
        instance_name = f"{service_name}-{index}"
        reserved_names.add(instance_name)
        return instance_name


def release_app_instance(layout_dir: Path, instance_name: str) -> None:
    """Drop one minted name's in-flight reservation.

    The deletion path calls this (mirroring how destroying a terminal discards
    its allocator reservation): a deleted instance's number must free up
    without waiting for a later allocation to notice, and a mint whose open
    was abandoned mid-flight would otherwise hold its number forever.
    """
    with _instance_allocate_lock:
        _recently_allocated_names_by_layout_dir.get(layout_dir.resolve(), set()).discard(instance_name)
