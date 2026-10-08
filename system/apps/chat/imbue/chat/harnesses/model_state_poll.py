"""One shared poller over the small files and directories each agent's live state is read from.

The model bar's live input is one tiny file per agent, rewritten only when the harness
records a model change -- but the previous design spent a dedicated watchdog observer on
each agent (its own poll thread, dispatcher, emitter, and inotify buffer: four OS threads
per agent, for EVERY agent mngr reports, for as long as the agent exists). On a host with
dozens of accumulated agents that machinery was the chat app's dominant thread count, and
it grew without bound as agents were created over the process's life.

This poller is the bounded replacement: ONE thread stats every known agent's watched paths
each interval and invokes ``on_path_changed`` only for an (agent, purpose) pair whose stamp
(mtime + size) differs from the remembered one. Each agent watches a small set of paths keyed
by purpose -- its ``model_state.json``, and, for a chat's active agent, the chat's
background-task marker directory, whose mtime moves on every marker added or removed -- and
each purpose's stamp is tracked apart, so a change to one never re-fires the other. The
invariant is level-triggered and one sentence long: a purpose is recomputed whenever its
path's observed stamp differs from the remembered stamp. Consequences:

- Resource use is a constant (one thread, one stat per watched path per interval) rather than
  a function of how many agents have ever been seen.
- The path set is re-resolved from ground truth on every pass, so an agent whose harness
  was guessed wrong at first sight (the create path tracks before observe reports the
  harness) is polled at its REAL path on the next pass -- the old per-agent watcher baked
  the guessed path in forever.
- A missed wake needs no special handling: there are no wakes, only the next pass.

What a stamp cannot see -- a marker going stale because its process died, which touches no
file -- is ``on_pass_complete``'s to catch: it runs after every pass.

The callbacks must be cheap and idempotent for spurious invocations (the manager's
recomputes are no-op-guarded), exactly like ``PathWatcher.on_change``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from collections.abc import Mapping
from enum import auto
from pathlib import Path
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum

# One stat per watched path per interval is trivial even under gVisor's elevated syscall cost;
# 1s keeps a harness-driven model change visible on the bar within a second of the write,
# indistinguishable from the inotify latency it replaces.
MODEL_STATE_POLL_INTERVAL_SECONDS: Final[float] = 1.0

# What "the path changed" means: a different (mtime_ns, size) pair, or a flip between
# existing and absent (None). Content-identical rewrites re-fire harmlessly.
_FileStamp = tuple[int, int] | None


class WatchedPathPurpose(UpperCaseStrEnum):
    """What a watched path is read for; each agent watches at most one path per purpose."""

    MODEL_STATE = auto()
    BACKGROUND_TASKS = auto()


class AgentStatePoller:
    """Polls each listed agent's watched paths and reports the (agent, purpose) pairs whose path changed."""

    # Snapshots the current agent -> purpose -> path mapping from ground truth each pass.
    _list_watched_paths: Callable[[], Mapping[str, Mapping[WatchedPathPurpose, Path]]]
    _on_path_changed: Callable[[str, WatchedPathPurpose], None]
    _on_pass_complete: Callable[[], None] | None
    _poll_interval_seconds: float
    _stop_event: threading.Event
    _thread: threading.Thread | None
    _stamp_by_key: dict[tuple[str, WatchedPathPurpose], _FileStamp]

    @classmethod
    def build(
        cls,
        list_watched_paths: Callable[[], Mapping[str, Mapping[WatchedPathPurpose, Path]]],
        on_path_changed: Callable[[str, WatchedPathPurpose], None],
        on_pass_complete: Callable[[], None] | None = None,
        poll_interval_seconds: float = MODEL_STATE_POLL_INTERVAL_SECONDS,
    ) -> "AgentStatePoller":
        self = cls.__new__(cls)
        self._list_watched_paths = list_watched_paths
        self._on_path_changed = on_path_changed
        self._on_pass_complete = on_pass_complete
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread = None
        self._stamp_by_key = {}
        return self

    def start(self) -> None:
        """Begin polling on a background thread. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="agent-state-poll")
        self._thread.start()

    def stop(self) -> None:
        """Stop polling. Idempotent; safe if ``start`` never ran."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def poll_once(self) -> None:
        """Run one pass: re-list the paths, stat each, fire the callback per change, then ``on_pass_complete``.

        Public so tests (and any caller that wants an immediate reconciliation) can drive
        a pass without the thread.
        """
        paths_by_agent = self._list_watched_paths()
        listed_keys = {
            (agent_id, purpose) for agent_id, path_by_purpose in paths_by_agent.items() for purpose in path_by_purpose
        }

        # Forget pairs that are no longer listed, so one that reappears later is
        # re-derived rather than silently assumed unchanged.
        for key in list(self._stamp_by_key):
            if key not in listed_keys:
                del self._stamp_by_key[key]

        for agent_id, path_by_purpose in paths_by_agent.items():
            for purpose, path in path_by_purpose.items():
                key = (agent_id, purpose)
                stamp = _read_file_stamp(path)
                if key in self._stamp_by_key and self._stamp_by_key[key] == stamp:
                    continue
                self._stamp_by_key[key] = stamp
                self._on_path_changed(agent_id, purpose)

        if self._on_pass_complete is not None:
            self._on_pass_complete()

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self._poll_interval_seconds):
            self.poll_once()


def _read_file_stamp(path: Path) -> _FileStamp:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size)
