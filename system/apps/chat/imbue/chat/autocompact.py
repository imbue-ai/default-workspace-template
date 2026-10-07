import threading
import time
from collections.abc import Callable
from collections.abc import Sequence
from typing import Final

from loguru import logger
from pydantic import ValidationError

from imbue.chat.agent_discovery import compact_stale_agents_if_enabled
from imbue.mngr.errors import MngrError
from imbue.mngr_autocompact.manager import compact_stale_agents_by_name

_DEFAULT_SWEEP_INTERVAL_SECONDS: Final[float] = 60.0
# A slow sweep delays the next tick, so one taking this long is logged.
SLOW_SWEEP_WARNING_SECONDS: Final[float] = 30.0
# The plugin call has no timeout, so ``stop`` does not wait out a sweep in flight; the thread is a daemon.
_STOP_JOIN_TIMEOUT_SECONDS: Final[float] = 5.0


def _compact_stale_opted_in_agents(names: Sequence[str]) -> list[str]:
    return compact_stale_agents_if_enabled(names, compact_by_name=compact_stale_agents_by_name)


def _ignore_compaction_request(agent_name: str) -> None:
    pass


def _no_harness_known(agent_name: str) -> str | None:
    return None


class ChatAutoCompactor:
    """Schedules periodic idle compaction for the chats that have it on, through mngr's autocompact plugin in process."""

    _list_opted_in_chat_agent_names: Callable[[], Sequence[str]]
    _compact: Callable[[Sequence[str]], Sequence[str]]
    _on_compaction_requested: Callable[[str], None]
    _harness_of_agent: Callable[[str], str | None]
    _monotonic: Callable[[], float]
    _interval_seconds: float
    _stop_event: threading.Event
    _thread: threading.Thread | None

    @classmethod
    def build(
        cls,
        list_opted_in_chat_agent_names: Callable[[], Sequence[str]],
        # Compacts whichever of the named agents are stale when the mngr config allows it; returns
        # the names it compacted.
        compact: Callable[[Sequence[str]], Sequence[str]] = _compact_stale_opted_in_agents,
        on_compaction_requested: Callable[[str], None] = _ignore_compaction_request,
        # Only for the log line.
        harness_of_agent: Callable[[str], str | None] = _no_harness_known,
        monotonic: Callable[[], float] = time.monotonic,
        interval_seconds: float = _DEFAULT_SWEEP_INTERVAL_SECONDS,
    ) -> "ChatAutoCompactor":
        instance = cls.__new__(cls)
        instance._list_opted_in_chat_agent_names = list_opted_in_chat_agent_names
        instance._compact = compact
        instance._on_compaction_requested = on_compaction_requested
        instance._harness_of_agent = harness_of_agent
        instance._monotonic = monotonic
        instance._interval_seconds = interval_seconds
        instance._stop_event = threading.Event()
        instance._thread = None
        return instance

    def start(self) -> None:
        """Start the background sweep thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._run_sweep,
            daemon=True,
            name="chat-autocompact-sweep",
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Signal the background sweep to stop, waiting briefly for the thread to end."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
            if self._thread.is_alive():
                logger.debug("autocompact: the sweep thread is still running a sweep; leaving it to exit")
            self._thread = None

    def sweep(self) -> list[str]:
        """Run one pass over the opted-in chats; returns the agents compaction was requested for."""
        if self._stop_event.is_set():
            return []
        started_at = self._monotonic()
        requested = self._sweep_opted_in_chats()
        elapsed_seconds = self._monotonic() - started_at
        if elapsed_seconds > SLOW_SWEEP_WARNING_SECONDS:
            logger.warning("autocompact: slow sweep took {:.1f}s", elapsed_seconds)
        return requested

    def _sweep_opted_in_chats(self) -> list[str]:
        names = list(self._list_opted_in_chat_agent_names())
        if not names:
            return []
        try:
            requested = list(self._compact(names))
        except (MngrError, OSError, ValidationError) as e:
            logger.warning("autocompact: could not request compaction for {}: {}", ", ".join(names), e)
            return []
        for name in requested:
            self._on_compaction_requested(name)
            harness = self._harness_of_agent(name)
            if harness is None:
                logger.info("autocompact: requested agent={}", name)
            else:
                logger.info("autocompact: requested agent={} harness={}", name, harness)
        return requested

    def _run_sweep(self) -> None:
        """Background loop executing sweeps on interval until stopped."""
        while not self._stop_event.wait(self._interval_seconds):
            # An exception escaping here would end idle compaction for the life of the process.
            try:
                self.sweep()
            except Exception as e:
                logger.opt(exception=e).error("autocompact: sweep failed unexpectedly")
