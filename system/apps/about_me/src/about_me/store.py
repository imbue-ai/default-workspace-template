"""The three files the page reads and writes, and the only changes it makes to them.

- ``data/memories/user-public-profile.md``: the lookup's memory, one fact per line (``about_me.profile``).
- ``data/memories/MEMORY.md``: the memory index, whose line naming ``(user-public-profile.md)`` points at it.
- ``data/.skills/personalized-ideas/sources.json``: the search results and pages the lookup used.

Editing a fact rewrites only its words, keeping its source and date. Adding a fact the user typed appends one ``(you told me)`` line after the last fact, creating the profile, its index
line and its sources entry when there are none yet. Deleting a fact removes exactly its line from the profile and, from the sources, the entries nothing left cites;
deleting the last fact, or forgetting all, removes the profile, its index line, and the sources, as asking the agent
to forget does. Every write replaces its file whole (a temporary file renamed over it), and one lock serializes them,
so a reader never sees half a file and two requests never interleave. Nothing here reaches the network.
"""

import datetime
import json
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger

from about_me.data_types import Fact
from about_me.data_types import ProfileResponse
from about_me.errors import InvalidFactError
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
MAX_FACT_CHARS: Final[int] = 500
# A comment marker inside a fact would end or start the line's date comment early.
_COMMENT_MARKERS: Final[tuple[str, ...]] = ("<!--", "-->")
NEW_PROFILE_HEADER: Final[bytes] = (
    b"---\n"
    b"name: user-public-profile\n"
    b"description: What the user has told Studio about themselves, and what a web lookup they asked for found\n"
    b"metadata:\n"
    b"  type: user\n"
    b"---\n"
    b"\n"
)
NEW_INDEX_LINE: Final[bytes] = (
    b"- [Public profile](user-public-profile.md) -- what the user told Studio and what a web lookup found\n"
)
TOLD_SOURCE_TITLE: Final[str] = "What you told me"


def normalized_fact_text(text: str) -> str:
    """The fact as one line, its whitespace collapsed; raises ``InvalidFactError`` when it cannot be kept."""
    line = " ".join(text.split())
    if not line:
        raise InvalidFactError("Type something to add.")
    if len(line) > MAX_FACT_CHARS:
        raise InvalidFactError(f"Keep it to {MAX_FACT_CHARS} characters.")
    if any(marker in line for marker in _COMMENT_MARKERS):
        raise InvalidFactError("A fact can't contain <!-- or -->.")
    return line


def told_fact_line(text: str, day: datetime.date) -> bytes:
    """The memory line for a fact the user told: the shape ``about_me.profile`` parses as told by the user."""
    return f"- {text} (you told me) <!-- found {day.isoformat()} -->\n".encode("utf-8")


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


def _kept_sources(entries: list[object], dropped_urls: frozenset[str], is_dropping_told: bool) -> list[object]:
    def is_dropped(entry: object) -> bool:
        if not isinstance(entry, dict):
            return False
        url = entry.get("url")
        if url is None:
            return is_dropping_told and entry.get("origin") == USER_ORIGIN
        return isinstance(url, str) and url in dropped_urls

    return [entry for entry in entries if not is_dropped(entry)]


def _edited_line(fact: Fact, raw: bytes, text: str) -> bytes:
    """``raw`` with ``fact``'s words replaced by ``text``: its source and date kept, its line ending as it was."""
    ending = raw[len(raw.rstrip(b"\r\n")) :]
    if fact.source_url is not None:
        line = f"- {text} ([{fact.source_title}]({fact.source_url})) <!-- found {fact.found_on} -->"
    elif fact.is_told_by_user:
        line = f"- {text} (you told me) <!-- found {fact.found_on} -->"
    else:
        line = f"- {text}"
    return line.encode("utf-8") + ending


def _with_line_after_last_fact(content: bytes, line: bytes) -> bytes:
    """``content`` with ``line`` inserted after its last fact line, or at the end of a profile with none."""
    file_lines = split_lines(content)
    facts = parse_profile(content)
    if not facts:
        if file_lines and not file_lines[-1].endswith(b"\n"):
            file_lines[-1] += b"\n"
        return b"".join(file_lines) + line
    after = facts[-1].index + 1
    if not file_lines[after - 1].endswith(b"\n"):
        file_lines[after - 1] += b"\n"
    return b"".join(file_lines[:after] + [line] + file_lines[after:])


class ProfileStore:
    """The profile, the index and the sources under one workspace root, behind one lock."""

    def __init__(self, workspace_root: Path, today: Callable[[], datetime.date] = datetime.date.today) -> None:
        self._profile = workspace_root / PROFILE_PATH
        self._index = workspace_root / MEMORY_INDEX_PATH
        self._sources = workspace_root / SOURCES_PATH
        self._today = today
        self._lock = threading.Lock()

    def read(self) -> ProfileResponse:
        """What the page shows: every fact, and both raw files as they are."""
        with self._lock:
            return self._read_locked()

    def add_fact(self, text: str) -> ProfileResponse:
        """Keep a fact the user typed, after the last fact; raises ``InvalidFactError`` when it cannot be one line."""
        line = told_fact_line(normalized_fact_text(text), self._today())
        with self._lock:
            content = _read_bytes(self._profile)
            if content is None:
                self._profile.parent.mkdir(parents=True, exist_ok=True)
                write_atomically(self._profile, NEW_PROFILE_HEADER + line)
            else:
                write_atomically(self._profile, _with_line_after_last_fact(content, line))
            self._ensure_index_line()
            self._ensure_told_source()
            return self._read_locked()

    def edit_fact(self, fact_id: str, text: str) -> ProfileResponse:
        """Give the one line ``fact_id`` names new words, keeping its source and date.

        Raises ``InvalidFactError`` when ``text`` cannot be one line, and ``StaleFactError`` when no line of the
        profile as it is now has that id.
        """
        words = normalized_fact_text(text)
        with self._lock:
            content = _read_bytes(self._profile)
            if content is None:
                raise StaleFactError(f"no profile holds the fact {fact_id}")
            target = next((line for line in parse_profile(content) if line.fact.id == fact_id), None)
            if target is None:
                raise StaleFactError(f"the profile has no fact {fact_id} any more")
            file_lines = split_lines(content)
            file_lines[target.index] = _edited_line(target.fact, target.raw, words)
            write_atomically(self._profile, b"".join(file_lines))
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

    def _ensure_index_line(self) -> None:
        index = _read_bytes(self._index)
        if index is not None and PROFILE_INDEX_MARKER in index:
            return
        existing = b"" if index is None else index if index.endswith(b"\n") or not index else index + b"\n"
        write_atomically(self._index, existing + NEW_INDEX_LINE)

    def _ensure_told_source(self) -> None:
        """Add the user's own null-``url`` sources entry once; leave a sources file it cannot read alone."""
        content = _read_bytes(self._sources)
        entries: list[object] = []
        if content is not None:
            try:
                parsed = json.loads(content)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                logger.warning("Leaving {} as it is: it is not JSON ({})", self._sources, error)
                return
            if not isinstance(parsed, list):
                logger.warning("Leaving {} as it is: it is not a JSON array", self._sources)
                return
            entries = parsed
        if any(isinstance(entry, dict) and entry.get("origin") == USER_ORIGIN for entry in entries):
            return
        entries.append(
            {"url": None, "title": TOLD_SOURCE_TITLE, "fetched_at": self._today().isoformat(), "origin": USER_ORIGIN}
        )
        self._sources.parent.mkdir(parents=True, exist_ok=True)
        write_atomically(self._sources, (json.dumps(entries, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

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
