"""The record of every change an agent made to its memory folder, and undoing one.

Stdlib-only (with the stdlib-only ``chat_background_tasks``): Claude's hooks
(``system/scripts/agent_memory_change_log.py``) import this from the bare ``python3``, and the
chat app imports it to show each change as a card in the conversation with an Undo button.

How a change is recorded, per chat session (Claude's ``session_id``):

* ``start_turn`` (UserPromptSubmit): save the folder's files as the session's baseline and
  start a new turn, so an edit the user made between turns is never charged to the agent.
* ``record_tool_call`` (PostToolUse): when the tool call mentions the memory folder, compare
  the folder with the baseline. Each note gets ONE change record per turn -- its text before
  the turn's first change and after its latest -- and the index (``MEMORY.md``) is split by
  line: a pointer line (``- [Title](x.md) — hook``) goes with the record of the note it points
  at, so a note and its index line undo together, and every other index line (a heading, a
  line written straight into the index) goes into the turn's one index-only record. Each
  record lists the tool calls that made it, which is where the chat shows its card.

A record's index part is a ``ChangedFile`` with ``is_index_lines`` set (on disk
``{"name": "MEMORY.md", "part": "lines", "before", "after"}``): only its own lines, joined
(``None``: none), and its Undo rewrites only those lines in the index as it stands. A record
written before records were split per note has whole-file entries only, and is shown and
undone whole.

A change made by one session (or undone through the chat) moves every other session's
baseline past it, so it is never charged to a second chat.

On disk, under the chat app's data (``data/.apps/chat/memory_changes/``):

* ``<change id>.json`` -- one change record (``MemoryChange.to_json``);
* ``state/baseline-<session>.json`` and ``state/turn-<session>.json`` -- the bookkeeping.

Both are bounded: a session's bookkeeping goes once the session has not started a turn for
``STALE_SESSION_DAYS`` (its next turn, if it ever has one, writes it afresh), and only the
newest ``MAX_CHANGE_RECORDS`` change records are kept.
"""

import difflib
import json
import logging
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, NamedTuple, Self

from chat_background_tasks.markers import json_object, main_checkout, own_repo_root

logger = logging.getLogger(__name__)

MEMORY_DIR_ENV: Final = "AGENT_MEMORY_DIR"
CHANGES_DIR_ENV: Final = "AGENT_MEMORY_CHANGES_DIR"
DEFAULT_MEMORY_DIR: Final = Path("data") / "memories"
# The chat app's data dir plus one segment: the chat shows and undoes the changes through this library.
DEFAULT_CHANGES_DIR: Final = Path("data") / ".apps" / "chat" / "memory_changes"
INDEX_FILE_NAME: Final = "MEMORY.md"
NOTE_SUFFIX: Final = ".md"
# Retention: a session idle this long loses its bookkeeping, and the log keeps this many changes.
STALE_SESSION_DAYS: Final = 30
MAX_CHANGE_RECORDS: Final = 1000
_SECONDS_PER_DAY: Final = 86_400
STALE_SESSION_SECONDS: Final = STALE_SESSION_DAYS * _SECONDS_PER_DAY

# The start of a pointer line of the index: its title and the note file it points at. notes.py
# extends it with the summary after the link.
POINTER_LINE_START: Final = re.compile(
    r"^\s*[-*]\s+\[(?P<title>[^\]]*)\]\((?P<file>[^)\s]+\.md)\)"
)
# The key of the index-only record: index lines that point at no note.
_INDEX_ONLY: Final = ""
# How a change record marks an index part holding only its own lines, on disk.
LINES_PART: Final = "lines"
# A change id: when it was made (so ids sort by time), then a random part.
_CHANGE_ID_TIME_FORMAT: Final = "%Y%m%dT%H%M%S%f"
_TIMESPEC: Final = "seconds"
_STATE_DIR_NAME: Final = "state"
_JSON_SUFFIX: Final = ".json"
_JSON_INDENT: Final = 2
_UNKNOWN_SESSION_NAME: Final = "unknown"
_SAFE_NAME_EXTRA_CHARS: Final = frozenset("-_")

# What Claude's hook tells the agent after a tool call changed memory. It lands in the agent's
# transcript as the hook's additional context, which is where the chat reads it back to draw
# the change's card: format and parse live together here so the two never drift.
AGENT_NOTE_PREFIX: Final = "MEMORY CHANGED"
_AGENT_NOTE_RE: Final = re.compile(
    rf"^{AGENT_NOTE_PREFIX} \((?P<id>[A-Za-z0-9_-]+)\): so far this reply you (?P<summary>.+?)\. The chat shows"
)


class AgentMemoryError(Exception):
    """Base of every error the agent_memory library raises."""


class ChangeNotFoundError(AgentMemoryError, KeyError):
    """No change record has that id."""

    def __init__(self, change_id: str) -> None:
        self.change_id = change_id
        super().__init__(change_id)


class ChangedSinceError(AgentMemoryError, ValueError):
    """A file the change touched was changed again since; undoing would lose that."""

    def __init__(self, names: Sequence[str]) -> None:
        self.names = list(names)
        super().__init__(", ".join(names))


class AgentNote(NamedTuple):
    """One change a hook's note to the agent names, and the note's line itself."""

    change_id: str
    summary: str
    line: str


def format_agent_note(change_id: str, summary: str) -> str:
    return (
        f"{AGENT_NOTE_PREFIX} ({change_id}): so far this reply you {summary}. The chat shows "
        "the user this as one card with the changed lines and an Undo button, so mention it in one "
        "short line; do not repeat the text."
    )


def parse_agent_note(text: str) -> AgentNote | None:
    """The change a hook note names, or None when ``text`` is not one."""
    line = text.strip()
    match = _AGENT_NOTE_RE.match(line)
    if match is None:
        return None
    return AgentNote(change_id=match["id"], summary=match["summary"], line=line)


def parse_agent_notes(text: str) -> list[AgentNote]:
    """Every change a hook's context names, one per line."""
    parsed = (parse_agent_note(line) for line in text.splitlines())
    return [note for note in parsed if note is not None]


def _is_optional_text(value: object) -> bool:
    return value is None or isinstance(value, str)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_time(moment: datetime) -> str:
    return moment.isoformat(timespec=_TIMESPEC)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ChangedFile(NamedTuple):
    """One file a change touched: its text before and after (``None``: absent).

    For a record's index part (``is_index_lines``), only that record's own lines of the index.
    """

    name: str
    before: str | None
    after: str | None
    is_index_lines: bool

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name}
        if self.is_index_lines:
            data["part"] = LINES_PART
        data["before"] = self.before
        data["after"] = self.after
        return data

    @classmethod
    def from_json(cls, data: object) -> Self | None:
        """The file entry a record's JSON holds, or None when it is not a well-formed one."""
        fields = json_object(data)
        if fields is None:
            return None
        name, before, after = (
            fields.get("name"),
            fields.get("before"),
            fields.get("after"),
        )
        if not (isinstance(name, str) and name):
            return None
        if not (_is_optional_text(before) and _is_optional_text(after)):
            return None
        return cls(
            name=name,
            before=before if isinstance(before, str) else None,
            after=after if isinstance(after, str) else None,
            is_index_lines=fields.get("part") == LINES_PART,
        )


class MemoryChange(NamedTuple):
    """What one agent turn changed in one note (with its index lines), or in the index's other lines."""

    id: str
    time: datetime
    session_id: str
    chat_id: str
    agent_name: str
    # The note file a per-note record belongs to; None for the index-only record and for a
    # record from before records were split per note.
    note: str | None
    tool_use_ids: tuple[str, ...]
    files: tuple[ChangedFile, ...]
    summary: str
    undone_at: datetime | None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "time": _format_time(self.time),
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "agent_name": self.agent_name,
            "note": self.note,
            "tool_use_ids": list(self.tool_use_ids),
            "files": [item.to_json() for item in self.files],
            "undone_at": None
            if self.undone_at is None
            else _format_time(self.undone_at),
            "summary": self.summary,
        }

    @classmethod
    def from_json(cls, data: object) -> Self | None:
        """The change a record's JSON holds, or None when it is not a well-formed record.

        A record from before records were split per note has no ``note``, and one from before
        summaries were stored has none: it gets one from its files.
        """
        fields = json_object(data)
        if fields is None:
            return None
        change_id, moment = fields.get("id"), _parse_time(fields.get("time"))
        raw_files, raw_tool_use_ids = (
            fields.get("files", []),
            fields.get("tool_use_ids", []),
        )
        note, summary = fields.get("note"), fields.get("summary")
        raw_undone_at = fields.get("undone_at")
        if not (isinstance(change_id, str) and change_id) or moment is None:
            return None
        if not (isinstance(raw_files, list) and isinstance(raw_tool_use_ids, list)):
            return None
        files = tuple(ChangedFile.from_json(item) for item in raw_files)
        if any(item is None for item in files) or not all(
            isinstance(tool_use_id, str) for tool_use_id in raw_tool_use_ids
        ):
            return None
        undone_at = None if raw_undone_at is None else _parse_time(raw_undone_at)
        if raw_undone_at is not None and undone_at is None:
            return None
        valid_files = tuple(item for item in files if item is not None)
        valid_note = note if isinstance(note, str) and note else None
        return cls(
            id=change_id,
            time=moment,
            session_id=_text_field(fields, "session_id"),
            chat_id=_text_field(fields, "chat_id"),
            agent_name=_text_field(fields, "agent_name"),
            note=valid_note,
            tool_use_ids=tuple(str(tool_use_id) for tool_use_id in raw_tool_use_ids),
            files=valid_files,
            summary=summary
            if isinstance(summary, str) and summary
            else describe(valid_files, valid_note),
            undone_at=undone_at,
        )


def _text_field(fields: Mapping[str, object], key: str) -> str:
    value = fields.get(key, "")
    return value if isinstance(value, str) else ""


def _safe_name(raw: str) -> str:
    kept = "".join(
        char for char in raw if char.isalnum() or char in _SAFE_NAME_EXTRA_CHARS
    )
    return kept or _UNKNOWN_SESSION_NAME


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(value, indent=_JSON_INDENT), encoding="utf-8")
    temp_path.replace(path)


def _read_json(path: Path) -> dict[str, object] | None:
    """The JSON object at ``path``; None when it is missing, or (logged) unreadable or not an object."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        logger.warning("Cannot read the memory change file %s: %s", path, error)
        return None
    try:
        value = json.loads(text)
    except ValueError as error:
        logger.warning("The memory change file %s is not JSON: %s", path, error)
        return None
    fields = json_object(value)
    if fields is None:
        logger.warning("The memory change file %s does not hold an object", path)
    return fields


def _read_snapshot(path: Path) -> dict[str, str] | None:
    """A saved baseline: each file's name and text."""
    fields = _read_json(path)
    if fields is None:
        return None
    return {name: text for name, text in fields.items() if isinstance(text, str)}


def diff_snapshots(
    before: Mapping[str, str], after: Mapping[str, str]
) -> list[ChangedFile]:
    """Every file whose text differs, with its text before and after (``None``: absent)."""
    return [
        ChangedFile(
            name=name,
            before=before.get(name),
            after=after.get(name),
            is_index_lines=False,
        )
        for name in sorted(set(before) | set(after))
        if before.get(name) != after.get(name)
    ]


def merge_files(
    earlier: Sequence[ChangedFile], later: Sequence[ChangedFile]
) -> tuple[ChangedFile, ...]:
    """One turn's changes as a whole: each file's text before the first change and after the last.

    A file the turn put back as it found it drops out.
    """
    merged = {item.name: item for item in earlier}
    for item in later:
        previous = merged.get(item.name)
        merged[item.name] = (
            item if previous is None else previous._replace(after=item.after)
        )
    return tuple(
        item for _, item in sorted(merged.items()) if item.before != item.after
    )


def _line_key(line: str) -> str:
    """The note file an index line belongs to, or the index-only key for any other line."""
    match = POINTER_LINE_START.match(line)
    return Path(match["file"]).name if match else _INDEX_ONLY


def _lines(text: str | None) -> list[str]:
    return [] if text is None else text.split("\n")


def _joined(lines: Sequence[str]) -> str | None:
    return "\n".join(lines) if lines else None


def index_lines_by_key(text: str | None) -> dict[str, list[str]]:
    """The index's lines grouped by the note each belongs to (the index-only key for the rest)."""
    grouped: dict[str, list[str]] = {}
    for line in _lines(text):
        grouped.setdefault(_line_key(line), []).append(line)
    return grouped


def _insertion_anchor(positions: Sequence[int], old_start: int, end: int) -> int:
    """Where a line added at ``old_start`` of a key's old lines goes in the current text."""
    if old_start < len(positions):
        return positions[old_start]
    if positions:
        return positions[-1] + 1
    return end


def replace_key_lines(
    text: str | None, key: str, old: Sequence[str], new: Sequence[str]
) -> str | None:
    """``text`` with ``key``'s lines (which read ``old``) rewritten to ``new``, every other line kept.

    A line ``new`` adds goes where its neighbour in ``old`` stands, or (when ``key`` has no line
    left) at the end, ahead of the final newline.
    """
    current = _lines(text)
    positions = [
        position for position, line in enumerate(current) if _line_key(line) == key
    ]
    end = len(current) - 1 if current and current[-1] == "" else len(current)
    drops: set[int] = set()
    inserts: dict[int, list[str]] = {}
    matcher = difflib.SequenceMatcher(a=list(old), b=list(new), autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        drops.update(positions[old_start:old_end])
        anchor = _insertion_anchor(positions, old_start, end)
        inserts.setdefault(anchor, []).extend(new[new_start:new_end])
    rewritten: list[str] = []
    for position in range(len(current) + 1):
        rewritten.extend(inserts.get(position, []))
        if position < len(current) and position not in drops:
            rewritten.append(current[position])
    return _joined(rewritten)


def split_by_note(files: Sequence[ChangedFile]) -> dict[str, list[ChangedFile]]:
    """One tool call's file changes, as each record's part: a note's file and its index lines
    under the note's file name, the rest of the index under the index-only key."""
    parts: dict[str, list[ChangedFile]] = {}
    for item in files:
        if item.name != INDEX_FILE_NAME:
            parts.setdefault(item.name, []).append(item)
            continue
        before = index_lines_by_key(item.before)
        after = index_lines_by_key(item.after)
        for key in sorted(set(before) | set(after)):
            if before.get(key, []) != after.get(key, []):
                parts.setdefault(key, []).append(
                    ChangedFile(
                        name=INDEX_FILE_NAME,
                        before=_joined(before.get(key, [])),
                        after=_joined(after.get(key, [])),
                        is_index_lines=True,
                    )
                )
    return parts


def note_title(file_name: str) -> str:
    """A note's file name as words: ``likes-tea.md`` is "Likes tea"."""
    words = " ".join(
        word
        for word in file_name.removesuffix(NOTE_SUFFIX).replace("_", "-").split("-")
        if word
    )
    return words[:1].upper() + words[1:]


def _change_verb(item: ChangedFile, removal_verb: str) -> str:
    if item.before is None:
        return "added"
    if item.after is None:
        return removal_verb
    return "changed"


def _index_part_phrase(note_count: int) -> str:
    if note_count == 1:
        return "its index line"
    if note_count > 1:
        return "the index lines"
    return "changed the index"


def describe(files: Sequence[ChangedFile], note: str | None) -> str:
    """One plain phrase naming what changed, e.g. `added the note "Tea" and its index line`.

    ``note`` is the note a per-note record belongs to, whose index line alone reads
    `changed the index line of "Tea"`.
    """
    index_part = next((item for item in files if item.name == INDEX_FILE_NAME), None)
    if note and index_part is not None and not any(item.name == note for item in files):
        return f'{_change_verb(index_part, "removed")} the index line of "{note_title(note)}"'
    notes = [item for item in files if item.name != INDEX_FILE_NAME]
    phrases = [
        f'{_change_verb(item, "deleted")} the note "{note_title(item.name)}"'
        for item in notes
    ]
    if index_part is not None:
        phrases.append(_index_part_phrase(len(notes)))
    if len(phrases) > 1 and index_part is not None:
        return ", ".join(phrases[:-1]) + " and " + phrases[-1]
    return ", ".join(phrases)


def _holds_now(current: Mapping[str, str], item: ChangedFile, key: str) -> bool:
    """Whether the folder still holds what ``item`` left there (for an index part, its lines)."""
    if item.is_index_lines:
        return index_lines_by_key(current.get(item.name)).get(key, []) == _lines(
            item.after
        )
    return current.get(item.name) == item.after


def _restored_text(
    item: ChangedFile, key: str, current: Mapping[str, str]
) -> str | None:
    """What undoing ``item`` leaves in its file: for an index part, the index as it stands with
    only ``key``'s lines put back."""
    if not item.is_index_lines:
        return item.before
    now = current.get(item.name)
    return replace_key_lines(
        now, key, index_lines_by_key(now).get(key, []), _lines(item.before)
    )


def _snapshot_with(
    snapshot: Mapping[str, str], files: Sequence[ChangedFile]
) -> dict[str, str]:
    """``snapshot`` with each of ``files`` holding its "after" text (gone, for ``None``)."""
    after_by_name = {item.name: item.after for item in files}
    kept = {name: text for name, text in snapshot.items() if name not in after_by_name}
    added = {name: text for name, text in after_by_name.items() if text is not None}
    return {**kept, **added}


def _is_safe_change_id(change_id: str) -> bool:
    """Whether ``change_id`` names a record file and nothing outside the log."""
    return bool(change_id) and _safe_name(change_id) == change_id


def _new_change_id(moment: datetime) -> str:
    return f"{moment.strftime(_CHANGE_ID_TIME_FORMAT)}-{uuid.uuid4().hex}"


class ChangeLog:
    """One memory folder's change records, and the per-session bookkeeping that finds them."""

    def __init__(
        self,
        changes_dir: Path,
        memory_dir: Path,
        max_change_records: int,
        stale_session_seconds: float,
    ) -> None:
        self.changes_dir = changes_dir
        self.memory_dir = memory_dir
        self.state_dir = changes_dir / _STATE_DIR_NAME
        self.max_change_records = max_change_records
        self.stale_session_seconds = stale_session_seconds

    # -- the folder and the per-session bookkeeping

    def snapshot(self) -> dict[str, str]:
        """Every file in the memory folder, by name, with its text."""
        if not self.memory_dir.is_dir():
            return {}
        return {
            path.name: path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(self.memory_dir.iterdir())
            if path.is_file() and not path.name.startswith(".")
        }

    def mentions_memory(self, tool_input: object) -> bool:
        """Whether a tool call's input names the memory folder (the cheap gate before a compare)."""
        text = json.dumps(tool_input)
        names = {
            "memories",
            INDEX_FILE_NAME,
            str(self.memory_dir),
            str(self.memory_dir.resolve()),
        }
        return any(name in text for name in names)

    def _baseline_path(self, session_id: str) -> Path:
        return self.state_dir / f"baseline-{_safe_name(session_id)}{_JSON_SUFFIX}"

    def _turn_path(self, session_id: str) -> Path:
        return self.state_dir / f"turn-{_safe_name(session_id)}{_JSON_SUFFIX}"

    def _move_baselines_past(
        self, files: Sequence[ChangedFile], except_session: str | None
    ) -> None:
        """Make every session's baseline (but one) already hold ``files``' "after" texts."""
        skipped = (
            None if except_session is None else self._baseline_path(except_session)
        )
        for path in self.state_dir.glob(f"baseline-*{_JSON_SUFFIX}"):
            if path == skipped:
                continue
            baseline = _read_snapshot(path)
            if baseline is not None:
                _write_json(path, _snapshot_with(baseline, files))

    def absorb_user_edit(self, before: Mapping[str, str]) -> None:
        """Move every session's baseline past what changed since ``before`` (a ``snapshot`` taken
        just ahead of an edit the user made, e.g. in the Memory app), so no chat's agent is
        charged with it, even one in the middle of a turn."""
        self._move_baselines_past(
            diff_snapshots(before, self.snapshot()), except_session=None
        )

    def start_turn(self, session_id: str) -> None:
        _write_json(self._baseline_path(session_id), self.snapshot())
        _write_json(self._turn_path(session_id), {"change_ids": {}})
        self._drop_stale_sessions()

    def _drop_stale_sessions(self) -> None:
        """Remove the bookkeeping of sessions that have not started a turn in ``stale_session_seconds``.

        Every baseline is a full copy of the folder, so without this the state grows with every
        chat ever run. A session that comes back simply starts its next turn with a fresh baseline.
        """
        cutoff = time.time() - self.stale_session_seconds
        for pattern in (f"baseline-*{_JSON_SUFFIX}", f"turn-*{_JSON_SUFFIX}"):
            for path in self.state_dir.glob(pattern):
                try:
                    modified_at = path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if modified_at < cutoff:
                    path.unlink(missing_ok=True)

    def _drop_oldest_changes(self) -> None:
        """Keep only the newest ``max_change_records`` change records (ids sort by time)."""
        records = sorted(
            self.changes_dir.glob(f"*{_JSON_SUFFIX}"), key=lambda path: path.stem
        )
        for path in records[: max(0, len(records) - self.max_change_records)]:
            path.unlink(missing_ok=True)

    def _turn_record_ids(self, session_id: str) -> dict[str, str]:
        """The turn's record id per note (the index-only key for the index's other lines).

        A turn file from before records were split per note holds one "change_id"; it is dropped.
        """
        turn = _read_json(self._turn_path(session_id)) or {}
        saved_ids = json_object(turn.get("change_ids"))
        if saved_ids is None:
            return {}
        return {
            key: change_id
            for key, change_id in saved_ids.items()
            if isinstance(change_id, str)
        }

    # -- recording

    def record_tool_call(
        self,
        session_id: str,
        tool_use_id: str,
        tool_input: object,
        chat_id: str,
        agent_name: str,
    ) -> list[MemoryChange]:
        """Fold what this tool call changed into its turn's records: one per note, one for the index's other lines.

        Returns every record the call touched as it now stands, oldest first; empty when the
        call changed nothing. A record whose changes this turn, taken together, cancel out is
        removed and not returned.
        """
        if not self.mentions_memory(tool_input):
            return []
        current = self.snapshot()
        before = _read_snapshot(self._baseline_path(session_id))
        _write_json(self._baseline_path(session_id), current)
        if before is None:
            return []
        files = diff_snapshots(before, current)
        if not files:
            return []
        self._move_baselines_past(files, except_session=session_id)
        record_ids = self._turn_record_ids(session_id)
        touched: list[MemoryChange] = []
        for key, part in split_by_note(files).items():
            change = self._fold(
                key=key,
                part=part,
                existing_id=record_ids.get(key),
                session_id=session_id,
                tool_use_id=tool_use_id,
                chat_id=chat_id,
                agent_name=agent_name,
            )
            if change is None:
                record_ids.pop(key, None)
            else:
                record_ids[key] = change.id
                touched.append(change)
        _write_json(self._turn_path(session_id), {"change_ids": record_ids})
        return sorted(touched, key=lambda change: change.id)

    def _fold(
        self,
        key: str,
        part: Sequence[ChangedFile],
        existing_id: str | None,
        session_id: str,
        tool_use_id: str,
        chat_id: str,
        agent_name: str,
    ) -> MemoryChange | None:
        """Fold one record's part of a tool call into the turn's record for ``key``, writing it.

        Returns the record, or None when the turn's changes to it cancel out (it is removed).
        """
        existing = None if existing_id is None else self._read(existing_id)
        if existing is not None and existing.undone_at is None:
            is_new_tool_call = (
                bool(tool_use_id) and tool_use_id not in existing.tool_use_ids
            )
            unsummarized = existing._replace(
                files=merge_files(existing.files, part),
                tool_use_ids=(*existing.tool_use_ids, tool_use_id)
                if is_new_tool_call
                else existing.tool_use_ids,
            )
        else:
            moment = _now()
            unsummarized = MemoryChange(
                id=_new_change_id(moment),
                time=moment.replace(microsecond=0),
                session_id=session_id,
                chat_id=chat_id,
                agent_name=agent_name,
                note=key or None,
                tool_use_ids=(tool_use_id,) if tool_use_id else (),
                files=tuple(item for item in part if item.before != item.after),
                summary="",
                undone_at=None,
            )
        path = self.changes_dir / f"{unsummarized.id}{_JSON_SUFFIX}"
        if not unsummarized.files:
            path.unlink(missing_ok=True)
            return None
        change = unsummarized._replace(
            summary=describe(unsummarized.files, key or None)
        )
        is_new = not path.exists()
        _write_json(path, change.to_json())
        if is_new:
            self._drop_oldest_changes()
        return change

    # -- reading and undoing

    def _path(self, change_id: str) -> Path:
        if not _is_safe_change_id(change_id):
            raise ChangeNotFoundError(change_id)
        return self.changes_dir / f"{change_id}{_JSON_SUFFIX}"

    def _read(self, change_id: str) -> MemoryChange | None:
        if not _is_safe_change_id(change_id):
            return None
        path = self._path(change_id)
        raw = _read_json(path)
        if raw is None:
            return None
        change = MemoryChange.from_json(raw)
        if change is None:
            logger.warning("The memory change record %s is not well-formed", path)
        return change

    def list_changes(self) -> list[MemoryChange]:
        """Every change, oldest first."""
        if not self.changes_dir.is_dir():
            return []
        read = (
            self._read(path.stem) for path in self.changes_dir.glob(f"*{_JSON_SUFFIX}")
        )
        return sorted(
            (change for change in read if change is not None),
            key=lambda change: change.id,
        )

    def get(self, change_id: str) -> MemoryChange:
        change = self._read(change_id)
        if change is None:
            raise ChangeNotFoundError(change_id)
        return change

    def _write_memory_file(self, name: str, text: str | None) -> None:
        target = self.memory_dir / Path(name).name
        if text is None:
            target.unlink(missing_ok=True)
            return
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        temp_path = target.with_name(f".{target.name}.undo")
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(target)

    def undo(self, change_id: str, is_forced: bool) -> MemoryChange:
        """Put back every file the change touched as it was before it.

        Refused with ChangedSinceError when a file no longer holds what the change left there
        (it was changed again later), unless ``is_forced``.
        """
        change = self.get(change_id)
        if change.undone_at is not None:
            return change
        current = self.snapshot()
        key = change.note or _INDEX_ONLY
        moved_on = [
            item.name for item in change.files if not _holds_now(current, item, key)
        ]
        if moved_on and not is_forced:
            raise ChangedSinceError(moved_on)
        for item in change.files:
            self._write_memory_file(item.name, _restored_text(item, key, current))
        # The undo is the user's, not any agent's: no session should see it as a change.
        self.absorb_user_edit(current)
        undone = change._replace(undone_at=_now().replace(microsecond=0))
        _write_json(self._path(change_id), undone.to_json())
        return undone


def default_log(environ: Mapping[str, str]) -> ChangeLog:
    """The change log of the main checkout, with the env overrides tests and previews use."""
    root = main_checkout(own_repo_root())
    memory_dir = Path(environ.get(MEMORY_DIR_ENV) or root / DEFAULT_MEMORY_DIR)
    changes_dir = Path(environ.get(CHANGES_DIR_ENV) or root / DEFAULT_CHANGES_DIR)
    return ChangeLog(
        changes_dir=changes_dir,
        memory_dir=memory_dir,
        max_change_records=MAX_CHANGE_RECORDS,
        stale_session_seconds=STALE_SESSION_SECONDS,
    )
