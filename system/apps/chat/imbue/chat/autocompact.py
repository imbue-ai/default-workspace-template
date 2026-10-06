import threading
import time
from collections.abc import Callable
from collections.abc import Sequence
from typing import Final

from loguru import logger
from pydantic import ValidationError

from imbue.chat.agent_discovery import compact_stale_agents_named
from imbue.chat.agent_discovery import read_plugin_config
from imbue.mngr.errors import MngrError
from imbue.mngr_autocompact.config import AutoCompactPluginConfig
from imbue.mngr_autocompact.config import ContextCompactionMode

_DEFAULT_SWEEP_INTERVAL_SECONDS: Final[float] = 60.0
# A sweep is one config load plus one plugin call over the opted-in chats, about two seconds
# measured; one taking this long is worth noticing before it starts overlapping ticks.
SLOW_SWEEP_WARNING_SECONDS: Final[float] = 30.0
# How long ``stop`` waits for a sweep in flight to finish before giving up on the thread.
_STOP_JOIN_TIMEOUT_SECONDS: Final[float] = 120.0


def is_proactive_autocompact_enabled() -> bool:
    """Whether this workspace's mngr config turns on the idle compaction the sweep requests."""
    config = read_plugin_config("autocompact", AutoCompactPluginConfig)
    return config.mode == ContextCompactionMode.PROACTIVE_TIMER


def _ignore_compaction_request(agent_name: str) -> None:
    pass


def _no_harness_known(agent_name: str) -> str | None:
    return None


class ChatAutoCompactor:
    """Schedules periodic idle compaction for the chats that have it on.

    Once every interval, asks mngr's autocompact plugin, in process, to compact whichever of the
    opted-in running chat agents are stale. It does nothing at all while no chat is opted in (not
    even a config read), and nothing past the config read while the workspace's mngr config leaves
    proactive compaction off. All collaborators are injectable for unit testing without real agents.
    """

    _list_opted_in_chat_agent_names: Callable[[], Sequence[str]]
    _is_enabled: Callable[[], bool]
    _compact: Callable[[Sequence[str]], Sequence[str]]
    _on_compaction_requested: Callable[[str], None]
    _harness_of_agent: Callable[[str], str | None]
    _monotonic: Callable[[], float]
    _interval_seconds: float
    _stop_event: threading.Event
    _sweep_in_progress: threading.Lock
    _thread: threading.Thread | None

    @classmethod
    def build(
        cls,
        list_opted_in_chat_agent_names: Callable[[], Sequence[str]],
        is_enabled: Callable[[], bool] = is_proactive_autocompact_enabled,
        # Compacts whichever of the named agents are stale; returns the names it compacted.
        compact: Callable[[Sequence[str]], Sequence[str]] = compact_stale_agents_named,
        # Told each agent the sweep asked to compact, by name.
        on_compaction_requested: Callable[[str], None] = _ignore_compaction_request,
        # The harness an agent runs, by name, for the log line; None when not known.
        harness_of_agent: Callable[[str], str | None] = _no_harness_known,
        monotonic: Callable[[], float] = time.monotonic,
        interval_seconds: float = _DEFAULT_SWEEP_INTERVAL_SECONDS,
    ) -> "ChatAutoCompactor":
        instance = cls.__new__(cls)
        instance._list_opted_in_chat_agent_names = list_opted_in_chat_agent_names
        instance._is_enabled = is_enabled
        instance._compact = compact
        instance._on_compaction_requested = on_compaction_requested
        instance._harness_of_agent = harness_of_agent
        instance._monotonic = monotonic
        instance._interval_seconds = interval_seconds
        instance._stop_event = threading.Event()
        instance._sweep_in_progress = threading.Lock()
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
        """Signal the background sweep to stop and wait for thread termination."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
            self._thread = None

    def sweep(self) -> list[str]:
        """Run one pass over the opted-in chats; returns the agents compaction was requested for.

        A pass already running (from another thread) makes this one a no-op.
        """
        if self._stop_event.is_set():
            return []
        if not self._sweep_in_progress.acquire(blocking=False):
            logger.debug("Skipped an autocompact sweep: the previous one is still running")
            return []
        try:
            started_at = self._monotonic()
            requested = self._sweep_opted_in_chats()
            elapsed_seconds = self._monotonic() - started_at
        finally:
            self._sweep_in_progress.release()
        if elapsed_seconds > SLOW_SWEEP_WARNING_SECONDS:
            logger.warning("autocompact: slow sweep took {:.1f}s", elapsed_seconds)
        return requested

    def _sweep_opted_in_chats(self) -> list[str]:
        names = list(self._list_opted_in_chat_agent_names())
        if not names:
            return []
        try:
            is_enabled = self._is_enabled()
        except (MngrError, OSError, ValidationError) as e:
            logger.warning("autocompact: could not read the autocompact mode from the mngr config: {}", e)
            return []
        if not is_enabled:
            return []
        try:
            requested = list(self._compact(names))
        except (MngrError, OSError, ValidationError) as e:
            logger.warning("autocompact: the compaction request failed for {}: {}", ", ".join(names), e)
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
            # The thread boundary: the plugin call can raise beyond the errors the sweep expects (a
            # corrupt agent record on a host, say), and an escaping exception would end idle
            # compaction for the life of the process. One bad tick is logged; the next runs as usual.
            try:
                self.sweep()
            except Exception as e:
                logger.opt(exception=e).error("autocompact: sweep failed unexpectedly")
