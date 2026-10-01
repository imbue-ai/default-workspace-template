#!/usr/bin/env python3
"""Print the workspace memory a harness without Claude's built-in memory needs: how to keep it, and its index.

Claude chats keep their notes in ``data/memories/`` through Claude Code's own auto memory (``autoMemoryDirectory``
in ``.claude/settings.json``): Claude Code tells the model how to write a note and loads the first 200 lines of
``MEMORY.md`` at the start of every session. Other harnesses have no such feature, so this prints the same two things
for them -- the protocol in ``.agents/shared/references/memory-protocol.md``, adapted from Claude Code's own memory
prompt so both write notes in one format, followed by the index as it is now -- and each harness's own wiring puts
the text in front of its model (for pi, ``.pi/extensions/memory.ts``, on every prompt).

The index is cut the way Claude Code cuts it: at 200 lines or 25KB, whichever comes first. The current UTC time is
filled in too, since Claude Code stamps a note's ``modified`` itself and a model left to guess the date gets it wrong.

It also prints the notes the user deleted or edited in the "What agents know" app, from the record that app keeps
(``data/.state/memories/user-changes.jsonl``: a file name, what was done and when, never the content). An open chat
still has a note it saw in its conversation, and without this it writes a deleted note back, or reverts an edit,
the next time it saves. Every harness needs that notice, Claude included.

Claude has one more gap: Claude Code loads ``MEMORY.md`` once, when a chat starts, so a note another chat saves later
never reaches it. pi reads the index on every message and has no such gap. So ``--claude-hook``, which Claude's
UserPromptSubmit hook in ``.claude/settings.json`` runs before every message, reads the hook's input (the chat's
session id and transcript path) and prints the change notice plus the index lines of notes saved or changed since the
chat started, leaving out the ones the chat saved itself -- and nothing at all when there is nothing new.

Fails open: if the protocol cannot be read, it prints nothing and exits 0, so a broken checkout costs a chat its
memory, never its turn. An unreadable or malformed change record reads as no changes. Stdlib only, since harness
hooks run it under a plain ``python3``.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PROTOCOL_PATH: Final[Path] = (
    REPO_ROOT / ".agents" / "shared" / "references" / "memory-protocol.md"
)
# The folder Claude's autoMemoryDirectory names: absolute, so a worker in its own worktree shares the main one.
DEFAULT_NOTES_DIR: Final[Path] = Path.home() / "workspace" / "data" / "memories"
DEFAULT_CHANGES_PATH: Final[Path] = (
    Path.home() / "workspace" / "data" / ".state" / "memories" / "user-changes.jsonl"
)
# The memories app drops older entries itself; this keeps a stale record from being read as news.
CHANGE_MAX_AGE: Final[timedelta] = timedelta(days=30)
INDEX_FILENAME: Final[str] = "MEMORY.md"
NON_NOTE_FILENAMES: Final[frozenset[str]] = frozenset({"MEMORY.md", "README.md"})
# Claude Code stamps this on every note a Claude session saves.
CLAUDE_SESSION_KEY: Final[str] = "originSessionId"
SAVED_SINCE_MAX_NOTES: Final[int] = 20
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


def render_memory_context(
    protocol_text: str,
    index_text: str | None,
    notes_dir: Path,
    harness: str,
    now: datetime,
) -> str:
    protocol = (
        protocol_text.replace("{notes_dir}", str(notes_dir))
        .replace("{harness}", harness)
        .replace("{now}", now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        .strip()
    )
    index_path = notes_dir / INDEX_FILENAME
    if index_text is None or not index_text.strip():
        return f"{protocol}\n\n## Your memory index\n\n{index_path} is empty: nothing has been saved yet.\n"
    return (
        f"{protocol}\n\n## Your memory index\n\n"
        f"The contents of {index_path} as of this message (open a note's file when it looks relevant):\n\n"
        f"{truncate_index(index_text)}\n"
    )


def read_index(notes_dir: Path) -> str | None:
    try:
        return (notes_dir / INDEX_FILENAME).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None


def latest_changes(changes_text: str, now: datetime) -> list[tuple[str, str, datetime]]:
    """Each note's latest delete or edit within ``CHANGE_MAX_AGE`` as (file name, change, when), oldest first."""
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
        ((name, change, at) for name, (change, at) in latest.items()),
        key=lambda item: item[2],
    )


def render_changes_notice(changes: list[tuple[str, str, datetime]]) -> str:
    """What every chat is told about the user's deletes and edits, or "" when there are none."""
    if not changes:
        return ""
    lines = [
        "## Changes the user made to saved memories",
        "",
        'The user deleted or edited these notes in the "What agents know" app. Their version is the one to keep, '
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


def session_start(transcript_path: Path) -> datetime | None:
    """When a Claude chat started: the first timestamp its transcript records, or None when it cannot be read."""
    try:
        with transcript_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    timestamp = json.loads(line).get("timestamp")
                    if isinstance(timestamp, str):
                        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue
    except OSError:
        return None
    return None


def _note_description(note_text: str) -> str:
    for line in note_text.splitlines()[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        if key == "description":
            return value.strip().strip("\"'")
    return ""


def notes_saved_since(
    notes_dir: Path, since: datetime, session_id: str, index_text: str | None
) -> list[str]:
    """The index line (or a stand-in) of each note saved or changed after ``since`` by anyone but this session."""
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
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if modified <= since.timestamp():
            continue
        if session_id and f"{CLAUDE_SESSION_KEY}: {session_id}" in text:
            continue
        line = next(
            (candidate for candidate in index_lines if f"]({path.name})" in candidate),
            f"- `{path.name}` — {_note_description(text)}",
        )
        found.append((modified, line))
    found.sort()
    return [line for _, line in found[-SAVED_SINCE_MAX_NOTES:]]


def render_saved_since_notice(lines: list[str]) -> str:
    if not lines:
        return ""
    return (
        "## Notes saved since this chat started\n\n"
        "This chat loaded the memory index when it started. Other chats (or the user) saved or changed these notes "
        "since then, so they are not in that index; open a note's file when it looks relevant:\n"
        + "\n".join(lines)
        + "\n"
    )


def claude_hook_output(
    hook_input: str, notes_dir: Path, changes_path: Path, now: datetime
) -> str:
    """What Claude's UserPromptSubmit hook adds to the next message: the change notice, then new notes."""
    notice = render_changes_notice(latest_changes(read_changes_text(changes_path), now))
    try:
        payload = json.loads(hook_input)
    except ValueError:
        payload = None
    saved_since = ""
    if isinstance(payload, dict) and isinstance(payload.get("transcript_path"), str):
        started = session_start(Path(payload["transcript_path"]))
        if started is not None and started.tzinfo is not None:
            saved_since = render_saved_since_notice(
                notes_saved_since(
                    notes_dir,
                    started,
                    str(payload.get("session_id") or ""),
                    read_index(notes_dir),
                )
            )
    return "\n".join(part for part in (notice, saved_since) if part)


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
        "--claude-hook",
        action="store_true",
        help="Run as Claude's UserPromptSubmit hook: read its input on stdin, print only what is new",
    )
    arguments = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    if arguments.claude_hook:
        sys.stdout.write(
            claude_hook_output(
                sys.stdin.read(), arguments.notes_dir, arguments.changes, now
            )
        )
        return 0
    notice = render_changes_notice(
        latest_changes(read_changes_text(arguments.changes), now)
    )
    try:
        protocol_text = arguments.protocol.read_text(encoding="utf-8")
    except OSError as e:
        print(
            f"agent_memory_context: cannot read {arguments.protocol}: {e}; printing nothing",
            file=sys.stderr,
        )
        return 0
    context = render_memory_context(
        protocol_text,
        read_index(arguments.notes_dir),
        arguments.notes_dir,
        arguments.harness,
        now,
    )
    sys.stdout.write(f"{context}\n{notice}" if notice else context)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
