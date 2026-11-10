"""The three files the page reads and writes, and the only changes it makes to them.

- ``data/memories/user-public-profile.md``: the lookup's memory, one fact per line (``about_me.profile``).
- ``data/memories/MEMORY.md``: the memory index, whose line naming ``(user-public-profile.md)`` points at it.
- ``data/.skills/personalized-ideas/sources.json``: the search results and pages the lookup used.

Deleting a fact removes exactly its line from the profile and, from the sources, the entries nothing left cites;
deleting the last fact, or forgetting all, removes the profile, its index line, and the sources, as asking the agent
to forget does. Every write replaces its file whole (a temporary file renamed over it), and one lock serializes them,
so a reader never sees half a file and two requests never interleave. Nothing here reaches the network.
"""

import json
import os
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger

from about_me.data_types import ProfileResponse
from about_me.errors import StaleFactError
from about_me.profile import ProfileLine
from about_me.profile import cited_urls
from about_me.profile import parse_profile
from about_me.profile import split_lines

PROFILE_PATH: Final[Path] = Path("data/memories/user-public-profile.md")
MEMORY_INDEX_PATH: Final[Path] = Path("data/memories/MEMORY.md")
SOURCES_PATH: Final[Path] = Path("data/.skills/personalized-ideas/sources.json")
# How the index line that points at the profile names it.
PROFILE_INDEX_MARKER: Final[bytes] = b"(user-public-profile.md)"
USER_ORIGIN: Final[str] = "user"


def _read_bytes(path: Path) -> bytes | None:
    """The file's bytes, or None when it is missing or cannot be read."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except (IsADirectoryError, PermissionError) as error:
        logger.warning("Cannot read {}: {}", path, error)
        return None


def write_atomically(path: Path, content: bytes) -> None:
    """Replace ``path`` with ``content`` in one rename, keeping the file's mode."""
    mode = path.stat().st_mode & 0o7777 if path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _kept_sources(entries: Sequence[object], dropped_urls: frozenset[str], is_dropping_told: bool) -> list[object]:
    def is_dropped(entry: object) -> bool:
        if not isinstance(entry, dict):
            return False
        url = entry.get("url")
        if url is None:
            return is_dropping_told and entry.get("origin") == USER_ORIGIN
        return isinstance(url, str) and url in dropped_urls

    return [entry for entry in entries if not is_dropped(entry)]


class ProfileStore:
    """The profile, the index and the sources under one workspace root, behind one lock."""

    def __init__(self, workspace_root: Path) -> None:
        self._profile = workspace_root / PROFILE_PATH
        self._index = workspace_root / MEMORY_INDEX_PATH
        self._sources = workspace_root / SOURCES_PATH
        self._lock = threading.Lock()

    def read(self) -> ProfileResponse:
        """What the page shows: every fact, and both raw files as they are."""
        with self._lock:
            return self._read_locked()

    def delete_fact(self, fact_id: str) -> ProfileResponse:
        """Remove the one line ``fact_id`` names, and the sources only it cited; the last fact forgets everything.

        Raises ``StaleFactError`` when no line of the profile as it is now has that id.
        """
        with self._lock:
            content = _read_bytes(self._profile)
            if content is None:
                raise StaleFactError(f"no profile holds the fact {fact_id}")
            lines = parse_profile(content)
            target = next((line for line in lines if line.fact.id == fact_id), None)
            if target is None:
                raise StaleFactError(f"the profile has no fact {fact_id} any more")
            file_lines = split_lines(content)
            remaining_content = b"".join(file_lines[: target.index] + file_lines[target.index + 1 :])
            remaining = parse_profile(remaining_content)
            if not remaining:
                self._forget_all_locked()
                return self._read_locked()
            write_atomically(self._profile, remaining_content)
            self._prune_sources(target, remaining)
            return self._read_locked()

    def forget_all(self) -> ProfileResponse:
        """Delete the profile, its line in the memory index (the rest of the index untouched), and the sources."""
        with self._lock:
            self._forget_all_locked()
            return self._read_locked()

    def _read_locked(self) -> ProfileResponse:
        content = _read_bytes(self._profile)
        sources = _read_bytes(self._sources)
        return ProfileResponse(
            facts=tuple(line.fact for line in parse_profile(content)) if content is not None else (),
            profile_markdown=content.decode("utf-8", errors="replace") if content is not None else None,
            sources_json=sources.decode("utf-8", errors="replace") if sources is not None else None,
        )

    def _forget_all_locked(self) -> None:
        self._profile.unlink(missing_ok=True)
        index = _read_bytes(self._index)
        if index is not None:
            index_lines = split_lines(index)
            kept = [line for line in index_lines if PROFILE_INDEX_MARKER not in line]
            if len(kept) != len(index_lines):
                write_atomically(self._index, b"".join(kept))
        self._sources.unlink(missing_ok=True)

    def _prune_sources(self, deleted: ProfileLine, remaining: tuple[ProfileLine, ...]) -> None:
        """Drop the sources the deleted line cited and no remaining line does; leave a file it cannot read alone."""
        dropped_urls = cited_urls((deleted,)) - cited_urls(remaining)
        is_dropping_told = deleted.fact.is_told_by_user and not any(line.fact.is_told_by_user for line in remaining)
        if not dropped_urls and not is_dropping_told:
            return
        content = _read_bytes(self._sources)
        if content is None:
            return
        try:
            entries = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            logger.warning("Leaving {} as it is: it is not JSON ({})", self._sources, error)
            return
        if not isinstance(entries, list):
            logger.warning("Leaving {} as it is: it is not a JSON array", self._sources)
            return
        kept = _kept_sources(entries, dropped_urls, is_dropping_told)
        if len(kept) != len(entries):
            write_atomically(self._sources, (json.dumps(kept, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
