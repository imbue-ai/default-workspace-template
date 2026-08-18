"""The names the user has given the machine's objects.

Renaming names the **object**, machine-wide, not the tab showing it. There is
one live page per object (see the frontend's ``liveSurfaces``), and a project is
only a view that may or may not show it, so a name kept with a panel would be a
name kept with one view's saved layout: the app renamed "Docs" in one project
would still read "docs-viewer" in another, and an object with no panel anywhere
-- backgrounded, still running, just not docked -- would have nowhere to keep a
name at all. Keying the name by ref instead makes both of those go away.

Keys are the member refs the rest of the system files objects under
(``service:<name>``, ``service:browser?session=<name>``, ``chat:<agent-id>``,
``terminal:<name>``, ``url:<hash>``), which is why the ref validator is borrowed
from ``projects`` rather than restated here. The dependency runs one way only:
nothing in ``projects`` reaches back into this module, so the store a title
lives in stays independent of the store a membership lives in.

That independence is also why this is its own file rather than another key in
``projects_meta.json``: a title belongs to the machine and a member list belongs
to a project, and folding one into the other would put a machine-wide fact
inside a per-project entry -- the very shape this replaces. Storage mirrors that
registry otherwise: a small JSON file (``member_titles.json``) beside it under
the workspace layout dir, written under a module-level lock. The file is created
on the first rename; a workspace where nothing has been renamed simply has none.

A name is dropped when the object behind it is destroyed (see
``clear_title``), so a ref handed out again later -- ``terminal:terminal-4``, a
name the allocator does reuse -- never inherits a dead one.
"""

import json
import threading
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

from imbue.imbue_common.pure import pure
from imbue.system_interface.projects import validated_member_ref

_TITLES_FILENAME: Final[str] = "member_titles.json"

# Longest name kept. Duplicated from the frontend's ``MAX_TAB_TITLE_LENGTH``
# (``frontend/src/views/tab-rename.ts``), which is where a typed name is capped
# before it is ever sent -- nothing here can import a TypeScript constant, so
# the two have to be kept in step by hand. This end rejects rather than
# truncates: a name arriving over the cap means the two disagree, and silently
# keeping half of it would hide that.
MAX_MEMBER_TITLE_LENGTH: Final[int] = 120

# Serializes every read-modify-write of the titles file across the threaded
# WSGI server, exactly as ``projects._projects_lock`` does for the registry.
_titles_lock = threading.Lock()


class MemberTitleLengthError(ValueError):
    """Raised when a chosen name is longer than the tab strip's cap."""

    ...


@pure
def validated_title(title: str) -> str | None:
    """Whitespace-trim a chosen name, or None when it names nothing.

    None is a real answer rather than a rejection: clearing a name is spelled
    by submitting an empty one, which is what an editor emptied and committed
    hands over. An over-long name is a disagreement with the frontend's own cap
    and raises instead.

    Public because a chat's name has to reach mngr *before* it is stored here
    (see the title endpoint), and the name that goes to mngr must be the exact
    one this would keep -- deriving it separately is how the two drift apart.
    """
    trimmed = title.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_MEMBER_TITLE_LENGTH:
        raise MemberTitleLengthError(
            f"Title is {len(trimmed)} characters, longer than the {MAX_MEMBER_TITLE_LENGTH}-character limit"
        )
    return trimmed


def _titles_path(layout_dir: Path) -> Path:
    return layout_dir / _TITLES_FILENAME


def _read_unlocked(layout_dir: Path) -> dict[str, str]:
    """The stored map, tolerating an absent, corrupt or hand-edited file.

    A file that cannot be read is reported as empty (logged at warning) rather
    than crashing every rename endpoint: a title is a display name, and every
    object still has the name it was called before anyone renamed it.
    """
    titles_path = _titles_path(layout_dir)
    if not titles_path.exists():
        return {}
    try:
        stored = json.loads(titles_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _loguru_logger.opt(exception=e).warning("Failed to read {}; treating every object as unnamed", titles_path)
        return {}
    title_by_ref: object = stored.get("title_by_ref") if isinstance(stored, dict) else None
    if not isinstance(title_by_ref, dict):
        return {}
    return {ref: title for ref, title in title_by_ref.items() if isinstance(ref, str) and isinstance(title, str)}


def _write_unlocked(layout_dir: Path, title_by_ref: dict[str, str]) -> None:
    layout_dir.mkdir(parents=True, exist_ok=True)
    _titles_path(layout_dir).write_text(json.dumps({"title_by_ref": title_by_ref}, indent=2))


def read_titles(layout_dir: Path) -> dict[str, str]:
    """Every name the user has given an object, keyed by ref.

    A ref that is absent is simply unnamed, which is the ordinary case: the
    caller falls back to whatever the object calls itself.
    """
    with _titles_lock:
        return _read_unlocked(layout_dir)


def set_title(layout_dir: Path, ref: str, title: str) -> str | None:
    """Name one object, returning the name kept -- or None when it was cleared.

    A name that is empty once trimmed clears the entry instead of storing a
    blank, so there is no such thing as an object named "". The ref is not
    checked against anything: an object can be named before it is filed in any
    project, and Everything shows objects no project holds.
    Raises MemberTitleLengthError for a name over the cap.
    """
    member_ref = validated_member_ref(ref)
    chosen_title = validated_title(title)
    with _titles_lock:
        title_by_ref = _read_unlocked(layout_dir)
        if chosen_title is None:
            if member_ref not in title_by_ref:
                return None
            del title_by_ref[member_ref]
        elif title_by_ref.get(member_ref) == chosen_title:
            return chosen_title
        else:
            title_by_ref[member_ref] = chosen_title
        _write_unlocked(layout_dir, title_by_ref)
        return chosen_title


def clear_title(layout_dir: Path, ref: str) -> bool:
    """Drop the name of a destroyed object, reporting whether it had one.

    Destroy is what this exists for. The refs are reused -- the terminal
    allocator hands out the lowest free ``terminal-<N>`` again once a session is
    gone -- so a name left behind would land on whatever object next answers to
    that ref. An object that was never renamed returns False and writes nothing.
    """
    member_ref = validated_member_ref(ref)
    with _titles_lock:
        title_by_ref = _read_unlocked(layout_dir)
        if member_ref not in title_by_ref:
            return False
        del title_by_ref[member_ref]
        _write_unlocked(layout_dir, title_by_ref)
        return True
