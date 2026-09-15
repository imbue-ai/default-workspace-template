import threading
from collections.abc import Callable
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Final

from loguru import logger

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version

_DEFAULT_SWEEP_INTERVAL_SECONDS: Final[float] = 60.0
_DEFAULT_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0
_DEFAULT_CHECK_CONCURRENCY: Final[int] = 4
_DEFAULT_MNGR_BINARY: Final[str] = "mngr"


class ChatAutoCompactor:
    """Schedules periodic context compaction checks for active chat agents.

    Runs `mngr autocompact run <agent name>` once every interval for each
    chat agent that is currently running. All collaborators are injectable for
    unit testing without subprocesses or real agents.
    """

    _list_running_chat_agent_names: Callable[[], Sequence[str]]
    _runner: Callable[..., FinishedProcess]
    _mngr_binary: str
    _interval_seconds: float
    _command_timeout_seconds: float
    _max_concurrency: int
    _stop_event: threading.Event
    _thread: threading.Thread | None

    @classmethod
    def build(
        cls,
        list_running_chat_agent_names: Callable[[], Sequence[str]],
        runner: Callable[..., FinishedProcess] = run_local_command_modern_version,
        mngr_binary: str = _DEFAULT_MNGR_BINARY,
        interval_seconds: float = _DEFAULT_SWEEP_INTERVAL_SECONDS,
        command_timeout_seconds: float = _DEFAULT_COMMAND_TIMEOUT_SECONDS,
        max_concurrency: int = _DEFAULT_CHECK_CONCURRENCY,
    ) -> "ChatAutoCompactor":
        instance = cls.__new__(cls)
        instance._list_running_chat_agent_names = list_running_chat_agent_names
        instance._runner = runner
        instance._mngr_binary = mngr_binary
        instance._interval_seconds = interval_seconds
        instance._command_timeout_seconds = command_timeout_seconds
        instance._max_concurrency = max_concurrency
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

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the background sweep to stop and wait for thread termination."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def sweep(self) -> list[FinishedProcess | None]:
        """Perform one pass of autocompact checks across all running chat agents."""
        if self._stop_event.is_set():
            return []
        names = self._list_running_chat_agent_names()
        if not names:
            return []

        results: list[FinishedProcess | None] = []
        with ThreadPoolExecutor(max_workers=self._max_concurrency) as executor:
            for ran, result in executor.map(self._check_agent_for_sweep, names):
                if ran:
                    results.append(result)
        return results

    def _check_agent_for_sweep(self, agent_name: str) -> tuple[bool, FinishedProcess | None]:
        if self._stop_event.is_set():
            return (False, None)
        return (True, self.check_agent(agent_name))

    def check_agent(self, agent_name: str) -> FinishedProcess | None:
        """Run `mngr autocompact run <agent_name>` for a single agent."""
        command = [self._mngr_binary, "autocompact", "run", agent_name]
        try:
            return self._runner(
                command=command,
                cwd=None,
                is_checked=True,
                timeout=self._command_timeout_seconds,
            )
        except (ProcessError, ProcessSetupError, OSError) as e:
            logger.warning("Failed to run autocompact for {}: {}", agent_name, e)
            return None

    def _run_sweep(self) -> None:
        """Background loop executing sweeps on interval until stopped."""
        while not self._stop_event.wait(self._interval_seconds):
            self.sweep()
