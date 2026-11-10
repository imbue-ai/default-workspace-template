"""The files the page reads and writes, and the only changes it makes to them.

- ``data/memories/*.md``: the agent's memories, one per file (``about_me.memories``), indexed by
  ``data/memories/MEMORY.md``.
- ``data/memories/user-public-profile.md``: the web lookup's memory, one fact per line (``about_me.profile``).
- ``data/.skills/personalized-ideas/sources.json``: the search results and pages the lookup used.

Adding a memory writes a new ``user`` memory file and its index line. Editing one rewrites only its description and
body (and its index line's hook); deleting one removes the file and its index lines. Editing a lookup fact rewrites
only its words; deleting one removes exactly its line and, from the sources, the entries nothing left cites.
Forgetting the lookup removes its memory, its index line and the sources; forgetting everything removes every
memory and the index too. Every write replaces its file whole (a temporary file renamed over it), and one lock
serializes them, so a reader never sees half a file and two requests never interleave. Nothing here reaches the
network.
"""

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Final

from loguru import logger

from about_me.data_types import Fact
from about_me.data_types import Memory
from about_me.data_types import ProfileResponse
from about_me.errors import InvalidFactError
from about_me.errors import StaleFactError
from about_me.errors import StaleMemoryError
from about_me.memories import index_line
from about_me.memories import index_titles
from about_me.memories import is_memory_file_name
from about_me.memories import memory_from_file
from about_me.memories import new_user_memory
from about_me.memories import parse_memory
from about_me.memories import render_memory
from about_me.memories import revision
from about_me.memories import with_description
from about_me.memories import without_index_lines
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
MAX_DESCRIPTION_CHARS: Final[int] = 300
MAX_BODY_CHARS: Final[int] = 5000
MAX_TITLE_CHARS: Final[int] = 60
MAX_SLUG_WORDS: Final[int] = 6
# A comment marker inside a fact would end or start the line's date comment early.
_COMMENT_MARKERS: Final[tuple[str, ...]] = ("<!--", "-->")


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


def _one_line(text: str, limit: int, what: str) -> str:
    """``text`` as one line, its whitespace collapsed; raises ``InvalidFactError`` when it is empty or too long."""
    line = " ".join(text.split())
    if not line:
        raise InvalidFactError(f"Type {what}.")
    if len(line) > limit:
        raise InvalidFactError(f"Keep {what} to {limit} characters.")
    return line


def normalized_body(text: str) -> str:
    """A memory's body with its outer whitespace trimmed; raises ``InvalidFactError`` when it is empty or too long."""
    body = text.strip()
    if not body:
        raise InvalidFactError("Type what I should remember.")
    if len(body) > MAX_BODY_CHARS:
        raise InvalidFactError(f"Keep it to {MAX_BODY_CHARS} characters.")
    return body


def short_title(text: str) -> str:
    """``text`` cut at a word boundary to fit a memory index title."""
    if len(text) <= MAX_TITLE_CHARS:
        return text
    cut_at = text.rfind(" ", 0, MAX_TITLE_CHARS)
    return text[: cut_at if cut_at > 0 else MAX_TITLE_CHARS].rstrip(" ,.;:") + "..."


def slug(text: str) -> str:
    """A file-name-safe slug from the first words of ``text``."""
    words = re.findall(r"[a-z0-9]+", text.lower())[:MAX_SLUG_WORDS]
    return "-".join(words) or "fact"


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


class ProfileStore:
    """The profile, the index and the sources under one workspace root, behind one lock."""

    def __init__(self, workspace_root: Path) -> None:
        self._profile = workspace_root / PROFILE_PATH
        self._index = workspace_root / MEMORY_INDEX_PATH
        self._memories = self._index.parent
        self._sources = workspace_root / SOURCES_PATH
        self._lock = threading.Lock()

    def read(self) -> ProfileResponse:
        """What the page shows: every fact, and both raw files as they are."""
        with self._lock:
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

    def forget_lookup(self) -> ProfileResponse:
        """Delete the lookup's memory, its line in the memory index (the rest of the index untouched), and the sources."""
        with self._lock:
            self._forget_all_locked()
            return self._read_locked()

    def forget_everything(self) -> ProfileResponse:
        """Delete every memory, the memory index, and the lookup's sources."""
        with self._lock:
            for path in self._memory_paths():
                path.unlink(missing_ok=True)
            self._index.unlink(missing_ok=True)
            self._sources.unlink(missing_ok=True)
            return self._read_locked()

    def add_memory(self, text: str) -> ProfileResponse:
        """Keep something the user typed about themselves as a new ``user`` memory, with its line in the index."""
        line = _one_line(text, MAX_FACT_CHARS, "something to add")
        with self._lock:
            self._memories.mkdir(parents=True, exist_ok=True)
            base = f"user-{slug(line)}"
            name = base
            suffix = 2
            while (self._memories / f"{name}.md").exists():
                name = f"{base}-{suffix}"
                suffix += 1
            file_name = f"{name}.md"
            write_atomically(self._memories / file_name, new_user_memory(name, line, line).encode("utf-8"))
            index = _read_bytes(self._index)
            existing = b"" if index is None else index if not index or index.endswith(b"\n") else index + b"\n"
            new_line = index_line(short_title(line), file_name, line) + "\n"
            write_atomically(self._index, existing + new_line.encode("utf-8"))
            return self._read_locked()

    def edit_memory(self, file_name: str, expected_revision: str, description: str, body: str) -> ProfileResponse:
        """Give one memory a new description and body, and its index line the new description as its hook.

        Raises ``InvalidFactError`` for an empty or overlong description or body, and ``StaleMemoryError`` when the
        file is gone or changed since the page read it.
        """
        new_description = _one_line(description, MAX_DESCRIPTION_CHARS, "a short summary")
        new_body = normalized_body(body)
        with self._lock:
            path, content = self._current_memory(file_name, expected_revision)
            parsed = parse_memory(content.decode("utf-8", errors="replace"))
            frontmatter = with_description(parsed.frontmatter, new_description) if parsed.frontmatter else []
            rendered = render_memory(frontmatter, new_body) if frontmatter else new_body + "\n"
            write_atomically(path, rendered.encode("utf-8"))
            self._rewrite_index_hook(file_name, new_description)
            return self._read_locked()

    def delete_memory(self, file_name: str, expected_revision: str) -> ProfileResponse:
        """Delete one memory and its lines in the index; deleting the lookup's memory forgets the lookup.

        Raises ``StaleMemoryError`` when the file is gone or changed since the page read it.
        """
        with self._lock:
            path, _ = self._current_memory(file_name, expected_revision)
            if path == self._profile:
                self._forget_all_locked()
                return self._read_locked()
            path.unlink()
            index = _read_bytes(self._index)
            if index is not None:
                kept = without_index_lines(index.decode("utf-8", errors="replace"), file_name)
                write_atomically(self._index, kept.encode("utf-8"))
            return self._read_locked()

    def _memory_paths(self) -> list[Path]:
        if not self._memories.is_dir():
            return []
        return sorted(path for path in self._memories.iterdir() if is_memory_file_name(path.name) and path.is_file())

    def _current_memory(self, file_name: str, expected_revision: str) -> tuple[Path, bytes]:
        if not is_memory_file_name(file_name):
            raise StaleMemoryError(f"{file_name!r} is not a memory file")
        path = self._memories / file_name
        content = _read_bytes(path)
        if content is None or revision(content) != expected_revision:
            raise StaleMemoryError(f"{file_name} changed since the page read it")
        return path, content

    def _rewrite_index_hook(self, file_name: str, hook: str) -> None:
        index = _read_bytes(self._index)
        if index is None:
            return
        text = index.decode("utf-8", errors="replace")
        title = index_titles(text).get(file_name)
        if title is None:
            return
        target = f"]({file_name})"
        lines = text.splitlines(keepends=True)
        rewritten = [
            index_line(title, file_name, hook) + ("\n" if line.endswith("\n") else "") if target in line else line
            for line in lines
        ]
        write_atomically(self._index, "".join(rewritten).encode("utf-8"))

    def _read_memories(self, index: bytes | None) -> tuple[Memory, ...]:
        titles = index_titles(index.decode("utf-8", errors="replace") if index is not None else None)
        memories = []
        for path in self._memory_paths():
            if path == self._profile:
                continue
            content = _read_bytes(path)
            if content is not None:
                memories.append(memory_from_file(path, content, titles))
        return tuple(memories)

    def _read_locked(self) -> ProfileResponse:
        content = _read_bytes(self._profile)
        sources = _read_bytes(self._sources)
        index = _read_bytes(self._index)
        return ProfileResponse(
            memories=self._read_memories(index),
            profile_file_name=self._profile.name,
            profile_revision=revision(content) if content is not None else None,
            memory_index_markdown=index.decode("utf-8", errors="replace") if index is not None else None,
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
