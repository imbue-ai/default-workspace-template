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
import os
import re
import tempfile
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Literal

import yaml
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

INDEX_FILE_NAME = "MEMORY.md"
# Files in the folder that are not notes.
NON_NOTE_FILE_NAMES = frozenset({INDEX_FILE_NAME, "README.md"})
NoteKind = Literal["user", "feedback", "project", "reference"]
NOTE_KINDS: tuple[str, ...] = ("user", "feedback", "project", "reference")
LOOSE_ID_PREFIX = "index-"

_POINTER_LINE = re.compile(r"^\s*[-*]\s+\[(?P<title>[^\]]*)\]\((?P<file>[^)\s]+\.md)\)\s*(?:[—–-]+\s*(?P<hook>.*))?$")
_LOOSE_LINE = re.compile(r"^\s*[-*]\s+(?P<text>\S.*)$")
_FRONTMATTER = re.compile(r"\A---\s*\n(?P<yaml>.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_WHY_MARKER = re.compile(r"^\*\*Why:\*\*[ \t]*", re.MULTILINE)
_HOW_MARKER = re.compile(r"^\*\*How to apply:\*\*[ \t]*", re.MULTILINE)
_LINK = re.compile(r"\[\[([^\]\n]+)\]\]")
_SLUG_JUNK = re.compile(r"[^a-z0-9]+")


class Note(FrozenModel):
    """One remembered note, as the editor shows it."""

    id: str = Field(description="Stable id: the note's name, or 'index-<hash>' for a note written straight into the index")
    name: str = Field(description="The note's short link name (other notes link it as [[name]]); empty for an index-only note")
    file_name: str = Field(description="The note's file in the memory folder; empty for an index-only note")
    title: str = Field(description="The note's title, as the index lists it")
    summary: str = Field(description="The one-line summary the agent reads to judge relevance")
    kind: str = Field(description="One of user, feedback, project, reference (or whatever the file says)")
    body: str = Field(description="What the agent should remember, without the Why and How to apply parts")
    why: str = Field(description="Why the note holds (feedback and project notes)")
    how: str = Field(description="How the agent should apply it (feedback and project notes)")
    links: tuple[str, ...] = Field(description="Names of the notes this one links to with [[name]]")
    raw: str = Field(description="The note exactly as saved: the file's text, or the index line")
    version: str = Field(description="A hash of the saved text; changes name it so a note edited elsewhere is not overwritten")
    is_index_only: bool = Field(description="Whether the note is a line in the index with no file of its own yet")


IndexEntryKind = Literal["heading", "note", "missing", "loose", "text"]


class IndexEntry(FrozenModel):
    """One line of the index, as the navigation mirrors it."""

    kind: IndexEntryKind = Field(
        description="heading; note (points at a note file); missing (points at a file that does not exist); "
        "loose (a note written straight into the index); text (any other line)"
    )
    text: str = Field(description="What the line shows: the heading, the note's title, or the line's text")
    hook: str = Field(default="", description="The summary the index line gives after the title")
    level: int = Field(default=0, description="A heading's level (1 for '#'); 0 otherwise")
    note_id: str = Field(default="", description="The id of the note the line stands for; empty for headings, text and missing files")
    file_name: str = Field(default="", description="The file the line points at; empty when it points at none")
    line_number: int = Field(description="The line's number in the index, from 1")


class IndexOutline(FrozenModel):
    """The index as the navigation mirrors it, plus the notes it leaves out."""

    entries: tuple[IndexEntry, ...] = Field(description="The index's non-blank lines, in order")
    unlisted_note_ids: tuple[str, ...] = Field(description="Notes with a file but no line in the index")
    raw: str = Field(description="The index exactly as saved")
    exists: bool = Field(description="Whether the index file exists")


class NoteDraft(FrozenModel):
    """A note's content as the editor submits it."""

    title: str = Field(description="The note's title")
    summary: str = Field(description="The one-line summary")
    kind: NoteKind = Field(description="One of user, feedback, project, reference")
    body: str = Field(description="What the agent should remember")
    why: str = Field(default="", description="Why the note holds")
    how: str = Field(default="", description="How the agent should apply it")


class NoteNotFoundError(Exception):
    """No note has that id."""


class NoteChangedError(Exception):
    """The note was changed elsewhere since the editor loaded it."""


class InvalidNoteError(Exception):
    """The submitted note cannot be saved as it is."""


def text_version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def slugify(title: str) -> str:
    slug = _SLUG_JUNK.sub("-", title.lower()).strip("-")
    return "-".join(slug.split("-")[:8]) or "note"


def humanize(name: str) -> str:
    words = name.replace("_", "-").split("-")
    text = " ".join(w for w in words if w)
    return text[:1].upper() + text[1:]


def split_body(text: str) -> tuple[str, str, str]:
    """A note's text after the frontmatter as (body, why, how)."""
    why_match = _WHY_MARKER.search(text)
    how_match = _HOW_MARKER.search(text)
    cut_points = [m.start() for m in (why_match, how_match) if m is not None]
    body_end = min(cut_points) if cut_points else len(text)
    body = text[:body_end].strip()
    why = ""
    how = ""
    if why_match is not None:
        why_end = how_match.start() if how_match is not None and how_match.start() > why_match.start() else len(text)
        why = text[why_match.end() : why_end].strip()
    if how_match is not None:
        how_end = why_match.start() if why_match is not None and why_match.start() > how_match.start() else len(text)
        how = text[how_match.end() : how_end].strip()
    return body, why, how


def join_body(body: str, why: str, how: str) -> str:
    text = body.strip()
    reasons = []
    if why.strip():
        reasons.append(f"**Why:** {why.strip()}")
    if how.strip():
        reasons.append(f"**How to apply:** {how.strip()}")
    if reasons:
        text = (text + "\n\n" if text else "") + "\n\n".join(reasons)
    return text + "\n"


def one_line(text: str) -> str:
    return " ".join(text.split())


def readable_text(file_name: str, text: str | None) -> str | None:
    """A memory file as a person reads it, for showing what a change did to it.

    A note keeps its summary and its text (with any Why and How to apply), and drops the
    frontmatter's link name and bookkeeping. The index shows each line as "Title — summary",
    a heading as its words, and a line written straight into it as that line's text.
    """
    if text is None:
        return None
    if file_name == INDEX_FILE_NAME:
        lines = []
        for line in text.splitlines():
            pointer = _POINTER_LINE.match(line)
            loose = _LOOSE_LINE.match(line)
            if pointer is not None:
                hook = (pointer["hook"] or "").strip()
                lines.append(f"{pointer['title'].strip()} — {hook}" if hook else pointer["title"].strip())
            elif line.lstrip().startswith("#"):
                lines.append(line.lstrip("#").strip())
            elif loose is not None:
                lines.append(loose["text"].strip())
            else:
                lines.append(line)
        return "\n".join(lines).strip()
    frontmatter, rest = NoteStore._parse_frontmatter(text)
    body, why, how = split_body(rest)
    parts = []
    description = one_line(str(frontmatter.get("description") or ""))
    if description:
        parts.append(f"Summary: {description}")
    if body:
        parts.append(body)
    if why:
        parts.append(f"Why: {why}")
    if how:
        parts.append(f"How to apply: {how}")
    return "\n\n".join(parts)


def index_titles(index_text: str | None) -> dict[str, str]:
    """The title the index gives each note file it points at, by file name."""
    titles: dict[str, str] = {}
    for line in (index_text or "").splitlines():
        pointer = _POINTER_LINE.match(line)
        if pointer is not None:
            titles[pointer["file"]] = pointer["title"].strip()
    return titles


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        # mkstemp makes the file owner-only; keep the mode the file had, or the folder's usual one.
        os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
        return sorted(p for p in self.folder.glob("*.md") if p.is_file() and p.name not in NON_NOTE_FILE_NAMES)

    def list_notes(self) -> list[Note]:
        lines = self._index_lines()
        titles: dict[str, str] = {}
        order: dict[str, int] = {}
        for position, line in enumerate(lines):
            match = _POINTER_LINE.match(line)
            if match:
                titles.setdefault(match["file"], match["title"].strip())
                order.setdefault(match["file"], position)
        notes_with_position: list[tuple[int, Note]] = []
        for path in self._note_files():
            note = self._read_file_note(path, titles.get(path.name, ""))
            notes_with_position.append((order.get(path.name, len(lines)), note))
        for position, line in enumerate(lines):
            loose = self._loose_note(line)
            if loose is not None:
                notes_with_position.append((position, loose))
        return [note for _, note in sorted(notes_with_position, key=lambda pair: pair[0])]

    def outline(self, notes: list[Note]) -> IndexOutline:
        """The index line by line, each line resolved against ``notes`` (as ``list_notes`` returned them)."""
        raw = self.index_path.read_text() if self.index_path.is_file() else ""
        by_file = {note.file_name: note for note in notes if note.file_name}
        loose_by_line = {note.raw: note for note in notes if note.is_index_only}
        entries: list[IndexEntry] = []
        listed: set[str] = set()
        for number, line in enumerate(raw.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
            pointer = _POINTER_LINE.match(line)
            if heading:
                entries.append(IndexEntry(kind="heading", text=heading[2].strip(), level=len(heading[1]), line_number=number))
            elif pointer:
                target = Path(pointer["file"]).name
                note = by_file.get(target)
                hook = (pointer["hook"] or "").strip()
                if note is not None:
                    listed.add(note.id)
                    entries.append(
                        IndexEntry(kind="note", text=pointer["title"].strip() or note.title, hook=hook, note_id=note.id, file_name=target, line_number=number)
                    )
                else:
                    entries.append(IndexEntry(kind="missing", text=pointer["title"].strip() or target, hook=hook, file_name=target, line_number=number))
            elif line in loose_by_line:
                note = loose_by_line[line]
                entries.append(IndexEntry(kind="loose", text=note.title, note_id=note.id, line_number=number))
            else:
                entries.append(IndexEntry(kind="text", text=stripped, line_number=number))
        unlisted = tuple(note.id for note in notes if not note.is_index_only and note.id not in listed)
        return IndexOutline(entries=tuple(entries), unlisted_note_ids=unlisted, raw=raw, exists=self.index_path.is_file())

    def _read_file_note(self, path: Path, index_title: str) -> Note:
        text = path.read_text()
        frontmatter, rest = self._parse_frontmatter(text)
        name = str(frontmatter.get("name") or path.stem)
        metadata = frontmatter.get("metadata")
        kind = str(metadata.get("type", "")) if isinstance(metadata, dict) else ""
        kind = kind or str(frontmatter.get("type") or "user")
        body, why, how = split_body(rest)
        return Note(
            id=path.stem,
            name=name,
            file_name=path.name,
            title=index_title or humanize(name),
            summary=one_line(str(frontmatter.get("description") or "")),
            kind=kind,
            body=body,
            why=why,
            how=how,
            links=tuple(dict.fromkeys(link.strip() for link in _LINK.findall(rest))),
            raw=text,
            version=text_version(text),
            is_index_only=False,
        )

    def _loose_note(self, line: str) -> Note | None:
        if _POINTER_LINE.match(line):
            return None
        match = _LOOSE_LINE.match(line)
        if match is None:
            return None
        text = match["text"].strip()
        title = text if len(text) <= 60 else text[:57].rstrip() + "..."
        return Note(
            id=LOOSE_ID_PREFIX + text_version(line),
            name="",
            file_name="",
            title=title,
            summary=one_line(text),
            kind="user",
            body=text,
            why="",
            how="",
            links=tuple(dict.fromkeys(link.strip() for link in _LINK.findall(text))),
            raw=line,
            version=text_version(line),
            is_index_only=True,
        )

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
        match = _FRONTMATTER.match(text)
        if match is None:
            return {}, text
        try:
            loaded = yaml.safe_load(match["yaml"])
        except yaml.YAMLError:
            return {}, text
        if not isinstance(loaded, dict):
            return {}, text
        return loaded, text[match.end() :]

    def _find(self, note_id: str) -> Note:
        for note in self.list_notes():
            if note.id == note_id:
                return note
        raise NoteNotFoundError(note_id)

    # Writing

    @staticmethod
    def _check_draft(draft: NoteDraft) -> None:
        if not draft.title.strip():
            raise InvalidNoteError("Give the note a title.")
        if not draft.summary.strip():
            raise InvalidNoteError("Add a one-line summary.")
        if not (draft.body.strip() or draft.why.strip() or draft.how.strip()):
            raise InvalidNoteError("Write what should be remembered.")

    def _unused_name(self, title: str) -> str:
        base = slugify(title)
        taken = {p.stem for p in self._note_files()}
        name = base
        suffix = 2
        while name in taken or f"{name}.md" in NON_NOTE_FILE_NAMES:
            name = f"{base}-{suffix}"
            suffix += 1
        return name

    def _file_text(self, name: str, draft: NoteDraft, existing_frontmatter: dict[str, Any]) -> str:
        frontmatter = dict(existing_frontmatter)
        frontmatter["name"] = name
        frontmatter["description"] = one_line(draft.summary)
        metadata = frontmatter.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata["type"] = draft.kind
        if "modified" in metadata:
            metadata["modified"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        frontmatter["metadata"] = metadata
        frontmatter.pop("type", None)
        header = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=10_000).strip()
        return f"---\n{header}\n---\n\n{join_body(draft.body, draft.why, draft.how)}"

    @staticmethod
    def _pointer_line(title: str, file_name: str, summary: str) -> str:
        return f"- [{one_line(title).replace(']', ')')}]({file_name}) — {one_line(summary)}"

    def _write_index(self, lines: list[str]) -> None:
        _write_atomically(self.index_path, "\n".join(lines).rstrip("\n") + "\n")

    def _set_pointer(self, lines: list[str], file_name: str, pointer: str | None) -> list[str]:
        """``lines`` with the pointer line for ``file_name`` replaced (or removed, for ``None``), appended if absent."""
        updated: list[str] = []
        found = False
        for line in lines:
            match = _POINTER_LINE.match(line)
            if match and match["file"] == file_name:
                if pointer is not None and not found:
                    updated.append(pointer)
                found = True
                continue
            updated.append(line)
        if not found and pointer is not None:
            updated.append(pointer)
        return updated

    def create(self, draft: NoteDraft) -> Note:
        self._check_draft(draft)
        with self._lock:
            name = self._unused_name(draft.title)
            file_name = f"{name}.md"
            _write_atomically(self.folder / file_name, self._file_text(name, draft, {}))
            lines = self._set_pointer(self._index_lines(), file_name, self._pointer_line(draft.title, file_name, draft.summary))
            self._write_index(lines)
            return self._find(name)

    def update(self, note_id: str, version: str, draft: NoteDraft) -> Note:
        self._check_draft(draft)
        with self._lock:
            note = self._find(note_id)
            if note.version != version:
                raise NoteChangedError(note_id)
            lines = self._index_lines()
            if note.is_index_only:
                name = self._unused_name(draft.title)
                file_name = f"{name}.md"
                _write_atomically(self.folder / file_name, self._file_text(name, draft, {}))
                pointer = self._pointer_line(draft.title, file_name, draft.summary)
                lines = [pointer if line == note.raw else line for line in lines]
                self._write_index(lines)
                return self._find(name)
            existing_frontmatter, _ = self._parse_frontmatter(note.raw)
            _write_atomically(self.folder / note.file_name, self._file_text(note.name, draft, existing_frontmatter))
            # The index line is rewritten only when what it shows changed, so a hand-written line survives a body edit.
            has_pointer = any((m := _POINTER_LINE.match(line)) and m["file"] == note.file_name for line in lines)
            if not has_pointer or one_line(draft.title) != note.title or one_line(draft.summary) != note.summary:
                lines = self._set_pointer(lines, note.file_name, self._pointer_line(draft.title, note.file_name, draft.summary))
                self._write_index(lines)
            return self._find(note_id)

    def delete(self, note_id: str, version: str) -> None:
        with self._lock:
            note = self._find(note_id)
            if note.version != version:
                raise NoteChangedError(note_id)
            lines = self._index_lines()
            if note.is_index_only:
                removed = False
                kept = []
                for line in lines:
                    if not removed and line == note.raw:
                        removed = True
                        continue
                    kept.append(line)
                self._write_index(kept)
                return
            (self.folder / note.file_name).unlink(missing_ok=True)
            self._write_index(self._set_pointer(lines, note.file_name, None))
