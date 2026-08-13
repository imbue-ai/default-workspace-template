#!/usr/bin/env python3
"""Topic declaration edits -- the owner of the topic `.toml` file format.

Right now this owns one edit: setting a topic's lifecycle **status** (the
human-set field, never inferred). It lives with the rest of the skill's
file-format logic so callers -- the viewer app, future tools -- share one correct
implementation of the toml rewrite instead of each reimplementing it.

Usage (library):
    import atlas_topic
    atlas_topic.set_status(repo_root, slug, "active")
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_checkpoint  # noqa: E402  (reuse the atomic write)

# The lifecycle states a human sets on a topic, in a sensible display order.
STATUSES = ["proposed", "active", "paused", "shipped", "abandoned"]

# A top-level `status = "..."` (or single-quoted) line, capturing the prefix and
# any trailing comment so both survive a rewrite.
_STATUS_LINE = re.compile(
    r"""^([ \t]*status[ \t]*=[ \t]*)(["'])[^"']*\2(.*)$""", re.MULTILINE
)


class UnknownStatus(ValueError):
    """The requested status is not one of STATUSES."""


class TopicNotFound(FileNotFoundError):
    """No declaration file for the slug."""


class StatusWriteFailed(RuntimeError):
    """The rewrite could not be applied or did not take effect."""


def rewrite_status(text: str, status: str) -> str | None:
    """Return `text` with the top-level `status` set to `status`.

    Handles single- or double-quoted values and a trailing comment. If there is
    no status line, inserts one before the first `[table]` header so it stays
    top-level (not captured into `[match]`). Returns None if it can't be applied.
    """
    if _STATUS_LINE.search(text):
        return _STATUS_LINE.sub(
            lambda m: f'{m.group(1)}"{status}"{m.group(3)}', text, count=1
        )
    lines = text.split("\n")
    insert_at = next(
        (i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), len(lines)
    )
    lines.insert(insert_at, f'status = "{status}"')
    return "\n".join(lines)


def set_status(repo_root: Path, slug: str, status: str) -> None:
    """Set a topic's lifecycle status, verifying the result before writing.

    Raises UnknownStatus / TopicNotFound / StatusWriteFailed on the respective
    failure so a caller can map them to its own error surface.
    """
    if status not in STATUSES:
        raise UnknownStatus(status)
    path = repo_root / "atlas" / "topics" / f"{slug}.toml"
    if not path.is_file():
        raise TopicNotFound(str(path))
    new_text = rewrite_status(path.read_text(encoding="utf-8"), status)
    if new_text is None:
        raise StatusWriteFailed("could not place a status line")
    try:  # confirm it parses and actually took effect -- no silent no-op
        if tomllib.loads(new_text).get("status") != status:
            raise StatusWriteFailed("status did not take effect")
    except tomllib.TOMLDecodeError as exc:
        raise StatusWriteFailed(f"result did not parse: {exc}") from exc
    atlas_checkpoint.atomic_write(path, new_text)
