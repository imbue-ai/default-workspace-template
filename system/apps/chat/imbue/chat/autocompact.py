import threading
from collections.abc import Callable
from collections.abc import Sequence
from typing import Final

from loguru import logger
from pydantic import ValidationError

from imbue.chat.agent_discovery import read_plugin_config
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.mngr.errors import MngrError
from imbue.mngr_autocompact.config import AutoCompactPluginConfig
from imbue.mngr_autocompact.config import ContextCompactionMode

_DEFAULT_SWEEP_INTERVAL_SECONDS: Final[float] = 60.0
_DEFAULT_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0
_DEFAULT_MNGR_BINARY: Final[str] = "mngr"


def is_proactive_autocompact_enabled() -> bool:
    """Whether this workspace's mngr config turns on the compaction `mngr autocompact run` performs."""
    config = read_plugin_config("autocompact", AutoCompactPluginConfig)
    return config.mode == ContextCompactionMode.PROACTIVE_TIMER


class ChatAutoCompactor:
    """Schedules periodic context compaction checks for active chat agents.

    Once every interval, runs one `mngr autocompact run <agent names...>` covering
    every chat agent that is currently running, and nothing at all while the
    workspace's mngr config leaves proactive compaction off: each run is a full
    mngr launch, and with the mode off it could only do nothing. When mngr
    rejects the batch with its exit 1, each chat is run on its own instead. All
    collaborators are injectable for unit testing without subprocesses or real
    agents.
    """

    _list_running_chat_agent_names: Callable[[], Sequence[str]]
    _is_enabled: Callable[[], bool]
    _runner: Callable[..., FinishedProcess]
    _mngr_binary: str
    _interval_seconds: float
    _command_timeout_seconds: float
    _stop_event: threading.Event
    _thread: threading.Thread | None

    @classmethod
    def build(
        cls,
        list_running_chat_agent_names: Callable[[], Sequence[str]],
        is_enabled: Callable[[], bool] = is_proactive_autocompact_enabled,
        runner: Callable[..., FinishedProcess] = run_local_command_modern_version,
        mngr_binary: str = _DEFAULT_MNGR_BINARY,
        interval_seconds: float = _DEFAULT_SWEEP_INTERVAL_SECONDS,
        command_timeout_seconds: float = _DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> "ChatAutoCompactor":
        instance = cls.__new__(cls)
        instance._list_running_chat_agent_names = list_running_chat_agent_names
        instance._is_enabled = is_enabled
        instance._runner = runner
        instance._mngr_binary = mngr_binary
        instance._interval_seconds = interval_seconds
        instance._command_timeout_seconds = command_timeout_seconds
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
        """Signal the background sweep to stop and wait for thread termination."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._command_timeout_seconds + 5)
            self._thread = None

    def sweep(self) -> list[FinishedProcess | None]:
        """Perform one pass of autocompact checks across all running chat agents.

        Returns nothing when no launch was made, the batch's outcome alone when it was
        not retried, and one outcome per chat run before a stop when it was; each
        outcome is the finished process when it exited 0, else None.
        """
        if self._stop_event.is_set():
            return []
        names = self._list_running_chat_agent_names()
        if not names:
            return []
        try:
            is_enabled = self._is_enabled()
        except (MngrError, OSError, ValidationError) as e:
            # The command reads the same config and decides for itself, so an unreadable
            # config costs a launch rather than silently turning compaction off. Its exit 1
            # is not retried per chat: mngr's own load of that config fails every chat alike.
            logger.warning("Could not read the autocompact mode from the mngr config, checking anyway: {}", e)
            return [_succeeded_or_none(self._run_autocompact(names))]
        if not is_enabled:
            return []

        batch_result = self._run_autocompact(names)
        # One chat the command cannot resolve (e.g. stopped since it was listed) fails the
        # whole command with mngr's exit 1, so the rest would go unchecked until it is gone.
        # Any other failure (a timeout, being killed, no mngr to launch) would repeat for each chat.
        is_retried_per_chat = (
            len(names) > 1
            and batch_result is not None
            and batch_result.returncode == 1
            and not batch_result.is_timed_out
        )
        if not is_retried_per_chat:
            return [_succeeded_or_none(batch_result)]
        results: list[FinishedProcess | None] = []
        for name in names:
            if self._stop_event.is_set():
                break
            results.append(self.check_agent(name))
        return results

    def check_agent(self, agent_name: str) -> FinishedProcess | None:
        """Run `mngr autocompact run <agent_name>` for a single agent."""
        return _succeeded_or_none(self._run_autocompact([agent_name]))

    def _run_autocompact(self, agent_names: Sequence[str]) -> FinishedProcess | None:
        """Run `mngr autocompact run <agent_names...>`, logging a failure; None when it could not be run."""
        command = [self._mngr_binary, "autocompact", "run", *agent_names]
        described_agents = ", ".join(agent_names)
        try:
            result = self._runner(
                command=command,
                cwd=None,
                is_checked=False,
                timeout=self._command_timeout_seconds,
            )
        except (ProcessError, OSError) as e:
            logger.warning("Failed to run autocompact for {}: {}", described_agents, e)
            return None

        match result.returncode:
            case 0:
                pass
            case 1:
                # mngr's own errors (a target that is not running, say) exit 1.
                logger.debug("Failed to run autocompact for {}: {}", described_agents, result.stderr)
            case _:
                logger.warning(
                    "Failed to run autocompact for {}: return code {}, stderr: {}",
                    described_agents,
                    result.returncode,
                    result.stderr,
                )
        return result

    def _run_sweep(self) -> None:
        """Background loop executing sweeps on interval until stopped."""
        while not self._stop_event.wait(self._interval_seconds):
            self.sweep()


def _succeeded_or_none(result: FinishedProcess | None) -> FinishedProcess | None:
    return result if result is not None and result.returncode == 0 else None
