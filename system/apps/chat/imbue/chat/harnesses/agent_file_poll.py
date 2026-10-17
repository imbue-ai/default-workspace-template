"""One shared poller over one small file (or directory) per agent.

Each agent's file is tiny and rewritten rarely, and a watchdog observer per agent costs four OS
threads per agent, growing without bound as agents accumulate on a host. Instead, ONE thread
stats every listed agent's file each interval and invokes ``on_file_changed`` only for agents whose file stamp (mtime + size,
and a directory's entry names) differs from the remembered one. The invariant is level-triggered and one sentence long: an
agent's derived state is recomputed whenever its file's observed stamp differs from the
remembered stamp. Consequences:

- Resource use is a constant (one thread, one stat per agent per interval) rather than a
  function of how many agents have ever been seen.
- The path set is re-resolved from ground truth on every pass, so an agent whose harness
  was guessed wrong at first sight (the create path tracks before observe reports the
  harness) is polled at its REAL path on the next pass.
- A missed wake needs no special handling: there are no wakes, only the next pass.

What a stamp cannot see -- a background-task marker going stale because its process died, which
touches no file -- is ``on_pass_complete``'s to catch: it runs after every pass.

The callback must be cheap and idempotent for spurious invocations (the manager's
recomputes are no-op-guarded), exactly like ``PathWatcher.on_change``.
"""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger

# One stat per agent per interval is trivial even under gVisor's elevated syscall cost;
# 1s keeps a harness-driven write visible within a second.
AGENT_FILE_POLL_INTERVAL_SECONDS: Final[float] = 1.0

# What "the file changed" means: a different (mtime_ns, size, entry names) triple, or a flip
# between existing and absent (None). A directory's names are part of it because its mtime is
# too coarse to tell two changes within one tick apart and its size does not move; a file has
# none. Content-identical rewrites re-fire harmlessly.
_FileStamp = tuple[int, int, tuple[str, ...]] | None


class AgentFilePoller:
    """Polls each listed agent's file and reports the agents whose file changed."""

    # Snapshots the current agent -> file mapping from ground truth each pass.
    _list_paths: Callable[[], Mapping[str, Path]]
    _on_file_changed: Callable[[str], None]
    _on_pass_complete: Callable[[], None] | None
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
        on_pass_complete: Callable[[], None] | None = None,
    ) -> "AgentFilePoller":
        self = cls.__new__(cls)
        self._list_paths = list_paths
        self._on_file_changed = on_file_changed
        self._on_pass_complete = on_pass_complete
        self._thread_name = thread_name
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread = None
        self._stamp_by_agent = {}
        return self

    def start(self) -> None:
        """Run one pass now, then poll on a background thread. Idempotent.

        The first pass runs before this returns, so what the files say is known before the
        app starts answering, rather than one interval later.
        """
        if self._thread is not None:
            return
        self.poll_once()
        self._thread = threading.Thread(target=self._run, daemon=True, name=self._thread_name)
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
            self._report_change(agent_id)

        on_pass_complete = self._on_pass_complete
        if on_pass_complete is not None:
            self._call_guarded(on_pass_complete, "the pass-complete callback failed")

    def _report_change(self, agent_id: str) -> None:
        self._call_guarded(lambda: self._on_file_changed(agent_id), f"the change callback failed for agent {agent_id}")

    def _call_guarded(self, callback: Callable[[], None], failure: str) -> None:
        # The thread boundary: an escaping error would end the poll thread for good.
        try:
            callback()
        except Exception as e:
            logger.opt(exception=e).error("{}: {}", self._thread_name, failure)

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self._poll_interval_seconds):
            self.poll_once()


def _read_file_stamp(path: Path) -> _FileStamp:
    try:
        stat_result = path.stat()
        names = tuple(sorted(os.listdir(path))) if stat.S_ISDIR(stat_result.st_mode) else ()
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size, names)
