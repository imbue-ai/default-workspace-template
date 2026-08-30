"""Tail a codex agent's live rollout and emit UI events.

Built on the shared :class:`~imbue.system_interface.harnesses.transcript_store` scaffolding
with a single lane: codex is one logical session to the UI, and the accumulated timeline
survives rollout rotation (a fresh file per session, and again on resume) because codex
re-serialises history into the new rollout with the same stable ids -- the store dedups the
copies and refreshes each event's source byte range to the newest serialisation, which is
exactly what the on-demand payload reads want.

We tail codex's LIVE rollout directly (the same real-time file codex writes), not the
stream_transcript.sh mirror -- the mirror lags codex by up to its 1s poll, long enough for
the "sending" bubble to visibly flip to "queued" before it reconciles. Which file is live
rotates, so we follow the ``codex_transcript_path`` marker (written by codex's
``UserPromptSubmit`` hook), falling back to the newest ``rollout-*.jsonl`` for web/CLI-only
agents whose hook never fires.

Codex-specific pieces kept here: the effective per-turn model read from ``turn_context``
lines and reflected into the model-bar state file, live suppression of ``user_message``
events (the subscribed ledger owns the live user-turn handoff -- the A3b chip-out-then-turn
ordering -- so the file reader must not broadcast a competing copy; the events stay in the
store for the read paths), and synthetic "Interrupted." results for tool calls a user
interrupt left open (codex never persists one, so the card would spin forever).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger

from imbue.mngr.utils.file_utils import read_json_dict
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.codex.ledger import write_codex_model_state
from imbue.system_interface.harnesses.codex.model import CODEX_STATE_RELATIVE_PATH
from imbue.system_interface.harnesses.codex.session_parser import SOURCE as _SOURCE
from imbue.system_interface.harnesses.codex.session_parser import parse_lines
from imbue.system_interface.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.system_interface.harnesses.events import SpecialEventKind
from imbue.system_interface.harnesses.model import model_state_path
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback
from imbue.system_interface.harnesses.transcript_store import StoreBackedWatcher
from imbue.system_interface.harnesses.transcript_store import iter_line_spans
from imbue.system_interface.harnesses.transcript_store import split_at_last_complete_line

logger = _loguru_logger

_MARKER_RELATIVE = Path("codex_transcript_path")
_SESSIONS_RELATIVE = Path("plugin") / "codex" / "home" / "sessions"

# The store's single lane: codex's whole timeline, across rollout rotations.
_LANE = "main"


def _is_live_suppressed(event: dict[str, Any]) -> bool:
    """Whether a parsed event is suppressed from the LIVE broadcast (kept in the store).

    Only ``user_message`` events: the subscribed ledger owns the live user-turn and emits it
    in the A3b ordered handoff (chip out, then turn), so the file reader must not broadcast
    a competing copy. The event stays in the store, so the read paths (page-load hydration,
    backfill) serve it.
    """
    return event.get("type") == "user_message"


def read_marker_rollout_path(marker_path: Path) -> Path | None:
    """The absolute rollout path recorded in a codex marker file, or None when the
    marker is absent/empty (the agent has not taken a turn yet)."""
    try:
        raw = marker_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return Path(raw) if raw else None


def newest_rollout_under(sessions_dir: Path) -> Path | None:
    """The most-recently-modified ``rollout-*.jsonl`` under ``sessions_dir``, or None if none.

    The marker file (``codex_transcript_path``) is written by codex's ``UserPromptSubmit``
    hook, which the app-server does NOT fire on a programmatic (web/CLI) ``turn/start`` --
    only on a turn typed into the ``--remote`` TUI. So a web-only codex agent never gets a
    marker and its transcript would render empty. This is the marker-free fallback: with one
    thread per daemon the live rollout is simply the newest rollout file.
    """
    try:
        rollouts = list(sessions_dir.rglob("rollout-*.jsonl"))
    except OSError:
        return None
    if not rollouts:
        return None
    return max(rollouts, key=lambda path: path.stat().st_mtime)


def resolve_active_rollout_path(agent_state_dir: Path) -> Path | None:
    """The live rollout for a codex agent: the hook-written marker if present, else the
    newest rollout on disk. Shared by the watcher and the model resolver so the resolution
    lives in one place (they keep separate read cursors over the file itself, by design)."""
    marker_path = read_marker_rollout_path(agent_state_dir / _MARKER_RELATIVE)
    if marker_path is not None:
        return marker_path
    return newest_rollout_under(codex_sessions_dir(agent_state_dir))


def codex_sessions_dir(agent_state_dir: Path) -> Path:
    """The stable dir every rollout (across rotation) lives under -- what a recursive
    watch targets."""
    return agent_state_dir / _SESSIONS_RELATIVE


class CodexSessionWatcher(StoreBackedWatcher):
    """Watches a codex agent's raw rollout file and emits parsed UI events."""

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "CodexSessionWatcher":
        """Build from the agent record. Codex needs only the state dir: its rollout lives
        under the per-agent CODEX_HOME there, so ``claude_config_dir`` is never read."""
        agent_state_dir = agent_info.agent_state_dir
        self = cls.__new__(cls)
        self._init_store_watcher(agent_info.id, on_events)
        self._marker_path = agent_state_dir / _MARKER_RELATIVE
        self._sessions_dir = codex_sessions_dir(agent_state_dir)
        # The rollout currently tailed (rotation = the marker points elsewhere) and the
        # bytes of it consumed through the last complete line.
        self._current_path: Path | None = None
        self._byte_offset = 0
        # GLOBAL monotonic line counter for synthetic event ids (an event_msg user_message
        # has no codex id). Never reset -- keeps ids unique ACROSS rollout files so a
        # resume's line 5 cannot collide with the prior file's line 5.
        self._line_index = 0
        # call_id -> tool_name, so a function_call_output can recover its tool name from
        # the earlier function_call. Persists across files (a resume re-serialises them).
        self._tool_name_by_call_id: dict[str, str] = {}
        # The EFFECTIVE per-turn model/effort, updated from each rollout ``turn_context``
        # line: the model the turn actually RAN on. Reflected into the model-bar state file
        # so a framework fallback shows in the bar rather than the selected model lying.
        self._turn_state: dict[str, Any] = {}
        self._model_state_path = model_state_path(agent_state_dir, CODEX_STATE_RELATIVE_PATH)
        return self

    # -- base hooks -----------------------------------------------------------------------

    def _watch_paths(self) -> tuple[Path, ...]:
        # CODEX_HOME (the parent of ``sessions/``), recursively, so an append to whichever
        # rollout is live wakes the loop immediately, with the poll as the safety net.
        return (self._sessions_dir.parent,)

    def _filter_broadcast(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [event for event in events if not _is_live_suppressed(event)]

    def _refresh_locked(self) -> None:
        """Bring the store up to date with the live rollout, following rotation."""
        target = self._resolve_active_rollout()
        if target is None:
            return
        if target != self._current_path:
            # First resolution or rotation (resume -> new rollout). Tail the new file from
            # its start. The line counter, tool-name map, and store all persist, so a
            # resume's re-serialised history (same codex ids) dedups against what we
            # already hold -- with each event's source range refreshed to the live file.
            self._current_path = target
            self._byte_offset = 0

        try:
            size = target.stat().st_size
        except OSError:
            # The marker points at a not-yet-created file; retry next cycle.
            return
        # Rollouts are append-only; a shrink is unexpected. Re-read from the start -- the
        # store's id-based dedup drops the re-read copies.
        if size < self._byte_offset:
            self._byte_offset = 0
        if size == self._byte_offset:
            return

        try:
            with target.open("rb") as f:
                f.seek(self._byte_offset)
                raw = f.read()
        except OSError:
            logger.debug("codex watcher: failed to read {}", target)
            return

        complete, _fragment = split_at_last_complete_line(raw)
        if not complete:
            return
        for byte_offset, byte_len, line_bytes in iter_line_spans(complete, self._byte_offset):
            # Every physical line consumes an index (even blanks/skips) so a given line
            # always maps to the same synthetic id across the run.
            idx = self._line_index
            self._line_index += 1
            stripped = line_bytes.decode("utf-8", errors="replace").strip()
            if not stripped:
                continue
            for event in self._adapt_line(stripped, idx):
                self._store.ingest(_LANE, event, (target, byte_offset, byte_len))
                # A user interrupt (turn_aborted) leaves any in-flight tool call with no
                # result -- codex never persists one -- so its card would spin forever.
                # Synthesise a terminal "Interrupted." result for every open call, keyed on
                # the id a real result would use so a real one supersedes it.
                if event.get("type") == SPECIAL_EVENT_TYPE and event.get("kind") == SpecialEventKind.TURN_ABORTED.value:
                    for synthetic in self._interrupt_results(event.get("timestamp", "")):
                        self._store.ingest(_LANE, synthetic)
        self._byte_offset += len(complete)

        # Reflect the effective per-turn model into the model-bar state file, so a
        # framework fallback (turn_context.model differing from the selected setting) shows
        # in the bar. Runs on every refresh (reads included), gated on divergence, so the
        # write is rare and cheap enough to do under the lock -- no callback runs here.
        self._reflect_effective_model(self._turn_state.get("model"), self._turn_state.get("effort"))

    # -- codex plumbing -------------------------------------------------------------------

    def _resolve_active_rollout(self) -> Path | None:
        marker = read_marker_rollout_path(self._marker_path)
        if marker is not None:
            return marker
        return newest_rollout_under(self._sessions_dir)

    def _reflect_effective_model(self, model: Any, effort: Any) -> None:
        """Write the effective per-turn model into ``model_state.json`` when it DIVERGES
        from what the file already holds.

        The file is the harness-neutral model-bar read path; the ledger writes the SELECTED
        settings to it on ``thread/settings/updated``. This writer reflects the EFFECTIVE
        model the rollout records per turn -- so a per-turn framework fallback (over-quota /
        tier downgrade) shows in the bar instead of the selected model lying. It writes only
        on divergence, preserving the file's ``fast`` bit (``turn_context`` carries no
        service tier, so fast is owned by the ledger's selected-settings write).
        """
        if not isinstance(model, str) or not model:
            return
        effective_effort = effort if isinstance(effort, str) and effort else None
        current = read_json_dict(self._model_state_path)
        if current.get("model") == model and current.get("effort") == effective_effort:
            return
        write_codex_model_state(self._model_state_path, model, effective_effort, current.get("fast") is True)

    def _interrupt_results(self, timestamp: str) -> list[dict[str, Any]]:
        """Synthetic terminal tool_results for every tool call still open (no result) in
        the store -- what an interrupt leaves behind. Each is keyed on the same
        ``codex-result-<call_id>`` id a real result would carry, so a real result written
        later supersedes it rather than duplicating."""
        events = self._store.all_events([_LANE])
        matched = {e.get("tool_call_id") for e in events if e.get("type") == "tool_result"}
        results: list[dict[str, Any]] = []
        for event in events:
            if event.get("type") != "assistant_message":
                continue
            for tool_call in event.get("tool_calls") or ():
                call_id = tool_call.get("tool_call_id")
                if not call_id or call_id in matched:
                    continue
                matched.add(call_id)
                results.append(
                    {
                        "timestamp": timestamp,
                        "type": "tool_result",
                        "event_id": f"codex-result-{call_id}",
                        "source": _SOURCE,
                        "tool_call_id": call_id,
                        "tool_name": self._tool_name_by_call_id.get(call_id, ""),
                        "output": "Interrupted.",
                        "is_error": True,
                        "message_uuid": f"codex-result-{call_id}",
                    }
                )
        return results

    def _adapt_line(self, line: str, line_index: int) -> list[dict[str, Any]]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            # The rollout is codex-owned state, so a line we cannot parse is real
            # corruption rather than a shape to tolerate quietly: warn so it is visible,
            # and skip the line so the rest of the transcript still renders.
            logger.warning("codex watcher: skipping malformed rollout line {}: {}", line_index, exc)
            return []
        if not isinstance(record, dict):
            return []
        return parse_lines(record, line_index, self._tool_name_by_call_id, self._turn_state)
