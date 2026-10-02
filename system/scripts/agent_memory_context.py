#!/usr/bin/env python3
"""What every chat is told about the workspace's shared memory, and the upkeep that keeps the notes consistent.

Claude chats keep their notes in ``data/memories/`` through Claude Code's own auto memory (``autoMemoryDirectory``
in ``.claude/settings.json``): Claude Code tells the model how to write a note and loads the first 200 lines of
``MEMORY.md`` at the start of every session. Other harnesses have no such feature, so this prints the same two things
for them -- the protocol in ``.agents/shared/references/memory-protocol.md``, adapted from Claude Code's own memory
prompt so both write notes in one format, followed by the index as it is now -- and each harness's own wiring puts
the text in front of its model (for pi, ``.pi/extensions/memory.ts``, on every prompt). With ``--json`` the two parts
come apart, the protocol (fixed) and the index with any notices (changing only when a note does), so a harness that
records prompt changes in the conversation, as pi does, records the protocol once and the index only when it changes.
Nothing in either part depends on the clock for that reason.

The index is cut the way Claude Code cuts it: at 200 lines or 25KB, whichever comes first.

Claude Code stamps ``modified`` on a note itself; a model left to write the date guesses it. So ``--stamp PATH`` does
the same for another harness: right after it writes or edits a note, it sets the note's ``metadata.modified`` to now
and adds ``metadata.source`` when the note names none. pi's memory extension runs it after every ``write`` or ``edit``
of a note.

It also prints the notes the user deleted or edited in the "Agent Memory" app, from the record that app keeps
(``data/.apps/memories/user-changes.jsonl``: a file name, what was done and when, never the content). An open chat
still has a note it saw in its conversation, and without this it writes a deleted note back, or reverts an edit,
the next time it saves. Every harness needs that notice, Claude included.

Claude has one more gap: Claude Code loads ``MEMORY.md`` once, when a chat starts, so a note another chat saves later
never reaches it. pi reads the index on every message and has no such gap. So ``--claude-hook``, which Claude's
UserPromptSubmit hook in ``.claude/settings.json`` runs before every message, reads the chat's transcript (its path is
in the hook's input) and prints the deletes and edits, and the index lines of notes others saved or changed, that are
newer than its last run for that chat (kept in the agent's state directory) -- each once, since the chat keeps what an
earlier message's hook added, and nothing at all when there is nothing new.

The user can pause memory, or turn it off for one kind of chat, in the Agent Memory app, which keeps the switches in
``data/.apps/memories/settings.json``. While memory is off for a harness, this prints a short notice saying so in
place of the protocol and the index (and, for Claude, on every message), and Claude's hook tells an open chat once
when it is back on. A settings file that cannot be read counts as off.

Fails open: if the protocol cannot be read, it prints nothing and exits 0, so a broken checkout costs a chat its
memory, never its turn. An unreadable or malformed change record reads as no changes. Stdlib only, since harness
hooks run it under a plain ``python3``.
"""

import argparse
import fcntl
import json
import os
import re
import sys
import uuid
from collections.abc import Set as AbstractSet
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, NamedTuple

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PROTOCOL_PATH: Final[Path] = (
    REPO_ROOT / ".agents" / "shared" / "references" / "memory-protocol.md"
)
# The folder Claude's autoMemoryDirectory names: absolute, so a worker in its own worktree shares the main one.
DEFAULT_NOTES_DIR: Final[Path] = Path.home() / "workspace" / "data" / "memories"
DEFAULT_CHANGES_PATH: Final[Path] = (
    Path.home() / "workspace" / "data" / ".apps" / "memories" / "user-changes.jsonl"
)
DEFAULT_CONTROLS_PATH: Final[Path] = (
    Path.home() / "workspace" / "data" / ".apps" / "memories" / "settings.json"
)
_HARNESS_LABELS: Final[dict[str, str]] = {"claude": "Claude", "pi-coding": "pi"}
# The memories app drops older entries itself; this keeps a stale record from being read as news.
CHANGE_MAX_AGE: Final[timedelta] = timedelta(days=30)
INDEX_FILENAME: Final[str] = "MEMORY.md"
NON_NOTE_FILENAMES: Final[frozenset[str]] = frozenset({"MEMORY.md", "README.md"})
SAVED_SINCE_MAX_NOTES: Final[int] = 20
# Held while MEMORY.md is read and rewritten; the Agent Memory app (memories/notes.py) takes the same lock file.
INDEX_LOCK_FILENAME: Final[str] = ".MEMORY.md.lock"
# The hook's mark is set this far before its clock, so a change the app timestamped just before the hook ran, but
# wrote just after it read the record, is announced next time rather than never.
HOOK_MARK_SLACK: Final[timedelta] = timedelta(seconds=5)
# A note this chat wrote is stamped a moment after its Write call is recorded.
OWN_WRITE_TOLERANCE: Final[timedelta] = timedelta(seconds=5)
_CLAUDE_WRITE_TOOLS: Final[frozenset[str]] = frozenset({"Write", "Edit", "MultiEdit"})
_WATERMARK_PREFIX: Final[str] = "memory-hook-"
_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=timezone.utc)
INDEX_MAX_LINES: Final[int] = 200
INDEX_MAX_BYTES: Final[int] = 25 * 1024


def truncate_index(index_text: str) -> str:
    """The index cut to its first 200 lines or 25KB, whichever is shorter, never mid-line."""
    kept: list[str] = []
    kept_bytes = 0
    for line in index_text.splitlines()[:INDEX_MAX_LINES]:
        line_bytes = len(line.encode("utf-8")) + 1
        if kept_bytes + line_bytes > INDEX_MAX_BYTES:
            break
        kept.append(line)
        kept_bytes += line_bytes
    return "\n".join(kept)


def render_protocol(protocol_text: str, notes_dir: Path, harness: str) -> str:
    return (
        protocol_text.replace("{notes_dir}", str(notes_dir))
        .replace("{harness}", harness)
        .strip()
    )


def render_index_section(index_text: str | None, notes_dir: Path) -> str:
    index_path = notes_dir / INDEX_FILENAME
    if index_text is None or not index_text.strip():
        return f"## Your memory index\n\n{index_path} is empty: nothing has been saved yet.\n"
    return (
        "## Your memory index\n\n"
        f"The current contents of {index_path} (open a note's file when it looks relevant):\n\n"
        f"{truncate_index(index_text)}\n"
    )


def render_memory_context(
    protocol_text: str, index_text: str | None, notes_dir: Path, harness: str
) -> str:
    return f"{render_protocol(protocol_text, notes_dir, harness)}\n\n{render_index_section(index_text, notes_dir)}"


def stamp_note_text(text: str, harness: str, now: datetime) -> str | None:
    """The note with ``metadata.modified`` set to ``now`` and ``metadata.source`` added when it names no source.

    Only the frontmatter's own keys are touched: a top-level ``modified`` (moved into ``metadata``) and the
    ``metadata`` block's direct children. Returns None for a note left as written: one with no frontmatter, or whose
    ``metadata`` is written inline (``metadata: {...}``), which a line-based edit cannot extend safely. Keeps the
    note's line endings.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    if not lines or lines[0].strip() != "---":
        return None
    closing_idx = next(
        (idx for idx in range(1, len(lines)) if lines[idx].strip() == "---"), None
    )
    if closing_idx is None:
        return None
    frontmatter = lines[1:closing_idx]
    if any(
        line.startswith("metadata:") and line.split(":", 1)[1].strip()
        for line in frontmatter
    ):
        return None
    metadata_idx = next(
        (idx for idx, line in enumerate(frontmatter) if line.rstrip() == "metadata:"),
        None,
    )
    block_end = metadata_idx + 1 if metadata_idx is not None else len(frontmatter)
    while (
        metadata_idx is not None
        and block_end < len(frontmatter)
        and frontmatter[block_end][:1] in (" ", "\t")
    ):
        block_end += 1
    child_indent = "  "
    if metadata_idx is not None and block_end > metadata_idx + 1:
        first_child = frontmatter[metadata_idx + 1]
        child_indent = first_child[: len(first_child) - len(first_child.lstrip())]

    def is_own_key(idx: int, key: str) -> bool:
        line = frontmatter[idx]
        if line.startswith(f"{key}:"):
            return True
        is_metadata_child = metadata_idx is not None and metadata_idx < idx < block_end
        return is_metadata_child and line.startswith(f"{child_indent}{key}:")

    has_source = any(is_own_key(idx, "source") for idx in range(len(frontmatter)))
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    additions = ([] if has_source else [f"{child_indent}source: {harness}"]) + [
        f"{child_indent}modified: {stamp}"
    ]
    stamped: list[str] = []
    for idx, line in enumerate(frontmatter):
        if not is_own_key(idx, "modified"):
            stamped.append(line)
        if idx == block_end - 1 and metadata_idx is not None:
            stamped.extend(additions)
    if metadata_idx is None:
        stamped.extend(["metadata:", *additions])
    return newline.join(["---", *stamped, *lines[closing_idx:]])


def stamp_note(path: Path, notes_dir: Path, harness: str, now: datetime) -> bool:
    """Stamp one note file in place; False (and the file untouched) when it is not a note or cannot be read."""
    if (
        path.parent.resolve() != notes_dir.resolve()
        or path.suffix != ".md"
        or path.name in NON_NOTE_FILENAMES
    ):
        return False
    try:
        text = _read_keeping_newlines(path)
        mode = path.stat().st_mode
    except (OSError, ValueError):
        return False
    stamped = stamp_note_text(text, harness, now)
    if stamped is None or stamped == text:
        return False
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_keeping_newlines(temporary, stamped)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        return False
    return True


def _read_keeping_newlines(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_keeping_newlines(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _is_note_file_name(file_name: str) -> bool:
    """The Agent Memory app's rule (``is_note_file_name``): any visible Markdown file directly in the folder."""
    return (
        file_name.endswith(".md")
        and file_name != ".md"
        and not file_name.startswith(".")
        and "/" not in file_name
        and "\\" not in file_name
        and file_name not in NON_NOTE_FILENAMES
    )


def read_index(notes_dir: Path) -> str | None:
    try:
        return (notes_dir / INDEX_FILENAME).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None


_INDEX_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<link>\s*[-*]\s+\[[^\]]*\]\((?P<file>[^)]+)\))(?P<hook>.*)$"
)


def _unquote(value: str) -> str:
    """A frontmatter scalar as YAML reads it: JSON-style double quotes, ``''`` inside single quotes, else as is."""
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        try:
            decoded = json.loads(stripped)
        except ValueError:
            return stripped[1:-1]
        return decoded if isinstance(decoded, str) else stripped
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == "'":
        return stripped[1:-1].replace("''", "'")
    return stripped


def _frontmatter_value(note_text: str, key: str) -> str:
    lines = note_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        found_key, _, value = line.partition(":")
        if found_key == key:
            # A block scalar (``|`` or ``>``) spans the lines below, which a line-based read cannot follow.
            if value.strip()[:1] in ("|", ">"):
                return ""
            return " ".join(_unquote(value).split())
    return ""


def _index_title(note_name: str, file_name: str) -> str:
    words = " ".join(
        (note_name or file_name.removesuffix(".md"))
        .replace("-", " ")
        .replace("_", " ")
        .split()
    )
    return words[:1].upper() + words[1:]


def sync_index_text(
    index_text: str,
    description_by_file: dict[str, str],
    name_by_file: dict[str, str],
    note_files: AbstractSet[str],
) -> str:
    """``MEMORY.md`` with each note's line summarizing it as its own ``description`` says.

    A chat that changes a note does not always change its line, and every chat starts from these lines, so the line
    is the note's description, kept in step here rather than left to each model. A note's first line is rewritten,
    later lines for it are dropped, a note with no line gets one appended, and a line for a note file that is gone
    is dropped, so a deleted fact never stays in what chats start from. Everything else (headings, prose, links to
    anything that is not a note) is left as it is.
    """
    newline = "\r\n" if "\r\n" in index_text else "\n"
    kept: list[str] = []
    seen: set[str] = set()
    for line in index_text.splitlines():
        match = _INDEX_LINE_PATTERN.match(line)
        file_name = match.group("file") if match is not None else None
        if (
            file_name is not None
            and _is_note_file_name(file_name)
            and file_name not in note_files
        ):
            continue
        if match is None or file_name not in description_by_file:
            kept.append(line)
            continue
        if file_name in seen:
            continue
        seen.add(file_name)
        kept.append(f"{match.group('link')} — {description_by_file[file_name]}")
    for file_name in sorted(set(description_by_file) - seen):
        title = _index_title(name_by_file.get(file_name, ""), file_name)
        kept.append(f"- [{title}]({file_name}) — {description_by_file[file_name]}")
    return newline.join(kept) + newline if kept else ""


def sync_index(notes_dir: Path) -> bool:
    """Bring ``MEMORY.md`` in line with the notes (see ``sync_index_text``); True when it was rewritten.

    The notes and the index are read and the index rewritten under the index lock the Agent Memory app also takes,
    so a delete in the app and a sync here never write over each other. A note that cannot be read, or has no
    description, keeps whatever line it has. The write goes through a temporary file and a rename, only when
    something changed, and keeps the file's line endings and permissions.
    """
    try:
        lock = (notes_dir / INDEX_LOCK_FILENAME).open("a")
    except OSError:
        return False
    with lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _sync_index_locked(notes_dir)


def _sync_index_locked(notes_dir: Path) -> bool:
    description_by_file: dict[str, str] = {}
    name_by_file: dict[str, str] = {}
    try:
        note_paths = [
            path
            for path in sorted(notes_dir.glob("*.md"))
            if _is_note_file_name(path.name)
        ]
    except OSError:
        return False
    for path in note_paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        description = _frontmatter_value(text, "description")
        if description:
            description_by_file[path.name] = description
            name_by_file[path.name] = _frontmatter_value(text, "name")
    index_path = notes_dir / INDEX_FILENAME
    try:
        index_text = _read_keeping_newlines(index_path)
    except FileNotFoundError:
        index_text = ""
    except (OSError, ValueError):
        return False
    synced = sync_index_text(
        index_text,
        description_by_file,
        name_by_file,
        {path.name for path in note_paths},
    )
    if synced == index_text:
        return False
    temporary = index_path.with_name(f".{INDEX_FILENAME}.{uuid.uuid4().hex}.tmp")
    try:
        _write_keeping_newlines(temporary, synced)
        if index_path.exists():
            os.chmod(temporary, index_path.stat().st_mode)
        os.replace(temporary, index_path)
    except OSError:
        temporary.unlink(missing_ok=True)
        return False
    return True


def latest_changes(
    changes_text: str, now: datetime, notes_dir: Path
) -> list[tuple[str, str, datetime]]:
    """Each note's latest delete or edit within ``CHANGE_MAX_AGE`` as (file name, change, when), oldest first.

    A delete is left out once a chat has saved that note again since (the user told it the fact again), so chats
    are not told to keep away from a note that is back.
    """
    latest: dict[str, tuple[str, datetime]] = {}
    for line in changes_text.splitlines():
        try:
            entry = json.loads(line)
            file_name = str(entry["file_name"])
            change = str(entry["change"]).lower()
            at = datetime.fromisoformat(str(entry["at"]).replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError):
            continue
        if at.tzinfo is None or change not in ("deleted", "edited"):
            continue
        if now - at > CHANGE_MAX_AGE:
            continue
        previous = latest.get(file_name)
        if previous is None or at >= previous[1]:
            latest[file_name] = (change, at)
    return sorted(
        (
            (name, change, at)
            for name, (change, at) in latest.items()
            if not (change == "deleted" and _saved_after(notes_dir / name, at))
        ),
        key=lambda item: item[2],
    )


def _saved_after(path: Path, at: datetime) -> bool:
    try:
        return path.stat().st_mtime > at.timestamp()
    except OSError:
        return False


def render_changes_notice(changes: list[tuple[str, str, datetime]]) -> str:
    """What every chat is told about the user's deletes and edits, or "" when there are none."""
    if not changes:
        return ""
    lines = [
        "## Changes the user made to saved memories",
        "",
        'The user deleted or edited these notes in the "Agent Memory" app. Their version is the one to keep, '
        "even where this conversation remembers something else:",
    ]
    for file_name, change, at in changes:
        when = at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if change == "deleted":
            lines.append(
                f"- `{file_name}` was deleted {when}. Don't save what it said again, in that note or any other, "
                "unless the user tells you it again."
            )
        else:
            lines.append(
                f"- `{file_name}` was edited {when}. Read it again before you change it, and don't put back "
                "anything the user removed."
            )
    return "\n".join(lines) + "\n"


def read_changes_text(changes_path: Path) -> str:
    try:
        return changes_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


class TranscriptFacts(NamedTuple):
    """What a Claude chat's transcript says about its memory: when it loaded the index, and its own writes."""

    started: datetime | None
    own_write_at_by_file: dict[str, datetime]


def _record_timestamp(line: str) -> datetime | None:
    """A transcript record's own timestamp (never one inside its content), or None."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    raw = record.get("timestamp") if isinstance(record, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def read_transcript_facts(transcript_path: Path, notes_dir: Path) -> TranscriptFacts:
    """One pass over a transcript, parsing only the lines that matter: the first that has a timestamp, and the ones
    whose Write/Edit calls touched a note."""
    started: datetime | None = None
    own_write_at_by_file: dict[str, datetime] = {}
    try:
        with transcript_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if started is None:
                    started = _record_timestamp(line)
                if notes_dir.name not in line or '"tool_use"' not in line:
                    continue
                at = _record_timestamp(line)
                for file_name in _written_note_names(line, notes_dir):
                    if at is not None and at > own_write_at_by_file.get(
                        file_name, _EPOCH
                    ):
                        own_write_at_by_file[file_name] = at
    except OSError:
        return TranscriptFacts(started=None, own_write_at_by_file={})
    return TranscriptFacts(started=started, own_write_at_by_file=own_write_at_by_file)


def _watermark_path(state_dir: Path, session_id: str) -> Path:
    return (
        state_dir
        / f"{_WATERMARK_PREFIX}{re.sub(r'[^A-Za-z0-9_-]', '_', session_id)}.json"
    )


class HookMark(NamedTuple):
    """What Claude's hook last told a chat: up to when it announced changes, and whether memory was off."""

    checked_at: datetime | None
    is_memory_off: bool


def read_hook_mark(state_dir: Path | None, session_id: str) -> HookMark | None:
    """The hook's mark for this chat, or None (it never ran, or there is nowhere to keep it)."""
    if state_dir is None or not session_id:
        return None
    try:
        record = json.loads(
            _watermark_path(state_dir, session_id).read_text(encoding="utf-8")
        )
        raw = record.get("checked_at")
        at = (
            None
            if raw is None
            else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        )
        is_memory_off = record.get("is_memory_off") is True
    except (OSError, ValueError, AttributeError):
        return None
    return HookMark(
        checked_at=at if at is None or at.tzinfo is not None else None,
        is_memory_off=is_memory_off,
    )


def write_hook_mark(state_dir: Path | None, session_id: str, mark: HookMark) -> None:
    if state_dir is None or not session_id:
        return
    path = _watermark_path(state_dir, session_id)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    checked_at = (
        None
        if mark.checked_at is None
        else mark.checked_at.astimezone(timezone.utc).isoformat()
    )
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps({"checked_at": checked_at, "is_memory_off": mark.is_memory_off}),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)


def memory_off_reason(controls_path: Path, harness: str) -> str | None:
    """Why memory is off for ``harness``, from the user's switches in the Agent Memory app, or None when it is on.

    No settings file means on. A file that cannot be read or is not the expected shape means off: the user may have
    turned memory off, and reading it as on would ignore that.
    """
    try:
        text = controls_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return "the memory settings could not be read"
    try:
        controls = json.loads(text)
    except ValueError:
        return "the memory settings could not be read"
    if not isinstance(controls, dict):
        return "the memory settings could not be read"
    is_paused = controls.get("is_paused", False)
    disabled = controls.get("disabled_harnesses", [])
    if not isinstance(is_paused, bool) or not isinstance(disabled, list):
        return "the memory settings could not be read"
    if is_paused:
        return "the user paused memory for every chat"
    if harness.upper().replace("-", "_") in disabled:
        return f"the user turned memory off for {_HARNESS_LABELS.get(harness, harness)} chats"
    return None


def render_memory_off_notice(reason: str, notes_dir: Path) -> str:
    return (
        "## Workspace memory is off\n\n"
        f"Memory is off in this workspace: {reason} (in the Agent Memory app). Until it is turned back on, don't read, "
        f"use or save memories in `{notes_dir}/`, don't change `MEMORY.md`, and don't bring up what is saved there, "
        "even if it is already in this conversation. Notes already saved are kept; they just aren't used.\n"
    )


def render_memory_on_again_notice(notes_dir: Path) -> str:
    return (
        "## Workspace memory is on again\n\n"
        f"The user turned memory back on. You may use and save memories in `{notes_dir}/` again, as before it was "
        "turned off.\n"
    )


def _written_note_names(line: str, notes_dir: Path) -> list[str]:
    try:
        record = json.loads(line)
    except ValueError:
        return []
    content = (
        (record.get("message") or {}).get("content")
        if isinstance(record, dict)
        else None
    )
    names: list[str] = []
    for item in content if isinstance(content, list) else []:
        if (
            not isinstance(item, dict)
            or item.get("type") != "tool_use"
            or item.get("name") not in _CLAUDE_WRITE_TOOLS
        ):
            continue
        file_path = (item.get("input") or {}).get("file_path")
        if (
            isinstance(file_path, str)
            and Path(file_path).expanduser().parent == notes_dir
        ):
            names.append(Path(file_path).name)
    return names


def notes_saved_since(
    notes_dir: Path,
    since: datetime,
    own_write_at_by_file: dict[str, datetime],
    index_text: str | None,
) -> list[str]:
    """The index line (or a stand-in) of each note changed after ``since`` whose last change was not this chat's."""
    index_lines = (index_text or "").splitlines()
    found: list[tuple[float, str]] = []
    try:
        paths = sorted(notes_dir.glob("*.md"))
    except OSError:
        return []
    for path in paths:
        if path.name in NON_NOTE_FILENAMES:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if modified <= since.timestamp():
            continue
        own_write_at = own_write_at_by_file.get(path.name)
        if (
            own_write_at is not None
            and own_write_at.timestamp()
            >= modified - OWN_WRITE_TOLERANCE.total_seconds()
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        line = next(
            (candidate for candidate in index_lines if f"]({path.name})" in candidate),
            f"- `{path.name}` — {_frontmatter_value(text, 'description')}",
        )
        found.append((modified, line))
    found.sort()
    return [line for _, line in found]


def render_saved_since_notice(lines: list[str], index_path: Path) -> str:
    """The newest ``SAVED_SINCE_MAX_NOTES`` lines, and how many more there are, so none goes unmentioned."""
    if not lines:
        return ""
    shown = lines[-SAVED_SINCE_MAX_NOTES:]
    more = len(lines) - len(shown)
    tail = f"- and {more} more; read `{index_path}` for the full list\n" if more else ""
    return (
        "## Notes saved since this chat started\n\n"
        "This chat loaded the memory index when it started. Other chats (or the user) saved or changed these notes "
        "since then, so they are not in that index; open a note's file when it looks relevant:\n"
        + "\n".join(shown)
        + "\n"
        + tail
    )


def claude_hook_output(
    hook_input: str,
    notes_dir: Path,
    changes_path: Path,
    state_dir: Path | None,
    off_reason: str | None,
    now: datetime,
) -> str:
    """What Claude's UserPromptSubmit hook adds to the next message: what changed since the chat last heard.

    The chat keeps what an earlier message's hook added, so each change and each note is announced once: the hook
    records when it ran (a mark per chat, in the agent's state directory) and announces only what is newer than
    that, or than the chat's start the first time. Nothing at all most of the time.

    While memory is off for Claude (``off_reason``), every message says so instead, and the mark keeps its old time,
    so the changes made meanwhile are announced once memory is back on, together with a note that it is.
    """
    all_changes = latest_changes(read_changes_text(changes_path), now, notes_dir)
    try:
        payload = json.loads(hook_input)
    except ValueError:
        payload = None
    transcript = payload.get("transcript_path") if isinstance(payload, dict) else None
    session_id = (
        str(payload.get("session_id") or "") if isinstance(payload, dict) else ""
    )
    facts = (
        read_transcript_facts(Path(transcript), notes_dir)
        if isinstance(transcript, str)
        else None
    )
    mark = read_hook_mark(state_dir, session_id)
    if off_reason is not None:
        write_hook_mark(
            state_dir,
            session_id,
            HookMark(
                checked_at=None if mark is None else mark.checked_at, is_memory_off=True
            ),
        )
        return render_memory_off_notice(off_reason, notes_dir)
    on_again = (
        render_memory_on_again_notice(notes_dir)
        if mark is not None and mark.is_memory_off
        else ""
    )
    if facts is None or facts.started is None:
        # Without the transcript there is no telling what the chat already heard, and a missed delete costs more than
        # a repeated notice, so the deletes and edits still go out.
        write_hook_mark(
            state_dir,
            session_id,
            HookMark(
                checked_at=None if mark is None else mark.checked_at,
                is_memory_off=False,
            ),
        )
        return "\n".join(
            part
            for part in (
                on_again,
                render_changes_notice(all_changes),
            )
            if part
        )
    marked_at = None if mark is None else mark.checked_at
    since = max(facts.started, marked_at or facts.started)
    changes = [change for change in all_changes if change[2] > since]
    notice = render_changes_notice(changes)
    saved_since = render_saved_since_notice(
        notes_saved_since(
            notes_dir, since, facts.own_write_at_by_file, read_index(notes_dir)
        ),
        notes_dir / INDEX_FILENAME,
    )
    write_hook_mark(
        state_dir,
        session_id,
        HookMark(checked_at=now - HOOK_MARK_SLACK, is_memory_off=False),
    )
    return "\n".join(part for part in (on_again, notice, saved_since) if part)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--harness",
        default="claude",
        help="The agent type writing notes, recorded as metadata.source",
    )
    parser.add_argument(
        "--notes-dir",
        type=Path,
        default=DEFAULT_NOTES_DIR,
        help="The shared notes folder",
    )
    parser.add_argument(
        "--protocol",
        type=Path,
        default=PROTOCOL_PATH,
        help="The memory protocol to print",
    )
    parser.add_argument(
        "--changes",
        type=Path,
        default=DEFAULT_CHANGES_PATH,
        help="The record of notes the user deleted or edited",
    )
    parser.add_argument(
        "--controls",
        type=Path,
        default=DEFAULT_CONTROLS_PATH,
        help="The user's memory switches from the Agent Memory app",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ["MNGR_AGENT_STATE_DIR"])
        if os.environ.get("MNGR_AGENT_STATE_DIR")
        else None,
        help="Where --claude-hook keeps when it last ran for each chat (default: the agent's state directory)",
    )
    parser.add_argument(
        "--claude-hook",
        action="store_true",
        help="Run as Claude's UserPromptSubmit hook: read its input on stdin, print only what is new",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help='Print {"protocol": ..., "memory": ...}: the fixed protocol apart from the index and notices',
    )
    parser.add_argument(
        "--sync-index",
        action="store_true",
        help="Set each note's MEMORY.md line to its description (adding missing lines, dropping repeats), then exit",
    )
    parser.add_argument(
        "--stamp",
        type=Path,
        help="Stamp metadata.modified (and metadata.source when missing) on this just-written note, sync the index, then exit",
    )
    arguments = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    if arguments.stamp is not None:
        stamp_note(arguments.stamp, arguments.notes_dir, arguments.harness, now)
        sync_index(arguments.notes_dir)
        return 0
    if arguments.sync_index:
        sync_index(arguments.notes_dir)
        return 0
    sync_index(arguments.notes_dir)
    off_reason = memory_off_reason(arguments.controls, arguments.harness)
    if arguments.claude_hook:
        sys.stdout.write(
            claude_hook_output(
                sys.stdin.read(),
                arguments.notes_dir,
                arguments.changes,
                arguments.state_dir,
                off_reason,
                now,
            )
        )
        return 0
    if off_reason is not None:
        off_notice = render_memory_off_notice(off_reason, arguments.notes_dir)
        sys.stdout.write(
            json.dumps({"protocol": off_notice, "memory": ""})
            if arguments.json
            else off_notice
        )
        return 0
    notice = render_changes_notice(
        latest_changes(read_changes_text(arguments.changes), now, arguments.notes_dir)
    )
    try:
        protocol_text = arguments.protocol.read_text(encoding="utf-8")
    except OSError as e:
        print(
            f"agent_memory_context: cannot read {arguments.protocol}: {e}; printing nothing",
            file=sys.stderr,
        )
        return 0
    index_text = read_index(arguments.notes_dir)
    if arguments.json:
        memory = render_index_section(index_text, arguments.notes_dir)
        sys.stdout.write(
            json.dumps(
                {
                    "protocol": render_protocol(
                        protocol_text, arguments.notes_dir, arguments.harness
                    ),
                    "memory": f"{memory}\n{notice}" if notice else memory,
                }
            )
        )
        return 0
    context = render_memory_context(
        protocol_text, index_text, arguments.notes_dir, arguments.harness
    )
    sys.stdout.write(f"{context}\n{notice}" if notice else context)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
