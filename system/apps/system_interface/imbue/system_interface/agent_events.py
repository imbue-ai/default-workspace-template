"""Where the UI's agent-lifecycle view comes from: own the observer, or follow it.

``mngr observe`` is single-writer per output directory (it holds an exclusive
``flock`` on ``<host_dir>/observe_lock``), and it is the only thing that probes
real agent lifecycle state. Exactly one process per host may therefore *run* an
observer -- but the events it produces are appended to a plain JSONL file, which
any number of processes may *read*.

That split is what this module models. A system interface serving the workspace
owns the observer (:attr:`AgentEventsMode.OBSERVE`) and consumes its
``--stream-events`` stdout. A *second* system interface on the same host -- the
live-editing preview, or the reveal script's pre-flight boot -- must not try to
start its own: the lock would reject it, the observer would exit seconds into
boot, and the second instance's agent view would silently freeze forever while
every other part of it kept working. Such an instance runs in
:attr:`AgentEventsMode.FOLLOW` instead and reads the same event stream out of
the file the real observer is writing.

The follower is deliberately loud about the one thing that can go wrong for it:
if no process holds the observe lock there is no live event stream to follow, so
it refuses to start rather than tailing a file nobody is appending to. Combined
with :func:`AgentManager.get_agent_events_status` and the ``/api/health``
endpoint that reports it, that is what lets a preview fail its boot health gate
instead of coming up looking fine with a dead lifecycle view.
"""

import fcntl
import json
import os
import threading
from collections.abc import Callable
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.observe import ObserveEventType
from imbue.mngr.api.observe import get_observe_events_path
from imbue.mngr.api.observe import get_observe_lock_path

# How often the follower re-reads the tail of the event file (and re-checks that
# the writing observer is still alive). The observer emits on agent activity and
# every five minutes otherwise, so a one-second poll is far finer-grained than
# the stream it is following and costs a single ``stat`` per tick when idle.
_FOLLOW_POLL_SECONDS: Final[float] = 1.0

# How long ``stop`` waits for the follow thread to notice the stop flag. It only
# ever blocks in the poll wait, so one extra poll interval is ample.
_FOLLOW_JOIN_TIMEOUT_SECONDS: Final[float] = 5.0

# Cheap byte-level pre-filter so seeding a large event file does not run every
# line through ``json.loads``. Only lines containing the literal type token are
# parsed to confirm they really are full-state snapshots.
_FULL_STATE_MARKER: Final[bytes] = b'"AGENTS_FULL_STATE"'


class AgentEventsMode(UpperCaseStrEnum):
    """How this system interface obtains agent lifecycle events."""

    # Run ``mngr observe --stream-events`` and consume its stdout. Requires the
    # observe lock, so at most one instance per host may do this.
    OBSERVE = auto()
    # Read the event file that another process's observer is writing. Takes no
    # lock, so any number of instances may do this alongside the one observer.
    FOLLOW = auto()


class AgentEventsStatus(FrozenModel):
    """Whether the agent-lifecycle event stream is actually feeding this instance.

    ``is_alive`` is the thing a health gate must assert. It is deliberately *not*
    "can I list agents": a one-shot discovery works fine on an instance whose
    lifecycle stream is dead, which is exactly how a broken preview used to pass
    its health check.
    """

    mode: AgentEventsMode = Field(description="How this instance sources lifecycle events")
    is_alive: bool = Field(description="Whether lifecycle events are actually reaching this instance")
    detail: str = Field(description="Human-readable explanation of the current state")


class ObserveStreamUnavailableError(ValueError):
    """Raised when there is no live observer whose event stream could be followed."""


def is_observe_writer_running(events_base_dir: Path) -> bool:
    """Whether some process currently holds the ``mngr observe`` lock for this directory.

    This is the exact liveness signal for the stream a follower reads: the
    observer holds the lock for its whole run, so "the lock is held" means
    "someone is writing the event file", and nothing else does.

    Implemented by trying to take the lock ourselves and immediately dropping it
    again. Failing to take it is the positive answer. The momentary hold in the
    negative case cannot lock out a real observer in any way that matters -- it
    only happens when no observer is running, lasts microseconds, and the caller
    is about to declare the stream dead anyway. The lock file is never created
    here: its absence already means no observer has ever run against this
    directory.
    """
    lock_path = get_observe_lock_path(events_base_dir)
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def _is_full_state_line(raw_line: bytes) -> bool:
    """Whether a raw JSONL line is an ``AGENTS_FULL_STATE`` snapshot event."""
    if _FULL_STATE_MARKER not in raw_line:
        return False
    try:
        data = json.loads(raw_line)
    except json.JSONDecodeError:
        # A torn or malformed line is simply not a usable snapshot; the writer's
        # next one will be. Nothing else in this module depends on it parsing.
        return False
    if not isinstance(data, dict):
        return False
    return data.get("type") == ObserveEventType.AGENTS_FULL_STATE


def find_last_full_state_offset(events_path: Path) -> int | None:
    """Byte offset of the last ``AGENTS_FULL_STATE`` line in the event file, or None.

    A follower must begin folding from a full snapshot, never from a mid-stream
    per-agent update: the fold rebuilds the whole agent set from what it has
    seen, so replaying a lone ``AGENT_STATE`` first would collapse the view to
    that single agent. Starting here and replaying forward reconstructs exactly
    the state the observer's own consumer holds.

    Scans the file once without retaining it, so a long-lived workspace's event
    history costs one sequential read and constant memory.
    """
    last_offset: int | None = None
    offset = 0
    with open(events_path, "rb") as handle:
        for raw_line in handle:
            if _is_full_state_line(raw_line):
                last_offset = offset
            offset += len(raw_line)
    return last_offset


class ObserveEventFollower(MutableModel):
    """Reads the agents event stream that another process's ``mngr observe`` writes.

    Feeds each raw JSONL line to ``on_line`` -- byte-for-byte the lines that
    ``mngr observe --stream-events`` prints on stdout -- so a consumer folds an
    identical event sequence whether it owns the observer or follows one.

    Two invariants make the fold safe:

    - Folding starts at a full-state snapshot. On start we seek to the last one
      in the file and replay forward from there; if the file has none yet, every
      line is dropped until the observer emits one.
    - Only complete lines are forwarded. A snapshot of many agents exceeds the
      atomic-append size, so the tail can legitimately hold a half-written line;
      it is left in place and picked up on a later poll.

    Threading: ``start`` spawns one daemon thread that calls :meth:`poll_once` on
    a fixed interval. Tests drive :meth:`poll_once` directly instead, so the
    class needs no test-only seam.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", frozen=False)

    events_base_dir: Path = Field(frozen=True, description="Directory the observer writes its events under")
    on_line: Callable[[str], None] = Field(frozen=True, description="Sink for each complete event line")
    poll_interval_seconds: float = Field(
        default=_FOLLOW_POLL_SECONDS, frozen=True, description="Seconds between tail reads"
    )

    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # None until something makes the stream unfollowable. "Never started" is not
    # represented here -- that is the owner's fact, not the follower's, and
    # ``AgentManager`` reports it from having no follower at all.
    _failure: str | None = PrivateAttr(default=None)
    _offset: int = PrivateAttr(default=0)
    _is_seeded: bool = PrivateAttr(default=False)
    _has_seen_snapshot: bool = PrivateAttr(default=False)

    def start(self) -> None:
        """Begin following the live observer's event stream.

        Raises :class:`ObserveStreamUnavailableError` when no observer holds the
        lock, because there is then no stream to follow and silently tailing a
        dormant file is the exact failure this class exists to prevent.
        """
        if not is_observe_writer_running(self.events_base_dir):
            raise ObserveStreamUnavailableError(
                f"No 'mngr observe' process holds {get_observe_lock_path(self.events_base_dir)}, so there is "
                "no live agent-lifecycle event stream to follow. The workspace's own system interface "
                "(which runs the observer) does not appear to be up."
            )
        thread = threading.Thread(target=self._follow_loop, name="observe-follower", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Stop the follow thread and wait briefly for it to exit. Idempotent."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=_FOLLOW_JOIN_TIMEOUT_SECONDS)
            self._thread = None

    def is_alive(self) -> bool:
        """Whether this follower is currently receiving the observer's event stream."""
        return self.failure_detail() is None

    def failure_detail(self) -> str | None:
        """Why the stream is not being followed, or None when it is."""
        with self._lock:
            return self._failure

    def poll_once(self) -> None:
        """Advance the follower by one tick: confirm the writer, then drain new lines.

        The writer check comes first so that a stream which died between ticks is
        reported as dead rather than as "no new events", which is precisely the
        distinction a health gate needs.
        """
        if not is_observe_writer_running(self.events_base_dir):
            self._record_failure(
                f"The 'mngr observe' process writing {get_observe_events_path(self.events_base_dir)} exited, "
                "so agent lifecycle events are no longer arriving."
            )
            return
        if not self._is_seeded:
            self._seed()
            return
        self._drain()

    def _follow_loop(self) -> None:
        """Poll until stopped, or until something makes the stream unfollowable."""
        try:
            while not self._stop_event.is_set():
                self.poll_once()
                if not self.is_alive():
                    return
                self._stop_event.wait(timeout=self.poll_interval_seconds)
        except (OSError, ValidationError, json.JSONDecodeError) as e:
            # Reading the file, or folding a line the current schema cannot
            # parse, has failed. The thread is finished either way, so record it
            # as a dead stream instead of dying quietly and leaving the agent
            # view frozen with no explanation.
            _loguru_logger.opt(exception=e).error("Agent lifecycle follower stopped on an unrecoverable error")
            self._record_failure(f"The agent-lifecycle follower stopped: {type(e).__name__}: {e}")

    def _seed(self) -> None:
        """Position at the newest full-state snapshot and replay from it to EOF."""
        events_path = get_observe_events_path(self.events_base_dir)
        self._is_seeded = True
        if not events_path.exists():
            self._offset = 0
            return
        snapshot_offset = find_last_full_state_offset(events_path)
        if snapshot_offset is None:
            # No snapshot has ever been written. Skip the existing history (it
            # cannot be folded from) and wait at the tail; ``_has_seen_snapshot``
            # keeps dropping lines until the observer emits its next snapshot.
            self._offset = events_path.stat().st_size
            return
        self._offset = snapshot_offset
        self._drain()

    def _drain(self) -> None:
        """Forward every complete line appended since the last read."""
        events_path = get_observe_events_path(self.events_base_dir)
        if not events_path.exists():
            return
        size = events_path.stat().st_size
        if size < self._offset:
            # The file shrank, so it was truncated or replaced. Re-seed from the
            # beginning of whatever is there now rather than reading garbage from
            # a stale offset.
            self._offset = 0
            self._has_seen_snapshot = False
        if size == self._offset:
            return
        with open(events_path, "rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read(size - self._offset)
        last_newline_index = chunk.rfind(b"\n")
        if last_newline_index == -1:
            # Only a partial line so far; leave the offset put and re-read it
            # once the writer has finished the line.
            return
        complete = chunk[: last_newline_index + 1]
        self._offset += len(complete)
        # Split on newlines only (not ``splitlines``, whose extra break
        # characters are not line terminators in JSONL).
        for raw_line in complete.split(b"\n"):
            self._forward(raw_line)

    def _forward(self, raw_line: bytes) -> None:
        """Hand one complete line to the sink, once folding has a snapshot to build on."""
        if not raw_line.strip():
            return
        if not self._has_seen_snapshot:
            if not _is_full_state_line(raw_line):
                return
            self._has_seen_snapshot = True
        self.on_line(raw_line.decode("utf-8", errors="replace"))

    def _record_failure(self, detail: str) -> None:
        """Mark the stream dead (first cause wins) and say so loudly."""
        with self._lock:
            if self._failure is not None:
                return
            self._failure = detail
        _loguru_logger.error("Agent lifecycle stream unavailable: {}", detail)
