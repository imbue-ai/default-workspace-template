"""Seeing through the ``uv run`` runner that workspace instructions put in front of a program.

``parser`` knows only shell syntax; the callers that ask what a command runs
(the secrets guard, the chat's tk-lifecycle detection) unwrap this one runner
first, and share the set of uv options that leave the program unchanged so the
two never disagree. Stdlib-only, like ``parser``, for the same bare-``python3``
importers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

# uv options that change neither the program ``uv run`` starts nor what it sees.
# Any other option (``--env-file``, ``--with``, ...) stays in front of the program.
UV_RUN_PASSTHROUGH_FLAGS: Final[frozenset[str]] = frozenset(
    {"--no-sync", "--frozen", "--quiet"}
)


def unwrap_uv_run(words: Sequence[str]) -> tuple[str, ...] | None:
    """The words of the program a ``uv run`` command starts, or None if ``words`` is not one."""
    if tuple(words[:2]) != ("uv", "run"):
        return None
    index = 2
    while index < len(words) and words[index] in UV_RUN_PASSTHROUGH_FLAGS:
        index += 1
    return tuple(words[index:])
