"""When the user last had each of the machine's objects in front of them.

The launcher orders its tables most-recently-used first, and recency is a fact
about the **object**, machine-wide, not about the tab showing it. There is one
live page per object (see the frontend's ``liveSurfaces``), and a project is
only a view that may or may not show it, so a timestamp kept with a panel would
be a timestamp kept with one view's saved layout: the app used moments ago in
one project would read as untouched in another, and an object with no panel
anywhere -- backgrounded, still running, just not docked -- would have nowhere
to keep its recency at all. Keying the timestamp by ref instead makes both of
those go away, exactly as ``member_titles`` does for names.

Keys are the member refs the rest of the system files objects under
(``service:<name>``, ``service:browser?session=<name>``, ``chat:<agent-id>``,
``terminal:<name>``, ``url:<hash>``), which is why the ref validator is
borrowed from ``projects`` rather than restated here. The dependency runs one
way only: nothing in ``projects`` reaches back into this module, so the store a
timestamp lives in stays independent of the store a membership lives in.

Values are epoch milliseconds, as plain ints. The server's clock is the
authority: a timestamp a little ahead of it is clamped back to now (two clocks
can disagree by a little, and "used in the future" is not a thing the launcher
should ever be told), and one far ahead is rejected outright rather than
stored -- nothing on this machine can have been used then.

Storage mirrors ``member_titles``: a small JSON file
(``member_last_used.json``) beside ``member_titles.json`` under the workspace
layout dir, written under a module-level lock. The file is created on the first
touch; a workspace where nothing has been used simply has none.

An entry is dropped when the object behind it is destroyed (see
``clear_last_used``), so a ref handed out again later -- ``terminal:terminal-4``,
a name the allocator does reuse -- never inherits a dead object's recency.
"""

import json
import threading
import time
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

from imbue.imbue_common.pure import pure
from imbue.system_interface.projects import validated_member_ref

_LAST_USED_FILENAME: Final[str] = "member_last_used.json"

# How far ahead of this machine's clock a timestamp may run and still be read
# as clock skew rather than nonsense. Within it the value is clamped to now;
# past it the value is rejected, because no clock is a day wrong and a stored
# "used tomorrow" would pin the object to the top of the launcher for a day.
_MAX_FUTURE_SKEW_MS: Final[int] = 24 * 60 * 60 * 1000

# Serializes every read-modify-write of the last-used file across the threaded
# WSGI server, exactly as ``member_titles._titles_lock`` does for the names.
_last_used_lock = threading.Lock()


class MemberLastUsedTimestampError(ValueError):
    """Raised when a timestamp is not a moment this machine can have seen."""

    ...


def _now_ms() -> int:
    """The machine's clock, as epoch milliseconds."""
    return int(time.time() * 1000)


@pure
def _validated_timestamp_ms(at_ms: int, now_ms: int) -> int:
    """The moment to store: ``at_ms`` itself, or ``now_ms`` when it runs ahead.

    A timestamp a little ahead of the clock is clamped rather than rejected --
    two clocks disagreeing by a little is ordinary, and the answer the launcher
    needs is "just now" either way. One that is non-positive or absurdly far
    ahead is not a moment anything was used at, and raises.
    """
    if at_ms <= 0:
        raise MemberLastUsedTimestampError(f"Last-used timestamp {at_ms} is not after the epoch")
    if at_ms > now_ms + _MAX_FUTURE_SKEW_MS:
        raise MemberLastUsedTimestampError(
            f"Last-used timestamp {at_ms} is absurdly far ahead of the clock ({now_ms})"
        )
    return min(at_ms, now_ms)


def _last_used_path(layout_dir: Path) -> Path:
    return layout_dir / _LAST_USED_FILENAME


def _read_unlocked(layout_dir: Path) -> dict[str, int]:
    """The stored map, tolerating an absent, corrupt or hand-edited file.

    A file that cannot be read is reported as empty (logged at warning) rather
    than crashing the launcher: recency is an ordering hint, and every object
    still renders -- just with no known recency, as before anything was used.
    """
    last_used_path = _last_used_path(layout_dir)
    if not last_used_path.exists():
        return {}
    try:
        stored = json.loads(last_used_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _loguru_logger.opt(exception=e).warning("Failed to read {}; treating every object as unused", last_used_path)
        return {}
    last_used_ms_by_ref: object = stored.get("last_used_ms_by_ref") if isinstance(stored, dict) else None
    if not isinstance(last_used_ms_by_ref, dict):
        return {}
    return {
        ref: at_ms
        for ref, at_ms in last_used_ms_by_ref.items()
        if isinstance(ref, str) and isinstance(at_ms, int) and not isinstance(at_ms, bool) and at_ms > 0
    }


def _write_unlocked(layout_dir: Path, last_used_ms_by_ref: dict[str, int]) -> None:
    layout_dir.mkdir(parents=True, exist_ok=True)
    _last_used_path(layout_dir).write_text(json.dumps({"last_used_ms_by_ref": last_used_ms_by_ref}, indent=2))


def read_last_used(layout_dir: Path) -> dict[str, int]:
    """When each object was last in front of the user, keyed by ref.

    A ref that is absent has simply never been used (or was destroyed), which
    is the ordinary case: the caller renders it with no recency rather than
    inventing one.
    """
    with _last_used_lock:
        return _read_unlocked(layout_dir)


def touch_last_used(layout_dir: Path, ref: str, at_ms: int) -> int:
    """Record that one object is in front of the user, returning the moment kept.

    The ref is not checked against anything: an object can be used before it is
    filed in any project, and Everything shows objects no project holds. The
    moment kept is monotonic per ref -- a touch that lands out of order (two
    clients racing) never moves an object's recency backwards -- and a repeated
    touch writes nothing. Raises MemberLastUsedTimestampError for a timestamp
    this machine cannot have seen (see ``_validated_timestamp_ms``).
    """
    member_ref = validated_member_ref(ref)
    stamped_ms = _validated_timestamp_ms(at_ms, _now_ms())
    with _last_used_lock:
        last_used_ms_by_ref = _read_unlocked(layout_dir)
        stored_ms = last_used_ms_by_ref.get(member_ref)
        if stored_ms is not None and stored_ms >= stamped_ms:
            return stored_ms
        last_used_ms_by_ref[member_ref] = stamped_ms
        _write_unlocked(layout_dir, last_used_ms_by_ref)
        return stamped_ms


def clear_last_used(layout_dir: Path, ref: str) -> bool:
    """Drop the recency of a destroyed object, reporting whether it had any.

    Destroy is what this exists for. The refs are reused -- the terminal
    allocator hands out the lowest free ``terminal-<N>`` again once a session is
    gone -- so a timestamp left behind would rank whatever object next answers
    to that ref as recently used the moment it appears. An object that was never
    used returns False and writes nothing.
    """
    member_ref = validated_member_ref(ref)
    with _last_used_lock:
        last_used_ms_by_ref = _read_unlocked(layout_dir)
        if member_ref not in last_used_ms_by_ref:
            return False
        del last_used_ms_by_ref[member_ref]
        _write_unlocked(layout_dir, last_used_ms_by_ref)
        return True
