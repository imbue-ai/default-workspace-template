"""Browser names (spec section 16.3): the addressing key of the fleet CLI's ``<name>`` argument, the fleet API's
paths, the manifest's ids and the profile directory's suffix, so a valid name is safe as a URL path segment, a
query value and a filesystem path component. A minted name is the first free ``browser-<N>``, the canonical form
of the "Browser N" the workspace shows, and closing a browser frees its number."""

import re
from typing import Final

from imbue.imbue_common.pure import pure

# Lowercase alphanumeric words joined by single dashes, 1 to 40 characters, no leading, trailing or double dash.
# Pure-numeric names are rejected so an upgraded workspace's old numeric profile directories never resurrect as
# named browsers.
NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME_LENGTH: Final[int] = 40
NUMBERED_NAME_STEM: Final[str] = "browser"


@pure
def is_valid_browser_name(name: str) -> bool:
    """Whether a user-typed name is a safe browser id."""
    if not name or len(name) > MAX_NAME_LENGTH:
        return False
    if name.isdigit():
        return False
    return NAME_PATTERN.fullmatch(name) is not None


@pure
def first_free_numbered_browser_name(taken_names: set[str]) -> str:
    """The first free ``browser-<N>`` (N counting from 1), filling the gap a closed browser left.

    ``taken_names`` is every name the caller must not collide with: the live registry, the manifest's saved
    browsers and the on-disk profile directories.
    """
    number = 1
    while f"{NUMBERED_NAME_STEM}-{number}" in taken_names:
        number += 1
    return f"{NUMBERED_NAME_STEM}-{number}"
