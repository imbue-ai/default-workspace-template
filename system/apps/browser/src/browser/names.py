"""Numbered browser names + server-side name validation.

Browsers are addressed by NAME. The name is the addressing key everywhere: the
CLI ``<name>`` arg, the address ``app:browser?instance=<name>``, the cast WS path
``/browsers/<name>/cast``, the manifest ``id``, and the persistent profile dir
``browser-use-user-data-dir-<name>``. :func:`is_valid_browser_name` therefore
guarantees a name is safe as a URL path segment, a query value, and a filesystem
path component.

A daemon-minted name is the first free ``browser-<N>`` -- the canonical form of
the human-readable "Browser N" the workspace UI shows for it, mirroring how
chats pair "Chat 2" with the agent name ``Chat-2``. "First free" fills gaps:
closing "Browser 1" deletes its profile and frees the slot for the next create
(see ``session.py``, whose taken set also spans the manifest and the on-disk
profiles so a pending restore or an orphaned profile can never be collided
with). Browsers created under older builds keep their random english names --
every name stays valid, only the minting changed.
"""

import re

# Lowercase alnum words joined by single dashes, 1..40 chars, no leading/trailing/
# double dash. This keeps a name safe as a URL path segment, a query value, and the
# ``browser-use-user-data-dir-<name>`` profile-dir suffix. Pure-numeric names (e.g.
# "0") are intentionally rejected: see is_valid_browser_name.
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_NAME_LEN = 40

# The stem daemon-minted names are numbered under. ``browser-<N>`` is the
# canonical form of the "Browser N" display name the workspace UI derives from
# it (see the system interface's ``derivedLabelForMemberRef``).
NUMBERED_NAME_STEM = "browser"


def first_free_numbered_browser_name(taken_names: set[str]) -> str:
    """The first free ``browser-<N>`` name (N counts from 1).

    ``taken_names`` is every name the caller must not collide with -- the live
    registry, the manifest's pending-restore entries, and the on-disk profile
    dirs. Uniqueness within the live fleet is the manager's responsibility (it
    allocates under its create lock); this just picks the number.
    """
    n = 1
    while f"{NUMBERED_NAME_STEM}-{n}" in taken_names:
        n += 1
    return f"{NUMBERED_NAME_STEM}-{n}"


def is_valid_browser_name(name: str) -> bool:
    """Whether a user-typed name is a safe browser id.

    Lowercase alnum words joined by single dashes, 1..40 chars, no leading/trailing/
    double dash. This is the server-side validation for a user-supplied name; it
    guarantees the name is safe as a URL path segment, a query value, and the
    ``browser-use-user-data-dir-<name>`` profile-dir suffix (no slashes, no dots, no
    ``.``/``..``). Pure-numeric names are rejected so an upgraded workspace's old
    numeric profile dirs ("0"/"1"/"2") never resurrect as named browsers.
    """
    if not name or len(name) > _MAX_NAME_LEN:
        return False
    if name.isdigit():
        return False
    return NAME_RE.fullmatch(name) is not None
