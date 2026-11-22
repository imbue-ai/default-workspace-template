"""Builders for what a deleted chat leaves on disk, shared by the history sweep's tests."""

from datetime import datetime
from pathlib import Path
from typing import Final

from imbue.mngr.api.preservation import (
    PRESERVATION_MANIFEST_FILENAME,
    PreservationManifest,
    PreservedAgentIdentity,
)
from imbue.mngr.primitives import (
    AgentId,
    AgentName,
    AgentTypeName,
    HostId,
    HostName,
    ProviderInstanceName,
)

from history_sweep.sweep import SESSION_ID_HISTORY_FILENAME, SweepRoots

DEFAULT_ACCOUNT: Final[str] = "acct1"
DEFAULT_PROJECT: Final[str] = "-home-user-workspace"


def write_session(
    roots: SweepRoots, session_id: str, account: str = DEFAULT_ACCOUNT
) -> list[Path]:
    """Claude's files for one session in an account folder: the jsonl, its subagent folder, its file history."""
    account_dir = roots.accounts_root / account
    jsonl = account_dir / "projects" / DEFAULT_PROJECT / f"{session_id}.jsonl"
    subagents = account_dir / "projects" / DEFAULT_PROJECT / session_id / "subagents"
    file_history = account_dir / "file-history" / session_id
    for directory in (jsonl.parent, subagents, file_history):
        directory.mkdir(parents=True, exist_ok=True)
    jsonl.write_text('{"type":"user"}\n')
    (subagents / "agent-1.jsonl").write_text('{"type":"user"}\n')
    (file_history / "backup@v1").write_text("old contents")
    return [jsonl, subagents.parent, file_history]


def write_archive(
    roots: SweepRoots,
    agent_id: str,
    session_ids: list[str],
    preserved_at: datetime,
    has_raw_transcript: bool = True,
) -> Path:
    """A destroyed agent's preserved archive: its raw transcript, session-id history and manifest."""
    archive = roots.host_dir / "preserved" / f"chat--{agent_id}"
    (archive / "logs" / "claude_transcript").mkdir(parents=True)
    (archive / "logs" / "claude_transcript" / "events.jsonl").write_text(
        '{"type":"user"}\n' if has_raw_transcript else ""
    )
    (archive / SESSION_ID_HISTORY_FILENAME).write_text(
        "".join(f"{sid}\n" for sid in session_ids)
    )
    manifest = PreservationManifest(
        preserved_at=preserved_at,
        identity=PreservedAgentIdentity(
            host_id=HostId.generate(),
            host_name=HostName("workspace"),
            provider_name=ProviderInstanceName("local"),
            agent_id=AgentId(agent_id),
            agent_name=AgentName("chat"),
            agent_type=AgentTypeName("claude"),
        ),
        items=(),
    )
    (archive / PRESERVATION_MANIFEST_FILENAME).write_text(manifest.model_dump_json())
    return archive
