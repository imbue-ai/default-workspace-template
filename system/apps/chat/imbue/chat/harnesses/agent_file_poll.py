"""One shared poller over one small file per agent, replacing per-agent watchers.

The manager runs two: one over every agent's ``model_state.json`` (the model bar's live input)
and one over every Claude agent's ``compacting`` marker (the compaction status). Each is one
tiny file per agent, rewritten rarely -- but the previous design spent a dedicated watchdog
observer on each agent (its own poll thread, dispatcher, emitter, and inotify buffer: four OS
threads per agent, for EVERY agent mngr reports, for as long as the agent exists). On a host
with dozens of accumulated agents that machinery was the chat app's dominant thread count, and
it grew without bound as agents were created over the process's life.

This poller is the bounded replacement: ONE thread stats every listed agent's file each
interval and invokes ``on_file_changed`` only for agents whose file stamp (mtime + size)
differs from the remembered one. The invariant is level-triggered and one sentence long: an
agent's derived state is recomputed whenever its file's observed stamp differs from the
remembered stamp. Consequences:

- Resource use is a constant (one thread, one stat per agent per interval) rather than a
  function of how many agents have ever been seen.
- The path set is re-resolved from ground truth on every pass, so an agent whose harness
  was guessed wrong at first sight (the create path tracks before observe reports the
  harness) is polled at its REAL path on the next pass -- the old per-agent watcher baked
  the guessed path in forever.
- A missed wake needs no special handling: there are no wakes, only the next pass.

The callback must be cheap and idempotent for spurious invocations (the manager's
recomputes are no-op-guarded), exactly like ``PathWatcher.on_change``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Final

# One stat per agent per interval is trivial even under gVisor's elevated syscall cost;
# 1s keeps a harness-driven write visible within a second, indistinguishable from the
# inotify latency it replaces.
AGENT_FILE_POLL_INTERVAL_SECONDS: Final[float] = 1.0

# What "the file changed" means: a different (mtime_ns, size) pair, or a flip between
# existing and absent (None). Content-identical rewrites re-fire harmlessly.
_FileStamp = tuple[int, int] | None


class AgentFilePoller:
    """Polls each listed agent's file and reports the agents whose file changed."""

    # Snapshots the current agent -> file mapping from ground truth each pass.
    _list_paths: Callable[[], Mapping[str, Path]]
    _on_file_changed: Callable[[str], None]
    _thread_name: str
    _poll_interval_seconds: float
    _stop_event: threading.Event
    _thread: threading.Thread | None
    _stamp_by_agent: dict[str, _FileStamp]

    @classmethod
    def build(
        cls,
        list_paths: Callable[[], Mapping[str, Path]],
        on_file_changed: Callable[[str], None],
        thread_name: str,
        poll_interval_seconds: float = AGENT_FILE_POLL_INTERVAL_SECONDS,
    ) -> "AgentFilePoller":
        self = cls.__new__(cls)
        self._list_paths = list_paths
        self._on_file_changed = on_file_changed
        self._thread_name = thread_name
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread = None
        self._stamp_by_agent = {}
        return self

    def start(self) -> None:
        """Begin polling on a background thread. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name=self._thread_name)
        self._thread.start()

    def stop(self) -> None:
        """Stop polling. Idempotent; safe if ``start`` never ran."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def poll_once(self) -> None:
        """Run one pass: re-list the paths, stat each, and fire the callback per change.

        Public so tests (and any caller that wants an immediate reconciliation) can drive
        a pass without the thread.
        """
        path_by_agent = self._list_paths()

        # Forget agents that are no longer listed, so one that reappears later is
        # re-derived rather than silently assumed unchanged.
        for agent_id in list(self._stamp_by_agent):
            if agent_id not in path_by_agent:
                del self._stamp_by_agent[agent_id]

        for agent_id, path in path_by_agent.items():
            stamp = _read_file_stamp(path)
            if agent_id in self._stamp_by_agent and self._stamp_by_agent[agent_id] == stamp:
                continue
            self._stamp_by_agent[agent_id] = stamp
            self._on_file_changed(agent_id)

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self._poll_interval_seconds):
            self.poll_once()


def _read_file_stamp(path: Path) -> _FileStamp:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size)
