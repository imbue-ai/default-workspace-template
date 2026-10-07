"""One shared poller over every agent's small state files, replacing per-agent watchers.

Two per-agent inputs are polled this way: the model bar's ``model_state.json``, rewritten only
when the harness records a model change, and the agent's ``background_tasks`` marker dir, whose
mtime changes whenever a pending task is recorded or cleared. The previous design spent a
dedicated watchdog observer on each agent's model-state file (its own poll thread, dispatcher,
emitter, and inotify buffer: four OS threads per agent, for EVERY agent mngr reports, for as
long as the agent exists). On a host with dozens of accumulated agents that machinery was the
chat app's dominant thread count, and it grew without bound as agents were created over the
process's life.

This poller is the bounded replacement: ONE thread asks each watch for a stamp per agent each
interval and invokes that watch's ``on_changed`` only for agents whose stamp differs from the
remembered one. The invariant is level-triggered and one sentence long: an agent's derived
value is recomputed whenever its observed stamp differs from the remembered stamp.
Consequences:

- Resource use is a constant (one thread, one stat per agent per watch per interval) rather
  than a function of how many agents have ever been seen.
- The stamps are re-read from ground truth on every pass, so an agent whose harness was guessed
  wrong at first sight (the create path tracks before observe reports the harness) is polled at
  its REAL path on the next pass -- the old per-agent watcher baked the guessed path in forever.
- A missed wake needs no special handling: there are no wakes, only the next pass.

The callbacks must be cheap and idempotent for spurious invocations (the manager's recomputes
are no-op-guarded), exactly like ``PathWatcher.on_change``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from collections.abc import Hashable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final
from typing import NamedTuple

# One stat per agent per interval is trivial even under gVisor's elevated syscall cost;
# 1s keeps a harness-driven model change visible on the bar within a second of the write,
# indistinguishable from the inotify latency it replaces.
AGENT_STATE_POLL_INTERVAL_SECONDS: Final[float] = 1.0

# What "the file changed" means: a different (mtime_ns, size) pair, or a flip between
# existing and absent (None). Content-identical rewrites re-fire harmlessly.
FileStamp = tuple[int, int] | None


class AgentStateWatch(NamedTuple):
    """One per-agent input the poller watches: how to stamp it, and whom to tell when a stamp changes."""

    # Snapshots the current agent -> stamp mapping from ground truth; called once per pass.
    read_stamp_by_agent: Callable[[], Mapping[str, Hashable]]
    on_changed: Callable[[str], None]


class AgentStatePoller:
    """Polls each watch's per-agent stamps and reports the agents whose stamp changed."""

    _watches: tuple[AgentStateWatch, ...]
    _poll_interval_seconds: float
    _stop_event: threading.Event
    _thread: threading.Thread | None
    # One remembered stamp per agent, per watch (indexed like ``_watches``).
    _stamp_by_agent_by_watch: tuple[dict[str, Hashable], ...]

    @classmethod
    def build(
        cls,
        watches: Sequence[AgentStateWatch],
        poll_interval_seconds: float = AGENT_STATE_POLL_INTERVAL_SECONDS,
    ) -> "AgentStatePoller":
        self = cls.__new__(cls)
        self._watches = tuple(watches)
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread = None
        self._stamp_by_agent_by_watch = tuple({} for _ in self._watches)
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
        """Run one pass: re-read every watch's stamps and fire its callback per change.

        Public so tests (and any caller that wants an immediate reconciliation) can drive
        a pass without the thread.
        """
        for watch, remembered in zip(self._watches, self._stamp_by_agent_by_watch):
            stamp_by_agent = watch.read_stamp_by_agent()

            # Forget agents that are no longer listed, so one that reappears later is
            # re-derived rather than silently assumed unchanged.
            for agent_id in list(remembered):
                if agent_id not in stamp_by_agent:
                    del remembered[agent_id]

            for agent_id, stamp in stamp_by_agent.items():
                if agent_id in remembered and remembered[agent_id] == stamp:
                    continue
                remembered[agent_id] = stamp
                watch.on_changed(agent_id)

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self._poll_interval_seconds):
            self.poll_once()


def read_file_stamp(path: Path) -> FileStamp:
    """A path's (mtime_ns, size), or None when it does not exist. A directory's mtime moves on every add or remove."""
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size)
