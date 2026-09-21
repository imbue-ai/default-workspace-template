"""Collecting terminals no window shows (docs/system/specs/window-bound-resources.md section 4).

A terminal lives as long as some window on some desktop shows it. The sweeper reads the shell's
windows on an interval, and at once when the shell posts that a window closed, and hands them to
the source, which marks the terminals that have a window and deletes the ones that had one and
have none any more. A shell that cannot be read is a skipped sweep, never an empty desktop.
"""

import threading
from typing import Final

from app_manifest.primitives import AppName
from app_manifest.shell_windows import read_app_window_paths
from loguru import logger
from pydantic import Field, PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel

from terminal_app.errors import TerminalAppError
from terminal_app.primitives import TmuxSessionName
from terminal_app.sessions import TmuxSessionSource

# The safety net behind the shell's close hint: a missed hint costs at most this long.
WINDOW_SWEEP_INTERVAL_SECONDS: Final[float] = 90.0

SWEEP_THREAD_JOIN_SECONDS: Final[float] = 5.0


class WindowSweeper(MutableModel):
    """The sweep thread: one sweep per interval, and one for every hint, never two at once."""

    source: TmuxSessionSource = Field(frozen=True, description="The terminals, which mark and delete themselves")
    shell_url: str = Field(frozen=True, description="The shell's loopback base URL")
    app_name: AppName = Field(frozen=True, description="Whose windows to read")
    interval_seconds: float = Field(frozen=True, description="How long a quiet sweeper waits between sweeps")
    _wake: threading.Event = PrivateAttr(default_factory=threading.Event)
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def request_sweep(self, closed_terminal: TmuxSessionName | None) -> None:
        """A window closed: mark the terminal it showed as window-seen, then sweep now rather than at the next interval.

        The mark is what lets a terminal whose window closed before any sweep observed it be collected at all.
        """
        if closed_terminal is not None and self.source.mark_window_seen(closed_terminal):
            logger.debug("Marked {} window-seen from the shell's close hint", closed_terminal)
        self._wake.set()

    def sweep_once(self) -> list[TmuxSessionName] | None:
        """One sweep: the terminals collected, or None when the shell could not be read and nothing was done."""
        window_paths = read_app_window_paths(self.shell_url, self.app_name)
        if window_paths is None:
            return None
        collected = self.source.sweep_windows(window_paths)
        if collected:
            logger.info("Collected {} terminal(s) no window shows: {}", len(collected), ", ".join(collected))
        return collected

    def start(self) -> None:
        """Start sweeping; the first quiet sweep is one interval away, so a shell still booting is not asked yet."""
        if self._thread is not None:
            return
        thread = threading.Thread(target=self._run, name="terminal-window-sweep", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=SWEEP_THREAD_JOIN_SECONDS)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.interval_seconds)
            self._wake.clear()
            if not self._stop.is_set():
                self._sweep_logging_failures()

    def _sweep_logging_failures(self) -> None:
        # The thread outlives one bad answer from tmux or the store; the next sweep retries.
        try:
            self.sweep_once()
        except TerminalAppError as e:
            logger.opt(exception=e).warning("A window sweep failed; the next one will retry")
