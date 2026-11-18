"""The record of every change an agent made to its memory folder, and undoing one.

Stdlib-only: Claude's hooks (``system/scripts/agent_memory_change_log.py``) import this from
the bare ``python3``, and the chat app imports it to show each change as a card in the
conversation with an Undo button.

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

A record's index part is ``{"name": "MEMORY.md", "part": "lines", "before", "after"}``: only
its own lines, joined (``None``: none), and its Undo rewrites only those lines in the index as
it stands. A record written before records were split per note has whole-file entries only,
and is shown and undone whole.

A change made by one session (or undone through the chat) moves every other session's
baseline past it, so it is never charged to a second chat.

On disk, under the chat app's data (``data/.apps/chat/memory_changes/``):

* ``<change id>.json`` -- one change record (see ``ChangeLog.record_tool_call``);
* ``state/baseline-<session>.json`` and ``state/turn-<session>.json`` -- the bookkeeping.

Both are bounded: a session's bookkeeping goes once the session has not started a turn for
``STALE_SESSION_DAYS`` (its next turn, if it ever has one, writes it afresh), and only the
newest ``MAX_CHANGE_RECORDS`` change records are kept.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
import uuid
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

MEMORY_DIR_ENV = "AGENT_MEMORY_DIR"
CHANGES_DIR_ENV = "AGENT_MEMORY_CHANGES_DIR"
DEFAULT_MEMORY_DIR = Path("data") / "memories"
# The chat app's data dir plus one segment: the chat shows and undoes the changes through this library.
DEFAULT_CHANGES_DIR = Path("data") / ".apps" / "chat" / "memory_changes"
INDEX_FILE_NAME = "MEMORY.md"
# Retention: a session idle this long loses its bookkeeping, and the log keeps this many changes.
STALE_SESSION_DAYS = 30
MAX_CHANGE_RECORDS = 1000

Change = dict[str, Any]

# A pointer line of the index, and the note file it points at (as notes.py reads it).
_POINTER_LINE = re.compile(r"^\s*[-*]\s+\[[^\]]*\]\((?P<file>[^)\s]+\.md)\)")
# The key of the index-only record: index lines that point at no note.
_INDEX_ONLY = ""
LINES_PART = "lines"

# What Claude's hook tells the agent after a tool call changed memory. It lands in the agent's
# transcript as the hook's additional context, which is where the chat reads it back to draw
# the change's card: format and parse live together here so the two never drift.
AGENT_NOTE_PREFIX = "MEMORY CHANGED"
_AGENT_NOTE_RE = re.compile(
    rf"^{AGENT_NOTE_PREFIX} \((?P<id>[A-Za-z0-9_-]+)\): so far this reply you (?P<summary>.+?)\. The chat shows"
)


def format_agent_note(change: Change) -> str:
    return (
        f"{AGENT_NOTE_PREFIX} ({change['id']}): so far this reply you {change['summary']}. The chat shows "
        "the user this as one card with the changed lines and an Undo button, so mention it in one "
        "short line; do not repeat the text."
    )


def parse_agent_note(text: str) -> tuple[str, str] | None:
    """The change id and summary a hook note names, or None when ``text`` is not one."""
    match = _AGENT_NOTE_RE.match(text.strip())
    return (match.group("id"), match.group("summary")) if match else None


def parse_agent_notes(text: str) -> list[tuple[str, str, str]]:
    """Every change a hook's context names, one per line: ``(id, summary, line)``."""
    notes = []
    for line in text.splitlines():
        parsed = parse_agent_note(line)
        if parsed is not None:
            notes.append((parsed[0], parsed[1], line.strip()))
    return notes


class ChangeNotFoundError(Exception):
    pass


class ChangedSinceError(Exception):
    """A file the change touched was changed again since; undoing would lose that."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(", ".join(names))
        self.names = names


def own_repo_root() -> Path:
    """The repo this package is in: the nearest ancestor holding ``system/scripts``, else cwd."""
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "system" / "scripts").is_dir():
            return ancestor
    return Path.cwd()


def main_checkout(repo_root: Path) -> Path:
    """The checkout a git worktree belongs to (read from its ``.git`` file), else ``repo_root``.

    The memory folder is the main checkout's, even for code running from a worktree.
    """
    try:
        git_file = (repo_root / ".git").read_text(encoding="utf-8")
    except OSError:
        return repo_root
    git_dir = (repo_root / git_file.strip().removeprefix("gitdir:").strip()).resolve()
    try:
        return (
            (git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip())
            .resolve()
            .parent
        )
    except OSError:
        return repo_root


def default_log(environ: Mapping[str, str] = os.environ) -> ChangeLog:
    """The change log of the main checkout, with the env overrides tests and previews use."""
    root = main_checkout(own_repo_root())
    memory_dir = Path(environ.get(MEMORY_DIR_ENV) or root / DEFAULT_MEMORY_DIR)
    changes_dir = Path(environ.get(CHANGES_DIR_ENV) or root / DEFAULT_CHANGES_DIR)
    return ChangeLog(changes_dir=changes_dir, memory_dir=memory_dir)


def _safe_name(raw: str) -> str:
    return "".join(c for c in raw if c.isalnum() or c in "-_") or "unknown"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def diff_snapshots(
    before: Mapping[str, str], after: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Every file whose text differs, with its text before and after (``None``: absent)."""
    return [
        {"name": name, "before": before.get(name), "after": after.get(name)}
        for name in sorted(set(before) | set(after))
        if before.get(name) != after.get(name)
    ]


def merge_files(
    earlier: list[dict[str, Any]], later: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One turn's changes as a whole: each file's text before the first change and after the last.

    A file the turn put back as it found it drops out.
    """
    merged = {item["name"]: dict(item) for item in earlier}
    for item in later:
        if item["name"] in merged:
            merged[item["name"]]["after"] = item["after"]
        else:
            merged[item["name"]] = dict(item)
    return [
        item for _, item in sorted(merged.items()) if item["before"] != item["after"]
    ]


def _line_key(line: str) -> str:
    """The note file an index line belongs to, or the index-only key for any other line."""
    match = _POINTER_LINE.match(line)
    return Path(match["file"]).name if match else _INDEX_ONLY


def _lines(text: str | None) -> list[str]:
    return [] if text is None else text.split("\n")


def _joined(lines: list[str]) -> str | None:
    return "\n".join(lines) if lines else None


def index_lines_by_key(text: str | None) -> dict[str, list[str]]:
    """The index's lines grouped by the note each belongs to (the index-only key for the rest)."""
    grouped: dict[str, list[str]] = {}
    for line in _lines(text):
        grouped.setdefault(_line_key(line), []).append(line)
    return grouped


def replace_key_lines(
    text: str | None, key: str, old: list[str], new: list[str]
) -> str | None:
    """``text`` with ``key``'s lines (which read ``old``) rewritten to ``new``, every other line kept.

    A line ``new`` adds goes where its neighbour in ``old`` stands, or (when ``key`` has no line
    left) at the end, ahead of the final newline.
    """
    current = _lines(text)
    positions = [i for i, line in enumerate(current) if _line_key(line) == key]
    end = len(current) - 1 if current and current[-1] == "" else len(current)
    drops: set[int] = set()
    inserts: dict[int, list[str]] = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=old, b=new, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        drops.update(positions[i1:i2])
        if i1 < len(positions):
            anchor = positions[i1]
        elif positions:
            anchor = positions[-1] + 1
        else:
            anchor = end
        inserts.setdefault(anchor, []).extend(new[j1:j2])
    out: list[str] = []
    for i in range(len(current) + 1):
        out.extend(inserts.get(i, []))
        if i < len(current) and i not in drops:
            out.append(current[i])
    return _joined(out)


def split_by_note(files: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """One tool call's file changes, as each record's part: a note's file and its index lines
    under the note's file name, the rest of the index under the index-only key."""
    parts: dict[str, list[dict[str, Any]]] = {}
    for item in files:
        if item["name"] != INDEX_FILE_NAME:
            parts.setdefault(item["name"], []).append(dict(item))
            continue
        before, after = (
            index_lines_by_key(item["before"]),
            index_lines_by_key(item["after"]),
        )
        for key in sorted(set(before) | set(after)):
            if before.get(key, []) != after.get(key, []):
                parts.setdefault(key, []).append(
                    {
                        "name": INDEX_FILE_NAME,
                        "part": LINES_PART,
                        "before": _joined(before.get(key, [])),
                        "after": _joined(after.get(key, [])),
                    }
                )
    return parts


def note_title(file_name: str) -> str:
    """A note's file name as words: ``likes-tea.md`` is "Likes tea"."""
    words = " ".join(
        w for w in file_name.removesuffix(".md").replace("_", "-").split("-") if w
    )
    return words[:1].upper() + words[1:]


def describe(files: list[dict[str, Any]], note: str | None = None) -> str:
    """One plain phrase naming what changed, e.g. `added the note "Tea" and its index line`.

    ``note`` is the note a per-note record belongs to, whose index line alone reads
    `changed the index line of "Tea"`.
    """
    if note:
        has_file = any(item["name"] == note for item in files)
        index_part = next(
            (item for item in files if item["name"] == INDEX_FILE_NAME), None
        )
        if not has_file and index_part is not None:
            verb = (
                "added"
                if index_part["before"] is None
                else "removed"
                if index_part["after"] is None
                else "changed"
            )
            return f'{verb} the index line of "{note_title(note)}"'
    index_changed = any(item["name"] == INDEX_FILE_NAME for item in files)
    notes = [item for item in files if item["name"] != INDEX_FILE_NAME]
    parts = []
    for item in notes:
        verb = (
            "added"
            if item["before"] is None
            else "deleted"
            if item["after"] is None
            else "changed"
        )
        parts.append(f'{verb} the note "{note_title(item["name"])}"')
    if index_changed:
        parts.append(
            "its index line"
            if len(notes) == 1
            else "the index lines"
            if notes
            else "changed the index"
        )
    if len(parts) > 1 and index_changed:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return ", ".join(parts)


class ChangeLog:
    def __init__(self, changes_dir: Path, memory_dir: Path) -> None:
        self.changes_dir = changes_dir
        self.memory_dir = memory_dir
        self.state_dir = changes_dir / "state"

    # -- the folder and the per-session bookkeeping

    def snapshot(self) -> dict[str, str]:
        """Every file in the memory folder, by name, with its text."""
        files: dict[str, str] = {}
        if not self.memory_dir.is_dir():
            return files
        for path in sorted(self.memory_dir.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                files[path.name] = path.read_text(errors="replace")
        return files

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
        return self.state_dir / f"baseline-{_safe_name(session_id)}.json"

    def _turn_path(self, session_id: str) -> Path:
        return self.state_dir / f"turn-{_safe_name(session_id)}.json"

    def _move_baselines_past(
        self, files: list[dict[str, Any]], *, except_session: str | None
    ) -> None:
        """Make every session's baseline (but one) already hold ``files``' "after" texts."""
        skip = (
            self._baseline_path(except_session) if except_session is not None else None
        )
        for path in self.state_dir.glob("baseline-*.json"):
            if path == skip:
                continue
            baseline = _read_json(path)
            if baseline is None:
                continue
            for item in files:
                if item["after"] is None:
                    baseline.pop(item["name"], None)
                else:
                    baseline[item["name"]] = item["after"]
            _write_json(path, baseline)

    def start_turn(self, session_id: str) -> None:
        _write_json(self._baseline_path(session_id), self.snapshot())
        _write_json(self._turn_path(session_id), {"change_ids": {}})
        self._drop_stale_sessions()

    def _drop_stale_sessions(self) -> None:
        """Remove the bookkeeping of sessions that have not started a turn in ``STALE_SESSION_DAYS``.

        Every baseline is a full copy of the folder, so without this the state grows with every
        chat ever run. A session that comes back simply starts its next turn with a fresh baseline.
        """
        cutoff = time.time() - STALE_SESSION_DAYS * 86400
        for pattern in ("baseline-*.json", "turn-*.json"):
            for path in self.state_dir.glob(pattern):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue

    def _drop_oldest_changes(self) -> None:
        """Keep only the newest ``MAX_CHANGE_RECORDS`` change records (ids sort by time)."""
        records = sorted(self.changes_dir.glob("*.json"), key=lambda path: path.stem)
        for path in records[: max(0, len(records) - MAX_CHANGE_RECORDS)]:
            path.unlink(missing_ok=True)

    # -- recording

    def record_tool_call(
        self,
        session_id: str,
        tool_use_id: str,
        tool_input: object,
        chat_id: str = "",
        agent_name: str = "",
    ) -> list[Change]:
        """Fold what this tool call changed into its turn's records: one per note, one for the index's other lines.

        Returns every record the call touched as it now stands, oldest first; empty when the
        call changed nothing. A record whose changes this turn, taken together, cancel out is
        removed and not returned.
        """
        if not self.mentions_memory(tool_input):
            return []
        current = self.snapshot()
        before = _read_json(self._baseline_path(session_id))
        _write_json(self._baseline_path(session_id), current)
        if before is None:
            return []
        files = diff_snapshots(before, current)
        if not files:
            return []
        self._move_baselines_past(files, except_session=session_id)
        turn = _read_json(self._turn_path(session_id)) or {}
        # A turn file from before records were split per note holds one "change_id"; it is dropped.
        saved_ids = turn.get("change_ids")
        record_ids: dict[str, str] = (
            dict(saved_ids) if isinstance(saved_ids, dict) else {}
        )
        touched: list[Change] = []
        for key, part in split_by_note(files).items():
            change = self._fold(
                key, part, record_ids, session_id, tool_use_id, chat_id, agent_name
            )
            if change is not None:
                touched.append(change)
        _write_json(self._turn_path(session_id), {"change_ids": record_ids})
        return sorted(touched, key=lambda c: c["id"])

    def _fold(
        self,
        key: str,
        part: list[dict[str, Any]],
        record_ids: dict[str, str],
        session_id: str,
        tool_use_id: str,
        chat_id: str,
        agent_name: str,
    ) -> Change | None:
        """Fold one record's part of a tool call into the turn's record for ``key``, writing it.

        Returns the record, or None when the turn's changes to it cancel out (it is removed).
        """
        existing = self._read(record_ids[key]) if key in record_ids else None
        if existing is not None and existing.get("undone_at") is None:
            change = existing
            change["files"] = merge_files(change["files"], part)
            if tool_use_id and tool_use_id not in change["tool_use_ids"]:
                change["tool_use_ids"].append(tool_use_id)
        else:
            now = datetime.now(timezone.utc)
            change = {
                "id": now.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid.uuid4().hex[:6],
                "time": now.isoformat(timespec="seconds"),
                "session_id": session_id,
                "chat_id": chat_id,
                "agent_name": agent_name,
                "note": key or None,
                "tool_use_ids": [tool_use_id] if tool_use_id else [],
                "files": [item for item in part if item["before"] != item["after"]],
                "undone_at": None,
            }
        path = self.changes_dir / f"{change['id']}.json"
        if not change["files"]:
            path.unlink(missing_ok=True)
            record_ids.pop(key, None)
            return None
        change["summary"] = describe(change["files"], key or None)
        is_new = not path.exists()
        _write_json(path, change)
        record_ids[key] = str(change["id"])
        if is_new:
            self._drop_oldest_changes()
        return change

    # -- reading and undoing

    def _path(self, change_id: str) -> Path:
        if not change_id or _safe_name(change_id) != change_id:
            raise ChangeNotFoundError(change_id)
        return self.changes_dir / f"{change_id}.json"

    def _read(self, change_id: str) -> Change | None:
        try:
            raw = _read_json(self._path(change_id))
        except ChangeNotFoundError:
            return None
        if raw is None:
            return None
        raw.setdefault("tool_use_ids", [])
        raw.setdefault("undone_at", None)
        raw.setdefault("note", None)
        raw.setdefault("summary", describe(raw.get("files", []), raw["note"]))
        return raw

    def list_changes(self, session_ids: set[str] | None = None) -> list[Change]:
        """Every change, oldest first, or only those of the given sessions."""
        if not self.changes_dir.is_dir():
            return []
        changes = []
        for path in self.changes_dir.glob("*.json"):
            change = self._read(path.stem)
            if change is None or (
                session_ids is not None and change.get("session_id") not in session_ids
            ):
                continue
            changes.append(change)
        return sorted(changes, key=lambda c: c["id"])

    def get(self, change_id: str) -> Change:
        change = self._read(change_id)
        if change is None:
            raise ChangeNotFoundError(change_id)
        return change

    @staticmethod
    def _holds_now(current: Mapping[str, str], item: dict[str, Any], key: str) -> bool:
        """Whether the folder still holds what ``item`` left there (for an index part, its lines)."""
        if item.get("part") == LINES_PART:
            return index_lines_by_key(current.get(item["name"])).get(key, []) == _lines(
                item["after"]
            )
        return current.get(item["name"]) == item["after"]

    def undo(self, change_id: str, force: bool = False) -> Change:
        """Put back every file the change touched as it was before it.

        Refused with ChangedSinceError when a file no longer holds what the change left there
        (it was changed again later), unless ``force``.
        """
        change = self.get(change_id)
        if change["undone_at"] is not None:
            return change
        current = self.snapshot()
        key = change.get("note") or _INDEX_ONLY
        moved_on = [
            f["name"]
            for f in change["files"]
            if self._holds_now(current, f, key) is False
        ]
        if moved_on and not force:
            raise ChangedSinceError(moved_on)
        for item in change["files"]:
            target = self.memory_dir / Path(item["name"]).name
            if item.get("part") == LINES_PART:
                now = current.get(item["name"])
                restored = replace_key_lines(
                    now,
                    key,
                    index_lines_by_key(now).get(key, []),
                    _lines(item["before"]),
                )
            else:
                restored = item["before"]
            if restored is None:
                target.unlink(missing_ok=True)
            else:
                self.memory_dir.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f".{target.name}.undo")
                tmp.write_text(restored)
                tmp.replace(target)
        # The undo is the user's, not any agent's: no session should see it as a change.
        self._move_baselines_past(
            diff_snapshots(current, self.snapshot()), except_session=None
        )
        change["undone_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write_json(self._path(change_id), change)
        return change
