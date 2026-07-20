"""Tail a codex agent's TUI log for the sse-event "generating now" heartbeat.

Codex's lifecycle state (RUNNING/WAITING) does NOT reliably indicate generation:
in code mode it keeps a JS cell alive, so it reads RUNNING while idle. But with
``RUST_LOG=...,codex_otel=info`` (set by mngr_codex on the codex launch) codex writes
``codex.sse_event`` lines to its TUI log on every streamed token / reasoning delta.

This watcher tails that log and records the wall-clock time of the last *generating*
delta. The activity tracker reads it as "the model is producing output right now" to
drive the "Thinking..." indicator for codex, instead of the unreliable lifecycle.

Deliberately simpler than the session watcher: it parses nothing and keeps no event
list -- it only tracks a single timestamp. It polls (no watchdog): 1s granularity is
fine for a status dot, and the poll doubles as the expiry tick (a recompute each
cycle lets "Thinking..." clear once deltas stop arriving).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from imbue.system_interface.watcher_common import POLL_INTERVAL_SECONDS

# The dedicated TUI log mngr_codex points codex's ``log_dir`` at (see mngr_codex
# codex_config.TUI_LOG_DIR_NAME / TUI_LOG_FILENAME -- kept in sync as a cross-repo
# contract), relative to the agent state dir.
_TUI_LOG_RELATIVE = Path("plugin") / "codex" / "home" / "tui_log" / "codex-tui.log"

# The line marker + the sse ``event.kind`` values that mean the model is actively
# producing output (streaming answer text, reasoning, or a tool call's args). Turn
# boundaries (``response.completed`` etc.) are intentionally excluded -- they are not
# "generating".
_HEARTBEAT_MARKER = "codex.sse_event"
_GENERATING_KINDS: tuple[str, ...] = (
    "response.output_text.delta",
    "response.reasoning_text.delta",
    "response.reasoning_summary_text.delta",
    "response.custom_tool_call_input.delta",
    "response.function_call_arguments.delta",
)


def _line_is_generating_delta(line: str) -> bool:
    if _HEARTBEAT_MARKER not in line:
        return False
    return any(kind in line for kind in _GENERATING_KINDS)


class CodexHeartbeatWatcher:
    """Watches a codex agent's TUI log and tracks the last generating-delta time."""

    def __init__(
        self,
        agent_id: str,
        agent_state_dir: Path,
        on_heartbeat: Callable[[str, float | None], None],
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._agent_id = agent_id
        self._log_path = agent_state_dir / _TUI_LOG_RELATIVE
        self._on_heartbeat = on_heartbeat
        self._now = now_fn
        self._last_generating_at: float | None = None
        self._byte_offset = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name=f"codex-heartbeat-{self._agent_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def last_generating_at(self) -> float | None:
        """Monotonic time of the most recent generating delta, or None if never seen."""
        return self._last_generating_at

    def _run(self) -> None:
        # Seed offset from whatever already exists (history), then poll for appends.
        self._consume_new_lines()
        while not self._stop_event.wait(timeout=POLL_INTERVAL_SECONDS):
            self._consume_new_lines()
            # Fire every cycle -- even with no new deltas -- so the tracker re-derives
            # and "Thinking..." expires once generation stops.
            self._on_heartbeat(self._agent_id, self._last_generating_at)

    def _consume_new_lines(self) -> None:
        try:
            size = self._log_path.stat().st_size
        except OSError:
            return  # log not written yet (agent hasn't started / no generation)
        if size < self._byte_offset:
            # Truncation/rotation (e.g. a restart recreated the file): re-read from 0.
            self._byte_offset = 0
        if size == self._byte_offset:
            return
        try:
            with self._log_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self._byte_offset)
                chunk = f.read()
                self._byte_offset = f.tell()
        except OSError as e:
            logger.debug("codex heartbeat: read failed for {}: {}", self._log_path, e)
            return
        for line in chunk.splitlines():
            if _line_is_generating_delta(line):
                self._last_generating_at = self._now()
