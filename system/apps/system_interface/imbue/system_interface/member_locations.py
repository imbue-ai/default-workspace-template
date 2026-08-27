"""Where each of the machine's objects was last looking, keyed by ref.

An app instance that beacons its location (the vendored file viewer posts the
folder it is showing on each page load) should reopen there -- across view
switches, full page reloads, and workspace restarts. The location is a fact
about the **object**, machine-wide, not about the tab showing it, exactly as a
name (``member_titles``) and a recency (``member_last_used``) are: a location
kept with a panel would live in one view's saved layout, and an instance with
no panel anywhere would have nowhere to keep one. This file mirrors those two
stores -- a small JSON file (``member_locations.json``) beside them under the
workspace layout dir, written under a module-level lock, created on the first
beacon.

Values are the path component an instance's page should reopen at, appended to
the service's origin: always ``/``-rooted, query and fragment included as the
app posted them. The shell is the only writer -- it validates the beacon's
origin and resolves the posting pane before anything lands here -- so this end
only checks shape (rooted, within a sane length).

An entry is dropped when the object behind it is destroyed (see
``clear_location``): instance names are handed out again -- the allocator
reuses the lowest free ``files-<N>`` -- so a location left behind would aim
whatever instance next answers to that ref at a folder it never visited.
"""

import json
import threading
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

from imbue.imbue_common.pure import pure
from imbue.system_interface.projects import validated_member_ref

_LOCATIONS_FILENAME: Final[str] = "member_locations.json"

# Longest stored path. A location is one path-plus-query, so anything past
# this is not a folder the viewer was showing but junk (or an attack) riding
# the beacon; rejected rather than truncated, since half a path is a
# different place.
MAX_MEMBER_LOCATION_LENGTH: Final[int] = 2048

# Serializes every read-modify-write of the locations file across the
# threaded WSGI server, exactly as ``member_titles._titles_lock`` does.
_locations_lock = threading.Lock()


class MemberLocationError(ValueError):
    """Raised when a beaconed location is not a usable in-origin path."""

    ...


@pure
def validated_location(path: str) -> str | None:
    """Whitespace-trim a beaconed path, or None when it names nowhere.

    None is a real answer rather than a rejection: clearing a stored location
    is spelled by submitting an empty one. A path that is not ``/``-rooted
    could escape the service origin it is appended to (``//host`` is a
    protocol-relative URL, ``foo`` a relative one), and one over the cap is
    junk; both raise.
    """
    trimmed = path.strip()
    if not trimmed:
        return None
    if not trimmed.startswith("/") or trimmed.startswith("//"):
        raise MemberLocationError(f"Location {trimmed[:80]!r} is not a single-slash-rooted path")
    if len(trimmed) > MAX_MEMBER_LOCATION_LENGTH:
        raise MemberLocationError(
            f"Location is {len(trimmed)} characters, longer than the {MAX_MEMBER_LOCATION_LENGTH}-character limit"
        )
    return trimmed


def _locations_path(layout_dir: Path) -> Path:
    return layout_dir / _LOCATIONS_FILENAME


def _read_unlocked(layout_dir: Path) -> dict[str, str]:
    """The stored map, tolerating an absent, corrupt or hand-edited file.

    A file that cannot be read is reported as empty (logged at warning) rather
    than crashing the shell: a location is an opening hint, and every instance
    still opens at its service origin as before anything was stored.
    """
    locations_path = _locations_path(layout_dir)
    if not locations_path.exists():
        return {}
    try:
        stored = json.loads(locations_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _loguru_logger.opt(exception=e).warning(
            "Failed to read {}; treating every object as unlocated", locations_path
        )
        return {}
    location_by_ref: object = stored.get("location_by_ref") if isinstance(stored, dict) else None
    if not isinstance(location_by_ref, dict):
        return {}
    return {ref: path for ref, path in location_by_ref.items() if isinstance(ref, str) and isinstance(path, str)}


def _write_unlocked(layout_dir: Path, location_by_ref: dict[str, str]) -> None:
    layout_dir.mkdir(parents=True, exist_ok=True)
    _locations_path(layout_dir).write_text(json.dumps({"location_by_ref": location_by_ref}, indent=2))


def read_locations(layout_dir: Path) -> dict[str, str]:
    """Where each object was last looking, keyed by ref.

    A ref that is absent has simply never beaconed (or was destroyed), which
    is the ordinary case: the caller opens the instance at its service origin.
    """
    with _locations_lock:
        return _read_unlocked(layout_dir)


def set_location(layout_dir: Path, ref: str, path: str) -> str | None:
    """Record where one object is looking, returning the path kept -- or None
    when it was cleared.

    A path that is empty once trimmed clears the entry instead of storing a
    blank. The ref is not checked against anything beyond being non-blank: a
    beacon can land before the instance is filed in any project. Raises
    MemberLocationError for a path that is not a rooted, cap-sized one.
    """
    member_ref = validated_member_ref(ref)
    stored_path = validated_location(path)
    with _locations_lock:
        location_by_ref = _read_unlocked(layout_dir)
        if stored_path is None:
            if member_ref not in location_by_ref:
                return None
            del location_by_ref[member_ref]
        elif location_by_ref.get(member_ref) == stored_path:
            return stored_path
        else:
            location_by_ref[member_ref] = stored_path
        _write_unlocked(layout_dir, location_by_ref)
        return stored_path


def clear_location(layout_dir: Path, ref: str) -> bool:
    """Drop the location of a destroyed object, reporting whether it had one.

    Destroy is what this exists for: instance names are reused, so a location
    left behind would land on whatever object next answers to that ref. An
    object that never beaconed returns False and writes nothing.
    """
    member_ref = validated_member_ref(ref)
    with _locations_lock:
        location_by_ref = _read_unlocked(layout_dir)
        if member_ref not in location_by_ref:
            return False
        del location_by_ref[member_ref]
        _write_unlocked(layout_dir, location_by_ref)
        return True
