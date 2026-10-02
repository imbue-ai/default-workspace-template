"""The Claude memory notes: reading them, correcting one, and deleting one.

Claude's built-in memory keeps one Markdown file per note in the notes folder (``autoMemoryDirectory``,
``data/memories/``), each with a small frontmatter block (``name``, ``description``, ``metadata.type``), plus
``MEMORY.md``, an index of one ``- [Title](file.md) — summary`` line per note that every chat loads when it starts.
So every change here keeps the index in step: a corrected summary is written into the note's index line, and a
deleted note's line leaves with it.

A delete erases the note: nothing in the workspace keeps a copy to restore. Snapshots the backups took before the
delete still hold it until they expire (see ``backups``), which the page tells the user before they delete.

Every write checks the note is still the version the page read (its mtime and size), since a chat may write to
it at any time, and goes through a temporary file and a rename so a chat never reads a half-written note.
"""

import json
import os
import re
import uuid
from collections.abc import Set as AbstractSet
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from memories.errors import NoteChangedError
from memories.errors import NoteNameError
from memories.errors import NoteNotFoundError
from memories.errors import NoteWriteError

INDEX_FILENAME: Final[str] = "MEMORY.md"
NON_NOTE_FILENAMES: Final[frozenset[str]] = frozenset({INDEX_FILENAME, "README.md"})
NOTE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
_FRONTMATTER_FENCE: Final[str] = "---"
_INDEX_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(\s*[-*]\s+\[(?P<title>[^\]]*)\]\((?P<file>[^)]+)\))(?P<hook>.*)$"
)
# How much of MEMORY.md a chat loads: Claude Code's limit, which agent_memory_context.py applies for pi too.
INDEX_LOADED_MAX_LINES: Final[int] = 200
INDEX_LOADED_MAX_BYTES: Final[int] = 25 * 1024
_HOOK_SEPARATOR: Final[str] = " — "
# The harness a note came from. pi writes ``metadata.source`` itself (the protocol it is given asks it to); Claude
# Code writes none, but stamps ``originSessionId`` on every note it saves.
CLAUDE_SOURCE: Final[str] = "claude"
_CLAUDE_SESSION_KEY: Final[str] = "originSessionId"


class NoteType(UpperCaseStrEnum):
    """What a note is about, from its ``type`` field."""

    USER = auto()
    FEEDBACK = auto()
    PROJECT = auto()
    REFERENCE = auto()
    OTHER = auto()


class ParsedNote(FrozenModel):
    """A note file split into its frontmatter and body."""

    name: str | None = Field(description="The ``name`` field, or None")
    description: str | None = Field(description="The ``description`` field: the note's one-line summary, or None")
    note_type: NoteType = Field(description="The ``metadata.type`` (or top-level ``type``) field")
    source: str | None = Field(description="The harness that saved it, or None when the note does not say")
    frontmatter_lines: tuple[str, ...] = Field(description="The frontmatter's lines verbatim, fences excluded")
    body: str = Field(description="Everything after the frontmatter")


class Note(FrozenModel):
    """One note as the page shows it."""

    file_name: str = Field(description="The note's file name in the notes folder")
    name: str | None = Field(description="The note's ``name`` field")
    description: str = Field(description="Its one-line summary (the file name when it has none)")
    note_type: NoteType = Field(description="What it is about")
    source: str | None = Field(description="The harness that saved it (``claude``, ``pi-coding``), when known")
    body: str = Field(description="Its text after the frontmatter")
    raw_text: str = Field(description="The file exactly as it is on disk")
    modified_at: datetime = Field(description="When the file last changed")
    version: str = Field(description="The file's mtime and size, which a write must match")


class IndexEntry(FrozenModel):
    """A note's line in ``MEMORY.md``: what every chat starts with, before it opens the note itself."""

    is_loaded: bool = Field(description="Whether it falls within what a chat loads (200 lines or 25KB)")


class IndexSummary(FrozenModel):
    """How much of ``MEMORY.md`` chats load, and what it points at that is not there."""

    line_count: int = Field(description="Its lines")
    loaded_line_count: int = Field(description="How many of them a chat loads")
    max_lines: int = Field(description="The most lines a chat loads")
    max_bytes: int = Field(description="The most bytes a chat loads")
    missing_files: tuple[str, ...] = Field(description="Notes it lists whose files no longer exist")


class NoteListing(FrozenModel):
    """The notes in the folder, and the note files that could not be read."""

    notes: tuple[Note, ...] = Field(description="Every readable note, most recently changed first")
    unreadable_file_names: tuple[str, ...] = Field(description="Note files that could not be read")


@pure
def _unquote(value: str) -> str:
    """A frontmatter scalar's text: a double-quoted one unescaped (YAML's escapes are JSON's for what notes hold), a
    single-quoted one with its doubled quotes undone, anything else as written."""
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        try:
            decoded = json.loads(stripped)
        except ValueError:
            return stripped[1:-1]
        return decoded if isinstance(decoded, str) else stripped[1:-1]
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == "'":
        return stripped[1:-1].replace("''", "'")
    return stripped


@pure
def parse_note(text: str) -> ParsedNote:
    """Split a note into frontmatter fields and body; a file with no frontmatter is all body."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return ParsedNote(
            name=None, description=None, note_type=NoteType.OTHER, source=None, frontmatter_lines=(), body=text
        )
    closing_idx = next((idx for idx in range(1, len(lines)) if lines[idx].strip() == _FRONTMATTER_FENCE), None)
    if closing_idx is None:
        return ParsedNote(
            name=None, description=None, note_type=NoteType.OTHER, source=None, frontmatter_lines=(), body=text
        )
    frontmatter_lines = tuple(lines[1:closing_idx])
    top_level: dict[str, str] = {}
    nested: dict[str, dict[str, str]] = {}
    current_block: str | None = None
    for line in frontmatter_lines:
        if not line.strip():
            continue
        key, separator, value = line.strip().partition(":")
        if not separator:
            continue
        if line[0] in " \t" and current_block is not None:
            nested.setdefault(current_block, {})[key.strip()] = _unquote(value)
            continue
        current_block = key.strip() if not value.strip() else None
        top_level[key.strip()] = _unquote(value)
    metadata = nested.get("metadata", {})
    type_text = metadata.get("type") or top_level.get("type") or ""
    try:
        note_type = NoteType(type_text.strip().upper())
    except ValueError:
        note_type = NoteType.OTHER
    return ParsedNote(
        name=top_level.get("name") or None,
        description=top_level.get("description") or None,
        note_type=note_type,
        source=metadata.get("source")
        or top_level.get("source")
        or (CLAUDE_SOURCE if _CLAUDE_SESSION_KEY in metadata else None),
        frontmatter_lines=frontmatter_lines,
        body="\n".join(lines[closing_idx + 1 :]).lstrip("\n"),
    )


@pure
def render_note(parsed: ParsedNote, description: str, body: str) -> str:
    """The note with a new summary and body, every other frontmatter line kept as it was."""
    one_line_description = " ".join(description.split())
    # Double-quoted, so a summary such as "Uses: tabs" or one starting with "-" or "[" is still one YAML string.
    description_line = f"description: {json.dumps(one_line_description, ensure_ascii=False)}"
    frontmatter = list(parsed.frontmatter_lines)
    description_idx = next(
        (idx for idx, line in enumerate(frontmatter) if line.startswith("description:")),
        None,
    )
    if description_idx is not None:
        frontmatter[description_idx] = description_line
    else:
        name_idx = next((idx for idx, line in enumerate(frontmatter) if line.startswith("name:")), -1)
        frontmatter.insert(name_idx + 1, description_line)
    return "\n".join([_FRONTMATTER_FENCE, *frontmatter, _FRONTMATTER_FENCE, "", body.strip(), ""])


@pure
def index_title(note_name: str | None, file_name: str) -> str:
    """The link text for a note's index line, in the style Claude writes it: "user-location" reads "User location"."""
    words = (note_name or file_name.removesuffix(".md")).replace("-", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


@pure
def rewrite_index_hook(index_text: str, file_name: str, title: str, description: str) -> str:
    """The index with ``file_name``'s line summarized by ``description``, or with a line added for it when it has
    none, so a note that fell out of the index is listed again; other lines untouched."""
    one_line_description = " ".join(description.split())
    out_lines: list[str] = []
    is_listed = False
    for line in index_text.split("\n"):
        match = _INDEX_LINE_PATTERN.match(line)
        if match is not None and match.group("file") == file_name:
            out_lines.append(f"{match.group(1)}{_HOOK_SEPARATOR}{one_line_description}")
            is_listed = True
        else:
            out_lines.append(line)
    if is_listed:
        return "\n".join(out_lines)
    base = index_text if index_text.endswith("\n") or not index_text else f"{index_text}\n"
    return f"{base}- [{title}]({file_name}){_HOOK_SEPARATOR}{one_line_description}\n"


@pure
def split_index_lines(index_text: str, file_name: str) -> tuple[str, tuple[str, ...]]:
    """The index without ``file_name``'s lines, and those lines."""
    kept: list[str] = []
    removed: list[str] = []
    for line in index_text.split("\n"):
        match = _INDEX_LINE_PATTERN.match(line)
        if match is not None and match.group("file") == file_name:
            removed.append(line)
        else:
            kept.append(line)
    return "\n".join(kept), tuple(removed)


@pure
def loaded_line_count(index_text: str) -> int:
    """How many lines of the index a chat loads: the first 200, cut sooner if they pass 25KB."""
    loaded = 0
    loaded_bytes = 0
    for line in index_text.splitlines()[:INDEX_LOADED_MAX_LINES]:
        loaded_bytes += len(line.encode("utf-8")) + 1
        if loaded_bytes > INDEX_LOADED_MAX_BYTES:
            break
        loaded += 1
    return loaded


@pure
def parse_index(index_text: str) -> dict[str, IndexEntry]:
    """Each note's first line in the index, keyed by file name."""
    loaded = loaded_line_count(index_text)
    entry_by_file_name: dict[str, IndexEntry] = {}
    for line_idx, line in enumerate(index_text.splitlines()):
        match = _INDEX_LINE_PATTERN.match(line)
        if match is None or match.group("file") in entry_by_file_name:
            continue
        entry_by_file_name[match.group("file")] = IndexEntry(is_loaded=line_idx < loaded)
    return entry_by_file_name


@pure
def summarize_index(index_text: str, note_file_names: AbstractSet[str]) -> IndexSummary:
    return IndexSummary(
        line_count=len(index_text.splitlines()),
        loaded_line_count=loaded_line_count(index_text),
        max_lines=INDEX_LOADED_MAX_LINES,
        max_bytes=INDEX_LOADED_MAX_BYTES,
        missing_files=tuple(name for name in parse_index(index_text) if name not in note_file_names),
    )


def validate_note_name(file_name: str) -> None:
    if not NOTE_NAME_PATTERN.match(file_name) or file_name in NON_NOTE_FILENAMES:
        raise NoteNameError(f"{file_name!r} is not a note file name")


def _stat_version(stat: os.stat_result) -> str:
    return f"{stat.st_mtime_ns}-{stat.st_size}"


def file_version(path: Path) -> str:
    return _stat_version(path.stat())


def write_atomically(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a sibling temporary file and a rename.

    The temporary name is unique per write: the server answers requests on several threads, and two writes of the
    index at once must not share one.
    """
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary_path.write_text(text, encoding="utf-8")
        os.replace(temporary_path, path)
    except OSError as e:
        temporary_path.unlink(missing_ok=True)
        raise NoteWriteError(f"could not write {path}: {e}") from e


def read_note(path: Path) -> Note:
    """A note as the page shows it. Its version is read before its text, so a chat writing in between makes the
    version older than the text, and the next save is refused rather than overwriting the chat's change."""
    stat = path.stat()
    raw_text = path.read_text(encoding="utf-8", errors="replace")
    parsed = parse_note(raw_text)
    return Note(
        file_name=path.name,
        name=parsed.name,
        description=parsed.description or path.stem.replace("_", " ").replace("-", " "),
        note_type=parsed.note_type,
        source=parsed.source,
        body=parsed.body,
        raw_text=raw_text,
        modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
        version=_stat_version(stat),
    )


def list_notes(notes_dir: Path) -> NoteListing:
    """Every note in the folder, most recently changed first, and the note files that could not be read."""
    if not notes_dir.is_dir():
        return NoteListing(notes=(), unreadable_file_names=())
    notes: list[Note] = []
    unreadable: list[str] = []
    for path in sorted(notes_dir.iterdir()):
        if not path.is_file() or path.name in NON_NOTE_FILENAMES or not NOTE_NAME_PATTERN.match(path.name):
            continue
        try:
            notes.append(read_note(path))
        except OSError as e:
            logger.warning("Could not read the note {}: {}", path, e)
            unreadable.append(path.name)
    return NoteListing(
        notes=tuple(sorted(notes, key=lambda note: note.modified_at, reverse=True)),
        unreadable_file_names=tuple(unreadable),
    )


def read_index(notes_dir: Path) -> str:
    try:
        return (notes_dir / INDEX_FILENAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _note_path_checked(notes_dir: Path, file_name: str, version: str) -> Path:
    validate_note_name(file_name)
    path = notes_dir / file_name
    if not path.is_file():
        raise NoteNotFoundError(f"no note named {file_name}")
    if file_version(path) != version:
        raise NoteChangedError(f"{file_name} changed since it was read")
    return path


def update_note(notes_dir: Path, file_name: str, description: str, body: str, version: str) -> Note:
    """Rewrite a note's summary and body, and its index line to match (adding the line when the index lacks it)."""
    path = _note_path_checked(notes_dir, file_name, version)
    parsed = parse_note(path.read_text(encoding="utf-8", errors="replace"))
    write_atomically(path, render_note(parsed, description, body))
    index_text = read_index(notes_dir)
    write_atomically(
        notes_dir / INDEX_FILENAME,
        rewrite_index_hook(index_text, file_name, index_title(parsed.name, file_name), description),
    )
    return read_note(path)


def delete_note(notes_dir: Path, file_name: str, version: str) -> None:
    """Erase a note's file and drop its lines from the index, so no chat loads or finds it again."""
    path = _note_path_checked(notes_dir, file_name, version)
    try:
        path.unlink()
    except OSError as e:
        raise NoteWriteError(f"could not delete {path}: {e}") from e
    kept_index, removed_lines = split_index_lines(read_index(notes_dir), file_name)
    if removed_lines:
        write_atomically(notes_dir / INDEX_FILENAME, kept_index)
