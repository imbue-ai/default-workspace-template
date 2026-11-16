"""The raise-only band sweep (docs/system/specs/screen/spec.md section 9.4).

Programs started from the Screen itself (the XFCE menu, a terminal on it) inherit the session's band, which is
the stack's own, so every ten seconds every descendant of ``screen-session`` whose executable is not one of the
session's own is raised to ``AGENT_SUBPROCESS``, and Chromium's processes, wherever they hang in the tree, are
remapped into the shared-browser band the way the browser app's ``oom_retag`` did: Chromium overwrites the
inherited ``oom_score_adj`` once per process with its own gradation (browser 0, gpu and utility 200, renderers
300), and an order-preserving remap into ``[SHARED_BROWSER_FLOOR, SHARED_BROWSER]`` keeps renderers at the ceiling
where shedding one costs one tab. Under pressure the order is therefore programs on the Screen, then Chromium's
renderers, then the stack. The sweep only ever raises, so repeating it writes nothing more.

Chromium's processes spawn, and self-write their values, over the seconds after a launch or a new tab, so the fleet
kicks a short burst of sweeps on those events on top of the periodic cadence.
"""

import threading
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from oom_priority import bands
from oom_priority.proctree import list_descendant_pids
from screen.primitives import CHROMIUM_EXECUTABLE_NAMES

SWEEP_INTERVAL_SECONDS: Final[float] = 10.0
# After a Chromium event, keep sweeping at this cadence until the burst window closes; each event extends it.
BURST_SWEEP_INTERVAL_SECONDS: Final[float] = 1.0
BURST_DURATION_SECONDS: Final[float] = 6.0

# The session's own processes, which keep the stack's band: the group ends together, so none is worth more or
# less than the service that starts it (spec section 9.4).
SESSION_EXECUTABLES: Final[frozenset[str]] = frozenset(
    {
        "startxfce4",
        "xfce4-session",
        "xfwm4",
        "xfce4-panel",
        "xfdesktop",
        "xfsettingsd",
        "xfconfd",
        "dbus-launch",
        "dbus-daemon",
    }
)

_PROC_DIR: Final[Path] = Path("/proc")


class BandWrite(FrozenModel):
    """One ``oom_score_adj`` the sweep raised."""

    pid: int = Field(description="The process")
    old: int = Field(description="What it carried")
    new: int = Field(description="What it was raised to")


class ProcessTreeInterface(MutableModel, ABC):
    """What the sweep reads and writes of the process tree."""

    @abstractmethod
    def descendants(self, pid: int) -> list[int]:
        """Every current descendant of ``pid``."""

    @abstractmethod
    def executable_name(self, pid: int) -> str | None:
        """The executable's name, or None when the process is gone."""

    @abstractmethod
    def read_adj(self, pid: int) -> int | None:
        """The process's ``oom_score_adj``, or None when unreadable."""

    @abstractmethod
    def write_adj(self, pid: int, adj: int) -> bool:
        """Set the process's ``oom_score_adj``; whether it stuck."""


class ProcProcessTree(ProcessTreeInterface):
    """The live tree under ``/proc``."""

    def descendants(self, pid: int) -> list[int]:
        return list_descendant_pids(pid)

    def executable_name(self, pid: int) -> str | None:
        try:
            return (_PROC_DIR / str(pid) / "comm").read_text().strip()
        except OSError:
            return None

    def read_adj(self, pid: int) -> int | None:
        return bands.read_oom_score_adj(pid)

    def write_adj(self, pid: int, adj: int) -> bool:
        return bands.set_oom_score_adj(pid, adj)


@pure
def target_band(executable_name: str, current: int) -> int | None:
    """The band a process under the session should sit in, or None to leave it: Chromium's remap into the
    shared-browser band when it sits below the floor, the agent-subprocess band for anything else that is not the
    session's own and sits below it."""
    if executable_name in CHROMIUM_EXECUTABLE_NAMES:
        if current >= bands.SHARED_BROWSER_FLOOR:
            return None
        return bands.shared_browser_oom_score_adj(current)
    if executable_name in SESSION_EXECUTABLES:
        return None
    if current >= bands.AGENT_SUBPROCESS:
        return None
    return bands.AGENT_SUBPROCESS


def sweep_once(session_pid: int | None, chromium_pids: Sequence[int], tree: ProcessTreeInterface) -> list[BandWrite]:
    """Raise what needs raising under the session and under every Chromium; a process that exited mid-walk is
    skipped, and a value already at or above its band is left alone."""
    candidates: list[int] = []
    if session_pid is not None:
        candidates.extend(tree.descendants(session_pid))
    for chromium_pid in chromium_pids:
        candidates.append(chromium_pid)
        candidates.extend(tree.descendants(chromium_pid))
    writes: list[BandWrite] = []
    for pid in dict.fromkeys(candidates):
        name = tree.executable_name(pid)
        current = tree.read_adj(pid)
        if name is None or current is None:
            continue
        wanted = target_band(name, current)
        if wanted is None:
            continue
        if tree.write_adj(pid, wanted):
            writes.append(BandWrite(pid=pid, old=current, new=wanted))
    return writes


class BandSweeper(MutableModel):
    """Runs the sweep every interval on its own thread, and in a burst after each ``kick``."""

    model_config = {"arbitrary_types_allowed": True}

    session_pid: Callable[[], int | None] = Field(frozen=True, description="The ``screen-session`` program's pid")
    chromium_pids: Callable[[], Sequence[int]] = Field(
        frozen=True, description="The browser processes the fleet launched"
    )
    tree: ProcessTreeInterface = Field(default_factory=ProcProcessTree, frozen=True)
    interval_seconds: float = Field(default=SWEEP_INTERVAL_SECONDS, frozen=True)
    burst_interval_seconds: float = Field(default=BURST_SWEEP_INTERVAL_SECONDS, frozen=True)
    burst_duration_seconds: float = Field(default=BURST_DURATION_SECONDS, frozen=True)
    clock: Callable[[], float] = Field(default=time.monotonic, frozen=True)

    _wake: threading.Event = PrivateAttr(default_factory=threading.Event)
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _burst_deadline: float = PrivateAttr(default=0.0)
    _thread: threading.Thread | None = PrivateAttr(default=None)
    _sweep_count: int = PrivateAttr(default=0)

    @property
    def sweep_count(self) -> int:
        return self._sweep_count

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="screen-band-sweep", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def kick(self) -> None:
        """Chromium is about to spawn processes: open or extend the burst window and wake the thread."""
        with self._lock:
            self._burst_deadline = self.clock() + self.burst_duration_seconds
        self._wake.set()

    def sweep(self) -> list[BandWrite]:
        writes = sweep_once(self.session_pid(), self.chromium_pids(), self.tree)
        self._sweep_count += 1
        if writes:
            logger.debug("Raised {} processes on the Screen into their bands", len(writes))
        return writes

    def _remaining_burst(self) -> float:
        with self._lock:
            return self._burst_deadline - self.clock()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sweep()
            wait = self.burst_interval_seconds if self._remaining_burst() > 0 else self.interval_seconds
            self._wake.wait(timeout=wait)
            self._wake.clear()
