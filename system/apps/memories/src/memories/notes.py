"""The Claude memory notes: reading them, correcting one, and deleting one.

Claude's built-in memory keeps one Markdown file per note in the notes folder (``autoMemoryDirectory``,
``data/memories/``), each with a small frontmatter block (``name``, ``description``, ``metadata.type``), plus
``MEMORY.md``, an index of one ``- [Title](file.md) — summary`` line per note that every chat loads when it starts.
So every change here keeps the index in step: a corrected summary is written into the note's index line, and a
deleted note's line leaves with it.

A delete erases the note's file; there is no copy here to restore. Backups taken before the delete, and the
conversations of chats that read it, still hold it (the page says so before the user deletes).

Every write checks the note is still the version the page read (its mtime and size), since a chat may write to
it at any time, and goes through a temporary file and a rename so a chat never reads a half-written note.
"""

import json
import os
import re
import uuid
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
NOTE_SUFFIX: Final[str] = ".md"
_FRONTMATTER_FENCE: Final[str] = "---"
_INDEX_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(\s*[-*]\s+\[[^\]]*\]\((?P<file>[^)]+)\))(?P<hook>.*)$")
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
def _scalar_text(value: str) -> str:
    """A frontmatter value as one line of text; a block scalar (``|`` or ``>``) spans the lines below, which this
    line-based reader does not follow, so it reads as empty and the note falls back to its file name."""
    if value.strip()[:1] in ("|", ">"):
        return ""
    return _unquote(value)


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
            nested.setdefault(current_block, {}).setdefault(key.strip(), _scalar_text(value))
            continue
        if line[0] in " \t":
            continue
        current_block = key.strip() if not value.strip() else None
        top_level.setdefault(key.strip(), _scalar_text(value))
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
        continuation_end = description_idx + 1
        while continuation_end < len(frontmatter) and frontmatter[continuation_end][:1] in (" ", "\t"):
            continuation_end += 1
        frontmatter[description_idx:continuation_end] = [description_line]
    else:
        name_idx = next((idx for idx, line in enumerate(frontmatter) if line.startswith("name:")), -1)
        frontmatter.insert(name_idx + 1, description_line)
    return "\n".join([_FRONTMATTER_FENCE, *frontmatter, _FRONTMATTER_FENCE, "", body.strip(), ""])


@pure
def rewrite_index_hook(index_text: str, file_name: str, description: str) -> str:
    """The index with ``file_name``'s line summarized by ``description``; other lines untouched."""
    one_line_description = " ".join(description.split())
    out_lines: list[str] = []
    for line in index_text.split("\n"):
        match = _INDEX_LINE_PATTERN.match(line)
        if match is not None and match.group("file") == file_name:
            out_lines.append(f"{match.group(1)}{_HOOK_SEPARATOR}{one_line_description}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


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
def is_note_file_name(file_name: str) -> bool:
    """Whether a name is a note's: any visible Markdown file directly in the folder, as chats may name it."""
    return (
        file_name.endswith(NOTE_SUFFIX)
        and file_name != NOTE_SUFFIX
        and not file_name.startswith(".")
        and "/" not in file_name
        and "\\" not in file_name
        and "\x00" not in file_name
        and file_name not in NON_NOTE_FILENAMES
    )


def validate_note_name(file_name: str) -> None:
    if not is_note_file_name(file_name):
        raise NoteNameError(f"{file_name!r} is not a note file name")


def _stat_version(stat: os.stat_result) -> str:
    return f"{stat.st_mtime_ns}-{stat.st_size}"


def file_version(path: Path) -> str:
    return _stat_version(path.stat())


def write_atomically(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a sibling temporary file and a rename, keeping the file's permissions.

    The temporary name is unique per write: the server answers requests on several threads, and two writes of the
    index at once must not share one.
    """
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary_path.write_text(text, encoding="utf-8")
        if path.exists():
            os.chmod(temporary_path, path.stat().st_mode)
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
        if not path.is_file() or not is_note_file_name(path.name):
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
        return (notes_dir / INDEX_FILENAME).read_text(encoding="utf-8", errors="replace")
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
    """Rewrite a note's summary and body, and its index line's summary to match."""
    path = _note_path_checked(notes_dir, file_name, version)
    parsed = parse_note(path.read_text(encoding="utf-8", errors="replace"))
    write_atomically(path, render_note(parsed, description, body))
    index_text = read_index(notes_dir)
    if index_text:
        write_atomically(notes_dir / INDEX_FILENAME, rewrite_index_hook(index_text, file_name, description))
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
