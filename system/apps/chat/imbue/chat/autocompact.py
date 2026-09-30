import threading
from collections.abc import Callable
from collections.abc import Sequence
from typing import Final

from loguru import logger

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
    mngr launch, and with the mode off it could only do nothing. All
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
        """Perform one pass of autocompact checks across all running chat agents."""
        if self._stop_event.is_set():
            return []
        names = self._list_running_chat_agent_names()
        if not names or not self._is_autocompact_enabled():
            return []

        batch_result = self._check_agents(names)
        if batch_result is not None or len(names) == 1:
            return [batch_result]
        # One chat the command cannot resolve (e.g. stopped since it was listed) fails the
        # whole command, so the rest would go unchecked until it is gone.
        results: list[FinishedProcess | None] = []
        for name in names:
            if self._stop_event.is_set():
                break
            results.append(self.check_agent(name))
        return results

    def check_agent(self, agent_name: str) -> FinishedProcess | None:
        """Run `mngr autocompact run <agent_name>` for a single agent."""
        return self._check_agents([agent_name])

    def _check_agents(self, agent_names: Sequence[str]) -> FinishedProcess | None:
        """Run `mngr autocompact run <agent_names...>`; the result when it exits 0, else None."""
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

        if result.returncode == 0:
            return result
        if result.returncode == 1:
            # mngr's own errors (a target that is not running, say) exit 1.
            logger.debug("Failed to run autocompact for {}: {}", described_agents, result.stderr)
            return None
        logger.warning(
            "Failed to run autocompact for {}: return code {}, stderr: {}",
            described_agents,
            result.returncode,
            result.stderr,
        )
        return None

    def _is_autocompact_enabled(self) -> bool:
        try:
            return self._is_enabled()
        except MngrError as e:
            # The command reads the same config and decides for itself, so an unreadable
            # config costs a launch rather than silently turning compaction off.
            logger.warning("Could not read the autocompact mode from the mngr config, checking anyway: {}", e)
            return True

    def _run_sweep(self) -> None:
        """Background loop executing sweeps on interval until stopped."""
        while not self._stop_event.wait(self._interval_seconds):
            self.sweep()
