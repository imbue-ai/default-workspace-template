"""Feed readers for the injected collection script (runs INSIDE the workspace).

This module is injected into ``data/.imbue/analytics/`` next to ``collect.py``
and imported by it there, so it must stay importable with only the script's
own environment: the stdlib plus pydantic. It must never import anything else
from the monorepo.

Every reader is cursor-based and budget-bounded: cursors are byte offsets into
append-only JSONL files (or the last collected git SHA), owned by the runner
and passed in; the shared input budget drains backfills over multiple runs.
Readers return raw parsed records -- redaction of transcript records happens
in ``workspace_redaction`` before anything is emitted.
"""

import json
import logging
import subprocess
import tomllib
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

logger = logging.getLogger("analytics_collect.feeds")

# Where the injected artifacts (script, cursors, run audit) live inside the
# workspace, relative to the workspace root. The whole directory is the user's
# audit surface: the last-run script stays in place and collections.jsonl
# records every run.
ANALYTICS_DIR_RELPATH: Final[str] = "data/.imbue/analytics"
COLLECTIONS_AUDIT_FILENAME: Final[str] = "collections.jsonl"

# Feed source names (must match the runner's protocol module).
TRANSCRIPTS_SOURCE: Final[str] = "transcripts"
CLIENT_ACTIVITY_SOURCE: Final[str] = "client_activity"
SERVICES_SOURCE: Final[str] = "services"
SERVERS_SOURCE: Final[str] = "servers"
GIT_NUMSTAT_SOURCE: Final[str] = "git_numstat"
WORKSPACE_STATE_SOURCE: Final[str] = "workspace_state"
RUN_SUMMARY_SOURCE: Final[str] = "run_summary"

_GIT_LOG_TIMEOUT_SECONDS: Final[float] = 120.0
_GIT_FIELD_SEPARATOR: Final[str] = "\x1f"
_MAX_COMMITS_PER_RUN: Final[int] = 10_000

_README_FILENAME: Final[str] = "README.md"
_README_TEXT: Final[str] = """# Workspace analytics collection

This directory is written by Imbue's analytics collection for explorer-plan
workspaces. Nothing here runs on its own: roughly once an hour while the
workspace is online, the collection service connects over SSH, writes the
then-current collection script into this directory, and runs it here -- so
the script you see is exactly the code that last ran.

- `collect.py` (and the modules under `imbue/analytics/injected/` next to
  it): the collection script. Transcript redaction runs here, inside your
  workspace, before anything leaves it.
- `cursors.json`: where each data feed's last collection stopped.
- `collections.jsonl`: one record per collection run -- when, which sources,
  how much data, which script version.

You can revoke collection at any time by removing the pool key from the
workspace's authorized_keys files. The workspace's sshd reads two of them --
`~/.ssh/authorized_keys` and `/root/.ssh/authorized_keys` -- and the pool key
is listed in both, so remove its line from each. See
`docs/system/analytics-collection.md` at this workspace's root for what is
collected and why.
"""


class WorkspaceFeedError(Exception):
    """Raised when one feed cannot be read; fails that feed only, never the run."""


class FeedOutput(BaseModel):
    """One feed's records for this run plus its advanced cursor."""

    model_config = ConfigDict(frozen=True)

    records: tuple[dict[str, Any], ...] = Field(description="Raw parsed records, in file order")
    cursor: dict[str, Any] = Field(description="The feed's new cursor (JSON-serializable)")
    read_bytes: int = Field(description="Input bytes this feed consumed from the budget")


class JsonlTail(BaseModel):
    """One bounded read from an append-only JSONL file."""

    model_config = ConfigDict(frozen=True)

    lines: tuple[str, ...] = Field(description="Complete lines read, in order")
    new_offset: int = Field(description="Byte offset just past the last complete line consumed")
    read_bytes: int = Field(description="Bytes consumed from the budget")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_jsonl_tail(path: Path, start_offset: int, budget_bytes: int) -> JsonlTail:
    """Read complete lines from ``start_offset``, consuming at most ``budget_bytes``.

    A start offset past the end of the file means the file was truncated or
    replaced; the read restarts from 0 (duplicates are deduped downstream by
    event id). A trailing partial line is left for the next run.
    """
    if budget_bytes <= 0 or not path.is_file():
        return JsonlTail(lines=(), new_offset=start_offset if path.is_file() else 0, read_bytes=0)
    file_size = path.stat().st_size
    effective_offset = start_offset if 0 <= start_offset <= file_size else 0
    with path.open("rb") as handle:
        handle.seek(effective_offset)
        chunk = handle.read(budget_bytes)
    if not chunk:
        return JsonlTail(lines=(), new_offset=effective_offset, read_bytes=0)
    last_newline = chunk.rfind(b"\n")
    if last_newline < 0:
        # No complete line fits the remaining budget; consume nothing so the
        # next run (with a fresh budget) picks the line up whole.
        return JsonlTail(lines=(), new_offset=effective_offset, read_bytes=0)
    complete = chunk[: last_newline + 1]
    lines = tuple(line for line in complete.decode("utf-8", errors="replace").split("\n") if line.strip())
    return JsonlTail(lines=lines, new_offset=effective_offset + len(complete), read_bytes=len(complete))


def _parse_jsonl_records(lines: tuple[str, ...]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipped one unparsable JSONL line")
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
        else:
            logger.warning("Skipped one non-object JSONL line")
    return records


def _read_offset_keyed_feed(
    host_dir: Path,
    relative_paths: list[Path],
    cursor: dict[str, Any],
    budget_bytes: int,
) -> tuple[list[tuple[Path, list[dict[str, Any]]]], dict[str, Any], int]:
    """Tail every file in ``relative_paths`` from its cursor offset, sharing the budget."""
    records_by_path: list[tuple[Path, list[dict[str, Any]]]] = []
    new_cursor: dict[str, Any] = dict(cursor)
    remaining = budget_bytes
    for relative_path in sorted(relative_paths):
        raw_offset = cursor.get(str(relative_path), 0)
        start_offset = raw_offset if isinstance(raw_offset, int) and raw_offset >= 0 else 0
        tail = read_jsonl_tail(host_dir / relative_path, start_offset, remaining)
        remaining -= tail.read_bytes
        new_cursor[str(relative_path)] = tail.new_offset
        if tail.lines:
            records_by_path.append((relative_path, _parse_jsonl_records(tail.lines)))
    return records_by_path, new_cursor, budget_bytes - remaining


def _agent_dirs(host_dir: Path) -> list[Path]:
    agents_root = host_dir / "agents"
    if not agents_root.is_dir():
        return []
    return sorted(path for path in agents_root.iterdir() if path.is_dir())


def read_transcript_feed(host_dir: Path, cursor: dict[str, Any], budget_bytes: int) -> FeedOutput:
    """Raw (pre-redaction) common-transcript records from every agent in the workspace.

    Each record is annotated with the collection-added ``agent_id`` (derived
    from the state-directory path) so transcript analysis can group by agent.
    """
    relative_paths: list[Path] = []
    for agent_dir in _agent_dirs(host_dir):
        for events_file in sorted(agent_dir.glob("events/*/common_transcript/events.jsonl")):
            relative_paths.append(events_file.relative_to(host_dir))
    records_by_path, new_cursor, read_bytes = _read_offset_keyed_feed(host_dir, relative_paths, cursor, budget_bytes)
    annotated: list[dict[str, Any]] = []
    for relative_path, records in records_by_path:
        agent_id = relative_path.parts[1] if len(relative_path.parts) > 1 else ""
        for record in records:
            annotated.append({**record, "agent_id": agent_id})
    return FeedOutput(records=tuple(annotated), cursor=new_cursor, read_bytes=read_bytes)


def read_client_activity_feed(host_dir: Path, cursor: dict[str, Any], budget_bytes: int) -> FeedOutput:
    """UI activity events, with chat text dropped at the source.

    ``message`` events carry the (truncated) chat text for in-workspace
    attribution; the redacted transcripts are the sanctioned channel for text,
    so this feed replaces ``message_text`` with its length before anything is
    emitted.
    """
    relative_paths = [
        events_file.relative_to(host_dir)
        for agent_dir in _agent_dirs(host_dir)
        for events_file in sorted(agent_dir.glob("workspace_layout/events/client_activity/events.jsonl"))
    ]
    records_by_path, new_cursor, read_bytes = _read_offset_keyed_feed(host_dir, relative_paths, cursor, budget_bytes)
    sanitized: list[dict[str, Any]] = []
    for _relative_path, records in records_by_path:
        for record in records:
            message_text = record.pop("message_text", None)
            if message_text is not None:
                record["message_text_length"] = len(str(message_text))
            sanitized.append(record)
    return FeedOutput(records=tuple(sanitized), cursor=new_cursor, read_bytes=read_bytes)


def read_registration_feed(
    host_dir: Path, events_source_name: str, cursor: dict[str, Any], budget_bytes: int
) -> FeedOutput:
    """Service/server registration events from every agent's events directory."""
    relative_paths = [
        events_file.relative_to(host_dir)
        for agent_dir in _agent_dirs(host_dir)
        for events_file in sorted(agent_dir.glob(f"events/{events_source_name}/events.jsonl"))
    ]
    records_by_path, new_cursor, read_bytes = _read_offset_keyed_feed(host_dir, relative_paths, cursor, budget_bytes)
    records = [record for _relative_path, file_records in records_by_path for record in file_records]
    return FeedOutput(records=tuple(records), cursor=new_cursor, read_bytes=read_bytes)


def _run_git_log_numstat(workspace_root: Path, range_argument: str | None) -> str:
    """Raises WorkspaceFeedError when git fails (including an unknown cursor SHA)."""
    command = [
        "git",
        "-C",
        str(workspace_root),
        "log",
        "--reverse",
        f"--format=%x1e%H{_GIT_FIELD_SEPARATOR}%cI",
        "--numstat",
        f"--max-count={_MAX_COMMITS_PER_RUN}",
    ]
    if range_argument:
        command.append(range_argument)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=_GIT_LOG_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WorkspaceFeedError(f"git log failed to run: {e}") from e
    if result.returncode != 0:
        raise WorkspaceFeedError(f"git log exited {result.returncode}: {result.stderr.strip()[:500]}")
    return result.stdout


def read_git_numstat_feed(workspace_root: Path, cursor: dict[str, Any], budget_bytes: int) -> FeedOutput:
    """Per-commit file/line counts (never paths, messages, or authors) since the cursor SHA.

    A cursor SHA that no longer exists (rewritten history) falls back to the
    full log; downstream dedupe by commit SHA absorbs the replay.
    """
    if budget_bytes <= 0 or not (workspace_root / ".git").exists():
        return FeedOutput(records=(), cursor=cursor, read_bytes=0)
    last_sha = cursor.get("last_sha", "")
    try:
        stdout = _run_git_log_numstat(workspace_root, f"{last_sha}..HEAD" if last_sha else None)
    except WorkspaceFeedError:
        if not last_sha:
            raise
        stdout = _run_git_log_numstat(workspace_root, None)
    read_bytes = min(len(stdout.encode("utf-8", errors="replace")), budget_bytes)
    records: list[dict[str, Any]] = []
    newest_sha = last_sha
    for commit_block in stdout.split("\x1e"):
        if not commit_block.strip():
            continue
        block_lines = commit_block.strip("\n").split("\n")
        header_parts = block_lines[0].split(_GIT_FIELD_SEPARATOR)
        if len(header_parts) != 2:
            continue
        sha, committed_at = header_parts
        insertions = 0
        deletions = 0
        file_count = 0
        for numstat_line in block_lines[1:]:
            columns = numstat_line.split("\t")
            if len(columns) != 3:
                continue
            file_count += 1
            insertions += int(columns[0]) if columns[0].isdigit() else 0
            deletions += int(columns[1]) if columns[1].isdigit() else 0
        records.append(
            {
                "timestamp": committed_at,
                "event_id": sha,
                "type": "git_commit",
                "source": GIT_NUMSTAT_SOURCE,
                "file_count": file_count,
                "insertions": insertions,
                "deletions": deletions,
            }
        )
        newest_sha = sha
    new_cursor = {"last_sha": newest_sha} if newest_sha else dict(cursor)
    return FeedOutput(records=tuple(records), cursor=new_cursor, read_bytes=read_bytes)


def _installed_app_names(workspace_root: Path) -> list[str]:
    """App names from the service registry; never labels or URLs (labels are unguessable secrets)."""
    apps_file = workspace_root / "data" / ".state" / "apps.toml"
    if not apps_file.is_file():
        return []
    try:
        parsed = tomllib.loads(apps_file.read_text(encoding="utf-8", errors="replace"))
    except tomllib.TOMLDecodeError:
        logger.warning("apps.toml is unparsable; reporting no installed apps")
        return []
    apps = parsed.get("apps", [])
    if not isinstance(apps, list):
        return []
    return sorted(str(entry.get("name", "")) for entry in apps if isinstance(entry, dict) and entry.get("name"))


def _template_descriptor(workspace_root: Path) -> dict[str, str]:
    descriptor: dict[str, str] = {"template_url": "", "template_branch": "", "template_git_describe": ""}
    parent_toml = workspace_root / "system" / "config" / "parent.toml"
    if parent_toml.is_file():
        try:
            parsed = tomllib.loads(parent_toml.read_text(encoding="utf-8", errors="replace"))
        except tomllib.TOMLDecodeError:
            logger.warning("parent.toml is unparsable; reporting an empty template descriptor")
            parsed = {}
        descriptor["template_url"] = str(parsed.get("url", ""))
        descriptor["template_branch"] = str(parsed.get("branch", ""))
    try:
        describe = subprocess.run(
            ["git", "-C", str(workspace_root), "describe", "--always", "--tags"],
            capture_output=True,
            text=True,
            timeout=15.0,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("git describe failed to run; reporting an empty template describe: %s", e)
        return descriptor
    if describe.returncode == 0:
        descriptor["template_git_describe"] = describe.stdout.strip()
    return descriptor


def _agent_type_counts(host_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for agent_dir in _agent_dirs(host_dir):
        data_file = agent_dir / "data.json"
        agent_type = "unknown"
        if data_file.is_file():
            try:
                agent_type = str(
                    json.loads(data_file.read_text(encoding="utf-8", errors="replace")).get("type", "unknown")
                )
            except json.JSONDecodeError:
                logger.warning("One agent data.json is unparsable; counting its type as unknown")
                agent_type = "unknown"
        counts[agent_type] = counts.get(agent_type, 0) + 1
    return counts


def read_workspace_state_snapshot(workspace_root: Path, host_dir: Path, run_id: str) -> FeedOutput:
    """One stateless snapshot record: sharing state, installed apps, agents, template version.

    Presence booleans only for the sharing files -- their contents (relay
    token, owner email) must never be read into the stream.
    """
    agent_type_counts = _agent_type_counts(host_dir)
    record = {
        "timestamp": utc_now_iso(),
        "event_id": f"workspace-state-{run_id}",
        "type": "workspace_state",
        "source": WORKSPACE_STATE_SOURCE,
        "is_sharing_enabled": (workspace_root / "data" / ".secrets" / "share.env").is_file(),
        "is_owner_email_present": (workspace_root / "data" / ".state" / "share" / "owner_email").is_file(),
        "installed_app_names": _installed_app_names(workspace_root),
        "agent_count": sum(agent_type_counts.values()),
        "agent_type_counts": agent_type_counts,
        **_template_descriptor(workspace_root),
    }
    return FeedOutput(records=(record,), cursor={}, read_bytes=0)


def append_collections_audit_record(
    workspace_root: Path,
    run_id: str,
    script_version: str,
    record_count_by_source: dict[str, int],
    error_by_source: dict[str, str],
    read_bytes: int,
) -> None:
    """Append this run's record to the in-workspace collections.jsonl audit file."""
    analytics_dir = workspace_root / ANALYTICS_DIR_RELPATH
    analytics_dir.mkdir(parents=True, exist_ok=True)
    audit_record = {
        "timestamp": utc_now_iso(),
        "type": "collection_run",
        "event_id": run_id,
        "source": "analytics_collection",
        "script_version": script_version,
        "record_count_by_source": record_count_by_source,
        "error_by_source": error_by_source,
        "read_bytes": read_bytes,
    }
    with (analytics_dir / COLLECTIONS_AUDIT_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(audit_record, sort_keys=True) + "\n")


def write_readme_if_absent(workspace_root: Path) -> None:
    analytics_dir = workspace_root / ANALYTICS_DIR_RELPATH
    analytics_dir.mkdir(parents=True, exist_ok=True)
    readme_path = analytics_dir / _README_FILENAME
    if not readme_path.exists():
        readme_path.write_text(_README_TEXT)
