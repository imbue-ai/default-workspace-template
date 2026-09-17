"""One shared poller over every agent's ``model_state.json``, replacing per-agent watchers.

The model bar's live input is one tiny file per agent, rewritten only when the harness
records a model change -- but the previous design spent a dedicated watchdog observer on
each agent (its own poll thread, dispatcher, emitter, and inotify buffer: four OS threads
per agent, for EVERY agent mngr reports, for as long as the agent exists). On a host with
dozens of accumulated agents that machinery was the chat app's dominant thread count, and
it grew without bound as agents were created over the process's life.

This poller is the bounded replacement: ONE thread stats every known agent's model-state
file each interval and invokes ``on_model_state_changed`` only for agents whose file
stamp (mtime + size) differs from the remembered one. The invariant is level-triggered
and one sentence long: an agent's model choice is recomputed whenever its state file's
observed stamp differs from the remembered stamp. Consequences:

- Resource use is a constant (one thread, one stat per agent per interval) rather than a
  function of how many agents have ever been seen.
- The path set is re-resolved from ground truth on every pass, so an agent whose harness
  was guessed wrong at first sight (the create path tracks before observe reports the
  harness) is polled at its REAL path on the next pass -- the old per-agent watcher baked
  the guessed path in forever.
- A missed wake needs no special handling: there are no wakes, only the next pass.

The callback must be cheap and idempotent for spurious invocations (the manager's
recompute is no-op-guarded), exactly like ``PathWatcher.on_change``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Final

# One stat per agent per interval is trivial even under gVisor's elevated syscall cost;
# 1s keeps a harness-driven model change visible on the bar within a second of the write,
# indistinguishable from the inotify latency it replaces.
MODEL_STATE_POLL_INTERVAL_SECONDS: Final[float] = 1.0

# What "the file changed" means: a different (mtime_ns, size) pair, or a flip between
# existing and absent (None). Content-identical rewrites re-fire harmlessly.
_FileStamp = tuple[int, int] | None


class ModelStatePoller:
    """Polls each listed agent's model-state file and reports the ones whose file changed."""

    # Snapshots the current agent -> model-state-file mapping from ground truth each pass.
    _list_model_state_paths: Callable[[], Mapping[str, Path]]
    _on_model_state_changed: Callable[[str], None]
    _poll_interval_seconds: float
    _stop_event: threading.Event
    _thread: threading.Thread | None
    _stamp_by_agent: dict[str, _FileStamp]

    @classmethod
    def build(
        cls,
        list_model_state_paths: Callable[[], Mapping[str, Path]],
        on_model_state_changed: Callable[[str], None],
        poll_interval_seconds: float = MODEL_STATE_POLL_INTERVAL_SECONDS,
    ) -> "ModelStatePoller":
        self = cls.__new__(cls)
        self._list_model_state_paths = list_model_state_paths
        self._on_model_state_changed = on_model_state_changed
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread = None
        self._stamp_by_agent = {}
        return self

    def start(self) -> None:
        """Begin polling on a background thread. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="model-state-poll")
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
        path_by_agent = self._list_model_state_paths()

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
            self._on_model_state_changed(agent_id)

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self._poll_interval_seconds):
            self.poll_once()


def _read_file_stamp(path: Path) -> _FileStamp:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size)
