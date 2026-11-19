"""Reading and writing the agent's memory notes.

The notes live in one folder (``data/memories/`` by default): one markdown file
per note, each opening with YAML frontmatter (``name``, ``description``,
``metadata.type``), plus ``MEMORY.md``, the index the agent loads every session,
with one ``- [Title](file.md) — summary`` line per note. A line in the index
that points at no file is a note written straight into the index; it is listed
as a note of its own, and saving it turns it into a file with a pointer line in
its place.

Every write goes through ``NoteStore`` so the index always agrees with the
files: saving a note rewrites its pointer line, deleting it removes the line.
Every change names the version (a hash of the text) the editor started from,
and a note that changed underneath since is refused rather than overwritten.
"""

import hashlib
import itertools
import os
import re
import tempfile
import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import auto
from pathlib import Path
from typing import Any, Final

import yaml
from imbue.imbue_common.enums import LowerCaseStrEnum, UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from agent_memory.changes import (
    INDEX_FILE_NAME,
    NOTE_SUFFIX,
    POINTER_LINE_START,
    AgentMemoryError,
    note_title,
)
from agent_memory.primitives import NoteId, NoteVersion

# Files in the folder that are not notes.
_NON_NOTE_FILE_NAMES: Final = frozenset({INDEX_FILE_NAME, "README.md"})
_LOOSE_ID_PREFIX: Final = "index-"


class NoteKind(LowerCaseStrEnum):
    """What a note is about, as its frontmatter's ``metadata.type`` names it.

    Lowercase, unlike the codebase's other enums: the values are Claude Code's own on-disk
    ``metadata.type`` words, which the files and the editor share as they are.
    """

    USER = auto()
    FEEDBACK = auto()
    PROJECT = auto()
    REFERENCE = auto()


_POINTER_LINE: Final = re.compile(
    POINTER_LINE_START.pattern + r"\s*(?:[—–-]+\s*(?P<hook>.*))?$"
)
_LOOSE_LINE: Final = re.compile(r"^\s*[-*]\s+(?P<text>\S.*)$")
_HEADING_LINE: Final = re.compile(r"^(#{1,6})\s+(.*)$")
_FRONTMATTER: Final = re.compile(r"\A---\s*\n(?P<yaml>.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_WHY_MARKER: Final = re.compile(r"^\*\*Why:\*\*[ \t]*", re.MULTILINE)
_HOW_MARKER: Final = re.compile(r"^\*\*How to apply:\*\*[ \t]*", re.MULTILINE)
_LINK: Final = re.compile(r"\[\[([^\]\n]+)\]\]")
_SLUG_JUNK: Final = re.compile(r"[^a-z0-9]+")
# A version is this many hex digits of the text's SHA-256.
_VERSION_LENGTH: Final = 16
_MAX_SLUG_WORDS: Final = 8
_DEFAULT_SLUG: Final = "note"
# A note written straight into the index is titled by its text, cut to this length.
_MAX_LOOSE_TITLE_LENGTH: Final = 60
_ELLIPSIS: Final = "..."
_PERMISSION_BITS: Final = 0o777
_DEFAULT_NOTE_MODE: Final = 0o644
# Wide enough that PyYAML never folds a frontmatter value onto a second line.
_YAML_LINE_WIDTH: Final = 10_000
_DEFAULT_KIND: Final = NoteKind.USER.value
# Claude Code's guidance for one line of the index: under about this many characters. A longer
# line costs the agent's starting overview room, so the editor warns about it (it still saves).
INDEX_LINE_LIMIT: Final = 150


class Note(FrozenModel):
    """One remembered note, as the editor shows it."""

    id: NoteId = Field(
        description="Stable id: the note's name, or 'index-<hash>' for a note written straight into the index"
    )
    name: str = Field(
        description="The note's short link name (other notes link it as [[name]]); empty for an index-only note"
    )
    file_name: str = Field(
        description="The note's file in the memory folder; empty for an index-only note"
    )
    title: str = Field(description="The note's title, as the index lists it")
    summary: str = Field(
        description="The one-line summary the agent reads to judge relevance"
    )
    # Plain text, not NoteKind: a file may name a kind of its own, and the editor shows it as it is.
    kind: str = Field(
        description="One of user, feedback, project, reference (or whatever the file says)"
    )
    body: str = Field(
        description="What the agent should remember, without the Why and How to apply parts"
    )
    why: str = Field(description="Why the note holds (feedback and project notes)")
    how: str = Field(
        description="How the agent should apply it (feedback and project notes)"
    )
    links: tuple[str, ...] = Field(
        description="Names of the notes this one links to with [[name]]"
    )
    raw: str = Field(
        description="The note exactly as saved: the file's text, or the index line"
    )
    version: NoteVersion = Field(
        description="A hash of the saved text; changes name it so a note edited elsewhere is not overwritten"
    )
    is_index_only: bool = Field(
        description="Whether the note is a line in the index with no file of its own yet"
    )


class IndexEntryKind(UpperCaseStrEnum):
    """What one line of the index is."""

    HEADING = auto()
    NOTE = auto()
    MISSING = auto()
    LOOSE = auto()
    TEXT = auto()


class IndexEntry(FrozenModel):
    """One line of the index, as the navigation mirrors it."""

    kind: IndexEntryKind = Field(
        description="HEADING; NOTE (points at a note file); MISSING (points at a file that does not exist); "
        "LOOSE (a note written straight into the index); TEXT (any other line)"
    )
    text: str = Field(
        description="What the line shows: the heading, the note's title, or the line's text"
    )
    hook: str = Field(
        default="", description="The summary the index line gives after the title"
    )
    level: int = Field(
        default=0, description="A heading's level (1 for '#'); 0 otherwise"
    )
    note_id: str = Field(
        default="",
        description="The id of the note the line stands for; empty for headings, text and missing files",
    )
    file_name: str = Field(
        default="",
        description="The file the line points at; empty when it points at none",
    )
    line_number: int = Field(description="The line's number in the index, from 1")
    length: int = Field(description="How many characters the line has")
    is_too_long: bool = Field(
        description=f"Whether the line is longer than the {INDEX_LINE_LIMIT} characters Claude Code suggests"
    )


class IndexOutline(FrozenModel):
    """The index as the navigation mirrors it, plus the notes it leaves out."""

    entries: tuple[IndexEntry, ...] = Field(
        description="The index's non-blank lines, in order"
    )
    unlisted_note_ids: tuple[NoteId, ...] = Field(
        description="Notes with a file but no line in the index"
    )
    raw: str = Field(description="The index exactly as saved")
    is_present: bool = Field(description="Whether the index file exists")


class NoteDraft(FrozenModel):
    """A note's content as the editor submits it."""

    title: str = Field(description="The note's title")
    summary: str = Field(description="The one-line summary")
    kind: NoteKind = Field(description="One of user, feedback, project, reference")
    body: str = Field(description="What the agent should remember")
    why: str = Field(default="", description="Why the note holds")
    how: str = Field(default="", description="How the agent should apply it")


class NoteNotFoundError(AgentMemoryError, KeyError):
    """No note has that id."""


class NoteChangedError(AgentMemoryError, ValueError):
    """The note was changed elsewhere since the editor loaded it."""


class InvalidNoteError(AgentMemoryError, ValueError):
    """The submitted note cannot be saved as it is."""


@pure
def _text_version(text: str) -> NoteVersion:
    return NoteVersion(hashlib.sha256(text.encode()).hexdigest()[:_VERSION_LENGTH])


@pure
def _slugify(title: str) -> str:
    slug = _SLUG_JUNK.sub("-", title.lower()).strip("-")
    return "-".join(slug.split("-")[:_MAX_SLUG_WORDS]) or _DEFAULT_SLUG


@pure
def _section_end(start: re.Match[str], other: re.Match[str] | None, text: str) -> int:
    """Where the section opened by ``start`` ends: at ``other``'s marker when that comes after it."""
    if other is not None and other.start() > start.start():
        return other.start()
    return len(text)


@pure
def _split_body(text: str) -> tuple[str, str, str]:
    """A note's text after the frontmatter as (body, why, how)."""
    why_match = _WHY_MARKER.search(text)
    how_match = _HOW_MARKER.search(text)
    cut_points = [
        match.start() for match in (why_match, how_match) if match is not None
    ]
    body_end = min(cut_points) if cut_points else len(text)
    why = (
        ""
        if why_match is None
        else text[why_match.end() : _section_end(why_match, how_match, text)].strip()
    )
    how = (
        ""
        if how_match is None
        else text[how_match.end() : _section_end(how_match, why_match, text)].strip()
    )
    return text[:body_end].strip(), why, how


@pure
def _join_body(body: str, why: str, how: str) -> str:
    text = body.strip()
    reasons = [
        f"{label} {value.strip()}"
        for label, value in (("**Why:**", why), ("**How to apply:**", how))
        if value.strip()
    ]
    parts = [part for part in (text, "\n\n".join(reasons)) if part]
    return "\n\n".join(parts) + "\n"


@pure
def _one_line(text: str) -> str:
    return " ".join(text.split())


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """A note's frontmatter (empty when it has none, or none PyYAML can read) and the text after it."""
    match = _FRONTMATTER.match(text)
    if match is None:
        return {}, text
    try:
        loaded = yaml.safe_load(match["yaml"])
    except yaml.YAMLError as error:
        logger.warning(
            "Skipped a memory note's frontmatter that is not valid YAML: {}", error
        )
        return {}, text
    if not isinstance(loaded, dict):
        return {}, text
    return loaded, text[match.end() :]


@pure
def _readable_index_line(line: str) -> str:
    pointer = _POINTER_LINE.match(line)
    if pointer is not None:
        hook = (pointer["hook"] or "").strip()
        title = pointer["title"].strip()
        return f"{title} — {hook}" if hook else title
    if line.lstrip().startswith("#"):
        return line.lstrip("#").strip()
    loose = _LOOSE_LINE.match(line)
    if loose is not None:
        return loose["text"].strip()
    return line


def readable_text(file_name: str, text: str | None) -> str | None:
    """A memory file as a person reads it, for showing what a change did to it.

    A note keeps its summary and its text (with any Why and How to apply), and drops the
    frontmatter's link name and bookkeeping. The index shows each line as "Title — summary",
    a heading as its words, and a line written straight into it as that line's text.
    """
    if text is None:
        return None
    if file_name == INDEX_FILE_NAME:
        return "\n".join(
            _readable_index_line(line) for line in text.splitlines()
        ).strip()
    frontmatter, rest = _parse_frontmatter(text)
    body, why, how = _split_body(rest)
    description = _one_line(str(frontmatter.get("description") or ""))
    parts = (
        f"Summary: {description}" if description else "",
        body,
        f"Why: {why}" if why else "",
        f"How to apply: {how}" if how else "",
    )
    return "\n\n".join(part for part in parts if part)


@pure
def index_titles(index_text: str | None) -> dict[str, str]:
    """The title the index gives each note file it points at, by file name."""
    title_by_file: dict[str, str] = {}
    for line in (index_text or "").splitlines():
        pointer = _POINTER_LINE.match(line)
        if pointer is not None:
            title_by_file[pointer["file"]] = pointer["title"].strip()
    return title_by_file


@contextmanager
def _temp_file_beside(path: Path) -> Generator[tuple[int, Path]]:
    """A new temp file in ``path``'s folder (its descriptor and path), removed if it is still there after."""
    handle, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        yield handle, Path(temp_name)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp makes the file owner-only; keep the mode the file had, or the folder's usual one.
    mode = (
        path.stat().st_mode & _PERMISSION_BITS if path.exists() else _DEFAULT_NOTE_MODE
    )
    with _temp_file_beside(path) as (handle, temp_path):
        with os.fdopen(handle, "w") as temp_file:
            temp_file.write(text)
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)


def _check_draft(draft: NoteDraft) -> None:
    """Refuse a draft the editor cannot save, in the sentence the editor shows.

    Not pydantic validators on ``NoteDraft``: the shared request parser reports a failed field as
    "field 'title': Value error, ...", and the editor shows these sentences to the person as they are.
    """
    if not draft.title.strip():
        raise InvalidNoteError("Give the note a title.")
    # A bracket would end the index line's link text early, so the line would no longer point at the note.
    if "[" in draft.title or "]" in draft.title:
        raise InvalidNoteError(
            "Titles can't contain square brackets. Remove them to save."
        )
    if not draft.summary.strip():
        raise InvalidNoteError("Add a one-line summary.")
    if not (draft.body.strip() or draft.why.strip() or draft.how.strip()):
        raise InvalidNoteError("Write what should be remembered.")


@pure
def pointer_line(title: str, file_name: str, summary: str) -> str:
    """The index line pointing at a note: ``- [Title](file.md) — summary``."""
    return f"- [{_one_line(title)}]({file_name}) — {_one_line(summary)}"


@pure
def _link_rewriter(old_name: str, new_name: str) -> Callable[[str], str]:
    """A function rewriting every ``[[old_name]]`` link in a text to ``[[new_name]]``."""
    if not old_name:
        return lambda text: text
    old_link = re.compile(r"\[\[\s*" + re.escape(old_name) + r"\s*\]\]")
    return lambda text: old_link.sub(f"[[{new_name}]]", text)


@pure
def _set_pointer(
    lines: Sequence[str], file_name: str, pointer: str | None
) -> list[str]:
    """``lines`` with the pointer line for ``file_name`` replaced (or removed, for ``None``), appended if absent."""
    updated: list[str] = []
    is_found = False
    for line in lines:
        match = _POINTER_LINE.match(line)
        if match and match["file"] == file_name:
            if pointer is not None and not is_found:
                updated.append(pointer)
            is_found = True
            continue
        updated.append(line)
    if not is_found and pointer is not None:
        updated.append(pointer)
    return updated


@pure
def _without_first(lines: Sequence[str], removed_line: str) -> list[str]:
    """``lines`` without the first one that reads ``removed_line``."""
    if removed_line not in lines:
        return list(lines)
    position = list(lines).index(removed_line)
    return [*lines[:position], *lines[position + 1 :]]


def _modified_stamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _file_text(
    name: str, draft: NoteDraft, existing_frontmatter: Mapping[str, Any]
) -> str:
    """The note file for ``draft``, keeping the frontmatter's other keys (Claude's bookkeeping)."""
    old_metadata = existing_frontmatter.get("metadata")
    kept_metadata = dict(old_metadata) if isinstance(old_metadata, dict) else {}
    stamp = {"modified": _modified_stamp()} if "modified" in kept_metadata else {}
    metadata = {**kept_metadata, "type": NoteKind(draft.kind).value, **stamp}
    kept = {key: value for key, value in existing_frontmatter.items() if key != "type"}
    frontmatter = {
        **kept,
        "name": name,
        "description": _one_line(draft.summary),
        "metadata": metadata,
    }
    header = yaml.safe_dump(
        frontmatter, sort_keys=False, allow_unicode=True, width=_YAML_LINE_WIDTH
    ).strip()
    return f"---\n{header}\n---\n\n{_join_body(draft.body, draft.why, draft.how)}"


@pure
def _note_kind(frontmatter: Mapping[str, Any]) -> str:
    metadata = frontmatter.get("metadata")
    kind = str(metadata.get("type", "")) if isinstance(metadata, dict) else ""
    return kind or str(frontmatter.get("type") or _DEFAULT_KIND)


@pure
def _links(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(link.strip() for link in _LINK.findall(text)))


@pure
def _loose_title(text: str) -> str:
    if len(text) <= _MAX_LOOSE_TITLE_LENGTH:
        return text
    return text[: _MAX_LOOSE_TITLE_LENGTH - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


@pure
def _loose_note(line: str) -> Note | None:
    """The note a line written straight into the index stands for, or None for any other line."""
    if _POINTER_LINE.match(line):
        return None
    match = _LOOSE_LINE.match(line)
    if match is None:
        return None
    text = match["text"].strip()
    return Note(
        id=NoteId(_LOOSE_ID_PREFIX + _text_version(line)),
        name="",
        file_name="",
        title=_loose_title(text),
        summary=_one_line(text),
        kind=_DEFAULT_KIND,
        body=text,
        why="",
        how="",
        links=_links(text),
        raw=line,
        version=_text_version(line),
        is_index_only=True,
    )


@pure
def _index_entry(
    line: str,
    number: int,
    note_by_file: Mapping[str, Note],
    loose_note_by_line: Mapping[str, Note],
) -> IndexEntry:
    """One non-blank index line resolved against the notes."""
    stripped = line.strip()
    length = len(line)
    is_too_long = length > INDEX_LINE_LIMIT
    heading = _HEADING_LINE.match(stripped)
    pointer = _POINTER_LINE.match(line)
    if heading:
        return IndexEntry(
            kind=IndexEntryKind.HEADING,
            text=heading[2].strip(),
            level=len(heading[1]),
            line_number=number,
            length=length,
            is_too_long=is_too_long,
        )
    if pointer:
        target = Path(pointer["file"]).name
        note = note_by_file.get(target)
        hook = (pointer["hook"] or "").strip()
        if note is None:
            return IndexEntry(
                kind=IndexEntryKind.MISSING,
                text=pointer["title"].strip() or target,
                hook=hook,
                file_name=target,
                line_number=number,
                length=length,
                is_too_long=is_too_long,
            )
        return IndexEntry(
            kind=IndexEntryKind.NOTE,
            text=pointer["title"].strip() or note.title,
            hook=hook,
            note_id=note.id,
            file_name=target,
            line_number=number,
            length=length,
            is_too_long=is_too_long,
        )
    if line in loose_note_by_line:
        note = loose_note_by_line[line]
        return IndexEntry(
            kind=IndexEntryKind.LOOSE,
            text=note.title,
            note_id=note.id,
            line_number=number,
            length=length,
            is_too_long=is_too_long,
        )
    return IndexEntry(
        kind=IndexEntryKind.TEXT,
        text=stripped,
        line_number=number,
        length=length,
        is_too_long=is_too_long,
    )


class NoteStore:
    """The memory folder: lists, saves, adds and deletes notes, keeping the index in step."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._lock = threading.Lock()

    @property
    def index_path(self) -> Path:
        return self.folder / INDEX_FILE_NAME

    # Reading

    def _index_lines(self) -> list[str]:
        if not self.index_path.is_file():
            return []
        return self.index_path.read_text().splitlines()

    def _note_files(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        return sorted(
            path
            for path in self.folder.glob(f"*{NOTE_SUFFIX}")
            if path.is_file() and path.name not in _NON_NOTE_FILE_NAMES
        )

    def list_notes(self) -> list[Note]:
        lines = self._index_lines()
        title_by_file: dict[str, str] = {}
        position_by_file: dict[str, int] = {}
        for position, line in enumerate(lines):
            match = _POINTER_LINE.match(line)
            if match:
                title_by_file.setdefault(match["file"], match["title"].strip())
                position_by_file.setdefault(match["file"], position)
        file_notes = [
            (
                position_by_file.get(path.name, len(lines)),
                self._read_file_note(path, title_by_file.get(path.name, "")),
            )
            for path in self._note_files()
        ]
        loose_notes = [
            (position, loose)
            for position, line in enumerate(lines)
            if (loose := _loose_note(line)) is not None
        ]
        ordered = sorted([*file_notes, *loose_notes], key=lambda pair: pair[0])
        return [note for _, note in ordered]

    def outline(self, notes: Sequence[Note]) -> IndexOutline:
        """The index line by line, each line resolved against ``notes`` (as ``list_notes`` returned them)."""
        raw = self.index_path.read_text() if self.index_path.is_file() else ""
        note_by_file = {note.file_name: note for note in notes if note.file_name}
        loose_note_by_line = {note.raw: note for note in notes if note.is_index_only}
        entries = tuple(
            _index_entry(line, number, note_by_file, loose_note_by_line)
            for number, line in enumerate(raw.splitlines(), start=1)
            if line.strip()
        )
        listed = {
            entry.note_id for entry in entries if entry.kind is IndexEntryKind.NOTE
        }
        unlisted = tuple(
            note.id
            for note in notes
            if not note.is_index_only and note.id not in listed
        )
        return IndexOutline(
            entries=entries,
            unlisted_note_ids=unlisted,
            raw=raw,
            is_present=self.index_path.is_file(),
        )

    def _read_file_note(self, path: Path, index_title: str) -> Note:
        text = path.read_text()
        frontmatter, rest = _parse_frontmatter(text)
        name = str(frontmatter.get("name") or path.stem)
        body, why, how = _split_body(rest)
        return Note(
            id=NoteId(path.stem),
            name=name,
            file_name=path.name,
            title=index_title or note_title(name),
            summary=_one_line(str(frontmatter.get("description") or "")),
            kind=_note_kind(frontmatter),
            body=body,
            why=why,
            how=how,
            links=_links(rest),
            raw=text,
            version=_text_version(text),
            is_index_only=False,
        )

    def _find(self, note_id: str) -> Note:
        for note in self.list_notes():
            if note.id == note_id:
                return note
        raise NoteNotFoundError(note_id)

    # Writing

    def _unused_name(self, title: str, own_stem: str) -> str:
        """A link name for ``title`` no other note file uses (``own_stem``, the note being renamed, may keep its own)."""
        base = _slugify(title)
        taken = {path.stem for path in self._note_files()} - {own_stem}
        candidates = (
            base if suffix == 1 else f"{base}-{suffix}" for suffix in itertools.count(1)
        )
        return next(
            name
            for name in candidates
            if name not in taken and f"{name}{NOTE_SUFFIX}" not in _NON_NOTE_FILE_NAMES
        )

    def _write_index(self, lines: Sequence[str]) -> None:
        _write_atomically(self.index_path, "\n".join(lines).rstrip("\n") + "\n")

    def _write_new_note_file(self, draft: NoteDraft) -> str:
        """Write ``draft`` as a new note under an unused name, and answer that name."""
        name = self._unused_name(draft.title, own_stem="")
        _write_atomically(
            self.folder / f"{name}{NOTE_SUFFIX}", _file_text(name, draft, {})
        )
        return name

    def create(self, draft: NoteDraft) -> Note:
        _check_draft(draft)
        with self._lock:
            name = self._write_new_note_file(draft)
            file_name = f"{name}{NOTE_SUFFIX}"
            pointer = pointer_line(draft.title, file_name, draft.summary)
            self._write_index(_set_pointer(self._index_lines(), file_name, pointer))
            return self._find(name)

    def update(self, note_id: str, version: str, draft: NoteDraft) -> Note:
        _check_draft(draft)
        with self._lock:
            note = self._find(note_id)
            if note.version != version:
                raise NoteChangedError(note_id)
            lines = self._index_lines()
            if note.is_index_only:
                name = self._write_new_note_file(draft)
                pointer = pointer_line(
                    draft.title, f"{name}{NOTE_SUFFIX}", draft.summary
                )
                self._write_index(
                    [pointer if line == note.raw else line for line in lines]
                )
                return self._find(name)
            existing_frontmatter, _ = _parse_frontmatter(note.raw)
            # A new title gives the note the link name it would get as a new note, so its file
            # name keeps reading as its title.
            if _one_line(draft.title) != note.title:
                new_name = self._unused_name(draft.title, own_stem=note.id)
                if new_name != note.id:
                    return self._rename(
                        note, new_name, draft, existing_frontmatter, lines
                    )
            _write_atomically(
                self.folder / note.file_name,
                _file_text(note.name, draft, existing_frontmatter),
            )
            # The index line is rewritten only when what it shows changed, so a hand-written line survives a body edit.
            has_pointer = any(
                (match := _POINTER_LINE.match(line)) and match["file"] == note.file_name
                for line in lines
            )
            is_shown_changed = (
                _one_line(draft.title) != note.title
                or _one_line(draft.summary) != note.summary
            )
            if not has_pointer or is_shown_changed:
                pointer = pointer_line(draft.title, note.file_name, draft.summary)
                self._write_index(_set_pointer(lines, note.file_name, pointer))
            return self._find(note_id)

    def _rename(
        self,
        note: Note,
        new_name: str,
        draft: NoteDraft,
        existing_frontmatter: Mapping[str, Any],
        lines: Sequence[str],
    ) -> Note:
        """Save ``note`` under ``new_name``: a new file, its index line pointed at it, every ``[[old-name]]`` link rewritten."""
        relink = _link_rewriter(note.name, new_name)
        new_file_name = f"{new_name}{NOTE_SUFFIX}"
        _write_atomically(
            self.folder / new_file_name,
            relink(_file_text(new_name, draft, existing_frontmatter)),
        )
        for path in self._note_files():
            if path.name in (note.file_name, new_file_name):
                continue
            text = path.read_text()
            relinked = relink(text)
            if relinked != text:
                _write_atomically(path, relinked)
        pointer = pointer_line(draft.title, new_file_name, draft.summary)
        self._write_index(
            _set_pointer([relink(line) for line in lines], note.file_name, pointer)
        )
        (self.folder / note.file_name).unlink(missing_ok=True)
        return self._find(new_name)

    def delete(self, note_id: str, version: str) -> None:
        with self._lock:
            note = self._find(note_id)
            if note.version != version:
                raise NoteChangedError(note_id)
            lines = self._index_lines()
            if note.is_index_only:
                self._write_index(_without_first(lines, note.raw))
                return
            (self.folder / note.file_name).unlink(missing_ok=True)
            self._write_index(_set_pointer(lines, note.file_name, None))
